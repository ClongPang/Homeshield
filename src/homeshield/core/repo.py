"""SQLite 仓储。共享连接上的数据库访问与写事务都经过 WRITE_LOCK。"""
from functools import wraps
import json
import secrets
import sqlite3
import threading
from dataclasses import dataclass

from homeshield.core.errors import DuplicateMessage, ValidationError
from homeshield.core.models import (
    CorrectionLabel, CorrectionRecord, CorrectionStatus, Level, Member, Mode, User, utcnow,
)

WRITE_LOCK = threading.RLock()


def _serialize_repo_access(cls):
    """共享连接不能被 FastAPI 工作线程并发操作,仓储方法统一串行执行。"""
    for name, method in tuple(vars(cls).items()):
        if name.startswith("_") or isinstance(method, (staticmethod, classmethod)) or not callable(method):
            continue

        @wraps(method)
        def serialized(self, *args, _method=method, **kwargs):
            with WRITE_LOCK:
                return _method(self, *args, **kwargs)

        setattr(cls, name, serialized)
    return cls


def _token() -> str:
    return secrets.token_urlsafe(24)


@_serialize_repo_access
class UserRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    @staticmethod
    def _model(row) -> User | None:
        return User(**dict(row)) if row else None

    def get(self, user_id: int) -> User | None:
        return self._model(self.conn.execute("SELECT * FROM user WHERE id=?", (user_id,)).fetchone())

    def get_by_openid(self, openid: str) -> User | None:
        return self._model(self.conn.execute("SELECT * FROM user WHERE openid=?", (openid,)).fetchone())

    def get_by_token(self, token: str) -> User | None:
        return self._model(self.conn.execute("SELECT * FROM user WHERE token=?", (token,)).fetchone())

    def get_or_create(self, openid: str) -> User:
        with WRITE_LOCK, self.conn:
            row = self.conn.execute("SELECT * FROM user WHERE openid=?", (openid,)).fetchone()
            if row:
                return User(**dict(row))
            cur = self.conn.execute(
                "INSERT INTO user(openid,token,created_at) VALUES(?,?,?)",
                (openid, _token(), utcnow()),
            )
            row = self.conn.execute("SELECT * FROM user WHERE id=?", (cur.lastrowid,)).fetchone()
        return User(**dict(row))


@_serialize_repo_access
class FamilyRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create(self, name: str, created_by_user_id: int | None = None) -> int:
        with WRITE_LOCK, self.conn:
            cur = self.conn.execute(
                "INSERT INTO family(name,created_by_user_id,created_at) VALUES(?,?,?)",
                (name, created_by_user_id, utcnow()),
            )
        return int(cur.lastrowid)

    def create_with_creator(self, name: str, creator_name: str, user_id: int) -> int:
        with WRITE_LOCK, self.conn:
            cur = self.conn.execute(
                "INSERT INTO family(name,created_by_user_id,created_at) VALUES(?,?,?)",
                (name, user_id, utcnow()),
            )
            fid = int(cur.lastrowid)
            self.conn.execute(
                "INSERT INTO member(family_id,user_id,name,trusted,created_at) VALUES(?,?,?,?,?)",
                (fid, user_id, creator_name, 1, utcnow()),
            )
        return fid

    def get(self, family_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM family WHERE id=?", (family_id,)).fetchone()
        return dict(row) if row else None

    def count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) c FROM family WHERE disbanded_at IS NULL").fetchone()
        return int(row["c"])

    def list_for_user(self, user_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT f.id,f.name,m.id AS membership_id,m.trusted,m.mute,"
            "(SELECT COUNT(*) FROM member x WHERE x.family_id=f.id AND x.ended_at IS NULL) member_count "
            "FROM member m JOIN family f ON f.id=m.family_id "
            "WHERE m.user_id=? AND m.ended_at IS NULL AND f.disbanded_at IS NULL ORDER BY f.id",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]


@_serialize_repo_access
class MemberRepo:
    def __init__(self, conn: sqlite3.Connection, users: UserRepo):
        self.conn, self.users = conn, users

    def add(
        self, family_id: int, name: str, trusted: bool = False,
        openid: str | None = None, user_id: int | None = None,
    ) -> int:
        if openid:
            user_id = self.users.get_or_create(openid).id
        with WRITE_LOCK, self.conn:
            cur = self.conn.execute(
                "INSERT INTO member(family_id,user_id,name,trusted,created_at) VALUES(?,?,?,?,?)",
                (family_id, user_id, name, int(trusted and user_id is not None), utcnow()),
            )
        return int(cur.lastrowid)

    def _model(self, row) -> Member | None:
        return Member(**dict(row)) if row else None

    def _select(self) -> str:
        return "SELECT m.* FROM member m"

    def get(self, member_id: int) -> Member | None:
        return self._model(self.conn.execute(self._select()+" WHERE m.id=?", (member_id,)).fetchone())

    def list_members(self, family_id: int, active_only: bool = True) -> list[Member]:
        sql = self._select()+" WHERE m.family_id=?"
        if active_only:
            sql += " AND m.ended_at IS NULL"
        sql += " ORDER BY m.id"
        return [self._model(r) for r in self.conn.execute(sql, (family_id,)).fetchall()]

    def list_for_user(self, user_id: int, active_only: bool = True) -> list[Member]:
        sql = self._select()+" JOIN family f ON f.id=m.family_id WHERE m.user_id=?"
        params: list = [user_id]
        if active_only:
            sql += " AND m.ended_at IS NULL AND f.disbanded_at IS NULL"
        sql += " ORDER BY f.id"
        return [self._model(r) for r in self.conn.execute(sql, params).fetchall()]

    def active_members_in_groups(self, family_ids: list[int]) -> list[Member]:
        if not family_ids:
            return []
        marks = ",".join("?" for _ in family_ids)
        sql = self._select()+f" JOIN family f ON f.id=m.family_id WHERE m.family_id IN ({marks}) " \
              "AND m.user_id IS NOT NULL AND m.ended_at IS NULL AND f.disbanded_at IS NULL ORDER BY f.id,m.id"
        return [self._model(r) for r in self.conn.execute(sql, family_ids).fetchall()]

    def set_openid(self, member_id: int, openid: str) -> User:
        user = self.users.get_or_create(openid)
        try:
            with WRITE_LOCK, self.conn:
                cur = self.conn.execute(
                    "UPDATE member SET user_id=? WHERE id=? AND user_id IS NULL AND ended_at IS NULL",
                    (user.id, member_id),
                )
                if cur.rowcount != 1:
                    raise ValidationError("member slot is no longer available")
        except sqlite3.IntegrityError as e:
            raise ValidationError(f"openid already has an active membership in this group: {openid}") from e
        return user

    def set_trust(self, member_id: int, trusted: bool) -> None:
        with WRITE_LOCK, self.conn:
            self.conn.execute(
                "UPDATE member SET trusted=? WHERE id=? AND user_id IS NOT NULL AND ended_at IS NULL",
                (int(trusted), member_id),
            )

    def demote_with_guard(self, member_id: int, family_id: int) -> bool:
        with WRITE_LOCK, self.conn:
            cur = self.conn.execute(
                "UPDATE member SET trusted=0 WHERE id=? AND family_id=? AND trusted=1 AND ended_at IS NULL"
                " AND (SELECT COUNT(*) FROM member WHERE family_id=? AND trusted=1 AND user_id IS NOT NULL"
                " AND ended_at IS NULL AND id<>?)>=1",
                (member_id, family_id, family_id, member_id),
            )
        return cur.rowcount > 0

    def set_mute(self, member_id: int, mute: bool) -> None:
        with WRITE_LOCK, self.conn:
            self.conn.execute("UPDATE member SET mute=? WHERE id=? AND ended_at IS NULL", (int(mute), member_id))

    def rename(self, member_id: int, name: str) -> None:
        with WRITE_LOCK, self.conn:
            cur = self.conn.execute(
                "UPDATE member SET name=? WHERE id=? AND ended_at IS NULL", (name, member_id)
            )
            if cur.rowcount != 1:
                raise ValidationError("member not found")

    def trusted_count(self, family_id: int, exclude_id: int | None = None) -> int:
        sql = "SELECT COUNT(*) c FROM member WHERE family_id=? AND trusted=1 AND user_id IS NOT NULL AND ended_at IS NULL"
        params: list = [family_id]
        if exclude_id is not None:
            sql += " AND id<>?"
            params.append(exclude_id)
        return int(self.conn.execute(sql, params).fetchone()["c"])

    def bound_count(self, family_id: int, exclude_id: int | None = None) -> int:
        sql = "SELECT COUNT(*) c FROM member WHERE family_id=? AND user_id IS NOT NULL AND ended_at IS NULL"
        params: list = [family_id]
        if exclude_id is not None:
            sql += " AND id<>?"
            params.append(exclude_id)
        return int(self.conn.execute(sql, params).fetchone()["c"])


@_serialize_repo_access
class QueryRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def insert(self, user_id: int, memberships: list[Member], content_type: str,
               content: str, msg_id: str | None) -> int:
        try:
            with WRITE_LOCK, self.conn:
                # 查询群快照和成员生命周期共用写锁,不把已退群成员的旧列表写入查询。
                active = self.conn.execute(
                    "SELECT m.id,m.family_id FROM member m JOIN family f ON f.id=m.family_id "
                    "WHERE m.user_id=? AND m.ended_at IS NULL AND f.disbanded_at IS NULL",
                    (user_id,),
                ).fetchall()
                allowed = {(int(r["id"]), int(r["family_id"])) for r in active}
                memberships = [m for m in memberships if (m.id, m.family_id) in allowed]
                if not memberships:
                    raise ValidationError("user has no active group")
                now = utcnow()
                cur = self.conn.execute(
                    "INSERT INTO query(user_id,content_type,content,msg_id,created_at) VALUES(?,?,?,?,?)",
                    (user_id, content_type, content, msg_id, now),
                )
                qid = int(cur.lastrowid)
                for member in memberships:
                    self.conn.execute(
                        "INSERT INTO query_group(query_id,family_id,query_member_id) VALUES(?,?,?)",
                        (qid, member.family_id, member.id),
                    )
        except sqlite3.IntegrityError as e:
            raise DuplicateMessage(msg_id or "") from e
        return qid

    def exists_by_msg_id(self, msg_id: str) -> dict | None:
        row = self.conn.execute("SELECT id,user_id FROM query WHERE msg_id=?", (msg_id,)).fetchone()
        return dict(row) if row else None

    def get(self, query_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM query WHERE id=?", (query_id,)).fetchone()
        return dict(row) if row else None

    def groups(self, query_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT qg.family_id,qg.query_member_id,f.name,f.disbanded_at FROM query_group qg "
            "JOIN family f ON f.id=qg.family_id WHERE qg.query_id=? ORDER BY qg.family_id",
            (query_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def count(self, family_id: int, since: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) c FROM query_group qg JOIN query q ON q.id=qg.query_id "
            "WHERE qg.family_id=? AND q.created_at>=?", (family_id, since),
        ).fetchone()
        return int(row["c"])


@_serialize_repo_access
class VerdictRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def insert(self, query_id: int, level: Level, cited_ids: list[str], features_snapshot: list[dict],
               reason: str, reply: str, latency_ms: int, mode: Mode) -> int:
        with WRITE_LOCK, self.conn:
            cur = self.conn.execute(
                "INSERT INTO verdict(query_id,level,cited_ids,features,reason,reply,latency_ms,mode,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (query_id, level.value, json.dumps(cited_ids), json.dumps(features_snapshot, ensure_ascii=False),
                 reason, reply, latency_ms, mode.value, utcnow()),
            )
        return int(cur.lastrowid)

    def get(self, verdict_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM verdict WHERE id=?", (verdict_id,)).fetchone()
        return dict(row) if row else None

    def get_with_context(self, verdict_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT v.id,v.query_id,v.level,v.reply,v.created_at,q.user_id,q.content "
            "FROM verdict v JOIN query q ON q.id=v.query_id WHERE v.id=?", (verdict_id,),
        ).fetchone()
        return dict(row) if row else None

    def count_dangerous(self, family_id: int, since: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT a.verdict_id) c FROM alert a JOIN member m ON m.id=a.membership_id "
            "JOIN verdict v ON v.id=a.verdict_id WHERE m.family_id=? AND a.delivered_at>=? "
            "AND v.level='dangerous'", (family_id, since),
        ).fetchone()
        return int(row["c"])


@_serialize_repo_access
class AlertRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create_for_verdict(self, verdict_id: int, query_id: int) -> dict:
        """写入判定时仍活跃的接收关系,并按 user 聚合收件人。"""
        recipients: dict[int, dict] = {}
        generated_groups: dict[int, str] = {}
        with WRITE_LOCK, self.conn:
            queryer_id = self.conn.execute(
                "SELECT user_id FROM query WHERE id=?", (query_id,)
            ).fetchone()["user_id"]
            rows = self.conn.execute(
                "SELECT m.id membership_id,m.user_id,m.family_id,m.mute,u.openid,u.token,f.name "
                "FROM query_group qg JOIN family f ON f.id=qg.family_id AND f.disbanded_at IS NULL "
                "JOIN member m ON m.family_id=f.id AND m.user_id IS NOT NULL AND m.ended_at IS NULL "
                "JOIN user u ON u.id=m.user_id WHERE qg.query_id=? ORDER BY m.user_id,f.id,m.id",
                (query_id,),
            ).fetchall()
            now = utcnow()
            for r in rows:
                self.conn.execute(
                    "INSERT OR IGNORE INTO alert(verdict_id,membership_id,group_name_at_alert,delivered_at)"
                    " VALUES(?,?,?,?)", (verdict_id,r["membership_id"],r["name"],now),
                )
                generated_groups[r["family_id"]] = r["name"]
                item = recipients.setdefault(r["user_id"], {
                    "user_id":r["user_id"],"openid":r["openid"],"token":r["token"],"groups":[]
                })
                item["groups"].append({"family_id":r["family_id"],"membership_id":r["membership_id"],
                                       "name":r["name"],"mute":bool(r["mute"])})
        query_groups = self.conn.execute(
            "SELECT family_id FROM query_group WHERE query_id=?", (query_id,),
        ).fetchall()
        names = [generated_groups[k] for k in sorted(generated_groups)]
        return {"queryer_id":queryer_id,"query_group_count":len(query_groups),
                "generated_group_names":names,"recipients":list(recipients.values())}

    def push_context(self, verdict_id: int, user_id: int) -> dict | None:
        rows = self.conn.execute(
            "SELECT a.membership_id,m.family_id,m.mute,a.group_name_at_alert name,u.openid,u.token "
            "FROM alert a JOIN member old ON old.id=a.membership_id "
            "JOIN family f ON f.id=old.family_id AND f.disbanded_at IS NULL "
            "JOIN member m ON m.family_id=f.id AND m.user_id=? AND m.ended_at IS NULL "
            "JOIN user u ON u.id=m.user_id WHERE a.verdict_id=? AND old.user_id=? AND m.mute=0 "
            "ORDER BY f.id,m.id", (user_id,verdict_id,user_id),
        ).fetchall()
        if not rows:
            return None
        active = self.conn.execute(
            "SELECT COUNT(*) c FROM member m JOIN family f ON f.id=m.family_id "
            "WHERE m.user_id=? AND m.ended_at IS NULL AND f.disbanded_at IS NULL", (user_id,),
        ).fetchone()["c"]
        return {"openid":rows[0]["openid"],"token":rows[0]["token"],"active_group_count":int(active),
                "groups":[{"family_id":r["family_id"],"membership_id":r["membership_id"],"name":r["name"]} for r in rows]}

    def list_for_user_group(self, user_id: int, family_id: int, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT v.id verdict_id,v.level,q.content,MAX(a.delivered_at) delivered_at,"
            "MAX(a.group_name_at_alert) group_name_at_alert FROM alert a "
            "JOIN member old ON old.id=a.membership_id JOIN verdict v ON v.id=a.verdict_id "
            "JOIN query q ON q.id=v.query_id JOIN family f ON f.id=old.family_id "
            "WHERE old.user_id=? AND old.family_id=? AND f.disbanded_at IS NULL "
            "AND EXISTS(SELECT 1 FROM member cur WHERE cur.user_id=? AND cur.family_id=? AND cur.ended_at IS NULL) "
            "GROUP BY v.id ORDER BY v.id DESC LIMIT ?",
            (user_id,family_id,user_id,family_id,limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def access_detail(self, user_id: int, verdict_id: int) -> tuple[dict | None, str | None]:
        detail = self.conn.execute(
            "SELECT v.id verdict_id,v.level,v.reply,v.created_at,q.content FROM verdict v "
            "JOIN query q ON q.id=v.query_id WHERE v.id=? AND v.level='dangerous'", (verdict_id,),
        ).fetchone()
        if not detail:
            return None, None
        had = self.conn.execute(
            "SELECT 1 FROM alert a JOIN member old ON old.id=a.membership_id "
            "WHERE a.verdict_id=? AND old.user_id=? LIMIT 1", (verdict_id,user_id),
        ).fetchone()
        if not had:
            return None, None
        groups = self.conn.execute(
            "SELECT DISTINCT f.id,a.group_name_at_alert name FROM alert a JOIN member old ON old.id=a.membership_id "
            "JOIN family f ON f.id=old.family_id JOIN member cur ON cur.family_id=f.id AND cur.user_id=? "
            "AND cur.ended_at IS NULL WHERE a.verdict_id=? AND old.user_id=? AND f.disbanded_at IS NULL "
            "ORDER BY f.id", (user_id,verdict_id,user_id),
        ).fetchall()
        if groups:
            return {**dict(detail),"group_names":[r["name"] for r in groups]}, None
        active_family = self.conn.execute(
            "SELECT 1 FROM alert a JOIN member old ON old.id=a.membership_id JOIN family f ON f.id=old.family_id "
            "WHERE a.verdict_id=? AND old.user_id=? AND f.disbanded_at IS NULL LIMIT 1", (verdict_id,user_id),
        ).fetchone()
        return None, "membership_ended" if active_family else "group_disbanded"

    def active_family_ids_for_user(self, user_id: int) -> set[int]:
        return {int(r[0]) for r in self.conn.execute(
            "SELECT m.family_id FROM member m JOIN family f ON f.id=m.family_id "
            "WHERE m.user_id=? AND m.ended_at IS NULL AND f.disbanded_at IS NULL", (user_id,),
        ).fetchall()}

    def family_was_recipient(self, verdict_id: int, user_id: int, family_id: int) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM alert a JOIN member m ON m.id=a.membership_id "
            "WHERE a.verdict_id=? AND m.user_id=? AND m.family_id=? LIMIT 1",
            (verdict_id,user_id,family_id),
        ).fetchone() is not None


@_serialize_repo_access
class CorrectionRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def insert(self, verdict_id: int, user_id: int, memberships: list[Member], label: CorrectionLabel,
               note: str, status: CorrectionStatus, decided_by: int | None) -> int:
        now = utcnow()
        with WRITE_LOCK, self.conn:
            existing = self.conn.execute(
                "SELECT id FROM correction WHERE verdict_id=? AND by_user_id=?", (verdict_id,user_id),
            ).fetchone()
            if existing:
                return int(existing["id"])
            cur = self.conn.execute(
                "INSERT INTO correction(verdict_id,by_user_id,label,note,status,decided_by_membership_id,created_at,decided_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (verdict_id,user_id,label.value,note,status.value,decided_by,now,now if status is CorrectionStatus.CONFIRMED else None),
            )
            cid = int(cur.lastrowid)
            for m in memberships:
                self.conn.execute(
                    "INSERT INTO correction_group(correction_id,family_id,by_membership_id) VALUES(?,?,?)",
                    (cid,m.family_id,m.id),
                )
        return cid

    def get(self, correction_id: int) -> CorrectionRecord | None:
        row = self.conn.execute("SELECT * FROM correction WHERE id=?", (correction_id,)).fetchone()
        return self._to_record(row) if row else None

    @staticmethod
    def _to_record(row) -> CorrectionRecord:
        return CorrectionRecord(
            id=row["id"],verdict_id=row["verdict_id"],by_user_id=row["by_user_id"],
            label=CorrectionLabel(row["label"]),note=row["note"],status=CorrectionStatus(row["status"]),
            decided_by_membership_id=row["decided_by_membership_id"],
        )

    def get_by_verdict_and_user(self, verdict_id: int, user_id: int) -> CorrectionRecord | None:
        row = self.conn.execute("SELECT * FROM correction WHERE verdict_id=? AND by_user_id=?",(verdict_id,user_id)).fetchone()
        return self._to_record(row) if row else None

    def get_with_groups(self, correction_id: int) -> tuple[dict | None,list[dict]]:
        row = self.conn.execute("SELECT * FROM correction WHERE id=?",(correction_id,)).fetchone()
        groups = self.conn.execute(
            "SELECT cg.family_id,cg.by_membership_id,m.user_id,f.disbanded_at FROM correction_group cg "
            "JOIN member m ON m.id=cg.by_membership_id JOIN family f ON f.id=cg.family_id WHERE cg.correction_id=?",
            (correction_id,),
        ).fetchall()
        return (dict(row) if row else None,[dict(g) for g in groups])

    def decide(self, correction_id: int, status: CorrectionStatus, decided_by: int) -> None:
        with WRITE_LOCK, self.conn:
            cur = self.conn.execute(
                "UPDATE correction SET status=?,decided_by_membership_id=?,decided_at=? "
                "WHERE id=? AND status='pending'",
                (status.value,decided_by,utcnow(),correction_id),
            )
            if cur.rowcount != 1:
                raise ValidationError("correction already decided")

    def count_group(self, family_id: int, since: int, status: str | None = None, label: str | None = None) -> int:
        sql = ("SELECT COUNT(*) c FROM correction_group cg JOIN correction c ON c.id=cg.correction_id "
               "WHERE cg.family_id=? AND c.decided_at>=?")
        params: list = [family_id,since]
        if status:
            sql += " AND c.status=?"
            params.append(status)
        if label:
            sql += " AND c.label=?"
            params.append(label)
        return int(self.conn.execute(sql,params).fetchone()["c"])

    def list_pending_with_context(self, family_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT c.id,c.label,c.note,c.created_at,submit.name by_name,q.content "
            "FROM correction_group cg JOIN correction c ON c.id=cg.correction_id "
            "JOIN verdict v ON v.id=c.verdict_id JOIN query q ON q.id=v.query_id "
            "JOIN member submit ON submit.id=cg.by_membership_id "
            "WHERE cg.family_id=? AND c.status='pending' ORDER BY c.created_at DESC",
            (family_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def expire_older_than(self, cutoff: int) -> int:
        with WRITE_LOCK, self.conn:
            cur = self.conn.execute(
                "UPDATE correction SET status='rejected',decided_at=? WHERE status='pending' AND created_at<?",
                (utcnow(),cutoff),
            )
        return max(0,cur.rowcount)


CODE_ALPHABET = "2346789ABCDEFGHJKMNPQRSTUVWXYZ"


@_serialize_repo_access
class BindCodeRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create(self, member_id: int, created_by: int | None, ttl_days: int) -> dict:
        now = utcnow()
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        with WRITE_LOCK, self.conn:
            self.conn.execute(
                "INSERT INTO bind_code(code,member_id,created_by,created_at,expires_at) VALUES(?,?,?,?,?)",
                (code,member_id,created_by,now,now+ttl_days*86400),
            )
        return {"code":code,"member_id":member_id,"expires_at":now+ttl_days*86400}

    def claim_and_bind(self, code: str, member_id: int, user_id: int) -> bool:
        """在一个事务内消费邀请码并绑定成员位,任一步失败都保留邀请码。"""
        now = utcnow()
        try:
            with WRITE_LOCK, self.conn:
                row = self.conn.execute(
                    "SELECT bc.id FROM bind_code bc JOIN member m ON m.id=bc.member_id "
                    "JOIN family f ON f.id=m.family_id WHERE UPPER(bc.code)=UPPER(?) "
                    "AND bc.member_id=? AND bc.used_at IS NULL AND bc.expires_at>? "
                    "AND m.user_id IS NULL AND m.ended_at IS NULL AND f.disbanded_at IS NULL",
                    (code,member_id,now),
                ).fetchone()
                if row is None:
                    return False
                bound = self.conn.execute(
                    "UPDATE member SET user_id=? WHERE id=? AND user_id IS NULL AND ended_at IS NULL "
                    "AND EXISTS(SELECT 1 FROM family WHERE family.id=member.family_id AND disbanded_at IS NULL)",
                    (user_id,member_id),
                )
                if bound.rowcount != 1:
                    return False
                claimed = self.conn.execute(
                    "UPDATE bind_code SET used_at=? WHERE id=? AND used_at IS NULL AND expires_at>?",
                    (now,row["id"],now),
                )
                if claimed.rowcount != 1:
                    raise ValidationError("invitation became unavailable")
        except sqlite3.Error as exc:
            raise ValidationError("binding transaction failed") from exc
        return True

    def invalidate_for_member(self, member_id: int) -> None:
        with WRITE_LOCK, self.conn:
            self.conn.execute("UPDATE bind_code SET used_at=? WHERE member_id=? AND used_at IS NULL",(utcnow(),member_id))

    def invalidate_family(self, family_id: int) -> None:
        with WRITE_LOCK, self.conn:
            self.conn.execute(
                "UPDATE bind_code SET used_at=? WHERE used_at IS NULL AND member_id IN "
                "(SELECT id FROM member WHERE family_id=?)", (utcnow(),family_id),
            )

    def peek(self, code: str) -> dict | None:
        row = self.conn.execute(
            "SELECT bc.* FROM bind_code bc JOIN member m ON m.id=bc.member_id "
            "JOIN family f ON f.id=m.family_id WHERE UPPER(bc.code)=UPPER(?) "
            "AND bc.used_at IS NULL AND bc.expires_at>? AND m.ended_at IS NULL AND f.disbanded_at IS NULL",
            (code,utcnow()),
        ).fetchone()
        return dict(row) if row else None

    def latest_active(self, member_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT code,expires_at FROM bind_code WHERE member_id=? AND used_at IS NULL AND expires_at>? ORDER BY id DESC LIMIT 1",
            (member_id,utcnow()),
        ).fetchone()
        return dict(row) if row else None


@dataclass
class Repos:
    conn: sqlite3.Connection
    users: UserRepo
    family: FamilyRepo
    member: MemberRepo
    query: QueryRepo
    verdict: VerdictRepo
    alert: AlertRepo
    correction: CorrectionRepo
    bind_code: BindCodeRepo


def make_repos(conn: sqlite3.Connection) -> Repos:
    users = UserRepo(conn)
    return Repos(conn,users,FamilyRepo(conn),MemberRepo(conn,users),QueryRepo(conn),VerdictRepo(conn),
                 AlertRepo(conn),CorrectionRepo(conn),BindCodeRepo(conn))

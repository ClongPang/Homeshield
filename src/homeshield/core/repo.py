"""仓储层(Repository 模式):SQL 只存在于本文件,领域层只见领域对象。

所有方法小而直白;事务由调用方 `with conn:` 控制。
"""
import json
import secrets
import sqlite3
from dataclasses import dataclass

from homeshield.core.errors import DuplicateMessage, ValidationError
from homeshield.core.models import (
    CorrectionLabel,
    CorrectionRecord,
    CorrectionStatus,
    Level,
    Member,
    Mode,
    utcnow,
)


class FamilyRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create(self, name: str) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO family(name, created_at) VALUES(?,?)", (name, utcnow())
            )
        return int(cur.lastrowid)

    def create_with_creator(self, name: str, creator_name: str, openid: str) -> int:
        """开群 + 创建者成员位(trusted)同一事务:中途失败不留孤儿家庭(占 MAX_FAMILIES 名额)。"""
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO family(name, created_at) VALUES(?,?)", (name, utcnow())
            )
            fid = int(cur.lastrowid)
            self.conn.execute(
                "INSERT INTO member(family_id,name,trusted,openid,token,created_at) VALUES(?,?,?,?,?,?)",
                (fid, creator_name, 1, openid, _new_member_token(), utcnow()),
            )
        return fid

    def get(self, family_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM family WHERE id=?", (family_id,)
        ).fetchone()
        return dict(row) if row else None

    def count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) c FROM family").fetchone()
        return int(row["c"])


def _new_member_token() -> str:
    return secrets.token_urlsafe(16)  # 个人链接凭证,创建即生成


class MemberRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add(
        self, family_id: int, name: str, trusted: bool = False, openid: str | None = None
    ) -> int:
        token = _new_member_token()
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO member(family_id,name,trusted,openid,token,created_at) VALUES(?,?,?,?,?,?)",
                (family_id, name, int(trusted), openid, token, utcnow()),
            )
        return int(cur.lastrowid)

    def _row_to_member(self, row: sqlite3.Row) -> Member:
        return Member(
            id=row["id"],
            family_id=row["family_id"],
            name=row["name"],
            trusted=bool(row["trusted"]),
            openid=row["openid"],
            token=row["token"],
        )

    def get(self, member_id: int) -> Member | None:
        row = self.conn.execute(
            "SELECT * FROM member WHERE id=?", (member_id,)
        ).fetchone()
        return self._row_to_member(row) if row else None

    def get_by_openid(self, openid: str) -> Member | None:
        row = self.conn.execute(
            "SELECT * FROM member WHERE openid=?", (openid,)
        ).fetchone()
        return self._row_to_member(row) if row else None

    def get_by_token(self, token: str) -> Member | None:
        row = self.conn.execute(
            "SELECT * FROM member WHERE token=?", (token,)
        ).fetchone()
        return self._row_to_member(row) if row else None

    def set_openid(self, member_id: int, openid: str) -> None:
        """绑定码流程:把 openid 落到成员位;openid 全局唯一,冲突即换绑他处。"""
        try:
            with self.conn:
                self.conn.execute(
                    "UPDATE member SET openid=? WHERE id=?", (openid, member_id)
                )
        except sqlite3.IntegrityError as e:
            raise ValidationError(f"openid already bound: {openid}") from e

    def list_members(self, family_id: int) -> list[Member]:
        rows = self.conn.execute(
            "SELECT * FROM member WHERE family_id=? ORDER BY id", (family_id,)
        ).fetchall()
        return [self._row_to_member(r) for r in rows]

    def set_trust(self, member_id: int, trusted: bool) -> None:
        """信任位翻转(仅信任成员可操作,校验在服务层)。"""
        with self.conn:
            self.conn.execute(
                "UPDATE member SET trusted=? WHERE id=?", (int(trusted), member_id)
            )

    def demote_with_guard(self, member_id: int, family_id: int) -> bool:
        """降级信任位,带群内不变式"至少保留一名其他信任成员";条件更新保证并发下原子。

        返回 False 表示被护栏拦下(目标已是普通成员,或这是群内最后一名信任成员)。
        """
        with self.conn:
            cur = self.conn.execute(
                "UPDATE member SET trusted=0 WHERE id=? AND family_id=? AND trusted=1"
                " AND (SELECT COUNT(*) FROM member WHERE family_id=? AND trusted=1 AND id<>?) >= 1",
                (member_id, family_id, family_id, member_id),
            )
        return cur.rowcount > 0


class QueryRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def insert(
        self,
        family_id: int,
        member_id: int,
        content_type: str,
        content: str,
        msg_id: str | None,
    ) -> int:
        try:
            with self.conn:
                cur = self.conn.execute(
                    "INSERT INTO query(family_id,member_id,content_type,content,msg_id,created_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (family_id, member_id, content_type, content, msg_id, utcnow()),
                )
        except sqlite3.IntegrityError as e:
            raise DuplicateMessage(msg_id or "") from e
        return int(cur.lastrowid)

    def exists_by_msg_id(self, msg_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT id, family_id, member_id FROM query WHERE msg_id=?", (msg_id,)
        ).fetchone()
        return dict(row) if row else None

    def get(self, query_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM query WHERE id=?", (query_id,)
        ).fetchone()
        return dict(row) if row else None

    def count(self, family_id: int, since: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) c FROM query WHERE family_id=? AND created_at>=?",
            (family_id, since),
        ).fetchone()
        return int(row["c"])


class VerdictRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def insert(
        self,
        query_id: int,
        level: Level,
        cited_ids: list[str],
        features_snapshot: list[dict],
        reason: str,
        reply: str,
        latency_ms: int,
        mode: Mode,
    ) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO verdict(query_id,level,cited_ids,features,reason,reply,latency_ms,mode,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    query_id,
                    level.value,
                    json.dumps(cited_ids),
                    json.dumps(features_snapshot, ensure_ascii=False),
                    reason,
                    reply,
                    latency_ms,
                    mode.value,
                    utcnow(),
                ),
            )
        return int(cur.lastrowid)

    def get(self, verdict_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM verdict WHERE id=?", (verdict_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_with_context(self, verdict_id: int) -> dict | None:
        """判定详情(含原消息与所属家庭),供告警落地页。"""
        row = self.conn.execute(
            "SELECT v.id, v.level, v.reply, v.created_at, q.content, q.family_id"
            " FROM verdict v JOIN query q ON q.id=v.query_id WHERE v.id=?",
            (verdict_id,),
        ).fetchone()
        return dict(row) if row else None

    def count_dangerous(self, family_id: int, since: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) c FROM verdict v JOIN query q ON q.id=v.query_id"
            " WHERE q.family_id=? AND v.level='dangerous' AND v.created_at>=?",
            (family_id, since),
        ).fetchone()
        return int(row["c"])


class AlertRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def insert(self, verdict_id: int, member_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO alert(verdict_id,member_id,delivered_at) VALUES(?,?,?)",
                (verdict_id, member_id, utcnow()),
            )

    def list_by_family(self, family_id: int, limit: int = 50) -> list[dict]:
        """家庭高危告警历史(每条判定一行,新→旧),供子女控制台打开时渲染。"""
        rows = self.conn.execute(
            "SELECT v.id AS verdict_id, v.level, q.content, MAX(a.delivered_at) AS delivered_at"
            " FROM alert a"
            " JOIN verdict v ON v.id=a.verdict_id"
            " JOIN query q ON q.id=v.query_id"
            " WHERE q.family_id=?"
            " GROUP BY v.id"
            " ORDER BY v.id DESC"
            " LIMIT ?",
            (family_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


class CorrectionRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def insert(
        self,
        verdict_id: int,
        by_member_id: int,
        label: CorrectionLabel,
        note: str,
        status: CorrectionStatus,
    ) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO correction(verdict_id,by_member_id,label,note,status,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (verdict_id, by_member_id, label.value, note, status.value, utcnow()),
            )
        return int(cur.lastrowid)

    def get(self, correction_id: int) -> CorrectionRecord | None:
        row = self.conn.execute(
            "SELECT * FROM correction WHERE id=?", (correction_id,)
        ).fetchone()
        return self._to_record(row) if row else None

    @staticmethod
    def _to_record(row: sqlite3.Row) -> CorrectionRecord:
        return CorrectionRecord(
            id=row["id"],
            verdict_id=row["verdict_id"],
            by_member_id=row["by_member_id"],
            label=CorrectionLabel(row["label"]),
            note=row["note"],
            status=CorrectionStatus(row["status"]),
            decided_by=row["decided_by"],
        )

    def get_by_verdict_and_member(self, verdict_id: int, member_id: int) -> CorrectionRecord | None:
        """本人对某条判定的反馈(落地页据此显示已反馈状态)。"""
        row = self.conn.execute(
            "SELECT * FROM correction WHERE verdict_id=? AND by_member_id=?"
            " ORDER BY id DESC LIMIT 1",
            (verdict_id, member_id),
        ).fetchone()
        return self._to_record(row) if row else None

    def decide(
        self, correction_id: int, status: CorrectionStatus, decided_by: int
    ) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE correction SET status=?, decided_by=?, decided_at=? WHERE id=?",
                (status.value, decided_by, utcnow(), correction_id),
            )

    def get_with_family(self, correction_id: int) -> dict | None:
        """纠正记录所属家庭(经 verdict→query 归属),供裁决前的越权校验。"""
        row = self.conn.execute(
            "SELECT c.*, q.family_id FROM correction c"
            " JOIN verdict v ON v.id=c.verdict_id"
            " JOIN query q ON q.id=v.query_id"
            " WHERE c.id=?",
            (correction_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_pending(self) -> list[CorrectionRecord]:
        rows = self.conn.execute(
            "SELECT * FROM correction WHERE status='pending'"
        ).fetchall()
        return [self._to_record(r) for r in rows]

    def count(self, family_id: int, since: int, status: str | None = None, label: str | None = None) -> int:
        sql = (
            "SELECT COUNT(*) c FROM correction c"
            " JOIN verdict v ON v.id=c.verdict_id JOIN query q ON q.id=v.query_id"
            " WHERE q.family_id=? AND c.created_at>=?"
        )
        params: list = [family_id, since]
        if status:
            sql += " AND c.status=?"
            params.append(status)
        if label:
            sql += " AND c.label=?"
            params.append(label)
        row = self.conn.execute(sql, params).fetchone()
        return int(row["c"])

    def expire_older_than(self, cutoff: int) -> int:
        """pending 超时自动 rejected。"""
        with self.conn:
            cur = self.conn.execute(
                "UPDATE correction SET status='rejected', decided_at=?"
                " WHERE status='pending' AND created_at<?",
                (utcnow(), cutoff),
            )
        return max(0, cur.rowcount)

    def list_pending_with_context(self, family_id: int) -> list[dict]:
        """待确认纠正队列(含消息内容与提交人),供子女控制台渲染。"""
        rows = self.conn.execute(
            "SELECT c.id, c.label, c.note, c.created_at, m.name AS by_name, q.content"
            " FROM correction c"
            " JOIN verdict v ON v.id=c.verdict_id"
            " JOIN query q ON q.id=v.query_id"
            " JOIN member m ON m.id=c.by_member_id"
            " WHERE c.status='pending' AND q.family_id=?"
            " ORDER BY c.created_at DESC",
            (family_id,),
        ).fetchall()
        return [dict(r) for r in rows]


CODE_ALPHABET = "2346789ABCDEFGHJKMNPQRSTUVWXYZ"  # 去除 0O1I5S,公众号手输不歧义


class BindCodeRepo:
    """绑定码:成员位的一次性领取凭证,过期/已用即失效。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create(self, member_id: int, created_by: int | None, ttl_days: int) -> dict:
        now = utcnow()
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        with self.conn:
            self.conn.execute(
                "INSERT INTO bind_code(code,member_id,created_by,created_at,expires_at)"
                " VALUES(?,?,?,?,?)",
                (code, member_id, created_by, now, now + ttl_days * 86400),
            )
        return {"code": code, "member_id": member_id, "expires_at": now + ttl_days * 86400}

    def claim(self, code: str) -> dict | None:
        """原子认领:未用且未过期才置 used_at,靠 rowcount 防并发双花。"""
        now = utcnow()
        with self.conn:
            cur = self.conn.execute(
                "UPDATE bind_code SET used_at=? WHERE UPPER(code)=UPPER(?)"
                " AND used_at IS NULL AND expires_at>?",
                (now, code, now),
            )
        if cur.rowcount != 1:
            return None
        row = self.conn.execute(
            "SELECT * FROM bind_code WHERE UPPER(code)=UPPER(?)", (code,)
        ).fetchone()
        return dict(row) if row else None

    def invalidate_for_member(self, member_id: int) -> None:
        """重发即作废:该成员所有未用码立即失效,始终只有一个有效码。"""
        with self.conn:
            self.conn.execute(
                "UPDATE bind_code SET used_at=? WHERE member_id=? AND used_at IS NULL",
                (utcnow(), member_id),
            )

    def peek(self, code: str) -> dict | None:
        """只读查看未用未过期的码,供绑定前给出精确错误(不消耗)。"""
        row = self.conn.execute(
            "SELECT * FROM bind_code WHERE UPPER(code)=UPPER(?)"
            " AND used_at IS NULL AND expires_at>?",
            (code, utcnow()),
        ).fetchone()
        return dict(row) if row else None

    def latest_active(self, member_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT code, expires_at FROM bind_code"
            " WHERE member_id=? AND used_at IS NULL AND expires_at>?"
            " ORDER BY id DESC LIMIT 1",
            (member_id, utcnow()),
        ).fetchone()
        return dict(row) if row else None


@dataclass
class Repos:
    family: FamilyRepo
    member: MemberRepo
    query: QueryRepo
    verdict: VerdictRepo
    alert: AlertRepo
    correction: CorrectionRepo
    bind_code: BindCodeRepo


def make_repos(conn: sqlite3.Connection) -> Repos:
    return Repos(
        family=FamilyRepo(conn),
        member=MemberRepo(conn),
        query=QueryRepo(conn),
        verdict=VerdictRepo(conn),
        alert=AlertRepo(conn),
        correction=CorrectionRepo(conn),
        bind_code=BindCodeRepo(conn),
    )

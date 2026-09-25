"""仓储层(Repository 模式):SQL 只存在于本文件,领域层只见领域对象。

所有方法小而直白;事务由调用方 `with conn:` 控制。
"""
import json
import secrets
import sqlite3
from dataclasses import dataclass

from core.errors import DuplicateMessage
from core.models import (
    CorrectionLabel,
    CorrectionRecord,
    CorrectionStatus,
    Level,
    Member,
    Mode,
    Role,
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

    def get(self, family_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM family WHERE id=?", (family_id,)
        ).fetchone()
        return dict(row) if row else None


class MemberRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add(
        self, family_id: int, name: str, role: Role, openid: str | None = None
    ) -> int:
        token = secrets.token_urlsafe(16)  # 个人链接凭证,创建即生成
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO member(family_id,name,role,openid,token,created_at) VALUES(?,?,?,?,?,?)",
                (family_id, name, role.value, openid, token, utcnow()),
            )
        return int(cur.lastrowid)

    def _row_to_member(self, row: sqlite3.Row) -> Member:
        return Member(
            id=row["id"],
            family_id=row["family_id"],
            name=row["name"],
            role=Role(row["role"]),
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

    def list_members(self, family_id: int) -> list[Member]:
        rows = self.conn.execute(
            "SELECT * FROM member WHERE family_id=? ORDER BY id", (family_id,)
        ).fetchall()
        return [self._row_to_member(r) for r in rows]

    def list_adults(self, family_id: int) -> list[Member]:
        rows = self.conn.execute(
            "SELECT * FROM member WHERE family_id=? AND role='adult'", (family_id,)
        ).fetchall()
        return [self._row_to_member(r) for r in rows]


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

    def decide(
        self, correction_id: int, status: CorrectionStatus, decided_by: int
    ) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE correction SET status=?, decided_by=?, decided_at=? WHERE id=?",
                (status.value, decided_by, utcnow(), correction_id),
            )

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


@dataclass
class Repos:
    family: FamilyRepo
    member: MemberRepo
    query: QueryRepo
    verdict: VerdictRepo
    alert: AlertRepo
    correction: CorrectionRepo


def make_repos(conn: sqlite3.Connection) -> Repos:
    return Repos(
        family=FamilyRepo(conn),
        member=MemberRepo(conn),
        query=QueryRepo(conn),
        verdict=VerdictRepo(conn),
        alert=AlertRepo(conn),
        correction=CorrectionRepo(conn),
    )

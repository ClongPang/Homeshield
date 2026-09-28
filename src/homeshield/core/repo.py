"""SQLite repositories. A process-wide lock serializes access to the shared connection."""
from functools import wraps
import json
import secrets
import sqlite3
import threading
from dataclasses import dataclass

from homeshield.core.errors import DuplicateMessage, ValidationError
from homeshield.core.models import Level, Mode, User, utc_timestamp

WRITE_LOCK = threading.RLock()
CODE_ALPHABET = "2346789ABCDEFGHJKMNPQRSTUVWXYZ"


def _serialize_repo_access(cls):
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
    def __init__(self, conn: sqlite3.Connection): self.conn = conn

    @staticmethod
    def _model(row) -> User | None: return User(**dict(row)) if row else None

    def get(self, user_id: int) -> User | None:
        return self._model(self.conn.execute("SELECT * FROM user WHERE id=?", (user_id,)).fetchone())

    def get_by_openid(self, openid: str) -> User | None:
        return self._model(self.conn.execute("SELECT * FROM user WHERE openid=?", (openid,)).fetchone())

    def get_by_token(self, token: str) -> User | None:
        return self._model(self.conn.execute("SELECT * FROM user WHERE token=?", (token,)).fetchone())

    def get_or_create(self, openid: str) -> User:
        with self.conn:
            row = self.conn.execute("SELECT * FROM user WHERE openid=?", (openid,)).fetchone()
            if row: return User(**dict(row))
            cur = self.conn.execute("INSERT INTO user(openid,token,created_at) VALUES(?,?,?)",
                                    (openid, _token(), utc_timestamp()))
            row = self.conn.execute("SELECT * FROM user WHERE id=?", (cur.lastrowid,)).fetchone()
        return User(**dict(row))


@_serialize_repo_access
class RelationRepo:
    def __init__(self, conn: sqlite3.Connection): self.conn = conn

    def get(self, relation_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM guard_relation WHERE id=?", (relation_id,)).fetchone()
        return dict(row) if row else None

    def list_for_user(self, user_id: int) -> dict:
        guardings = self.conn.execute(
            "SELECT id,name,mute,created_at FROM guard_relation WHERE protector_user_id=? AND ended_at IS NULL ORDER BY id",
            (user_id,)).fetchall()
        guardians = self.conn.execute(
            "SELECT id,COALESCE(NULLIF(inverse_name,''),'联防者 #'||id) name FROM guard_relation "
            "WHERE protected_user_id=? AND ended_at IS NULL ORDER BY id", (user_id,)).fetchall()
        return {"guardings": [dict(r) for r in guardings], "guardians": [dict(r) for r in guardians]}

    def count_active(self, user_id: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) n FROM guard_relation WHERE ended_at IS NULL AND (protector_user_id=? OR protected_user_id=?)",
            (user_id, user_id)).fetchone()
        return int(row["n"])

    def update(self, relation_id: int, user_id: int, *, name: str | None = None,
               inverse_name: str | None = None, mute: bool | None = None) -> str | None:
        with self.conn:
            row = self.conn.execute("SELECT * FROM guard_relation WHERE id=? AND ended_at IS NULL", (relation_id,)).fetchone()
            if row is None: return "relation_ended"
            if row["protector_user_id"] == user_id:
                if inverse_name is not None: return "wrong_side"
                fields, values = [], []
                if name is not None: fields.append("name=?"); values.append(name)
                if mute is not None: fields.append("mute=?"); values.append(int(mute))
            elif row["protected_user_id"] == user_id:
                if name is not None or mute is not None: return "wrong_side"
                fields, values = [], []
                if inverse_name is not None: fields.append("inverse_name=?"); values.append(inverse_name)
            else: return "not_participant"
            if fields:
                self.conn.execute(f"UPDATE guard_relation SET {','.join(fields)} WHERE id=?", (*values, relation_id))
            return "updated"

    def end(self, relation_id: int, user_id: int) -> str:
        with self.conn:
            row = self.conn.execute("SELECT * FROM guard_relation WHERE id=?", (relation_id,)).fetchone()
            if row is None or user_id not in (row["protector_user_id"], row["protected_user_id"]):
                return "not_found"
            if row["ended_at"] is not None: return "already_ended"
            reason = "by_protector" if row["protector_user_id"] == user_id else "by_protected"
            now = utc_timestamp()
            self.conn.execute("UPDATE guard_relation SET ended_at=?,end_reason=? WHERE id=? AND ended_at IS NULL",
                              (now, reason, relation_id))
            if reason == "by_protector":
                self.conn.execute("UPDATE invite_code SET revoked_at=? WHERE creator_user_id=? AND used_at IS NULL AND revoked_at IS NULL",
                                  (now, user_id))
            return reason

    def snapshot_for_protected(self, user_id: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT id FROM guard_relation WHERE protected_user_id=? AND ended_at IS NULL ORDER BY id", (user_id,))]


@_serialize_repo_access
class InviteRepo:
    def __init__(self, conn: sqlite3.Connection): self.conn = conn

    def create(self, creator_id: int, name: str, ttl_days: int, max_relations: int = 10) -> dict:
        now = utc_timestamp()
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        expires = now + ttl_days * 86400
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            count = self.conn.execute("SELECT COUNT(*) n FROM guard_relation WHERE ended_at IS NULL "
                                      "AND (protector_user_id=? OR protected_user_id=?)", (creator_id, creator_id)).fetchone()["n"]
            if int(count) >= max_relations:
                self.conn.rollback()
                raise ValidationError("relation limit reached")
            cur = self.conn.execute(
                "INSERT INTO invite_code(code,creator_user_id,name,created_at,expires_at) VALUES(?,?,?,?,?)",
                (code, creator_id, name, now, expires))
            self.conn.commit()
        except Exception:
            if self.conn.in_transaction: self.conn.rollback()
            raise
        return {"id": int(cur.lastrowid), "code": code, "expires_at": expires}

    def get(self, code: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM invite_code WHERE UPPER(code)=UPPER(?)", (code,)).fetchone()
        return dict(row) if row else None

    def get_valid(self, code: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM invite_code WHERE UPPER(code)=UPPER(?) AND used_at IS NULL "
                                "AND revoked_at IS NULL AND expires_at>?", (code, utc_timestamp())).fetchone()
        return dict(row) if row else None

    def list_for_creator(self, user_id: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT id,code,name,created_at,expires_at,used_at,used_by_user_id,revoked_at FROM invite_code "
            "WHERE creator_user_id=? ORDER BY id DESC", (user_id,))]

    def revoke(self, invite_id: int, creator_id: int) -> str:
        with self.conn:
            row = self.conn.execute("SELECT * FROM invite_code WHERE id=? AND creator_user_id=?", (invite_id, creator_id)).fetchone()
            if row is None: return "not_found"
            if row["used_at"] is not None: return "used"
            if row["revoked_at"] is not None: return "revoked"
            self.conn.execute("UPDATE invite_code SET revoked_at=? WHERE id=? AND used_at IS NULL AND revoked_at IS NULL",
                              (utc_timestamp(), invite_id))
            return "revoked"

    def claim(self, code: str, protected_id: int, max_relations: int) -> tuple[str, int | None]:
        """Consume a code and create its directed relation in one write transaction."""
        now = utc_timestamp()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute("SELECT * FROM invite_code WHERE UPPER(code)=UPPER(?)", (code,)).fetchone()
            if row is None: reason = "invalid"
            elif row["used_at"] is not None: reason = "used"
            elif row["revoked_at"] is not None: reason = "revoked"
            elif row["expires_at"] <= now: reason = "expired"
            elif int(row["creator_user_id"]) == protected_id: reason = "self"
            else:
                creator_id = int(row["creator_user_id"])
                exists = self.conn.execute("SELECT 1 FROM guard_relation WHERE protector_user_id=? AND protected_user_id=? AND ended_at IS NULL",
                                           (creator_id, protected_id)).fetchone()
                if exists: reason = "already_exists"
                else:
                    ids = (creator_id, protected_id)
                    counts = [self.conn.execute("SELECT COUNT(*) n FROM guard_relation WHERE ended_at IS NULL AND (protector_user_id=? OR protected_user_id=?)", (uid, uid)).fetchone()["n"] for uid in ids]
                    if any(int(count) >= max_relations for count in counts): reason = "limit"
                    else:
                        cur = self.conn.execute("INSERT INTO guard_relation(protector_user_id,protected_user_id,name,created_at) VALUES(?,?,?,?)",
                                                (creator_id, protected_id, row["name"], now))
                        claimed = self.conn.execute("UPDATE invite_code SET used_at=?,used_by_user_id=? WHERE id=? AND used_at IS NULL AND revoked_at IS NULL AND expires_at>?",
                                                    (now, protected_id, row["id"], now))
                        if claimed.rowcount != 1: raise ValidationError("invite became unavailable")
                        reason = "created"; relation_id = int(cur.lastrowid)
            if reason == "created": self.conn.commit(); return reason, relation_id
            self.conn.rollback(); return reason, None
        except Exception:
            self.conn.rollback()
            raise


@_serialize_repo_access
class QueryRepo:
    def __init__(self, conn: sqlite3.Connection): self.conn = conn

    def insert(self, user_id: int, content_type: str, content: str, msg_id: str | None,
               kind: str = "query") -> int:
        try:
            with self.conn:
                now = utc_timestamp()
                cur = self.conn.execute("INSERT INTO query(user_id,content_type,content,msg_id,created_at,kind) VALUES(?,?,?,?,?,?)",
                                        (user_id, content_type, content, msg_id, now, kind))
                query_id = int(cur.lastrowid)
                self.conn.execute("INSERT INTO query_relation(query_id,relation_id) "
                                  "SELECT ?,id FROM guard_relation WHERE protected_user_id=? AND ended_at IS NULL",
                                  (query_id, user_id))
        except sqlite3.IntegrityError as exc:
            # 只把 MsgId 幂等键冲突映射为重复消息;其余约束违例原样抛出,不冒充重复
            if msg_id and "query.msg_id" in str(exc):
                raise DuplicateMessage(msg_id) from exc
            raise
        return query_id

    def find_by_msg_id(self, msg_id: str) -> dict | None:
        row = self.conn.execute("SELECT id,user_id FROM query WHERE msg_id=?", (msg_id,)).fetchone()
        return dict(row) if row else None

    def get(self, query_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM query WHERE id=?", (query_id,)).fetchone()
        return dict(row) if row else None

    def update_transcript(self, query_id: int, transcript: str) -> None:
        with self.conn: self.conn.execute("UPDATE query SET transcript=? WHERE id=? AND content_type='image'", (transcript, query_id))

    def supply_context(self, query_id: int, user_id: int, window_seconds: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT prior.id,prior.content_type,prior.content,prior.transcript,prior.created_at,prior.incident_id,"
            "cur.incident_id AS current_incident_id,cur.created_at AS current_created_at FROM query cur JOIN query prior "
            "ON prior.user_id=cur.user_id WHERE cur.id=? AND cur.user_id=? AND cur.kind='query' AND cur.incident_id IS NOT NULL "
            "AND prior.kind='query' AND prior.id<cur.id AND prior.created_at>=cur.created_at-? "
            "ORDER BY prior.created_at DESC,prior.id DESC LIMIT 200", (query_id, user_id, window_seconds)).fetchall()
        return [dict(r) for r in rows]

    def list_relations_for_query(self, query_id: int, active_only: bool = False) -> list[dict]:
        sql = "SELECT r.* FROM query_relation qr JOIN guard_relation r ON r.id=qr.relation_id WHERE qr.query_id=?"
        if active_only: sql += " AND r.ended_at IS NULL"
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY r.id", (query_id,))]

    def list_for_user(self, user_id: int, limit: int = 100) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT v.id verdict_id,v.query_id,v.level,v.created_at verdict_at,c.status correction_status,c.resolved_label "
            "FROM query q JOIN verdict v ON v.query_id=q.id LEFT JOIN correction_case c ON c.verdict_id=v.id "
            "WHERE q.user_id=? ORDER BY v.id DESC LIMIT ?", (user_id, limit))]

    def get_my_detail(self, user_id: int, verdict_id: int) -> dict | None:
        row = self.conn.execute("SELECT v.id verdict_id,v.query_id,v.level,v.reason,v.reply,v.created_at verdict_at,"
                                "q.content,q.content_type,c.id case_id,c.status correction_status,c.resolved_label,"
                                "c.queryer_label,c.queryer_note FROM verdict v JOIN query q ON q.id=v.query_id "
                                "LEFT JOIN correction_case c ON c.verdict_id=v.id WHERE v.id=? AND q.user_id=?",
                                (verdict_id, user_id)).fetchone()
        return dict(row) if row else None


@_serialize_repo_access
class IncidentRepo:
    def __init__(self, conn: sqlite3.Connection): self.conn = conn

    def current_epoch(self, user_id: int) -> int:
        row = self.conn.execute("SELECT session_epoch FROM user WHERE id=?", (user_id,)).fetchone()
        if row is None: raise ValidationError("user not found")
        return int(row["session_epoch"])

    def attach_query_to_incident(self, query_id: int, user_id: int, idle_seconds: int,
                                 expected_epoch: int | None = None) -> int | None:
        for attempt in range(2):
            try:
                with WRITE_LOCK, self.conn:
                    self.conn.execute("BEGIN IMMEDIATE")
                    if expected_epoch is not None and self.current_epoch(user_id) != expected_epoch: return None
                    query = self.conn.execute("SELECT created_at FROM query WHERE id=? AND user_id=? AND kind='query'", (query_id, user_id)).fetchone()
                    if query is None: raise ValidationError("query not found")
                    now = int(query["created_at"])
                    inc = self.conn.execute("SELECT id,opened_at,last_query_at FROM incident WHERE user_id=? AND closed_at IS NULL", (user_id,)).fetchone()
                    if inc is not None and now < int(inc["opened_at"]) - idle_seconds: return None
                    if inc is not None and now - int(inc["last_query_at"]) <= idle_seconds:
                        incident_id = int(inc["id"])
                        self.conn.execute("UPDATE incident SET last_query_at=? WHERE id=?", (max(now, int(inc["last_query_at"])), incident_id))
                    else:
                        if inc is not None: self.conn.execute("UPDATE incident SET closed_at=?,close_reason='timeout' WHERE id=?", (now, inc["id"]))
                        cur = self.conn.execute("INSERT INTO incident(user_id,opened_at,last_query_at) VALUES(?,?,?)", (user_id, now, now))
                        incident_id = int(cur.lastrowid)
                    self.conn.execute("UPDATE query SET incident_id=? WHERE id=?", (incident_id, query_id))
                    return incident_id
            except sqlite3.IntegrityError:
                if attempt: raise
        raise RuntimeError("incident attach failed")

    def close_open_incident(self, user_id: int, reason: str = "explicit", msg_id: str | None = None) -> bool:
        if reason not in {"explicit", "manual"}: raise ValidationError("invalid close reason")
        with self.conn:
            if msg_id:
                try: self.conn.execute("INSERT INTO session_reset_msg(msg_id,user_id,created_at) VALUES(?,?,?)", (msg_id, user_id, utc_timestamp()))
                except sqlite3.IntegrityError: return False
            self.conn.execute("UPDATE user SET session_epoch=session_epoch+1 WHERE id=?", (user_id,))
            self.conn.execute("UPDATE incident SET closed_at=?,close_reason=? WHERE user_id=? AND closed_at IS NULL", (utc_timestamp(), reason, user_id))
        return True

    def list_for_user(self, user_id: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT i.*,COUNT(q.id) AS query_count FROM incident i LEFT JOIN query q "
               "ON q.incident_id=i.id AND q.kind='query' WHERE i.user_id=? GROUP BY i.id ORDER BY i.id DESC", (user_id,))]


@_serialize_repo_access
class VerdictRepo:
    def __init__(self, conn: sqlite3.Connection): self.conn = conn

    def insert(self, query_id: int, level: Level, cited_ids: list[str], features_snapshot: list[dict], reason: str,
               reply: str, latency_ms: int, mode: Mode, context_snapshot: str | None = None) -> int:
        with self.conn:
            cur = self.conn.execute("INSERT INTO verdict(query_id,level,cited_ids,features,reason,reply,latency_ms,mode,created_at,context_snapshot) "
                                    "VALUES(?,?,?,?,?,?,?,?,?,?)", (query_id, level.value, json.dumps(cited_ids),
                                    json.dumps(features_snapshot, ensure_ascii=False), reason, reply, latency_ms, mode.value,
                                    utc_timestamp(), context_snapshot))
        return int(cur.lastrowid)

    def get(self, verdict_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM verdict WHERE id=?", (verdict_id,)).fetchone()
        return dict(row) if row else None

    def get_with_context(self, verdict_id: int) -> dict | None:
        row = self.conn.execute("SELECT v.id,v.query_id,v.level,v.reply,v.created_at,q.user_id,q.content FROM verdict v "
                                "JOIN query q ON q.id=v.query_id WHERE v.id=?", (verdict_id,)).fetchone()
        return dict(row) if row else None


@_serialize_repo_access
class AlertRepo:
    def __init__(self, conn: sqlite3.Connection): self.conn = conn

    def record_alerts_for_verdict(self, verdict_id: int, query_id: int) -> dict:
        now = utc_timestamp()
        queryer = self.conn.execute("SELECT user_id FROM query WHERE id=?", (query_id,)).fetchone()
        if queryer is None: return {"queryer_id": None, "recipients": []}
        recipients = []
        with self.conn:
            rows = self.conn.execute("SELECT r.id relation_id,r.protector_user_id user_id,r.name,r.inverse_name,r.mute,u.openid,u.token "
                "FROM query_relation qr JOIN guard_relation r ON r.id=qr.relation_id JOIN user u ON u.id=r.protector_user_id "
                "WHERE qr.query_id=? AND r.ended_at IS NULL ORDER BY r.id", (query_id,)).fetchall()
            for row in rows:
                self.conn.execute("INSERT OR IGNORE INTO alert(verdict_id,relation_id,name_at_alert,delivered_at) VALUES(?,?,?,?)",
                                  (verdict_id, row["relation_id"], row["name"], now))
                alert = self.conn.execute("SELECT id,delivered_at FROM alert WHERE verdict_id=? AND relation_id=?",
                                          (verdict_id, row["relation_id"])).fetchone()
                recipients.append({"relation_id": int(row["relation_id"]), "user_id": int(row["user_id"]),
                                   "openid": row["openid"], "token": row["token"], "name_at_alert": row["name"],
                                   "inverse_name": row["inverse_name"], "mute": bool(row["mute"]),
                                   "alert_id": int(alert["id"]), "delivered_at": int(alert["delivered_at"])})
        return {"queryer_id": int(queryer["user_id"]), "recipients": recipients}

    def event_context(self, alert_id: int) -> dict | None:
        row = self.conn.execute("SELECT a.id alert_id,a.verdict_id,a.relation_id,a.name_at_alert,a.delivered_at,"
            "r.protector_user_id user_id,r.mute FROM alert a JOIN guard_relation r ON r.id=a.relation_id "
            "WHERE a.id=? AND r.ended_at IS NULL", (alert_id,)).fetchone()
        return dict(row) if row else None

    def push_context(self, alert_id: int) -> dict | None:
        row = self.conn.execute("SELECT a.id alert_id,a.verdict_id,a.relation_id,a.name_at_alert,a.delivered_at,"
            "r.protector_user_id user_id,r.mute,u.openid,u.token FROM alert a JOIN guard_relation r ON r.id=a.relation_id "
            "JOIN user u ON u.id=r.protector_user_id WHERE a.id=? AND r.ended_at IS NULL AND r.mute=0", (alert_id,)).fetchone()
        return dict(row) if row else None

    def list_for_user(self, user_id: int, relation_id: int | None = None, limit: int = 100) -> list[dict]:
        sql = ("SELECT a.id alert_id,a.verdict_id,a.relation_id,a.name_at_alert,a.delivered_at,a.read_at,v.level,q.content "
               "FROM alert a JOIN guard_relation r ON r.id=a.relation_id JOIN verdict v ON v.id=a.verdict_id "
               "JOIN query q ON q.id=v.query_id WHERE r.protector_user_id=? AND r.ended_at IS NULL")
        params: list = [user_id]
        if relation_id is not None: sql += " AND a.relation_id=?"; params.append(relation_id)
        sql += " ORDER BY a.id DESC LIMIT ?"; params.append(limit)
        return [dict(r) for r in self.conn.execute(sql, params)]

    def detail_for_user(self, user_id: int, alert_id: int) -> tuple[dict | None, str | None]:
        row = self.conn.execute("SELECT a.id alert_id,a.verdict_id,a.relation_id,a.name_at_alert,a.delivered_at,"
            "v.level,v.reply,v.created_at verdict_at,q.content,q.content_type,c.id case_id,c.status correction_status,"
            "c.resolved_label,cv.label my_vote,cv.voted_at FROM alert a JOIN guard_relation r ON r.id=a.relation_id "
            "JOIN verdict v ON v.id=a.verdict_id JOIN query q ON q.id=v.query_id "
            "LEFT JOIN correction_case c ON c.verdict_id=v.id LEFT JOIN correction_vote cv ON cv.case_id=c.id AND cv.relation_id=r.id "
            "WHERE a.id=? AND r.protector_user_id=?", (alert_id, user_id)).fetchone()
        if row is None: return None, None
        if self.conn.execute("SELECT ended_at FROM guard_relation WHERE id=?", (row["relation_id"],)).fetchone()["ended_at"] is not None:
            return None, "relation_ended"
        return dict(row), None


@_serialize_repo_access
class CorrectionRepo:
    def __init__(self, conn: sqlite3.Connection): self.conn = conn

    def submit(self, verdict_id: int, user_id: int, label: str, note: str, window_days: int) -> dict:
        """Open a case, save queryer feedback, or cast one relation vote atomically."""
        now = utc_timestamp()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            verdict = self.conn.execute("SELECT v.id,v.level,v.query_id,q.user_id queryer_id,q.content FROM verdict v "
                                        "JOIN query q ON q.id=v.query_id WHERE v.id=?", (verdict_id,)).fetchone()
            if verdict is None: raise ValidationError("verdict not found")
            if verdict["level"] not in ("safe", "suspicious", "dangerous"):
                raise ValidationError("verdict is not eligible for correction")
            case = self.conn.execute("SELECT * FROM correction_case WHERE verdict_id=?", (verdict_id,)).fetchone()
            if case and case["status"] == "pending" and now >= int(case["closes_at"]):
                self.conn.execute("UPDATE correction_case SET status='no_consensus',resolved_at=? WHERE id=? AND status='pending'", (now, case["id"]))
                case = self.conn.execute("SELECT * FROM correction_case WHERE id=?", (case["id"],)).fetchone()
            is_queryer = int(verdict["queryer_id"]) == user_id
            relation_id = None
            if not is_queryer:
                if case is not None:
                    rel = self.conn.execute("SELECT r.id FROM correction_vote cv JOIN guard_relation r ON r.id=cv.relation_id "
                        "WHERE cv.case_id=? AND r.protector_user_id=? AND r.ended_at IS NULL", (case["id"], user_id)).fetchone()
                else:
                    if verdict["level"] != "dangerous": raise ValidationError("only queryer can open low-risk correction")
                    rel = self.conn.execute("SELECT r.id FROM guard_relation r JOIN alert a ON a.relation_id=r.id "
                        "WHERE a.verdict_id=? AND r.protector_user_id=? AND r.ended_at IS NULL", (verdict_id, user_id)).fetchone()
                if rel is None: raise ValidationError("no active voting relation")
                relation_id = int(rel["id"])
            if case is None:
                if not is_queryer and verdict["level"] != "dangerous": raise ValidationError("only queryer can open correction")
                opened = now; closes = now + window_days * 86400
                cur = self.conn.execute("INSERT INTO correction_case(verdict_id,queryer_label,queryer_note,queryer_feedback_at,status,opened_at,closes_at) "
                    "VALUES(?,NULL,'',NULL,'pending',?,?)", (verdict_id, opened, closes))
                case_id = int(cur.lastrowid)
                if verdict["level"] == "dangerous":
                    eligible = self.conn.execute("SELECT DISTINCT r.id FROM alert a JOIN guard_relation r ON r.id=a.relation_id "
                        "WHERE a.verdict_id=? AND r.ended_at IS NULL", (verdict_id,)).fetchall()
                else:
                    eligible = self.conn.execute("SELECT r.id FROM query_relation qr JOIN guard_relation r ON r.id=qr.relation_id "
                        "WHERE qr.query_id=? AND r.ended_at IS NULL", (verdict["query_id"],)).fetchall()
                self.conn.executemany("INSERT INTO correction_vote(case_id,relation_id) VALUES(?,?)",
                                      [(case_id, int(r["id"])) for r in eligible])
                if not eligible:
                    self.conn.execute("UPDATE correction_case SET status='no_consensus',resolved_at=? WHERE id=?", (now, case_id))
                case = self.conn.execute("SELECT * FROM correction_case WHERE id=?", (case_id,)).fetchone()
            case_id = int(case["id"])
            if is_queryer:
                if case["queryer_label"] is not None:
                    if case["queryer_label"] != label:
                        raise ValidationError("queryer feedback cannot be changed")
                    self.conn.commit()
                    return self.case_summary(case_id)
                self.conn.execute("UPDATE correction_case SET queryer_label=?,queryer_note=?,queryer_feedback_at=? WHERE id=?",
                                  (label, note, now, case_id))
            else:
                eligible = self.conn.execute("SELECT label FROM correction_vote WHERE case_id=? AND relation_id=?", (case_id, relation_id)).fetchone()
                if eligible is None: raise ValidationError("relation is not eligible for this correction")
                if eligible["label"] is not None:
                    if eligible["label"] != label: raise ValidationError("vote cannot be changed")
                    self.conn.commit()
                    return self.case_summary(case_id)
                else:
                    if case["status"] != "pending": raise ValidationError("correction case is closed")
                    self.conn.execute("UPDATE correction_vote SET label=?,voted_at=? WHERE case_id=? AND relation_id=? AND label IS NULL",
                                      (label, now, case_id, relation_id))
                    total = int(self.conn.execute("SELECT COUNT(*) n FROM correction_vote WHERE case_id=?", (case_id,)).fetchone()["n"])
                    required = total // 2 + 1
                    counts = self.conn.execute("SELECT label,COUNT(*) n FROM correction_vote WHERE case_id=? AND label IS NOT NULL GROUP BY label", (case_id,)).fetchall()
                    winner = next((r["label"] for r in counts if int(r["n"]) >= required), None)
                    if winner:
                        self.conn.execute("UPDATE correction_case SET status='confirmed',resolved_label=?,resolved_at=? WHERE id=? AND status='pending'",
                                          (winner, now, case_id))
            self.conn.commit()
            return self.case_summary(case_id)
        except Exception:
            self.conn.rollback()
            raise

    def close_expired(self) -> int:
        now = utc_timestamp()
        with self.conn:
            cur = self.conn.execute("UPDATE correction_case SET status='no_consensus',resolved_at=? WHERE status='pending' AND closes_at<=?", (now, now))
        return cur.rowcount

    def case_summary(self, case_id: int) -> dict:
        row = self.conn.execute("SELECT * FROM correction_case WHERE id=?", (case_id,)).fetchone()
        if row is None: raise ValidationError("correction case not found")
        counts = {r["label"]: int(r["n"]) for r in self.conn.execute(
            "SELECT label,COUNT(*) n FROM correction_vote WHERE case_id=? AND label IS NOT NULL GROUP BY label", (case_id,))}
        total = int(self.conn.execute("SELECT COUNT(*) n FROM correction_vote WHERE case_id=?", (case_id,)).fetchone()["n"])
        return {"case_id": int(row["id"]), "verdict_id": int(row["verdict_id"]), "status": row["status"],
                "resolved_label": row["resolved_label"], "votes": counts, "eligible_count": total,
                "required_votes": total // 2 + 1 if total else 0, "closes_at": int(row["closes_at"]),
                "queryer_label": row["queryer_label"], "queryer_note": row["queryer_note"]}

    def list_pending_for_user(self, user_id: int) -> list[dict]:
        self.close_expired()
        rows = self.conn.execute("SELECT c.id case_id,c.verdict_id,c.closes_at,v.level,q.content,q.created_at query_at,"
            "cv.label my_vote,COUNT(allv.relation_id) eligible_count FROM correction_case c "
            "JOIN correction_vote cv ON cv.case_id=c.id JOIN guard_relation r ON r.id=cv.relation_id "
            "JOIN verdict v ON v.id=c.verdict_id JOIN query q ON q.id=v.query_id "
            "LEFT JOIN correction_vote allv ON allv.case_id=c.id WHERE c.status='pending' AND r.protector_user_id=? "
            "AND r.ended_at IS NULL GROUP BY c.id,cv.relation_id ORDER BY c.opened_at", (user_id,)).fetchall()
        return [{**dict(r), "required_votes": int(r["eligible_count"]) // 2 + 1} for r in rows]

    def detail_for_relation(self, case_id: int, relation_id: int, user_id: int) -> dict | None:
        self.close_expired()
        row = self.conn.execute("SELECT c.id case_id,c.verdict_id,c.status,c.resolved_label,c.closes_at,c.queryer_label,"
            "c.queryer_note,v.level,q.content,cv.label my_vote,cv.voted_at FROM correction_case c "
            "JOIN correction_vote cv ON cv.case_id=c.id JOIN guard_relation r ON r.id=cv.relation_id "
            "JOIN verdict v ON v.id=c.verdict_id JOIN query q ON q.id=v.query_id "
            "WHERE c.id=? AND cv.relation_id=? AND r.protector_user_id=? AND r.ended_at IS NULL",
            (case_id, relation_id, user_id)).fetchone()
        return dict(row) if row else None

    def get_case_for_verdict(self, verdict_id: int) -> dict | None:
        row = self.conn.execute("SELECT id FROM correction_case WHERE verdict_id=?", (verdict_id,)).fetchone()
        return self.case_summary(int(row["id"])) if row else None

    def confirmed_labels(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT v.id verdict_id,c.resolved_label FROM correction_case c "
            "JOIN verdict v ON v.id=c.verdict_id WHERE c.status='confirmed'")]


@_serialize_repo_access
class WecomMemberRepo:
    """wxkf 用户 ↔ 企业微信成员 userid 的映射;应用消息告警按此触达微信插件。"""

    def __init__(self, conn: sqlite3.Connection): self.conn = conn

    def link(self, user_id: int, corp_userid: str) -> None:
        with self.conn:
            # corp_userid 亦有 UNIQUE 约束:成员改绑到另一用户时先腾出旧映射
            self.conn.execute("DELETE FROM wecom_member WHERE corp_userid=? AND user_id<>?",
                              (corp_userid, user_id))
            self.conn.execute("INSERT INTO wecom_member(user_id,corp_userid) VALUES(?,?) "
                              "ON CONFLICT(user_id) DO UPDATE SET corp_userid=excluded.corp_userid",
                              (user_id, corp_userid))

    def get(self, user_id: int) -> str | None:
        row = self.conn.execute("SELECT corp_userid FROM wecom_member WHERE user_id=?",
                                (user_id,)).fetchone()
        return row["corp_userid"] if row else None


@dataclass
class Repos:
    conn: sqlite3.Connection
    users: UserRepo
    relation: RelationRepo
    invite: InviteRepo
    query: QueryRepo
    verdict: VerdictRepo
    alert: AlertRepo
    correction: CorrectionRepo
    incident: IncidentRepo
    wecom_member: WecomMemberRepo


def make_repos(conn: sqlite3.Connection) -> Repos:
    return Repos(conn, UserRepo(conn), RelationRepo(conn), InviteRepo(conn), QueryRepo(conn),
                 VerdictRepo(conn), AlertRepo(conn), CorrectionRepo(conn), IncidentRepo(conn),
                 WecomMemberRepo(conn))

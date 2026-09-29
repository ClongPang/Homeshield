"""Postgres repositories. Each public method borrows its own pooled connection."""
from functools import wraps
import json
import secrets
from contextvars import ContextVar
from dataclasses import dataclass

from psycopg.types.json import Jsonb
from psycopg.errors import UniqueViolation
from psycopg_pool import AsyncConnectionPool

from homeshield.core.errors import DuplicateMessage, ValidationError
from homeshield.core.models import Level, Mode, User, utc_timestamp

CODE_ALPHABET = "2346789ABCDEFGHJKMNPQRSTUVWXYZ"
_ACTIVE_CONNECTION: ContextVar = ContextVar("homeshield_repo_connection", default=None)


def _pool_repo_access(cls):
    for name, method in tuple(vars(cls).items()):
        if name.startswith("_") or isinstance(method, (staticmethod, classmethod)) or not callable(method):
            continue
        @wraps(method)
        async def serialized(self, *args, _method=method, **kwargs):
            active = _ACTIVE_CONNECTION.get()
            if active is not None:
                return await _method(self, *args, **kwargs)
            async with self.pool.connection() as conn:
                token = _ACTIVE_CONNECTION.set(conn)
                try:
                    return await _method(self, *args, **kwargs)
                finally:
                    _ACTIVE_CONNECTION.reset(token)
        setattr(cls, name, serialized)
    return cls


class _Repo:
    def __init__(self, pool: AsyncConnectionPool):
        self.pool = pool

    @property
    def conn(self):
        connection = _ACTIVE_CONNECTION.get()
        if connection is None:
            raise RuntimeError("repository connection is only available inside a repository method")
        return connection


def _token() -> str:
    return secrets.token_urlsafe(24)


@_pool_repo_access
class UserRepo(_Repo):

    @staticmethod
    def _model(row) -> User | None: return User(**dict(row)) if row else None

    async def get(self, user_id: int) -> User | None:
        return self._model(await (await self.conn.execute('SELECT * FROM "user" WHERE id=%s', (user_id,))).fetchone())

    async def get_by_openid(self, openid: str) -> User | None:
        return self._model(await (await self.conn.execute('SELECT * FROM "user" WHERE openid=%s', (openid,))).fetchone())

    async def get_by_token(self, token: str) -> User | None:
        return self._model(await (await self.conn.execute('SELECT * FROM "user" WHERE token=%s', (token,))).fetchone())

    async def get_or_create(self, openid: str) -> User:
        async with self.conn.transaction():
            await self.conn.execute(
                'INSERT INTO "user"(openid,token,created_at) VALUES(%s,%s,to_timestamp(%s)) '
                "ON CONFLICT(openid) DO NOTHING",
                (openid, _token(), utc_timestamp()),
            )
            row = await (await self.conn.execute('SELECT * FROM "user" WHERE openid=%s', (openid,))).fetchone()
        return User(**dict(row))


@_pool_repo_access
class RelationRepo(_Repo):

    async def get(self, relation_id: int) -> dict | None:
        row = await (await self.conn.execute("SELECT * FROM guard_relation WHERE id=%s", (relation_id,))).fetchone()
        return dict(row) if row else None

    async def list_for_user(self, user_id: int) -> dict:
        guardings = await (await self.conn.execute(
            "SELECT id,name,mute,created_at FROM guard_relation WHERE protector_user_id=%s AND ended_at IS NULL ORDER BY id",
            (user_id,))).fetchall()
        guardians = await (await self.conn.execute(
            "SELECT id,COALESCE(NULLIF(inverse_name,''),'联防者 #'||id) name FROM guard_relation "
            "WHERE protected_user_id=%s AND ended_at IS NULL ORDER BY id", (user_id,))).fetchall()
        return {"guardings": [dict(r) for r in guardings], "guardians": [dict(r) for r in guardians]}

    async def update(self, relation_id: int, user_id: int, *, name: str | None = None,
               inverse_name: str | None = None, mute: bool | None = None) -> str | None:
        async with self.conn.transaction():
            row = await (await self.conn.execute("SELECT * FROM guard_relation WHERE id=%s AND ended_at IS NULL", (relation_id,))).fetchone()
            if row is None: return "relation_ended"
            if row["protector_user_id"] == user_id:
                if inverse_name is not None: return "wrong_side"
                fields, values = [], []
                if name is not None: fields.append("name=%s"); values.append(name)
                if mute is not None: fields.append("mute=%s"); values.append(mute)
            elif row["protected_user_id"] == user_id:
                if name is not None or mute is not None: return "wrong_side"
                fields, values = [], []
                if inverse_name is not None: fields.append("inverse_name=%s"); values.append(inverse_name)
            else: return "not_participant"
            if fields:
                await self.conn.execute(f"UPDATE guard_relation SET {','.join(fields)} WHERE id=%s", (*values, relation_id))
            return "updated"

    async def end(self, relation_id: int, user_id: int) -> str:
        async with self.conn.transaction():
            row = await (await self.conn.execute("SELECT * FROM guard_relation WHERE id=%s", (relation_id,))).fetchone()
            if row is None or user_id not in (row["protector_user_id"], row["protected_user_id"]):
                return "not_found"
            if row["ended_at"] is not None: return "already_ended"
            reason = "by_protector" if row["protector_user_id"] == user_id else "by_protected"
            now = utc_timestamp()
            await self.conn.execute("UPDATE guard_relation SET ended_at=to_timestamp(%s),end_reason=%s WHERE id=%s AND ended_at IS NULL",
                              (now, reason, relation_id))
            if reason == "by_protector":
                await self.conn.execute("UPDATE invite_code SET revoked_at=to_timestamp(%s) WHERE creator_user_id=%s AND used_at IS NULL AND revoked_at IS NULL",
                                  (now, user_id))
            return reason


@_pool_repo_access
class InviteRepo(_Repo):

    async def create(self, creator_id: int, name: str, ttl_days: int, max_relations: int = 10) -> dict:
        now = utc_timestamp()
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        expires = now + ttl_days * 86400
        async with self.conn.transaction():
            count = (await (await self.conn.execute("SELECT COUNT(*) n FROM guard_relation WHERE ended_at IS NULL "
                                      "AND (protector_user_id=%s OR protected_user_id=%s)", (creator_id, creator_id))).fetchone())["n"]
            if int(count) >= max_relations:
                raise ValidationError("relation limit reached")
            cur = await self.conn.execute(
                "INSERT INTO invite_code(code,creator_user_id,name,created_at,expires_at) "
                "VALUES(%s,%s,%s,to_timestamp(%s),to_timestamp(%s)) RETURNING id",
                (code, creator_id, name, now, expires))
        return {"id": int((await cur.fetchone())["id"]), "code": code, "expires_at": expires}

    async def get(self, code: str) -> dict | None:
        row = await (await self.conn.execute("SELECT * FROM invite_code WHERE UPPER(code)=UPPER(%s)", (code,))).fetchone()
        return dict(row) if row else None

    async def get_valid(self, code: str) -> dict | None:
        row = await (await self.conn.execute("SELECT * FROM invite_code WHERE UPPER(code)=UPPER(%s) AND used_at IS NULL "
                                "AND revoked_at IS NULL AND expires_at>to_timestamp(%s)", (code, utc_timestamp()))).fetchone()
        return dict(row) if row else None

    async def list_for_creator(self, user_id: int) -> list[dict]:
        return [dict(r) for r in await (await self.conn.execute(
            "SELECT id,code,name,created_at,expires_at,used_at,used_by_user_id,revoked_at FROM invite_code "
            "WHERE creator_user_id=%s ORDER BY id DESC", (user_id,))).fetchall()]

    async def revoke(self, invite_id: int, creator_id: int) -> str:
        async with self.conn.transaction():
            row = await (await self.conn.execute("SELECT * FROM invite_code WHERE id=%s AND creator_user_id=%s", (invite_id, creator_id))).fetchone()
            if row is None: return "not_found"
            if row["used_at"] is not None: return "used"
            if row["revoked_at"] is not None: return "revoked"
            await self.conn.execute("UPDATE invite_code SET revoked_at=to_timestamp(%s) WHERE id=%s AND used_at IS NULL AND revoked_at IS NULL",
                              (utc_timestamp(), invite_id))
            return "revoked"

    async def claim(self, code: str, protected_id: int, max_relations: int) -> tuple[str, int | None]:
        """Consume a code and create its directed relation in one write transaction."""
        now = utc_timestamp()
        async with self.conn.transaction():
            row = await (await self.conn.execute("SELECT * FROM invite_code WHERE UPPER(code)=UPPER(%s) FOR UPDATE", (code,))).fetchone()
            if row is None: reason = "invalid"
            elif row["used_at"] is not None: reason = "used"
            elif row["revoked_at"] is not None: reason = "revoked"
            elif row["expires_at"] <= now: reason = "expired"
            elif int(row["creator_user_id"]) == protected_id: reason = "self"
            else:
                creator_id = int(row["creator_user_id"])
                # Different invite rows can still compete for either participant's
                # MAX_RELATIONS capacity; stable lock order serializes those checks.
                await self.conn.execute(
                    'SELECT id FROM "user" WHERE id = ANY(%s) ORDER BY id FOR UPDATE',
                    ([creator_id, protected_id],),
                )
                exists = await (await self.conn.execute("SELECT 1 FROM guard_relation WHERE protector_user_id=%s AND protected_user_id=%s AND ended_at IS NULL",
                                           (creator_id, protected_id))).fetchone()
                if exists: reason = "already_exists"
                else:
                    ids = (creator_id, protected_id)
                    counts = [(await (await self.conn.execute("SELECT COUNT(*) n FROM guard_relation WHERE ended_at IS NULL AND (protector_user_id=%s OR protected_user_id=%s)", (uid, uid))).fetchone())["n"] for uid in ids]
                    if any(int(count) >= max_relations for count in counts): reason = "limit"
                    else:
                        cur = await self.conn.execute("INSERT INTO guard_relation(protector_user_id,protected_user_id,name,created_at) "
                                                "VALUES(%s,%s,%s,to_timestamp(%s)) RETURNING id",
                                                (creator_id, protected_id, row["name"], now))
                        claimed = await self.conn.execute("UPDATE invite_code SET used_at=to_timestamp(%s),used_by_user_id=%s WHERE id=%s AND used_at IS NULL AND revoked_at IS NULL AND expires_at>to_timestamp(%s)",
                                                    (now, protected_id, row["id"], now))
                        if claimed.rowcount != 1: raise ValidationError("invite became unavailable")
                        reason = "created"; relation_id = int((await cur.fetchone())["id"])
        return (reason, relation_id) if reason == "created" else (reason, None)


@_pool_repo_access
class QueryRepo(_Repo):

    async def insert(self, user_id: int, content_type: str, content: str, msg_id: str | None,
               kind: str = "query") -> int:
        try:
            async with self.conn.transaction():
                now = utc_timestamp()
                cur = await self.conn.execute("INSERT INTO query(user_id,content_type,content,msg_id,created_at,kind) "
                                        "VALUES(%s,%s,%s,%s,to_timestamp(%s),%s) RETURNING id",
                                        (user_id, content_type, content, msg_id, now, kind))
                query_id = int((await cur.fetchone())["id"])
                await self.conn.execute("INSERT INTO query_relation(query_id,relation_id) "
                                  "SELECT %s,id FROM guard_relation WHERE protected_user_id=%s AND ended_at IS NULL",
                                  (query_id, user_id))
        except UniqueViolation as exc:
            # 只把 MsgId 幂等键冲突映射为重复消息;其余约束违例原样抛出,不冒充重复
            if msg_id and exc.diag.constraint_name == "uq_query_msg_id":
                raise DuplicateMessage(msg_id) from exc
            raise
        return query_id

    async def find_by_msg_id(self, msg_id: str) -> dict | None:
        row = await (await self.conn.execute("SELECT id,user_id FROM query WHERE msg_id=%s", (msg_id,))).fetchone()
        return dict(row) if row else None

    async def get(self, query_id: int) -> dict | None:
        row = await (await self.conn.execute("SELECT * FROM query WHERE id=%s", (query_id,))).fetchone()
        return dict(row) if row else None

    async def record_degraded_reply(self, query_id: int, reply: str) -> None:
        async with self.conn.transaction():
            await self.conn.execute(
                "UPDATE query SET degraded_reply=%s WHERE id=%s", (reply, query_id)
            )

    async def update_transcript(self, query_id: int, transcript: str) -> None:
        async with self.conn.transaction(): await self.conn.execute("UPDATE query SET transcript=%s WHERE id=%s AND content_type='image'", (transcript, query_id))

    async def supply_context(self, query_id: int, user_id: int, window_seconds: int) -> list[dict]:
        rows = await (await self.conn.execute(
            "SELECT prior.id,prior.content_type,prior.content,prior.transcript,prior.created_at,prior.incident_id,"
            "cur.incident_id AS current_incident_id,cur.created_at AS current_created_at FROM query cur JOIN query prior "
            "ON prior.user_id=cur.user_id WHERE cur.id=%s AND cur.user_id=%s AND cur.kind='query' AND cur.incident_id IS NOT NULL "
            "AND prior.kind='query' AND prior.id<cur.id "
            "AND prior.created_at>=cur.created_at-(%s * interval '1 second') "
            "ORDER BY prior.created_at DESC,prior.id DESC LIMIT 200", (query_id, user_id, window_seconds))).fetchall()
        return [dict(r) for r in rows]

    async def list_relations_for_query(self, query_id: int, active_only: bool = False) -> list[dict]:
        sql = "SELECT r.* FROM query_relation qr JOIN guard_relation r ON r.id=qr.relation_id WHERE qr.query_id=%s"
        if active_only: sql += " AND r.ended_at IS NULL"
        return [dict(r) for r in await (await self.conn.execute(sql + " ORDER BY r.id", (query_id,))).fetchall()]

    async def list_for_user(self, user_id: int, limit: int = 100) -> list[dict]:
        return [dict(r) for r in await (await self.conn.execute(
            "SELECT v.id verdict_id,v.query_id,v.level,v.created_at verdict_at,c.status correction_status,c.resolved_label "
            "FROM query q JOIN verdict v ON v.query_id=q.id LEFT JOIN correction_case c ON c.verdict_id=v.id "
            "WHERE q.user_id=%s ORDER BY v.id DESC LIMIT %s", (user_id, limit))).fetchall()]

    async def get_my_detail(self, user_id: int, verdict_id: int) -> dict | None:
        row = await (await self.conn.execute("SELECT v.id verdict_id,v.query_id,v.level,v.reason,v.reply,v.created_at verdict_at,"
                                "q.content,q.content_type,c.id case_id,c.status correction_status,c.resolved_label,"
                                "c.queryer_label,c.queryer_note FROM verdict v JOIN query q ON q.id=v.query_id "
                                "LEFT JOIN correction_case c ON c.verdict_id=v.id WHERE v.id=%s AND q.user_id=%s",
                                (verdict_id, user_id))).fetchone()
        return dict(row) if row else None


@_pool_repo_access
class IncidentRepo(_Repo):

    async def current_epoch(self, user_id: int) -> int:
        row = await (await self.conn.execute('SELECT session_epoch FROM "user" WHERE id=%s', (user_id,))).fetchone()
        if row is None: raise ValidationError("user not found")
        return int(row["session_epoch"])

    async def attach_query_to_incident(self, query_id: int, user_id: int, idle_seconds: int,
                                 expected_epoch: int | None = None) -> int | None:
        async with self.conn.transaction():
            # Serialize even when no open incident row exists yet; the partial unique index remains the backstop.
            await self.conn.execute('SELECT id FROM "user" WHERE id=%s FOR UPDATE', (user_id,))
            if expected_epoch is not None and await self.current_epoch(user_id) != expected_epoch: return None
            query = await (await self.conn.execute("SELECT created_at FROM query WHERE id=%s AND user_id=%s AND kind='query'", (query_id, user_id))).fetchone()
            if query is None: raise ValidationError("query not found")
            now = int(query["created_at"])
            inc = await (await self.conn.execute(
                "SELECT id,opened_at,last_query_at FROM incident WHERE user_id=%s AND closed_at IS NULL FOR UPDATE",
                (user_id,),
            )).fetchone()
            if inc is not None and now < int(inc["opened_at"]) - idle_seconds: return None
            if inc is not None and now - int(inc["last_query_at"]) <= idle_seconds:
                incident_id = int(inc["id"])
                await self.conn.execute("UPDATE incident SET last_query_at=to_timestamp(%s) WHERE id=%s",
                                  (max(now, int(inc["last_query_at"])), incident_id))
            else:
                if inc is not None:
                    await self.conn.execute("UPDATE incident SET closed_at=to_timestamp(%s),close_reason='timeout' WHERE id=%s",
                                      (now, inc["id"]))
                cur = await self.conn.execute(
                    "INSERT INTO incident(user_id,opened_at,last_query_at) VALUES(%s,to_timestamp(%s),to_timestamp(%s)) RETURNING id",
                    (user_id, now, now),
                )
                incident_id = int((await cur.fetchone())["id"])
            await self.conn.execute("UPDATE query SET incident_id=%s WHERE id=%s", (incident_id, query_id))
            return incident_id

    async def close_open_incident(self, user_id: int, reason: str = "explicit", msg_id: str | None = None) -> bool:
        if reason not in {"explicit", "manual"}: raise ValidationError("invalid close reason")
        async with self.conn.transaction():
            if msg_id:
                try:
                    await self.conn.execute("INSERT INTO session_reset_msg(msg_id,user_id,created_at) VALUES(%s,%s,to_timestamp(%s))",
                                      (msg_id, user_id, utc_timestamp()))
                except UniqueViolation as exc:
                    if exc.diag.constraint_name != "session_reset_msg_pkey": raise
                    return False
            await self.conn.execute('UPDATE "user" SET session_epoch=session_epoch+1 WHERE id=%s', (user_id,))
            await self.conn.execute("UPDATE incident SET closed_at=to_timestamp(%s),close_reason=%s WHERE user_id=%s AND closed_at IS NULL",
                              (utc_timestamp(), reason, user_id))
        return True

    async def list_for_user(self, user_id: int) -> list[dict]:
        return [dict(r) for r in await (await self.conn.execute("SELECT i.*,COUNT(q.id) AS query_count FROM incident i LEFT JOIN query q "
               "ON q.incident_id=i.id AND q.kind='query' WHERE i.user_id=%s GROUP BY i.id ORDER BY i.id DESC", (user_id,))).fetchall()]


@_pool_repo_access
class VerdictRepo(_Repo):

    async def insert(self, query_id: int, level: Level, cited_ids: list[str], features_snapshot: list[dict], reason: str,
               reply: str, latency_ms: int, mode: Mode, context_snapshot: str | None = None) -> int:
        context = json.loads(context_snapshot) if isinstance(context_snapshot, str) else context_snapshot
        async with self.conn.transaction():
            cur = await self.conn.execute(
                "INSERT INTO verdict(query_id,level,cited_ids,features,reason,reply,latency_ms,mode,created_at,context_snapshot) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,to_timestamp(%s),%s) RETURNING id",
                (query_id, level.value, Jsonb(cited_ids), Jsonb(features_snapshot), reason, reply,
                 latency_ms, mode.value, utc_timestamp(), Jsonb(context) if context is not None else None),
            )
            verdict_id = int((await cur.fetchone())["id"])
            await self.conn.execute(
                "SELECT pg_notify('verdict_completed', %s)",
                (json.dumps({"verdict_id": verdict_id, "query_id": query_id}),),
            )
        return verdict_id

    async def get(self, verdict_id: int) -> dict | None:
        row = await (await self.conn.execute("SELECT * FROM verdict WHERE id=%s", (verdict_id,))).fetchone()
        return dict(row) if row else None

    async def notification_context(self, verdict_id: int) -> dict | None:
        row = await (await self.conn.execute(
            "SELECT v.id verdict_id,v.query_id,v.level,v.cited_ids,q.user_id,q.content_type,q.content,q.created_at "
            "FROM verdict v JOIN query q ON q.id=v.query_id WHERE v.id=%s",
            (verdict_id,),
        )).fetchone()
        return dict(row) if row else None


@_pool_repo_access
class AlertRepo(_Repo):

    async def record_alerts_for_verdict(self, verdict_id: int, query_id: int) -> dict:
        now = utc_timestamp()
        async with self.conn.transaction():
            queryer = await (await self.conn.execute("SELECT user_id FROM query WHERE id=%s", (query_id,))).fetchone()
            if queryer is None: return {"queryer_id": None, "recipients": []}
            recipients = []
            rows = await (await self.conn.execute("SELECT r.id relation_id,r.protector_user_id user_id,r.name,r.inverse_name,r.mute,u.openid,u.token "
                'FROM query_relation qr JOIN guard_relation r ON r.id=qr.relation_id JOIN "user" u ON u.id=r.protector_user_id '
                "WHERE qr.query_id=%s AND r.ended_at IS NULL ORDER BY r.id", (query_id,))).fetchall()
            for row in rows:
                inserted = await self.conn.execute("INSERT INTO alert(verdict_id,relation_id,name_at_alert,delivered_at) "
                                  "VALUES(%s,%s,%s,to_timestamp(%s)) ON CONFLICT(verdict_id,relation_id) DO NOTHING RETURNING id",
                                  (verdict_id, row["relation_id"], row["name"], now))
                created = await inserted.fetchone() is not None
                alert = await (await self.conn.execute("SELECT id,delivered_at FROM alert WHERE verdict_id=%s AND relation_id=%s",
                                          (verdict_id, row["relation_id"]))).fetchone()
                recipients.append({"relation_id": int(row["relation_id"]), "user_id": int(row["user_id"]),
                                   "openid": row["openid"], "token": row["token"], "name_at_alert": row["name"],
                                   "inverse_name": row["inverse_name"], "mute": bool(row["mute"]),
                                   "alert_id": int(alert["id"]), "delivered_at": int(alert["delivered_at"]),
                                   "newly_created": created})
            return {"queryer_id": int(queryer["user_id"]), "recipients": recipients}

    async def event_context(self, alert_id: int) -> dict | None:
        row = await (await self.conn.execute("SELECT a.id alert_id,a.verdict_id,a.relation_id,a.name_at_alert,a.delivered_at,"
            "r.protector_user_id user_id,r.mute FROM alert a JOIN guard_relation r ON r.id=a.relation_id "
            "WHERE a.id=%s AND r.ended_at IS NULL", (alert_id,))).fetchone()
        return dict(row) if row else None

    async def push_context(self, alert_id: int) -> dict | None:
        row = await (await self.conn.execute("SELECT a.id alert_id,a.verdict_id,a.relation_id,a.name_at_alert,a.delivered_at,"
            "r.protector_user_id user_id,r.mute,u.openid,u.token FROM alert a JOIN guard_relation r ON r.id=a.relation_id "
            'JOIN "user" u ON u.id=r.protector_user_id WHERE a.id=%s AND r.ended_at IS NULL AND r.mute=FALSE', (alert_id,))).fetchone()
        return dict(row) if row else None

    async def list_for_user(self, user_id: int, relation_id: int | None = None, limit: int = 100) -> list[dict]:
        sql = ("SELECT a.id alert_id,a.verdict_id,a.relation_id,a.name_at_alert,a.delivered_at,a.read_at,v.level,q.content "
               "FROM alert a JOIN guard_relation r ON r.id=a.relation_id JOIN verdict v ON v.id=a.verdict_id "
               "JOIN query q ON q.id=v.query_id WHERE r.protector_user_id=%s AND r.ended_at IS NULL")
        params: list = [user_id]
        if relation_id is not None: sql += " AND a.relation_id=%s"; params.append(relation_id)
        sql += " ORDER BY a.id DESC LIMIT %s"; params.append(limit)
        return [dict(r) for r in await (await self.conn.execute(sql, params)).fetchall()]

    async def detail_for_user(self, user_id: int, alert_id: int) -> tuple[dict | None, str | None]:
        row = await (await self.conn.execute("SELECT a.id alert_id,a.verdict_id,a.relation_id,a.name_at_alert,a.delivered_at,"
            "v.level,v.reply,v.created_at verdict_at,q.content,q.content_type,c.id case_id,c.status correction_status,"
            "c.resolved_label,cv.label my_vote,cv.voted_at FROM alert a JOIN guard_relation r ON r.id=a.relation_id "
            "JOIN verdict v ON v.id=a.verdict_id JOIN query q ON q.id=v.query_id "
            "LEFT JOIN correction_case c ON c.verdict_id=v.id LEFT JOIN correction_vote cv ON cv.case_id=c.id AND cv.relation_id=r.id "
            "WHERE a.id=%s AND r.protector_user_id=%s", (alert_id, user_id))).fetchone()
        if row is None: return None, None
        if (await (await self.conn.execute("SELECT ended_at FROM guard_relation WHERE id=%s", (row["relation_id"],))).fetchone())["ended_at"] is not None:
            return None, "relation_ended"
        return dict(row), None

    async def mark_read(self, alert_id: int) -> None:
        async with self.conn.transaction():
            await self.conn.execute(
                "UPDATE alert SET read_at=COALESCE(read_at,to_timestamp(%s)) WHERE id=%s",
                (utc_timestamp(), alert_id),
            )


@_pool_repo_access
class CorrectionRepo(_Repo):

    async def submit(self, verdict_id: int, user_id: int, label: str, note: str, window_days: int) -> dict:
        """Open a case, save queryer feedback, or cast one relation vote atomically."""
        now = utc_timestamp()
        async with self.conn.transaction():
            verdict = await (await self.conn.execute("SELECT v.id,v.level,v.query_id,q.user_id queryer_id,q.content FROM verdict v "
                                        "JOIN query q ON q.id=v.query_id WHERE v.id=%s FOR UPDATE OF v", (verdict_id,))).fetchone()
            if verdict is None: raise ValidationError("verdict not found")
            if verdict["level"] not in ("safe", "suspicious", "dangerous"):
                raise ValidationError("verdict is not eligible for correction")
            case = await (await self.conn.execute("SELECT * FROM correction_case WHERE verdict_id=%s", (verdict_id,))).fetchone()
            if case and case["status"] == "pending" and now >= int(case["closes_at"]):
                await self.conn.execute("UPDATE correction_case SET status='no_consensus',resolved_at=to_timestamp(%s) WHERE id=%s AND status='pending'", (now, case["id"]))
                case = await (await self.conn.execute("SELECT * FROM correction_case WHERE id=%s", (case["id"],))).fetchone()
            is_queryer = int(verdict["queryer_id"]) == user_id
            relation_id = None
            if not is_queryer:
                if case is not None:
                    rel = await (await self.conn.execute("SELECT r.id FROM correction_vote cv JOIN guard_relation r ON r.id=cv.relation_id "
                        "WHERE cv.case_id=%s AND r.protector_user_id=%s AND r.ended_at IS NULL", (case["id"], user_id))).fetchone()
                else:
                    if verdict["level"] != "dangerous": raise ValidationError("only queryer can open low-risk correction")
                    rel = await (await self.conn.execute("SELECT r.id FROM guard_relation r JOIN alert a ON a.relation_id=r.id "
                        "WHERE a.verdict_id=%s AND r.protector_user_id=%s AND r.ended_at IS NULL", (verdict_id, user_id))).fetchone()
                if rel is None: raise ValidationError("no active voting relation")
                relation_id = int(rel["id"])
            if case is None:
                if not is_queryer and verdict["level"] != "dangerous": raise ValidationError("only queryer can open correction")
                opened = now; closes = now + window_days * 86400
                cur = await self.conn.execute(
                    "INSERT INTO correction_case(verdict_id,queryer_label,queryer_note,queryer_feedback_at,status,opened_at,closes_at) "
                    "VALUES(%s,NULL,'',NULL,'pending',to_timestamp(%s),to_timestamp(%s)) "
                    "ON CONFLICT(verdict_id) DO NOTHING RETURNING id",
                    (verdict_id, opened, closes),
                )
                created = await cur.fetchone()
                if created:
                    case_id = int(created["id"])
                    if verdict["level"] == "dangerous":
                        eligible = await (await self.conn.execute("SELECT DISTINCT r.id FROM alert a JOIN guard_relation r ON r.id=a.relation_id "
                            "WHERE a.verdict_id=%s AND r.ended_at IS NULL", (verdict_id,))).fetchall()
                    else:
                        eligible = await (await self.conn.execute("SELECT r.id FROM query_relation qr JOIN guard_relation r ON r.id=qr.relation_id "
                            "WHERE qr.query_id=%s AND r.ended_at IS NULL", (verdict["query_id"],))).fetchall()
                    async with self.conn.cursor() as cursor:
                        await cursor.executemany("INSERT INTO correction_vote(case_id,relation_id) VALUES(%s,%s)",
                                           [(case_id, int(r["id"])) for r in eligible])
                    if not eligible:
                        await self.conn.execute("UPDATE correction_case SET status='no_consensus',resolved_at=to_timestamp(%s) WHERE id=%s",
                                          (now, case_id))
                case = await (await self.conn.execute("SELECT * FROM correction_case WHERE verdict_id=%s FOR UPDATE", (verdict_id,))).fetchone()
            case_id = int(case["id"])
            if is_queryer:
                if case["queryer_label"] is not None:
                    if case["queryer_label"] != label:
                        raise ValidationError("queryer feedback cannot be changed")
                    return await self.case_summary(case_id)
                await self.conn.execute("UPDATE correction_case SET queryer_label=%s,queryer_note=%s,queryer_feedback_at=to_timestamp(%s) WHERE id=%s",
                                  (label, note, now, case_id))
            else:
                eligible = await (await self.conn.execute("SELECT label FROM correction_vote WHERE case_id=%s AND relation_id=%s", (case_id, relation_id))).fetchone()
                if eligible is None: raise ValidationError("relation is not eligible for this correction")
                if eligible["label"] is not None:
                    if eligible["label"] != label: raise ValidationError("vote cannot be changed")
                    return await self.case_summary(case_id)
                else:
                    if case["status"] != "pending": raise ValidationError("correction case is closed")
                    await self.conn.execute("UPDATE correction_vote SET label=%s,voted_at=to_timestamp(%s) WHERE case_id=%s AND relation_id=%s AND label IS NULL",
                                      (label, now, case_id, relation_id))
                    total = int((await (await self.conn.execute("SELECT COUNT(*) n FROM correction_vote WHERE case_id=%s", (case_id,))).fetchone())["n"])
                    required = total // 2 + 1
                    counts = await (await self.conn.execute("SELECT label,COUNT(*) n FROM correction_vote WHERE case_id=%s AND label IS NOT NULL GROUP BY label", (case_id,))).fetchall()
                    winner = next((r["label"] for r in counts if int(r["n"]) >= required), None)
                    if winner:
                        await self.conn.execute("UPDATE correction_case SET status='confirmed',resolved_label=%s,resolved_at=to_timestamp(%s) WHERE id=%s AND status='pending'",
                                          (winner, now, case_id))
            return await self.case_summary(case_id)

    async def close_expired(self) -> int:
        now = utc_timestamp()
        async with self.conn.transaction():
            cur = await self.conn.execute("UPDATE correction_case SET status='no_consensus',resolved_at=to_timestamp(%s) "
                                    "WHERE status='pending' AND closes_at<=to_timestamp(%s)", (now, now))
        return cur.rowcount

    async def case_summary(self, case_id: int) -> dict:
        row = await (await self.conn.execute("SELECT * FROM correction_case WHERE id=%s", (case_id,))).fetchone()
        if row is None: raise ValidationError("correction case not found")
        counts = {r["label"]: int(r["n"]) for r in await (await self.conn.execute(
            "SELECT label,COUNT(*) n FROM correction_vote WHERE case_id=%s AND label IS NOT NULL GROUP BY label", (case_id,))).fetchall()}
        total = int((await (await self.conn.execute("SELECT COUNT(*) n FROM correction_vote WHERE case_id=%s", (case_id,))).fetchone())["n"])
        return {"case_id": int(row["id"]), "verdict_id": int(row["verdict_id"]), "status": row["status"],
                "resolved_label": row["resolved_label"], "votes": counts, "eligible_count": total,
                "required_votes": total // 2 + 1 if total else 0, "closes_at": int(row["closes_at"]),
                "queryer_label": row["queryer_label"], "queryer_note": row["queryer_note"]}

    async def list_pending_for_user(self, user_id: int) -> list[dict]:
        await self.close_expired()
        rows = await (await self.conn.execute("SELECT c.id case_id,c.verdict_id,c.closes_at,v.level,q.content,q.created_at query_at,"
            "cv.label my_vote,COUNT(allv.relation_id) eligible_count FROM correction_case c "
            "JOIN correction_vote cv ON cv.case_id=c.id JOIN guard_relation r ON r.id=cv.relation_id "
            "JOIN verdict v ON v.id=c.verdict_id JOIN query q ON q.id=v.query_id "
            "LEFT JOIN correction_vote allv ON allv.case_id=c.id WHERE c.status='pending' AND r.protector_user_id=%s "
            "AND r.ended_at IS NULL GROUP BY c.id,c.verdict_id,c.closes_at,v.level,q.content,q.created_at,"
            "cv.relation_id,cv.label,c.opened_at ORDER BY c.opened_at", (user_id,))).fetchall()
        return [{**dict(r), "required_votes": int(r["eligible_count"]) // 2 + 1} for r in rows]

    async def detail_for_relation(self, case_id: int, relation_id: int, user_id: int) -> dict | None:
        await self.close_expired()
        row = await (await self.conn.execute("SELECT c.id case_id,c.verdict_id,c.status,c.resolved_label,c.closes_at,c.queryer_label,"
            "c.queryer_note,v.level,q.content,cv.label my_vote,cv.voted_at FROM correction_case c "
            "JOIN correction_vote cv ON cv.case_id=c.id JOIN guard_relation r ON r.id=cv.relation_id "
            "JOIN verdict v ON v.id=c.verdict_id JOIN query q ON q.id=v.query_id "
            "WHERE c.id=%s AND cv.relation_id=%s AND r.protector_user_id=%s AND r.ended_at IS NULL",
            (case_id, relation_id, user_id))).fetchone()
        return dict(row) if row else None

    async def get_case_for_verdict(self, verdict_id: int) -> dict | None:
        row = await (await self.conn.execute("SELECT id FROM correction_case WHERE verdict_id=%s", (verdict_id,))).fetchone()
        return await self.case_summary(int(row["id"])) if row else None

    async def confirmed_labels(self) -> list[dict]:
        return [dict(r) for r in await (await self.conn.execute("SELECT v.id verdict_id,c.resolved_label FROM correction_case c "
            "JOIN verdict v ON v.id=c.verdict_id WHERE c.status='confirmed'")).fetchall()]


@_pool_repo_access
class WecomMemberRepo(_Repo):
    """wxkf 用户 ↔ 企业微信成员 userid 的映射;应用消息告警按此触达微信插件。

    绑定生命周期由 bound_via/bound_at/verified_at/last_fail_* 表达,四态语义
    (未绑定/已绑定未确认/送达就绪/异常)在 core/push.py 计算;重绑一律重开确认窗口。
    """

    async def link(self, user_id: int, corp_userid: str, via: str = "cli") -> None:
        # corp_userid 亦有 UNIQUE 约束:成员改绑到另一用户时先腾出旧映射。
        # The savepoint keeps the transaction usable if another worker wins the unique race.
        async with self.conn.transaction():
            for attempt in range(2):
                try:
                    async with self.conn.transaction():
                        await self.conn.execute("DELETE FROM wecom_member WHERE corp_userid=%s AND user_id<>%s",
                                          (corp_userid, user_id))
                        await self.conn.execute(
                            "INSERT INTO wecom_member(user_id,corp_userid,bound_via,bound_at) "
                            "VALUES(%s,%s,%s,to_timestamp(%s)) "
                            "ON CONFLICT(user_id) DO UPDATE SET corp_userid=excluded.corp_userid,"
                            "bound_via=excluded.bound_via,bound_at=excluded.bound_at,"
                            "verified_at=NULL,last_fail_at=NULL,last_fail_reason=NULL",
                            (user_id, corp_userid, via, utc_timestamp()))
                    return
                except UniqueViolation as exc:
                    if exc.diag.constraint_name != "wecom_member_corp_userid_key" or attempt:
                        raise

    async def get(self, user_id: int) -> str | None:
        row = await (await self.conn.execute("SELECT corp_userid FROM wecom_member WHERE user_id=%s",
                                (user_id,))).fetchone()
        return row["corp_userid"] if row else None

    async def get_member(self, user_id: int) -> dict | None:
        row = await (await self.conn.execute(
            "SELECT user_id,corp_userid,bound_via,bound_at,verified_at,last_fail_at,last_fail_reason "
            "FROM wecom_member WHERE user_id=%s", (user_id,))).fetchone()
        return dict(row) if row else None

    async def unbind(self, user_id: int) -> bool:
        cur = await self.conn.execute("DELETE FROM wecom_member WHERE user_id=%s", (user_id,))
        return cur.rowcount > 0

    async def mark_verified(self, user_id: int) -> None:
        await self.conn.execute("UPDATE wecom_member SET verified_at=to_timestamp(%s) WHERE user_id=%s",
                          (utc_timestamp(), user_id))

    async def mark_failed(self, user_id: int, reason: str) -> None:
        await self.conn.execute("UPDATE wecom_member SET last_fail_at=to_timestamp(%s),last_fail_reason=%s "
                          "WHERE user_id=%s", (utc_timestamp(), reason[:200], user_id))

    async def touch_confirm(self, user_id: int) -> None:
        """重测:刷新确认窗口起点并清除失败信号,状态回到已绑定（未确认）。"""
        await self.conn.execute("UPDATE wecom_member SET bound_at=to_timestamp(%s),"
                          "last_fail_at=NULL,last_fail_reason=NULL WHERE user_id=%s",
                          (utc_timestamp(), user_id))


@_pool_repo_access
class KfCursorRepo(_Repo):
    """Durable per-customer-service-account sync cursor."""

    async def get(self, kfid: str) -> str | None:
        row = await (await self.conn.execute("SELECT cursor FROM kf_cursor WHERE kfid=%s", (kfid,))).fetchone()
        return row["cursor"] if row else None

    async def set(self, kfid: str, cursor: str) -> None:
        await self.conn.execute(
            "INSERT INTO kf_cursor(kfid,cursor) VALUES(%s,%s) "
            "ON CONFLICT(kfid) DO UPDATE SET cursor=excluded.cursor,updated_at=now()",
            (kfid, cursor),
        )


@dataclass
class Repos:
    pool: AsyncConnectionPool
    users: UserRepo
    relation: RelationRepo
    invite: InviteRepo
    query: QueryRepo
    verdict: VerdictRepo
    alert: AlertRepo
    correction: CorrectionRepo
    incident: IncidentRepo
    wecom_member: WecomMemberRepo
    kf_cursor: KfCursorRepo


def make_repos(pool: AsyncConnectionPool) -> Repos:
    return Repos(pool, UserRepo(pool), RelationRepo(pool), InviteRepo(pool), QueryRepo(pool),
                 VerdictRepo(pool), AlertRepo(pool), CorrectionRepo(pool), IncidentRepo(pool),
                 WecomMemberRepo(pool), KfCursorRepo(pool))

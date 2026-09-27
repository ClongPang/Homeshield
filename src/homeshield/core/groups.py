"""防护群管理与退群生命周期。"""
from homeshield.core.errors import ValidationError
from homeshield.core.models import utcnow
from homeshield.core.repo import Repos, WRITE_LOCK


class GroupService:
    def __init__(self, repos: Repos, max_members: int):
        self.repos = repos
        self.max_members = max_members

    def create_member_slot(self, family_id: int, name: str, trusted: bool = False) -> int:
        if trusted:
            raise ValidationError("an unbound invitation cannot be trusted")
        return self.add_member(family_id, name)

    def add_member(self, family_id: int, name: str, trusted: bool = False,
                   openid: str | None = None) -> int:
        name = name.strip()
        if not name:
            raise ValidationError("name is empty")
        conn = self.repos.conn
        with WRITE_LOCK, conn:
            fam = conn.execute("SELECT disbanded_at FROM family WHERE id=?", (family_id,)).fetchone()
            if fam is None or fam["disbanded_at"] is not None:
                raise ValidationError("group not found")
            count = conn.execute(
                "SELECT COUNT(*) FROM member WHERE family_id=? AND ended_at IS NULL", (family_id,)
            ).fetchone()[0]
            if count >= self.max_members:
                raise ValidationError("family reached max members")
            user_id = self.repos.users.get_or_create(openid).id if openid else None
            if trusted and user_id is None:
                raise ValidationError("an unbound invitation cannot be trusted")
            cur = conn.execute(
                "INSERT INTO member(family_id,user_id,name,trusted,created_at) VALUES(?,?,?,?,?)",
                (family_id, user_id, name, int(trusted), utcnow()),
            )
            return int(cur.lastrowid)

    def rename(self, family_id: int, name: str) -> None:
        name = name.strip()
        if not name:
            raise ValidationError("name is empty")
        with WRITE_LOCK, self.repos.conn:
            cur = self.repos.conn.execute(
                "UPDATE family SET name=? WHERE id=? AND disbanded_at IS NULL", (name,family_id),
            )
            if cur.rowcount != 1:
                raise ValidationError("group not found")

    def set_trust(self, family_id: int, target_id: int, trusted: bool, actor_id: int) -> None:
        with WRITE_LOCK:
            actor = self.repos.member.get(actor_id)
            if actor is None or actor.family_id != family_id or actor.ended_at is not None or not actor.trusted:
                raise ValidationError("only active trusted member can manage members")
            if target_id == actor_id:
                raise ValidationError("cannot change own trust")
            target = self.repos.member.get(target_id)
            if target is None or target.family_id != family_id or target.user_id is None or target.ended_at is not None:
                raise ValidationError("member not found")
            if trusted:
                self.repos.member.set_trust(target_id, True)
            elif target.trusted and not self.repos.member.demote_with_guard(target_id,family_id):
                raise ValidationError("cannot demote the last trusted member")

    def set_trust_by_operator(self, member_id: int, trusted: bool) -> None:
        with WRITE_LOCK:
            target = self.repos.member.get(member_id)
            if target is None or target.user_id is None or target.ended_at is not None:
                raise ValidationError("only active bound members can be trusted")
            if trusted:
                self.repos.member.set_trust(member_id, True)
            elif target.trusted and not self.repos.member.demote_with_guard(member_id,target.family_id):
                raise ValidationError("cannot demote the last trusted member")

    def set_mute(self, family_id: int, user_id: int, mute: bool) -> None:
        with WRITE_LOCK:
            member = next((m for m in self.repos.member.list_for_user(user_id) if m.family_id == family_id),None)
            if member is None:
                raise ValidationError("group not found")
            self.repos.member.set_mute(member.id,mute)

    def leave(self, user_id: int, family_id: int) -> str:
        member = next((m for m in self.repos.member.list_for_user(user_id) if m.family_id==family_id),None)
        if member is None:
            raise ValidationError("not a member")
        return self._terminate(family_id,member.id,"left")

    def remove(self, family_id: int, member_id: int) -> str:
        target = self.repos.member.get(member_id)
        if target is None or target.family_id != family_id or target.ended_at is not None:
            raise ValidationError("member not found")
        return self._terminate(family_id,member_id,"removed")

    def _terminate(self, family_id: int, member_id: int, reason: str) -> str:
        now = utcnow()
        conn = self.repos.conn
        with WRITE_LOCK, conn:
            family = conn.execute("SELECT * FROM family WHERE id=?",(family_id,)).fetchone()
            target = conn.execute("SELECT * FROM member WHERE id=? AND family_id=? AND ended_at IS NULL",(member_id,family_id)).fetchone()
            if not family or family["disbanded_at"] is not None or not target:
                raise ValidationError("member not found")
            bound = conn.execute(
                "SELECT COUNT(*) FROM member WHERE family_id=? AND user_id IS NOT NULL AND ended_at IS NULL",(family_id,),
            ).fetchone()[0]
            trusted_others = conn.execute(
                "SELECT COUNT(*) FROM member WHERE family_id=? AND user_id IS NOT NULL AND trusted=1 "
                "AND ended_at IS NULL AND id<>?",(family_id,member_id),
            ).fetchone()[0]
            if target["user_id"] is not None and bound > 1 and target["trusted"] and trusted_others == 0:
                raise ValidationError("transfer trust before leaving")
            conn.execute("UPDATE member SET ended_at=?,end_reason=? WHERE id=?",(now,reason,member_id))
            conn.execute(
                "UPDATE bind_code SET used_at=? WHERE member_id=? AND used_at IS NULL", (now, member_id)
            )
            if target["user_id"] is not None and bound <= 1:
                conn.execute("UPDATE family SET disbanded_at=? WHERE id=? AND disbanded_at IS NULL",(now,family_id))
                conn.execute(
                    "UPDATE member SET ended_at=?,end_reason='disbanded' WHERE family_id=? AND ended_at IS NULL",
                    (now,family_id),
                )
                conn.execute(
                    "UPDATE bind_code SET used_at=? WHERE used_at IS NULL AND member_id IN "
                    "(SELECT id FROM member WHERE family_id=?)",(now,family_id),
                )
                return "disbanded"
            return "left"

    def disband(self, user_id: int, family_id: int) -> None:
        now = utcnow()
        conn = self.repos.conn
        with WRITE_LOCK, conn:
            family = conn.execute("SELECT * FROM family WHERE id=?",(family_id,)).fetchone()
            creator_member = conn.execute(
                "SELECT 1 FROM member WHERE family_id=? AND user_id=? AND trusted=1 AND ended_at IS NULL",
                (family_id,user_id),
            ).fetchone()
            if not family or family["disbanded_at"] is not None:
                raise ValidationError("group not found")
            if family["created_by_user_id"] != user_id or not creator_member:
                raise ValidationError("only active trusted creator can disband")
            conn.execute("UPDATE family SET disbanded_at=? WHERE id=?",(now,family_id))
            conn.execute(
                "UPDATE member SET ended_at=?,end_reason='disbanded' WHERE family_id=? AND ended_at IS NULL",
                (now,family_id),
            )
            conn.execute(
                "UPDATE bind_code SET used_at=? WHERE used_at IS NULL AND member_id IN "
                "(SELECT id FROM member WHERE family_id=?)",(now,family_id),
            )

    def disband_by_operator(self, family_id: int) -> None:
        """CLI 运维解散无创建者的群,与用户解散共用历史保留口径。"""
        now = utcnow()
        conn = self.repos.conn
        with WRITE_LOCK, conn:
            family = conn.execute("SELECT disbanded_at FROM family WHERE id=?", (family_id,)).fetchone()
            if family is None:
                raise ValidationError("group not found")
            if family["disbanded_at"] is not None:
                return
            conn.execute("UPDATE family SET disbanded_at=? WHERE id=?", (now, family_id))
            conn.execute(
                "UPDATE member SET ended_at=?,end_reason='disbanded' WHERE family_id=? AND ended_at IS NULL",
                (now, family_id),
            )
            conn.execute(
                "UPDATE bind_code SET used_at=? WHERE used_at IS NULL AND member_id IN "
                "(SELECT id FROM member WHERE family_id=?)", (now, family_id),
            )

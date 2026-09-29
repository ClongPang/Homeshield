"""会话指令处理:邀请 / 绑定 / 我的联防 / 解除,通道无关(企微客服会话与网页共用)。

输入是用户在会话里的一段文本,输出面向用户的回复文案;查询/判定不经过这里
(走 verification)。identity 为 user.openid 字符串,由通道层按自身规则生成
(企微为 wxkf: 前缀 + external_userid)。
"""
from homeshield.core.config import Settings
from homeshield.core.models import User
from homeshield.core.relations import (RelationError, RelationService, is_old_group_command,
                                       parse_bind_command, parse_end_command, parse_invite_command)
from homeshield.core.repo import Repos

OLD_COMMAND_HINT = "命令已更新：回复「邀请 称呼」发起联防，「我的联防」查看，「解除 称呼」停止。"


async def handle_relation_command(relations: RelationService, repos: Repos, settings: Settings,
                            user: User, kind: str, text: str) -> str | None:
    """关系指令的同步回复;非指令消息返回 None 交回判定链路。"""
    if kind != "text":
        return None
    if is_old_group_command(text):
        return OLD_COMMAND_HINT
    code = parse_bind_command(text)
    invite_name = parse_invite_command(text)
    end_selector = parse_end_command(text)
    if code is not None:
        try:
            _, relation_id, _ = await relations.join(user.openid, code)
        except RelationError as exc:
            return {
                "invalid": "这个邀请码无效。请让邀请者检查是否过期、已撤销或已使用。",
                "expired": "这个邀请码已过期，请让邀请者重新邀请。",
                "used": "这个邀请码已被使用。",
                "revoked": "这个邀请码已撤销。",
                "self": "不能绑定自己发出的邀请码。",
                "already_exists": "这条联防关系已经建立，无需重复绑定。",
                "limit": f"你或邀请者的活跃联防已达上限（{settings.max_relations} 条）。",
            }.get(exc.reason, "绑定暂时未完成，请稍后重试。")
        return ("已建立联防关系。对方会收到你的查询提醒；你主动纠正其他判定时，原查询内容也会供对方投票查看。"
                "回复「我的联防」查看，回复「解除 #" + str(relation_id) + "」可停止。")
    if invite_name is not None:
        try:
            invite = await relations.issue_invite(user.id, invite_name)
        except RelationError as exc:
            return f"活跃联防已达上限（{settings.max_relations} 条），请先解除一条再邀请。" if exc.reason == "limit" else "暂时无法生成邀请码。"
        url = f"{settings.public_base_url.rstrip('/')}/join/{invite['code']}" if settings.public_base_url else ""
        link = f"\n邀请链接：{url}" if url else ""
        return (f"邀请码：{invite['code']}{link}\nTA 绑定后，你将收到 TA 的查询提醒；"
                "TA 主动纠正低风险判定时，原查询也会供你投票查看。")
    if text.strip() == "我的联防":
        data = await relations.list_for_user(user.id)
        outgoing = [f"#{r['id']} {r['name']}" + ("（已静音）" if r["mute"] else "") for r in data["guardings"]]
        incoming = [f"#{r['id']} {r['name']}" for r in data["guardians"]]
        result = "我护着：" + ("、".join(outgoing) if outgoing else "暂无") + "\n护着我：" + ("、".join(incoming) if incoming else "暂无")
        if not outgoing and not incoming:
            result += "\n还没有联防。回复「邀请 称呼」发起联防；也可以直接转发可疑消息给我查。"
        url = user.entry_url(settings.public_base_url)
        if url:
            result += f"\n个人控制台：{url}"
        return result
    if end_selector is not None:
        status, matches = await relations.end_by_selector(user.id, end_selector)
        if status == "ambiguous":
            options = "、".join(f"#{r['id']} {r['display_name']}" for r in matches)
            return f"称呼重复，请用关系编号解除：{options}"
        if status == "not_found":
            return "没有找到这条联防关系。回复「我的联防」查看关系编号。"
        relation = matches[0]
        if status == "by_protector":
            return f"已解除 #{relation['id']}：对方不再收到你的查询提醒。"
        if status == "by_protected":
            return f"已解除 #{relation['id']}：你不再收到对方的查询提醒。"
        return "这条联防关系已解除。"
    return None

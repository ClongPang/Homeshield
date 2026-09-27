"""管理运维 CLI:部署前的初始化与使用期的运维操作。

与 server.py 共用 core/deps 的装配、同一份 .env 配置和同一个数据库。
超时清算在纠正决定时自动进行;expire-corrections 提供手动入口。

用法:
    uv run homeshield-cli init-db
    uv run homeshield-cli add-family --name 我的家庭
    uv run homeshield-cli add-member --family-id 1 --name 妈妈 --trusted
    uv run homeshield-cli set-trust --member-id 2 --trusted 1
    uv run uvicorn homeshield.server:app --reload
"""
import argparse
import secrets

from homeshield.core.config import Settings
from homeshield.core.deps import build_deps
from homeshield.core.feedback import CorrectionService
from homeshield.core.logsetup import setup_logging


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser("homeshield") # 创建主解析器
    sub = parser.add_subparsers(dest="cmd", required=True) # 让程序支持子命令，就像 git add、git commit 那样

    sub.add_parser("init-db", help="建库建表,幂等")
    f = sub.add_parser("add-family", help="创建家庭")
    f.add_argument("--name", required=True)         # homeshield add-family --name "张三家"

    m = sub.add_parser("add-member", help="添加成员位")
    m.add_argument("--family-id", type=int, required=True)
    m.add_argument("--name", required=True)
    m.add_argument("--trusted", action="store_true", help="纠正信任位:纠正即时生效 + 可管理成员")
    m.add_argument("--openid", default=None, help="微信 openid,绑定后微信消息归属此群成员")
    m.add_argument("--demo-user", action="store_true", help="创建仅供演示的合成身份与个人入口,不会推送微信")

    t = sub.add_parser("set-trust", help="翻转成员的纠正信任位") # 设置成员的纠正信任权限，系统把一条消息判成诈骗，家人认为判错了，提交“纠正”。普通成员提交后，先等信任成员确认。信任成员提交后，纠正立即生效；也可以确认其他人的纠正、管理成员。
    t.add_argument("--member-id", type=int, required=True)
    t.add_argument("--trusted", type=int, choices=[0, 1], required=True) # 1是给这个成员开通信任权限

    l = sub.add_parser("link", help="打印成员的网页入口(链接即凭证)")
    l.add_argument("--member-id", type=int, required=True)
    l.add_argument("--base-url", default="http://localhost:8000", help="服务对外可达地址")

    d = sub.add_parser("disband", help="运维解散群并保留所有历史记录")
    d.add_argument("--family-id", type=int, required=True)

    sub.add_parser("expire-corrections", help="手动清算超时 pending")

    args = parser.parse_args()
    deps = build_deps(Settings.load())

    if args.cmd == "init-db":
        print("db ready:", deps.settings.db_path)
    elif args.cmd == "add-family":
        print("family_id =", deps.repos.family.create(args.name))
    elif args.cmd == "add-member":
        if args.openid and args.demo_user:
            raise SystemExit("--openid 与 --demo-user 不能同时使用")
        openid = args.openid
        if args.demo_user:
            openid = f"demo:{secrets.token_urlsafe(10)}"
        print(
            "member_id =",
            deps.groups.add_member(args.family_id, args.name, args.trusted, openid),
        )
    elif args.cmd == "set-trust":
        deps.groups.set_trust_by_operator(args.member_id, bool(args.trusted))
        m = deps.repos.member.get(args.member_id)
        print(f"member_id={m.id} {m.name} trusted={m.trusted}")
    elif args.cmd == "link":
        member = deps.repos.member.get(args.member_id)
        if member is None:
            raise SystemExit("member not found")
        if member.user_id is None:
            raise SystemExit("member slot is not bound; no personal link")
        # 链接即凭证:身份级入口由 User.entry_url 统一拼接。
        user = deps.repos.users.get(member.user_id)
        print(user.entry_url(args.base_url)) # 返回一个网页URL，是一个控制台网址
    elif args.cmd == "expire-corrections":
        print("expired", CorrectionService(deps.repos).expire_pending()) # 凡是普通成员提交、还没被信任成员确认、且已经放了超过 7 天的纠正，一律自动变为 rejected
    elif args.cmd == "disband":
        deps.groups.disband_by_operator(args.family_id)
        print(f"family_id={args.family_id} disbanded")


if __name__ == "__main__":
    main()

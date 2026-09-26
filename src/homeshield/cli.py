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

    t = sub.add_parser("set-trust", help="翻转成员的纠正信任位")
    t.add_argument("--member-id", type=int, required=True)
    t.add_argument("--trusted", type=int, choices=[0, 1], required=True)

    l = sub.add_parser("link", help="打印成员的网页入口(链接即凭证)")
    l.add_argument("--member-id", type=int, required=True)
    l.add_argument("--base-url", default="http://localhost:8000", help="服务对外可达地址")

    sub.add_parser("expire-corrections", help="手动清算超时 pending")

    args = parser.parse_args()
    deps = build_deps(Settings.load())

    if args.cmd == "init-db":
        print("db ready:", deps.settings.db_path)
    elif args.cmd == "add-family":
        print("family_id =", deps.repos.family.create(args.name))
    elif args.cmd == "add-member":
        # openid 唯一,重复绑定抛 IntegrityError
        print(
            "member_id =",
            deps.repos.member.add(args.family_id, args.name, args.trusted, args.openid),
        )
    elif args.cmd == "set-trust":
        deps.repos.member.set_trust(args.member_id, bool(args.trusted))
        m = deps.repos.member.get(args.member_id)
        print(f"member_id={m.id} {m.name} trusted={m.trusted}")
    elif args.cmd == "link":
        member = deps.repos.member.get(args.member_id)
        if member is None:
            raise SystemExit("member not found")
        # 链接即凭证:拼接逻辑唯一收敛在 Member.entry_url(全员控制台)
        print(member.entry_url(args.base_url))
    elif args.cmd == "expire-corrections":
        print("expired", CorrectionService(deps.repos).expire_pending()) # 凡是普通成员提交、还没被信任成员确认、且已经放了超过 7 天的纠正，一律自动变为 rejected


if __name__ == "__main__":
    main()

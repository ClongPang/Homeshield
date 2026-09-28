"""Operations CLI. Relation management and invitations belong to the personal console."""
import argparse
import json

from homeshield.core.config import Settings
from homeshield.core.deps import build_deps, make_pipeline
from homeshield.core.logsetup import setup_logging
from homeshield.core.models import ContentType, Message
from homeshield.core.pipeline import PipelineConfig


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser("homeshield")
    sub = parser.add_subparsers(dest="cmd", required=True)  # 允许使用 cmd 子命令行

    sub.add_parser("init-db", help="create the current schema")

    link = sub.add_parser("link", help="print a user's personal console link")
    link.add_argument("--user-id", type=int, required=True)
    link.add_argument("--base-url", default="http://localhost:8000")

    incident = sub.add_parser("incident", help="show a user's incident history")
    incident.add_argument("--user-id", type=int, required=True)

    preview = sub.add_parser("supply-preview", help="read-only preview of query context")
    preview.add_argument("--query-id", type=int, required=True)

    wlink = sub.add_parser("wecom-link", help="map a user to their WeCom corp userid (alert delivery)")
    wlink.add_argument("--user-id", type=int, required=True)
    wlink.add_argument("--corp-userid", required=True)

    args = parser.parse_args()
    deps = build_deps(Settings.load())

    if args.cmd == "init-db":
        print("db ready:", deps.settings.db_path)
    elif args.cmd == "link":
        user = deps.repos.users.get(args.user_id)
        if user is None: raise SystemExit("user not found")
        print(user.entry_url(args.base_url))
    elif args.cmd == "wecom-link":
        if deps.repos.users.get(args.user_id) is None: raise SystemExit("user not found")
        deps.repos.wecom_member.link(args.user_id, args.corp_userid)
        print("linked", args.user_id, "->", args.corp_userid)
    elif args.cmd == "incident":
        print(json.dumps(deps.repos.incident.list_for_user(args.user_id), ensure_ascii=False, indent=2))
    elif args.cmd == "supply-preview":
        row = deps.repos.query.get(args.query_id)
        if row is None: raise SystemExit("query not found")
        if row["kind"] != "query": raise SystemExit("ack has no supply")
        message = Message(user_id=row["user_id"], relation_ids=[], content_type=ContentType(row["content_type"]), content=row["content"])
        text = (row["transcript"] or "") if row["content_type"] == "image" else row["content"]
        pipeline = make_pipeline(deps, PipelineConfig(supply_features=True))
        items = pipeline._supply(message, args.query_id, text)
        from homeshield.core.features import extract_rule_features, get_rule_risk_floor
        from homeshield.core.pipeline import Extraction, _to_conversation
        conversation = _to_conversation(text, row["content_type"])
        current = extract_rule_features(text)
        synthesis, floor = pipeline._cross_message_features(items, Extraction([], [], get_rule_risk_floor(current), conversation, current))
        print(json.dumps({"prior": [{"query_id": i.query_id, "source": i.source, "matched_values": i.matched_values} for i in items],
                          "synthetic": [s.model_dump() for s in synthesis], "cross_floor": floor.value}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

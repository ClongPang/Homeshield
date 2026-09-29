"""Operations CLI. Relation management and invitations belong to the personal console."""
import argparse
import asyncio
import json

from homeshield.core.config import Settings
from homeshield.core.deps import build_deps, initialize_deps, make_pipeline
from homeshield.core.logsetup import setup_logging
from homeshield.core.models import ContentType, Message
from homeshield.core.pipeline import PipelineConfig


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser("homeshield")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="create the current schema")
    link = sub.add_parser("link", help="print a user's personal console link")
    link.add_argument("--user-id", type=int, required=True)
    link.add_argument("--base-url", default="http://localhost:8000")
    incident = sub.add_parser("incident", help="show a user's incident history")
    incident.add_argument("--user-id", type=int, required=True)
    preview = sub.add_parser("supply-preview", help="read-only preview of query context")
    preview.add_argument("--query-id", type=int, required=True)
    asyncio.run(_run(parser.parse_args()))


async def _run(args) -> None:
    deps = build_deps(Settings.load())
    try:
        await initialize_deps(deps)
        if args.cmd == "init-db":
            print("db ready")
        elif args.cmd == "link":
            user = await deps.repos.users.get(args.user_id)
            if user is None: raise SystemExit("user not found")
            print(user.entry_url(args.base_url))
        elif args.cmd == "incident":
            rows = await deps.repos.incident.list_for_user(args.user_id)
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        elif args.cmd == "supply-preview":
            row = await deps.repos.query.get(args.query_id)
            if row is None: raise SystemExit("query not found")
            if row["kind"] != "query": raise SystemExit("ack has no supply")
            message = Message(user_id=row["user_id"], relation_ids=[], content_type=ContentType(row["content_type"]), content=row["content"])
            text = (row["transcript"] or "") if row["content_type"] == "image" else row["content"]
            pipeline = make_pipeline(deps, PipelineConfig(supply_features=True))
            items = await pipeline._supply(message, args.query_id, text)
            from homeshield.core.features import extract_rule_features, get_rule_risk_floor
            from homeshield.core.pipeline import Extraction, _to_conversation
            conversation = _to_conversation(text, row["content_type"])
            current = extract_rule_features(text)
            synthesis, floor = pipeline._cross_message_features(items, Extraction([], [], get_rule_risk_floor(current), conversation, current))
            print(json.dumps({"prior": [{"query_id": i.query_id, "source": i.source, "matched_values": i.matched_values} for i in items],
                              "synthetic": [s.model_dump() for s in synthesis], "cross_floor": floor.value}, ensure_ascii=False, indent=2))
    finally:
        await deps.pool.close()


if __name__ == "__main__":
    main()

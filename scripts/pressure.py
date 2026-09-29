#!/usr/bin/env python3
"""Repeatable client-side API and pooled-DB pressure checks for a test account."""
import argparse
import asyncio
import json
import math
import os
import platform
import time
import uuid

import httpx

from homeshield.core.config import Settings
from homeshield.core.deps import build_deps, initialize_deps


def p99(values: list[float]) -> float:
    if not values:
        return 0.0
    return sorted(values)[max(0, math.ceil(0.99 * len(values)) - 1)]


def summarize(values: list[float], statuses: list[int]) -> dict:
    ordered = sorted(values)
    return {
        "count": len(values),
        "p50_ms": round(ordered[max(0, math.ceil(0.5 * len(ordered)) - 1)] * 1000, 3) if ordered else 0,
        "p99_ms": round(p99(values) * 1000, 3),
        "5xx": sum(status >= 500 for status in statuses),
        "status_counts": {str(code): statuses.count(code) for code in sorted(set(statuses))},
    }


async def run(args) -> dict:
    settings = Settings.load()
    if settings.mode != "mock":
        raise SystemExit("压测要求服务与脚本环境均使用 MODE=mock")
    base_url = args.base_url.rstrip("/")
    timeout = httpx.Timeout(args.timeout)
    deps = build_deps(settings)
    await initialize_deps(deps)
    db_read: list[float] = []
    db_write: list[float] = []
    try:
        user = await deps.repos.users.get_by_token(args.token)
        if user is None:
            raise SystemExit("token 未命中 DATABASE_URL 对应的数据库用户")
        async with deps.pool.connection() as conn:
            pg_version = (await (await conn.execute("SELECT version() AS version")).fetchone())["version"]
        db_cursor_ids = [f"pressure-bench-{uuid.uuid4().hex}" for _ in range(args.db_operations)]
        for index, cursor_id in enumerate(db_cursor_ids):
            start = time.perf_counter()
            await deps.repos.users.get_by_token(args.token)
            db_read.append(time.perf_counter() - start)
            start = time.perf_counter()
            await deps.repos.kf_cursor.set(cursor_id, f"cursor-{index}")
            db_write.append(time.perf_counter() - start)
        async with deps.pool.connection() as conn:
            await conn.execute("DELETE FROM kf_cursor WHERE kfid = ANY(%s)", (db_cursor_ids,))

        async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as client:
            preflight = await client.get("/api/relations", params={"token": args.token})
            if preflight.status_code != 200:
                raise SystemExit(f"API token 检查失败: HTTP {preflight.status_code}")
            queries = (await client.get("/api/my-queries", params={"token": args.token})).json().get("queries", [])
            alerts = (await client.get("/api/alerts", params={"token": args.token})).json().get("alerts", [])
            query_id = args.verdict_id or (int(queries[0]["verdict_id"]) if queries else 0)
            alert_id = args.alert_id or (int(alerts[0]["alert_id"]) if alerts else 0)
            reads = {
                "relations": lambda: client.get("/api/relations", params={"token": args.token}),
                "my_queries": lambda: client.get("/api/my-queries", params={"token": args.token}),
                "my_query_detail": lambda: client.get(f"/api/my-queries/{query_id}", params={"token": args.token}),
                "alerts": lambda: client.get("/api/alerts", params={"token": args.token}),
                "alert_detail": lambda: client.get(f"/api/alerts/{alert_id}", params={"token": args.token}),
                "corrections": lambda: client.get("/api/corrections", params={"token": args.token}),
            }

            async def timed_read(name: str) -> tuple[str, float, int]:
                start = time.perf_counter()
                try:
                    response = await reads[name]()
                    return name, time.perf_counter() - start, response.status_code
                except Exception:
                    return name, time.perf_counter() - start, 599

            read_semaphore = asyncio.Semaphore(args.concurrency)

            async def limited_read(index: int) -> tuple[str, float, int]:
                async with read_semaphore:
                    return await timed_read(names[index % len(names)])

            names = list(reads)
            wave = await asyncio.gather(*(
                limited_read(index) for index in range(args.read_requests)
            ))
            read_times = [item[1] for item in wave]
            read_statuses = [item[2] for item in wave]
            per_endpoint = {
                name: summarize([t for n, t, _ in wave if n == name], [s for n, _, s in wave if n == name])
                for name in names
            }

            slow_base_url = (args.slow_base_url or base_url).rstrip("/")
            async with httpx.AsyncClient(base_url=slow_base_url, timeout=timeout) as slow_client:
                slow_tasks = [asyncio.create_task(slow_client.post(
                    "/api/query",
                    json={"token": args.token, "content": f"压力慢判定 {uuid.uuid4().hex}",
                          "msg_id": f"pressure-slow-{uuid.uuid4().hex}"},
                )) for _ in range(args.slow_query_concurrency)]
                await asyncio.sleep(args.slow_query_observe_seconds)
                during_slow = await asyncio.gather(*(
                    limited_read(index) for index in range(args.slow_read_requests)
                ))
                slow_responses = await asyncio.gather(*slow_tasks, return_exceptions=True)

            mixed_times: list[float] = []
            mixed_statuses: list[int] = []
            semaphore = asyncio.Semaphore(args.concurrency)

            async def mixed_request(index: int) -> None:
                async with semaphore:
                    if (index % 100) < int(args.write_ratio * 100):
                        start = time.perf_counter()
                        try:
                            response = await client.post(
                                "/api/query",
                                json={"token": args.token, "content": f"压力混合读写 {uuid.uuid4().hex}",
                                      "msg_id": f"pressure-mixed-{uuid.uuid4().hex}"},
                            )
                            status = response.status_code
                        except Exception:
                            status = 599
                        mixed_times.append(time.perf_counter() - start)
                        mixed_statuses.append(status)
                    else:
                        name = names[index % len(names)]
                        _, elapsed, status = await timed_read(name)
                        mixed_times.append(elapsed)
                        mixed_statuses.append(status)

            mixed_tasks = []
            deadline = time.perf_counter() + args.duration
            interval = 1 / args.qps
            tick = time.perf_counter()
            index = 0
            while tick < deadline:
                mixed_tasks.append(asyncio.create_task(mixed_request(index)))
                index += 1
                tick += interval
                await asyncio.sleep(max(0, tick - time.perf_counter()))
            await asyncio.gather(*mixed_tasks)

        report = {
            "machine": {
                "platform": platform.platform(), "processor": platform.processor(),
                "cpu_count": os.cpu_count(), "memory_bytes": _memory_bytes(),
                "postgres_version": pg_version,
            },
            "configuration": {
                "base_url": base_url, "slow_base_url": slow_base_url,
                "concurrency": args.concurrency, "qps": args.qps,
                "duration_seconds": args.duration,
                "expected_mock_judge_delay_seconds": args.mock_judge_delay_seconds,
                "slow_query_concurrency": args.slow_query_concurrency,
            },
            "database": {
                "get_by_token": summarize(db_read, [200] * len(db_read)),
                "kf_cursor_insert": summarize(db_write, [200] * len(db_write)),
            },
            "concurrent_reads": summarize(read_times, read_statuses),
            "read_endpoints": per_endpoint,
            "reads_during_slow_judgements": summarize(
                [item[1] for item in during_slow], [item[2] for item in during_slow]
            ),
            "slow_query_statuses": [
                response.status_code if isinstance(response, httpx.Response) else 599
                for response in slow_responses
            ],
            "mixed_traffic": summarize(mixed_times, mixed_statuses),
        }
        report["gates"] = {
            "db_get_p99_under_5ms": report["database"]["get_by_token"]["p99_ms"] < 5,
            "db_single_row_insert_p99_under_5ms": report["database"]["kf_cursor_insert"]["p99_ms"] < 5,
            "50_concurrent_reads_under_500ms_and_no_5xx": (
                report["concurrent_reads"]["p99_ms"] < 500 and report["concurrent_reads"]["5xx"] == 0
            ),
            "reads_during_slow_judgements_under_500ms_and_no_5xx": (
                report["reads_during_slow_judgements"]["p99_ms"] < 500
                and report["reads_during_slow_judgements"]["5xx"] == 0
                and all(status == 200 for status in report["slow_query_statuses"])
            ),
            "mixed_traffic_no_5xx_and_p99_under_1s": (
                report["mixed_traffic"]["5xx"] == 0 and report["mixed_traffic"]["p99_ms"] < 1000
            ),
        }
        report["passed"] = all(report["gates"].values())
        return report
    finally:
        await deps.pool.close()


def _memory_bytes() -> int | None:
    if hasattr(os, "sysconf"):
        try:
            return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
        except (ValueError, OSError):
            return None
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="正常 mock 模式 Homeshield 地址")
    parser.add_argument("--slow-base-url", help="MOCK_JUDGE_DELAY_SECONDS=1 的独立 worker 地址")
    parser.add_argument("--token", required=True, help="专用压测用户 token")
    parser.add_argument("--verdict-id", type=int, default=0)
    parser.add_argument("--alert-id", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--read-requests", type=int, default=1000)
    parser.add_argument("--slow-read-requests", type=int, default=50)
    parser.add_argument("--db-operations", type=int, default=1000)
    parser.add_argument("--qps", type=float, default=20)
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--write-ratio", type=float, default=0.2)
    parser.add_argument("--slow-query-concurrency", type=int, default=10)
    parser.add_argument("--slow-query-observe-seconds", type=float, default=0.2)
    parser.add_argument("--mock-judge-delay-seconds", type=float, default=1)
    parser.add_argument("--timeout", type=float, default=20)
    args = parser.parse_args()
    if (args.qps <= 0 or args.concurrency <= 0 or args.duration <= 0 or args.read_requests <= 0
            or args.slow_read_requests <= 0
            or args.db_operations <= 0 or args.slow_query_concurrency <= 0
            or args.mock_judge_delay_seconds < 0 or not 0 <= args.write_ratio <= 1):
        parser.error("QPS、并发数、时长需为正数，write-ratio 必须在 [0,1]")
    report = asyncio.run(run(args))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Run an isolated two-Uvicorn/two-worker query, SSE, and idempotency smoke test."""
import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from homeshield.core.db import init_schema, make_pool
from homeshield.core.relations import RelationService
from homeshield.core.repo import make_repos


def _port_available(port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


def _validate_database_url(database_url: str) -> None:
    database_name = urlsplit(database_url).path.rsplit("/", 1)[-1].lower()
    if "acceptance" not in database_name:
        raise SystemExit("拒绝运行：数据库名必须包含 acceptance，使用隔离验收库")


def _server_env(database_url: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": database_url,
        "MODE": "mock",
        "MOCK_JUDGE_DELAY_SECONDS": "0",
        "DB_PATH": ":memory:",
        "PUBLIC_BASE_URL": "",
        "WECOM_CORPID": "",
        "WECOM_AGENT_ID": "",
        "WECOM_APP_SECRET": "",
        "WECOM_KF_SECRET": "",
        "WECOM_TOKEN": "",
        "WECOM_AES_KEY": "",
    })
    return env


async def _seed(pool, run_id: str) -> tuple[str, str]:
    repos = make_repos(pool)
    relations = RelationService(repos)
    queryer = await repos.users.get_or_create(f"acceptance:multiworker:{run_id}:queryer")
    protector = await repos.users.get_or_create(f"acceptance:multiworker:{run_id}:protector")
    invite = await relations.issue_invite(protector.id, "验收家人")
    await relations.join(queryer.openid, invite["code"])
    return queryer.token, protector.token


async def _wait_ready(process: subprocess.Popen, port: int, client: httpx.AsyncClient) -> None:
    deadline = time.monotonic() + 30
    url = f"http://127.0.0.1:{port}/"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Uvicorn on port {port} exited during startup")
        try:
            response = await client.get(url, timeout=1)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.2)
    raise TimeoutError(f"Uvicorn on port {port} did not become ready")


async def _read_sse_event(lines, timeout: float) -> list[str]:
    deadline = time.monotonic() + timeout
    event: list[str] = []
    while time.monotonic() < deadline:
        line = await asyncio.wait_for(anext(lines), max(0.1, deadline - time.monotonic()))
        if line:
            event.append(line)
        elif event:
            return event
    raise TimeoutError("SSE alert did not arrive")


async def _assert_smoke(
    pool, query_token: str, protector_token: str, query_port: int, sse_port: int, run_id: str,
) -> dict:
    timeout = httpx.Timeout(connect=5, read=None, write=10, pool=5)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "GET", f"http://127.0.0.1:{sse_port}/api/stream", params={"token": protector_token}
        ) as response:
            if response.status_code != 200:
                raise RuntimeError(f"SSE endpoint returned HTTP {response.status_code}")
            lines = response.aiter_lines()
            if await asyncio.wait_for(anext(lines), 5) != "event: ready":
                raise RuntimeError("SSE endpoint did not send its ready event")
            await anext(lines)
            await anext(lines)

            msg_id = f"multiworker-smoke-{run_id}"
            query_response = await client.post(
                f"http://127.0.0.1:{query_port}/api/query",
                json={
                    "token": query_token,
                    "content": "别告诉家人，马上转账5万元到安全账户",
                    "msg_id": msg_id,
                },
            )
            if query_response.status_code != 200:
                raise RuntimeError(f"query endpoint returned HTTP {query_response.status_code}")
            verdict_id = query_response.json().get("verdict_id")
            if not verdict_id:
                raise RuntimeError("query did not persist a verdict")

            event = "\n".join(await _read_sse_event(lines, 10))
            if "event: alert" not in event or f'"verdict_id": {verdict_id}' not in event:
                raise RuntimeError("the other Uvicorn instance did not deliver the SSE alert")

            duplicate = await client.post(
                f"http://127.0.0.1:{query_port}/api/query",
                json={"token": query_token, "content": "重放", "msg_id": msg_id},
            )
            if duplicate.status_code != 409:
                raise RuntimeError(f"duplicate MsgId returned HTTP {duplicate.status_code}, expected 409")

            async with pool.connection() as conn:
                query_count = await (await conn.execute(
                    "SELECT COUNT(*) AS n FROM query WHERE msg_id=%s", (msg_id,)
                )).fetchone()
                alert_count = await (await conn.execute(
                    "SELECT COUNT(*) AS n FROM alert WHERE verdict_id=%s", (verdict_id,)
                )).fetchone()
            if query_count["n"] != 1 or alert_count["n"] != 1:
                raise RuntimeError("query/alert idempotency count mismatch")
            return {
                "query_http": query_response.status_code,
                "sse_received": True,
                "duplicate_msg_id_http": duplicate.status_code,
                "query_rows": query_count["n"],
                "alert_rows": alert_count["n"],
            }


async def _cleanup(pool, run_id: str) -> None:
    queryer_openid = f"acceptance:multiworker:{run_id}:queryer"
    protector_openid = f"acceptance:multiworker:{run_id}:protector"
    async with pool.connection() as conn, conn.transaction():
        users = await (await conn.execute(
            'SELECT id FROM "user" WHERE openid = ANY(%s)',
            ([queryer_openid, protector_openid],),
        )).fetchall()
        user_ids = [row["id"] for row in users]
        if not user_ids:
            return
        queries = await (await conn.execute(
            "SELECT id, incident_id FROM query WHERE user_id = ANY(%s)", (user_ids,)
        )).fetchall()
        query_ids = [row["id"] for row in queries]
        verdicts = await (await conn.execute(
            "SELECT id FROM verdict WHERE query_id = ANY(%s)", (query_ids or [0],)
        )).fetchall()
        verdict_ids = [row["id"] for row in verdicts]
        cases = await (await conn.execute(
            "SELECT id FROM correction_case WHERE verdict_id = ANY(%s)", (verdict_ids or [0],)
        )).fetchall()
        case_ids = [row["id"] for row in cases]
        await conn.execute("DELETE FROM correction_vote WHERE case_id = ANY(%s)", (case_ids or [0],))
        await conn.execute("DELETE FROM correction_case WHERE id = ANY(%s)", (case_ids or [0],))
        await conn.execute("DELETE FROM alert WHERE verdict_id = ANY(%s)", (verdict_ids or [0],))
        await conn.execute("DELETE FROM query_relation WHERE query_id = ANY(%s)", (query_ids or [0],))
        await conn.execute("DELETE FROM verdict WHERE id = ANY(%s)", (verdict_ids or [0],))
        await conn.execute("DELETE FROM query WHERE id = ANY(%s)", (query_ids or [0],))
        incident_ids = [row["incident_id"] for row in queries if row["incident_id"] is not None]
        await conn.execute("DELETE FROM incident WHERE id = ANY(%s)", (incident_ids or [0],))
        await conn.execute("DELETE FROM invite_code WHERE creator_user_id = ANY(%s) OR used_by_user_id = ANY(%s)",
                           (user_ids, user_ids))
        await conn.execute("DELETE FROM guard_relation WHERE protector_user_id = ANY(%s) OR protected_user_id = ANY(%s)",
                           (user_ids, user_ids))
        await conn.execute("DELETE FROM session_reset_msg WHERE user_id = ANY(%s)", (user_ids,))
        await conn.execute("DELETE FROM wecom_member WHERE user_id = ANY(%s)", (user_ids,))
        await conn.execute('DELETE FROM "user" WHERE id = ANY(%s)', (user_ids,))


def _tail(path: Path, database_url: str) -> str:
    value = "\n".join(path.read_text(errors="replace").splitlines()[-30:])
    return value.replace(database_url, "<DATABASE_URL>")


async def run(args) -> dict:
    _validate_database_url(args.database_url)
    if args.query_port == args.sse_port:
        raise SystemExit("query-port 与 sse-port 必须不同")
    for port in (args.query_port, args.sse_port):
        if not 1 <= port <= 65535 or not _port_available(port):
            raise SystemExit(f"本机端口不可用：{port}")

    run_id = uuid.uuid4().hex
    pool = make_pool(args.database_url)
    pool_open = False
    processes: list[subprocess.Popen] = []
    logs: list[tuple[object, Path]] = []
    try:
        await pool.open(wait=True)
        pool_open = True
        await init_schema(pool)
        query_token, protector_token = await _seed(pool, run_id)
        with tempfile.TemporaryDirectory(prefix="homeshield-multiworker-") as tmp:
            temp_path = Path(tmp)
            child_env = _server_env(args.database_url)
            for port in (args.query_port, args.sse_port):
                log_path = temp_path / f"uvicorn-{port}.log"
                log_file = log_path.open("w")
                logs.append((log_file, log_path))
                processes.append(subprocess.Popen(
                    [sys.executable, "-m", "uvicorn", "homeshield.server:app",
                     "--host", "127.0.0.1", "--port", str(port), "--workers", str(args.workers)],
                    cwd=Path(__file__).resolve().parents[1],
                    env=child_env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                ))
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    for process, port in zip(processes, (args.query_port, args.sse_port), strict=True):
                        await _wait_ready(process, port, client)
                smoke = await _assert_smoke(
                    pool, query_token, protector_token, args.query_port, args.sse_port, run_id
                )
                return {
                    "workers_per_instance": args.workers,
                    "query_port": args.query_port,
                    "sse_port": args.sse_port,
                    "wecom_disabled": True,
                    **smoke,
                }
            except Exception as exc:
                tails = {str(path.name): _tail(path, args.database_url) for _, path in logs if path.exists()}
                raise RuntimeError(f"multi-worker smoke failed: {exc}; logs={json.dumps(tails)}") from exc
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for log_file, _ in logs:
            log_file.close()
        if pool_open:
            try:
                await _cleanup(pool, run_id)
            finally:
                await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True, help="isolated acceptance DB; name must include 'acceptance'")
    parser.add_argument("--query-port", type=int, default=18080)
    parser.add_argument("--sse-port", type=int, default=18081)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    if args.workers < 2:
        parser.error("--workers must be at least 2")
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

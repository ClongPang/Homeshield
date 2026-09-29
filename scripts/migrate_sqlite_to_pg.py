"""One-time, transactional SQLite to Postgres data migration."""
import argparse
import asyncio
import json
import sqlite3
from pathlib import Path

from psycopg.types.json import Jsonb

from homeshield.core.config import Settings
from homeshield.core.db import init_schema, make_pool

TABLES = (
    '"user"', "session_reset_msg", "incident", "query", "guard_relation", "invite_code",
    "verdict", "query_relation", "alert", "correction_case", "correction_vote", "wecom_member",
)
TARGET_ONLY_TABLES = ("kf_cursor",)
TIMESTAMP_COLUMNS = {
    "created_at", "opened_at", "last_query_at", "closed_at", "ended_at", "expires_at",
    "used_at", "revoked_at", "delivered_at", "read_at", "queryer_feedback_at", "closes_at",
    "resolved_at", "voted_at",
}
JSON_COLUMNS = {"cited_ids", "features", "context_snapshot"}
IDENTITY_TABLES = {
    '"user"', "incident", "query", "guard_relation", "invite_code", "verdict", "alert", "correction_case",
}


def _columns(source: sqlite3.Connection, table: str) -> list[str]:
    return [row["name"] for row in source.execute(f"PRAGMA table_info({table})")]


def _convert(row: sqlite3.Row, columns: list[str]):
    values = []
    for column in columns:
        value = row[column]
        if column in JSON_COLUMNS and value is not None:
            value = Jsonb(json.loads(value))
        elif column == "mute" and value is not None:
            value = bool(value)
        values.append(value)
    return values


async def migrate(sqlite_path: Path, database_url: str) -> dict[str, int]:
    if not sqlite_path.is_file():
        raise FileNotFoundError(sqlite_path)
    source = sqlite3.connect(f"file:{sqlite_path.resolve()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    pool = make_pool(database_url)
    try:
        await init_schema(pool)
        counts = {
            table: source.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in TABLES
        }
        async with pool.connection() as conn, conn.transaction():
            occupied = {}
            for table in (*TABLES, *TARGET_ONLY_TABLES):
                row = await (await conn.execute(f"SELECT COUNT(*) AS n FROM {table}")).fetchone()
                occupied[table] = row["n"]
            nonempty = {table: count for table, count in occupied.items() if count}
            if nonempty:
                raise RuntimeError(f"target tables must be empty before migration: {nonempty}")

            for table in TABLES:
                columns = _columns(source, table)
                quoted_columns = ",".join(f'"{column}"' for column in columns)
                expressions = [
                    "to_timestamp(%s)" if column in TIMESTAMP_COLUMNS else "%s"
                    for column in columns
                ]
                override = " OVERRIDING SYSTEM VALUE" if table in IDENTITY_TABLES else ""
                statement = (
                    f"INSERT INTO {table} ({quoted_columns}){override} "
                    f"VALUES ({','.join(expressions)})"
                )
                rows = source.execute(f"SELECT {quoted_columns} FROM {table}").fetchall()
                if rows:
                    async with conn.cursor() as cursor:
                        await cursor.executemany(statement, [_convert(row, columns) for row in rows])

            for table in IDENTITY_TABLES:
                sequence = await (await conn.execute(
                    "SELECT pg_get_serial_sequence(%s, 'id') AS sequence",
                    (f"public.{table}",),
                )).fetchone()
                await conn.execute(
                    "SELECT setval(%s::regclass, COALESCE((SELECT MAX(id) FROM " + table + "), 0) + 1, false)",
                    (sequence["sequence"],),
                )

            actual = {}
            for table in TABLES:
                row = await (await conn.execute(f"SELECT COUNT(*) AS n FROM {table}")).fetchone()
                actual[table] = row["n"]
            if counts != actual:
                raise RuntimeError(f"row-count mismatch after migration: sqlite={counts}, postgres={actual}")
        return counts
    finally:
        await pool.close()
        source.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sqlite_path", type=Path, help="read-only SQLite source file")
    parser.add_argument("--database-url", default=None, help="Postgres target; defaults to DATABASE_URL")
    args = parser.parse_args()
    database_url = args.database_url or Settings.load().database_url
    counts = asyncio.run(migrate(args.sqlite_path, database_url))
    print(json.dumps(counts, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""One-time migration of an existing WARDOGS SQLite database to PostgreSQL.

The target database is never dropped. Run it against a fresh PostgreSQL
database first, then verify counts and /api/ready before switching the app.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
from pathlib import Path


TABLES = {
    "users": ["steam_id", "persona", "last_seen_utc", "session_version"],
    "bindings": ["user_id", "discord_id", "discord_name", "guild_member", "roles", "updated_utc"],
    "sessions": ["token", "user_id", "created_utc", "expires_utc", "session_version"],
    "oauth_states": ["state", "provider", "user_id", "created_utc", "expires_utc"],
    "idempotency_keys": [
        "actor_steam_id", "idem_key", "fingerprint", "created_utc",
        "expires_utc", "status_code", "response_json",
    ],
    "role_permissions": ["role_ref", "perms", "updated_utc"],
    "population_samples": ["server_id", "bucket_utc", "players", "max_players"],
    "player_activity": [
        "server_id", "steam_id", "name", "kills", "deaths", "cash",
        "ping_ms", "faction", "seen_utc", "first_seen_utc", "last_seen_utc",
    ],
    "player_presence": [
        "server_id", "steam_id", "total_seconds", "session_count",
        "current_session_started_utc", "last_seen_utc", "online",
    ],
    "player_notes": [
        "id", "steam_id", "author_steam_id", "note", "created_utc",
    ],
    "player_tags": [
        "id", "steam_id", "label", "color", "created_by", "created_utc",
    ],
    "clans": [
        "id", "name", "tag", "color", "description", "created_by",
        "created_utc", "updated_utc",
    ],
    "clan_members": [
        "clan_id", "steam_id", "member_role", "joined_utc",
    ],
    "vip_slots": [
        "id", "server_id", "steam_id", "display_name", "expires_utc", "note",
        "created_by", "created_utc", "updated_utc", "synced_utc",
        "sync_state", "sync_error",
    ],
    "tickets": [
        "id", "title", "description", "status", "priority", "player_steam_id",
        "assigned_to", "created_by", "created_utc", "updated_utc", "closed_utc",
    ],
    "ticket_comments": [
        "id", "ticket_id", "author_steam_id", "body", "created_utc",
    ],
    "managed_bans": [
        "id", "server_id", "steam_id", "display_name", "reason", "expires_utc",
        "created_by", "created_utc", "updated_utc", "sync_state", "sync_error",
    ],
    "site_audit": [
        "id", "timestamp_utc", "actor_steam_id", "server_id", "event",
        "detail", "prev_hash", "entry_hash",
    ],
    "server_health": ["server_id", "online", "detail", "checked_utc"],
}

DEFAULTS = {
    "session_version": 0,
    "guild_member": 0,
    "roles": "[]",
    "perms": "[]",
    "updated_utc": 0,
    "detail": "",
    "prev_hash": "",
    "entry_hash": "",
}


def quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def source_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({quote(table)})").fetchall()
    return {str(row[1]) for row in rows}


async def migrate(sqlite_path: Path, database_url: str, dry_run: bool = False) -> None:
    try:
        import asyncpg
    except ImportError as exc:
        raise RuntimeError("Установите asyncpg из requirements.txt") from exc

    if not sqlite_path.is_file():
        raise RuntimeError(f"SQLite база не найдена: {sqlite_path}")

    root = Path(__file__).resolve().parent.parent
    migrations_dir = root / "migrations" / "postgres"
    migration_files = sorted(migrations_dir.glob("[0-9][0-9][0-9]_*.sql"))
    if not migration_files:
        raise RuntimeError(f"Не найдены PostgreSQL-миграции: {migrations_dir}")

    with sqlite3.connect(sqlite_path) as source:
        source.row_factory = sqlite3.Row
        available_tables = {
            row[0]
            for row in source.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        rows_by_table = {}
        for table, columns in TABLES.items():
            if table not in available_tables:
                rows_by_table[table] = []
                continue
            available_columns = source_columns(source, table)
            selected = [column for column in columns if column in available_columns]
            rows_by_table[table] = [
                dict(row)
                for row in source.execute(
                    "SELECT " + ", ".join(quote(column) for column in selected)
                    + f" FROM {quote(table)}"
                )
            ]

    if dry_run:
        for table, rows in rows_by_table.items():
            if rows:
                print(f"{table}: {len(rows)}")
        return

    connection = await asyncpg.connect(
        database_url,
        timeout=10,
        server_settings={"application_name": "wardogs-sqlite-migration"},
    )
    try:
        async with connection.transaction():
            for path in migration_files:
                version = int(path.name.split("_", 1)[0])
                await connection.execute(path.read_text(encoding="utf-8"))
                await connection.execute(
                    "INSERT INTO schema_migrations (version, applied_utc) "
                    "VALUES ($1, EXTRACT(EPOCH FROM NOW())) "
                    "ON CONFLICT (version) DO NOTHING",
                    version,
                )

            for table, columns in TABLES.items():
                rows = rows_by_table[table]
                if not rows:
                    continue
                placeholders = ", ".join(f"${index}" for index in range(1, len(columns) + 1))
                statement = (
                    f"INSERT INTO {quote(table)} "
                    f"({', '.join(quote(column) for column in columns)}) "
                    f"VALUES ({placeholders}) ON CONFLICT DO NOTHING"
                )
                values = [
                    tuple(row.get(column, DEFAULTS.get(column)) for column in columns)
                    for row in rows
                ]
                await connection.executemany(statement, values)

            max_id = await connection.fetchval("SELECT MAX(id) FROM site_audit")
            if max_id is not None:
                await connection.execute(
                    "SELECT setval(pg_get_serial_sequence('site_audit', 'id'), $1, true)",
                    max_id,
                )
    finally:
        await connection.close()

    print("SQLite → PostgreSQL migration completed.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sqlite",
        type=Path,
        default=Path(os.environ.get("WARDOGS_DB_PATH") or "site.db"),
        help="путь к исходной SQLite-базе",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("WARDOGS_DATABASE_URL") or os.environ.get("DATABASE_URL") or "",
        help="PostgreSQL URL; лучше передавать через переменную окружения",
    )
    parser.add_argument("--dry-run", action="store_true", help="только показать количество строк")
    args = parser.parse_args()
    if not args.database_url:
        parser.error("нужен --database-url или WARDOGS_DATABASE_URL")
    asyncio.run(migrate(args.sqlite, args.database_url, args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
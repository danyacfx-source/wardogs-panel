"""Перенос данных из SQLite (site.db) в PostgreSQL (WARDOGS_DATABASE_URL).

Запуск:
    WARDOGS_DATABASE_URL=postgresql://... python tools/migrate_sqlite_to_postgres.py /path/to/site.db

Вставляет все общие таблицы через upsert по первичному ключу.
Таблицы без PK — обычный INSERT (append).
"""

import asyncio
import os
import sqlite3
import sys


async def migrate(dsn, db_path):
    import asyncpg

    src = sqlite3.connect(db_path)
    src.row_factory = sqlite3.Row
    tables = [
        r[0]
        for r in src.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=4, timeout=30)
    total = {}
    try:
        async with pool.acquire() as conn:
            pg_tables = {
                r[0]
                for r in await conn.fetch(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='public'"
                )
            }
        for table in tables:
            if table == "schema_migrations" or table not in pg_tables:
                print(f"skip {table} (нет в PG)")
                continue
            async with pool.acquire() as conn:
                cols = [
                    r[0]
                    for r in await conn.fetch(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name=$1 "
                        "ORDER BY ordinal_position",
                        table,
                    )
                ]
                pk = [
                    r[0]
                    for r in await conn.fetch(
                        "SELECT kcu.column_name FROM information_schema.table_constraints tc "
                        "JOIN information_schema.key_column_usage kcu "
                        "  ON tc.constraint_name = kcu.constraint_name "
                        " AND tc.table_schema = kcu.table_schema "
                        "WHERE tc.table_schema='public' AND tc.table_name=$1 "
                        "  AND tc.constraint_type='PRIMARY KEY' "
                        "ORDER BY kcu.ordinal_position",
                        table,
                    )
                ]
            src_cols = [d[1] for d in src.execute(f'PRAGMA table_info("{table}")')]
            common = [c for c in cols if c in src_cols]
            if not common:
                print(f"skip {table} (нет общих колонок)")
                continue
            rows = src.execute(
                f'SELECT {", ".join(chr(34) + c + chr(34) for c in common)} FROM "{table}"'
            ).fetchall()
            if not rows:
                total[table] = 0
                continue
            col_sql = ", ".join(f'"{c}"' for c in common)
            if pk:
                pk_cols = [c for c in pk if c in common]
                conflict = ", ".join(f'"{c}"' for c in pk_cols)
                updates = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in common if c not in pk_cols)
                if updates:
                    sql = (
                        f'INSERT INTO "{table}" ({col_sql}) VALUES ({", ".join("$" + str(i + 1) for i in range(len(common)))}) '
                        f"ON CONFLICT ({conflict}) DO UPDATE SET {updates}"
                    )
                else:
                    sql = (
                        f'INSERT INTO "{table}" ({col_sql}) VALUES ({", ".join("$" + str(i + 1) for i in range(len(common)))}) '
                        f"ON CONFLICT ({conflict}) DO NOTHING"
                    )
            else:
                sql = (
                    f'INSERT INTO "{table}" ({col_sql}) VALUES '
                    f'({", ".join("$" + str(i + 1) for i in range(len(common)))})'
                )
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.executemany(sql, [tuple(r) for r in rows])
            total[table] = len(rows)
            print(f"{table}: {len(rows)}")
    finally:
        await pool.close()
        src.close()
    print("---")
    for table, count in total.items():
        print(f"{table}: {count}")
    print("total rows:", sum(total.values()))


if __name__ == "__main__":
    dsn = os.environ.get("WARDOGS_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        print("нет WARDOGS_DATABASE_URL")
        sys.exit(1)
    if len(sys.argv) != 2:
        print("usage: migrate_sqlite_to_postgres.py /path/to/site.db")
        sys.exit(1)
    asyncio.run(migrate(dsn, sys.argv[1]))

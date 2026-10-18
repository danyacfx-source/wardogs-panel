"""Перенос данных бота из SQLite (wardogs_v2.db) в PostgreSQL (схема bot).

Использование:
    python tools/migrate_bot_sqlite_to_postgres.py [путь/к/wardogs_v2.db]

DSN берётся из BOT_DATABASE_URL (или WARDOGS_DATABASE_URL/DATABASE_URL).
Схему bot и таблицы создаёт сам (init_db бота). Идемпотентен: повторный
запуск не дублирует строки (ON CONFLICT DO NOTHING); последовательности
после импорта выставляются по MAX(id).
"""

import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOT_DIR = ROOT / "app" / "bot"

SKIP_TABLES = {"bot_lease"}


def main() -> int:
    dsn = (
        os.environ.get("BOT_DATABASE_URL")
        or os.environ.get("WARDOGS_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or ""
    ).strip()
    if not dsn:
        print("нет BOT_DATABASE_URL (или WARDOGS_DATABASE_URL) в окружении")
        return 1
    os.environ["BOT_DATABASE_URL"] = dsn

    src_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "wardogs_v2.db"
    if not src_path.exists():
        print(f"нет файла-источника: {src_path}")
        return 1

    sys.path.insert(0, str(BOT_DIR))
    import database  # noqa: E402

    print("Создаю схему bot и таблицы в PostgreSQL...")
    database.init_db()
    conn = database.get_conn()

    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        tables = [
            r["name"]
            for r in src.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
    except sqlite3.DatabaseError as exc:
        print(f"источник не читается как SQLite: {exc}")
        return 1

    total = 0
    for table in tables:
        if table in SKIP_TABLES:
            print(f"  {table}: пропущена (runtime)")
            continue
        cols = [r["name"] for r in src.execute(f'PRAGMA table_info("{table}")')]
        if not cols:
            continue
        col_sql = ", ".join(f'"{c}"' for c in cols)
        placeholders = ", ".join("?" for _ in cols)
        insert_sql = (
            f'INSERT INTO "{table}" ({col_sql}) VALUES ({placeholders}) '
            "ON CONFLICT DO NOTHING"
        )
        cur = src.execute(f'SELECT * FROM "{table}"')
        n = 0
        while True:
            rows = cur.fetchmany(2000)
            if not rows:
                break
            batch = [tuple(row[c] for c in cols) for row in rows]
            conn.executemany(insert_sql, batch)
            n += len(batch)
        print(f"  {table}: {n}")
        total += n

    # Выставляем sequence автоинкрементов по MAX(id) загруженных данных.
    seq_rows = conn.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'bot' AND column_default LIKE 'nextval%'"
    ).fetchall()
    for r in seq_rows:
        t, col = r["table_name"], r["column_name"]
        conn.execute(
            f"SELECT setval(pg_get_serial_sequence('bot.{t}', '{col}'), "
            f"COALESCE((SELECT MAX({col}) FROM bot.{t}), 0) + 1, false)"
        )

    src.close()
    print(f"Готово. Перенесено строк: {total}. Sequence выставлены: {len(seq_rows)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

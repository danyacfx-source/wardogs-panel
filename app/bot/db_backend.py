"""Dual-mode DB backend для встроенного бота.

sqlite (по умолчанию) — тесты и локальная разработка.
PostgreSQL — продакшен: BOT_DATABASE_URL задан.

Мимикрирует API sqlite3, который используется в database.py и когах:
execute/executemany/executescript/commit/close, rowcount, Row по ключу и
индексу, плейсхолдеры "?", INSERT OR IGNORE / INSERT OR REPLACE,
COLLATE NOCASE, ORDER BY RANDOM(), PRAGMA (no-op), IntegrityError.
"""

import os
import re
import sqlite3
import threading


def bot_dsn() -> str:
    return (os.environ.get("BOT_DATABASE_URL") or "").strip()


def panel_dsn() -> str:
    return (
        os.environ.get("WARDOGS_PANEL_DATABASE_URL")
        or os.environ.get("WARDOGS_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or ""
    ).strip()


def using_pg() -> bool:
    return bool(bot_dsn())


def panel_using_pg() -> bool:
    return bool(panel_dsn())


# PK-колонки для INSERT OR REPLACE (conflict target + SET clause).
_PK_COLUMNS: dict[str, list[str]] = {
    "tickets": ["number"],
    "streamers": ["login"],
    "mirrors": ["id"],
    "log_channels": ["log_type"],
    "log_sent": ["fingerprint"],
    "bot_lease": ["id"],
    "warnings": ["id"],
    "automod_config": ["key"],
    "automod_badwords": ["word"],
    "automod_whitelist": ["target_type", "target_id"],
    "giveaways": ["id"],
    "giveaway_participants": ["giveaway_id", "user_id"],
    "server_members": ["user_id", "guild_id"],
    "server_roles": ["role_id"],
    "server_channels": ["channel_id"],
    "server_events": ["id"],
    "announce_pending": ["user_id"],
    "trap_channels": ["channel_id"],
    "donations_orders": ["id"],
    "donations_carts": ["id"],
    "donations_donors": ["user_id"],
    "donations_payments": ["operation_id"],
    "donations_reviews": ["operation_id"],
    "donations_vip": ["id"],
    "donations_adjustments": ["id"],
    "donations_meta": ["key"],
    "seed_users": ["id"],
    "seed_link_challenges": ["discord_id"],
    "seed_donor_links": ["id"],
    "seed_weekly_stats": ["user_id", "week_key"],
    "seed_active_sessions": ["user_id"],
    "seed_point_ledger": ["id"],
    "seed_audit_log": ["id"],
    "seed_vip_memberships": ["user_id"],
    "seed_vip_orders": ["id"],
    "seed_weekly_reports": ["week_key"],
    "seed_system_state": ["key"],
    "sw_log": ["id"],
}

_RE_OR_IGNORE = re.compile(r"(?i)\bINSERT\s+OR\s+IGNORE\s+INTO\b")
_RE_OR_REPLACE = re.compile(
    r"(?i)\bINSERT\s+OR\s+REPLACE\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)"
)
_RE_COLLATE_NOCASE = re.compile(
    r"(?i)\b([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)\s+COLLATE\s+NOCASE\b"
)
_RE_RANDOM = re.compile(r"(?i)\bRANDOM\s*\(\s*\)")
_RE_DDL_AUTOINC = re.compile(r"(?i)\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b")
_RE_DDL_INTEGER = re.compile(r"(?i)\bINTEGER\b")


def _convert_placeholders(sql: str) -> str:
    """? -> %s, литеральный % -> %%; строковые литералы копируются как есть."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            out.append(ch)
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        out.append("''")
                        i += 2
                        continue
                    out.append("'")
                    i += 1
                    break
                out.append(sql[i])
                i += 1
            continue
        if ch == "?":
            out.append("%s")
        elif ch == "%":
            out.append("%%")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def translate(sql: str) -> str:
    sql = sql.strip()
    if not sql:
        return sql
    if sql.upper().startswith("PRAGMA"):
        return "SELECT 1 WHERE false"
    flags = {"ignore": False, "replace": False}
    if _RE_OR_IGNORE.search(sql):
        flags["ignore"] = True
        sql = _RE_OR_IGNORE.sub("INSERT INTO", sql)
    m = _RE_OR_REPLACE.search(sql)
    if m:
        flags["replace"] = True
        table = m.group(1).lower()
        cols = [c.strip() for c in m.group(2).split(",") if c.strip()]
        sql = _RE_OR_REPLACE.sub(f"INSERT INTO {m.group(1)} ({m.group(2)})", sql, count=1)
        pk = _PK_COLUMNS.get(table)
        if pk:
            target = ", ".join(pk)
            updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols)
            sql = f"{sql} ON CONFLICT ({target}) DO UPDATE SET {updates}"
        else:
            sql = f"{sql} ON CONFLICT DO NOTHING"
    sql = _RE_COLLATE_NOCASE.sub(r"LOWER(\1)", sql)
    sql = _RE_RANDOM.sub("random()", sql)
    sql = _convert_placeholders(sql)
    if flags["ignore"]:
        sql = f"{sql} ON CONFLICT DO NOTHING"
    return sql


def _split_ddl_statements(script: str) -> list[str]:
    stmts: list[str] = []
    buf: list[str] = []
    in_str = False
    i, n = 0, len(script)
    while i < n:
        ch = script[i]
        if in_str:
            buf.append(ch)
            if ch == "'":
                if i + 1 < n and script[i + 1] == "'":
                    buf.append("'")
                    i += 2
                    continue
                in_str = False
            i += 1
            continue
        if ch == "'":
            in_str = True
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            stmts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    stmts.append("".join(buf))
    return [s for s in stmts if s.strip()]


def _translate_ddl(stmt: str) -> str:
    s = stmt.strip()
    if not s or s.upper().startswith("PRAGMA"):
        return ""
    s = _RE_DDL_AUTOINC.sub("BIGSERIAL PRIMARY KEY", s)
    s = _RE_DDL_INTEGER.sub("BIGINT", s)
    return s


class PgConnection:
    """Соединение с PostgreSQL, мимикрирующее sqlite3.Connection (autocommit)."""

    is_pg = True

    def __init__(self, dsn: str, schema: str | None = None):
        self._dsn = dsn
        self._schema = schema
        self._conn = None
        self._connect()

    def _connect(self) -> None:
        import psycopg
        from psycopg.rows import dict_row

        conn = psycopg.connect(self._dsn, autocommit=True)
        conn.row_factory = dict_row
        try:
            if self._schema:
                conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{self._schema}"')
                conn.execute(f'SET search_path TO "{self._schema}", public')
        except Exception:
            conn.close()
            raise
        self._conn = conn

    def _ensure_open(self) -> None:
        # PostgreSQL can close idle connections without restarting the bot.
        if self._conn is None or self._conn.closed:
            self._connect()

    def execute(self, sql: str, params=None):
        from psycopg import errors as pg_errors

        pg_sql = translate(sql)
        # Retry a lost connection only for plain SELECT statements. Writes may
        # already have committed before a network failure: never replay them.
        read_only = pg_sql.lstrip().upper().startswith("SELECT ")
        for attempt in range(2 if read_only else 1):
            try:
                self._ensure_open()
                if params is None:
                    return self._conn.execute(pg_sql)
                return self._conn.execute(pg_sql, tuple(params))
            except pg_errors.UniqueViolation as exc:
                raise sqlite3.IntegrityError(str(exc)) from exc
            except pg_errors.IntegrityError as exc:
                raise sqlite3.IntegrityError(str(exc)) from exc
            except pg_errors.Error as exc:
                if read_only and attempt == 0 and self._conn is not None and self._conn.closed:
                    continue
                raise sqlite3.OperationalError(str(exc)) from exc
        raise RuntimeError("Unreachable PostgreSQL retry state")

    def executemany(self, sql: str, seq_of_params):
        from psycopg import errors as pg_errors

        pg_sql = translate(sql)
        try:
            self._ensure_open()
            with self._conn.cursor() as cur:
                return cur.executemany(pg_sql, [tuple(p) for p in seq_of_params])
        except pg_errors.UniqueViolation as exc:
            raise sqlite3.IntegrityError(str(exc)) from exc
        except pg_errors.IntegrityError as exc:
            raise sqlite3.IntegrityError(str(exc)) from exc
        except pg_errors.Error as exc:
            raise sqlite3.OperationalError(str(exc)) from exc

    def executescript(self, script: str):
        for stmt in _split_ddl_statements(script):
            translated = _translate_ddl(stmt)
            if translated:
                self.execute(translated)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass


_bot_local = threading.local()
_panel_local = threading.local()
_lock = threading.Lock()
_pg_conns: set = set()


def _register(conn) -> None:
    with _lock:
        _pg_conns.add(conn)


def get_bot_conn() -> PgConnection:
    conn = getattr(_bot_local, "conn", None)
    if conn is None:
        conn = PgConnection(bot_dsn(), schema="bot")
        _bot_local.conn = conn
        _register(conn)
    return conn


def open_bot_connection() -> PgConnection:
    conn = PgConnection(bot_dsn(), schema="bot")
    _register(conn)
    return conn


def get_panel_conn() -> PgConnection:
    conn = getattr(_panel_local, "conn", None)
    if conn is None:
        conn = PgConnection(panel_dsn())
        _panel_local.conn = conn
        _register(conn)
    return conn


def bot_ready() -> bool:
    """Есть ли схема бота (bot.bot_lease) в PG."""
    try:
        conn = get_bot_conn()
        row = conn.execute(
            "SELECT to_regclass('bot.bot_lease') AS t"
        ).fetchone()
        return bool(row and row["t"])
    except Exception:
        return False


def close_all() -> None:
    with _lock:
        for conn in list(_pg_conns):
            try:
                conn.close()
            except Exception:
                pass
        _pg_conns.clear()
    _bot_local.conn = None
    _panel_local.conn = None

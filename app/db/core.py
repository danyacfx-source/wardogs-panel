"""Соединения, схема, миграции, аудит и health."""

import hashlib
import json
import os
import re
import time
import asyncio
from pathlib import Path

import aiosqlite

try:
    from .. import config
except ImportError:
    import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    steam_id      TEXT PRIMARY KEY,
    persona       TEXT,
    last_seen_utc REAL
);
CREATE TABLE IF NOT EXISTS bindings (
    user_id       TEXT PRIMARY KEY,
    discord_id    TEXT UNIQUE,
    discord_name  TEXT,
    guild_member  INTEGER DEFAULT 0,
    roles         TEXT DEFAULT '[]',
    updated_utc   REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    TEXT,
    created_utc REAL,
    expires_utc REAL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS oauth_states (
    state       TEXT PRIMARY KEY,
    provider    TEXT NOT NULL,
    user_id     TEXT,
    created_utc REAL NOT NULL,
    expires_utc REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_oauth_states_expiry ON oauth_states(expires_utc);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    actor_steam_id TEXT NOT NULL,
    idem_key       TEXT NOT NULL,
    fingerprint    TEXT NOT NULL,
    created_utc    REAL NOT NULL,
    expires_utc    REAL NOT NULL,
    status_code    INTEGER,
    response_json  TEXT,
    PRIMARY KEY (actor_steam_id, idem_key)
);
CREATE INDEX IF NOT EXISTS idx_idempotency_expiry ON idempotency_keys(expires_utc);
CREATE TABLE IF NOT EXISTS role_permissions (
    role_ref    TEXT PRIMARY KEY,
    perms       TEXT DEFAULT '[]',
    updated_utc REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS population_samples (
    server_id TEXT,
    bucket_utc INTEGER,
    players INTEGER NOT NULL DEFAULT 0,
    max_players INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (server_id, bucket_utc)
);
CREATE INDEX IF NOT EXISTS idx_population_time ON population_samples(bucket_utc);
CREATE TABLE IF NOT EXISTS player_activity (
    server_id TEXT,
    steam_id TEXT,
    name TEXT,
    kills INTEGER NOT NULL DEFAULT 0,
    deaths INTEGER NOT NULL DEFAULT 0,
    cash INTEGER NOT NULL DEFAULT 0,
    ping_ms INTEGER NOT NULL DEFAULT 0,
    faction TEXT,
    seen_utc REAL,
    PRIMARY KEY (server_id, steam_id)
);
CREATE INDEX IF NOT EXISTS idx_player_activity_seen ON player_activity(seen_utc);
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_utc REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS site_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc REAL NOT NULL,
    actor_steam_id TEXT,
    server_id TEXT,
    event TEXT NOT NULL,
    detail TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_site_audit_time ON site_audit(timestamp_utc DESC);
CREATE TABLE IF NOT EXISTS server_health (
    server_id TEXT PRIMARY KEY,
    online INTEGER NOT NULL,
    detail TEXT DEFAULT '',
    checked_utc REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS seed_shop_config (
    sku TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    cost INTEGER NOT NULL,
    duration_seconds INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1
);
"""

_audit_verify_cache = {"checked_at": 0.0, "result": None, "db_path": None}
_pg_pool = None
_pg_pool_lock = asyncio.Lock()
REQUIRED_SCHEMA_VERSION = 19


class _ConfiguredConnection:
    """aiosqlite context with safe concurrency defaults for one-instance SQLite."""

    def __init__(self, path):
        self._connection = aiosqlite.connect(path)

    async def __aenter__(self):
        self._db = await self._connection.__aenter__()
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.execute("PRAGMA trusted_schema=OFF")
        await self._db.execute("PRAGMA journal_mode=WAL")
        return self._db

    async def __aexit__(self, exc_type, exc, tb):
        return await self._connection.__aexit__(exc_type, exc, tb)


class _PGCursor:
    def __init__(self, rows=()):
        self._rows = list(rows)
        self._position = 0

    async def fetchone(self):
        if self._position >= len(self._rows):
            return None
        row = self._rows[self._position]
        self._position += 1
        return row

    async def fetchall(self):
        rows = self._rows[self._position :]
        self._position = len(self._rows)
        return rows


def _postgres_sql(sql):
    """Convert the small SQLite-style placeholder subset used by this module."""
    index = 0

    def replace(_match):
        nonlocal index
        index += 1
        return f"${index}"

    return re.sub(r"\?", replace, sql)


class _PostgresDatabase:
    def __init__(self, connection):
        self.connection = connection
        self.transaction = None

    async def execute(self, sql, params=()):
        statement = str(sql).strip()
        if statement.upper() in {"BEGIN", "BEGIN IMMEDIATE"}:
            if self.transaction is None:
                self.transaction = self.connection.transaction()
                await self.transaction.start()
            return _PGCursor()
        if statement.upper() == "COMMIT":
            await self.commit()
            return _PGCursor()
        if statement.upper() == "ROLLBACK":
            await self.rollback()
            return _PGCursor()
        translated = _postgres_sql(statement)
        keyword = translated.lstrip().split(None, 1)[0].upper() if translated.strip() else ""
        if keyword in {"SELECT", "WITH", "EXPLAIN"}:
            return _PGCursor(await self.connection.fetch(translated, *(params or ())))
        await self.connection.execute(translated, *(params or ()))
        return _PGCursor()

    async def executescript(self, script):
        await self.connection.execute(script)

    async def commit(self):
        if self.transaction is not None:
            await self.transaction.commit()
            self.transaction = None

    async def rollback(self):
        if self.transaction is not None:
            await self.transaction.rollback()
            self.transaction = None


class _PostgresConnection:
    async def __aenter__(self):
        pool = await _get_pg_pool()
        self._pool = pool
        self._connection = await pool.acquire()
        self._database = _PostgresDatabase(self._connection)
        return self._database

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is not None:
            await self._database.rollback()
        elif self._database.transaction is not None:
            await self._database.rollback()
        await self._pool.release(self._connection)


async def _get_pg_pool():
    global _pg_pool
    if not config.DATABASE_URL:
        raise RuntimeError("PostgreSQL URL не настроен")
    if _pg_pool is not None and not _pg_pool._closed:
        return _pg_pool
    async with _pg_pool_lock:
        if _pg_pool is None or _pg_pool._closed:
            try:
                import asyncpg
            except ImportError as exc:
                raise RuntimeError("Установите asyncpg для PostgreSQL") from exc
            _pg_pool = await asyncpg.create_pool(
                dsn=config.DATABASE_URL,
                min_size=config.DB_POOL_MIN,
                max_size=config.DB_POOL_MAX,
                timeout=10,
                command_timeout=config.DB_COMMAND_TIMEOUT,
                server_settings={"application_name": "wardogs-panel"},
            )
    return _pg_pool


async def close():
    """Close the PostgreSQL pool during application shutdown."""
    global _pg_pool
    if _pg_pool is not None and not _pg_pool._closed:
        await _pg_pool.close()
    _pg_pool = None


def _conn():
    if config.DATABASE_URL:
        return _PostgresConnection()
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return _ConfiguredConnection(config.DB_PATH)


def _db_identity():
    return config.DATABASE_URL or str(config.DB_PATH)


async def init():
    async with _conn() as db:
        if config.DATABASE_URL:
            await db.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_utc DOUBLE PRECISION NOT NULL)"
            )
            migrations_dir = Path(config.BASE_DIR) / "migrations" / "postgres"
        else:
            await db.executescript(_SCHEMA)
            migrations_dir = Path(config.BASE_DIR) / "migrations"
        for path in sorted(migrations_dir.glob("[0-9][0-9][0-9]_*.sql")) if migrations_dir.exists() else []:
            version = int(path.name[:3])
            cur = await db.execute("SELECT 1 FROM schema_migrations WHERE version = ?", (version,))
            if await cur.fetchone():
                continue
            await db.executescript(path.read_text(encoding="utf-8"))
            await db.execute("INSERT INTO schema_migrations (version, applied_utc) VALUES (?, ?)", (version, _now()))
        await db.commit()
    if config.DATABASE_URL:
        return
    for path in (config.DB_PATH, Path(f"{config.DB_PATH}-wal"), Path(f"{config.DB_PATH}-shm")):
        if path.exists():
            try:
                os.chmod(path, 0o600)
            except OSError:
                if config.ENVIRONMENT == "production":
                    raise


def _now():
    return time.time()


async def schema_version():
    async with _conn() as db:
        cur = await db.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations")
        row = await cur.fetchone()
    return int(row[0] or 0)


async def log_site_audit(actor_steam_id, server_id, event, detail=""):
    async with _conn() as db:
        await db.execute("BEGIN IMMEDIATE")
        if config.DATABASE_URL:
            await db.execute("SELECT pg_advisory_xact_lock(728391)")
        cur = await db.execute(
            "SELECT entry_hash FROM site_audit WHERE entry_hash IS NOT NULL AND entry_hash <> '' "
            "ORDER BY id DESC LIMIT 1"
        )
        previous = await cur.fetchone()
        previous_hash = previous[0] if previous else ""
        timestamp = _now()
        event_value = str(event)[:80]
        detail_value = str(detail)[:2000]
        canonical = json.dumps(
            {
                "timestamp_utc": timestamp,
                "actor_steam_id": actor_steam_id or None,
                "server_id": server_id or None,
                "event": event_value,
                "detail": detail_value,
                "prev_hash": previous_hash,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        entry_hash = hashlib.sha256(canonical).hexdigest()
        await db.execute(
            "INSERT INTO site_audit "
            "(timestamp_utc, actor_steam_id, server_id, event, detail, prev_hash, entry_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (timestamp, actor_steam_id or None, server_id or None, event_value, detail_value, previous_hash, entry_hash),
        )
        await db.commit()
        _audit_verify_cache["result"] = None


async def get_site_audit(limit=200):
    async with _conn() as db:
        cur = await db.execute(
            "SELECT a.timestamp_utc, a.actor_steam_id, a.server_id, a.event, a.detail, "
            "COALESCE(u.persona, '') "
            "FROM site_audit a "
            "LEFT JOIN users u ON u.steam_id = a.actor_steam_id "
            "ORDER BY a.id DESC LIMIT ?",
            (max(1, min(int(limit), 1000)),),
        )
        rows = await cur.fetchall()
    return [
        {
            "timestamp_utc": r[0],
            "actor_steam_id": r[1],
            "actor_name": r[5] or "",
            "server_id": r[2],
            "event": r[3],
            "detail": r[4],
        }
        for r in rows
    ]


async def verify_site_audit_chain(force=False):
    now = _now()
    if (
        not force
        and _audit_verify_cache["result"] is not None
        and _audit_verify_cache["db_path"] == _db_identity()
        and now - _audit_verify_cache["checked_at"] < 30
    ):
        return dict(_audit_verify_cache["result"])
    async with _conn() as db:
        cur = await db.execute(
            "SELECT timestamp_utc, actor_steam_id, server_id, event, detail, prev_hash, entry_hash "
            "FROM site_audit WHERE entry_hash IS NOT NULL AND entry_hash <> '' ORDER BY id"
        )
        rows = await cur.fetchall()
    expected_previous = None
    checked = 0
    for timestamp, actor, server_id, event, detail, previous, entry_hash in rows:
        if expected_previous is not None and previous != expected_previous:
            result = {"ok": False, "checked": checked, "legacy_unhashed": 0}
            _audit_verify_cache.update(checked_at=now, result=result, db_path=_db_identity())
            return result
        canonical = json.dumps(
            {
                "timestamp_utc": timestamp,
                "actor_steam_id": actor,
                "server_id": server_id,
                "event": event,
                "detail": detail,
                "prev_hash": previous or "",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if hashlib.sha256(canonical).hexdigest() != entry_hash:
            result = {"ok": False, "checked": checked, "legacy_unhashed": 0}
            _audit_verify_cache.update(checked_at=now, result=result, db_path=_db_identity())
            return result
        expected_previous = entry_hash
        checked += 1
    async with _conn() as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM site_audit WHERE entry_hash IS NULL OR entry_hash = ''"
        )
        legacy_unhashed = int((await cur.fetchone())[0] or 0)
    result = {"ok": True, "checked": checked, "legacy_unhashed": legacy_unhashed}
    _audit_verify_cache.update(checked_at=now, result=result, db_path=_db_identity())
    return result


async def get_server_health(server_id):
    async with _conn() as db:
        cur = await db.execute("SELECT online, detail, checked_utc FROM server_health WHERE server_id = ?", (server_id,))
        row = await cur.fetchone()
    return {"online": bool(row[0]), "detail": row[1], "checked_utc": row[2]} if row else None


async def set_server_health(server_id, online, detail=""):
    async with _conn() as db:
        await db.execute(
            "INSERT INTO server_health (server_id, online, detail, checked_utc) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(server_id) DO UPDATE SET online=excluded.online, detail=excluded.detail, checked_utc=excluded.checked_utc",
            (server_id, 1 if online else 0, str(detail)[:500], _now()),
        )
        await db.commit()

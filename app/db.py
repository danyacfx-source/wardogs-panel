import json
import hashlib
import os
import re
import secrets
import time
import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite

try:
    from . import config
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
"""

_audit_verify_cache = {"checked_at": 0.0, "result": None, "db_path": None}
_pg_pool = None
_pg_pool_lock = asyncio.Lock()
REQUIRED_SCHEMA_VERSION = 16


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
    # The database contains sessions, bindings and audit data. Keep the
    # SQLite file and its sidecars private even when the host umask is weak.
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


# ---------- migrations and site audit ----------

async def schema_version():
    async with _conn() as db:
        cur = await db.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations")
        row = await cur.fetchone()
    return int(row[0] or 0)


async def log_site_audit(actor_steam_id, server_id, event, detail=""):
    async with _conn() as db:
        await db.execute("BEGIN IMMEDIATE")
        if config.DATABASE_URL:
            # Serialize the hash-chain tail even when the audit table is empty.
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
            "SELECT timestamp_utc, actor_steam_id, server_id, event, detail FROM site_audit ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 1000)),),
        )
        rows = await cur.fetchall()
    return [{"timestamp_utc": r[0], "actor_steam_id": r[1], "server_id": r[2], "event": r[3], "detail": r[4]} for r in rows]


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


# ---------- users ----------

async def upsert_user(steam_id, persona=None):
    async with _conn() as db:
        await db.execute(
            "INSERT INTO users (steam_id, persona, last_seen_utc) VALUES (?, ?, ?) "
            "ON CONFLICT(steam_id) DO UPDATE SET last_seen_utc = excluded.last_seen_utc",
            (steam_id, persona, _now()),
        )
        if persona:
            await db.execute(
                "UPDATE users SET persona = ? WHERE steam_id = ?", (persona, steam_id)
            )
        await db.commit()


async def touch_user(steam_id):
    async with _conn() as db:
        await db.execute(
            "UPDATE users SET last_seen_utc = ? WHERE steam_id = ?", (_now(), steam_id)
        )
        await db.commit()


async def get_user(steam_id):
    async with _conn() as db:
        cur = await db.execute("SELECT steam_id, persona FROM users WHERE steam_id = ?", (steam_id,))
        row = await cur.fetchone()
    return {"steam_id": row[0], "persona": row[1]} if row else None


async def add_player_note(steam_id, author_steam_id, note):
    """Create a staff note and return its portable opaque identifier."""
    note_id = secrets.token_hex(12)
    created_utc = _now()
    async with _conn() as db:
        await db.execute(
            "INSERT INTO player_notes (id, steam_id, author_steam_id, note, created_utc) "
            "VALUES (?, ?, ?, ?, ?)",
            (note_id, steam_id, author_steam_id, note, created_utc),
        )
        await db.commit()
    return {
        "id": note_id,
        "steam_id": steam_id,
        "author_steam_id": author_steam_id,
        "author_name": "",
        "note": note,
        "created_utc": created_utc,
    }


async def list_player_notes(steam_id, limit=100):
    limit = max(1, min(int(limit or 100), 200))
    async with _conn() as db:
        cur = await db.execute(
            "SELECT n.id, n.steam_id, n.author_steam_id, "
            "COALESCE(u.persona, ''), n.note, n.created_utc "
            "FROM player_notes n "
            "LEFT JOIN users u ON u.steam_id = n.author_steam_id "
            "WHERE n.steam_id = ? "
            "ORDER BY n.created_utc DESC LIMIT ?",
            (steam_id, limit),
        )
        rows = await cur.fetchall()
    return [
        {
            "id": row[0],
            "steam_id": row[1],
            "author_steam_id": row[2],
            "author_name": row[3] or "",
            "note": row[4],
            "created_utc": row[5],
        }
        for row in rows
    ]


async def delete_player_note(note_id, steam_id):
    """Delete one note only when it belongs to the requested player."""
    async with _conn() as db:
        cur = await db.execute(
            "SELECT 1 FROM player_notes WHERE id = ? AND steam_id = ?",
            (note_id, steam_id),
        )
        exists = await cur.fetchone()
        if not exists:
            return False
        await db.execute(
            "DELETE FROM player_notes WHERE id = ? AND steam_id = ?",
            (note_id, steam_id),
        )
        await db.commit()
    return True


async def list_player_punishments(steam_id, limit=100):
    """Return moderation events for one player without exposing unrelated audit rows."""
    limit = max(1, min(int(limit or 100), 200))
    events = ("ban.add", "ban.remove", "rcon.kick", "rcon.kill")
    placeholders = ", ".join("?" for _ in events)
    async with _conn() as db:
        cur = await db.execute(
            "SELECT a.timestamp_utc, a.actor_steam_id, COALESCE(u.persona, ''), "
            "a.server_id, a.event "
            "FROM site_audit a "
            "LEFT JOIN users u ON u.steam_id = a.actor_steam_id "
            f"WHERE a.detail = ? AND a.event IN ({placeholders}) "
            "ORDER BY a.timestamp_utc DESC LIMIT ?",
            (steam_id, *events, limit),
        )
        rows = await cur.fetchall()
    return [
        {
            "timestamp_utc": row[0],
            "actor_steam_id": row[1],
            "actor_name": row[2] or "",
            "server_id": row[3],
            "event": row[4],
        }
        for row in rows
    ]


async def list_player_tags(steam_id):
    async with _conn() as db:
        cur = await db.execute(
            "SELECT t.id, t.steam_id, t.label, t.color, t.created_by, "
            "COALESCE(u.persona, ''), t.created_utc "
            "FROM player_tags t "
            "LEFT JOIN users u ON u.steam_id = t.created_by "
            "WHERE t.steam_id = ? ORDER BY t.created_utc, t.label",
            (steam_id,),
        )
        rows = await cur.fetchall()
    return [
        {
            "id": row[0],
            "steam_id": row[1],
            "label": row[2],
            "color": row[3],
            "created_by": row[4],
            "created_by_name": row[5] or "",
            "created_utc": row[6],
        }
        for row in rows
    ]


async def add_player_tag(steam_id, label, color, created_by):
    tag_id = secrets.token_hex(12)
    created_utc = _now()
    async with _conn() as db:
        await db.execute(
            "INSERT INTO player_tags (id, steam_id, label, color, created_by, created_utc) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(steam_id, label) DO NOTHING",
            (tag_id, steam_id, label, color, created_by, created_utc),
        )
        cur = await db.execute(
            "SELECT id, color, created_by, created_utc FROM player_tags "
            "WHERE steam_id = ? AND label = ?",
            (steam_id, label),
        )
        row = await cur.fetchone()
        await db.commit()
    return {
        "id": row[0],
        "steam_id": steam_id,
        "label": label,
        "color": row[1],
        "created_by": row[2],
        "created_by_name": "",
        "created_utc": row[3],
        "created": row[0] == tag_id,
    }


async def delete_player_tag(tag_id, steam_id):
    async with _conn() as db:
        cur = await db.execute(
            "SELECT 1 FROM player_tags WHERE id = ? AND steam_id = ?",
            (tag_id, steam_id),
        )
        if not await cur.fetchone():
            return False
        await db.execute(
            "DELETE FROM player_tags WHERE id = ? AND steam_id = ?",
            (tag_id, steam_id),
        )
        await db.commit()
    return True


async def create_clan(name, tag, color, description, created_by):
    clan_id = secrets.token_hex(12)
    now = _now()
    async with _conn() as db:
        await db.execute(
            "INSERT INTO clans (id, name, tag, color, description, created_by, created_utc, updated_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(tag) DO NOTHING",
            (clan_id, name, tag, color, description, created_by, now, now),
        )
        cur = await db.execute(
            "SELECT id, name, tag, color, description, created_by, created_utc, updated_utc, emblem_url, cover_url "
            "FROM clans WHERE tag = ?",
            (tag,),
        )
        row = await cur.fetchone()
        await db.commit()
    if not row:
        return None
    return {
        "id": row[0],
        "name": row[1],
        "tag": row[2],
        "color": row[3],
        "description": row[4],
        "created_by": row[5],
        "created_utc": row[6],
        "updated_utc": row[7],
        "member_count": 0, "emblem_url": row[8] or "", "cover_url": row[9] or "",
        "created": row[0] == clan_id,
    }


async def list_clans():
    async with _conn() as db:
        cur = await db.execute(
            "SELECT c.id, c.name, c.tag, c.color, c.description, c.created_by, "
            "c.created_utc, c.updated_utc, c.emblem_url, c.cover_url, COUNT(m.steam_id) "
            "FROM clans c LEFT JOIN clan_members m ON m.clan_id = c.id "
            "GROUP BY c.id, c.name, c.tag, c.color, c.description, c.created_by, "
            "c.created_utc, c.updated_utc, c.emblem_url, c.cover_url "
            "ORDER BY c.name, c.tag"
        )
        rows = await cur.fetchall()
    return [
        {
            "id": row[0],
            "name": row[1],
            "tag": row[2],
            "color": row[3],
            "description": row[4],
            "created_by": row[5],
            "created_utc": row[6],
            "updated_utc": row[7],
            "emblem_url": row[8] or "", "cover_url": row[9] or "", "member_count": int(row[10] or 0),
        }
        for row in rows
    ]


async def get_clan(clan_id):
    """Карточка клана с лидером и полным составом."""
    async with _conn() as db:
        cur = await db.execute(
            "SELECT id, name, tag, color, description, created_by, created_utc, updated_utc, emblem_url, cover_url "
            "FROM clans WHERE id = ?",
            (clan_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        members_cur = await db.execute(
            "SELECT m.steam_id, COALESCE(u.persona, ''), m.member_role, m.joined_utc, "
            "COALESCE(u.last_seen_utc, 0) FROM clan_members m "
            "LEFT JOIN users u ON u.steam_id = m.steam_id "
            "WHERE m.clan_id = ? ORDER BY "
            "CASE WHEN lower(m.member_role) IN ('лидер', 'leader') THEN 0 ELSE 1 END, "
            "COALESCE(u.persona, ''), m.steam_id",
            (clan_id,),
        )
        member_rows = await members_cur.fetchall()
    members = [
        {
            "steam_id": member[0],
            "name": member[1] or "",
            "member_role": member[2] or "Участник",
            "joined_utc": member[3],
            "last_seen_utc": member[4] or None,
        }
        for member in member_rows
    ]
    leader = next(
        (member for member in members if member["member_role"].casefold() in {"лидер", "leader"}),
        None,
    )
    return {
        "id": row[0], "name": row[1], "tag": row[2], "color": row[3],
        "description": row[4] or "", "created_by": row[5],
        "created_utc": row[6], "updated_utc": row[7], "emblem_url": row[8] or "", "cover_url": row[9] or "",
        "member_count": len(members), "leader": leader, "members": members,
    }


async def list_bindings():
    """Все известные привязки Steam ↔ Discord для диагностического раздела."""
    async with _conn() as db:
        cur = await db.execute(
            "SELECT b.user_id, COALESCE(u.persona, ''), b.discord_id, b.discord_name, "
            "b.guild_member, b.roles, b.updated_utc FROM bindings b "
            "LEFT JOIN users u ON u.steam_id = b.user_id ORDER BY b.updated_utc DESC"
        )
        rows = await cur.fetchall()
    result = []
    for row in rows:
        try:
            roles = json.loads(row[5] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            roles = []
        result.append({
            "steam_id": row[0], "persona": row[1] or "", "discord_id": row[2],
            "discord_name": row[3] or "", "guild_member": bool(row[4]),
            "role_ids": roles if isinstance(roles, list) else [], "updated_utc": row[6],
        })
    return result


async def update_clan(clan_id, name, tag, color, description):
    now = _now()
    async with _conn() as db:
        cur = await db.execute("SELECT 1 FROM clans WHERE id = ?", (clan_id,))
        if not await cur.fetchone():
            return False
        await db.execute(
            "UPDATE clans SET name = ?, tag = ?, color = ?, description = ?, updated_utc = ? "
            "WHERE id = ?",
            (name, tag, color, description, now, clan_id),
        )
        await db.commit()
    return True


async def update_clan_media(clan_id, kind, url):
    column = {"emblem": "emblem_url", "cover": "cover_url"}.get(kind)
    if not column:
        return False
    async with _conn() as db:
        cur = await db.execute("UPDATE clans SET " + column + " = ?, updated_utc = ? WHERE id = ?", (url, _now(), clan_id))
        await db.commit()
    return cur.rowcount > 0


async def set_clan_member_role(clan_id, steam_id, member_role):
    now = _now()
    async with _conn() as db:
        cur = await db.execute("SELECT 1 FROM clans WHERE id = ?", (clan_id,))
        if not await cur.fetchone():
            return False
        if member_role == "Лидер":
            await db.execute("UPDATE clan_members SET member_role = 'Участник' WHERE clan_id = ? AND member_role = 'Лидер'", (clan_id,))
        await db.execute(
            "INSERT INTO clan_members (clan_id, steam_id, member_role, joined_utc) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(steam_id) DO UPDATE SET clan_id=excluded.clan_id, member_role=excluded.member_role",
            (clan_id, steam_id, member_role, now),
        )
        await db.execute("UPDATE clans SET updated_utc = ? WHERE id = ?", (now, clan_id))
        await db.commit()
    return True


async def remove_clan_member(clan_id, steam_id):
    async with _conn() as db:
        cur = await db.execute("DELETE FROM clan_members WHERE clan_id = ? AND steam_id = ?", (clan_id, steam_id))
        if cur.rowcount:
            await db.execute("UPDATE clans SET updated_utc = ? WHERE id = ?", (_now(), clan_id))
            await db.commit()
        return cur.rowcount > 0


async def delete_clan(clan_id):
    async with _conn() as db:
        cur = await db.execute("SELECT 1 FROM clans WHERE id = ?", (clan_id,))
        if not await cur.fetchone():
            return False
        await db.execute("DELETE FROM clans WHERE id = ?", (clan_id,))
        await db.commit()
    return True


async def set_player_clan(steam_id, clan_id, member_role="Участник"):
    now = _now()
    async with _conn() as db:
        cur = await db.execute("SELECT 1 FROM clans WHERE id = ?", (clan_id,))
        if not await cur.fetchone():
            return False
        await db.execute(
            "INSERT INTO clan_members (clan_id, steam_id, member_role, joined_utc) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(steam_id) DO UPDATE SET clan_id=excluded.clan_id, "
            "member_role=excluded.member_role, joined_utc=excluded.joined_utc",
            (clan_id, steam_id, member_role, now),
        )
        await db.commit()
    return True


async def remove_player_clan(steam_id):
    async with _conn() as db:
        cur = await db.execute("SELECT clan_id FROM clan_members WHERE steam_id = ?", (steam_id,))
        row = await cur.fetchone()
        if not row:
            return False
        await db.execute("DELETE FROM clan_members WHERE steam_id = ?", (steam_id,))
        await db.commit()
    return True


async def get_player_clan(steam_id):
    async with _conn() as db:
        cur = await db.execute(
            "SELECT c.id, c.name, c.tag, c.color, c.description, "
            "m.member_role, m.joined_utc "
            "FROM clan_members m JOIN clans c ON c.id = m.clan_id "
            "WHERE m.steam_id = ?",
            (steam_id,),
        )
        row = await cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "name": row[1],
        "tag": row[2],
        "color": row[3],
        "description": row[4],
        "member_role": row[5],
        "joined_utc": row[6],
    }


def _vip_row(row):
    if not row:
        return None
    return {
        "id": row[0],
        "server_id": row[1],
        "steam_id": row[2],
        "display_name": row[3] or "",
        "expires_utc": row[4],
        "note": row[5] or "",
        "created_by": row[6],
        "created_utc": row[7],
        "updated_utc": row[8],
        "synced_utc": row[9],
        "sync_state": row[10],
        "sync_error": row[11] or "",
    }


async def save_vip_slot(server_id, steam_id, display_name, expires_utc, note, created_by):
    vip_id = secrets.token_hex(12)
    now = _now()
    async with _conn() as db:
        await db.execute(
            "INSERT INTO vip_slots "
            "(id, server_id, steam_id, display_name, expires_utc, note, created_by, "
            "created_utc, updated_utc, sync_state, sync_error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', '') "
            "ON CONFLICT(server_id, steam_id) DO UPDATE SET "
            "display_name=excluded.display_name, expires_utc=excluded.expires_utc, "
            "note=excluded.note, updated_utc=excluded.updated_utc, "
            "synced_utc=NULL, sync_state='pending', sync_error=''",
            (vip_id, server_id, steam_id, display_name, expires_utc, note, created_by, now, now),
        )
        cur = await db.execute(
            "SELECT id, server_id, steam_id, display_name, expires_utc, note, created_by, "
            "created_utc, updated_utc, synced_utc, sync_state, sync_error "
            "FROM vip_slots WHERE server_id = ? AND steam_id = ?",
            (server_id, steam_id),
        )
        row = await cur.fetchone()
        await db.commit()
    return _vip_row(row)


async def list_vip_slots(server_id):
    async with _conn() as db:
        cur = await db.execute(
            "SELECT id, server_id, steam_id, display_name, expires_utc, note, created_by, "
            "created_utc, updated_utc, synced_utc, sync_state, sync_error "
            "FROM vip_slots WHERE server_id = ? "
            "ORDER BY CASE WHEN expires_utc IS NULL THEN 1 ELSE 0 END, expires_utc, display_name, steam_id",
            (server_id,),
        )
        rows = await cur.fetchall()
    return [_vip_row(row) for row in rows]


async def list_player_vip_slots(steam_id):
    async with _conn() as db:
        cur = await db.execute(
            "SELECT id, server_id, steam_id, display_name, expires_utc, note, created_by, "
            "created_utc, updated_utc, synced_utc, sync_state, sync_error "
            "FROM vip_slots WHERE steam_id = ? ORDER BY server_id",
            (steam_id,),
        )
        rows = await cur.fetchall()
    return [_vip_row(row) for row in rows]


async def get_vip_slot(vip_id, server_id=None):
    query = (
        "SELECT id, server_id, steam_id, display_name, expires_utc, note, created_by, "
        "created_utc, updated_utc, synced_utc, sync_state, sync_error "
        "FROM vip_slots WHERE id = ?"
    )
    params = [vip_id]
    if server_id is not None:
        query += " AND server_id = ?"
        params.append(server_id)
    async with _conn() as db:
        cur = await db.execute(query, tuple(params))
        row = await cur.fetchone()
    return _vip_row(row)


async def mark_vip_sync(vip_id, state, error=""):
    now = _now()
    async with _conn() as db:
        await db.execute(
            "UPDATE vip_slots SET sync_state = ?, sync_error = ?, synced_utc = ?, updated_utc = ? "
            "WHERE id = ?",
            (state, str(error or "")[:500], now, now, vip_id),
        )
        await db.commit()


async def delete_vip_slot(vip_id, server_id):
    async with _conn() as db:
        cur = await db.execute(
            "SELECT 1 FROM vip_slots WHERE id = ? AND server_id = ?",
            (vip_id, server_id),
        )
        if not await cur.fetchone():
            return False
        await db.execute(
            "DELETE FROM vip_slots WHERE id = ? AND server_id = ?",
            (vip_id, server_id),
        )
        await db.commit()
    return True


async def list_expired_vip_slots(now=None, limit=200):
    now = _now() if now is None else float(now)
    limit = max(1, min(int(limit or 200), 1000))
    async with _conn() as db:
        cur = await db.execute(
            "SELECT id, server_id, steam_id, display_name, expires_utc, note, created_by, "
            "created_utc, updated_utc, synced_utc, sync_state, sync_error "
            "FROM vip_slots WHERE expires_utc IS NOT NULL AND expires_utc <= ? "
            "AND sync_state <> 'expired' ORDER BY expires_utc LIMIT ?",
            (now, limit),
        )
        rows = await cur.fetchall()
    return [_vip_row(row) for row in rows]


def _ticket_row(row):
    if not row:
        return None
    return {
        "id": row[0],
        "title": row[1],
        "description": row[2] or "",
        "status": row[3],
        "priority": row[4],
        "player_steam_id": row[5],
        "assigned_to": row[6],
        "assigned_name": row[7] or "",
        "created_by": row[8],
        "created_by_name": row[9] or "",
        "created_utc": row[10],
        "updated_utc": row[11],
        "closed_utc": row[12],
        "comment_count": int(row[13] or 0),
    }


async def create_ticket(title, description, priority, player_steam_id, created_by):
    ticket_id = secrets.token_hex(12)
    now = _now()
    async with _conn() as db:
        await db.execute(
            "INSERT INTO tickets "
            "(id, title, description, status, priority, player_steam_id, assigned_to, "
            "created_by, created_utc, updated_utc, closed_utc) "
            "VALUES (?, ?, ?, 'open', ?, ?, NULL, ?, ?, ?, NULL)",
            (ticket_id, title, description, priority, player_steam_id, created_by, now, now),
        )
        await db.commit()
    return await get_ticket(ticket_id)


async def list_tickets(limit=500):
    limit = max(1, min(int(limit or 500), 1000))
    async with _conn() as db:
        cur = await db.execute(
            "SELECT t.id, t.title, t.description, t.status, t.priority, t.player_steam_id, "
            "t.assigned_to, COALESCE(assignee.persona, ''), t.created_by, "
            "COALESCE(creator.persona, ''), t.created_utc, t.updated_utc, t.closed_utc, "
            "COUNT(c.id) "
            "FROM tickets t "
            "LEFT JOIN users assignee ON assignee.steam_id = t.assigned_to "
            "LEFT JOIN users creator ON creator.steam_id = t.created_by "
            "LEFT JOIN ticket_comments c ON c.ticket_id = t.id "
            "GROUP BY t.id, t.title, t.description, t.status, t.priority, "
            "t.player_steam_id, t.assigned_to, assignee.persona, t.created_by, "
            "creator.persona, t.created_utc, t.updated_utc, t.closed_utc "
            "ORDER BY CASE t.status WHEN 'open' THEN 0 WHEN 'in_progress' THEN 1 "
            "WHEN 'resolved' THEN 2 ELSE 3 END, "
            "CASE t.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 "
            "WHEN 'normal' THEN 2 ELSE 3 END, t.updated_utc DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
    return [_ticket_row(row) for row in rows]


async def get_ticket(ticket_id):
    async with _conn() as db:
        cur = await db.execute(
            "SELECT t.id, t.title, t.description, t.status, t.priority, t.player_steam_id, "
            "t.assigned_to, COALESCE(assignee.persona, ''), t.created_by, "
            "COALESCE(creator.persona, ''), t.created_utc, t.updated_utc, t.closed_utc, "
            "(SELECT COUNT(*) FROM ticket_comments c WHERE c.ticket_id = t.id) "
            "FROM tickets t "
            "LEFT JOIN users assignee ON assignee.steam_id = t.assigned_to "
            "LEFT JOIN users creator ON creator.steam_id = t.created_by "
            "WHERE t.id = ?",
            (ticket_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        comments_cur = await db.execute(
            "SELECT c.id, c.ticket_id, c.author_steam_id, COALESCE(u.persona, ''), "
            "c.body, c.created_utc "
            "FROM ticket_comments c LEFT JOIN users u ON u.steam_id = c.author_steam_id "
            "WHERE c.ticket_id = ? ORDER BY c.created_utc",
            (ticket_id,),
        )
        comment_rows = await comments_cur.fetchall()
    ticket = _ticket_row(row)
    ticket["comments"] = [
        {
            "id": comment[0],
            "ticket_id": comment[1],
            "author_steam_id": comment[2],
            "author_name": comment[3] or "",
            "body": comment[4],
            "created_utc": comment[5],
        }
        for comment in comment_rows
    ]
    return ticket


async def update_ticket(ticket_id, title, description, status, priority, player_steam_id, assigned_to):
    now = _now()
    closed_utc = now if status in {"resolved", "closed"} else None
    async with _conn() as db:
        cur = await db.execute("SELECT 1 FROM tickets WHERE id = ?", (ticket_id,))
        if not await cur.fetchone():
            return False
        await db.execute(
            "UPDATE tickets SET title = ?, description = ?, status = ?, priority = ?, "
            "player_steam_id = ?, assigned_to = ?, updated_utc = ?, closed_utc = ? "
            "WHERE id = ?",
            (
                title, description, status, priority, player_steam_id,
                assigned_to, now, closed_utc, ticket_id,
            ),
        )
        await db.commit()
    return True


async def delete_ticket(ticket_id):
    async with _conn() as db:
        cur = await db.execute("SELECT 1 FROM tickets WHERE id = ?", (ticket_id,))
        if not await cur.fetchone():
            return False
        await db.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))
        await db.commit()
    return True


async def add_ticket_comment(ticket_id, author_steam_id, body):
    comment_id = secrets.token_hex(12)
    now = _now()
    async with _conn() as db:
        cur = await db.execute("SELECT 1 FROM tickets WHERE id = ?", (ticket_id,))
        if not await cur.fetchone():
            return None
        await db.execute(
            "INSERT INTO ticket_comments (id, ticket_id, author_steam_id, body, created_utc) "
            "VALUES (?, ?, ?, ?, ?)",
            (comment_id, ticket_id, author_steam_id, body, now),
        )
        await db.execute("UPDATE tickets SET updated_utc = ? WHERE id = ?", (now, ticket_id))
        await db.commit()
    return {
        "id": comment_id,
        "ticket_id": ticket_id,
        "author_steam_id": author_steam_id,
        "author_name": "",
        "body": body,
        "created_utc": now,
    }


def _managed_ban_row(row):
    if not row:
        return None
    return {
        "id": row[0],
        "server_id": row[1],
        "steam_id": row[2],
        "display_name": row[3] or "",
        "reason": row[4] or "",
        "expires_utc": row[5],
        "created_by": row[6],
        "created_utc": row[7],
        "updated_utc": row[8],
        "sync_state": row[9],
        "sync_error": row[10] or "",
    }


async def save_managed_ban(server_id, steam_id, display_name, reason, expires_utc, created_by):
    ban_id = secrets.token_hex(12)
    now = _now()
    async with _conn() as db:
        await db.execute(
            "INSERT INTO managed_bans "
            "(id, server_id, steam_id, display_name, reason, expires_utc, created_by, "
            "created_utc, updated_utc, sync_state, sync_error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', '') "
            "ON CONFLICT(server_id, steam_id) DO UPDATE SET "
            "display_name=excluded.display_name, reason=excluded.reason, "
            "expires_utc=excluded.expires_utc, created_by=excluded.created_by, "
            "updated_utc=excluded.updated_utc, sync_state='active', sync_error=''",
            (ban_id, server_id, steam_id, display_name, reason, expires_utc, created_by, now, now),
        )
        cur = await db.execute(
            "SELECT id, server_id, steam_id, display_name, reason, expires_utc, "
            "created_by, created_utc, updated_utc, sync_state, sync_error "
            "FROM managed_bans WHERE server_id = ? AND steam_id = ?",
            (server_id, steam_id),
        )
        row = await cur.fetchone()
        await db.commit()
    return _managed_ban_row(row)


async def list_managed_bans(server_id):
    async with _conn() as db:
        cur = await db.execute(
            "SELECT id, server_id, steam_id, display_name, reason, expires_utc, "
            "created_by, created_utc, updated_utc, sync_state, sync_error "
            "FROM managed_bans WHERE server_id = ?",
            (server_id,),
        )
        rows = await cur.fetchall()
    return [_managed_ban_row(row) for row in rows]


async def list_expired_managed_bans(now=None, limit=200):
    now = _now() if now is None else float(now)
    async with _conn() as db:
        cur = await db.execute(
            "SELECT id, server_id, steam_id, display_name, reason, expires_utc, "
            "created_by, created_utc, updated_utc, sync_state, sync_error "
            "FROM managed_bans WHERE expires_utc IS NOT NULL AND expires_utc <= ? "
            "AND sync_state <> 'expired' ORDER BY expires_utc LIMIT ?",
            (now, max(1, min(int(limit or 200), 1000))),
        )
        rows = await cur.fetchall()
    return [_managed_ban_row(row) for row in rows]


async def mark_managed_ban(ban_id, state, error=""):
    async with _conn() as db:
        await db.execute(
            "UPDATE managed_bans SET sync_state = ?, sync_error = ?, updated_utc = ? WHERE id = ?",
            (state, str(error or "")[:500], _now(), ban_id),
        )
        await db.commit()


async def delete_managed_ban(server_id, steam_id):
    async with _conn() as db:
        await db.execute(
            "DELETE FROM managed_bans WHERE server_id = ? AND steam_id = ?",
            (server_id, steam_id),
        )
        await db.commit()


async def list_staff_users():
    """Return linked Discord guild members known to the panel."""
    async with _conn() as db:
        cur = await db.execute(
            "SELECT u.steam_id, u.persona, u.last_seen_utc, "
            "b.discord_id, b.discord_name, b.roles, b.updated_utc "
            "FROM users u JOIN bindings b ON b.user_id = u.steam_id "
            "WHERE b.guild_member = 1 ORDER BY COALESCE(u.persona, ''), u.steam_id"
        )
        rows = await cur.fetchall()
    result = []
    for row in rows:
        try:
            roles = json.loads(row[5] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            roles = []
        result.append(
            {
                "steam_id": row[0],
                "persona": row[1] or "",
                "last_seen_utc": row[2],
                "discord_id": row[3],
                "discord_name": row[4] or "",
                "role_ids": [str(role)[:64] for role in roles[:100] if str(role).strip()],
                "roles_updated_utc": row[6],
            }
        )
    return result


# ---------- bindings ----------

async def upsert_binding(user_id, discord_id, discord_name, member_roles, guild_member):
    roles_json = json.dumps(
        [str(role)[:64] for role in (member_roles or [])[:100] if str(role).strip()],
        ensure_ascii=False,
    )
    async with _conn() as db:
        await db.execute(
            "INSERT INTO bindings (user_id, discord_id, discord_name, guild_member, roles, updated_utc) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET discord_id=excluded.discord_id, "
            "discord_name=excluded.discord_name, guild_member=excluded.guild_member, "
            "roles=excluded.roles, updated_utc=excluded.updated_utc",
            (user_id, discord_id, discord_name, 1 if guild_member else 0, roles_json, _now()),
        )
        await db.commit()


async def get_binding(user_id):
    async with _conn() as db:
        cur = await db.execute(
            "SELECT user_id, discord_id, discord_name, guild_member, roles, updated_utc "
            "FROM bindings WHERE user_id = ?",
            (user_id,),
        )
        row = await cur.fetchone()
    if not row:
        return None
    try:
        roles = json.loads(row[4] or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        roles = []
    if not isinstance(roles, list):
        roles = []
    return {
        "user_id": row[0],
        "discord_id": row[1],
        "discord_name": row[2],
        "guild_member": bool(row[3]),
        "roles": [str(role)[:64] for role in roles[:100] if str(role).strip()],
        "updated_utc": row[5],
    }


async def get_steam_by_discord(discord_id):
    async with _conn() as db:
        cur = await db.execute(
            "SELECT user_id FROM bindings WHERE discord_id = ?", (discord_id,)
        )
        row = await cur.fetchone()
    return row[0] if row else None


async def delete_binding(user_id):
    async with _conn() as db:
        await db.execute("DELETE FROM bindings WHERE user_id = ?", (user_id,))
        await db.commit()


# ---------- sessions ----------

async def create_oauth_state(state, provider, user_id=None):
    now = _now()
    async with _conn() as db:
        await db.execute(
            "INSERT INTO oauth_states (state, provider, user_id, created_utc, expires_utc) VALUES (?, ?, ?, ?, ?)",
            (state, str(provider)[:32], user_id, now, now + 600),
        )
        await db.commit()


async def consume_oauth_state(state, provider, user_id=None):
    """Atomically validate and consume a short-lived, one-time OAuth state."""
    if not state or not provider:
        return False
    async with _conn() as db:
        await db.execute("BEGIN IMMEDIATE")
        lock_clause = " FOR UPDATE" if config.DATABASE_URL else ""
        cur = await db.execute(
            "SELECT user_id, expires_utc FROM oauth_states "
            "WHERE state = ? AND provider = ?" + lock_clause,
            (state, provider),
        )
        row = await cur.fetchone()
        await db.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
        await db.commit()
    if not row or row[1] < _now():
        return False
    return row[0] == user_id


async def claim_idempotency(actor_steam_id, idem_key, fingerprint, ttl=600):
    """Claim a request key or return a safe replay/conflict decision."""
    now = _now()
    async with _conn() as db:
        await db.execute("BEGIN IMMEDIATE")
        if config.DATABASE_URL:
            # Insert-first makes concurrent claims serialize on PostgreSQL's
            # primary key instead of racing after a plain SELECT.
            await db.execute(
                "INSERT INTO idempotency_keys "
                "(actor_steam_id, idem_key, fingerprint, created_utc, expires_utc) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(actor_steam_id, idem_key) DO NOTHING",
                (actor_steam_id, idem_key, fingerprint, now, now + ttl),
            )
            await db.execute(
                "UPDATE idempotency_keys SET fingerprint = ?, created_utc = ?, "
                "expires_utc = ?, status_code = NULL, response_json = NULL "
                "WHERE actor_steam_id = ? AND idem_key = ? AND expires_utc < ?",
                (fingerprint, now, now + ttl, actor_steam_id, idem_key, now),
            )
        cur = await db.execute(
            "SELECT fingerprint, status_code, response_json, expires_utc "
            "FROM idempotency_keys WHERE actor_steam_id = ? AND idem_key = ?",
            (actor_steam_id, idem_key),
        )
        row = await cur.fetchone()
        if row and row[3] >= now:
            await db.commit()
            if row[0] != fingerprint:
                return {"kind": "conflict"}
            if row[2] is None:
                return {"kind": "in_progress"}
            try:
                response = json.loads(row[2])
            except (TypeError, ValueError, json.JSONDecodeError):
                return {"kind": "in_progress"}
            return {"kind": "replay", "status_code": int(row[1] or 200), "response": response}
        await db.execute(
            "INSERT INTO idempotency_keys "
            "(actor_steam_id, idem_key, fingerprint, created_utc, expires_utc) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(actor_steam_id, idem_key) DO UPDATE SET "
            "fingerprint=excluded.fingerprint, created_utc=excluded.created_utc, "
            "expires_utc=excluded.expires_utc, status_code=NULL, response_json=NULL",
            (actor_steam_id, idem_key, fingerprint, now, now + ttl),
        )
        await db.commit()
    return {"kind": "claimed"}


async def complete_idempotency(actor_steam_id, idem_key, status_code, response):
    try:
        serialized = json.dumps(response, ensure_ascii=False)
    except (TypeError, ValueError):
        serialized = json.dumps({"ok": True, "replayed": True}, ensure_ascii=False)
    if len(serialized.encode("utf-8")) > 256 * 1024:
        serialized = json.dumps(
            {"ok": True, "replayed": True, "response_truncated": True},
            ensure_ascii=False,
        )
    async with _conn() as db:
        await db.execute(
            "UPDATE idempotency_keys SET status_code = ?, response_json = ? "
            "WHERE actor_steam_id = ? AND idem_key = ?",
            (int(status_code), serialized, actor_steam_id, idem_key),
        )
        await db.commit()


async def abort_idempotency(actor_steam_id, idem_key):
    async with _conn() as db:
        await db.execute(
            "DELETE FROM idempotency_keys WHERE actor_steam_id = ? AND idem_key = ?",
            (actor_steam_id, idem_key),
        )
        await db.commit()

def new_session_token():
    return secrets.token_urlsafe(32)


def _session_key(token):
    """Store only a one-way digest so a database leak cannot replay sessions."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_session(steam_id):
    token = new_session_token()
    now = _now()
    expires = now + timedelta(days=config.SESSION_TTL_DAYS).total_seconds()
    async with _conn() as db:
        cur = await db.execute("SELECT session_version FROM users WHERE steam_id = ?", (steam_id,))
        row = await cur.fetchone()
        session_version = int(row[0] if row else 0)
        await db.execute(
            "INSERT INTO sessions (token, user_id, created_utc, expires_utc, session_version) VALUES (?, ?, ?, ?, ?)",
            (_session_key(token), steam_id, now, expires, session_version),
        )
        await db.commit()
    return token


async def get_session(token):
    if not token or len(token) > 512:
        return None
    async with _conn() as db:
        cur = await db.execute(
            "SELECT s.user_id, s.expires_utc, s.session_version, COALESCE(u.session_version, 0) "
            "FROM sessions s LEFT JOIN users u ON u.steam_id = s.user_id WHERE s.token = ?",
            (_session_key(token),),
        )
        row = await cur.fetchone()
        if not row:
            return None
        user_id, expires, session_version, current_version = row
        if expires < _now():
            await db.execute("DELETE FROM sessions WHERE token = ?", (_session_key(token),))
            await db.commit()
            return None
        if int(session_version) != int(current_version):
            await db.execute("DELETE FROM sessions WHERE token = ?", (_session_key(token),))
            await db.commit()
            return None
    return user_id


async def delete_session(token):
    if not token:
        return
    async with _conn() as db:
        await db.execute("DELETE FROM sessions WHERE token = ?", (_session_key(token),))
        await db.commit()


async def revoke_user_sessions(user_id):
    async with _conn() as db:
        await db.execute(
            "UPDATE users SET session_version = session_version + 1 WHERE steam_id = ?",
            (user_id,),
        )
        await db.commit()


async def prune_sessions():
    async with _conn() as db:
        await db.execute("DELETE FROM sessions WHERE expires_utc < ?", (_now(),))
        await db.execute("DELETE FROM oauth_states WHERE expires_utc < ?", (_now(),))
        await db.execute("DELETE FROM idempotency_keys WHERE expires_utc < ?", (_now(),))
        await db.commit()


# ---------- role permissions ----------

async def get_role_perms(role_ref):
    async with _conn() as db:
        cur = await db.execute(
            "SELECT perms FROM role_permissions WHERE role_ref = ?", (role_ref,)
        )
        row = await cur.fetchone()
    if not row:
        return None
    try:
        value = json.loads(row[0] or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


async def get_role_perms_all():
    async with _conn() as db:
        cur = await db.execute("SELECT role_ref, perms FROM role_permissions")
        rows = await cur.fetchall()
    result = {}
    for role_ref, raw_perms in rows:
        try:
            perms = json.loads(raw_perms or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            perms = []
        result[role_ref] = perms if isinstance(perms, list) else []
    return result


async def set_role_perms(role_ref, perms):
    async with _conn() as db:
        await db.execute(
            "INSERT INTO role_permissions (role_ref, perms, updated_utc) VALUES (?, ?, ?) "
            "ON CONFLICT(role_ref) DO UPDATE SET perms = excluded.perms, updated_utc = excluded.updated_utc",
            (role_ref, json.dumps(perms or [], ensure_ascii=False), _now()),
        )
        await db.commit()


# ---------- monitoring history ----------

async def record_monitoring(server_id, current, maximum, players):
    """Сохраняет минутный срез онлайна и последнее состояние игроков."""
    now = _now()
    bucket = int(now // 60) * 60
    async with _conn() as db:
        await db.execute(
            "INSERT INTO population_samples (server_id, bucket_utc, players, max_players) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(server_id, bucket_utc) DO UPDATE SET players=excluded.players, max_players=excluded.max_players",
            (server_id, bucket, int(current or 0), int(maximum or 0)),
        )
        presence_cur = await db.execute(
            "SELECT steam_id, total_seconds, session_count, current_session_started_utc, "
            "last_seen_utc, online FROM player_presence WHERE server_id = ?",
            (server_id,),
        )
        previous_presence = {
            str(row[0]): {
                "total_seconds": float(row[1] or 0),
                "session_count": int(row[2] or 0),
                "session_started": row[3],
                "last_seen": float(row[4] or 0),
                "online": bool(row[5]),
            }
            for row in await presence_cur.fetchall()
        }
        await db.execute(
            "UPDATE player_presence SET online = 0 WHERE server_id = ?",
            (server_id,),
        )
        for player in (players or [])[:1000]:
            if not isinstance(player, dict):
                continue
            steam_id = str(player.get("steamId") or "").strip()
            if not steam_id:
                continue
            previous = previous_presence.get(steam_id)
            continuous = bool(
                previous
                and previous["online"]
                and 0 <= now - previous["last_seen"] <= 180
            )
            total_seconds = previous["total_seconds"] if previous else 0.0
            session_count = previous["session_count"] if previous else 0
            session_started = previous["session_started"] if continuous else now
            if continuous:
                total_seconds += min(120.0, max(0.0, now - previous["last_seen"]))
            else:
                session_count += 1
            await db.execute(
                "INSERT INTO player_presence "
                "(server_id, steam_id, total_seconds, session_count, "
                "current_session_started_utc, last_seen_utc, online) "
                "VALUES (?, ?, ?, ?, ?, ?, 1) "
                "ON CONFLICT(server_id, steam_id) DO UPDATE SET "
                "total_seconds=excluded.total_seconds, session_count=excluded.session_count, "
                "current_session_started_utc=excluded.current_session_started_utc, "
                "last_seen_utc=excluded.last_seen_utc, online=1",
                (
                    server_id, steam_id, total_seconds, session_count,
                    session_started, now,
                ),
            )
            await db.execute(
                "INSERT INTO player_activity "
                "(server_id, steam_id, name, kills, deaths, cash, ping_ms, faction, seen_utc, first_seen_utc, last_seen_utc) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(server_id, steam_id) DO UPDATE SET "
                "name=excluded.name, kills=excluded.kills, deaths=excluded.deaths, cash=excluded.cash, "
                "ping_ms=excluded.ping_ms, faction=excluded.faction, seen_utc=excluded.seen_utc, "
                "last_seen_utc=excluded.last_seen_utc",
                (
                    server_id, steam_id, str(player.get("name") or "Игрок")[:80],
                    int(player.get("kills") or 0), int(player.get("deaths") or 0),
                    int(player.get("cash") or 0), int(player.get("pingMs") or 0),
                    str(player.get("faction") or "")[:40], now, now, now,
                ),
            )
        await db.execute("DELETE FROM population_samples WHERE bucket_utc < ?", (int(now - 31 * 86400),))
        await db.execute(
            "DELETE FROM player_activity WHERE seen_utc < ?",
            (int(now - config.PLAYER_ACTIVITY_RETENTION_DAYS * 86400),),
        )
        await db.commit()


async def get_population(hours=24):
    since = int(_now() - max(1, min(int(hours), 24 * 31)) * 3600)
    async with _conn() as db:
        cur = await db.execute(
            "SELECT server_id, bucket_utc, players, max_players FROM population_samples "
            "WHERE bucket_utc >= ? ORDER BY bucket_utc", (since,)
        )
        rows = await cur.fetchall()
    return [
        {"server_id": r[0], "ts": r[1], "players": r[2], "max_players": r[3]}
        for r in rows
    ]


async def get_player_activity(limit=100):
    async with _conn() as db:
        cur = await db.execute(
            "SELECT server_id, steam_id, name, kills, deaths, cash, ping_ms, faction, seen_utc, "
            "first_seen_utc, last_seen_utc "
            "FROM player_activity ORDER BY kills DESC, deaths ASC, seen_utc DESC LIMIT ?",
            (max(1, min(int(limit), 100)),),
        )
        rows = await cur.fetchall()
    return [
        {"server_id": r[0], "steam_id": r[1], "name": r[2], "kills": r[3], "deaths": r[4],
         "cash": r[5], "ping_ms": r[6], "faction": r[7], "seen_utc": r[8],
         "first_seen_utc": r[9], "last_seen_utc": r[10]}
        for r in rows
    ]


async def get_player_leaderboard(limit=100, sort="kills"):
    """Return aggregated player statistics enriched with presence counters."""
    limit = max(1, min(int(limit or 100), 200))
    sort = str(sort or "kills").lower()
    if sort not in {"kills", "kd", "playtime", "sessions", "recent"}:
        sort = "kills"
    async with _conn() as database:
        activity_cur = await database.execute(
            "SELECT server_id, steam_id, name, kills, deaths, cash, ping_ms, faction, seen_utc, "
            "first_seen_utc, last_seen_utc FROM player_activity "
            "ORDER BY last_seen_utc DESC LIMIT 10000"
        )
        activity_rows = await activity_cur.fetchall()
        presence_cur = await database.execute(
            "SELECT steam_id, total_seconds, session_count, last_seen_utc, online "
            "FROM player_presence ORDER BY last_seen_utc DESC"
        )
        presence_rows = await presence_cur.fetchall()

    profiles = _aggregate_player_rows(activity_rows)
    now = _now()
    presence = {}
    for row in presence_rows:
        steam_id = str(row[0] or "")
        if not steam_id:
            continue
        item = presence.setdefault(
            steam_id,
            {"total_seconds": 0.0, "session_count": 0, "online": False},
        )
        item["total_seconds"] += float(row[1] or 0)
        item["session_count"] += int(row[2] or 0)
        item["online"] = item["online"] or (
            bool(row[4]) and 0 <= now - float(row[3] or 0) <= 180
        )

    result = []
    for steam_id, profile in profiles.items():
        tracked = presence.get(steam_id, {})
        profile["total_seconds"] = round(float(tracked.get("total_seconds", 0)))
        profile["session_count"] = int(tracked.get("session_count", 0))
        profile["online"] = bool(tracked.get("online", False))
        result.append(profile)

    def kd_value(item):
        deaths = int(item.get("deaths") or 0)
        kills = int(item.get("kills") or 0)
        # K/D is undefined until a player has at least one death.  Do not
        # promote such entries above players with a real calculated ratio.
        return kills / deaths if deaths else None

    sorters = {
        "kills": lambda item: (
            -int(item.get("kills") or 0),
            int(item.get("deaths") or 0),
            -float(item.get("last_seen_utc") or 0),
        ),
        "kd": lambda item: (
            1 if kd_value(item) is None else 0,
            -(kd_value(item) or 0),
            -int(item.get("kills") or 0),
            -float(item.get("last_seen_utc") or 0),
        ),
        "playtime": lambda item: (
            -int(item.get("total_seconds") or 0),
            -int(item.get("kills") or 0),
        ),
        "sessions": lambda item: (
            -int(item.get("session_count") or 0),
            -int(item.get("total_seconds") or 0),
        ),
        "recent": lambda item: (
            -float(item.get("last_seen_utc") or 0),
            str(item.get("name") or "").casefold(),
        ),
    }
    result.sort(key=sorters[sort])
    return result[:limit]


def _aggregate_player_rows(rows):
    profiles = {}
    for row in rows:
        steam_id = str(row[1] or "")
        if not steam_id:
            continue
        profile = profiles.setdefault(
            steam_id,
            {
                "steam_id": steam_id,
                "name": str(row[2] or "Игрок"),
                "kills": 0,
                "deaths": 0,
                "servers": [],
                "first_seen_utc": None,
                "last_seen_utc": None,
            },
        )
        if row[2]:
            profile["name"] = str(row[2])
        profile["kills"] += int(row[3] or 0)
        profile["deaths"] += int(row[4] or 0)
        first_seen = row[9] if row[9] is not None else row[8]
        last_seen = row[10] if row[10] is not None else row[8]
        if first_seen is not None:
            profile["first_seen_utc"] = (
                first_seen
                if profile["first_seen_utc"] is None
                else min(profile["first_seen_utc"], first_seen)
            )
        if last_seen is not None:
            profile["last_seen_utc"] = (
                last_seen
                if profile["last_seen_utc"] is None
                else max(profile["last_seen_utc"], last_seen)
            )
        profile["servers"].append(
            {
                "server_id": row[0],
                "kills": int(row[3] or 0),
                "deaths": int(row[4] or 0),
                "cash": int(row[5] or 0),
                "ping_ms": int(row[6] or 0),
                "faction": str(row[7] or ""),
                "seen_utc": row[8],
            }
        )
    for profile in profiles.values():
        profile["server_count"] = len(profile["servers"])
        profile["kd"] = round(profile["kills"] / max(1, profile["deaths"]), 2)
    return profiles


async def search_players(query="", limit=50, offset=0):
    query = str(query or "").strip()[:120]
    limit = max(1, min(int(limit), 100))
    offset = max(0, min(int(offset), 10000))
    pattern = f"%{query}%"
    async with _conn() as db:
        cur = await db.execute(
            "SELECT server_id, steam_id, name, kills, deaths, cash, ping_ms, faction, seen_utc, "
            "first_seen_utc, last_seen_utc FROM player_activity "
            "WHERE ? = '' OR steam_id LIKE ? OR name LIKE ? "
            "ORDER BY last_seen_utc DESC, name ASC LIMIT ? OFFSET ?",
            (query, pattern, pattern, limit * 8, offset),
        )
        rows = await cur.fetchall()
    profiles = list(_aggregate_player_rows(rows).values())
    profiles.sort(key=lambda item: (item["last_seen_utc"] is None, -(item["last_seen_utc"] or 0)))
    return profiles[:limit]


async def get_player_profile(steam_id):
    steam_id = str(steam_id or "").strip()
    async with _conn() as db:
        cur = await db.execute(
            "SELECT server_id, steam_id, name, kills, deaths, cash, ping_ms, faction, seen_utc, "
            "first_seen_utc, last_seen_utc FROM player_activity WHERE steam_id = ? "
            "ORDER BY last_seen_utc DESC",
            (steam_id,),
        )
        rows = await cur.fetchall()
    profiles = _aggregate_player_rows(rows)
    profile = profiles.get(steam_id)
    if not profile:
        return None
    presence = await get_player_presence(steam_id)
    profile.update(
        {
            "total_seconds": presence["total_seconds"],
            "session_count": presence["session_count"],
            "current_session_seconds": presence["current_session_seconds"],
        }
    )
    by_server = {item["server_id"]: item for item in presence["servers"]}
    for server in profile["servers"]:
        server_presence = by_server.get(server["server_id"], {})
        server["total_seconds"] = server_presence.get("total_seconds", 0)
        server["session_count"] = server_presence.get("session_count", 0)
        server["online"] = server_presence.get("online", False)
    return profile


async def get_player_presence(steam_id):
    now = _now()
    async with _conn() as db:
        cur = await db.execute(
            "SELECT server_id, total_seconds, session_count, current_session_started_utc, "
            "last_seen_utc, online FROM player_presence WHERE steam_id = ? "
            "ORDER BY last_seen_utc DESC",
            (str(steam_id or "").strip(),),
        )
        rows = await cur.fetchall()
    servers = []
    total_seconds = 0.0
    session_count = 0
    current_session_seconds = 0.0
    for row in rows:
        recent_online = bool(row[5]) and 0 <= now - float(row[4] or 0) <= 180
        server_current = (
            max(0.0, now - float(row[3]))
            if recent_online and row[3] is not None
            else 0.0
        )
        stored_total = float(row[1] or 0)
        total_seconds += stored_total
        session_count += int(row[2] or 0)
        current_session_seconds += server_current
        servers.append(
            {
                "server_id": row[0],
                "total_seconds": round(stored_total),
                "session_count": int(row[2] or 0),
                "current_session_seconds": round(server_current),
                "last_seen_utc": row[4],
                "online": recent_online,
            }
        )
    return {
        "total_seconds": round(total_seconds),
        "session_count": session_count,
        "current_session_seconds": round(current_session_seconds),
        "servers": servers,
    }


# ---------- multi-bot control plane ----------

def _bot_row(row):
    if not row:
        return None
    try:
        scopes = json.loads(row[6] or "[]")
    except (TypeError, ValueError):
        scopes = []
    try:
        metadata = json.loads(row[11] or "{}")
    except (TypeError, ValueError):
        metadata = {}
    return {
        "bot_id": row[0], "name": row[1], "guild_id": row[2] or "",
        "bot_user_id": row[3] or "", "base_url": row[4] or "",
        "token_hash": row[5], "scopes": scopes, "enabled": bool(row[7]),
        "status": row[8], "version": row[9] or "", "detail": row[10] or "",
        "metadata": metadata, "created_utc": row[12], "updated_utc": row[13],
        "last_seen_utc": row[14],
    }


_BOT_COLUMNS = (
    "bot_id, name, guild_id, bot_user_id, base_url, token_hash, scopes, enabled, "
    "status, version, detail, metadata, created_utc, updated_utc, last_seen_utc"
)


async def create_bot_integration(bot_id, name, token_hash, scopes, guild_id="", base_url=""):
    now = _now()
    async with _conn() as database:
        await database.execute(
            "INSERT INTO bot_integrations "
            "(bot_id, name, guild_id, base_url, token_hash, scopes, created_utc, updated_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (bot_id, name, guild_id, base_url, token_hash, json.dumps(scopes), now, now),
        )
        await database.commit()
    return await get_bot_integration(bot_id)


async def get_bot_integration(bot_id):
    async with _conn() as database:
        cur = await database.execute(
            f"SELECT {_BOT_COLUMNS} FROM bot_integrations WHERE bot_id = ?", (bot_id,)
        )
        row = await cur.fetchone()
    return _bot_row(row)


async def list_bot_integrations():
    async with _conn() as database:
        cur = await database.execute(
            f"SELECT {_BOT_COLUMNS} FROM bot_integrations ORDER BY enabled DESC, name ASC"
        )
        rows = await cur.fetchall()
    return [_bot_row(row) for row in rows]


async def update_bot_integration(bot_id, **changes):
    allowed = {"name", "guild_id", "bot_user_id", "base_url", "token_hash", "scopes", "enabled", "status", "version", "detail", "metadata", "last_seen_utc"}
    values = []
    assignments = []
    for key, value in changes.items():
        if key not in allowed:
            continue
        if key in {"scopes", "metadata"}:
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if key == "enabled":
            value = 1 if value else 0
        assignments.append(f"{key} = ?")
        values.append(value)
    if not assignments:
        return await get_bot_integration(bot_id)
    assignments.append("updated_utc = ?")
    values.extend((_now(), bot_id))
    async with _conn() as database:
        await database.execute(
            f"UPDATE bot_integrations SET {', '.join(assignments)} WHERE bot_id = ?", values
        )
        await database.commit()
    return await get_bot_integration(bot_id)


async def delete_bot_integration(bot_id):
    async with _conn() as database:
        await database.execute("DELETE FROM bot_events WHERE bot_id = ?", (bot_id,))
        await database.execute("DELETE FROM bot_integrations WHERE bot_id = ?", (bot_id,))
        await database.commit()


async def append_bot_event(bot_id, event_id, event_type, payload):
    try:
        async with _conn() as database:
            await database.execute(
                "INSERT INTO bot_events (bot_id, event_id, event_type, payload, created_utc) "
                "VALUES (?, ?, ?, ?, ?)",
                (bot_id, event_id, event_type, json.dumps(payload, ensure_ascii=False, separators=(",", ":")), _now()),
            )
            await database.commit()
        return True
    except Exception as exc:
        # Duplicate event ids are an expected idempotency path on both engines.
        if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
            return False
        raise


async def create_vip_order(order):
    async with _conn() as database:
        await database.execute(
            "INSERT INTO vip_orders (id,buyer_steam_id,server_id,plan_id,amount,months,seats,recipients,status,created_utc,actor_steam_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            tuple(json.dumps(order[k]) if k == 'recipients' else order[k] for k in ('id','buyer_steam_id','server_id','plan_id','amount','months','seats','recipients','status','created_utc','actor_steam_id')),
        )
        await database.commit()
    return order


async def list_vip_orders():
    keys = ('id','buyer_steam_id','server_id','plan_id','amount','months','seats','recipients','status','created_utc','actor_steam_id')
    async with _conn() as database:
        cur = await database.execute('SELECT ' + ','.join(keys) + ' FROM vip_orders ORDER BY created_utc DESC LIMIT 200')
        rows = await cur.fetchall()
    result = [dict(zip(keys,row)) for row in rows]
    for item in result:
        item['recipients'] = json.loads(item['recipients'])
    return result


async def cancel_vip_order(order_id):
    async with _conn() as database:
        cur = await database.execute("UPDATE vip_orders SET status='cancelled' WHERE id=? AND status='awaiting_payment'", (order_id,))
        await database.commit()
        return cur.rowcount > 0


async def list_bot_events(bot_id, limit=100):
    async with _conn() as database:
        cur = await database.execute(
            "SELECT event_id, event_type, payload, created_utc FROM bot_events "
            "WHERE bot_id = ? ORDER BY id DESC LIMIT ?",
            (bot_id, max(1, min(int(limit), 500))),
        )
        rows = await cur.fetchall()
    result = []
    for row in rows:
        try:
            payload = json.loads(row[2] or "{}")
        except (TypeError, ValueError):
            payload = {}
        result.append({"event_id": row[0], "event_type": row[1], "payload": payload, "created_utc": row[3]})
    return result

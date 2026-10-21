"""Управляемые баны."""

import secrets

try:
    from .core import _conn, _now
except ImportError:
    from core import _conn, _now


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


def _queued_ban_row(row):
    if not row:
        return None
    return {
        "steam_id": row[0],
        "display_name": row[1] or "",
        "reason": row[2] or "",
        "expires_utc": row[3],
        "created_by": row[4],
        "created_utc": row[5],
        "updated_utc": row[6],
        "applied_servers": [s for s in (row[7] or "").split(",") if s],
    }


_QUEUED_BAN_COLS = (
    "steam_id, display_name, reason, expires_utc, created_by, "
    "created_utc, updated_utc, applied_servers"
)


async def enqueue_queued_ban(steam_id, display_name, reason, expires_utc, created_by, applied_servers=()):
    now = _now()
    applied = ",".join(dict.fromkeys(str(s) for s in applied_servers if s))
    async with _conn() as db:
        await db.execute(
            "INSERT INTO queued_bans "
            f"({_QUEUED_BAN_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(steam_id) DO UPDATE SET "
            "display_name=excluded.display_name, reason=excluded.reason, "
            "expires_utc=excluded.expires_utc, updated_utc=excluded.updated_utc, "
            "applied_servers=excluded.applied_servers",
            (steam_id, display_name, reason, expires_utc, created_by, now, now, applied),
        )
        cur = await db.execute(
            f"SELECT {_QUEUED_BAN_COLS} FROM queued_bans WHERE steam_id = ?",
            (steam_id,),
        )
        row = await cur.fetchone()
        await db.commit()
    return _queued_ban_row(row)


async def get_queued_ban(steam_id):
    async with _conn() as db:
        cur = await db.execute(
            f"SELECT {_QUEUED_BAN_COLS} FROM queued_bans WHERE steam_id = ?",
            (steam_id,),
        )
        row = await cur.fetchone()
    return _queued_ban_row(row)


async def list_queued_bans():
    async with _conn() as db:
        cur = await db.execute(
            f"SELECT {_QUEUED_BAN_COLS} FROM queued_bans ORDER BY created_utc"
        )
        rows = await cur.fetchall()
    return [_queued_ban_row(row) for row in rows]


async def update_queued_ban(steam_id, reason, expires_utc):
    async with _conn() as db:
        await db.execute(
            "UPDATE queued_bans SET reason = ?, expires_utc = ?, updated_utc = ? "
            "WHERE steam_id = ?",
            (reason, expires_utc, _now(), steam_id),
        )
        await db.commit()


async def mark_queued_ban_applied(steam_id, applied_servers):
    applied = ",".join(dict.fromkeys(str(s) for s in applied_servers if s))
    async with _conn() as db:
        await db.execute(
            "UPDATE queued_bans SET applied_servers = ?, updated_utc = ? WHERE steam_id = ?",
            (applied, _now(), steam_id),
        )
        await db.commit()


async def delete_queued_ban(steam_id):
    async with _conn() as db:
        cur = await db.execute(
            "DELETE FROM queued_bans WHERE steam_id = ?", (steam_id,)
        )
        removed = cur.rowcount
        await db.commit()
    return bool(removed)

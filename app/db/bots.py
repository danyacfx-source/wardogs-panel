"""Интеграции ботов, события, публичные реестры."""

import json
import re

try:
    from .core import _conn, _now
except ImportError:
    from core import _conn, _now


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
        if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
            return False
        raise


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


async def save_public_registry(key, payload_json):
    async with _conn() as database:
        await database.execute(
            "INSERT INTO public_registry (key, payload_json, updated_utc) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET payload_json=excluded.payload_json, "
            "updated_utc=excluded.updated_utc",
            (key, payload_json, _now()),
        )
        await database.commit()


async def load_public_registry(key):
    async with _conn() as database:
        cur = await database.execute(
            "SELECT payload_json, updated_utc FROM public_registry WHERE key = ?",
            (key,),
        )
        row = await cur.fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    return {"payload": payload, "updated_utc": row[1]}

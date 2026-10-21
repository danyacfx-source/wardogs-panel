"""Пользователи, заметки, теги, наказания."""

import secrets

try:
    from .core import _conn, _now
except ImportError:
    from core import _conn, _now


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


async def get_personas(steam_ids):
    """Batch: steam_id -> persona для переданных id (пустые имена отбрасываются)."""
    ids = []
    seen = set()
    for value in steam_ids or []:
        sid = str(value or "").strip()
        if sid and sid not in seen:
            seen.add(sid)
            ids.append(sid)
    if not ids:
        return {}
    placeholders = ", ".join("?" for _ in ids)
    async with _conn() as db:
        cur = await db.execute(
            f"SELECT steam_id, COALESCE(persona, '') FROM users WHERE steam_id IN ({placeholders})",
            ids,
        )
        rows = await cur.fetchall()
    return {row[0]: row[1].strip() for row in rows if (row[1] or "").strip()}


async def list_player_punishments(steam_id, limit=100):
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

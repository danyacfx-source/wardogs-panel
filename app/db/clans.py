"""Кланы."""

import secrets

try:
    from .core import _conn, _now
except ImportError:
    from core import _conn, _now


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

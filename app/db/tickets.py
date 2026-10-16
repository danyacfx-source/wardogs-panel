"""Тикеты поддержки."""

import secrets

try:
    from .core import _conn, _now
except ImportError:
    from core import _conn, _now


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

"""VIP-слоты и заявки на покупку VIP."""

import json
import re
import secrets

try:
    from .core import _conn, _now
except ImportError:
    from core import _conn, _now


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


async def get_vip_order(order_id):
    if not re.fullmatch(r"WD-[0-9A-F]{16}", str(order_id or "")):
        return None
    keys = ('id','buyer_steam_id','server_id','plan_id','amount','months','seats','recipients','status','created_utc','actor_steam_id')
    async with _conn() as database:
        cur = await database.execute('SELECT ' + ','.join(keys) + ' FROM vip_orders WHERE id=?', (order_id,))
        row = await cur.fetchone()
    if not row:
        return None
    item = dict(zip(keys, row))
    item['recipients'] = json.loads(item['recipients'])
    return item


async def mark_vip_order(order_id, status, expected=None):
    query = "UPDATE vip_orders SET status=? WHERE id=?"
    params = [status, order_id]
    if expected:
        if isinstance(expected, (list, tuple, set)):
            query += " AND status IN (" + ",".join("?" for _ in expected) + ")"
            params.extend(expected)
        else:
            query += " AND status=?"
            params.append(expected)
    async with _conn() as database:
        cur = await database.execute(query, tuple(params))
        await database.commit()
        return cur.rowcount > 0

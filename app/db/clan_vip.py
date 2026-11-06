"""VIP клана: пул мест, которые лидер или заместитель раздаёт участникам клана."""

try:
    from .core import _conn, _now
except ImportError:
    from core import _conn, _now

MONTH_SECONDS = 30 * 86400


async def get_clan_pool(clan_id):
    async with _conn() as db:
        cur = await db.execute(
            "SELECT clan_id, seats, expires_utc FROM clan_vip_pools WHERE clan_id = ?", (clan_id,)
        )
        row = await cur.fetchone()
    if not row:
        return None
    return {"clan_id": row[0], "seats": int(row[1] or 0), "expires_utc": float(row[2] or 0)}


async def list_clan_grants(clan_id):
    async with _conn() as db:
        cur = await db.execute(
            "SELECT steam_id, granted_by, created_utc FROM clan_vip_grants WHERE clan_id = ? ORDER BY created_utc",
            (clan_id,),
        )
        rows = await cur.fetchall()
    return [{"steam_id": r[0], "granted_by": r[1] or "", "created_utc": float(r[2])} for r in rows]


async def clan_pool_usage(clan_id):
    """Сколько мест занято: выданные места только у тех, кто ещё состоит в клане."""
    async with _conn() as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM clan_vip_grants g JOIN clan_members m "
            "ON m.clan_id = g.clan_id AND m.steam_id = g.steam_id WHERE g.clan_id = ?",
            (clan_id,),
        )
        row = await cur.fetchone()
    return int(row[0] or 0)


async def add_clan_grant(clan_id, steam_id, granted_by):
    """Выдаёт место одним запросом: проверка мест, членства и повтора внутри INSERT.

    Возвращает "ok" или причину отказа: "no_pool", "expired", "full", "not_member", "exists".
    """
    now = _now()
    async with _conn() as db:
        cur = await db.execute(
            "INSERT INTO clan_vip_grants (clan_id, steam_id, granted_by, created_utc) "
            "SELECT ?, ?, ?, ? "
            "WHERE EXISTS (SELECT 1 FROM clan_members WHERE clan_id = ? AND steam_id = ?) "
            "AND NOT EXISTS (SELECT 1 FROM clan_vip_grants WHERE clan_id = ? AND steam_id = ?) "
            "AND EXISTS (SELECT 1 FROM clan_vip_pools WHERE clan_id = ? AND expires_utc > ? AND seats > ("
            "  SELECT COUNT(*) FROM clan_vip_grants g JOIN clan_members m "
            "  ON m.clan_id = g.clan_id AND m.steam_id = g.steam_id WHERE g.clan_id = ?))",
            (clan_id, steam_id, granted_by, now, clan_id, steam_id, clan_id, steam_id, clan_id, now, clan_id),
        )
        if cur.rowcount == 1:
            await db.commit()
            return "ok"
        await db.rollback()
        pool = await get_clan_pool(clan_id)
        if not pool:
            return "no_pool"
        if pool["expires_utc"] <= now:
            return "expired"
        cur = await db.execute(
            "SELECT 1 FROM clan_members WHERE clan_id = ? AND steam_id = ?", (clan_id, steam_id)
        )
        if not await cur.fetchone():
            return "not_member"
        cur = await db.execute(
            "SELECT 1 FROM clan_vip_grants WHERE clan_id = ? AND steam_id = ?", (clan_id, steam_id)
        )
        if await cur.fetchone():
            return "exists"
        return "full"


async def remove_clan_grant(clan_id, steam_id):
    async with _conn() as db:
        cur = await db.execute(
            "DELETE FROM clan_vip_grants WHERE clan_id = ? AND steam_id = ?", (clan_id, steam_id)
        )
        await db.commit()
    return cur.rowcount > 0


async def credit_clan_pool(clan_id, seats, months=1):
    """Добавляет места и срок. Срок прибавляется к уже действующему, даже если осталось меньше дня."""
    now = _now()
    current = await get_clan_pool(clan_id)
    base = max(now, current["expires_utc"]) if current else now
    total_seats = (current["seats"] if current else 0) + int(seats)
    expires = base + months * MONTH_SECONDS
    async with _conn() as db:
        await db.execute(
            "INSERT INTO clan_vip_pools (clan_id, seats, expires_utc) VALUES (?, ?, ?) "
            "ON CONFLICT(clan_id) DO UPDATE SET seats = excluded.seats, expires_utc = excluded.expires_utc",
            (clan_id, total_seats, expires),
        )
        await db.commit()
    return {"clan_id": clan_id, "seats": total_seats, "expires_utc": expires}


async def create_clan_order(order):
    async with _conn() as db:
        await db.execute(
            "INSERT INTO clan_vip_orders (id, clan_id, buyer_steam_id, product_id, pack_seats, amount, status, created_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (order["id"], order["clan_id"], order["buyer_steam_id"], order["product_id"],
             int(order["pack_seats"]), int(order["amount"]), "awaiting_payment", _now()),
        )
        await db.commit()
    return order["id"]


async def get_clan_order(order_id):
    async with _conn() as db:
        cur = await db.execute(
            "SELECT id, clan_id, buyer_steam_id, product_id, pack_seats, amount, status, created_utc "
            "FROM clan_vip_orders WHERE id = ?",
            (order_id,),
        )
        row = await cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0], "clan_id": row[1], "buyer_steam_id": row[2], "product_id": row[3],
        "pack_seats": int(row[4]), "amount": int(row[5]), "status": row[6], "created_utc": float(row[7]),
    }


async def mark_clan_order_paid(order_id):
    """Переход awaiting_payment -> paid: повторное уведомление ЮMoney ничего не дублирует."""
    async with _conn() as db:
        cur = await db.execute(
            "UPDATE clan_vip_orders SET status = 'paid', paid_utc = ? WHERE id = ? AND status = 'awaiting_payment'",
            (_now(), order_id),
        )
        await db.commit()
    return cur.rowcount == 1


async def list_orphan_clan_grants():
    """Места, которые пора снять: игрок вышел из клана или пул истёк или удалён."""
    now = _now()
    async with _conn() as db:
        cur = await db.execute(
            "SELECT g.clan_id, g.steam_id FROM clan_vip_grants g "
            "LEFT JOIN clan_vip_pools p ON p.clan_id = g.clan_id "
            "LEFT JOIN clan_members m ON m.clan_id = g.clan_id AND m.steam_id = g.steam_id "
            "WHERE m.steam_id IS NULL OR p.clan_id IS NULL OR p.expires_utc <= ?",
            (now,),
        )
        rows = await cur.fetchall()
    return [{"clan_id": r[0], "steam_id": r[1]} for r in rows]


async def reopen_clan_order(order_id):
    """Возврат в awaiting_payment, если зачисление не удалось: повторное уведомление ЮMoney повторит его."""
    async with _conn() as db:
        await db.execute(
            "UPDATE clan_vip_orders SET status = 'awaiting_payment', paid_utc = NULL WHERE id = ? AND status = 'paid'",
            (order_id,),
        )
        await db.commit()


async def list_clan_pools():
    async with _conn() as db:
        cur = await db.execute("SELECT clan_id, seats, expires_utc FROM clan_vip_pools")
        rows = await cur.fetchall()
    return [{"clan_id": r[0], "seats": int(r[1] or 0), "expires_utc": float(r[2] or 0)} for r in rows]

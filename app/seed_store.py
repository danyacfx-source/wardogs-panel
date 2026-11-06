"""Покупка VIP за баллы сидинга: читает wardogs_v2.db бота, цены в site.db."""

import json
import sqlite3
import time
import uuid

try:
    from . import config, db
except ImportError:
    import config
    import db

SEED_DB = config.SEED_DB_PATH

_DEFAULT_SKUS = {
    "1d": {"label": "VIP на 1 день", "cost": 120, "duration": 86_400},
    "7d": {"label": "VIP на 7 дней", "cost": 360, "duration": 604_800},
    "30d": {"label": "VIP на 30 дней", "cost": 720, "duration": 2_592_000},
}


def _seed_conn():
    try:
        from .bot import db_backend
    except ImportError:
        from bot import db_backend
    if db_backend.using_pg():
        return db_backend.open_bot_connection()
    if not SEED_DB:
        return None
    from pathlib import Path
    if not Path(SEED_DB).exists():
        return None
    c = sqlite3.connect(str(SEED_DB), timeout=5)
    c.row_factory = sqlite3.Row
    return c


async def get_shop_config() -> dict:
    """Конфиг магазина: site.db → дефолты."""
    try:
        cur = await db.execute("SELECT sku, label, cost, duration_seconds, enabled FROM seed_shop_config")
        rows = await cur.fetchall()
    except Exception:
        rows = []
    stored = {r["sku"]: r for r in rows}
    result = {}
    for sku, default in _DEFAULT_SKUS.items():
        r = stored.get(sku)
        if r:
            result[sku] = {
                "label": r["label"], "cost": r["cost"],
                "duration": r["duration_seconds"], "enabled": bool(r["enabled"]),
            }
        else:
            result[sku] = {**default, "enabled": True}
    return result


async def save_shop_config(updates: dict) -> dict:
    current = await get_shop_config()
    for sku, patch in updates.items():
        if sku not in current:
            continue
        e = current[sku]
        e["label"] = str(patch.get("label", e["label"]))[:100]
        e["cost"] = max(1, min(1_000_000, int(patch.get("cost", e["cost"]))))
        e["duration"] = max(3600, min(31_536_000, int(patch.get("duration", e["duration"]))))
        e["enabled"] = bool(patch.get("enabled", e["enabled"]))
        await db.execute(
            "INSERT INTO seed_shop_config (sku, label, cost, duration_seconds, enabled) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(sku) DO UPDATE SET label=excluded.label, cost=excluded.cost, "
            "duration_seconds=excluded.duration_seconds, enabled=excluded.enabled",
            (sku, e["label"], e["cost"], e["duration"], 1 if e["enabled"] else 0),
        )
    return await get_shop_config()


def unlink_by_steam(steam_id: str, actor_steam_id: str) -> dict:
    """Отвязка Steam сотрудником из карточки игрока. Баллы остаются на старом SteamID."""
    try:
        from .bot import database as bot_database
    except ImportError:
        from bot import database as bot_database
    user = bot_database.seed_get_user_by_steam(steam_id)
    if not user:
        return {"ok": False, "error": "Этот SteamID не привязан к Discord."}
    try:
        bot_database.seed_unlink_discord(user["discord_id"], actor_id=actor_steam_id, reason="staff_unlink")
    except bot_database.SeedDomainError as e:
        return {"ok": False, "error": e.message}
    return {"ok": True, "discord_id": user["discord_id"]}


def discord_for_steam(steam_id: str) -> str:
    """Discord-ID, привязанный к SteamID через бота сидинга; пусто, если привязки нет."""
    c = _seed_conn()
    if not c:
        return ""
    try:
        row = c.execute(
            "SELECT discord_id FROM seed_users WHERE steam_id = ? AND linked = 1", (str(steam_id),)
        ).fetchone()
        return str(row["discord_id"]) if row else ""
    finally:
        c.close()


def get_balance(discord_id: str) -> dict | None:
    c = _seed_conn()
    if not c:
        return None
    try:
        row = c.execute(
            "SELECT discord_id, steam_id, display_name, balance FROM seed_users WHERE discord_id = ?",
            (discord_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        c.close()


def get_player_summary(steam_id: str, discord_id: str = "") -> dict:
    """Бонусы, время сида и срок кланового VIP из базы бота. Пусто, если базы или записи нет."""
    summary = {"balance": None, "seed_seconds": 0, "clan_vip_until": None, "paid_tier": 0, "paid_until": None}
    c = _seed_conn()
    if not c:
        return summary
    try:
        row = c.execute(
            "SELECT discord_id, balance, total_seconds FROM seed_users WHERE steam_id = ? AND linked = 1",
            (steam_id,),
        ).fetchone()
        if row:
            summary["balance"] = int(row["balance"] or 0)
            summary["seed_seconds"] = int(row["total_seconds"] or 0)
            discord_id = discord_id or str(row["discord_id"] or "")
        if discord_id.isdigit():
            vip = c.execute(
                "SELECT MAX(ends_at) AS ends_at FROM donations_vip "
                "WHERE user_id = ? AND product_id LIKE 'clan_%' AND ends_at > ?",
                (int(discord_id), int(time.time() * 1000)),
            ).fetchone()
            if vip and vip["ends_at"]:
                summary["clan_vip_until"] = int(vip["ends_at"]) / 1000
            state = c.execute("SELECT value FROM seed_system_state WHERE key = 'donation_feed'").fetchone()
            entry = (json.loads(state["value"]).get("vip") or {}).get(discord_id) if state else None
            if entry and int(entry.get("endsAt") or 0) > time.time() * 1000:
                summary["paid_tier"] = int(entry.get("tier") or 1)
                summary["paid_until"] = int(entry["endsAt"]) / 1000
    finally:
        c.close()
    return summary


def paid_vip_steam_ids(discord_to_steam: dict | None = None) -> set[str]:
    """SteamID игроков с действующим VIP, купленным через бота.

    В базе бота покупка записана на Discord ID. SteamID берём из привязок бота
    и из привязок панели (discord_to_steam): игрока без привязки определить нельзя.
    """
    c = _seed_conn()
    if not c:
        return set()
    now_ms = int(time.time() * 1000)
    try:
        rows = c.execute("SELECT user_id FROM donations_vip WHERE ends_at > ?", (now_ms,)).fetchall()
        buyers = {str(row["user_id"]) for row in rows}
        state = c.execute("SELECT value FROM seed_system_state WHERE key = 'donation_feed'").fetchone()
        feed = (json.loads(state["value"]).get("vip") or {}) if state else {}
        buyers |= {discord_id for discord_id, entry in feed.items() if int(entry.get("endsAt") or 0) > now_ms}
        if not buyers:
            return set()
        links = dict(discord_to_steam or {})
        for row in c.execute("SELECT discord_id, steam_id FROM seed_users WHERE linked = 1").fetchall():
            if row["steam_id"]:
                links[str(row["discord_id"])] = str(row["steam_id"])
        return {links[discord_id] for discord_id in buyers if discord_id in links}
    finally:
        c.close()


def purchase_vip(discord_id: str, sku: str, cost: int, duration: int) -> dict:
    c = _seed_conn()
    if not c:
        return {"ok": False, "error": "База сидинга недоступна"}
    try:
        row = c.execute("SELECT id, balance FROM seed_users WHERE discord_id = ?", (discord_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "Привязка Discord↔Steam не найдена. Привяжите Steam кнопкой «Привязать Steam» в канале правил Discord."}
        if row["balance"] < cost:
            return {"ok": False, "error": f"Недостаточно баллов: нужно {cost}, доступно {row['balance']}."}
        new_balance = row["balance"] - cost
        now_ms = int(time.time() * 1000)
        order_id = uuid.uuid4().hex
        c.execute("UPDATE seed_users SET balance = ?, updated_at = ? WHERE id = ?",
                  (new_balance, now_ms, row["id"]))
        c.execute(
            "INSERT INTO seed_point_ledger (user_id, ref, delta, balance_after, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (row["id"], f"vip:{order_id}", -cost, new_balance, f"panel_vip:{sku}", now_ms),
        )
        c.commit()
        return {
            "ok": True,
            "balance": new_balance,
            "cost": cost,
            "duration": duration,
            "sku": sku,
            "ledger_ref": f"vip:{order_id}",
        }
    finally:
        c.close()


def refund_vip(discord_id: str, ledger_ref: str, cost: int) -> bool:
    """Возвращает баллы за покупку VIP, если слоты не удалось создать.

    Использует ledger_ref из purchase_vip для нахождения транзакции;
    не компенсирует, если запись уже отменена (повторный вызов безопасен).
    """
    c = _seed_conn()
    if not c:
        return False
    try:
        row = c.execute(
            "SELECT id, balance FROM seed_users WHERE discord_id = ?",
            (discord_id,),
        ).fetchone()
        if not row:
            return False
        # Проверяем, что транзакция существует и не была ещё возвращена
        existing = c.execute(
            "SELECT delta FROM seed_point_ledger WHERE user_id = ? AND ref = ?",
            (row["id"], ledger_ref),
        ).fetchone()
        if not existing or existing["delta"] >= 0:
            # Либо не найдено, либо уже компенсировано
            return False
        new_balance = row["balance"] + cost
        now_ms = int(time.time() * 1000)
        c.execute(
            "UPDATE seed_users SET balance = ?, updated_at = ? WHERE id = ?",
            (new_balance, now_ms, row["id"]),
        )
        c.execute(
            "INSERT INTO seed_point_ledger (user_id, ref, delta, balance_after, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (row["id"], f"{ledger_ref}:refund", cost, new_balance, "panel_vip_refund", now_ms),
        )
        c.commit()
        return True
    finally:
        c.close()

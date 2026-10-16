"""Покупка VIP за баллы сидинга: читает wardogs_v2.db бота напрямую."""

import sqlite3
import uuid
import time

try:
    from . import config
except ImportError:
    import config

SEED_DB = config.SEED_DB_PATH

# SKU -> (стоимость в баллах, длительность в секундах)
VIP_SKUS = {
    "1d": {"label": "VIP на 1 день", "cost": config.SEED_VIP_1_DAY_COST, "duration": 86_400},
    "7d": {"label": "VIP на 7 дней", "cost": config.SEED_VIP_7_DAY_COST, "duration": 604_800},
    "30d": {"label": "VIP на 30 дней", "cost": config.SEED_VIP_30_DAY_COST, "duration": 2_592_000},
}


def _conn():
    if not SEED_DB or not SEED_DB.exists():
        return None
    c = sqlite3.connect(str(SEED_DB), timeout=5)
    c.row_factory = sqlite3.Row
    return c


def get_balance(discord_id: str) -> dict | None:
    """Баланс seed-пользователя по discord_id. None — нет привязки/БД."""
    c = _conn()
    if not c:
        return None
    try:
        row = c.execute(
            "SELECT discord_id, steam_id, display_name, balance FROM seed_users WHERE discord_id = ?",
            (discord_id,),
        ).fetchone()
        if not row:
            return None
        return dict(row)
    finally:
        c.close()


def purchase_vip(discord_id: str, sku: str) -> dict:
    """Списывает баллы за VIP. Возвращает {ok, balance, error}."""
    if sku not in VIP_SKUS:
        return {"ok": False, "error": "Неизвестный SKU"}
    entry = VIP_SKUS[sku]
    c = _conn()
    if not c:
        return {"ok": False, "error": "База сидинга недоступна"}
    try:
        row = c.execute("SELECT id, balance FROM seed_users WHERE discord_id = ?", (discord_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "Привязка Discord↔Steam не найдена. Используйте /seed link."}
        balance = row["balance"]
        if balance < entry["cost"]:
            return {"ok": False, "error": f"Недостаточно баллов: нужно {entry['cost']}, доступно {balance}."}
        new_balance = balance - entry["cost"]
        now_ms = int(time.time() * 1000)
        order_id = uuid.uuid4().hex
        c.execute("UPDATE seed_users SET balance = ?, updated_at = ? WHERE id = ?",
                  (new_balance, now_ms, row["id"]))
        c.execute(
            "INSERT INTO seed_point_ledger (user_id, ref, delta, balance_after, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (row["id"], f"vip:{order_id}", -entry["cost"], new_balance, f"panel_vip:{sku}", now_ms),
        )
        c.commit()
        return {"ok": True, "balance": new_balance, "cost": entry["cost"], "duration": entry["duration"], "sku": sku}
    finally:
        c.close()

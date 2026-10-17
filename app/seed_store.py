"""Покупка VIP за баллы сидинга: читает wardogs_v2.db бота, цены в site.db."""

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


def purchase_vip(discord_id: str, sku: str, cost: int, duration: int) -> dict:
    c = _seed_conn()
    if not c:
        return {"ok": False, "error": "База сидинга недоступна"}
    try:
        row = c.execute("SELECT id, balance FROM seed_users WHERE discord_id = ?", (discord_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "Привязка Discord↔Steam не найдена. Используйте /seed link."}
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
        return {"ok": True, "balance": new_balance, "cost": cost, "duration": duration, "sku": sku}
    finally:
        c.close()

"""Единый каталог тарифов VIP: цены задаёт владелец, остальные части панели читают их отсюда.

Состав тарифа (уровень, срок, места) определяется его ID и не меняется, поэтому заказ,
созданный по старой цене, остаётся действительным: сумма хранится в подписанном заказе.
"""

import json
import logging
import time

try:
    from . import db
except ImportError:
    import db

log = logging.getLogger("vip_catalog")

REGISTRY_KEY = "vip_catalog"
CACHE_SECONDS = 5.0
MAX_RUB = 1_000_000
MONTHS = (1, 3, 12)
TIERS = (1, 2, 3)
TIER_PERKS = {
    1: "Роль VIP и резервный слот",
    2: "VIP и личный тикет с администрацией",
    3: "VIP, личный тикет и объявление о входе в игре",
}


def _months_label(months):
    return {1: "1 месяц", 3: "3 месяца", 12: "12 месяцев"}[months]


def _personal_id(tier, months):
    # ID тарифов Tier 1 остаются прежними: по ним уже есть оплаченные заказы.
    return f"personal_{months}" if tier == 1 else f"personal_t{tier}_{months}"


def _build_products():
    defaults = {1: 50000, 3: 120000, 12: 480000}
    products = {}
    for tier in TIERS:
        for months in MONTHS:
            product_id = _personal_id(tier, months)
            products[product_id] = {
                "id": product_id, "kind": "personal", "tier": tier, "months": months, "seats": 1,
                "name": f"VIP Tier {tier} · {_months_label(months)}",
                "term": _months_label(months),
                # Цен для новых уровней ещё нет: они выключены, пока владелец их не задаст.
                "default_kopecks": defaults[months] if tier == 1 else None,
            }
    for seats, kopecks in ((10, 450000), (20, 850000), (30, 1200000)):
        product_id = f"clan_{seats}"
        products[product_id] = {
            "id": product_id, "kind": "clan", "tier": 1, "months": 1, "seats": seats,
            "name": f"VIP для клана · {seats} мест · 1 месяц",
            "term": f"{seats} участников · 1 месяц",
            "default_kopecks": kopecks,
        }
    return products


PRODUCTS = _build_products()

_overrides = {}
_loaded_until = 0.0


def reset():
    global _loaded_until
    _overrides.clear()
    _loaded_until = 0.0


def _row(product):
    stored = _overrides.get(product["id"]) or {}
    kopecks = stored.get("kopecks", product["default_kopecks"])
    enabled = bool(stored.get("enabled", product["default_kopecks"] is not None)) and kopecks is not None
    return {
        "id": product["id"], "kind": product["kind"], "tier": product["tier"], "months": product["months"],
        "seats": product["seats"], "name": product["name"], "term": product["term"],
        "rub": kopecks // 100 if kopecks is not None else None, "enabled": enabled,
    }


def items(include_disabled=True):
    rows = [_row(product) for product in PRODUCTS.values()]
    return rows if include_disabled else [row for row in rows if row["enabled"]]


def price_kopecks(product_id):
    """Текущая цена или None, если тарифа нет или он выключен."""
    product = PRODUCTS.get(product_id or "")
    if not product:
        return None
    row = _row(product)
    return row["rub"] * 100 if row["enabled"] else None


def _plan_id(product_id):
    return product_id.replace("_", "-")


def panel_plans(include_disabled=False):
    """Тарифы для заявок из панели."""
    return [
        {
            "id": _plan_id(row["id"]), "name": row["name"], "amount": row["rub"] or 0,
            "months": row["months"], "seats": row["seats"], "tier": row["tier"], "enabled": row["enabled"],
        }
        for row in items(include_disabled)
    ]


def plan_by_id(plan_id):
    return next((plan for plan in panel_plans(True) if plan["id"] == plan_id), None)


def tier_of_plan(plan_id):
    plan = plan_by_id(plan_id)
    return plan["tier"] if plan else 1


async def refresh(force=False):
    """Читает цены из базы. При сбое остаются последние известные, продажи не останавливаются."""
    global _loaded_until
    now = time.monotonic()
    if not force and now < _loaded_until:
        return
    _loaded_until = now + CACHE_SECONDS
    try:
        stored = await db.load_public_registry(REGISTRY_KEY)
    except Exception:
        log.warning("Не удалось прочитать цены VIP", exc_info=True)
        return
    payload = (stored or {}).get("payload") if isinstance(stored, dict) else None
    products = payload.get("products") if isinstance(payload, dict) else None
    _overrides.clear()
    for product_id, entry in (products or {}).items():
        if product_id not in PRODUCTS or not isinstance(entry, dict):
            continue
        kopecks = entry.get("kopecks")
        if kopecks is not None and (type(kopecks) is not int or kopecks <= 0):
            continue
        _overrides[product_id] = {"kopecks": kopecks, "enabled": bool(entry.get("enabled"))}


def _validated(updates):
    if not isinstance(updates, list) or not updates:
        raise ValueError("Нужен список тарифов")
    cleaned = {}
    for entry in updates:
        if not isinstance(entry, dict) or entry.get("id") not in PRODUCTS:
            raise ValueError("Неизвестный тариф")
        name = PRODUCTS[entry["id"]]["name"]
        rub = entry.get("rub")
        enabled = entry.get("enabled")
        if type(enabled) is not bool:
            raise ValueError(f"{name}: не указано, включён ли тариф")
        if rub is None:
            if enabled:
                raise ValueError(f"{name}: чтобы включить тариф, укажите цену")
        elif type(rub) is not int or not 1 <= rub <= MAX_RUB:
            raise ValueError(f"{name}: цена должна быть целым числом от 1 до {MAX_RUB} ₽")
        cleaned[entry["id"]] = {"kopecks": rub * 100 if rub is not None else None, "enabled": enabled}
    return cleaned


async def save(updates):
    """Сохраняет цены. Возвращает список изменений для журнала."""
    cleaned = _validated(updates)
    await refresh(force=True)
    before = {row["id"]: row for row in items()}
    merged = {
        product_id: {
            "kopecks": (before[product_id]["rub"] * 100) if before[product_id]["rub"] is not None else None,
            "enabled": before[product_id]["enabled"],
        }
        for product_id in PRODUCTS
    }
    merged.update(cleaned)
    await db.save_public_registry(REGISTRY_KEY, json.dumps({"products": merged}, ensure_ascii=False))
    await refresh(force=True)
    changes = []
    for row in items():
        old = before[row["id"]]
        if (old["rub"], old["enabled"]) != (row["rub"], row["enabled"]):
            changes.append({
                "id": row["id"], "before": old["rub"] if old["enabled"] else None,
                "after": row["rub"] if row["enabled"] else None,
            })
    return changes

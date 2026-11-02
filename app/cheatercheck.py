"""Клиент публичного API cheatercheck.io: скор, публичные баны и жалобы игрока.

Ключ берётся из CHEATERCHECK_API_KEY. Квота ключа дневная, а неизвестный игрок
сканируется за 10 токенов, поэтому успешные ответы кэшируются в памяти.
"""

import logging
import time

import aiohttp

try:
    from . import config
except ImportError:
    import config

log = logging.getLogger("wardogs.cheatercheck")

API_BASE = "https://cheatercheck.io/api/v1"
SITE_URL = "https://cheatercheck.io"
CACHE_TTL = 30 * 60

_cache: dict[str, tuple[float, dict]] = {}


def _int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _normalize(steam_id, raw):
    """Оставляет только то, что показывает карточка. Остальные поля API панели не нужны."""
    bans = raw.get("bans") or {}
    reports = raw.get("reports") or {}
    score = raw.get("cheatScore") or {}
    last30 = reports.get("last30Days") or {}
    return {
        "steam_id": steam_id,
        "status": raw.get("status") or "ok",
        "score": _number(score.get("score")),
        "scanned_at": raw.get("scannedAt"),
        "vac_bans": _int(bans.get("vacBans")),
        "game_bans": _int(bans.get("gameBans")),
        "economy_ban": bans.get("economyBan") or "none",
        "reports_total": _int(reports.get("total")),
        "reports_30d": _int(last30.get("total")),
        "profile_url": raw.get("profileUrl") or SITE_URL,
    }


async def _get(steam_id, key):
    """Возвращает (статус, тело). Для 404 тело None."""
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(
            f"{API_BASE}/players/{steam_id}",
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        ) as resp:
            if resp.status != 200:
                return resp.status, None
            return resp.status, await resp.json(content_type=None)


async def fetch_player(steam_id):
    """Возвращает {"configured", "reason", "player"}. Ошибки внешнего сервиса не поднимаются наружу."""
    key = config.CHEATERCHECK_API_KEY
    if not key:
        return {"configured": False, "reason": "not_configured", "player": None}
    cached = _cache.get(steam_id)
    if cached and time.monotonic() - cached[0] < CACHE_TTL:
        return cached[1]
    try:
        status, raw = await _get(steam_id, key)
    except (aiohttp.ClientError, OSError, TimeoutError, ValueError) as e:
        log.warning("cheatercheck: сбой запроса для %s: %r", steam_id, e)
        return {"configured": True, "reason": "unavailable", "player": None}
    if status == 404:
        result = {"configured": True, "reason": "not_found", "player": None}
    elif status != 200 or not isinstance(raw, dict):
        if status == 401:
            log.warning("cheatercheck: ключ отклонён (401), проверьте CHEATERCHECK_API_KEY")
        elif status == 429:
            log.warning("cheatercheck: лимит запросов или квота исчерпаны (429)")
        else:
            log.warning("cheatercheck: неожиданный статус %s для %s", status, steam_id)
        return {"configured": True, "reason": "unavailable", "player": None}
    else:
        player = _normalize(steam_id, raw)
        result = {"configured": True, "reason": None, "player": player}
        # Статусы scanning и deferred означают, что данных ещё нет: их не кэшируем.
        if player["status"] not in ("ok", "notFound"):
            return result
    _cache[steam_id] = (time.monotonic(), result)
    return result

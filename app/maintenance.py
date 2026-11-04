"""Закрытие разделов панели и сайта на технические работы (вкладка Dev владельца)."""

import html
import json
import logging
import re
import time

try:
    from . import db
except ImportError:
    import db

log = logging.getLogger("maintenance")

REGISTRY_KEY = "maintenance"
CACHE_SECONDS = 5.0
MAX_MESSAGE_LENGTH = 1000
DEFAULT_MESSAGE = "Раздел временно закрыт на технические работы. Скоро всё заработает."

_SID = r"/api/server/[^/]+"


def _site_page(name, *extra):
    names = (name, *extra)
    return [rf"^/{item}(\.html)?$" for item in names] + [rf"^/static/{name}\.html$"]


# Маршруты входят в секцию, чтобы закрытие работало и для прямых запросов, а не только
# скрывало вкладку. Сюда намеренно не входят маршруты, которыми пользуются машины:
# вебхук и фид донатов, оплата по ссылкам бота, приём килфида, API ботов, синхронизация
# кланов, публичные JSON-фиды и проверки /api/health и /api/ready.
#
# paths — страницы и маршруты, которые открыты только владельцу.
# data  — публичные данные страницы: закрыты для посетителей, но не для сотрудников,
#         потому что панель работает на тех же данных (например, overview).
SECTIONS = [
    {"id": "status", "label": "Мониторинг", "group": "panel", "paths": []},
    {"id": "clans", "label": "Кланы", "group": "panel", "paths": [r"^/api/clans(/|$)", r"^/api/players/[^/]+/clan$"]},
    {"id": "players", "label": "Игроки", "group": "panel", "paths": [
        r"^/api/players(/|$)", r"^/api/stats/players$", rf"^{_SID}/players(/|$)", rf"^{_SID}/broadcast$",
    ]},
    {"id": "matches", "label": "Матчи", "group": "panel", "paths": [r"^/api/matches$"]},
    {"id": "killfeed", "label": "Killfeed", "group": "panel", "paths": [r"^/api/killfeed(/status)?$"], "methods": {"GET"}},
    {"id": "controls", "label": "Карта / Матч", "group": "panel", "paths": [
        rf"^{_SID}/match(/|$)", rf"^{_SID}/world(/|$)", rf"^{_SID}/catalog$",
    ]},
    {"id": "rotation", "label": "Ротация", "group": "panel", "paths": [rf"^{_SID}/rotation$"]},
    {"id": "bans", "label": "Баны", "group": "panel", "paths": [rf"^{_SID}/bans(/|$)"]},
    {"id": "reserved", "label": "Резервные слоты", "group": "panel", "paths": [rf"^{_SID}/reserved-slots(/|$)"]},
    {"id": "vip", "label": "VIP-слоты", "group": "panel", "paths": [rf"^{_SID}/vip-slots(/|$)", r"^/api/vip/orders(/|$)"]},
    {"id": "tickets", "label": "Тикеты", "group": "panel", "paths": [r"^/api/tickets(/|$)", r"^/api/ticket-types(/|$)"]},
    {"id": "audit", "label": "Аудит", "group": "panel", "paths": [rf"^{_SID}/audit$"]},
    {"id": "journal", "label": "Журнал", "group": "panel", "paths": [r"^/api/audit(/|$)"]},
    {"id": "config", "label": "Конфиг", "group": "panel", "paths": [
        rf"^{_SID}/config$", rf"^{_SID}/routes$", r"^/api/config/seed-shop$",
    ]},
    {"id": "staff", "label": "Администрация", "group": "panel", "paths": [r"^/api/staff$"]},
    {"id": "roles", "label": "Права ролей", "group": "panel", "paths": [r"^/api/roles(/|$)"]},
    {"id": "site_home", "label": "Главная и мониторинг", "group": "site", "page": True,
     "paths": [r"^/$", r"^/index\.html$", r"^/static/index\.html$"], "data": [rf"^{_SID}/overview$"]},
    {"id": "site_stats", "label": "Статистика", "group": "site", "page": True, "paths": _site_page("stats"),
     "data": [r"^/api/stats/population$"]},
    {"id": "site_calc", "label": "Калькулятор", "group": "site", "page": True, "paths": [
        *_site_page("calc"), r"^/(static/)?calc-app(/|$)", r"^/calc-assets/",
    ]},
    {"id": "site_activity", "label": "Активность", "group": "site", "page": True, "paths": [
        *_site_page("activity"), r"^/activity/top$",
    ]},
    {"id": "site_clans", "label": "Кланы", "group": "site", "page": True, "paths": _site_page("clans"),
     "data": [r"^/api/public/clans(/|$)", r"^/api/public/clan-applications$"]},
    {"id": "site_community", "label": "Сообщество", "group": "site", "page": True, "paths": _site_page("community")},
    {"id": "site_support", "label": "Поддержать", "group": "site", "page": True, "paths": _site_page("support"),
     "data": [r"^/api/donate/checkout$", r"^/api/public/vip-catalog$"]},
]
SECTIONS_BY_ID = {section["id"]: section for section in SECTIONS}
_COMPILED = [
    (
        section,
        [re.compile(pattern) for pattern in section["paths"]],
        [re.compile(pattern) for pattern in section.get("data", [])],
    )
    for section in SECTIONS
]

_cache = {"expires": 0.0, "closed": {}}


def reset_cache():
    _cache["expires"] = 0.0
    _cache["closed"] = {}


def _clean_entry(entry):
    if not isinstance(entry, dict):
        return None
    until = entry.get("until_utc")
    return {
        "message": str(entry.get("message") or DEFAULT_MESSAGE)[:MAX_MESSAGE_LENGTH],
        "until_utc": float(until) if isinstance(until, (int, float)) and not isinstance(until, bool) else None,
        "closed_utc": entry.get("closed_utc"),
    }


async def closed_sections():
    """Закрытые разделы. Сбой чтения не закрывает панель: остаётся последнее известное состояние."""
    now = time.monotonic()
    if now < _cache["expires"]:
        return _cache["closed"]
    _cache["expires"] = now + CACHE_SECONDS
    try:
        stored = await db.load_public_registry(REGISTRY_KEY)
    except Exception:
        log.warning("Не удалось прочитать состояние техработ", exc_info=True)
        return _cache["closed"]
    payload = (stored or {}).get("payload") if isinstance(stored, dict) else None
    sections = payload.get("sections") if isinstance(payload, dict) else None
    closed = {}
    for section_id, entry in (sections or {}).items():
        cleaned = _clean_entry(entry)
        if section_id in SECTIONS_BY_ID and cleaned:
            closed[section_id] = cleaned
    _cache["closed"] = closed
    return closed


async def set_section(section_id, enabled, message="", until_utc=None):
    closed = dict(await closed_sections())
    if enabled:
        previous = closed.get(section_id) or {}
        closed[section_id] = {
            "message": (message or "").strip()[:MAX_MESSAGE_LENGTH] or DEFAULT_MESSAGE,
            "until_utc": until_utc,
            "closed_utc": previous.get("closed_utc") or time.time(),
        }
    else:
        closed.pop(section_id, None)
    await db.save_public_registry(REGISTRY_KEY, json.dumps({"sections": closed}, ensure_ascii=False))
    _cache["closed"] = closed
    _cache["expires"] = time.monotonic() + CACHE_SECONDS
    return closed


async def closed_section_for(path, method):
    """Раздел, закрытый для этого запроса: (section, entry, is_data) или None."""
    for section, patterns, data_patterns in _COMPILED:
        if section.get("methods") and method not in section["methods"]:
            continue
        is_data = any(pattern.match(path) for pattern in data_patterns)
        if is_data or any(pattern.match(path) for pattern in patterns):
            entry = (await closed_sections()).get(section["id"])
            if entry:
                return section, entry, is_data
    return None


def _page_actions(section, me):
    """На закрытой главной нет обычного входа, поэтому сотрудникам нужен свой путь в панель."""
    if section["id"] != "site_home":
        return '<a class="btn ghost" href="/">На главную</a>'
    if me and me.get("permissions"):
        return '<a class="btn primary" href="/panel">Панель управления</a>'
    if not me:
        return '<a class="btn ghost" href="/api/auth/steam/start">Вход для администрации</a>'
    if not (me.get("discord") or {}).get("bound"):
        return '<a class="btn ghost" href="/api/auth/discord/start">Привязать Discord</a>'
    return ""


def page_html(section, entry, me=None):
    label = html.escape(section["label"])
    message = html.escape(entry["message"])
    until = ""
    if entry.get("until_utc"):
        moment = time.strftime("%d.%m.%Y %H:%M", time.gmtime(entry["until_utc"]))
        until = f'<p class="maintenance-until">Планируемое окончание: {moment} UTC</p>'
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>WARDOGS // {label}: технические работы</title>"
        '<link rel="stylesheet" href="/static/style.css"></head><body>'
        '<main class="maintenance-page"><section class="panel maintenance-screen" role="status">'
        '<div class="admin-path">ТЕХНИЧЕСКИЕ РАБОТЫ</div>'
        f"<h1>{label}</h1>"
        f'<p class="maintenance-text">{message}</p>{until}'
        f"<p>{_page_actions(section, me)}</p>"
        "</section></main></body></html>"
    )

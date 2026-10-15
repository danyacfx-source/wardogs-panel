"""Логи банов/випок из панели в Discord-канал (от имени бота панели).

Отправка фоновая и best-effort: сбой Discord не должен ломать действие в панели.
Канал задаётся WARDOGS_LOG_CHANNEL_ID; без него и без токена всё — no-op.
"""

import asyncio
import logging
from datetime import datetime, timezone

import aiohttp

try:
    from . import config
except ImportError:
    import config

log = logging.getLogger("disclog")
API = "https://discord.com/api/v10"

EVENTS = {
    "ban.add": ("Бан выдан", 0xE74C3C),
    "ban.remove": ("Бан снят", 0x2ECC71),
    "ban.update": ("Бан обновлён", 0xE67E22),
    "ban.bulk.remove": ("Снятие банов (пакетно)", 0x2ECC71),
    "ban.expire": ("Бан истёк", 0x95A5A6),
    "vip.save": ("VIP выдан", 0xF1C40F),
    "vip.delete": ("VIP удалён", 0xE67E22),
    "vip.sync": ("VIP синхронизирован", 0x95A5A6),
    "vip.expire": ("VIP истёк", 0x95A5A6),
    "vip.order.paid": ("VIP-заказ оплачен", 0xF1C40F),
}

_tasks = set()


def _fire(coro):
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _post(payload):
    headers = {"Authorization": f"Bot {config.DISCORD_BOT_TOKEN}"}
    url = f"{API}/channels/{config.DISCORD_LOG_CHANNEL_ID}/messages"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status >= 400:
                    log.warning("Лог-канал Discord ответил HTTP %s", resp.status)
    except Exception:
        log.exception("Не удалось отправить лог в Discord")


def emit(event, *, actor=None, server="", steam_id="", detail=""):
    """Ставит embed события в очередь Discord-лога (no-op без настройки)."""
    if not (config.DISCORD_LOG_CHANNEL_ID and config.DISCORD_BOT_TOKEN):
        return
    title, color = EVENTS.get(event, (event, 0x5865F2))
    fields = []
    if steam_id:
        fields.append({
            "name": "SteamID",
            "value": f"[{steam_id}](https://steamcommunity.com/profiles/{steam_id})",
            "inline": True,
        })
    if server:
        fields.append({"name": "Сервер", "value": str(server)[:100], "inline": True})
    if actor:
        fields.append({"name": "Кто", "value": str(actor)[:100], "inline": True})
    if detail:
        fields.append({"name": "Детали", "value": str(detail)[:500], "inline": False})
    embed = {
        "title": title,
        "color": color,
        "fields": fields or [{"name": "Событие", "value": event, "inline": False}],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "WARDOGS панель"},
    }
    _fire(_post({"embeds": [embed], "allowed_mentions": {"parse": []}}))

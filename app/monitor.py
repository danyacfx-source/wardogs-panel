"""Фоновая проверка доступности серверов и необязательные webhook-уведомления."""

import asyncio
import logging

import aiohttp

try:
    from . import config, db
    from .rcon_client import RCONError
except ImportError:
    import config, db
    from rcon_client import RCONError

log = logging.getLogger("monitor")


async def _notify(text):
    if not config.ALERT_WEBHOOK_URL:
        return
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
            async with session.post(config.ALERT_WEBHOOK_URL, json={"content": text}) as response:
                if response.status >= 400:
                    log.warning("Webhook уведомлений ответил HTTP %s", response.status)
    except Exception:
        log.exception("Не удалось отправить webhook уведомление")


async def check_servers(pool):
    """Возвращает состояния и уведомляет только о переходах online/offline."""
    async def check_one(srv):
        previous = await db.get_server_health(srv.id)
        try:
            await srv.api.health()
            online, detail = True, ""
        except RCONError as exc:
            online, detail = False, f"{exc.code}: {exc.message}"
        except Exception as exc:
            online, detail = False, type(exc).__name__
        changed = previous is not None and previous["online"] != online
        await db.set_server_health(srv.id, online, detail)
        if changed:
            state = "в сети" if online else "недоступен"
            await db.log_site_audit(None, srv.id, "health.transition", state)
            await _notify(f"WARDOGS: сервер {srv.name} — {state}.")
        return {"id": srv.id, "ok": online, "detail": detail if not online else None}

    # A dead RCON endpoint must not block checks for every other server.
    return await asyncio.gather(*(check_one(srv) for srv in pool.values()))


async def health_loop(pool, stop_event):
    while not stop_event.is_set():
        try:
            await check_servers(pool)
        except Exception:
            log.exception("Ошибка фоновой проверки серверов")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=config.HEALTHCHECK_INTERVAL)
        except asyncio.TimeoutError:
            pass

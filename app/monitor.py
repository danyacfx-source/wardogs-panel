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
            # Имя сервера из панели не должно пинговать всех: упоминания отключены явно.
            payload = {"content": text, "allowed_mentions": {"parse": []}}
            async with session.post(config.ALERT_WEBHOOK_URL, json=payload) as response:
                if response.status >= 400:
                    log.warning("Webhook уведомлений ответил HTTP %s", response.status)
    except Exception:
        log.exception("Не удалось отправить webhook уведомление")


async def check_servers(pool):
    """Возвращает состояния и уведомляет только о переходах online/offline."""
    async def check_one(srv):
        try:
            return await _check_one_safe(srv)
        except Exception:
            # Сбой одного сервера (в том числе в БД) не должен терять результаты остальных.
            log.exception("Проверка сервера %s завершилась сбоем", srv.id)
            return {"id": srv.id, "ok": False, "detail": "check_failed"}

    async def _check_one_safe(srv):
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
        if online:
            try:
                st = await srv.api.status()
                status_players = (st or {}).get("players") or {}
                try:
                    pl = await srv.api.players()
                except RCONError:
                    # Список не получен: не пишем пустой срез в историю, иначе появятся ложные нули.
                    pl = None
                player_list = ((pl or {}).get("players") or []) if pl is not None else None
                if player_list is not None:
                    await db.record_monitoring(
                        srv.id,
                        status_players.get("current", len(player_list)),
                        status_players.get("max", 0),
                        player_list,
                    )
            except Exception:
                log.exception("Не удалось записать срез мониторинга для %s", srv.id)
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

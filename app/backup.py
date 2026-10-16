"""Бэкап и восстановление site.db через Discord-канал.

При обновлении на Bothost контейнер пересоздаётся и /app/data может быть
утерян. Этот модуль отправляет копию БД в Discord-канал и восстанавливает
её при старте, если БД пуста или отсутствует.
"""

import asyncio
import logging
import time
from pathlib import Path

import aiohttp

try:
    from . import config
except ImportError:
    import config

log = logging.getLogger("backup")

DISCORD_API = "https://discord.com/api/v10"
_DB_PREFIX = "site_"


def _enabled() -> bool:
    return bool(config.DISCORD_BOT_TOKEN and config.BACKUP_CHANNEL_ID)


async def send_backup(db_path: Path | None = None) -> bool:
    """Отправляет site.db в Discord-канал. Возвращает True при успехе."""
    if not _enabled():
        log.debug("Бэкап пропущен: нет токена или канала")
        return False

    path = db_path or config.DB_PATH
    if not path.exists():
        log.warning("БД не найдена: %s", path)
        return False

    data = path.read_bytes()
    if len(data) > 24 * 1024 * 1024:
        log.warning("БД слишком большая для Discord (%d байт)", len(data))
        return False

    ts = time.strftime("%Y%m%d_%H%M%S")
    filename = f"{_DB_PREFIX}{ts}.db"

    form = aiohttp.FormData()
    form.add_field(
        "payload_json",
        '{"content":"📦 Авто-бэкап site.db","flags":64}',
    )
    form.add_field(
        "files[0]", data, filename=filename, content_type="application/octet-stream"
    )

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{DISCORD_API}/channels/{config.BACKUP_CHANNEL_ID}/messages",
                headers={"Authorization": f"Bot {config.DISCORD_BOT_TOKEN}"},
                data=form,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status in (200, 201):
                    log.info("Бэкап отправлен: %s (%d байт)", filename, len(data))
                    await cleanup_old(session)
                    return True
                body = await resp.text()
                log.error("Discord %d: %s", resp.status, body[:200])
    except Exception as e:
        log.error("Ошибка отправки бэкапа: %s", e)
    return False


async def restore_backup(db_path: Path | None = None) -> bool:
    """Восстанавливает site.db из последнего бэкапа в Discord-канале.

    Не затирает уже существующую непустую БД — защита от отката к старому
    бэкапу после того, как контейнер уже отработал с актуальными данными.
    """
    if not _enabled():
        return False

    path = db_path or config.DB_PATH
    if path.exists() and path.stat().st_size > 1024:
        log.info(
            "БД уже на месте (%d байт в %s) — восстановление не требуется",
            path.stat().st_size,
            path,
        )
        return False

    bot_id = await _bot_user_id()
    try:
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bot {config.DISCORD_BOT_TOKEN}"}
            async with session.get(
                f"{DISCORD_API}/channels/{config.BACKUP_CHANNEL_ID}/messages",
                headers=headers,
                params={"limit": 50},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    log.warning("Не удалось прочитать канал бэкапов: %d", resp.status)
                    return False
                messages = await resp.json()

            for msg in messages:
                author_id = str(msg.get("author", {}).get("id", ""))
                if bot_id and author_id != str(bot_id):
                    continue
                for att in msg.get("attachments", []):
                    fname = att.get("filename", "")
                    if not fname.startswith(_DB_PREFIX) or not fname.endswith(".db"):
                        continue
                    url = att.get("url")
                    if not url:
                        continue
                    async with session.get(
                        url, timeout=aiohttp.ClientTimeout(total=120)
                    ) as dl:
                        if dl.status != 200:
                            continue
                        data = await dl.read()
                        if len(data) < 1024:
                            continue
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(data)
                        log.info(
                            "БД восстановлена из бэкапа: %s (%d байт)",
                            fname,
                            len(data),
                        )
                        return True
        log.warning("В канале бэкапов не найдено site_*.db")
    except Exception as e:
        log.error("Ошибка восстановления БД: %s", e)
    return False


async def cleanup_old(session: aiohttp.ClientSession | None = None) -> None:
    """Удаляет из канала бэкапы, выходящие за BACKUP_KEEP."""
    if not _enabled():
        return
    bot_id = await _bot_user_id()
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        headers = {"Authorization": f"Bot {config.DISCORD_BOT_TOKEN}"}
        async with session.get(
            f"{DISCORD_API}/channels/{config.BACKUP_CHANNEL_ID}/messages",
            headers=headers,
            params={"limit": 50},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                return
            messages = await resp.json()

        backups = []
        for msg in messages:
            author_id = str(msg.get("author", {}).get("id", ""))
            if bot_id and author_id != str(bot_id):
                continue
            for att in msg.get("attachments", []):
                if att.get("filename", "").startswith(_DB_PREFIX) and att[
                    "filename"
                ].endswith(".db"):
                    backups.append((msg["id"], att.get("id")))
                    break

        excess = len(backups) - config.BACKUP_KEEP
        if excess <= 0:
            return
        for msg_id, _att_id in backups[:excess]:
            async with session.delete(
                f"{DISCORD_API}/channels/{config.BACKUP_CHANNEL_ID}/messages/{msg_id}",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 204:
                    log.info("Удалён старый бэкап msg=%s", msg_id)
    except Exception as e:
        log.debug("Чистка старых бэкапов: %s", e)
    finally:
        if own_session:
            await session.close()


async def backup_loop(stop_event: asyncio.Event) -> None:
    """Периодический авто-бэкап каждые BACKUP_INTERVAL_SECONDS."""
    # Первый бэкап через 2 минуты после старта, затем по интервалу.
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=120)
        return
    except asyncio.TimeoutError:
        pass
    while not stop_event.is_set():
        try:
            await send_backup()
        except Exception:
            log.exception("Ошибка периодического бэкапа")
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=config.BACKUP_INTERVAL_SECONDS
            )
        except asyncio.TimeoutError:
            pass


_bot_id_cache = 0


async def _bot_user_id() -> int:
    global _bot_id_cache
    if _bot_id_cache:
        return _bot_id_cache
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{DISCORD_API}/users/@me",
                headers={"Authorization": f"Bot {config.DISCORD_BOT_TOKEN}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    _bot_id_cache = int(data["id"])
    except Exception:
        pass
    return _bot_id_cache

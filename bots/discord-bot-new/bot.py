import asyncio
import logging
import os
import sys
import uuid

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import discord
from discord.ext import commands

import config
from database import (
    init_db,
    acquire_lease,
    renew_lease,
    release_lease,
    migrate_giveaways_from,
    automatic_backup,
    close_all_connections,
    db_file_exists,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("bot")

# Единая метка процесса — проставляется в footer каждого лог-сообщения,
# чтобы по дублям видеть, из одного экземпляра они или из разных.
RUN_ID = uuid.uuid4().hex[:6]

bot = commands.Bot(
    command_prefix="!",
    intents=config.intents,
    reconnect=True,
    help_command=None,
)


async def _lease_heartbeat():
    try:
        await bot.wait_until_ready()
    except Exception:
        return
    while True:
        renew_lease(RUN_ID)
        await asyncio.sleep(10)


async def _restore_db_from_channel() -> bool:
    """Если на локальном диске БД пропала (обнова пересоздала папку),
    вытаскивает последний бэкап wardogs_v2.*.db из резервного канала.
    Возвращает True, если БД восстановлена."""
    ch = bot.get_channel(config.BACKUP_CHANNEL_ID)
    if ch is None:
        return False
    try:
        async for msg in ch.history(limit=30):
            if msg.author.id != bot.user.id:
                continue
            for att in msg.attachments:
                if not att.filename.startswith("wardogs_v2.") or not att.filename.endswith(".db"):
                    continue
                data = await att.read()
                close_all_connections()
                with open("wardogs_v2.db", "wb") as f:
                    f.write(data)
                log.info("БД восстановлена из канала: %s (%d байт)", att.filename, len(data))
                return True
        log.warning("В канале %s не найдено бэкапов БД для восстановления", config.BACKUP_CHANNEL_ID)
    except Exception as e:
        log.error("Ошибка восстановления БД из канала: %s", e)
    return False


@bot.event
async def on_ready():
    global _fresh_db
    log.info("%s запущен. Гильдий: %s (run_id=%s)", bot.user, len(bot.guilds), RUN_ID)
    # БД была пустой (хостинг пересоздал папку) — возвращаем данные из канала.
    if _fresh_db and config.BACKUP_CHANNEL_ID:
        if await _restore_db_from_channel():
            _fresh_db = False
            log.info("БД восстановлена из резервного канала")
            # Кнопки розыгрышей регистрируются при загрузке кога (по пустой БД).
            # После восстановления БД — пере-регистрируем, иначе кнопки не отвечают.
            try:
                cog = bot.get_cog("GiveawayCog")
                if cog and hasattr(cog, "reload_views"):
                    await cog.reload_views()
            except Exception as e:
                log.error("Ошибка пере-регистрации кнопок розыгрыша: %s", e)
    # Стартовый бэкап делаем СЕЙЧАС: БД уже точно существует и наполнена,
    # а отправку делаем через fetch, чтобы канал гарантированно нашёлся.
    try:
        bak = automatic_backup()
        if bak:
            log.info("Стартовый авто-бэкап БД создан: %s", bak)
            await _send_backup_to_channel(bak)
    except Exception as e:
        log.error("Ошибка стартового авто-бэкапа БД: %s", e)
    # Периодические бэкапы каждые 6-8 часов.
    bot.loop.create_task(_periodic_backup_loop())
    # Persistent-кнопки (timeout=None) после рестарта нужно перерегистрировать,
    # иначе Discord присылает нажатие, а обработчика нет => «не ответило вовремя».
    try:
        from cogs.tickets import TicketClosedView, TicketCloseView, TicketPanelView
        bot.add_view(TicketPanelView())
        bot.add_view(TicketCloseView())
        bot.add_view(TicketClosedView())
        log.info("Persistent-кнопки тикетов перерегистрированы")
    except Exception as e:
        log.error("Ошибка регистрации persistent-кнопок тикетов: %s", e)
    try:
        from cogs.clan_tickets import (
            ClanTicketClosedView,
            ClanTicketCloseView,
            ClanTicketPanelView,
        )
        bot.add_view(ClanTicketPanelView())
        bot.add_view(ClanTicketCloseView())
        bot.add_view(ClanTicketClosedView())
        log.info("Persistent-кнопки клановых тикетов перерегистрированы")
    except Exception as e:
        log.error("Ошибка регистрации persistent-кнопок кланов: %s", e)
    try:
        from cogs.temp_voice import VoiceControlPanelView
        bot.add_view(VoiceControlPanelView())
        log.info("Persistent-кнопки панели войсов перерегистрированы")
    except Exception as e:
        log.error("Ошибка регистрации persistent-кнопок войсов: %s", e)
    try:
        from cogs.embed import EmbedBuilderView
        bot.add_view(EmbedBuilderView())
        log.info("Persistent-кнопки конструктора эмбеда перерегистрированы")
    except Exception as e:
        log.error("Ошибка регистрации persistent-кнопок эмбеда: %s", e)
    # Глобальный sync — команды видны и на серверах, и в ЛС бота.
    # Серверные команды (guild_only) не показываются в ЛС автоматически.
    try:
        await bot.tree.sync()
        log.info("Слэш-команды синхронизированы глобально")
    except Exception as e:
        log.error("Ошибка глобального синка команд: %s", e)


COGS = [
    "cogs.translate",
    "cogs.temp_voice",
    "cogs.tickets",
    "cogs.announce",
    "cogs.levels",
    "cogs.activity_roles",
    "cogs.streamers",
    "cogs.mirror",
    "cogs.backgrounds",
    "cogs.logging",
    "cogs.automod",
    "cogs.server_sync",
    "cogs.giveaway",
    "cogs.clan_tickets",
    "cogs.embed",
    "cogs.trap_channel",
]


def _migrate_v1_giveaways():
    """Перенос активных розыгрышей из БД v1 (если найдена рядом).
    Позволяет идущему розыгрышу не отвалиться при переезде бота на v2."""
    import glob
    import os
    own = os.path.abspath("wardogs_v2.db")
    candidates = set()
    for pat in ("*.db", "../*.db", "бот WARDOGS/*.db", "**/бот WARDOGS/*.db"):
        try:
            candidates.update(glob.glob(pat, recursive=True))
        except Exception:
            pass
    for path in candidates:
        if not path or os.path.abspath(path) == own:
            continue
        try:
            moved = migrate_giveaways_from(path)
            if moved:
                log.info("Перенесено активных розыгрышей из %s: %s", path, moved)
        except Exception as e:
            log.error("Ошибка миграции розыгрышей из %s: %s", path, e)


async def _send_backup_to_channel(path: str) -> bool:
    """Отправка копии БД в резервный канал (если BACKUP_CHANNEL_ID задан).
    Возвращает True при успехе."""
    import os
    if not path:
        return False
    if not config.BACKUP_CHANNEL_ID:
        log.warning("BACKUP_CHANNEL_ID не задан — бэкап не отправлен в Discord (файл остался в backups/): %s", path)
        return False
    try:
        ch = bot.get_channel(config.BACKUP_CHANNEL_ID)
        if ch is None:
            ch = await bot.fetch_channel(config.BACKUP_CHANNEL_ID)
    except Exception as e:
        log.error("Не удалось получить резервный канал %s: %s", config.BACKUP_CHANNEL_ID, e)
        return False
    try:
        with open(path, "rb") as f:
            await ch.send(
                content="Авто-бэкап БД",
                file=discord.File(f, filename=os.path.basename(path)),
            )
        log.info("Бэкап БД отправлен в канал %s: %s", config.BACKUP_CHANNEL_ID, path)
        return True
    except Exception as e:
        log.error("Ошибка отправки бэкапа в канал %s: %s", config.BACKUP_CHANNEL_ID, e)
        return False


_fresh_db = False


async def _periodic_backup_loop():
    """Бэкап БД каждые 6-8 часов (случайный интервал), страховка от потери данных."""
    import random
    await asyncio.sleep(random.randint(6 * 3600, 8 * 3600))
    while True:
        try:
            bak = automatic_backup()
            if bak:
                log.info("Периодический авто-бэкап БД создан: %s", bak)
                await _send_backup_to_channel(bak)
        except Exception as e:
            log.error("Ошибка периодического авто-бэкапа: %s", e)
        await asyncio.sleep(random.randint(6 * 3600, 8 * 3600))


async def main():
    # Хостинг мог пересоздать папку (кнопка «обновить с GitHub») и стереть БД.
    # Восстановление из канала делается в on_ready (там клиент уже подключён),
    # но факт «свежей пустой БД» фиксируем до init_db, чтобы не потерять сигнал.
    global _fresh_db
    _fresh_db = not db_file_exists()

    init_db()
    _migrate_v1_giveaways()

    # Единственный инстанс: если лисcp держит другой живой процесс — выходим.
    if not acquire_lease(RUN_ID):
        log.warning("Второй инстанс бота уже работает (run_id другой) — завершаюсь.")
        return

    tg = config.TICKET_GUILD_ID
    if not tg:
        log.warning("TICKET_GUILD_ID не задан — тикеты работать не будут")

    try:
        async with bot:
            bot.run_id = RUN_ID
            for cog in COGS:
                try:
                    await bot.load_extension(cog)
                    log.info("Загружен ког: %s", cog)
                except Exception as e:
                    log.exception("Ошибка загрузки %s: %s", cog, e)
            # Сердцебиение лисцпа, чтобы живой бот не терял владение.
            bot.loop.create_task(_lease_heartbeat())
            await bot.start(config.TOKEN)
    finally:
        release_lease(RUN_ID)
        log.info("Лисцпа освобождён (run_id=%s)", RUN_ID)


if __name__ == "__main__":
    asyncio.run(main())
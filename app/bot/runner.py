"""Запуск Discord-бота как фоновой задачи FastAPI-панели.

Вместо отдельного процесса bot.py бот работает внутри панели:
один процесс, один .env, одна точка входа.
"""

import asyncio
import logging
import sys
import uuid
from pathlib import Path

# Cogs и support-модули бота используют плоские импорты
# (import config, from database import ..., from cogs.xxx import ...).
# Добавляем каталог app/bot в начало sys.path, чтобы они резолвились.
_BOT_DIR = str(Path(__file__).resolve().parent)
if _BOT_DIR not in sys.path:
    sys.path.insert(0, _BOT_DIR)

import discord  # noqa: E402
from discord.ext import commands  # noqa: E402

from . import config  # noqa: E402

# Cogs делают flat `import config` — регистрируем наш модуль под тем же именем.
sys.modules.setdefault("config", config)

log = logging.getLogger("bot")

RUN_ID = uuid.uuid4().hex[:6]

bot = commands.Bot(
    command_prefix="!",
    intents=config.intents,
    reconnect=True,
    help_command=None,
)

_fresh_db = False


@bot.event
async def on_error(event_method, *args, **kwargs):
    log.error("Unhandled error in %s", event_method, exc_info=True)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("Недостаточно прав для этой команды.")
        return
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"Подождите {error.retry_after:.1f}s.")
        return
    log.error("Command error in %s: %s", ctx.command, error, exc_info=True)
    try:
        await ctx.send("Ошибка при выполнении команды. Попробуйте позже.")
    except Exception:
        pass


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: Exception):
    original = getattr(error, "original", error)
    log.error(
        "App command error in %s (user=%s): %s",
        getattr(interaction.command, "qualified_name", interaction.data.get("custom_id", "?") if interaction.data else "?"),
        interaction.user,
        original,
        exc_info=original,
    )
    message = "Произошла ошибка. Попробуйте позже."
    if isinstance(original, discord.Forbidden):
        message = "Недостаточно прав для этого действия."
    elif isinstance(original, discord.NotFound):
        message = "Объект не найден (возможно, был удалён)."
    elif isinstance(original, commands.CheckFailure):
        return
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except Exception:
        pass


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
    "cogs.embed",
    "cogs.trap_channel",
    "cogs.donation",
    "cogs.clans",
    "cogs.clan_tickets",
    "cogs.seeding",
    "cogs.swap",
]


async def _restore_db_from_channel() -> bool:
    if not config.BACKUP_CHANNEL_ID:
        return False
    import database
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
                database.close_all_connections()
                with open(database.DB_FILE, "wb") as f:
                    f.write(data)
                log.info("БД восстановлена из канала: %s (%d байт)", att.filename, len(data))
                return True
    except Exception as e:
        log.error("Ошибка восстановления БД из канала: %s", e)
    return False


async def _send_backup_to_channel(path: str) -> bool:
    import os
    if not path or not config.BACKUP_CHANNEL_ID:
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
        log.error("Ошибка отправки бэкапа: %s", e)
        return False


@bot.event
async def on_ready():
    global _fresh_db
    log.info("%s запущен. Гильдий: %s (run_id=%s)", bot.user, len(bot.guilds), RUN_ID)
    if _fresh_db and config.BACKUP_CHANNEL_ID:
        if await _restore_db_from_channel():
            _fresh_db = False
            log.info("БД восстановлена из резервного канала")
            try:
                cog = bot.get_cog("GiveawayCog")
                if cog and hasattr(cog, "reload_views"):
                    await cog.reload_views()
            except Exception as e:
                log.error("Ошибка пере-регистрации кнопок розыгрыша: %s", e)
    try:
        import database
        bak = database.automatic_backup()
        if bak:
            log.info("Стартовый авто-бэкап БД создан: %s", bak)
            await _send_backup_to_channel(bak)
    except Exception as e:
        log.error("Ошибка стартового авто-бэкапа БД: %s", e)
    # Persistent-кнопки
    for mod_name, view_names in [
        ("cogs.tickets", ["TicketPanelView", "TicketCloseView", "TicketClosedView"]),
        ("cogs.temp_voice", ["VoiceControlPanelView"]),
        ("cogs.embed", ["EmbedBuilderView"]),
    ]:
        try:
            mod = __import__(mod_name, fromlist=view_names)
            for vn in view_names:
                cls = getattr(mod, vn, None)
                if cls:
                    bot.add_view(cls())
            log.info("Persistent-кнопки %s перерегистрированы", mod_name)
        except Exception as e:
            log.error("Ошибка регистрации persistent-кнопок %s: %s", mod_name, e)
    try:
        await bot.tree.sync()
        log.info("Слэш-команды синхронизированы глобально")
    except Exception as e:
        log.error("Ошибка глобального синка команд: %s", e)


async def _lease_heartbeat():
    import database
    try:
        await bot.wait_until_ready()
    except Exception:
        return
    while True:
        database.renew_lease(RUN_ID)
        await asyncio.sleep(10)


async def _periodic_backup_loop():
    import random
    import database
    await asyncio.sleep(random.randint(6 * 3600, 8 * 3600))
    while True:
        try:
            bak = database.automatic_backup()
            if bak:
                log.info("Периодический авто-бэкап БД создан: %s", bak)
                await _send_backup_to_channel(bak)
        except Exception as e:
            log.error("Ошибка периодического авто-бэкапа: %s", e)
        await asyncio.sleep(random.randint(6 * 3600, 8 * 3600))


async def start_bot():
    """Точка входа — вызывается из main.py как asyncio-задача."""
    import database

    _fresh = not database.db_file_exists()
    database.init_db()

    if not database.acquire_lease(RUN_ID):
        log.warning("Второй инстанс бота уже работает — бот не запущен.")
        return

    global _fresh_db
    _fresh_db = _fresh

    if not config.TOKEN:
        log.warning("DISCORD_TOKEN не задан — бот не запущен")
        database.release_lease(RUN_ID)
        return

    try:
        for cog in COGS:
            loaded = False
            for attempt in range(3):
                try:
                    await bot.load_extension(cog)
                    log.info("Загружен ког: %s", cog)
                    loaded = True
                    break
                except Exception as e:
                    log.warning("Ошибка загрузки %s (попытка %d/3): %s", cog, attempt + 1, e)
                    if attempt < 2:
                        await asyncio.sleep(2.0 * (attempt + 1))
            if not loaded:
                log.error("Ког %s не загрузился после 3 попыток — пропускаю", cog)
        bot.loop.create_task(_lease_heartbeat())
        bot.loop.create_task(_periodic_backup_loop())
        await bot.start(config.TOKEN)
    except asyncio.CancelledError:
        log.info("Бот остановлен (CancelledError)")
        await bot.close()
    finally:
        database.release_lease(RUN_ID)
        log.info("Лисцпа освобождён (run_id=%s)", RUN_ID)

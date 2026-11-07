"""/db-export: выгрузка базы в резервный канал (вручную администратором и по расписанию DB_EXPORT_HOURS)."""
from __future__ import annotations

import asyncio
import io
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from database import seed_get_state, seed_set_state
from db_dump import ExportError, build_archives, export_due

log = logging.getLogger(__name__)
AUTO_STATE_KEY = "db_export_last_ts"


class DbExportCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._running = False

    async def cog_load(self):
        if config.DB_EXPORT_HOURS > 0 and not self.auto_export_loop.is_running():
            self.auto_export_loop.start()

    async def cog_unload(self):
        if self.auto_export_loop.is_running():
            self.auto_export_loop.cancel()

    async def _resolve_channel(self):
        channel = self.bot.get_channel(config.BACKUP_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(config.BACKUP_CHANNEL_ID)
            except Exception:
                channel = None
        return channel if isinstance(channel, discord.TextChannel) else None

    async def _send_archives(self, channel: discord.TextChannel, archives: list, caption: str) -> None:
        total = len(archives)
        # Каждая часть — отдельное сообщение: лимит вложений Discord считается на одно сообщение.
        for index, (filename, data) in enumerate(archives, start=1):
            text = caption + (f" · часть {index} из {total}" if total > 1 else "")
            await channel.send(
                content=text,
                file=discord.File(io.BytesIO(data), filename=filename),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            log.info("db_export_sent file=%s bytes=%d part=%d/%d", filename, len(data), index, total)

    @app_commands.command(name="db-export", description="Выгрузить базу данных в резервный канал")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def db_export(self, interaction: discord.Interaction):
        member = interaction.user
        if not isinstance(member, discord.Member) or not member.guild_permissions.administrator:
            await interaction.response.send_message("Выгрузка доступна только администраторам сервера.", ephemeral=True)
            return
        if not config.BACKUP_CHANNEL_ID:
            await interaction.response.send_message("Резервный канал не настроен (WARDOGS_BACKUP_CHANNEL_ID).", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            archives = await asyncio.to_thread(build_archives)
        except ExportError as e:
            await interaction.followup.send(f"Выгрузка не выполнена: {e}", ephemeral=True)
            return
        except Exception as e:
            log.exception("db_export_failed: %s", e)
            await interaction.followup.send("Выгрузка не выполнена: ошибка чтения базы. Подробности в журнале.", ephemeral=True)
            return
        channel = await self._resolve_channel()
        if channel is None:
            await interaction.followup.send("Резервный канал не найден или это не текстовый канал.", ephemeral=True)
            return
        await self._send_archives(channel, archives, f"📦 Выгрузка базы по запросу {member.mention}")
        names = ", ".join(f"`{name}`" for name, _ in archives)
        await interaction.followup.send(f"Выгрузка отправлена в резервный канал ({len(archives)} шт.): {names}", ephemeral=True)

    @tasks.loop(minutes=30)
    async def auto_export_loop(self):
        if self._running:
            return
        now = time.time()
        if not export_due(seed_get_state(AUTO_STATE_KEY, 0), now, config.DB_EXPORT_HOURS):
            return
        self._running = True
        try:
            # Отметка ставится до выгрузки: при ошибке не будем повторять каждые 30 минут.
            seed_set_state(AUTO_STATE_KEY, now)
            archives = await asyncio.to_thread(build_archives)
            channel = await self._resolve_channel()
            if channel is None:
                log.error("db_export_auto: резервный канал не найден")
                return
            await self._send_archives(channel, archives, "📦 Автовыгрузка базы")
        except ExportError as e:
            log.error("db_export_auto_failed: %s", e)
        except Exception as e:
            log.exception("db_export_auto_failed: %s", e)
        finally:
            self._running = False

    @auto_export_loop.before_loop
    async def _before_auto_export(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(120)


async def setup(bot: commands.Bot):
    await bot.add_cog(DbExportCog(bot))

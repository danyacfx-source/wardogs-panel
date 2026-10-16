import sys
import os
import logging
from datetime import datetime, timezone as dt_timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from database import (
    get_streamers,
    add_streamer,
    remove_streamer,
    set_streamer_live,
    streamer_live_state,
)
from twitch_api import fetch_stream, fetch_viewer_count, fetch_next_start, TwitchError

log = logging.getLogger("streamers")
LIVE_COLOR = discord.Color(0x9146FF)
OFFLINE_COLOR = discord.Color(0x5865F2)

# ID владельца бота: в ЛС разрешаем управление только ему (модель прав без ролей).
_OWNER_ID = 635370416252125184


def _can_manage_streamers(interaction: discord.Interaction) -> bool:
    """Право добавлять/удалять стримеров. На сервере — manage_guild, в ЛС — владелец."""
    if interaction.guild is not None:
        return interaction.user.guild_permissions.manage_guild
    return interaction.user.id == _OWNER_ID


class StreamersCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._live_messages: dict[str, tuple[int, int]] = {}
        self.twitch_loop.start()

    def cog_unload(self):
        self.twitch_loop.cancel()

    @tasks.loop(seconds=config.TWITCH_CHECK_INTERVAL)
    async def twitch_loop(self):
        try:
            await self._tick()
        except TwitchError as e:
            log.warning("Twitch API: %s", e)
        except Exception as e:
            log.warning("Twitch tick: %s", e)

    @twitch_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()
        # Мигрируем логины из .env в БД (добавляем только новые).
        for login in config.TWITCH_CHANNELS:
            try:
                add_streamer(login, 0)
            except Exception:
                pass

        channel = self.bot.get_channel(config.TWITCH_ANNOUNCE_CHANNEL)
        for login in get_streamers():
            try:
                stream = await fetch_stream(login)
            except TwitchError:
                continue
            set_streamer_live(login, stream.is_live)
            if stream.is_live and channel is not None:
                try:
                    msg = await channel.send(embed=self._live_embed(stream))
                    self._live_messages[login] = (channel.id, msg.id)
                except discord.HTTPException as e:
                    log.warning("Не смог отправить стартовое: %s", e)
        log.info("Стримеры инициализированы: %s", get_streamers())

    async def _get_msg(self, login: str) -> discord.Message | None:
        ref = self._live_messages.get(login)
        if not ref:
            return None
        channel_id, msg_id = ref
        channel = self.bot.get_channel(channel_id)
        if not channel:
            return None
        try:
            return await channel.fetch_message(msg_id)
        except discord.NotFound:
            self._live_messages.pop(login, None)
            return None

    async def _tick(self):
        channel = self.bot.get_channel(config.TWITCH_ANNOUNCE_CHANNEL)
        if channel is None:
            log.warning("TWITCH_ANNOUNCE_CHANNEL не найден (%s)", config.TWITCH_ANNOUNCE_CHANNEL)
            return

        for login in get_streamers():
            try:
                stream = await fetch_stream(login)
            except TwitchError:
                continue

            was_live = streamer_live_state(login)
            msg = await self._get_msg(login)

            if stream.is_live and not was_live:
                new_msg = await channel.send(embed=self._live_embed(stream))
                self._live_messages[login] = (channel.id, new_msg.id)
                set_streamer_live(login, True)
            elif stream.is_live and was_live and msg is not None:
                try:
                    await msg.edit(embed=self._live_embed(stream))
                except discord.NotFound:
                    self._live_messages.pop(login, None)
            elif not stream.is_live and was_live:
                if msg is not None:
                    # Меняем живой эмбед на «завершён» вместо отправки нового сообщения.
                    try:
                        await msg.edit(embed=self._offline_embed(stream))
                    except discord.NotFound:
                        self._live_messages.pop(login, None)
                        await channel.send(embed=self._offline_embed(stream))
                else:
                    await channel.send(embed=self._offline_embed(stream))
                self._live_messages.pop(login, None)
                set_streamer_live(login, False)

    def _primary_login(self) -> str | None:
        """Основной стример для /stream: сначала стартовые из конфига."""
        if config.TWITCH_CHANNELS:
            return config.TWITCH_CHANNELS[0]
        streamers = get_streamers()
        return streamers[0] if streamers else None

    @staticmethod
    def _schedule_text(start_dt) -> str:
        now = datetime.now(dt_timezone.utc)
        seconds = int((start_dt - now).total_seconds())
        if seconds <= 0:
            return "Эфир скоро!"
        days, rem = divmod(seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes = rem // 60
        if days > 0:
            return f"Эфир через {days} д {hours} ч"
        if hours > 0:
            return f"Эфир через {hours} ч {minutes} мин"
        return f"Эфир через {max(1, minutes)} мин"

    def _live_embed(self, stream) -> discord.Embed:
        embed = discord.Embed(
            title="🔴 Прямо в эфире!",
            description=f"**{stream.stream_title}**" if stream.stream_title else None,
            url=stream.url,
            color=LIVE_COLOR,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(
            name=stream.display_name,
            url=stream.url,
            icon_url=stream.avatar or None,
        )
        embed.add_field(name="🎮 Игра", value=stream.game, inline=True)
        embed.add_field(name="👥 Зрители", value=f"**{stream.viewers}**", inline=True)
        if stream.thumbnail:
            embed.set_image(url=stream.thumbnail)
        embed.set_footer(text=f"Twitch • {stream.login}")
        return embed

    def _offline_embed(self, stream) -> discord.Embed:
        embed = discord.Embed(
            title="Стрим завершён",
            description=f"[{stream.display_name}]({stream.url}) закончил(а) трансляцию.",
            color=OFFLINE_COLOR,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(
            name=stream.display_name,
            url=stream.url,
            icon_url=stream.avatar or None,
        )
        embed.set_footer(text=f"Twitch • {stream.login}")
        return embed

    # ---------- Команды (без правки кода) ----------

    @app_commands.command(name="add_streamer", description="Добавить Twitch-канал стримера для анонсов")
    @app_commands.describe(login="Логин канала на Twitch (например: f_a_n_e)")
    async def add_streamer_cmd(self, interaction: discord.Interaction, login: str):
        if not _can_manage_streamers(interaction):
            return await interaction.response.send_message("❌ Нет прав.", ephemeral=True)

        login = login.strip().lower().lstrip("@")
        if not login or any(c.isspace() for c in login):
            return await interaction.response.send_message("❌ Неверный логин.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        try:
            stream = await fetch_stream(login)
        except TwitchError as e:
            return await interaction.followup.send(f"❌ Не удалось проверить канал: {e}", ephemeral=True)
        if not stream.login:
            return await interaction.followup.send("❌ Канал не найден на Twitch.", ephemeral=True)

        added = add_streamer(stream.login, interaction.user.id)
        if added:
            set_streamer_live(stream.login, stream.is_live)
            await interaction.followup.send(f"✅ Стример **{stream.login}** добавлен!", ephemeral=True)
        else:
            await interaction.followup.send(f"ℹ️ **{stream.login}** уже в списке.", ephemeral=True)

    @app_commands.command(name="remove_streamer", description="Убрать Twitch-канал из анонсов")
    @app_commands.describe(login="Логин на Twitch")
    async def remove_streamer_cmd(self, interaction: discord.Interaction, login: str):
        if not _can_manage_streamers(interaction):
            return await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
        login = login.strip().lower().lstrip("@")
        removed = remove_streamer(login)
        if removed:
            await interaction.response.send_message(f"✅ Стример **{login}** удалён.", ephemeral=True)
        else:
            await interaction.response.send_message(f"ℹ️ **{login}** не в списке.", ephemeral=True)

    @app_commands.command(name="streamers", description="Список отслеживаемых стримеров")
    async def streamers_list(self, interaction: discord.Interaction):
        logins = get_streamers()
        if not logins:
            return await interaction.response.send_message("📭 Нет стримеров.", ephemeral=True)
        lines = []
        for login in logins:
            icon = "🔴" if streamer_live_state(login) else "⚫"
            lines.append(f"{icon} **{login}**")
        await interaction.response.send_message(
            f"**Стримеры ({len(logins)}):**\n" + "\n".join(lines), ephemeral=True
        )

    @app_commands.command(name="stream", description="Статус стрима Twitch: зрители, игра, расписание")
    async def stream_status(self, interaction: discord.Interaction):
        login = self._primary_login()
        if not login:
            return await interaction.response.send_message(
                "Модуль статуса стрима отключён в конфиге.",
                ephemeral=True,
            )
        await interaction.response.defer()
        embed = discord.Embed(title="📺 Статус стрима", color=LIVE_COLOR)
        try:
            viewers = await fetch_viewer_count(login)
            stream = await fetch_stream(login)
            if viewers is not None:
                title = stream.stream_title or "Стрим идёт"
                embed.description = f"**{title}**"
                embed.add_field(name="Статус", value="🔴 В эфире", inline=True)
                embed.add_field(name="Зрители", value=str(viewers), inline=True)
                if stream.game and stream.game != "Без категории":
                    embed.add_field(name="Игра", value=stream.game, inline=True)
                thumb = (stream.thumbnail or "").replace("{width}", "1280").replace("{height}", "720")
                if thumb:
                    embed.set_image(url=thumb)
                embed.add_field(name="Ссылка", value=stream.url, inline=False)
            else:
                embed.description = "Сейчас эфира нет."
                embed.add_field(name="Статус", value="⚪ Офлайн", inline=True)
                next_start = await fetch_next_start(login)
                embed.add_field(
                    name="Ближайший эфир",
                    value=self._schedule_text(next_start) if next_start else "Не запланирован",
                    inline=True,
                )
        except TwitchError as e:
            log.warning("Twitch /stream: %s", e)
            embed.description = f"⚠️ Не удалось получить статус стрима: {e}"
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(StreamersCog(bot))
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
import discord
from discord import app_commands
from discord.ext import commands

from database import (
    clear_announce_channel,
    get_announce_channel,
    set_announce_channel,
)

log = logging.getLogger("announce")


class GuildSelectView(discord.ui.View):
    def __init__(self, guilds: list[discord.Guild]):
        super().__init__(timeout=120)
        options = [
            discord.SelectOption(label=g.name, value=str(g.id))
            for g in guilds[:25]
        ]
        select = discord.ui.Select(placeholder="Выберите сервер...", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        guild_id = int(interaction.data["values"][0])
        guild = interaction.client.get_guild(guild_id)
        await interaction.response.send_message(
            f"**Шаг 2:** Выберите канал в **{guild.name}**:",
            view=ChannelSelectView(guild),
        )


class ChannelSelectView(discord.ui.View):
    CHANNELS_PER_PAGE = 24

    def __init__(self, guild: discord.Guild, page: int = 0):
        super().__init__(timeout=120)
        self.guild = guild
        self.channels = list(guild.text_channels)
        if self.channels:
            self.page = page
            self._pages = max(1, -(-len(self.channels) // self.CHANNELS_PER_PAGE))
        else:
            self.page = 0
            self._pages = 1
        self._rebuild()

    def _rebuild(self):
        self.clear_items()
        options = []
        if self.channels:
            start = self.page * self.CHANNELS_PER_PAGE
            chunk = self.channels[start : start + self.CHANNELS_PER_PAGE]
            options = [
                discord.SelectOption(label=f"#{ch.name}", value=str(ch.id))
                for ch in chunk
            ]
            select = discord.ui.Select(placeholder="Выберите канал...", options=options)
        else:
            select = discord.ui.Select(
                placeholder="Нет каналов на этом сервере",
                options=[discord.SelectOption(label="Нет каналов", value="none")],
                disabled=True,
            )
        select.callback = self._on_select
        self.add_item(select)

        prev = discord.ui.Button(
            label="◀",
            style=discord.ButtonStyle.secondary,
            disabled=(self.page == 0),
        )
        prev.callback = self._on_prev
        self.add_item(prev)

        counter = discord.ui.Button(
            label=f"{self.page + 1} / {self._pages}",
            style=discord.ButtonStyle.secondary,
            disabled=True,
        )
        self.add_item(counter)

        nxt = discord.ui.Button(
            label="▶",
            style=discord.ButtonStyle.secondary,
            disabled=(self.page >= self._pages - 1),
        )
        nxt.callback = self._on_next
        self.add_item(nxt)

    async def _on_prev(self, interaction: discord.Interaction):
        if self.page > 0:
            self.page -= 1
        self._rebuild()
        await interaction.response.edit_message(view=self)

    async def _on_next(self, interaction: discord.Interaction):
        if self.page < self._pages - 1:
            self.page += 1
        self._rebuild()
        await interaction.response.edit_message(view=self)

    async def _on_select(self, interaction: discord.Interaction):
        channel_id = int(interaction.data["values"][0])
        # В ЛС interaction.guild = None, поэтому берём гильдию из view (self.guild).
        channel = self.guild.get_channel(channel_id)
        if channel is None:
            return await interaction.response.send_message("❌ Канал не найден.", ephemeral=True)
        # Запоминаем канал в БД: шаг с текстом/картинкой переживает рестарт бота.
        set_announce_channel(interaction.user.id, channel.id, self.guild.id)
        await interaction.response.send_message(
            f"**Шаг 3:** Напишите **текст** объявления в этот личный чат.\n"
            f"📍 Канал: **#{channel.name}**\n"
            f"После текста бот попросит прислать картинку.\n"
            f"Начните с `-`, чтобы пропустить текст и сразу перейти к картинке."
        )


class AnnounceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Чат-мастер: user_id -> {"channel_id", "guild_id", "phase": "text"|"image", "text"}
        self._state: dict[int, dict] = {}

    def _admin_guilds(self, user_id: int) -> list[discord.Guild]:
        out = []
        for guild in self.bot.guilds:
            member = guild.get_member(user_id)
            if not member:
                continue
            if member.guild_permissions.manage_guild or member.guild_permissions.administrator:
                out.append(guild)
        return out

    async def _finish(self, message: discord.Message, st: dict, attachments: list[discord.Attachment]):
        """Публикуем готовое объявление (текст + файлы) и сбрасываем мастер."""
        channel = None
        guild = self.bot.get_guild(st["guild_id"])
        if guild:
            channel = guild.get_channel(st["channel_id"])
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(st["channel_id"])
            except Exception:
                channel = None
        if channel is None:
            clear_announce_channel(message.author.id)
            self._state.pop(message.author.id, None)
            return await message.channel.send(
                "❌ Канал не найден. Начните заново: напишите «старт» в личку бота."
            )

        if not st["text"] and not attachments:
            clear_announce_channel(message.author.id)
            self._state.pop(message.author.id, None)
            return await message.channel.send(
                "❌ Объявление пустое: и текст, и картинка пропущены. Отправка отменена."
            )

        files = [await a.to_file() for a in attachments]
        await channel.send(content=st["text"], files=files if files else None)
        clear_announce_channel(message.author.id)
        self._state.pop(message.author.id, None)
        await message.channel.send(f"✅ Отправлено в **#{channel.name}**.")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not isinstance(message.channel, discord.DMChannel) or message.author.bot:
            return
        text = message.content.strip()
        if text.lower() in ("старт", "start", "/start"):
            guilds = self._admin_guilds(message.author.id)
            if not guilds:
                return await message.channel.send("❌ Нет прав администратора ни на одном сервере!")
            self._state.pop(message.author.id, None)
            clear_announce_channel(message.author.id)
            return await message.channel.send(
                "⚙️ **Панель отправки сообщений**\n**Шаг 1:** Выберите сервер:",
                view=GuildSelectView(guilds),
            )

        # Продолжение мастера. Если state потерян (рестарт), берём канал из БД.
        st = self._state.get(message.author.id)
        if st is None:
            saved = get_announce_channel(message.author.id)
            if not saved:
                return
            st = {
                "channel_id": saved["channel_id"],
                "guild_id": saved["guild_id"],
                "phase": "text",
                "text": None,
            }
            self._state[message.author.id] = st

        if st["phase"] == "text":
            if text == "-":
                st["text"] = None
                if message.attachments:
                    return await self._finish(message, st, list(message.attachments))
                st["phase"] = "image"
                return await message.channel.send(
                    "🖼️ **Шаг 4:** Пришлите **картинку/фото** для объявления (файлом)\n"
                    "или отправьте `-`, чтобы отправить без картинки."
                )
            if not text:
                return await message.channel.send(
                    "🗒️ Сначала напишите **текст** объявления (`-` — пропустить текст)."
                )
            st["text"] = text
            if message.attachments:
                return await self._finish(message, st, list(message.attachments))
            st["phase"] = "image"
            return await message.channel.send(
                "🖼️ **Шаг 4:** Пришлите **картинку/фото** для объявления (файлом)\n"
                "или отправьте `-`, чтобы отправить без картинки."
            )

        # phase == "image": ждём картинку; "-" = отправить без картинки.
        if text == "-":
            await self._finish(message, st, [])
            return
        if not message.attachments:
            return await message.channel.send(
                "🖼️ Пришлите картинку/фото файлом или отправьте `-`, чтобы отправить без картинки."
            )
        await self._finish(message, st, list(message.attachments))

    @app_commands.command(name="announce", description="Отправить объявление от имени бота в канал")
    @app_commands.describe(channel="Канал (по умолчанию текущий)", text="Текст объявления")
    @app_commands.guild_only()
    async def announce(self, interaction: discord.Interaction, channel: discord.TextChannel = None, text: str = None):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
        target = channel or interaction.channel
        if not text:
            return await interaction.response.send_message("❌ Укажите текст объявления.", ephemeral=True)
        await target.send(f"🐺 **WARDOGS**\n\n{text}")
        await interaction.response.send_message(f"✅ Отправлено в {target.mention}.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AnnounceCog(bot))
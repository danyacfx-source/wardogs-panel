import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord
from discord import app_commands
from discord.ext import commands

import config
from database import get_mirrors, add_mirror, remove_mirror, get_mirror_dests


def _safe_truncate(text: str, limit: int) -> str:
    """Обрезает текст, не разрывая суррогатную пару (эмодзи) на границе."""
    if text is None or len(text) <= limit:
        return text
    clipped = text[:limit]
    while clipped and 0xD800 <= ord(clipped[-1]) <= 0xDBFF:
        clipped = clipped[:-1]
    return clipped


class MirrorCog(commands.Cog):
    """Пункт 6: отслеживание чужого канала (например разработчиков)
    и дублирование новых сообщений в наши каналы."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---- Разрешения ----

    def _can_manage(self, member: discord.Member) -> bool:
        if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
            return True
        return member.id in config.MIRROR_ADD_COMMAND_USERS

    # ---- Листенер ----

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if not message.guild:
            return

        dests = get_mirror_dests(message.guild.id, message.channel.id)
        if not dests:
            return

        # Не зеркалим собственные пересланные сообщения повторно.
        if getattr(message, "_mirrored", False):
            return
        if message.author.id == self.bot.user.id:
            return
        # Не копируем сообщения, пришедшие из этого же зеркала (защита от циклов).
        if message.type == discord.MessageType.default and message.reference:
            if message.reference.resolved and self.bot.user in getattr(message.reference.resolved, "mentions", []):
                return

        # Собираем данные для копирования.
        embeds = message.embeds[:1]  # одна встроенная карточка
        files = []
        for att in message.attachments[:5]:
            files.append(await att.to_file())
        content = message.content
        approved_mentions = discord.AllowedMentions.none()

        for dest_id in dests:
            dest = self.bot.get_channel(dest_id)
            if dest is None:
                continue
            try:
                safe_content = _safe_truncate(content, 2000)
                sent = await dest.send(
                    content=safe_content if safe_content else None,
                    embeds=embeds,
                    files=files,
                    allowed_mentions=approved_mentions,
                )
                # Гасим повторное зеркалирование копии (в т.ч. если копия окажется источником).
                if sent:
                    sent._mirrored = True
            except Exception as e:
                import logging
                logging.getLogger("mirror").warning(
                    "Ошибка зеркалирования %s -> %s: %s", message.channel, dest, e
                )

    # ---- Команды ----

    mirror_group = app_commands.Group(name="mirror", description="Зеркало каналов (пункт 6)")

    @mirror_group.command(name="add", description="Добавить источник зеркала (канал на этом сервере)")
    @app_commands.describe(
        guild_id="ID сервера-источника (например сервер разработчиков)",
        channel_id="ID канала-источника в том сервере",
        dest="Канал назначения на ЭТОМ сервере",
        title="Название зеркала (необязательно)",
    )
    @app_commands.guild_only()
    async def mirror_add(
        self,
        interaction: discord.Interaction,
        guild_id: str,
        channel_id: str,
        dest: discord.TextChannel,
        title: str = "",
    ):
        if not self._can_manage(interaction.user):
            return await interaction.response.send_message("❌ Нет прав.", ephemeral=True)

        try:
            sgid = int(guild_id)
            scid = int(channel_id)
        except ValueError:
            return await interaction.response.send_message("❌ ID должны быть числами.", ephemeral=True)

        added = add_mirror(sgid, scid, [dest.id], title, interaction.user.id)
        if added:
            await interaction.response.send_message(
                f"✅ Зеркало добавлено: `<{sgid}>/{scid}` → **#{dest.name}**"
                f"{' («' + title + '»)' if title else ''}",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message("ℹ️ Такое зеркало уже есть.", ephemeral=True)

    @mirror_group.command(name="list", description="Список зеркал (на этом сервере)")
    @app_commands.guild_only()
    async def mirror_list(self, interaction: discord.Interaction):
        if not self._can_manage(interaction.user):
            return await interaction.response.send_message("❌ Нет прав.", ephemeral=True)

        rows = get_mirrors()
        if not rows:
            return await interaction.response.send_message("📭 Зеркал нет.", ephemeral=True)

        lines = []
        for m in rows:
            src = m["source_guild_id"]
            ch = m["source_channel_id"]
            dests = m["dest_channels"]
            title = m["title"] or f"{src}/{ch}"
            lines.append(f"**#{m['id']}** «{title}» → каналы `{dests}`")
        await interaction.response.send_message(
            "**Зеркала:**\n" + "\n".join(lines), ephemeral=True
        )

    @mirror_group.command(name="remove", description="Удалить зеркало по ID (см. /mirror list)")
    @app_commands.describe(id="ID зеркала")
    @app_commands.guild_only()
    async def mirror_remove(self, interaction: discord.Interaction, id: int):
        if not self._can_manage(interaction.user):
            return await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
        removed = remove_mirror(id)
        if removed:
            await interaction.response.send_message(f"✅ Зеркало #{id} удалено.", ephemeral=True)
        else:
            await interaction.response.send_message(f"ℹ️ Зеркало #{id} не найдено.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(MirrorCog(bot))
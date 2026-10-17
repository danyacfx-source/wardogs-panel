import time
import discord
from discord import app_commands
from discord.ext import commands

import config
from translator import async_translate_text


# Контекст-меню регистрируется на уровне модуля: discord.py 2.7+ не даёт
# определять context_menu внутри класса кога.
@app_commands.context_menu(name="Перевести")
async def translate_context(interaction: discord.Interaction, message: discord.Message):
    text = _message_text(message)
    if not text:
        await interaction.response.send_message("❌ Сообщение без текста.", ephemeral=True)
        return

    await interaction.response.defer()
    translated = await async_translate_text(text, config.TRANSLATE_TARGET)
    if not translated:
        await interaction.followup.send("❌ Не удалось перевести.", ephemeral=True)
        return

    files = _attachment_files(message)
    if files:
        await interaction.followup.send(f"**Перевод:**\n{translated}", files=files)
    else:
        await interaction.followup.send(f"**Перевод:**\n{translated}")


def _message_text(message: discord.Message) -> str:
    """Весь текст сообщения с сохранением структуры: content + все embed'ы."""
    parts: list[str] = []

    if message.content and message.content.strip():
        parts.append(message.content.strip())

    for emb in message.embeds:
        emb_parts: list[str] = []
        if emb.title:
            emb_parts.append(f"**{emb.title}**")
        if emb.description:
            emb_parts.append(emb.description.strip())
        for field in emb.fields:
            if field.name or field.value:
                emb_parts.append(f"**{field.name}**: {field.value}".strip())
        if emb_parts:
            parts.append("\n\n".join(emb_parts))

    return "\n\n".join(parts).strip()


def _attachment_files(message: discord.Message) -> list[discord.Attachment]:
    """Вложения сообщения, чтобы пересылать вместе с переводом без потерь."""
    return list(message.attachments)[:10]


class TranslateCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # ID собственных сообщений-переводов: по ним не переводим повторно.
        self._sent_ids: set[int] = set()
        self._sent_ts: dict[int, float] = {}

    def _remember(self, msg_id: int):
        self._sent_ids.add(msg_id)
        self._sent_ts[msg_id] = time.monotonic()
        # Чистим старые записи, чтобы множество не разрасталось.
        now = time.monotonic()
        stale = [mid for mid, ts in self._sent_ts.items() if now - ts > 3600]
        for mid in stale:
            self._sent_ids.discard(mid)
            self._sent_ts.pop(mid, None)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Автоперевод только в перечисленных каналах.
        if message.channel.id not in config.TRANSLATE_CHANNELS:
            return

        # Своё же сообщение-перевод не переводим повторно, чтобы не зациклиться.
        # Всё остальное (включая сообщения бота/вебхука в канале) — переводим.
        if message.id in self._sent_ids:
            return

        text = _message_text(message)
        if not text:
            return

        translated = await async_translate_text(text, config.TRANSLATE_TARGET)
        if not translated:
            return
        # Сообщение уже на языке назначения — дублировать не нужно.
        if translated.strip().lower() == text.strip().lower():
            return

        files = _attachment_files(message)
        if files:
            sent = await message.channel.send(translated, files=files)
        else:
            sent = await message.channel.send(translated)
        if sent:
            self._remember(sent.id)


async def setup(bot: commands.Bot):
    bot.tree.add_command(translate_context)
    await bot.add_cog(TranslateCog(bot))
import logging
import traceback
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from database import (
    get_log_channel,
    set_log_channel,
    get_all_log_channels,
    remove_log_channel,
    make_log_fingerprint,
    try_claim_log,
)

log = logging.getLogger("logging")

LOG_TYPES = {
    "messages": "Удаление/редактирование сообщений",
    "moderation": "Модерация / вход-выход / бан / кик",
    "roles": "Изменение ролей и прав",
    "channels": "Изменение каналов",
    "voice": "Голосовые каналы",
    "joins": "Вход/выход участников",
    "tickets": "Логи тикетов",
    "errors": "Логи любых ошибок",
}

COLORS = {
    "messages": discord.Color.red(),
    "moderation": discord.Color.orange(),
    "roles": discord.Color.blurple(),
    "channels": discord.Color.teal(),
    "voice": discord.Color.dark_teal(),
    "joins": discord.Color.green(),
    "tickets": discord.Color.gold(),
    "errors": discord.Color.dark_red(),
}

# Каналы логов по умолчанию (хардкод). Если канал для типа не задан через
# /logs set — лог уходит сюда. Приоритет: /logs set > БД > этот словарь.
DEFAULT_LOG_CHANNELS = {
    "tickets": 1545113128625246310,
    "errors": 1546556382566678528,
    "voice": 1546556434421129327,
    "joins": 1546556497398468618,
    "moderation": 1546556555036721252,
    "messages": 1546556589157388359,
}


class LoggingCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._voice_dedup = {}
        self._VOICE_DEDUP_SECONDS = 6
        # run_id процесса задан в bot.py (bot.run_id) и единый для всех когов.
        self.run_id = getattr(bot, "run_id", "?")
        log.info("Логгер запущен, run_id=%s", self.run_id)

    # ---------- Хелперы ----------

    def _channel_for(self, guild: discord.Guild, log_type: str) -> discord.TextChannel | None:
        if guild is None:
            return None
        cid = get_log_channel(log_type) or DEFAULT_LOG_CHANNELS.get(log_type)
        if not cid:
            return None
        ch = guild.get_channel(cid)
        return ch if isinstance(ch, discord.TextChannel) else None

    async def _moderator(self, guild: discord.Guild, action, target_id: int):
        try:
            async for entry in guild.audit_logs(limit=8, action=action):
                if entry.target and entry.target.id == target_id:
                    if entry.user and entry.user.id != self.bot.user.id:
                        return entry.user, entry.reason
        except discord.Forbidden:
            pass
        except Exception:
            pass
        return None, None

    def _truncate(self, text: str, limit: int) -> str:
        if not text:
            return "—"
        text = text.replace("`", "\\`")
        if len(text) > limit:
            return text[: limit - 3] + "..."
        return text

    async def _send(self, guild, log_type, embed):
        ch = self._channel_for(guild, log_type)
        if not ch:
            return
        # Анти-дубль: один и тот же лог (по содержимому) не отправляем дважды
        # в течение окна. Защита от двух инстансов бота / двойной обработки.
        fp = make_log_fingerprint(log_type, embed)
        if not try_claim_log(fp):
            log.info("Пропущен дубль лога (%s)", log_type)
            return
        try:
            embed.set_footer(text=f"⚙ {self.run_id}")
            await ch.send(embed=embed)
        except discord.Forbidden:
            log.warning("Нет прав писать в лог-канал %s (%s)", ch, log_type)
        except Exception as e:
            log.error("Ошибка отправки лога %s: %s", log_type, e)

    # ---------- Команды настройки ----------

    logs = app_commands.Group(name="logs", description="Настройка каналов логов", guild_only=True, default_permissions=discord.Permissions(manage_guild=True))

    @logs.command(name="set", description="Назначить канал для типа логов")
    @app_commands.describe(log_type="Тип логов", channel="Канал для логов")
    async def logs_set_channel(self, interaction: discord.Interaction, log_type: str, channel: discord.TextChannel):
        if log_type not in LOG_TYPES:
            await interaction.response.send_message(
                f"❌ Неизвестный тип `{log_type}`. Доступные: {', '.join(LOG_TYPES)}", ephemeral=True
            )
            return
        set_log_channel(log_type, channel.id)
        embed = discord.Embed(
            title="✅ Канал логов назначен",
            description=f"**{log_type}** → {channel.mention}\n{LOG_TYPES[log_type]}",
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @logs.command(name="show", description="Показать привязки каналов логов")
    async def logs_show(self, interaction: discord.Interaction):
        cfg = get_all_log_channels()
        if not cfg:
            await interaction.response.send_message(
                "📭 Каналы логов не настроены. Используй `/logs set channel`.", ephemeral=True
            )
            return
        embed = discord.Embed(
            title="📋 Каналы логов",
            color=discord.Color.blurple(),
        )
        for k, v in cfg.items():
            ch = interaction.guild.get_channel(v)
            embed.add_field(name=LOG_TYPES.get(k, k), value=ch.mention if ch else f"`{v}`", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @logs.command(name="test", description="Отправить тестовый эмбед во все настроенные каналы")
    async def logs_test(self, interaction: discord.Interaction):
        cfg = get_all_log_channels()
        if not cfg:
            await interaction.response.send_message("📭 Каналы логов не настроены.", ephemeral=True)
            return
        sent = 0
        for k, v in cfg.items():
            ch = interaction.guild.get_channel(v)
            if not ch:
                continue
            embed = discord.Embed(
                title="🧪 Тестовый лог",
                description=f"Тип: **{LOG_TYPES.get(k, k)}** (`{k}`)",
                color=COLORS.get(k, discord.Color.blurple()),
                timestamp=discord.utils.utcnow(),
            )
            try:
                await ch.send(embed=embed)
                sent += 1
            except discord.Forbidden:
                pass
        await interaction.response.send_message(f"✅ Отправлено в {sent} каналов.", ephemeral=True)

    @logs.command(name="remove", description="Убрать привязку типа логов")
    @app_commands.describe(log_type="Тип логов")
    async def logs_remove(self, interaction: discord.Interaction, log_type: str):
        if log_type not in LOG_TYPES:
            await interaction.response.send_message(f"❌ Неизвестный тип `{log_type}`.", ephemeral=True)
            return
        if remove_log_channel(log_type):
            await interaction.response.send_message(f"✅ Привязка `{log_type}` убрана.", ephemeral=True)
        else:
            await interaction.response.send_message("ℹ️ Такой привязки не было.", ephemeral=True)

    # ---------- Сообщения ----------

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.guild is None or message.author is None or message.author.bot:
            return
        embed = discord.Embed(
            title="🗑 Сообщение удалено",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Автор", value=f"{message.author.mention} ({message.author})", inline=True)
        embed.add_field(name="Канал", value=message.channel.mention, inline=True)
        embed.add_field(
            name="ID",
            value=f"{message.author.id} • {message.id}",
            inline=False,
        )
        embed.add_field(name="Содержимое", value=self._truncate(message.content or "*нет текста*", 1000), inline=False)
        await self._send(message.guild, "messages", embed)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.guild is None or before.author is None or before.author.bot:
            return
        if before.content == after.content:
            return
        embed = discord.Embed(
            title="✏️ Сообщение изменено",
            color=discord.Color.yellow(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Автор", value=f"{before.author.mention} ({before.author})", inline=True)
        embed.add_field(name="Канал", value=f"[перейти]({after.jump_url})", inline=True)
        embed.add_field(name="Было", value=self._truncate(before.content or "*нет текста*", 500), inline=False)
        embed.add_field(name="Стало", value=self._truncate(after.content or "*нет текста*", 500), inline=False)
        await self._send(before.guild, "messages", embed)

    # ---------- Вход / выход ----------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.guild is None:
            return
        embed = discord.Embed(
            title="👋 Участник присоединился",
            description=f"{member.mention} ({member})",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="ID", value=str(member.id), inline=True)
        embed.add_field(
            name="Аккаунт создан",
            value=f"<t:{int(member.created_at.timestamp())}:R>" if member.created_at else "—",
            inline=True,
        )
        if member.avatar:
            embed.set_thumbnail(url=member.avatar.url)
        try:
            embed.set_footer(text=f"Участников: {member.guild.member_count}")
        except Exception:
            pass
        await self._send(member.guild, "joins", embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if member.guild is None:
            return
        mod, reason = await self._moderator(member.guild, discord.AuditLogAction.kick, member.id)
        if mod:
            title = "👢 Пользователь кикнут"
            desc = f"{mod.mention} кикнул {member.mention} ({member})"
        else:
            title = "🚪 Участник покинул сервер"
            desc = f"{member.mention} ({member})"
        embed = discord.Embed(title=title, description=desc, color=discord.Color.orange(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Причина", value=reason or "Не указана", inline=False)
        roles = [r.mention for r in member.roles if r != member.guild.default_role]
        embed.add_field(name="Роли", value=", ".join(roles) if roles else "@everyone", inline=False)
        embed.set_thumbnail(url=member.display_avatar.url)
        await self._send(member.guild, "moderation", embed)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user):
        if guild is None:
            return
        mod, reason = await self._moderator(guild, discord.AuditLogAction.ban, user.id)
        embed = discord.Embed(
            title="🔨 Пользователь забанен",
            description=(
                f"{mod.mention if mod else 'Система'} заблокировал {user.mention} ({user})"
            ),
            color=discord.Color.dark_red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Причина", value=reason or "Не указана", inline=False)
        embed.set_thumbnail(url=getattr(user, "display_avatar", None) and user.display_avatar.url or "")
        await self._send(guild, "moderation", embed)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user):
        if guild is None:
            return
        mod, reason = await self._moderator(guild, discord.AuditLogAction.unban, user.id)
        embed = discord.Embed(
            title="♻️ Пользователь разбанен",
            description=f"{mod.mention if mod else 'Система'} разблокировал {user.mention} ({user})",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Причина", value=reason or "Не указана", inline=False)
        await self._send(guild, "moderation", embed)

    # ---------- Изменение участника (ник / роли / таймаут) ----------

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.guild is None:
            return
        guild = before.guild

        if before.nick != after.nick:
            embed = discord.Embed(
                title="🏷 Никнейм изменён",
                description=f"{after.mention} ({after})",
                color=discord.Color.blurple(),
                timestamp=discord.utils.utcnow(),
            )
            mod, _ = await self._moderator(guild, discord.AuditLogAction.member_update, after.id)
            embed.add_field(name="Было", value=self._truncate(before.nick or "—", 200), inline=True)
            embed.add_field(name="Стало", value=self._truncate(after.nick or "—", 200), inline=True)
            embed.add_field(name="Кто", value=mod.mention if mod else "—", inline=False)
            await self._send(guild, "roles", embed)

        if before.timed_out_until != after.timed_out_until:
            mod, _ = await self._moderator(guild, discord.AuditLogAction.member_update, after.id)
            if after.timed_out_until and (not before.timed_out_until or after.timed_out_until > before.timed_out_until):
                dur = after.timed_out_until - discord.utils.utcnow()
                title, color = "🔇 Участник заглушен", discord.Color.orange()
                extra = f"Длительность: **{str(dur).split('.')[0]}**"
            else:
                title, color = "🔊 Участник разглушен", discord.Color.green()
                extra = ""
            embed = discord.Embed(
                title=title,
                description=f"{after.mention} ({after})",
                color=color,
                timestamp=discord.utils.utcnow(),
            )
            embed.add_field(name="Модератор", value=mod.mention if mod else "—", inline=False)
            if extra:
                embed.add_field(name="Детали", value=extra, inline=False)
            await self._send(guild, "moderation", embed)

        old_roles = set(r.id for r in before.roles)
        new_roles = set(r.id for r in after.roles)
        added = new_roles - old_roles
        removed = old_roles - new_roles
        if added or removed:
            mod, _ = await self._moderator(guild, discord.AuditLogAction.member_role_update, after.id)
            embed = discord.Embed(
                title=f"Роли участника {after.display_name} изменены",
                description=f"{after.mention} ({after})",
                color=discord.Color.blurple(),
                timestamp=discord.utils.utcnow(),
            )
            if added:
                embed.add_field(
                    name="Добавлены",
                    value=", ".join(f"<@&{rid}>" for rid in added),
                    inline=False,
                )
            if removed:
                embed.add_field(
                    name="Убраны",
                    value=", ".join(f"<@&{rid}>" for rid in removed),
                    inline=False,
                )
            embed.add_field(name="Кто", value=mod.mention if mod else "Неизвестно", inline=False)
            await self._send(guild, "roles", embed)

    # ---------- Роли ----------

    @commands.Cog.listener()
    async def on_guild_role_create(self, role):
        if role.guild is None:
            return
        embed = discord.Embed(
            title="➕ Роль создана",
            description=f"{role.mention} ({role.name})",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="ID", value=str(role.id), inline=True)
        embed.add_field(name="Цвет", value=str(role.color), inline=True)
        await self._send(role.guild, "roles", embed)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role):
        if role.guild is None:
            return
        embed = discord.Embed(
            title="➖ Роль удалена",
            description=f"`{role.name}`",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="ID", value=str(role.id), inline=True)
        await self._send(role.guild, "roles", embed)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before, after):
        if after.guild is None:
            return
        embed = None
        if before.name != after.name:
            embed = discord.Embed(title="✏️ Роль переименована", color=discord.Color.blurple())
            embed.add_field(name="Было", value=before.name, inline=True)
            embed.add_field(name="Стало", value=after.name, inline=True)
            embed.timestamp = discord.utils.utcnow()
        elif before.color != after.color:
            embed = discord.Embed(title="🎨 Цвет роли изменён", description=after.mention, color=discord.Color.blurple())
            embed.add_field(name="Было", value=str(before.color), inline=True)
            embed.add_field(name="Стало", value=str(after.color), inline=True)
            embed.timestamp = discord.utils.utcnow()
        if embed:
            await self._send(after.guild, "roles", embed)

    # ---------- Каналы ----------

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel):
        if channel.guild is None:
            return
        embed = discord.Embed(
            title="➕ Канал создан",
            description=channel.mention if hasattr(channel, "mention") else f"`{channel.name}`",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Тип", value=str(channel.type).title(), inline=True)
        embed.add_field(name="ID", value=str(channel.id), inline=True)
        await self._send(channel.guild, "channels", embed)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        if channel.guild is None:
            return
        embed = discord.Embed(
            title="➖ Канал удалён",
            description=f"`{channel.name}`",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Тип", value=str(channel.type).title(), inline=True)
        embed.add_field(name="ID", value=str(channel.id), inline=True)
        await self._send(channel.guild, "channels", embed)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after):
        if after.guild is None:
            return
        if before.name != after.name:
            embed = discord.Embed(
                title="✏️ Канал переименован",
                description=after.mention if hasattr(after, "mention") else f"`{after.name}`",
                color=discord.Color.blurple(),
                timestamp=discord.utils.utcnow(),
            )
            embed.add_field(name="Было", value=before.name, inline=True)
            embed.add_field(name="Стало", value=after.name, inline=True)
            await self._send(after.guild, "channels", embed)

    # ---------- Голос ----------

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.guild is None or member.bot:
            return
        guild = member.guild
        now = discord.utils.utcnow()

        # Дедз-канал для анти-дубля
        key = (member.id, getattr(before.channel, "id", None), getattr(after.channel, "id", None))
        last = self._voice_dedup.get(key)
        if last and (now - last).total_seconds() < self._VOICE_DEDUP_SECONDS:
            return
        self._voice_dedup[key] = now

        if not before.channel and after.channel:
            embed = discord.Embed(
                title="🔊 Зашёл в голосовой",
                description=f"{member.mention} → {after.channel.mention}",
                color=discord.Color.green(),
                timestamp=now,
            )
            await self._send(guild, "voice", embed)
        elif before.channel and not after.channel:
            embed = discord.Embed(
                title="🔇 Вышел из голосового",
                description=f"{member.mention} ← {before.channel.mention}",
                color=discord.Color.red(),
                timestamp=now,
            )
            await self._send(guild, "voice", embed)
        elif before.channel and after.channel and before.channel != after.channel:
            embed = discord.Embed(
                title="🔀 Переместился в голосовом",
                description=f"{member.mention}: {before.channel.mention} → {after.channel.mention}",
                color=discord.Color.blurple(),
                timestamp=now,
            )
            await self._send(guild, "voice", embed)

    # ---------- Логи ошибок ----------

    @commands.Cog.listener()
    async def on_app_command_error(self, interaction: discord.Interaction, error: Exception):
        if interaction.guild is None:
            return
        from discord.app_commands.errors import CommandInvokeError, CommandNotFound
        # CommandNotFound — нормальная ситуация (команда ещё не синхронизирована
        # в кэше Discord). Такое не считаем ошибкой и не логируем.
        if isinstance(error, CommandNotFound):
            return
        # Логируем только реальные сбои выполнения, остальное (нет прав,
        # не найдено, ошибки проверки) — это ожидаемое поведение.
        if not isinstance(error, CommandInvokeError):
            return

        real = error.original if isinstance(error, CommandInvokeError) else error

        tb = "".join(
            traceback.format_exception(type(real), real, real.__traceback__)
        )
        name = (interaction.command.qualified_name if interaction.command else "?")
        line_no = real.__traceback__.tb_lineno if real.__traceback__ else "?"
        embed = discord.Embed(
            title=f"Ошибка команды {name}",
            description=f"```py\n{tb[-1500:]}\n```",
            color=discord.Color.dark_red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Пользователь", value=f"{interaction.user.mention} ({interaction.user})", inline=False)
        embed.add_field(
            name="Тип",
            value=f"`{type(real).__name__}` (строка {line_no})",
            inline=False,
        )
        await self._send(interaction.guild, "errors", embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(LoggingCog(bot))

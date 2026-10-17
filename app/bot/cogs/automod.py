import logging
import re
import time
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from database import (
    add_warning,
    get_warnings,
    get_warning_count,
    clear_warnings,
    get_automod_config,
    set_automod_config,
    get_all_automod_config,
    add_badword,
    remove_badword,
    get_badwords,
    add_whitelist,
    remove_whitelist,
    get_whitelist,
    is_whitelisted,
    get_log_channel,
    make_log_fingerprint,
    try_claim_log,
)

log = logging.getLogger("automod")

URL_RE = re.compile(r"(https?://|www\.)\S+", re.IGNORECASE)
WORD_RE = re.compile(r"\b(\w+)\b", re.UNICODE)


class AutomodCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._spam = {}
        self._own_actions = set()
        self.run_id = getattr(bot, "run_id", "?")
        log.info("Автомод запущен, run_id=%s", self.run_id)

    # ---------- Хелперы ----------

    def _is_mod(self, member: discord.Member) -> bool:
        return (
            member.guild_permissions.administrator
            or member.guild_permissions.manage_messages
            or member.guild_permissions.manage_guild
        )

    def _whitelisted(self, member: discord.Member) -> bool:
        role_ids = [r.id for r in member.roles]
        if self._is_mod(member):
            return True
        return is_whitelisted(member.id, role_ids)

    def _bypass(self, message: discord.Message) -> bool:
        if message.guild is None or message.author is None or message.author.bot:
            return True
        if not isinstance(message.channel, discord.TextChannel):
            return True
        if self._whitelisted(message.author):
            return True
        return False

    async def _log_action(self, guild: discord.Guild, embed: discord.Embed):
        cid = get_log_channel("moderation")
        if not cid:
            return
        ch = guild.get_channel(cid)
        if not ch:
            return
        if not try_claim_log(make_log_fingerprint("moderation", embed)):
            log.info("Пропущен дубль лога модерации")
            return
        try:
            embed.set_footer(text=f"⚙ {self.run_id}")
            await ch.send(embed=embed)
        except Exception as e:
            log.error("Ошибка лога модерации: %s", e)

    async def _timeout(self, member: discord.Member, minutes: int, reason: str):
        try:
            until = discord.utils.utcnow() + timedelta(minutes=minutes)
            await member.timeout(until, reason=reason)
            return True
        except discord.Forbidden:
            return False
        except Exception as e:
            log.error("timeout error: %s", e)
            return False

    def _auto_mod_log(self, guild, member, action: str, reason: str, detail: str = ""):
        embed = discord.Embed(
            title=f"🤖 Автомодерация: {action}",
            description=f"{member.mention} ({member})",
            color=discord.Color.dark_red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Причина", value=reason, inline=False)
        if detail:
            embed.add_field(name="Детали", value=detail, inline=False)
        return self._log_action(guild, embed)

    # ---------- Спам-детекция ----------

    def _check_spam(self, member: discord.Member, channel_id: int) -> int | None:
        cfg = get_all_automod_config()
        msgs = int(cfg["spam_msgs"])
        secs = int(cfg["spam_secs"])
        mute_min = int(cfg["spam_mute_mins"])
        if msgs <= 0 or secs <= 0 or mute_min <= 0:
            return None

        now = time.time()
        key = (member.id, channel_id)
        times = [t for t in self._spam.get(key, []) if now - t < secs]
        times.append(now)
        self._spam[key] = times

        if len(times) >= msgs:
            self._spam.pop(key, None)
            return mute_min
        return None

    # ---------- on_message: все фильтры ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if self._bypass(message):
            return
        member = message.author
        cfg = get_all_automod_config()

        # 1. Чёрный список слов
        badwords = get_badwords()
        if badwords:
            content_lower = message.content.lower()
            for word in badwords:
                pattern = r"\b" + re.escape(word.lower()) + r"\b"
                if re.search(pattern, content_lower):
                    self._own_actions.add(message.id)
                    try:
                        await message.delete()
                    except discord.Forbidden:
                        pass
                    await self._auto_mod_log(
                        message.guild, member, "запрещённое слово",
                        f"Сообщение удалено: {word}",
                        self._truncate(message.content, 300),
                    )
                    return

        # 2. Антispам
        mute_min = self._check_spam(member, message.channel.id)
        if mute_min:
            msg = "Спам: слишком много сообщений за короткое время"
            ok = await self._timeout(member, mute_min, msg)
            if ok:
                await self._auto_mod_log(
                    message.guild, member, "спам", f"Мут на {mute_min} мин.", self._truncate(message.content, 300)
                )
            return

        # 3. Ссылки
        if cfg["links_allowed"] != "1":
            if URL_RE.search(message.content):
                self._own_actions.add(message.id)
                try:
                    await message.delete()
                except discord.Forbidden:
                    pass
                await self._auto_mod_log(
                    message.guild, member, "ссылка",
                    "Ссылки запрещены", self._truncate(message.content, 300),
                )
                return

        # 4. Капс
        caps_pct = int(cfg["caps_percent"])
        min_len = int(cfg["caps_min_len"])
        letters = [c for c in message.content if c.isalpha()]
        if (
            caps_pct > 0
            and len(message.content) >= min_len
            and len(letters) > 0
            and sum(1 for c in letters if c.isupper()) / len(letters) * 100 >= caps_pct
        ):
            self._own_actions.add(message.id)
            try:
                await message.delete()
            except discord.Forbidden:
                pass
            await self._auto_mod_log(
                message.guild, member, "капс",
                f"Много заглавных букв (> {caps_pct}%)", self._truncate(message.content, 300),
            )
            return

    def _truncate(self, text: str, limit: int) -> str:
        if not text:
            return "—"
        if len(text) > limit:
            return text[: limit - 3] + "..."
        return text

    # ---------- Модерационные команды ----------

    @app_commands.command(name="warn", description="Выдать предупреждение участнику")
    @app_commands.guild_only()
    @app_commands.default_permissions(kick_members=True)
    async def warn(self, interaction: discord.Interaction, user: discord.Member, reason: str = "Не указана"):
        count = add_warning(user.id, interaction.guild_id, reason, interaction.user.id)
        embed = discord.Embed(
            title="⚠️ Предупреждение выдано",
            description=f"{user.mention} — предупреждение **#{count}**",
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Причина", value=reason, inline=False)
        embed.add_field(name="Модератор", value=interaction.user.mention, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await self._log_action(interaction.guild, embed)

        auto_mute = int(get_automod_config("warns_to_mute"))
        if count >= auto_mute:
            mins = int(get_automod_config("warn_mute_mins"))
            ok = await self._timeout(user, mins, f"Автомут: {count} предупреждений")
            if ok:
                auto_embed = discord.Embed(
                    title="🔇 Автомут за предупреждения",
                    description=f"{user.mention} заглушен на **{mins} мин.**",
                    color=discord.Color.dark_red(),
                    timestamp=discord.utils.utcnow(),
                )
                await self._log_action(interaction.guild, auto_embed)

    @app_commands.command(name="warnings", description="Список предупреждений участника")
    @app_commands.guild_only()
    @app_commands.default_permissions(kick_members=True)
    async def warnings(self, interaction: discord.Interaction, user: discord.Member):
        rows = get_warnings(user.id, interaction.guild_id)
        if not rows:
            await interaction.response.send_message(f"✅ У {user.mention} нет предупреждений.", ephemeral=True)
            return
        embed = discord.Embed(
            title=f"Предупреждения {user.display_name}",
            description=f"Всего: **{len(rows)}**",
            color=discord.Color.orange(),
        )
        for r in rows[-10:]:
            mod = interaction.guild.get_member(r["moderator_id"])
            embed.add_field(
                name=f"#{r['id']}",
                value=f"{r['reason']}\nмодератор: {mod.mention if mod else r['moderator_id']}",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="clearwarn", description="Очистить предупреждения участника")
    @app_commands.guild_only()
    @app_commands.default_permissions(kick_members=True)
    async def clearwarn(self, interaction: discord.Interaction, user: discord.Member):
        n = clear_warnings(user.id, interaction.guild_id)
        await interaction.response.send_message(f"🗑 Убрано предупреждений: {n}.", ephemeral=True)

    @app_commands.command(name="kick", description="Кикнуть участника")
    @app_commands.guild_only()
    @app_commands.default_permissions(kick_members=True)
    async def kick(self, interaction: discord.Interaction, user: discord.Member, reason: str = "Не указана"):
        if not interaction.user.top_role > user.top_role and interaction.user.id != interaction.guild.owner_id:
            await interaction.response.send_message("❌ Нельзя кикнуть этого участника.", ephemeral=True)
            return
        try:
            await user.kick(reason=f"{interaction.user}: {reason}")
        except discord.Forbidden:
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return
        embed = discord.Embed(
            title="👢 Участник кикнут",
            description=f"{user.mention} ({user})",
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Причина", value=reason, inline=False)
        embed.add_field(name="Модератор", value=interaction.user.mention, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await self._log_action(interaction.guild, embed)

    @app_commands.command(name="ban", description="Забанить участника")
    @app_commands.guild_only()
    @app_commands.default_permissions(ban_members=True)
    async def ban(self, interaction: discord.Interaction, user: discord.Member, reason: str = "Не указана"):
        try:
            await user.ban(reason=f"{interaction.user}: {reason}", delete_message_days=0)
        except discord.Forbidden:
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return
        embed = discord.Embed(
            title="🔨 Участник забанен",
            description=f"{user.mention} ({user})",
            color=discord.Color.dark_red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Причина", value=reason, inline=False)
        embed.add_field(name="Модератор", value=interaction.user.mention, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await self._log_action(interaction.guild, embed)

    @app_commands.command(name="unban", description="Разбанить участника по ID")
    @app_commands.guild_only()
    @app_commands.default_permissions(ban_members=True)
    async def unban(self, interaction: discord.Interaction, user_id: str):
        try:
            user = await self.bot.fetch_user(int(user_id))
            await interaction.guild.unban(user, reason=f"Разбан от {interaction.user}")
        except Exception as e:
            await interaction.response.send_message(f"❌ Не удалось разбанить: {e}", ephemeral=True)
            return
        embed = discord.Embed(
            title="♻️ Участник разбанен",
            description=f"{user.mention} ({user})",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await self._log_action(interaction.guild, embed)

    @app_commands.command(name="mute", description="Заглушить участника (таймаут)")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    async def mute(self, interaction: discord.Interaction, user: discord.Member, minutes: int, reason: str = "Не указана"):
        if not interaction.user.top_role > user.top_role and interaction.user.id != interaction.guild.owner_id:
            await interaction.response.send_message("❌ Нельзя заглушить этого участника.", ephemeral=True)
            return
        ok = await self._timeout(user, minutes, f"{interaction.user}: {reason}")
        if not ok:
            await interaction.response.send_message("❌ Не удалось заглушить (нет прав).", ephemeral=True)
            return
        embed = discord.Embed(
            title="🔇 Участник заглушен",
            description=f"{user.mention} на **{minutes} мин.**",
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Причина", value=reason, inline=False)
        embed.add_field(name="Модератор", value=interaction.user.mention, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await self._log_action(interaction.guild, embed)

    @app_commands.command(name="unmute", description="Снять таймаут с участника")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    async def unmute(self, interaction: discord.Interaction, user: discord.Member):
        try:
            await user.timeout(None, reason=f"Размут от {interaction.user}")
        except discord.Forbidden:
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return
        embed = discord.Embed(
            title="🔊 Участник разглушен",
            description=f"{user.mention} ({user})",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await self._log_action(interaction.guild, embed)

    # ---------- Настройка автомода ----------

    automod = app_commands.Group(name="automod", description="Настройка автомодерации")

    @automod.command(name="config", description="Показать текущие настройки автомода")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def automod_config(self, interaction: discord.Interaction):
        cfg = get_all_automod_config()
        embed = discord.Embed(
            title="⚙️ Настройки автомодерации",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Антispам", value=f"{cfg['spam_msgs']} сообщений / {cfg['spam_secs']} сек. → мут {cfg['spam_mute_mins']} мин.", inline=False)
        embed.add_field(name="Ссылки", value="Запрещены" if cfg["links_allowed"] != "1" else "Разрешены", inline=False)
        embed.add_field(name="Капс", value=f"блок при >{cfg['caps_percent']}% заглавных (мин. {cfg['caps_min_len']} симв.)" if int(cfg["caps_percent"]) > 0 else "Выключен", inline=False)
        embed.add_field(name="Варны → мут", value=f"{cfg['warns_to_mute']} варнов → мут {cfg['warn_mute_mins']} мин.", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @automod.command(name="spam", description="Настроить антispам")
    @app_commands.describe(msgs="Сообщений", secs="За секунд", mute_mins="Мут на минут")
    async def automod_spam(self, interaction: discord.Interaction, msgs: int, secs: int, mute_mins: int):
        set_automod_config("spam_msgs", msgs)
        set_automod_config("spam_secs", secs)
        set_automod_config("spam_mute_mins", mute_mins)
        await interaction.response.send_message(
            f"✅ Антispам: {msgs} сообщений / {secs} сек. → мут {mute_mins} мин.", ephemeral=True
        )

    @automod.command(name="links", description="Включить/выключить блокировку ссылок")
    @app_commands.describe(enabled="true — запретить, false — разрешить")
    async def automod_links(self, interaction: discord.Interaction, enabled: bool):
        set_automod_config("links_allowed", "0" if enabled else "1")
        await interaction.response.send_message(
            f"✅ Ссылки {'запрещены' if enabled else 'разрешены'}.", ephemeral=True
        )

    @automod.command(name="caps", description="Порог капса (0 = выключено)")
    @app_commands.describe(percent="Процент заглавных букв для блокировки")
    async def automod_caps(self, interaction: discord.Interaction, percent: int):
        percent = max(0, min(100, percent))
        set_automod_config("caps_percent", percent)
        await interaction.response.send_message(
            f"✅ Капс-фильтр: {'выключен' if percent == 0 else f'порог {percent}%'}.",
            ephemeral=True,
        )

    @automod.command(name="warnmute", description="Настроить автомут за варны")
    @app_commands.describe(warns="Кол-во варнов", mute_mins="Мут на минут")
    async def automod_warnmute(self, interaction: discord.Interaction, warns: int, mute_mins: int):
        set_automod_config("warns_to_mute", warns)
        set_automod_config("warn_mute_mins", mute_mins)
        await interaction.response.send_message(f"✅ Автомут: {warns} варнов → мут {mute_mins} мин.", ephemeral=True)

    # --- badwords ---

    badwords = app_commands.Group(name="badwords", description="Чёрный список слов", parent=automod)

    @badwords.command(name="add", description="Добавить слово в чёрный список")
    async def badwords_add(self, interaction: discord.Interaction, word: str):
        if add_badword(word):
            await interaction.response.send_message(f"✅ Слово `{word.lower()}` добавлено.", ephemeral=True)
        else:
            await interaction.response.send_message("ℹ️ Слово уже в списке.", ephemeral=True)

    @badwords.command(name="remove", description="Убрать слово из чёрного списка")
    async def badwords_remove(self, interaction: discord.Interaction, word: str):
        if remove_badword(word):
            await interaction.response.send_message(f"✅ Слово `{word.lower()}` убрано.", ephemeral=True)
        else:
            await interaction.response.send_message("ℹ️ Слова нет в списке.", ephemeral=True)

    @badwords.command(name="list", description="Показать чёрный список слов")
    async def badwords_list(self, interaction: discord.Interaction):
        words = get_badwords()
        if not words:
            await interaction.response.send_message("📭 Чёрный список пуст.", ephemeral=True)
            return
        await interaction.response.send_message(
            "**Чёрный список слов:**\n" + ", ".join(f"`{w}`" for w in words), ephemeral=True
        )

    # --- whitelist ---

    whitelist_group = app_commands.Group(name="whitelist", description="Белый список автомода", parent=automod)

    @whitelist_group.command(name="add", description="Добавить юзера/роль в белый список")
    async def whitelist_add(self, interaction: discord.Interaction, member: discord.Member = None, role: discord.Role = None):
        if member:
            add_whitelist("user", member.id)
            await interaction.response.send_message(f"✅ {member.mention} добавлен в белый список.", ephemeral=True)
        elif role:
            add_whitelist("role", role.id)
            await interaction.response.send_message(f"✅ Роль {role.mention} добавлена в белый список.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Укажи `member` или `role`.", ephemeral=True)

    @whitelist_group.command(name="remove", description="Убрать из белого списка")
    async def whitelist_remove(self, interaction: discord.Interaction, member: discord.Member = None, role: discord.Role = None):
        if member:
            remove_whitelist("user", member.id)
            await interaction.response.send_message(f"✅ {member.mention} убран из белого списка.", ephemeral=True)
        elif role:
            remove_whitelist("role", role.id)
            await interaction.response.send_message(f"✅ Роль {role.mention} убрана из белого списка.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Укажи `member` или `role`.", ephemeral=True)

    @whitelist_group.command(name="list", description="Показать белый список")
    async def whitelist_list(self, interaction: discord.Interaction):
        rows = get_whitelist()
        if not rows:
            await interaction.response.send_message("📭 Белый список пуст.", ephemeral=True)
            return
        lines = []
        for r in rows:
            if r["target_type"] == "user":
                u = interaction.guild.get_member(r["target_id"])
                lines.append(f"👤 {u.mention if u else r['target_id']}")
            else:
                lines.append(f"🎭 <@&{r['target_id']}>")
        await interaction.response.send_message("**Белый список:**\n" + "\n".join(lines), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AutomodCog(bot))

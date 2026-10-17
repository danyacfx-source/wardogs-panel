import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import random
import asyncio
import discord
from discord import app_commands
from discord.ext import commands

from database import (
    add_xp,
    get_user_xp,
    xp_progress,
    get_leaderboard,
    get_xp_rank,
    set_last_message,
    can_get_message_xp,
    get_conn,
)
from rank_image import render_rank_card

MSG_XP_MIN = 15
MSG_XP_MAX = 25
VOICE_XP_PER_SECOND = 0.2
XP_COOLDOWN_SECONDS = 60


# ================== UI лидерборда ==================

def _leaderboard_rows(guild_id: int, metric: str, limit: int = 10) -> list[tuple]:
    """(name, value) строки для лидерборда по выбранной метрике."""
    conn = get_conn()
    medals = ["🥇", "🥈", "🥉"]

    if metric == "level":
        rows = get_leaderboard(guild_id, limit)
        out = []
        for i, r in enumerate(rows):
            medal = medals[i] if i < 3 else f"**{i+1}.**"
            out.append((r["user_id"], f"{medal} · {r['xp']} XP · ур. {r['level']}"))
        return out

    col = {"messages": "messages", "voice": "voice_seconds", "voice_joins": "voice_joins"}[metric]
    rows = conn.execute(
        f"SELECT user_id, {col} FROM member_stats WHERE {col} > 0 ORDER BY {col} DESC LIMIT ?",
        (limit,),
    ).fetchall()
    out = []
    for i, r in enumerate(rows):
        medal = medals[i] if i < 3 else f"**{i+1}.**"
        if metric == "voice":
            val = f"{r[col] // 60} мин"
        else:
            val = str(r[col])
        out.append((r["user_id"], f"{medal} · {val}"))
    return out


def _metric_label(metric: str) -> str:
    return {
        "level": "⭐ Уровень",
        "messages": "💬 Сообщения",
        "voice": "🔊 Голос",
        "voice_joins": "🚪 Заходы",
    }[metric]


class LeaderboardView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.metric = "level"

    def _build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"🏆 Лидерборд — {_metric_label(self.metric)}",
            color=discord.Color(0xF39C12),
        )
        rows = _leaderboard_rows(self.guild_id, self.metric)
        if not rows:
            embed.description = "_Пока нет данных._"
            return embed

        lines = []
        guild = None
        bot = getattr(self, "_bot", None)
        for uid, val in rows:
            if bot is not None:
                member = None
                for g in bot.guilds:
                    if g.id == self.guild_id:
                        guild = g
                        break
                if guild:
                    member = guild.get_member(uid)
                name = member.display_name if member else f"id:{uid}"
            else:
                name = f"id:{uid}"
            lines.append(f"**{name}** — {val}")
        embed.description = "\n".join(lines)
        embed.set_footer(text="Нажми /rank — свой прогресс • Обновиться: кнопка ниже")
        return embed

    def _setup_view(self):
        self.clear_items()

        select = discord.ui.Select(
            placeholder="Метрика",
            options=[
                discord.SelectOption(label="⭐ Уровень", value="level", default=(self.metric == "level")),
                discord.SelectOption(label="💬 Сообщения", value="messages", default=(self.metric == "messages")),
                discord.SelectOption(label="🔊 Голос", value="voice", default=(self.metric == "voice")),
                discord.SelectOption(label="🚪 Заходы", value="voice_joins", default=(self.metric == "voice_joins")),
            ],
        )
        select.callback = self._on_metric
        self.add_item(select)

        refresh = discord.ui.Button(label="🔄", style=discord.ButtonStyle.secondary, custom_id="lb_refresh")
        refresh.callback = self._on_refresh
        self.add_item(refresh)

    async def _on_metric(self, interaction: discord.Interaction):
        self.metric = interaction.data["values"][0]
        self._setup_view()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    async def _on_refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return True


class LevelsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._voice_sessions: dict[int, dict] = {}

    def _hook_view(self, view: LeaderboardView):
        view._bot = self.bot
        view._setup_view()
        return view

    # ---------- XP за сообщения ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        name = getattr(message.channel, "name", "")
        if name.startswith("тикет-") or name.startswith("ticket-"):
            return

        # Статистика: счётчик сообщений во всех каналах (кроме тикетов, ботов).
        conn = get_conn()
        conn.execute(
            "INSERT INTO member_stats (user_id, messages, voice_seconds, voice_joins) "
            "VALUES (?, 1, 0, 0) "
            "ON CONFLICT(user_id) DO UPDATE SET messages = messages + 1",
            (message.author.id,),
        )
        conn.commit()

        if not can_get_message_xp(message.author.id, message.guild.id, XP_COOLDOWN_SECONDS):
            return
        amount = random.randint(MSG_XP_MIN, MSG_XP_MAX)
        old_l, new_l = add_xp(message.author.id, message.guild.id, amount)
        set_last_message(message.author.id, message.guild.id)
        if new_l != old_l:
            await self._level_up_msg(message.author, new_l)

    # ---------- XP за голос + статистика ----------

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before, after):
        if member.bot or not member.guild:
            return

        joined = before.channel is None and after.channel is not None
        left = before.channel is not None and after.channel is None
        switched = before.channel and after.channel and before.channel.id != after.channel.id

        if joined:
            self._voice_sessions[member.id] = discord.utils.utcnow()
            # Засчитываем заход даже в неактивный канал: voice_joins растёт.
            conn = get_conn()
            conn.execute(
                "INSERT INTO member_stats (user_id, messages, voice_seconds, voice_joins) "
                "VALUES (?, 0, 0, 1) "
                "ON CONFLICT(user_id) DO UPDATE SET voice_joins = voice_joins + 1",
                (member.id,),
            )
            conn.commit()
            return

        # Активный голосовой канал: это не AFK-канал и не канал отключённого звука.
        def _counts_as_active(vc, guild: discord.Guild):
            if vc is None:
                return False
            if guild.afk_channel and vc.id == guild.afk_channel.id:
                return False
            cat = vc.category.name.lower() if vc.category else ""
            if "afk" in cat or "афк" in cat:
                return False
            return True

        if left or switched:
            start = self._voice_sessions.pop(member.id, None)
            if start:
                seconds = (discord.utils.utcnow() - start).total_seconds()
                if seconds > 5:
                    # Статистика: время в активных каналах.
                    if _counts_as_active(before.channel, member.guild):
                        conn = get_conn()
                        conn.execute(
                            "INSERT INTO member_stats (user_id, messages, voice_seconds, voice_joins) "
                            "VALUES (?, 0, ?, 0) "
                            "ON CONFLICT(user_id) DO UPDATE SET voice_seconds = voice_seconds + ?",
                            (member.id, int(seconds), int(seconds)),
                        )
                        conn.commit()
                    # XP — от времени одинаково во всех каналах.
                    xp = int(seconds * VOICE_XP_PER_SECOND)
                    old_l, new_l = add_xp(member.id, member.guild.id, xp)
                    if new_l != old_l:
                        await self._level_up_msg(member, new_l)
            if switched:
                self._voice_sessions[member.id] = discord.utils.utcnow()

    async def _level_up_msg(self, member: discord.Member, level: int):
        try:
            await member.send(f"🎉 **{member.display_name}**, ты достиг уровня **{level}**!")
        except discord.Forbidden:
            pass

    # ---------- Команды ----------

    @app_commands.command(name="givexp", description="Выдать XP участнику (для тестов)")
    @app_commands.describe(user="Участник", amount="Сколько XP")
    @app_commands.guild_only()
    async def givexp(self, interaction: discord.Interaction, user: discord.Member, amount: int):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
        if not -100000 <= amount <= 100000:
            return await interaction.response.send_message("❌ Слишком много.", ephemeral=True)

        if amount >= 0:
            old_l, new_l = add_xp(user.id, interaction.guild.id, amount)
            action = "добавлено"
        else:
            old_l, new_l = add_xp(user.id, interaction.guild.id, 0)
            # Отнимаем напрямую через отрицательный xp невозможно — откат через БД
            conn = get_conn()
            conn.execute(
                "UPDATE user_xp SET xp = MAX(0, xp - ?) WHERE user_id = ? AND guild_id = ?",
                (-amount, user.id, interaction.guild.id),
            )
            conn.commit()
            data = get_user_xp(user.id, interaction.guild.id)
            new_l = 1
            if data:
                from database import level_from_xp
                new_l = level_from_xp(data[0])
                # Сохраняем пересчитанный уровень, иначе лидерборд показывает устаревший.
                conn.execute(
                    "UPDATE user_xp SET level = ? WHERE user_id = ? AND guild_id = ?",
                    (new_l, user.id, interaction.guild.id),
                )
                conn.commit()
            old_l = new_l
            action = "снято"

        data = get_user_xp(user.id, interaction.guild.id)
        xp_now = data[0] if data else 0
        await interaction.response.send_message(
            f"✅ **{user.display_name}**: {action} **{abs(amount)}** XP → теперь **{xp_now} XP**, уровень {new_l}.",
            ephemeral=True,
        )

    @app_commands.command(name="rank", description="Уровень и прогресс участника (картинка)")
    @app_commands.describe(user="Участник (по умолчанию ты)")
    @app_commands.guild_only()
    async def rank(self, interaction: discord.Interaction, user: discord.Member = None):
        member = user or interaction.user

        await interaction.response.defer()

        data = get_user_xp(member.id, interaction.guild.id)
        if not data:
            return await interaction.followup.send(
                f"У **{member.display_name}** пока нет XP — пиши в чат и сиди в войсе!",
                ephemeral=True,
            )
        xp, level = data
        cur_xp, need_xp = xp_progress(xp, level)
        rank_pos = get_xp_rank(member.id, interaction.guild.id)
        total_users = len([m for m in interaction.guild.members if not m.bot])

        conn = get_conn()
        stats = conn.execute(
            "SELECT messages, voice_seconds FROM member_stats WHERE user_id = ?",
            (member.id,),
        ).fetchone()
        messages = stats["messages"] if stats else 0
        voice_minutes = (stats["voice_seconds"] // 60) if stats else 0

        try:
            buf = await asyncio.to_thread(
                render_rank_card,
                member.display_name,
                member.display_avatar.url,
                level,
                rank_pos,
                total_users,
                cur_xp,
                need_xp,
                messages,
                voice_minutes,
            )
        except Exception:
            return await interaction.followup.send(
                "❌ Не удалось сгенерировать карточку.", ephemeral=True
            )

        file = discord.File(buf, filename=f"rank-{member.id}.png")
        await interaction.followup.send(file=file)

    @app_commands.command(name="leaderboard", description="Топ участников прямо здесь (UI-панель)")
    @app_commands.describe(channel="Канал для панели (по умолчанию текущий)")
    @app_commands.guild_only()
    async def leaderboard(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("❌ Нет прав.", ephemeral=True)

        view = self._hook_view(LeaderboardView(interaction.guild.id))
        target = channel or interaction.channel
        await interaction.response.send_message(embed=view._build_embed(), view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(LevelsCog(bot))
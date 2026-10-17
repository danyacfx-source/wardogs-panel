import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
import discord
from discord.ext import commands, tasks
from datetime import datetime, timezone

import config
from database import get_conn, get_user_xp, level_from_xp


class ActivityRolesCog(commands.Cog):
    """Роли за активность. Поддерживает:
    - type: "voice_hours" — порог голоса (hours)
    - type: "messages" — порог сообщений (count)
    - type: "level" — уровень XP (level)
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _build_roles_config(self):
        """Группирует правила по role_id. Для каждой роли список альтернатив (ИЛИ)."""
        roles = {}
        for rule in config.ACTIVITY_ROLES_CONFIG:
            role_id = rule.get("role_id")
            if not role_id:
                continue
            entry = roles.setdefault(role_id, {"prev_role_id": rule.get("prev_role_id"), "checks": []})
            check_type = rule.get("type")
            if check_type == "voice_hours":
                entry["checks"].append(("voice_seconds", rule.get("hours", 0) * 3600))
            elif check_type == "messages":
                entry["checks"].append(("messages", rule.get("count", 0)))
            elif check_type == "level":
                entry["checks"].append(("level", rule.get("level", 1)))
        return roles

    def _eligible_ids(self, conn, col, threshold, guild_id):
        """Возвращает ID пользователей, прошедших порог."""
        if col == "level":
            out = set()
            rows = conn.execute(
                "SELECT user_id, xp FROM user_xp WHERE guild_id = ? AND xp >= ?",
                (guild_id, threshold_xp(threshold)),
            ).fetchall()
            return {r["user_id"] for r in rows}
        rows = conn.execute(
            f"SELECT user_id FROM member_stats WHERE {col} >= ?", (threshold,)
        ).fetchall()
        return {r["user_id"] for r in rows}

    def _has_previous_role(self, member: discord.Member, prev_role_id):
        if not prev_role_id:
            return False
        return any(r.id == prev_role_id for r in member.roles)

    @tasks.loop(minutes=5)
    async def check_activity_roles(self):
        roles_conf = self._build_roles_config()
        if not roles_conf:
            return

        for guild in self.bot.guilds:
            conn = get_conn()
            ordered = [r for r in config.ACTIVITY_ROLES_CONFIG if r.get("role_id")]
            seen = set()
            for rule in ordered:
                role_id = rule["role_id"]
                if role_id in seen:
                    continue
                seen.add(role_id)

                conf = roles_conf[role_id]
                role = guild.get_role(role_id)
                if not role:
                    continue

                eligible_ids = set()
                for col, threshold in conf["checks"]:
                    eligible_ids |= self._eligible_ids(conn, col, threshold, guild.id)

                for member in guild.members:
                    if member.bot:
                        continue
                    has_role = any(r.id == role_id for r in member.roles)
                    if not has_role and member.id in eligible_ids:
                        try:
                            prev_role_id = conf.get("prev_role_id")
                            if prev_role_id:
                                prev_role = guild.get_role(prev_role_id)
                                if prev_role and prev_role in member.roles:
                                    await member.remove_roles(
                                        prev_role, reason="Повышение: снята предыдущая роль"
                                    )
                                    conn.execute(
                                        "INSERT INTO activity_roles_log "
                                        "(user_id, role_id, granted, timestamp) VALUES (?, ?, 0, ?)",
                                        (member.id, prev_role_id, datetime.now(timezone.utc).isoformat()),
                                    )
                            await member.add_roles(role, reason="Activity role: threshold met")
                            conn.execute(
                                "INSERT INTO activity_roles_log "
                                "(user_id, role_id, granted, timestamp) VALUES (?, ?, 1, ?)",
                                (member.id, role_id, datetime.now(timezone.utc).isoformat()),
                            )
                            logging.info("Выдана роль %s → %s", role.name, member)
                        except Exception as e:
                            logging.error("Ошибка выдачи роли %s: %s", role.name, e)

            conn.commit()

    @check_activity_roles.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.check_activity_roles.is_running():
            self.check_activity_roles.start()


def threshold_xp(level: int) -> int:
    """Суммарный XP, нужный уровня level (переиспользуем функцию из database)."""
    from database import xp_needed_for_level
    return xp_needed_for_level(level)


async def setup(bot: commands.Bot):
    await bot.add_cog(ActivityRolesCog(bot))
import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from database import (
    get_log_channel,
    log_server_event,
    remove_channel,
    remove_member,
    remove_role,
    upsert_channel,
    upsert_member,
    upsert_role,
)

log = logging.getLogger("server_sync")

# Какие события считаем «интересными» и копируем в БД (плюс в лог-канал).
_SYNC_INTERVAL_SECONDS = 3600


def _role_ids(member: discord.Member) -> str:
    return ",".join(str(r.id) for r in member.roles[1:])


class ServerSyncCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._sync_task: asyncio.Task | None = None

    # ---------- Полный прогон ----------

    async def sync_guild(self, guild: discord.Guild):
        """Разовый/фоновый прогон: участники, роли, каналы — в БД."""
        try:
            members = [m async for m in guild.fetch_members(limit=None)]
            for m in members:
                upsert_member(
                    m.id, guild.id, m.name, m.display_name,
                    getattr(m, "joined_at", None), _role_ids(m), m.bot,
                )
        except discord.Forbidden:
            log.warning("Нет прав на fetch_members в %s", guild.name)
        except Exception as e:
            log.error("Ошибка синка участников %s: %s", guild.name, e)

        for r in guild.roles:
            upsert_role(r.id, guild.id, r.name, r.color.value, r.position, r.permissions.value)

        for ch in guild.channels:
            ctype = type(ch).__name__.replace("CategoryChannel", "category").replace("TextChannel", "text").replace("VoiceChannel", "voice").replace("ForumChannel", "forum").replace("StageChannel", "stage").replace("AnnouncementsChannel", "news")
            upsert_channel(
                ch.id, guild.id, getattr(ch, "name", "?"), ctype,
                getattr(ch, "category_id", None), getattr(ch, "position", 0),
                getattr(ch, "topic", None),
            )

        log_server_event(guild.id, "sync_full", None, None, f"Участников: {len(guild.members)}")
        log.info("Полный синк %s завершён", guild.name)

        # Восстановление розыгрышей, пропавших из БД, но оставшихся в каналах.
        try:
            gwa = self.bot.get_cog("GiveawayCog")
            if gwa:
                await gwa.recover_giveaways(guild)
        except Exception as e:
            log.error("Ошибка восстановления розыгрышей в %s: %s", guild.name, e)

    async def full_sync_all(self):
        for guild in self.bot.guilds:
            await self.sync_guild(guild)

    async def _sync_loop(self):
        await self.bot.wait_until_ready()
        await self.full_sync_all()
        while True:
            await asyncio.sleep(_SYNC_INTERVAL_SECONDS)
            try:
                await self.full_sync_all()
            except Exception as e:
                log.error("Ошибка фонового синка: %s", e)

    async def cog_load(self):
        self._sync_task = asyncio.create_task(self._sync_loop())

    # ---------- События ----------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        upsert_member(
            member.id, member.guild.id, member.name, member.display_name,
            member.joined_at, _role_ids(member), member.bot,
        )
        log_server_event(member.guild.id, "member_join", member.id, None, f"<@{member.id}> зашёл на сервер")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        remove_member(member.id, member.guild.id)
        log_server_event(member.guild.id, "member_leave", member.id, None, f"<@{member.id}> покинул сервер")

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.roles != after.roles or before.display_name != after.display_name:
            upsert_member(
                after.id, after.guild.id, after.name, after.display_name,
                after.joined_at, _role_ids(after), after.bot,
            )
            log_server_event(after.guild.id, "member_update", after.id, None, f"<@{after.id}> обновил профиль/роли")

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        upsert_role(role.id, role.guild.id, role.name, role.color.value, role.position, role.permissions.value)
        log_server_event(role.guild.id, "role_create", role.id, None, f"Роль **{role.name}** создана")

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        remove_role(role.id)
        log_server_event(role.guild.id, "role_delete", role.id, None, f"Роль **{role.name}** удалена")

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        if after.guild is None:
            return
        upsert_role(after.id, after.guild.id, after.name, after.color.value, after.position, after.permissions.value)
        log_server_event(after.guild.id, "role_update", after.id, None, f"Роль **{after.name}** обновлена")

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        ctype = type(channel).__name__.replace("CategoryChannel", "category").replace("TextChannel", "text").replace("VoiceChannel", "voice").replace("ForumChannel", "forum").replace("StageChannel", "stage").replace("AnnouncementsChannel", "news")
        upsert_channel(
            channel.id, channel.guild.id, getattr(channel, "name", "?"), ctype,
            getattr(channel, "category_id", None), getattr(channel, "position", 0),
            getattr(channel, "topic", None),
        )
        log_server_event(channel.guild.id, "channel_create", channel.id, None, f"Канал **{getattr(channel, 'name', '?')}** создан")

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        remove_channel(channel.id)
        log_server_event(channel.guild.id, "channel_delete", channel.id, None, f"Канал **{getattr(channel, 'name', '?')}** удалён")

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
        if after.guild is None:
            return
        ctype = type(after).__name__.replace("CategoryChannel", "category").replace("TextChannel", "text").replace("VoiceChannel", "voice").replace("ForumChannel", "forum").replace("StageChannel", "stage").replace("AnnouncementsChannel", "news")
        upsert_channel(
            after.id, after.guild.id, getattr(after, "name", "?"), ctype,
            getattr(after, "category_id", None), getattr(after, "position", 0),
            getattr(after, "topic", None),
        )
        log_server_event(after.guild.id, "channel_update", after.id, None, f"Канал **{getattr(after, 'name', '?')}** обновлён")

    # ---------- Команды ----------

    @app_commands.command(name="sync", description="Принудительно выгрузить данные сервера в БД")
    @app_commands.default_permissions(manage_guild=True)
    async def sync_now(self, interaction: discord.Interaction):
        try:
            await self.sync_guild(interaction.guild)
            await interaction.response.send_message("✅ Данные сервера выгружены в БД.", ephemeral=True)
        except Exception as e:
            log.exception("Ошибка ручного синка: %s", e)
            await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ServerSyncCog(bot))
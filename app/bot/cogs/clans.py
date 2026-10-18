import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from config import GUILD_ID
from cogs.clans_store import (
    COLORS,
    ROLE_KEYS,
    ROLE_LABELS,
    IMAGE_URL,
    INVITE_URL,
    CLAN_LOGO_MAX_BYTES,
    ROLE_ICON_MAX_BYTES,
    CLAN_LOGO_MIMES,
    BACKUP_FORMAT,
    BACKUP_FORMAT_VERSION,
    MAX_BACKUP_BYTES,
    MAX_PAYLOAD_BYTES,
    DATA_DIR,
    STATE_FILE,
    BACKUP_DIR,
    CLAN_SYNC_URL,
    CLAN_SYNC_SECRET,
    CLAN_BACKUP_SECRET,
    CLAN_BACKUP_INTERVAL_HOURS,
    CLAN_BACKUP_RETENTION,
    CLAN_ADMIN_ROLE_ID,
    CLAN_BACKUP_CHANNEL_ID,
    ClanStore,
    BackupManager,
    clean,
    slug,
    detect_image_mime,
    safe_timestamp,
    iso_now,
    blank_state,
    validate_state,
    inspect_discord_image,
    sync_site,
)

log = logging.getLogger("clans")


class ClansCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.store = ClanStore()
        self.backup_manager = BackupManager(self.store)
        self._session: aiohttp.ClientSession | None = None
        self._rate_limits: dict[str, float] = {}
        self._refresh_tasks: dict[str, asyncio.Task] = {}

    async def cog_load(self):
        await self.store.load()
        self._session = aiohttp.ClientSession()
        # Авто-refresh панелей/карточек на старте — чтобы уже настроенные
        # эмбеды подхватились без ручного /clan-setup.
        asyncio.create_task(self._startup_refresh())

    async def _startup_refresh(self):
        await self.bot.wait_until_ready()
        guild = self.bot.get_guild(GUILD_ID)
        if guild is None:
            return
        setup = self.store.state.get("setup") or {}
        # Панель регистрации.
        reg_id = setup.get("registrationChannelId")
        panel_id = setup.get("panelMessageId")
        if reg_id and panel_id:
            try:
                ch = guild.get_channel(int(reg_id))
                if isinstance(ch, discord.TextChannel):
                    msg = await ch.fetch_message(int(panel_id))
                    view = discord.ui.View(timeout=None)
                    view.add_item(discord.ui.Button(custom_id="clan:apply", label="Подать заявку на регистрацию клана", emoji="🛡️", style=discord.ButtonStyle.success))
                    await msg.edit(embed=self._registration_panel(), view=view)
                    log.info("Клановая панель регистрации обновлена на старте")
            except Exception as e:
                log.warning("Не удалось обновить панель регистрации кланов: %s", e)
        # Карточки кланов в списке + агитация.
        for clan in list(self.store.state.get("clans", {}).values()):
            try:
                await self._update_clan_artifacts(guild, clan, sync=False)
            except Exception as e:
                log.warning("Не удалось обновить карточку клана %s: %s", clan.get("name"), e)
        # Синк в сайт — фоном с ретраями: на старте прокси/сайт может подниматься позже бота.
        asyncio.create_task(self._site_sync_with_retries())

    async def _site_sync_with_retries(self, attempts: int = 10, delay: float = 20.0):
        for attempt in range(1, attempts + 1):
            try:
                await sync_site(list(self.store.state.get("clans", {}).values()))
                if attempt > 1:
                    log.info("Синк кланов в сайт выполнен (попытка %d)", attempt)
                return
            except Exception as e:
                if attempt == attempts:
                    log.warning("Site sync on startup failed after %d attempts: %s", attempts, e)
                else:
                    log.warning("Site sync attempt %d/%d failed: %s — retry через %ds", attempt, attempts, e, int(delay))
                    await asyncio.sleep(delay)

    async def _ensure_channel(self, guild: discord.Guild, stored_id, **kwargs):
        if stored_id:
            try:
                existing = await guild.fetch_channel(int(stored_id))
                if existing:
                    return existing
            except Exception:
                pass
        return None

    async def cog_unload(self):
        self.backup_manager.stop()
        if self._session:
            await self._session.close()
        for t in self._refresh_tasks.values():
            t.cancel()

    def _can_run(self, user_id: int, key: str, ms: int = 4000) -> bool:
        rid = f"{user_id}:{key}"
        now = time.monotonic()
        if self._rate_limits.get(rid, 0) > now:
            return False
        self._rate_limits[rid] = now + ms / 1000
        return True

    def _is_admin(self, member: discord.Member) -> bool:
        role_id = self.store.state["setup"].get("adminRoleId") or CLAN_ADMIN_ROLE_ID
        if member.guild_permissions.manage_guild:
            return True
        return bool(role_id and any(r.id == role_id for r in member.roles))

    def _clan_rank(self, member: discord.Member, clan: dict) -> int:
        if self._is_admin(member):
            return 99
        role_ids = clan.get("roleIds", {})
        member_role_ids = {r.id for r in member.roles}
        for rank, key in [(5, "leader"), (4, "deputy"), (3, "officer"), (2, "fighter"), (1, "recruit")]:
            if role_ids.get(key) in member_role_ids:
                return rank
        return 0

    async def _audit(self, guild: discord.Guild, title: str, description: str, color: int = COLORS["dark"]):
        ch_id = self.store.state["setup"].get("logChannelId")
        if not ch_id:
            return
        ch = guild.get_channel(ch_id)
        if not isinstance(ch, discord.TextChannel):
            return
        await ch.send(
            embed=discord.Embed(color=color, title=title, description=description, timestamp=datetime.now(timezone.utc)),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _member_count(self, guild: discord.Guild, clan: dict) -> int:
        role_ids = set(clan.get("roleIds", {}).values())
        count = 0
        for member in guild.members:
            if member.bot:
                continue
            if any(r.id in role_ids for r in member.roles):
                count += 1
        return count

    def _application_embed(self, app: dict, applicant: discord.User) -> discord.Embed:
        e = discord.Embed(
            color=COLORS["red"],
            title=f"Заявка на регистрацию: {app['name']}",
            description=app.get("description", ""),
            timestamp=datetime.fromisoformat(app["createdAt"]) if app.get("createdAt") else None,
        )
        e.add_field(name="Тег", value=f"[{app['tag']}]", inline=True)
        e.add_field(name="Лидер", value=f"{app['leaderName']}\n<@{app['applicantId']}>", inline=True)
        e.add_field(name="Общий состав", value=str(app.get("declaredMembers", 0)), inline=True)
        e.add_field(name="Discord клана", value=app.get("discordUrl", ""))
        e.add_field(name="Логотип", value=f"{(app.get('logoSize', 0) + 1023) // 1024} КБ · {app.get('logoMime', '').replace('image/', '').upper()}", inline=True)
        e.add_field(name="Иконка ролей", value=f"{(app.get('roleIconSize', 0) + 1023) // 1024} КБ · {app.get('roleIconMime', '').replace('image/', '').upper()}", inline=True)
        e.add_field(name="Цели и задачи", value=app.get("goals", ""))
        e.set_footer(text=f"Заявитель: {applicant} · ID {app['applicantId']}")
        if app.get("logoUrl") and IMAGE_URL.match(app["logoUrl"]):
            e.set_thumbnail(url=app["logoUrl"])
        return e

    def _clan_embed(self, clan: dict) -> discord.Embed:
        status = "🟢 Набор открыт" if clan.get("recruitmentStatus") == "open" else "⚫ Набор закрыт"
        color = COLORS["red"] if clan.get("recruitmentStatus") == "open" else COLORS["grey"]
        e = discord.Embed(
            color=color,
            title=clan.get("name", ""),
            description=clan.get("description", ""),
            timestamp=datetime.fromisoformat(clan["updatedAt"]) if clan.get("updatedAt") else None,
        )
        e.set_author(name=f"ПОДРАЗДЕЛЕНИЕ [{clan['tag']}]")
        e.add_field(name="Командир", value=clan.get("leaderName", ""), inline=True)
        e.add_field(name="Бойцов на сервере", value=str(clan.get("serverMembers", 0)), inline=True)
        e.add_field(name="Общий состав", value=str(clan.get("declaredMembers", 0)), inline=True)
        e.add_field(name="Статус", value=status, inline=True)
        e.add_field(name="Discord клана", value=f"[Открыть приглашение]({clan.get('discordUrl', '')})", inline=True)
        e.add_field(name="Цели и задачи", value=clan.get("goals", ""))
        e.set_footer(text="Проверено администрацией WARDOGS RU")
        if clan.get("logoUrl") and IMAGE_URL.match(clan["logoUrl"]):
            e.set_thumbnail(url=clan["logoUrl"])
        return e

    def _recruitment_embed(self, clan: dict) -> discord.Embed:
        r = clan.get("recruitment") or {}
        is_open = clan.get("recruitmentStatus") == "open"
        e = discord.Embed(
            color=COLORS["green"] if is_open else COLORS["grey"],
            title=f"📣 [{clan['tag']}] {clan['name']} — набор {'открыт' if is_open else 'закрыт'}",
            description=clan.get("description", ""),
            timestamp=datetime.fromisoformat(clan["updatedAt"]) if clan.get("updatedAt") else None,
        )
        e.add_field(name="Кого ищем", value=r.get("lookingFor") or "Информация уточняется")
        e.add_field(name="Требования", value=r.get("requirements") or "Информация уточняется")
        e.add_field(name="Прайм-тайм", value=r.get("schedule") or "По договорённости", inline=True)
        e.add_field(name="Контакт", value=r.get("contact") or clan.get("leaderName", ""), inline=True)
        e.add_field(name="Состав", value=f"{clan.get('serverMembers', 0)} на сервере · {clan.get('declaredMembers', 0)} всего", inline=True)
        e.add_field(name="Discord клана", value=f"[Присоединиться]({clan.get('discordUrl', '')})")
        e.set_footer(text="Официальная карточка набора WARDOGS RU · одна публикация на клан")
        if r.get("imageUrl") and IMAGE_URL.match(r["imageUrl"]):
            e.set_image(url=r["imageUrl"])
        if clan.get("logoUrl") and IMAGE_URL.match(clan["logoUrl"]):
            e.set_thumbnail(url=clan["logoUrl"])
        return e

    def _registration_panel(self):
        e = discord.Embed(
            color=COLORS["red"],
            title="РЕГИСТРАЦИЯ КЛАНА",
            description="Создайте заявку на добавление клана в WARDOGS RU. После заполнения откроется закрытый тикет, а инфраструктура клана будет создана только после ручного подтверждения администратора.",
        )
        e.add_field(name="Понадобится", value="Название и тег · ник лидера · число участников · Discord-ссылка · описание · цели · логотип клана · отдельная иконка ролей PNG/JPG/WebP до 256 КБ")
        e.set_footer(text="Один пользователь может вести одну активную заявку")
        return e

    def _clan_control_row(self, clan_id: str):
        return discord.ui.View(timeout=None).add_item(
            discord.ui.Button(custom_id=f"recruit:create:{clan_id}", label="Создать набор", emoji="📣", style=discord.ButtonStyle.danger)
        )

    def _hq_panel(self, clan: dict, restored: bool = False):
        cid = clan["id"]
        e = discord.Embed(
            color=COLORS["red"],
            title=f"ШТАБ КЛАНА [{clan['tag']}]",
            description=(
                "Штаб восстановлен. Закрытая панель руководства снова управляет участниками, кланом и набором."
                if restored
                else "Закрытая панель руководства: управление участниками, составом, логотипом, синхронизацией и официальной карточкой набора."
            ),
        )
        e.add_field(name="Голосовые комнаты", value="Зайдите в «➕・Создать комнату». Для настройки используйте `/voice name`, `/voice limit`, `/voice lock` и `/voice unlock`.")
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(custom_id=f"recruit:create:{cid}", label="Создать набор", emoji="📣", style=discord.ButtonStyle.danger))
        view.add_item(discord.ui.Button(custom_id=f"recruit:edit:{cid}", label="Изменить", style=discord.ButtonStyle.secondary))
        view.add_item(discord.ui.Button(custom_id=f"recruit:toggle:{cid}", label="Открыть / закрыть", style=discord.ButtonStyle.secondary))
        view.add_item(discord.ui.Button(custom_id=f"recruit:delete:{cid}", label="Удалить карточку", style=discord.ButtonStyle.secondary))
        return e, view

    def _hq_panel_full(self, clan: dict, restored: bool = False):
        e, _ = self._hq_panel(clan, restored)
        cid = clan["id"]
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(custom_id=f"recruit:create:{cid}", label="Создать набор", emoji="📣", style=discord.ButtonStyle.danger))
        view.add_item(discord.ui.Button(custom_id=f"recruit:edit:{cid}", label="Изменить", style=discord.ButtonStyle.secondary))
        view.add_item(discord.ui.Button(custom_id=f"recruit:toggle:{cid}", label="Открыть / закрыть", style=discord.ButtonStyle.secondary))
        view.add_item(discord.ui.Button(custom_id=f"recruit:delete:{cid}", label="Удалить карточку", style=discord.ButtonStyle.secondary))
        view2 = discord.ui.View(timeout=None)
        view2.add_item(discord.ui.Button(custom_id=f"manage:members:{cid}", label="Участники", emoji="👥", style=discord.ButtonStyle.primary))
        view2.add_item(discord.ui.Button(custom_id=f"manage:count:{cid}", label="Состав", style=discord.ButtonStyle.secondary))
        view2.add_item(discord.ui.Button(custom_id=f"manage:sync:{cid}", label="Обновить", style=discord.ButtonStyle.secondary))
        view2.add_item(discord.ui.Button(custom_id=f"manage:logo:{cid}", label="Логотип", style=discord.ButtonStyle.secondary))
        view2.add_item(discord.ui.Button(custom_id=f"manage:dissolve:{cid}", label="Распустить", style=discord.ButtonStyle.danger))
        return e, [view, view2]

    def _recruitment_application_row(self, clan: dict):
        view = discord.ui.View(timeout=None)
        is_open = clan.get("recruitmentStatus") == "open"
        view.add_item(discord.ui.Button(
            custom_id=f"join:apply:{clan['id']}",
            label="Подать заявку в клан" if is_open else "Набор закрыт",
            emoji="📨", style=discord.ButtonStyle.success, disabled=not is_open,
        ))
        return view

    async def _update_clan_artifacts(self, guild: discord.Guild, clan: dict, sync: bool = True):
        clan["serverMembers"] = await self._member_count(guild, clan)
        clan["updatedAt"] = iso_now()
        setup = self.store.state["setup"]
        list_id = setup.get("listChannelId")
        if list_id:
            list_ch = guild.get_channel(list_id)
            if isinstance(list_ch, discord.TextChannel):
                old = None
                if clan.get("listMessageId"):
                    try:
                        old = await list_ch.fetch_message(int(clan["listMessageId"]))
                    except Exception:
                        old = None
                if old:
                    await old.edit(embed=self._clan_embed(clan))
                else:
                    msg = await list_ch.send(embed=self._clan_embed(clan), allowed_mentions=discord.AllowedMentions.none())
                    clan["listMessageId"] = str(msg.id)
        if clan.get("recruitmentThreadId"):
            try:
                thread = await guild.fetch_channel(int(clan["recruitmentThreadId"]))
            except Exception:
                thread = None
            if isinstance(thread, discord.Thread):
                try:
                    starter = await thread.fetch_starter_message()
                    if starter:
                        await starter.edit(embed=self._recruitment_embed(clan), view=self._recruitment_application_row(clan))
                except Exception:
                    pass
        await self.store.update(lambda s: s["clans"].__setitem__(clan["id"], clan))
        if sync:
            try:
                await sync_site(list(self.store.state["clans"].values()))
            except Exception as e:
                log.warning("Site sync failed: %s", e)

    def _schedule_clan_refresh(self, guild: discord.Guild, clan_id: str):
        if clan_id in self._refresh_tasks and not self._refresh_tasks[clan_id].done():
            self._refresh_tasks[clan_id].cancel()
        async def _do():
            await asyncio.sleep(2.5)
            clan = self.store.state["clans"].get(clan_id)
            if clan:
                try:
                    await self._update_clan_artifacts(guild, clan)
                except Exception as e:
                    log.error("Clan refresh failed: %s", e)
        self._refresh_tasks[clan_id] = asyncio.create_task(_do())

    async def _position_clan_roles(self, guild: discord.Guild, role_ids: dict):
        anchor_id = self.store.state["setup"].get("roleAnchorId")
        anchor = guild.get_role(int(anchor_id)) if anchor_id else None
        if not anchor:
            raise RuntimeError("Опорная роль для клановых ролей не найдена. Повторите /clan-setup.")
        if anchor.position >= guild.me.roles.highest.position:
            raise RuntimeError("Опорная роль должна находиться ниже роли бота.")
        for key in reversed(ROLE_KEYS):
            rid = role_ids.get(key)
            if not rid:
                continue
            role = guild.get_role(int(rid))
            if not role:
                continue
            current_anchor = guild.get_role(int(anchor.id)) or anchor
            await role.edit(position=max(1, current_anchor.position - 1), reason="WARDOGS clan hierarchy")

    def _clan_category_overwrites(self, guild: discord.Guild, role_ids: dict):
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, connect=True, speak=True, manage_channels=True, move_members=True),
        }
        admin_id = self.store.state["setup"].get("adminRoleId") or CLAN_ADMIN_ROLE_ID
        if admin_id:
            role = guild.get_role(int(admin_id))
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, connect=True, speak=True)
        for key in ROLE_KEYS:
            rid = role_ids.get(key)
            if rid:
                role = guild.get_role(int(rid))
                if role:
                    overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, connect=True, speak=True)
        return overwrites

    def _clan_hq_overwrites(self, guild: discord.Guild, role_ids: dict):
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True),
        }
        admin_id = self.store.state["setup"].get("adminRoleId") or CLAN_ADMIN_ROLE_ID
        if admin_id:
            role = guild.get_role(int(admin_id))
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        for key in ["leader", "deputy", "officer"]:
            rid = role_ids.get(key)
            if rid:
                role = guild.get_role(int(rid))
                if role:
                    overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        for key in ["fighter", "recruit"]:
            rid = role_ids.get(key)
            if rid:
                role = guild.get_role(int(rid))
                if role:
                    overwrites[role] = discord.PermissionOverwrite(view_channel=False)
        return overwrites

    def _join_ticket_overwrites(self, guild: discord.Guild, clan: dict, applicant_id: int):
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.get_member(applicant_id): discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True) if guild.get_member(applicant_id) else discord.PermissionOverwrite(),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True, embed_links=True),
        }
        admin_id = self.store.state["setup"].get("adminRoleId") or CLAN_ADMIN_ROLE_ID
        if admin_id:
            role = guild.get_role(int(admin_id))
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        for key in ["leader", "deputy", "officer"]:
            rid = clan.get("roleIds", {}).get(key)
            if rid:
                role = guild.get_role(int(rid))
                if role:
                    overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        for key in ["fighter", "recruit"]:
            rid = clan.get("roleIds", {}).get(key)
            if rid:
                role = guild.get_role(int(rid))
                if role:
                    overwrites[role] = discord.PermissionOverwrite(view_channel=False)
        return overwrites

    async def _upgrade_clan_infrastructure(self, guild: discord.Guild, clan: dict):
        try:
            category = await guild.fetch_channel(int(clan["channelIds"]["category"]))
        except Exception:
            category = None
        if not isinstance(category, discord.CategoryChannel):
            raise RuntimeError(f"Категория клана [{clan['tag']}] не найдена.")
        await category.edit(overwrites=self._clan_category_overwrites(guild, clan["roleIds"]), reason=f"WARDOGS clan permissions: {clan['name']}")
        chat = None
        if clan["channelIds"].get("chat"):
            try:
                chat = await guild.fetch_channel(int(clan["channelIds"]["chat"]))
            except Exception:
                chat = None
        if not isinstance(chat, discord.TextChannel):
            chat = await guild.create_text_channel("💬・общий-чат", category=category, topic="Общий чат всех участников клана.", reason=f"WARDOGS clan upgrade: {clan['name']}")
        clan["channelIds"]["chat"] = str(chat.id)
        try:
            hq = await guild.fetch_channel(int(clan["channelIds"]["hq"]))
        except Exception:
            hq = None
        if not isinstance(hq, discord.TextChannel):
            raise RuntimeError(f"Штаб клана [{clan['tag']}] не найден.")
        await hq.edit(overwrites=self._clan_hq_overwrites(guild, clan["roleIds"]), reason=f"WARDOGS headquarters permissions: {clan['name']}")
        panel = None
        if clan.get("controlMessageId"):
            try:
                panel = await hq.fetch_message(int(clan["controlMessageId"]))
            except Exception:
                panel = None
        if not panel:
            async for msg in hq.history(limit=50):
                if msg.author == self.bot.user and any(
                    comp.custom_id and comp.custom_id.startswith("recruit:create:")
                    for row in msg.components for comp in row.children
                ):
                    panel = msg
                    break
        e, views = self._hq_panel_full(clan)
        if panel:
            await panel.edit(embed=e, view=views[0])
        else:
            panel = await hq.send(embed=e, view=views[0], allowed_mentions=discord.AllowedMentions.none())
        clan["controlMessageId"] = str(panel.id)

    async def _run_setup(self, interaction: discord.Interaction):
        member = interaction.user
        if not isinstance(member, discord.Member) or not member.guild_permissions.manage_guild or not member.guild_permissions.manage_channels:
            await interaction.response.send_message("Нужны права «Управлять сервером» и «Управлять каналами».", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        me = guild.me
        required = ["manage_channels", "manage_roles", "move_members", "send_messages", "embed_links", "attach_files", "manage_threads"]
        if not all(getattr(me.guild_permissions, p, False) for p in required):
            await interaction.followup.send("Боту не хватает прав: Manage Channels, Manage Roles, Move Members, Send Messages, Embed Links, Attach Files и Manage Threads.")
            return
        admin_role = interaction.namespace.admin_role
        role_anchor = interaction.namespace.role_anchor
        if role_anchor.id == guild.default_role.id or role_anchor.managed:
            await interaction.followup.send("Выберите обычную опорную роль, а не @everyone или роль интеграции.")
            return
        if role_anchor.position >= me.roles.highest.position:
            await interaction.followup.send("Опорная роль должна находиться ниже роли бота. Сначала поднимите роль бота выше неё.")
            return
        previous = self.store.state["setup"]
        system_category = await self._ensure_channel(guild, previous.get("systemCategoryId"), name="══ КЛАНОВАЯ СИСТЕМА ══", category=None)
        if not isinstance(system_category, discord.CategoryChannel):
            system_category = await guild.create_category("══ КЛАНОВАЯ СИСТЕМА ══")
        ticket_category = await self._ensure_channel(guild, previous.get("ticketCategoryId"), name="══ ЗАЯВКИ КЛАНОВ ══")
        if not isinstance(ticket_category, discord.CategoryChannel):
            ticket_category = await guild.create_category("══ ЗАЯВКИ КЛАНОВ ══")
        ticket_overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            admin_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True),
        }
        await ticket_category.edit(overwrites=ticket_overwrites)
        public_overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=True, read_message_history=True, send_messages=False),
            me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, embed_links=True, manage_messages=True),
        }
        registration = await self._ensure_channel(guild, previous.get("registrationChannelId"), name="🛡️・регистрация-кланов", category=system_category, overwrites=public_overwrites)
        if not isinstance(registration, discord.TextChannel):
            registration = await guild.create_text_channel("🛡️・регистрация-кланов", category=system_category, overwrites=public_overwrites)
        list_ch = await self._ensure_channel(guild, previous.get("listChannelId"), name="📜・список-кланов", category=system_category, overwrites=public_overwrites)
        if not isinstance(list_ch, discord.TextChannel):
            list_ch = await guild.create_text_channel("📜・список-кланов", category=system_category, overwrites=public_overwrites)
        forum_overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=True, read_message_history=True, send_messages=False, create_public_threads=False, send_messages_in_threads=True),
            me: discord.PermissionOverwrite(view_channel=True, send_messages=True, send_messages_in_threads=True, create_public_threads=True, manage_threads=True, embed_links=True),
        }
        try:
            forum = await guild.fetch_channel(int(previous.get("recruitmentForumId") or 0))
        except Exception:
            forum = None
        if not isinstance(forum, discord.ForumChannel):
            forum_tags = [
                discord.ForumTag(name="Набор открыт", emoji="🟢"),
                discord.ForumTag(name="Набор закрыт", emoji="⚫"),
                discord.ForumTag(name="Соревновательный", emoji="⚔️"),
                discord.ForumTag(name="Свободная игра", emoji="🎮"),
            ]
            forum = await guild.create_forum(
                "📢・агитация-кланов", category=system_category,
                topic="Единый форум набора. Официальную публикацию на клан создаёт бот.",
                available_tags=forum_tags, overwrites=forum_overwrites,
            )
        log_overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            admin_role: discord.PermissionOverwrite(view_channel=True, read_message_history=True),
            me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, embed_links=True),
        }
        logs = await self._ensure_channel(guild, previous.get("logChannelId"), name="🔒・журнал-кланов", category=system_category, overwrites=log_overwrites)
        if not isinstance(logs, discord.TextChannel):
            logs = await guild.create_text_channel("🔒・журнал-кланов", category=system_category, overwrites=log_overwrites)
        backup_overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            admin_role: discord.PermissionOverwrite(view_channel=True, read_message_history=True),
            me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, embed_links=True, attach_files=True, manage_messages=True),
        }
        backup_ch = await self._ensure_channel(guild, previous.get("backupChannelId") or CLAN_BACKUP_CHANNEL_ID, name="🔐・резервные-копии", category=system_category, overwrites=backup_overwrites)
        if not isinstance(backup_ch, discord.TextChannel):
            backup_ch = await guild.create_text_channel("🔐・резервные-копии", category=system_category, topic="Зашифрованные автоматические копии данных клановой системы. Доступ только администрации.", overwrites=backup_overwrites)
        panel_msg = None
        if previous.get("panelMessageId"):
            try:
                panel_msg = await registration.fetch_message(int(previous["panelMessageId"]))
            except Exception:
                panel_msg = None
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(custom_id="clan:apply", label="Подать заявку", emoji="🛡️", style=discord.ButtonStyle.danger))
        if panel_msg:
            await panel_msg.edit(embed=self._registration_panel(), view=view)
        else:
            panel_msg = await registration.send(embed=self._registration_panel(), view=view, allowed_mentions=discord.AllowedMentions.none())
        await self.store.update(lambda s: s.__setitem__("setup", {
            "adminRoleId": admin_role.id,
            "systemCategoryId": system_category.id,
            "ticketCategoryId": ticket_category.id,
            "registrationChannelId": registration.id,
            "listChannelId": list_ch.id,
            "recruitmentForumId": forum.id,
            "logChannelId": logs.id,
            "backupChannelId": backup_ch.id,
            "panelMessageId": panel_msg.id,
            "roleAnchorId": role_anchor.id,
        }))
        for clan in list(self.store.state["clans"].values()):
            try:
                await self._position_clan_roles(guild, clan["roleIds"])
                await self._upgrade_clan_infrastructure(guild, clan)
            except Exception as e:
                log.error("Upgrade failed for %s: %s", clan.get("name"), e)
        self.backup_manager.start(guild)
        if self.backup_manager.enabled:
            try:
                await self.backup_manager.create(guild, "setup")
            except Exception as e:
                log.error("Initial backup failed: %s", e)
        try:
            await sync_site(list(self.store.state["clans"].values()))
        except Exception:
            pass
        await interaction.followup.send(f"Готово. Система настроена. Администраторская роль: {admin_role.mention}. Клановые роли будут размещаться сразу под ролью {role_anchor.mention}.")

    def _first_application_modal(self):
        modal = discord.ui.Modal(custom_id="clan:apply:first", title="Заявка на клан · 1 из 2")
        modal.add_item(discord.ui.TextInput(label="1. Название клана", min_length=2, max_length=80, required=True))
        modal.add_item(discord.ui.TextInput(label="2. Тег клана", placeholder="Например: WDR", min_length=2, max_length=16, required=True))
        modal.add_item(discord.ui.TextInput(label="3. Ник лидера", min_length=2, max_length=80, required=True))
        modal.add_item(discord.ui.TextInput(label="4. Участников всего", placeholder="Число, включая людей вне WARDOGS RU", max_length=6, required=True))
        return modal

    def _second_application_modal(self, user_id: int):
        modal = discord.ui.Modal(custom_id=f"clan:apply:second:{user_id}", title="Заявка на клан · 2 из 2")
        modal.add_item(discord.ui.TextInput(label="5. Ссылка на Discord клана", max_length=120, required=True))
        modal.add_item(discord.ui.TextInput(label="6. Кратко о клане", style=discord.TextStyle.paragraph, min_length=20, max_length=600, required=True))
        modal.add_item(discord.ui.TextInput(label="7. Цели и задачи клана", style=discord.TextStyle.paragraph, min_length=10, max_length=600, required=True))
        modal.add_item(discord.ui.TextInput(label="8. Ссылка на логотип клана Discord", placeholder="PNG, JPG/JPEG или WebP", max_length=500, required=True))
        modal.add_item(discord.ui.TextInput(label="9. Иконка ролей Discord (до 256 КБ)", placeholder="Отдельный PNG, JPG/JPEG или WebP", max_length=500, required=True))
        return modal

    async def _submit_first(self, interaction: discord.Interaction):
        fields = interaction.data.get("components", [])
        values = {}
        for comp in fields:
            for row in comp:
                if row.get("type") == 1:
                    for item in row:
                        if item.get("type") == 4:
                            values[item.get("custom_id")] = item.get("value", "")
        member_count_str = clean(values.get("members", ""), 6)
        tag = clean(values.get("tag", ""), 16)
        tag = re.sub(r"[^\w\-]", "", tag)
        try:
            member_count = int(member_count_str)
        except ValueError:
            member_count = 0
        if member_count < 1 or member_count > 100000:
            await interaction.response.send_message("Количество участников должно быть целым числом от 1 до 100000.", ephemeral=True)
            return
        if len(tag) < 2:
            await interaction.response.send_message("Тег должен содержать 2–16 букв, цифр, дефисов или подчёркиваний.", ephemeral=True)
            return
        name = clean(values.get("name", ""), 80)
        for clan in self.store.state["clans"].values():
            if clan.get("tag", "").lower() == tag.lower() or clan.get("name", "").lower() == name.lower():
                await interaction.response.send_message("Клан с таким названием или тегом уже зарегистрирован.", ephemeral=True)
                return
        await self.store.update(lambda s: s["drafts"].__setitem__(str(interaction.user.id), {
            "name": name, "tag": tag,
            "leaderName": clean(values.get("leader", ""), 80),
            "declaredMembers": member_count, "createdAt": iso_now(),
        }))
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(custom_id=f"clan:apply:continue:{interaction.user.id}", label="Продолжить заявку", style=discord.ButtonStyle.danger))
        await interaction.response.send_message("Первая часть сохранена. Нажмите кнопку и заполните оставшиеся три пункта.", view=view, ephemeral=True)

    async def _submit_second(self, interaction: discord.Interaction, user_id: str):
        if interaction.user.id != int(user_id):
            await interaction.response.send_message("Эта форма принадлежит другому пользователю.", ephemeral=True)
            return
        draft = self.store.state["drafts"].get(user_id)
        if not draft or (datetime.now(timezone.utc) - datetime.fromisoformat(draft["createdAt"])).total_seconds() > 3600:
            await interaction.response.send_message("Черновик истёк. Начните заявку заново.", ephemeral=True)
            return
        fields = interaction.data.get("components", [])
        values = {}
        for comp in fields:
            for row in comp:
                if row.get("type") == 1:
                    for item in row:
                        if item.get("type") == 4:
                            values[item.get("custom_id")] = item.get("value", "")
        discord_url = clean(values.get("discord", ""), 120)
        if not INVITE_URL.match(discord_url):
            await interaction.response.send_message("Укажите полную ссылку вида https://discord.gg/код.", ephemeral=True)
            return
        guild = interaction.guild
        admin_role_id = self.store.state["setup"].get("adminRoleId")
        category_id = self.store.state["setup"].get("ticketCategoryId")
        if not admin_role_id or not category_id:
            await interaction.response.send_message("Клановая система ещё не настроена администратором.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            logo = await inspect_discord_image(self._session, values.get("logo", ""), CLAN_LOGO_MAX_BYTES, "Логотип клана")
            role_icon = await inspect_discord_image(self._session, values.get("role-icon", ""), ROLE_ICON_MAX_BYTES, "Иконка ролей")
        except ValueError as e:
            await interaction.followup.send(f"Изображение не принято: {clean(str(e), 300)}")
            return
        ticket = await guild.create_text_channel(
            f"заявка-{slug(draft['tag'], 20)}-{str(interaction.user.id)[-4:]}",
            category=discord.Object(id=int(category_id)),
            overwrites=self._join_ticket_overwrites(guild, {"roleIds": {}}, interaction.user.id),
            reason=f"Clan application from {interaction.user}",
        )
        app = {
            **draft,
            "applicantId": interaction.user.id, "discordUrl": discord_url,
            "logoUrl": logo["url"], "logoHash": logo["hash"], "logoMime": logo["mime"], "logoSize": logo["size"],
            "roleIconUrl": role_icon["url"], "roleIconHash": role_icon["hash"], "roleIconMime": role_icon["mime"],
            "roleIconSize": role_icon["size"], "roleIconData": base64.b64encode(role_icon["bytes"]).decode(),
            "description": clean(values.get("description", ""), 600),
            "goals": clean(values.get("goals", ""), 600),
            "ticketId": str(ticket.id), "createdAt": iso_now(), "status": "pending",
        }
        await self.store.update(lambda s: (
            s["applications"].__setitem__(str(ticket.id), app),
            s["drafts"].pop(user_id, None),
        ))
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(custom_id=f"clan:approve:{ticket.id}", label="Одобрить", emoji="✅", style=discord.ButtonStyle.success))
        view.add_item(discord.ui.Button(custom_id=f"clan:reject:{ticket.id}", label="Отклонить", emoji="✖️", style=discord.ButtonStyle.danger))
        admin_role = guild.get_role(int(admin_role_id))
        await ticket.send(
            content=f"<@&{admin_role_id}>" if admin_role else "",
            embed=self._application_embed(app, interaction.user),
            view=view,
            allowed_mentions=discord.AllowedMentions(roles=[int(admin_role_id)] if admin_role else []),
        )
        await interaction.followup.send(f"Заявка создана: {ticket.mention}. Ожидайте решения администрации.")
        await self._audit(guild, "Новая заявка на клан", f"{app['name']} [{app['tag']}] · заявитель <@{app['applicantId']}>", COLORS["red"])

    async def _create_clan(self, interaction: discord.Interaction, ticket_id: str):
        if not isinstance(interaction.user, discord.Member) or not self._is_admin(interaction.user):
            await interaction.response.send_message("Подтверждать заявки может только администрация.", ephemeral=True)
            return
        app = self.store.state["applications"].get(ticket_id)
        if not app or app.get("status") != "pending":
            await interaction.response.send_message("Заявка уже обработана или не найдена.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        try:
            member = await guild.fetch_member(int(app["applicantId"]))
        except Exception:
            member = None
        if not member:
            await interaction.followup.send("Заявитель вышел с сервера. Одобрение отменено.")
            return
        for clan in self.store.state["clans"].values():
            if clan.get("tag", "").lower() == app["tag"].lower():
                await interaction.followup.send("Клан с таким тегом уже существует.")
                return
        if "ROLE_ICONS" not in [str(f) for f in guild.features]:
            await interaction.followup.send("Discord не разрешает иконки ролей на этом сервере: отсутствует функция ROLE_ICONS. Клан не создан, роли не создавались.")
            return
        try:
            icon_bytes = base64.b64decode(app["roleIconData"])
            if detect_image_mime(icon_bytes) != app.get("roleIconMime") or hashlib.sha256(icon_bytes).hexdigest() != app.get("roleIconHash"):
                raise ValueError("Контрольная сумма иконки ролей не совпала.")
            if len(icon_bytes) > ROLE_ICON_MAX_BYTES:
                raise ValueError("Сохранённая иконка ролей превышает 256 КБ.")
        except Exception as e:
            await interaction.followup.send(f"Не удалось подготовить иконку ролей: {clean(str(e), 300)}")
            return
        roles = {}
        try:
            for key in reversed(ROLE_KEYS):
                role_name = f"{app['name'][:78 - len(ROLE_LABELS[key])]} {ROLE_LABELS[key]}"
                role = await guild.create_role(
                    name=role_name,
                    color=discord.Color(COLORS["red"]) if key == "leader" else discord.Color(COLORS["dark"]),
                    hoist=(key == "leader"), mentionable=False,
                    reason=f"Approved clan {app['name']}",
                )
                try:
                    await role.edit(icon=icon_bytes, reason=f"Approved clan {app['name']}")
                except Exception:
                    pass
                roles[key] = role.id
            await self._position_clan_roles(guild, roles)
        except Exception as e:
            for rid in roles.values():
                try:
                    r = guild.get_role(rid)
                    if r:
                        await r.delete(reason="Rollback after failed clan role creation")
                except Exception:
                    pass
            await interaction.followup.send(f"Клан не создан: Discord отклонил создание ролей или их иконку. Созданные на этом шаге роли удалены. {clean(str(e), 220)}")
            return
        category_overwrites = self._clan_category_overwrites(guild, roles)
        category = await guild.create_category(f"[{app['tag']}] {app['name']}"[:100], overwrites=category_overwrites, reason=f"Approved clan {app['name']}")
        chat = await guild.create_text_channel("💬・общий-чат", category=category, topic="Общий чат всех участников клана.")
        hq = await guild.create_text_channel(
            "⚔️・штаб-клана", category=category,
            topic="Закрытое управление кланом, участниками и набором.",
            overwrites=self._clan_hq_overwrites(guild, roles),
        )
        voice = await guild.create_voice_channel("🔊・Общий штаб", category=category)
        creator = await guild.create_voice_channel("➕・Создать комнату", category=category, user_limit=1)
        await member.add_roles(discord.Object(id=roles["leader"]), reason=f"Clan {app['name']} approved")
        now = iso_now()
        clan = {
            "id": str(category.id),
            "name": app["name"], "tag": app["tag"],
            "leaderId": member.id, "leaderName": app["leaderName"],
            "declaredMembers": app["declaredMembers"], "serverMembers": 1,
            "discordUrl": app["discordUrl"],
            "logoUrl": app["logoUrl"], "logoHash": app["logoHash"], "logoMime": app["logoMime"], "logoSize": app["logoSize"],
            "roleIconUrl": app["roleIconUrl"], "roleIconHash": app["roleIconHash"], "roleIconMime": app["roleIconMime"],
            "roleIconSize": app["roleIconSize"], "roleIconData": app["roleIconData"],
            "description": app["description"], "goals": app["goals"],
            "roleIds": roles,
            "channelIds": {"category": str(category.id), "chat": str(chat.id), "hq": str(hq.id), "voice": str(voice.id), "creator": str(creator.id)},
            "recruitmentStatus": "closed", "recruitmentThreadId": None, "recruitmentUrl": None, "recruitment": None,
            "listMessageId": None, "createdAt": now, "updatedAt": now,
        }
        await self.store.update(lambda s: (
            s["clans"].__setitem__(clan["id"], clan),
            s["applications"][ticket_id].__setitem__("status", "approved"),
        ))
        e, views = self._hq_panel_full(clan)
        panel = await hq.send(embed=e, view=views[0], allowed_mentions=discord.AllowedMentions.none())
        clan["controlMessageId"] = str(panel.id)
        await self.store.update(lambda s: s["clans"].__setitem__(clan["id"], clan))
        await self._update_clan_artifacts(guild, clan)
        await self._audit(guild, "Клан зарегистрирован", f"{clan['name']} [{clan['tag']}] · лидер <@{clan['leaderId']}> · одобрил <@{interaction.user.id}>", COLORS["green"])
        try:
            await member.send(f"Ваш клан «{clan['name']}» одобрен в WARDOGS RU. Закрытый штаб создан на сервере.")
        except Exception:
            pass
        await interaction.followup.send(f"Клан «{clan['name']}» создан. Лидеру выдана роль, карточка добавлена в список.")
        asyncio.get_event_loop().call_later(10, lambda: asyncio.ensure_future(self._delete_channel(guild, ticket_id)))

    async def _delete_channel(self, guild: discord.Guild, channel_id: str):
        try:
            ch = await guild.fetch_channel(int(channel_id))
            await ch.delete(reason="Archived clan application")
        except Exception:
            pass

    def _recruitment_modal(self, clan: dict, mode: str):
        r = clan.get("recruitment") or {}
        modal = discord.ui.Modal(
            custom_id=f"recruit:submit:{mode}:{clan['id']}",
            title="Карточка набора" if mode == "create" else "Изменить набор",
        )
        modal.add_item(discord.ui.TextInput(label="Кого ищем", style=discord.TextStyle.paragraph, max_length=500, default=r.get("lookingFor", "") or None, required=True))
        modal.add_item(discord.ui.TextInput(label="Требования", style=discord.TextStyle.paragraph, max_length=500, default=r.get("requirements", "") or None, required=True))
        modal.add_item(discord.ui.TextInput(label="Прайм-тайм", max_length=100, default=r.get("schedule", "") or None, required=True))
        modal.add_item(discord.ui.TextInput(label="Контакт", max_length=100, default=r.get("contact") or clan.get("leaderName", ""), required=True))
        modal.add_item(discord.ui.TextInput(label="Ссылка на изображение Discord (необяз.)", max_length=300, default=r.get("imageUrl", "") or None, required=False))
        return modal

    async def _submit_recruitment(self, interaction: discord.Interaction, mode: str, clan_id: str):
        clan = self.store.state["clans"].get(clan_id)
        if not clan or self._clan_rank(interaction.user, clan) < 3:
            await interaction.response.send_message("Карточкой набора управляют лидер, заместитель, офицер или администратор.", ephemeral=True)
            return
        if mode == "create" and clan.get("recruitmentThreadId"):
            await interaction.response.send_message("У клана уже есть публикация. Используйте «Изменить».", ephemeral=True)
            return
        fields = interaction.data.get("components", [])
        values = {}
        for comp in fields:
            for row in comp:
                if row.get("type") == 1:
                    for item in row:
                        if item.get("type") == 4:
                            values[item.get("custom_id")] = item.get("value", "")
        image_url = clean(values.get("image", ""), 300)
        if image_url and not IMAGE_URL.match(image_url):
            await interaction.response.send_message("Изображение должно быть загружено в Discord. Скопируйте ссылку на вложение с cdn.discordapp.com или media.discordapp.net.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        clan["recruitment"] = {
            "lookingFor": clean(values.get("looking", ""), 500),
            "requirements": clean(values.get("requirements", ""), 500),
            "schedule": clean(values.get("schedule", ""), 100),
            "contact": clean(values.get("contact", ""), 100),
            "imageUrl": image_url,
        }
        clan["recruitmentStatus"] = "open"
        clan["updatedAt"] = iso_now()
        try:
            forum = await interaction.guild.fetch_channel(int(self.store.state["setup"]["recruitmentForumId"]))
        except Exception:
            forum = None
        if not isinstance(forum, discord.ForumChannel):
            await interaction.followup.send("Форум набора не найден. Администратору нужно повторить `/clan-setup`.")
            return
        if not clan.get("recruitmentThreadId"):
            open_tag = None
            for tag in forum.available_tags:
                if tag.name == "Набор открыт":
                    open_tag = tag
                    break
            thread_name = f"[{clan['tag']}] {clan['name']} — набор открыт"[:100]
            e = self._recruitment_embed(clan)
            view = self._recruitment_application_row(clan)
            thread = await forum.create_thread(
                name=thread_name, auto_archive_duration=10080,
                applied_tags=[open_tag] if open_tag else [],
                content="",
                embed=e,
                view=view,
                reason=f"Recruitment post for {clan['name']}",
            )
            clan["recruitmentThreadId"] = str(thread[0].id if isinstance(thread, tuple) else thread.id)
            clan["recruitmentUrl"] = f"https://discord.com/channels/{GUILD_ID}/{forum.id}/{clan['recruitmentThreadId']}"
        else:
            try:
                thread = await interaction.guild.fetch_channel(int(clan["recruitmentThreadId"]))
            except Exception:
                thread = None
            if not isinstance(thread, discord.Thread):
                clan["recruitmentThreadId"] = None
                clan["recruitmentUrl"] = None
                await interaction.followup.send("Старая публикация была удалена. Нажмите «Создать набор» ещё раз.")
                return
            await thread.edit(archived=False, locked=False)
            await thread.edit(name=f"[{clan['tag']}] {clan['name']} — набор открыт"[:100])
        await self._update_clan_artifacts(interaction.guild, clan)
        await self._audit(interaction.guild, "Создана карточка набора" if mode == "create" else "Изменена карточка набора", f"{clan['name']} [{clan['tag']}] · автор <@{interaction.user.id}>", COLORS["red"])
        await interaction.followup.send(f"Карточка набора сохранена: {clan['recruitmentUrl']}")

    async def _toggle_recruitment(self, interaction: discord.Interaction, clan: dict):
        if not clan.get("recruitmentThreadId"):
            await interaction.response.send_message("Сначала создайте карточку набора.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            thread = await interaction.guild.fetch_channel(int(clan["recruitmentThreadId"]))
        except Exception:
            thread = None
        if not isinstance(thread, discord.Thread):
            await interaction.followup.send("Публикация не найдена.")
            return
        clan["recruitmentStatus"] = "closed" if clan.get("recruitmentStatus") == "open" else "open"
        is_open = clan["recruitmentStatus"] == "open"
        tag_name = "Набор открыт" if is_open else "Набор закрыт"
        status_tag = None
        other_tags = []
        forum = thread.parent
        if forum and isinstance(forum, discord.ForumChannel):
            for tag in forum.available_tags:
                if tag.name == tag_name:
                    status_tag = tag
                    break
            status_names = {"Набор открыт", "Набор закрыт"}
            other_tags = [t for t in thread.applied_tags if t.name not in status_names]
        if is_open:
            await thread.edit(archived=False, locked=False)
        new_tags = ([status_tag] if status_tag else []) + other_tags
        await thread.edit(applied_tags=new_tags[:5])
        await thread.edit(name=f"[{clan['tag']}] {clan['name']} — набор {'открыт' if is_open else 'закрыт'}"[:100])
        await self._update_clan_artifacts(interaction.guild, clan)
        await thread.edit(locked=not is_open, archived=not is_open)
        await interaction.followup.send(f"Набор {'открыт' if is_open else 'закрыт'}.")

    async def _delete_recruitment(self, interaction: discord.Interaction, clan: dict):
        if not clan.get("recruitmentThreadId"):
            await interaction.response.send_message("У клана нет карточки набора.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        thread_id = clan["recruitmentThreadId"]
        clan["recruitmentThreadId"] = None
        clan["recruitmentUrl"] = None
        clan["recruitment"] = None
        clan["recruitmentStatus"] = "closed"
        try:
            thread = await interaction.guild.fetch_channel(int(thread_id))
            await thread.delete(reason=f"Recruitment post deleted by {interaction.user}")
        except Exception:
            pass
        await self._update_clan_artifacts(interaction.guild, clan)
        await interaction.followup.send("Карточка набора удалена. Теперь можно создать новую.")

    def _join_application_modal(self, clan: dict):
        modal = discord.ui.Modal(custom_id=f"join:submit:{clan['id']}", title=f"Вступление в [{clan['tag']}]")
        modal.add_item(discord.ui.TextInput(label="Игровой ник", min_length=2, max_length=80, required=True))
        modal.add_item(discord.ui.TextInput(label="Возраст (необязательно)", max_length=3, required=False))
        modal.add_item(discord.ui.TextInput(label="Опыт и предпочитаемая роль", style=discord.TextStyle.paragraph, min_length=3, max_length=500, required=True))
        modal.add_item(discord.ui.TextInput(label="Почему хотите вступить, когда играете", style=discord.TextStyle.paragraph, min_length=10, max_length=700, required=True))
        return modal

    def _join_application_embed(self, application: dict, clan: dict, user) -> discord.Embed:
        e = discord.Embed(
            color=COLORS["red"],
            title=f"Заявка на вступление в [{clan['tag']}]",
            description=f"Кандидат: {user} · ID {application['applicantId']}",
            timestamp=datetime.fromisoformat(application["createdAt"]) if application.get("createdAt") else None,
        )
        e.add_field(name="Игровой ник", value=application.get("nickname", ""), inline=True)
        e.add_field(name="Возраст", value=application.get("age") or "Не указан", inline=True)
        e.add_field(name="Опыт и роль", value=application.get("experience", ""))
        e.add_field(name="Мотивация и время игры", value=application.get("motivation", ""))
        e.set_footer(text="Решение принимают лидер, заместитель или офицер клана")
        return e

    async def _submit_join_application(self, interaction: discord.Interaction, clan_id: str):
        clan = self.store.state["clans"].get(clan_id)
        if not clan or clan.get("recruitmentStatus") != "open" or not clan.get("recruitmentThreadId"):
            await interaction.response.send_message("Набор в этот клан сейчас закрыт.", ephemeral=True)
            return
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Ошибка.", ephemeral=True)
            return
        member_role_ids = {r.id for r in member.roles}
        if self.store.find_clan_by_member_ids(member_role_ids):
            await interaction.response.send_message("Вы уже состоите в зарегистрированном клане.", ephemeral=True)
            return
        for app in self.store.state.get("joinApplications", {}).values():
            if app.get("clanId") == clan_id and app.get("applicantId") == interaction.user.id and app.get("status") == "pending":
                await interaction.response.send_message(f"У вас уже есть активная заявка: <#{app['ticketId']}>.", ephemeral=True)
                return
        await interaction.response.defer(ephemeral=True)
        try:
            category = await interaction.guild.fetch_channel(int(clan["channelIds"]["category"]))
        except Exception:
            category = None
        ticket = await interaction.guild.create_text_channel(
            f"вступление-{slug(clan['tag'], 20)}-{str(interaction.user.id)[-4:]}",
            category=category,
            topic=f"Заявка {interaction.user} на вступление в [{clan['tag']}]",
            overwrites=self._join_ticket_overwrites(interaction.guild, clan, interaction.user.id),
            reason=f"Join application to {clan['name']} from {interaction.user}",
        )
        fields = interaction.data.get("components", [])
        values = {}
        for comp in fields:
            for row in comp:
                if row.get("type") == 1:
                    for item in row:
                        if item.get("type") == 4:
                            values[item.get("custom_id")] = item.get("value", "")
        application = {
            "ticketId": str(ticket.id), "clanId": clan_id, "applicantId": interaction.user.id,
            "nickname": clean(values.get("nickname", ""), 80),
            "age": clean(values.get("age", ""), 3),
            "experience": clean(values.get("experience", ""), 500),
            "motivation": clean(values.get("motivation", ""), 700),
            "status": "pending", "createdAt": iso_now(),
        }
        await self.store.update(lambda s: s["joinApplications"].__setitem__(str(ticket.id), application))
        leadership = [clan["roleIds"].get(k) for k in ["leader", "deputy", "officer"] if clan["roleIds"].get(k)]
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(custom_id=f"join:approve:{ticket.id}", label="Принять рекрутом", emoji="✅", style=discord.ButtonStyle.success))
        view.add_item(discord.ui.Button(custom_id=f"join:reject:{ticket.id}", label="Отклонить", emoji="✖️", style=discord.ButtonStyle.danger))
        content = " ".join(f"<@&{rid}>" for rid in leadership)
        await ticket.send(
            content=content,
            embed=self._join_application_embed(application, clan, interaction.user),
            view=view,
            allowed_mentions=discord.AllowedMentions(roles=[int(rid) for rid in leadership if rid]),
        )
        await self._audit(interaction.guild, "Заявка на вступление", f"[{clan['tag']}] · кандидат <@{interaction.user.id}> · тикет <#{ticket.id}>", COLORS["red"])
        await interaction.followup.send(f"Заявка отправлена руководству клана: {ticket.mention}.")

    async def _approve_join_application(self, interaction: discord.Interaction, ticket_id: str):
        application = self.store.state.get("joinApplications", {}).get(ticket_id)
        clan = self.store.state["clans"].get(application["clanId"]) if application else None
        if not application or application.get("status") != "pending" or not clan:
            await interaction.response.send_message("Заявка уже обработана или клан не найден.", ephemeral=True)
            return
        if self._clan_rank(interaction.user, clan) < 3:
            await interaction.response.send_message("Решение может принять только руководство этого клана.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            applicant = await interaction.guild.fetch_member(int(application["applicantId"]))
        except Exception:
            applicant = None
        if not applicant:
            await interaction.followup.send("Кандидат больше не состоит на сервере.")
            return
        member_role_ids = {r.id for r in applicant.roles}
        other_clan = self.store.find_clan_by_member_ids(member_role_ids)
        if other_clan and other_clan["id"] != clan["id"]:
            await interaction.followup.send("Кандидат уже вступил в другой клан.")
            return
        recruit_role_id = clan["roleIds"].get("recruit")
        if recruit_role_id:
            await applicant.add_roles(discord.Object(id=recruit_role_id), reason=f"Join application approved by {interaction.user}")
        await self.store.update(lambda s: (
            s["joinApplications"][ticket_id].update({"status": "approved", "decidedAt": iso_now(), "decidedBy": interaction.user.id}),
        ))
        await self._update_clan_artifacts(interaction.guild, clan)
        try:
            await applicant.send(f"Ваша заявка в клан «{clan['name']}» одобрена. Вам выдана роль рекрута.")
        except Exception:
            pass
        await self._audit(interaction.guild, "Кандидат принят в клан", f"[{clan['tag']}] · <@{applicant.id}> · решение <@{interaction.user.id}>", COLORS["green"])
        await interaction.followup.send(f"{applicant.mention} принят в клан как рекрут.")
        asyncio.get_event_loop().call_later(15, lambda: asyncio.ensure_future(self._delete_channel(interaction.guild, ticket_id)))

    async def _handle_management_button(self, interaction: discord.Interaction, action: str, clan_id_str: str):
        clan = self.store.state["clans"].get(clan_id_str)
        if not clan or str(interaction.channel_id) != clan["channelIds"].get("hq") or self._clan_rank(interaction.user, clan) < 3:
            await interaction.response.send_message("Эта панель доступна только руководству в штабе своего клана.", ephemeral=True)
            return
        if action == "members":
            await interaction.response.send_message(
                "Выберите участника сервера:",
                view=self._user_select_view(clan["id"]),
                ephemeral=True,
            )
        elif action == "count":
            count = await self._member_count(interaction.guild, clan)
            await interaction.response.send_message(f"На сервере: {count}. Общий заявленный состав: {clan['declaredMembers']}.", ephemeral=True)
        elif action == "sync":
            await interaction.response.defer(ephemeral=True)
            await self._update_clan_artifacts(interaction.guild, clan)
            await interaction.followup.send("Карточки Discord и сайта обновлены.")
        elif action == "logo":
            modal = discord.ui.Modal(custom_id=f"clan:logo:{clan['id']}", title=f"Логотип [{clan['tag']}]")
            modal.add_item(discord.ui.TextInput(label="Ссылка на логотип клана Discord", max_length=500, default=clan.get("logoUrl", ""), required=True))
            await interaction.response.send_modal(modal)
        elif action == "dissolve":
            if interaction.user.id != clan.get("leaderId") and not self._is_admin(interaction.user):
                await interaction.response.send_message("Распустить клан может только лидер или администрация.", ephemeral=True)
                return
            modal = discord.ui.Modal(custom_id=f"clan:dissolve:{clan['id']}", title="Подтверждение роспуска")
            modal.add_item(discord.ui.TextInput(label=f"Введите тег {clan['tag']}", max_length=16, required=True))
            await interaction.response.send_modal(modal)

    def _user_select_view(self, clan_id: str):
        view = discord.ui.View(timeout=None)
        select = discord.ui.UserSelect(
            custom_id=f"member:select:{clan_id}",
            placeholder="Участник", min_values=1, max_values=1,
        )
        view.add_item(select)
        return view

    def _role_select_view(self, clan_id: str, user_id: int):
        view = discord.ui.View(timeout=None)
        select = discord.ui.StringSelect(
            custom_id=f"member:role:{clan_id}:{user_id}",
            placeholder="Выберите клановую роль",
        )
        for key in ROLE_KEYS:
            select.add_option(label=ROLE_LABELS[key], value=key)
        view.add_item(select)
        return view

    def _member_management_view(self, clan_id: str, user_id: int):
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(custom_id=f"member:add:{clan_id}:{user_id}", label="Принять рекрутом", style=discord.ButtonStyle.success))
        view.add_item(discord.ui.Button(custom_id=f"member:roles:{clan_id}:{user_id}", label="Назначить роль", style=discord.ButtonStyle.primary))
        view.add_item(discord.ui.Button(custom_id=f"member:remove:{clan_id}:{user_id}", label="Исключить", style=discord.ButtonStyle.danger))
        return view

    async def _apply_member_action(self, interaction: discord.Interaction, action: str, clan_id_str: str, user_id_str: str):
        clan = self.store.state["clans"].get(clan_id_str)
        if not clan or str(interaction.channel_id) != clan["channelIds"].get("hq") or self._clan_rank(interaction.user, clan) < 3:
            await interaction.response.send_message("Доступ запрещён.", ephemeral=True)
            return
        try:
            member = await interaction.guild.fetch_member(int(user_id_str))
        except Exception:
            member = None
        if not member or member.bot:
            await interaction.response.send_message("Участник не найден.", ephemeral=True)
            return
        if action == "roles":
            if self._clan_rank(interaction.user, clan) < 4:
                await interaction.response.send_message("Назначать роли может заместитель, лидер или администрация.", ephemeral=True)
                return
            await interaction.response.edit_message(
                content=f"Выберите роль для {member.mention}:",
                view=self._role_select_view(clan["id"], member.id),
            )
        elif action == "add":
            member_role_ids = {r.id for r in member.roles}
            other_clan = self.store.find_clan_by_member_ids(member_role_ids)
            if other_clan and other_clan["id"] != clan["id"]:
                await interaction.response.send_message("Участник уже состоит в другом клане.", ephemeral=True)
                return
            recruit_role_id = clan["roleIds"].get("recruit")
            if recruit_role_id:
                await member.add_roles(discord.Object(id=recruit_role_id), reason=f"Added in clan HQ by {interaction.user}")
            self._schedule_clan_refresh(interaction.guild, clan["id"])
            await interaction.response.edit_message(content=f"{member.mention} принят рекрутом.", view=None)
        elif action == "remove":
            if member.id == clan.get("leaderId") and not self._is_admin(interaction.user):
                await interaction.response.send_message("Лидера нельзя исключить без участия администрации.", ephemeral=True)
                return
            role_ids = [discord.Object(id=rid) for rid in clan["roleIds"].values() if rid]
            if role_ids:
                await member.remove_roles(*role_ids, reason=f"Removed in clan HQ by {interaction.user}")
            self._schedule_clan_refresh(interaction.guild, clan["id"])
            await interaction.response.edit_message(content=f"{member.mention} исключён из клана.", view=None)

    async def _dissolve_clan(self, interaction: discord.Interaction, clan_id_str: str):
        clan = self.store.state["clans"].get(clan_id_str)
        if not clan or (interaction.user.id != clan.get("leaderId") and not self._is_admin(interaction.user)):
            await interaction.response.send_message("Распустить клан может только лидер или администрация.", ephemeral=True)
            return
        fields = interaction.data.get("components", [])
        expected = ""
        for comp in fields:
            for row in comp:
                if row.get("type") == 1:
                    for item in row:
                        if item.get("type") == 4:
                            expected = clean(item.get("value", ""), 16)
        if expected.lower() != clan["tag"].lower():
            await interaction.response.send_message("Тег не совпал. Клан не был распущен.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        join_ticket_ids = [a["ticketId"] for a in self.store.state.get("joinApplications", {}).values() if a.get("clanId") == clan_id_str]
        await self.store.update(lambda s: (
            s["clans"].pop(clan_id_str, None),
            [s["joinApplications"].pop(tid, None) for tid in join_ticket_ids],
        ))
        try:
            await sync_site(list(self.store.state["clans"].values()))
        except Exception:
            pass
        list_id = self.store.state["setup"].get("listChannelId")
        if clan.get("listMessageId") and list_id:
            try:
                list_ch = interaction.guild.get_channel(int(list_id))
                if isinstance(list_ch, discord.TextChannel):
                    msg = await list_ch.fetch_message(int(clan["listMessageId"]))
                    await msg.delete()
            except Exception:
                pass
        if clan.get("recruitmentThreadId"):
            try:
                thread = await interaction.guild.fetch_channel(int(clan["recruitmentThreadId"]))
                await thread.delete(reason="Clan dissolved")
            except Exception:
                pass
        await self._audit(interaction.guild, "Клан распущен", f"{clan['name']} [{clan['tag']}] · инициатор <@{interaction.user.id}>", COLORS["grey"])
        for tid in join_ticket_ids:
            await self._delete_channel(interaction.guild, tid)
        for cid in [clan["channelIds"].get(k) for k in ["chat", "hq", "voice", "creator"]]:
            if cid:
                await self._delete_channel(interaction.guild, str(cid))
        for vid, voice_data in list(self.store.state.get("temporaryVoices", {}).items()):
            if voice_data.get("clanId") == clan_id_str:
                await self._delete_channel(interaction.guild, str(vid))
                await self.store.update(lambda s, v=vid: s["temporaryVoices"].pop(v, None))
        await self._delete_channel(interaction.guild, str(clan["channelIds"]["category"]))
        for rid in clan["roleIds"].values():
            if rid:
                try:
                    role = interaction.guild.get_role(int(rid))
                    if role:
                        await role.delete(reason="Clan dissolved")
                except Exception:
                    pass
        await interaction.followup.send(f"Клан «{clan['name']}» распущен. Его роли, каналы и публичные карточки удалены.")

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if not interaction.guild or interaction.guild_id != GUILD_ID:
            return
        key = interaction.data.get("custom_id", "") if interaction.data else ""
        if not key and interaction.command:
            key = interaction.command.name
        if not self._can_run(interaction.user.id, key, 1200):
            if interaction.response.is_done():
                await interaction.followup.send("Подождите пару секунд и повторите действие.", ephemeral=True)
            else:
                await interaction.response.send_message("Подождите пару секунд и повторите действие.", ephemeral=True)
            return
        try:
            if interaction.type == discord.InteractionType.component:
                await self._handle_component(interaction)
            elif interaction.type == discord.InteractionType.modal_submit:
                await self._handle_modal(interaction)
            elif interaction.type == discord.InteractionType.component and isinstance(interaction.data, dict):
                ctype = interaction.data.get("component_type")
                if ctype == discord.ComponentType.user_select.value:
                    await self._handle_user_select(interaction)
                elif ctype == discord.ComponentType.string_select.value:
                    await self._handle_string_select(interaction)
        except Exception as e:
            log.error("Interaction error: %s", e, exc_info=True)
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("Произошла ошибка при обработке действия.", ephemeral=True)
            except Exception:
                pass

    async def _handle_component(self, interaction: discord.Interaction):
        custom_id = interaction.data.get("custom_id", "")
        ctype = interaction.data.get("component_type")
        if ctype == discord.ComponentType.user_select.value:
            await self._handle_user_select(interaction)
            return
        if ctype == discord.ComponentType.string_select.value:
            await self._handle_string_select(interaction)
            return
        parts = custom_id.split(":")
        if custom_id == "clan:apply":
            if not self.store.state["setup"].get("ticketCategoryId"):
                await interaction.response.send_message("Регистрация ещё не настроена.", ephemeral=True)
                return
            if self.store.find_clan_by_leader(interaction.user.id) or any(
                a.get("applicantId") == interaction.user.id and a.get("status") == "pending"
                for a in self.store.state["applications"].values()
            ):
                await interaction.response.send_message("У вас уже есть зарегистрированный клан или активная заявка.", ephemeral=True)
                return
            await interaction.response.send_modal(self._first_application_modal())
            return
        if parts[0] == "clan" and parts[1] == "approve":
            if not self._is_admin(interaction.user):
                await interaction.response.send_message("Кнопка доступна администрации.", ephemeral=True)
                return
            view = discord.ui.View(timeout=None)
            view.add_item(discord.ui.Button(custom_id=f"clan:approve-confirm:{parts[2]}", label="Да, создать клан", style=discord.ButtonStyle.success))
            await interaction.response.send_message("Подтвердите создание ролей, закрытой категории и публичной карточки.", view=view, ephemeral=True)
            return
        if parts[0] == "clan" and parts[1] == "approve-confirm":
            await self._create_clan(interaction, parts[2])
            return
        if parts[0] == "clan" and parts[1] == "reject":
            if not self._is_admin(interaction.user):
                await interaction.response.send_message("Кнопка доступна администрации.", ephemeral=True)
                return
            modal = discord.ui.Modal(custom_id=f"clan:reject-submit:{parts[2]}", title="Отклонение заявки")
            modal.add_item(discord.ui.TextInput(label="Причина", style=discord.TextStyle.paragraph, min_length=3, max_length=500, required=True))
            await interaction.response.send_modal(modal)
            return
        if parts[0] == "recruit":
            clan = self.store.state["clans"].get(parts[2])
            if not clan or self._clan_rank(interaction.user, clan) < 3:
                await interaction.response.send_message("У вас нет прав на управление этой карточкой.", ephemeral=True)
                return
            if parts[1] in ("create", "edit"):
                await interaction.response.send_modal(self._recruitment_modal(clan, parts[1]))
            elif parts[1] == "toggle":
                await self._toggle_recruitment(interaction, clan)
            elif parts[1] == "delete":
                await self._delete_recruitment(interaction, clan)
            return
        if parts[0] == "join" and parts[1] == "apply":
            clan = self.store.state["clans"].get(parts[2])
            if not clan or clan.get("recruitmentStatus") != "open":
                await interaction.response.send_message("Набор в этот клан сейчас закрыт.", ephemeral=True)
                return
            member = interaction.user
            if not isinstance(member, discord.Member):
                return
            member_role_ids = {r.id for r in member.roles}
            if self.store.find_clan_by_member_ids(member_role_ids):
                await interaction.response.send_message("Вы уже состоите в зарегистрированном клане.", ephemeral=True)
                return
            for app in self.store.state.get("joinApplications", {}).values():
                if app.get("clanId") == parts[2] and app.get("applicantId") == interaction.user.id and app.get("status") == "pending":
                    await interaction.response.send_message(f"У вас уже есть активная заявка: <#{app['ticketId']}>.", ephemeral=True)
                    return
            await interaction.response.send_modal(self._join_application_modal(clan))
            return
        if parts[0] == "join" and parts[1] == "approve":
            await self._approve_join_application(interaction, parts[2])
            return
        if parts[0] == "join" and parts[1] == "reject":
            application = self.store.state.get("joinApplications", {}).get(parts[2])
            clan = self.store.state["clans"].get(application["clanId"]) if application else None
            if not application or application.get("status") != "pending" or not clan:
                await interaction.response.send_message("Заявка уже обработана.", ephemeral=True)
                return
            if self._clan_rank(interaction.user, clan) < 3:
                await interaction.response.send_message("Решение может принять только руководство этого клана.", ephemeral=True)
                return
            modal = discord.ui.Modal(custom_id=f"join:reject-submit:{parts[2]}", title="Отклонение заявки")
            modal.add_item(discord.ui.TextInput(label="Причина", style=discord.TextStyle.paragraph, min_length=3, max_length=500, required=True))
            await interaction.response.send_modal(modal)
            return
        if parts[0] == "manage":
            await self._handle_management_button(interaction, parts[1], parts[2])
            return
        if parts[0] == "member" and len(parts) >= 4:
            await self._apply_member_action(interaction, parts[1], parts[2], parts[3])
            return
        if custom_id.startswith("clan:apply:continue:"):
            uid = parts[3]
            if interaction.user.id != int(uid):
                await interaction.response.send_message("Это не ваша заявка.", ephemeral=True)
                return
            await interaction.response.send_modal(self._second_application_modal(int(uid)))
            return

    async def _handle_user_select(self, interaction: discord.Interaction):
        custom_id = interaction.data.get("custom_id", "")
        parts = custom_id.split(":")
        if len(parts) >= 3 and parts[0] == "member" and parts[1] == "select":
            clan_id_str = parts[2]
            clan = self.store.state["clans"].get(clan_id_str)
            if not clan or str(interaction.channel_id) != clan["channelIds"].get("hq") or self._clan_rank(interaction.user, clan) < 3:
                await interaction.response.send_message("Доступ запрещён.", ephemeral=True)
                return
            user_ids = interaction.data.get("values", [])
            if not user_ids:
                await interaction.response.edit_message(content="Участник не выбран.", view=None)
                return
            try:
                member = await interaction.guild.fetch_member(int(user_ids[0]))
            except Exception:
                member = None
            if not member or member.bot:
                await interaction.response.edit_message(content="Выбранный участник недоступен.", view=None)
                return
            await interaction.response.edit_message(
                content=f"Управление участником {member.mention}:",
                view=self._member_management_view(clan["id"], member.id),
            )

    async def _handle_string_select(self, interaction: discord.Interaction):
        custom_id = interaction.data.get("custom_id", "")
        parts = custom_id.split(":")
        if len(parts) >= 4 and parts[0] == "member" and parts[1] == "role":
            clan_id_str, user_id_str = parts[2], parts[3]
            clan = self.store.state["clans"].get(clan_id_str)
            if not clan or str(interaction.channel_id) != clan["channelIds"].get("hq") or self._clan_rank(interaction.user, clan) < 4:
                await interaction.response.send_message("Назначать роли может заместитель, лидер или администрация.", ephemeral=True)
                return
            role_key = interaction.data.get("values", [""])[0]
            try:
                member = await interaction.guild.fetch_member(int(user_id_str))
            except Exception:
                member = None
            if not member or role_key not in ROLE_KEYS:
                await interaction.response.edit_message(content="Участник или роль не найдены.", view=None)
                return
            member_role_ids = {r.id for r in member.roles}
            other_clan = self.store.find_clan_by_member_ids(member_role_ids)
            if other_clan and other_clan["id"] != clan["id"]:
                await interaction.response.edit_message(content="Участник состоит в другом клане.", view=None)
                return
            if role_key == "leader" and not self._is_admin(interaction.user):
                await interaction.response.edit_message(content="Передать роль лидера может только администрация.", view=None)
                return
            role_ids = [discord.Object(id=rid) for rid in clan["roleIds"].values() if rid]
            if role_ids:
                await member.remove_roles(*role_ids)
            target_role_id = clan["roleIds"].get(role_key)
            if target_role_id:
                await member.add_roles(discord.Object(id=target_role_id), reason=f"Clan role assigned in HQ by {interaction.user}")
            if role_key == "leader":
                clan["leaderId"] = member.id
                clan["leaderName"] = member.display_name
            await self._update_clan_artifacts(interaction.guild, clan)
            await interaction.response.edit_message(content=f"{member.mention} назначен: {ROLE_LABELS[role_key]}.", view=None)

    async def _handle_modal(self, interaction: discord.Interaction):
        custom_id = interaction.data.get("custom_id", "")
        if custom_id == "clan:apply:first":
            await self._submit_first(interaction)
            return
        if custom_id.startswith("clan:apply:second:"):
            await self._submit_second(interaction, custom_id.split(":")[3])
            return
        if custom_id.startswith("recruit:submit:"):
            parts = custom_id.split(":")
            await self._submit_recruitment(interaction, parts[2], parts[3])
            return
        if custom_id.startswith("clan:dissolve:"):
            await self._dissolve_clan(interaction, custom_id.split(":")[2])
            return
        if custom_id.startswith("join:submit:"):
            await self._submit_join_application(interaction, custom_id.split(":")[2])
            return
        if custom_id.startswith("join:reject-submit:"):
            ticket_id = custom_id.split(":")[3]
            application = self.store.state.get("joinApplications", {}).get(ticket_id)
            clan = self.store.state["clans"].get(application["clanId"]) if application else None
            if not application or application.get("status") != "pending" or not clan:
                await interaction.response.send_message("Заявка уже обработана.", ephemeral=True)
                return
            if self._clan_rank(interaction.user, clan) < 3:
                await interaction.response.send_message("Доступ запрещён.", ephemeral=True)
                return
            fields = interaction.data.get("components", [])
            reason = ""
            for comp in fields:
                for row in comp:
                    if row.get("type") == 1:
                        for item in row:
                            if item.get("type") == 4:
                                reason = clean(item.get("value", ""), 500)
            await self.store.update(lambda s: (
                s["joinApplications"][ticket_id].update({"status": "rejected", "reason": reason, "decidedAt": iso_now(), "decidedBy": interaction.user.id}),
            ))
            try:
                applicant = await interaction.guild.fetch_member(int(application["applicantId"]))
                await applicant.send(f"Ваша заявка в клан «{clan['name']}» отклонена. Причина: {reason}")
            except Exception:
                pass
            await self._audit(interaction.guild, "Заявка на вступление отклонена", f"[{clan['tag']}] · <@{application['applicantId']}> · {reason} · решение <@{interaction.user.id}>", COLORS["grey"])
            await interaction.response.send_message("Заявка отклонена, кандидат уведомлён.", ephemeral=True)
            asyncio.get_event_loop().call_later(15, lambda: asyncio.ensure_future(self._delete_channel(interaction.guild, ticket_id)))
            return
        if custom_id.startswith("clan:logo:"):
            clan_id_str = custom_id.split(":")[2]
            clan = self.store.state["clans"].get(clan_id_str)
            if not clan or str(interaction.channel_id) != clan["channelIds"].get("hq") or self._clan_rank(interaction.user, clan) < 3:
                await interaction.response.send_message("Доступ запрещён.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            fields = interaction.data.get("components", [])
            logo_url = ""
            for comp in fields:
                for row in comp:
                    if row.get("type") == 1:
                        for item in row:
                            if item.get("type") == 4:
                                logo_url = item.get("value", "")
            try:
                logo = await inspect_discord_image(self._session, logo_url, CLAN_LOGO_MAX_BYTES, "Логотип клана")
            except ValueError as e:
                await interaction.followup.send(f"Логотип не принят: {clean(str(e), 300)}")
                return
            clan["logoUrl"] = logo["url"]
            clan["logoHash"] = logo["hash"]
            clan["logoMime"] = logo["mime"]
            clan["logoSize"] = logo["size"]
            await self._update_clan_artifacts(interaction.guild, clan)
            await self._audit(interaction.guild, "Логотип клана обновлён", f"[{clan['tag']}] · автор <@{interaction.user.id}>", COLORS["red"])
            await interaction.followup.send("Логотип обновлён в Discord и передан на сайт.")
            return
        if custom_id.startswith("clan:reject-submit:"):
            if not self._is_admin(interaction.user):
                await interaction.response.send_message("Кнопка доступна администрации.", ephemeral=True)
                return
            ticket_id = custom_id.split(":")[2]
            app = self.store.state["applications"].get(ticket_id)
            if not app or app.get("status") != "pending":
                await interaction.response.send_message("Заявка уже обработана.", ephemeral=True)
                return
            fields = interaction.data.get("components", [])
            reason = ""
            for comp in fields:
                for row in comp:
                    if row.get("type") == 1:
                        for item in row:
                            if item.get("type") == 4:
                                reason = clean(item.get("value", ""), 500)
            await self.store.update(lambda s: s["applications"][ticket_id].update({"status": "rejected", "rejectReason": reason}))
            try:
                applicant = await interaction.guild.fetch_member(int(app["applicantId"]))
                await applicant.send(f"Ваша заявка на регистрацию клана «{app['name']}» отклонена. Причина: {reason}")
            except Exception:
                pass
            await self._audit(interaction.guild, "Заявка на клан отклонена", f"{app['name']} [{app['tag']}] · {reason} · решение <@{interaction.user.id}>", COLORS["grey"])
            await interaction.response.send_message("Заявка отклонена, заявитель уведомлён.", ephemeral=True)
            asyncio.get_event_loop().call_later(15, lambda: asyncio.ensure_future(self._delete_channel(interaction.guild, ticket_id)))
            return

    @app_commands.command(name="clan-setup", description="Настроить клановую систему")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(admin_role="Роль администрации, проверяющей заявки", role_anchor="Клановые роли будут создаваться сразу под этой ролью")
    @app_commands.guild_only()
    async def slash_clan_setup(self, interaction: discord.Interaction, admin_role: discord.Role, role_anchor: discord.Role):
        await self._run_setup(interaction)

    clan_group = app_commands.Group(name="clan", description="Управление кланом")

    @clan_group.command(name="invite", description="Принять участника рекрутом")
    @app_commands.describe(member="Участник")
    @app_commands.guild_only()
    async def clan_invite(self, interaction: discord.Interaction, member: discord.Member):
        await self._handle_clan_sub(interaction, "invite", member, None, None)

    @clan_group.command(name="role", description="Назначить внутреннюю роль")
    @app_commands.describe(member="Участник", role="Роль")
    @app_commands.choices(role=[app_commands.Choice(name=ROLE_LABELS[k], value=k) for k in ROLE_KEYS])
    @app_commands.guild_only()
    async def clan_role(self, interaction: discord.Interaction, member: discord.Member, role: str):
        await self._handle_clan_sub(interaction, "role", member, role, None)

    @clan_group.command(name="remove", description="Исключить участника из клана")
    @app_commands.describe(member="Участник")
    @app_commands.guild_only()
    async def clan_remove(self, interaction: discord.Interaction, member: discord.Member):
        await self._handle_clan_sub(interaction, "remove", member, None, None)

    @clan_group.command(name="count", description="Показать численность клана")
    @app_commands.guild_only()
    async def clan_count(self, interaction: discord.Interaction):
        await self._handle_clan_sub(interaction, "count", None, None, None)

    @clan_group.command(name="dissolve", description="Распустить клан с подтверждением")
    @app_commands.describe(tag="Тег клана; для администрации")
    @app_commands.guild_only()
    async def clan_dissolve(self, interaction: discord.Interaction, tag: str = None):
        await self._handle_clan_sub(interaction, "dissolve", None, None, tag)

    @clan_group.command(name="sync", description="Принудительно синхронизировать все кланы")
    @app_commands.guild_only()
    async def clan_sync(self, interaction: discord.Interaction):
        if not self._is_admin(interaction.user):
            await interaction.response.send_message("Команда доступна администрации.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        errors = []
        for clan in list(self.store.state["clans"].values()):
            try:
                await self._update_clan_artifacts(interaction.guild, clan, sync=False)
            except Exception as e:
                log.warning("clan sync: карточка %s не обновилась: %s", clan.get("name") or clan.get("id"), e)
                errors.append(f"{clan.get('name') or clan.get('id')}: {e}")
        site_status = "ok"
        try:
            await sync_site(list(self.store.state["clans"].values()))
        except Exception as e:
            log.warning("clan sync: сайт не принял данные: %s", e)
            site_status = f"ошибка — {e}"
        if errors or site_status != "ok":
            lines = "\n".join(f"• {x}" for x in errors[:5])
            more = f"\n…и ещё {len(errors) - 5}" if len(errors) > 5 else ""
            await interaction.followup.send(
                f"Синк завершён частично.\n**Сайт:** {site_status}\n**Карточки Discord ({len(errors)} ошиб.):**\n{lines}{more}",
                ephemeral=True,
            )
        else:
            await interaction.followup.send("Счётчики, карточки Discord и данные сайта синхронизированы.")

    async def _handle_clan_sub(self, interaction: discord.Interaction, sub: str, member, role_key: str, tag: str):
        clan = None
        member_role_ids = {r.id for r in interaction.user.roles} if isinstance(interaction.user, discord.Member) else set()
        clan = self.store.find_clan_by_member_ids(member_role_ids)
        if sub == "dissolve" and self._is_admin(interaction.user) and tag:
            for c in self.store.state["clans"].values():
                if c.get("tag", "").lower() == tag.lower():
                    clan = c
                    break
        if not clan:
            await interaction.response.send_message("Вы не состоите в зарегистрированном клане.", ephemeral=True)
            return
        if sub == "count":
            count = await self._member_count(interaction.guild, clan)
            await interaction.response.send_message(f"На сервере WARDOGS RU: {count}. Общий заявленный состав: {clan['declaredMembers']}.", ephemeral=True)
            return
        if sub == "dissolve":
            if interaction.user.id != clan.get("leaderId") and not self._is_admin(interaction.user):
                await interaction.response.send_message("Распустить клан может только лидер или администрация.", ephemeral=True)
                return
            modal = discord.ui.Modal(custom_id=f"clan:dissolve:{clan['id']}", title="Подтверждение роспуска")
            modal.add_item(discord.ui.TextInput(label=f"Введите тег {clan['tag']}", max_length=16, required=True))
            await interaction.response.send_modal(modal)
            return
        rank = self._clan_rank(interaction.user, clan)
        if rank < 3:
            await interaction.response.send_message("Команда доступна офицерам, заместителю, лидеру и администрации.", ephemeral=True)
            return
        if not member or member.bot:
            await interaction.response.send_message("Выберите участника сервера, который не является ботом.", ephemeral=True)
            return
        target_member_role_ids = {r.id for r in member.roles}
        target_clan = self.store.find_clan_by_member_ids(target_member_role_ids)
        if sub == "invite":
            if target_clan and target_clan["id"] != clan["id"]:
                await interaction.response.send_message("Участник уже состоит в другом клане.", ephemeral=True)
                return
            rid = clan["roleIds"].get("recruit")
            if rid:
                await member.add_roles(discord.Object(id=rid), reason=f"Invited by {interaction.user}")
            self._schedule_clan_refresh(interaction.guild, clan["id"])
            await interaction.response.send_message(f"{member.mention} получил роль рекрута [{clan['tag']}].", ephemeral=True)
        elif sub == "remove":
            if member.id == clan.get("leaderId") and not self._is_admin(interaction.user):
                await interaction.response.send_message("Лидера нельзя исключить. Сначала передайте руководство через администрацию.", ephemeral=True)
                return
            role_ids = [discord.Object(id=rid) for rid in clan["roleIds"].values() if rid]
            if role_ids:
                await member.remove_roles(*role_ids, reason=f"Removed by {interaction.user}")
            self._schedule_clan_refresh(interaction.guild, clan["id"])
            await interaction.response.send_message(f"{member.mention} исключён из клана.", ephemeral=True)
        elif sub == "role":
            if rank < 4:
                await interaction.response.send_message("Назначать внутренние роли может заместитель, лидер или администрация.", ephemeral=True)
                return
            if role_key == "leader" and not self._is_admin(interaction.user):
                await interaction.response.send_message("Передачу роли лидера выполняет только администрация.", ephemeral=True)
                return
            if target_clan and target_clan["id"] != clan["id"]:
                await interaction.response.send_message("Участник состоит в другом клане.", ephemeral=True)
                return
            old_role_ids = [discord.Object(id=rid) for rid in clan["roleIds"].values() if rid]
            if old_role_ids:
                await member.remove_roles(*old_role_ids)
            target_rid = clan["roleIds"].get(role_key)
            if target_rid:
                await member.add_roles(discord.Object(id=target_rid), reason=f"Clan role assigned by {interaction.user}")
            if role_key == "leader":
                clan["leaderId"] = member.id
                clan["leaderName"] = member.display_name
            await self._update_clan_artifacts(interaction.guild, clan)
            await interaction.response.send_message(f"{member.mention} назначен: {ROLE_LABELS[role_key]}.", ephemeral=True)

    recruitment_group = app_commands.Group(name="recruitment", description="Управление карточкой набора")

    @recruitment_group.command(name="create", description="Создать публикацию набора")
    @app_commands.guild_only()
    async def recruitment_create(self, interaction: discord.Interaction):
        await self._handle_recruitment_sub(interaction, "create")

    @recruitment_group.command(name="edit", description="Изменить публикацию набора")
    @app_commands.guild_only()
    async def recruitment_edit(self, interaction: discord.Interaction):
        await self._handle_recruitment_sub(interaction, "edit")

    @recruitment_group.command(name="toggle", description="Открыть или закрыть набор")
    @app_commands.guild_only()
    async def recruitment_toggle(self, interaction: discord.Interaction):
        await self._handle_recruitment_sub(interaction, "toggle")

    @recruitment_group.command(name="delete", description="Удалить публикацию набора")
    @app_commands.guild_only()
    async def recruitment_delete(self, interaction: discord.Interaction):
        await self._handle_recruitment_sub(interaction, "delete")

    async def _handle_recruitment_sub(self, interaction: discord.Interaction, sub: str):
        member_role_ids = {r.id for r in interaction.user.roles} if isinstance(interaction.user, discord.Member) else set()
        clan = self.store.find_clan_by_member_ids(member_role_ids)
        if not clan or self._clan_rank(interaction.user, clan) < 3:
            await interaction.response.send_message("Команда доступна руководству зарегистрированного клана.", ephemeral=True)
            return
        if sub in ("create", "edit"):
            if sub == "create" and clan.get("recruitmentThreadId"):
                await interaction.response.send_message("У клана уже есть публикация. Используйте `/recruitment edit`.", ephemeral=True)
                return
            if sub == "edit" and not clan.get("recruitmentThreadId"):
                await interaction.response.send_message("Сначала создайте публикацию.", ephemeral=True)
                return
            await interaction.response.send_modal(self._recruitment_modal(clan, sub))
        elif sub == "toggle":
            await self._toggle_recruitment(interaction, clan)
        elif sub == "delete":
            await self._delete_recruitment(interaction, clan)

    voice_group = app_commands.Group(name="voice", description="Настройка временной голосовой комнаты")

    @voice_group.command(name="name", description="Изменить название")
    @app_commands.describe(value="Новое название")
    @app_commands.guild_only()
    async def voice_name(self, interaction: discord.Interaction, value: str):
        await self._handle_voice_sub(interaction, "name", value)

    @voice_group.command(name="limit", description="Лимит участников, 0 — без лимита")
    @app_commands.describe(value="От 0 до 99")
    @app_commands.guild_only()
    async def voice_limit(self, interaction: discord.Interaction, value: int):
        await self._handle_voice_sub(interaction, "limit", str(value))

    @voice_group.command(name="lock", description="Закрыть комнату для новых участников")
    @app_commands.guild_only()
    async def voice_lock(self, interaction: discord.Interaction):
        await self._handle_voice_sub(interaction, "lock", None)

    @voice_group.command(name="unlock", description="Открыть комнату участникам клана")
    @app_commands.guild_only()
    async def voice_unlock(self, interaction: discord.Interaction):
        await self._handle_voice_sub(interaction, "unlock", None)

    async def _handle_voice_sub(self, interaction: discord.Interaction, sub: str, value: str | None):
        member = interaction.user
        if not isinstance(member, discord.Member) or not member.voice or not member.voice.channel:
            await interaction.response.send_message("Сначала зайдите в свою временную голосовую комнату.", ephemeral=True)
            return
        channel = member.voice.channel
        data = self.store.state.get("temporaryVoices", {}).get(str(channel.id))
        if not data:
            await interaction.response.send_message("Сначала зайдите в свою временную голосовую комнату.", ephemeral=True)
            return
        if data.get("ownerId") != interaction.user.id and not self._is_admin(interaction.user):
            await interaction.response.send_message("Настраивать комнату может её создатель или администратор.", ephemeral=True)
            return
        if sub == "name":
            await channel.edit(name=clean(value, 80))
        elif sub == "limit":
            try:
                await channel.edit(user_limit=int(value))
            except (ValueError, TypeError):
                await channel.edit(user_limit=0)
        elif sub == "lock":
            clan = self.store.state["clans"].get(data.get("clanId", ""))
            if clan:
                for rid in clan["roleIds"].values():
                    if rid:
                        try:
                            role = interaction.guild.get_role(int(rid))
                            if role:
                                await channel.set_permissions(role, connect=False)
                        except Exception:
                            pass
                await channel.set_permissions(interaction.user, view_channel=True, connect=True, speak=True, manage_channels=True)
        elif sub == "unlock":
            await channel.sync_permissions()
        await interaction.response.send_message("Настройки голосовой комнаты обновлены.", ephemeral=True)

    backup_group = app_commands.Group(name="clan-backup", description="Резервные копии клановой системы")

    @backup_group.command(name="status", description="Показать состояние автоматических копий")
    @app_commands.guild_only()
    async def backup_status(self, interaction: discord.Interaction):
        if not self._is_admin(interaction.user):
            await interaction.response.send_message("Команда доступна только администрации.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Автобэкап включён." if self.backup_manager.enabled
            else "Резервное копирование выключено: добавьте CLAN_BACKUP_SECRET длиной не менее 32 символов в .env и перезапустите бота.",
            ephemeral=True,
        )

    @backup_group.command(name="create", description="Создать зашифрованную копию сейчас")
    @app_commands.guild_only()
    async def backup_create(self, interaction: discord.Interaction):
        if not self._is_admin(interaction.user):
            await interaction.response.send_message("Команда доступна только администрации.", ephemeral=True)
            return
        if not self.backup_manager.enabled:
            await interaction.response.send_message("Сначала настройте CLAN_BACKUP_SECRET в .env и перезапустите бота.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        result = await self.backup_manager.create(interaction.guild, f"manual:{interaction.user.id}")
        await interaction.followup.send(f"Готово: `{result['filename']}`. Кланов в копии: {result['clanCount']}. Файл отправлен в закрытый канал резервных копий.")

    @backup_group.command(name="restore-latest", description="Восстановить последнюю копию из закрытого канала")
    @app_commands.describe(confirmation="Введите ВОССТАНОВИТЬ")
    @app_commands.guild_only()
    async def backup_restore(self, interaction: discord.Interaction, confirmation: str):
        if not self._is_admin(interaction.user):
            await interaction.response.send_message("Команда доступна только администрации.", ephemeral=True)
            return
        if not self.backup_manager.enabled:
            await interaction.response.send_message("Сначала настройте CLAN_BACKUP_SECRET в .env и перезапустите бота.", ephemeral=True)
            return
        if confirmation != "ВОССТАНОВИТЬ":
            await interaction.response.send_message("Восстановление отменено. Введите слово ВОССТАНОВИТЬ точно, заглавными буквами.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        source = await self.backup_manager.load_latest(interaction.guild)
        await self.store.replace(source["payload"]["state"])
        await interaction.followup.send("Восстановление завершено. Данные клановой системы загружены из резервной копии.")


async def setup(bot: commands.Bot):
    await bot.add_cog(ClansCog(bot))

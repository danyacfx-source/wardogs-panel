import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime
from io import BytesIO

import config
import database
from database import get_conn

log = logging.getLogger("clan_tickets")

# Фолбэк, если БД с типами недоступна (текст формы по умолчанию).
CLAN_APPLICATION_FORM = (
    "# ЗАЯВКА НА РЕГИСТРАЦИЮ КЛАНА\n\n"
    "1. Название клана.\n"
    "2. Тег|Роль клана.\n"
    "3. Ник лидера.\n"
    "4. Количество участников.\n"
    "5. Ссылка на Discord клана.\n"
    "6. Кратко о клане.\n"
    "7. Цели и задачи клана.\n\n"
    "> Ознакомлен с Положением о регистрации и деятельности кланов WARDOGS | RU "
    "и согласен соблюдать установленные правила."
)

_CLAN_FALLBACK = {
    "type_id": "clan",
    "label": "Зарегистрировать клан",
    "description": CLAN_APPLICATION_FORM,
    "emoji": "📝",
    "color": 16766720,
    "enabled": 1,
}


def _clan_type():
    """Тип 'clan' из панели (label=кнопка, description=форма, color=эмбед)."""
    try:
        row = database.get_ticket_type("clan")
    except Exception:
        log.exception("clan_ticket: не удалось прочитать тип clan из БД")
        row = None
    return row or dict(_CLAN_FALLBACK)


def _clan_title(ttype: dict) -> str:
    emoji = (ttype.get("emoji") or "").strip() or "📝"
    first_line = (ttype.get("description") or CLAN_APPLICATION_FORM).strip().splitlines()[0].strip().lstrip("#").strip()
    return f"{emoji} {first_line}" if first_line else f"{emoji} ЗАЯВКА НА РЕГИСТРАЦИЮ КЛАНА"


def _clan_color(ttype: dict) -> discord.Color:
    return discord.Color(int(ttype.get("color") or 0xFFD700) & 0xFFFFFF)


class ClanTicketPanelView(discord.ui.View):
    """Persistent-панель регистрации клана (подписи/форма — из ticket_types)."""

    def __init__(self):
        super().__init__(timeout=None)
        ttype = _clan_type()
        button = discord.ui.Button(
            label=(ttype.get("label") or "Зарегистрировать клан")[:80],
            style=discord.ButtonStyle.primary,
            custom_id="ck_create",
            emoji=(ttype.get("emoji") or "").strip() or "📝",
            disabled=not int(ttype.get("enabled") or 1),
        )
        button.callback = self._create_from_button
        self.add_item(button)

    async def _create_from_button(self, interaction: discord.Interaction):
        if not int(_clan_type().get("enabled") or 1):
            return await interaction.response.send_message(
                "Регистрация кланов временно закрыта.", ephemeral=True
            )
        await self._create_ticket(interaction)

    async def _create_ticket(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        conn = get_conn()
        existing = conn.execute(
            "SELECT 1 FROM tickets WHERE user_id = ? AND ticket_type = 'clan' AND status = 'open'",
            (interaction.user.id,),
        ).fetchone()
        if existing:
            return await interaction.followup.send("У вас уже есть открытая заявка на регистрацию клана.", ephemeral=True)

        guild = interaction.guild
        user = interaction.user
        category = guild.get_channel(config.CLAN_TICKET_CATEGORY)
        if not category:
            return await interaction.followup.send("❌ Категория тикетов для кланов не настроена.", ephemeral=True)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                attach_files=True, embed_links=True,
            ),
        }
        for staff_role_id in config.CLAN_TICKET_STAFF_ROLES:
            staff = guild.get_role(staff_role_id)
            if staff:
                overwrites[staff] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                )

        try:
            channel = await guild.create_text_channel(
                name=f"клан-{user.name}",
                category=category,
                overwrites=overwrites,
                topic=f"Регистрация клана от {user} ({user.id})",
            )
        except discord.HTTPException as e:
            log.warning("Ошибка создания кланового тикета для %s: %s", user, e)
            return await interaction.followup.send(
                "❌ Не удалось создать заявку (ошибка Discord).", ephemeral=True
            )
        except Exception as e:
            log.exception("Непредвиденная ошибка создания кланового тикета: %s", e)
            return await interaction.followup.send(
                "❌ Не удалось создать заявку.", ephemeral=True
            )

        ttype = _clan_type()
        embed = discord.Embed(
            title=_clan_title(ttype),
            description=ttype.get("description") or CLAN_APPLICATION_FORM,
            color=_clan_color(ttype),
            timestamp=datetime.utcnow(),
        )
        embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)
        embed.set_footer(text=f"ID: {user.id}")

        staff_mention = " ".join(f"<@&{r}>" for r in config.CLAN_TICKET_STAFF_ROLES)
        await channel.send(content=f"{staff_mention} {user.mention}", embed=embed, view=ClanTicketCloseView())

        conn.execute(
            "INSERT INTO tickets (user_id, channel_id, ticket_type, status, created_at) VALUES (?, ?, 'clan', 'open', ?)",
            (user.id, channel.id, datetime.utcnow().isoformat()),
        )
        conn.commit()

        await interaction.followup.send(f"Заявка на регистрацию клана создана: {channel.mention}", ephemeral=True)

        if config.CLAN_TICKET_LOG_CHANNEL:
            log_ch = guild.get_channel(config.CLAN_TICKET_LOG_CHANNEL)
            if log_ch:
                await log_ch.send(f"📝 Заявка на клан: {channel.mention} | Пользователь: {user}")


class ClanTicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Закрыть заявку", style=discord.ButtonStyle.red, custom_id="ck_close", emoji="🔒")
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Вы уверены?", view=ClanTicketCloseConfirmView()
        )


class ClanTicketCloseConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="Да", style=discord.ButtonStyle.green, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        conn = get_conn()
        row = conn.execute(
            "SELECT * FROM tickets WHERE channel_id = ?", (interaction.channel.id,)
        ).fetchone()

        if row:
            conn.execute(
                "UPDATE tickets SET status = 'closed', closed_at = ?, closed_by = ? WHERE channel_id = ?",
                (datetime.utcnow().isoformat(), interaction.user.id, interaction.channel.id),
            )
            conn.commit()
            member = interaction.guild.get_member(row["user_id"])
            if member:
                # Автор заявки больше не видит канал.
                await interaction.channel.set_permissions(member, overwrite=None)
            # Страховка через @everyone.
            await interaction.channel.set_permissions(
                interaction.guild.default_role, view_channel=False
            )
            # Персонал оставляет доступ после закрытия.
            for staff_role_id in config.CLAN_TICKET_STAFF_ROLES:
                staff = interaction.guild.get_role(staff_role_id)
                if staff:
                    await interaction.channel.set_permissions(
                        staff, view_channel=True, send_messages=False, read_message_history=True
                    )

        embed = discord.Embed(
            title="Заявка закрыта",
            description=f"Закрыл: {interaction.user.mention}\nСохраните транскрипт или удалите канал.",
            color=discord.Color.greyple(),
        )
        await interaction.message.edit(embed=embed, view=ClanTicketClosedView())

        if config.CLAN_TICKET_LOG_CHANNEL:
            log_ch = interaction.guild.get_channel(config.CLAN_TICKET_LOG_CHANNEL)
            if log_ch:
                await log_ch.send(
                    f"📝 Заявка на клан закрыта: {interaction.channel.mention} | Закрыл: {interaction.user}"
                )

    @discord.ui.button(label="Нет", style=discord.ButtonStyle.red, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Закрытие отменено.", view=None)


class ClanTicketClosedView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Удалить канал", style=discord.ButtonStyle.danger, custom_id="ck_delete", emoji="🗑️")
    async def delete_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        conn = get_conn()
        conn.execute("DELETE FROM tickets WHERE channel_id = ?", (interaction.channel.id,))
        conn.commit()
        await interaction.channel.delete()

    @discord.ui.button(label="Сохранить транскрипт", style=discord.ButtonStyle.blurple, custom_id="ck_transcript", emoji="📄")
    async def save_transcript(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        messages = [msg async for msg in interaction.channel.history(oldest_first=True)]

        lines = []
        for msg in messages:
            ts = msg.created_at.strftime("%d.%m.%Y %H:%M:%S")
            content = msg.content or ""
            if msg.embeds:
                for e in msg.embeds:
                    if e.description:
                        content += f" {e.description}"
            lines.append(f"[{ts}] {msg.author}: {content}")

        transcript = "\n".join(lines)
        file = discord.File(
            fp=BytesIO(transcript.encode("utf-8")),
            filename=f"clan-{interaction.channel.name}.txt",
        )

        if config.CLAN_TICKET_TRANSCRIPT_CHANNEL:
            tr_ch = interaction.guild.get_channel(config.CLAN_TICKET_TRANSCRIPT_CHANNEL)
            if tr_ch:
                await tr_ch.send(file=file)
                return await interaction.followup.send("Транскрипт сохранён.", ephemeral=True)
        await interaction.followup.send(file=file, ephemeral=True)


class ClanTicketCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    clan_group = app_commands.Group(name="clan-reg", description="Регистрация кланов (панель заявок)")

    @clan_group.command(name="panel", description="Отправить панель регистрации кланов")
    @app_commands.describe(channel="Канал для панели")
    @app_commands.guild_only()
    async def clan_panel(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❌ Нет прав.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        target = channel or interaction.channel

        ttype = _clan_type()
        embed = discord.Embed(
            title=_clan_title(ttype),
            description=ttype.get("description") or CLAN_APPLICATION_FORM,
            color=_clan_color(ttype),
        )
        await target.send(embed=embed, view=ClanTicketPanelView())
        await interaction.followup.send(f"Панель отправлена в {target.mention}.", ephemeral=True)

    async def panel_submit_application(self, payload: dict) -> dict:
        """Создаёт клановый тикет из заявки, пришедшей с панели."""
        name = str(payload.get("name") or "").strip()
        tag = str(payload.get("tag") or "").strip()
        description = str(payload.get("description") or "").strip()
        leader = str(payload.get("leader") or "").strip()
        steam_id = str(payload.get("steam_id") or "").strip()
        discord_name = str(payload.get("discord_name") or "").strip()
        ticket_id = payload.get("ticket_id")

        if not name or not tag:
            return {"ok": False, "error": "missing_name_or_tag"}

        if not config.GUILD_ID:
            return {"ok": False, "error": "guild_not_configured"}
        guild = self.bot.get_guild(int(config.GUILD_ID))
        if not guild:
            return {"ok": False, "error": "guild_not_found"}

        category = guild.get_channel(config.CLAN_TICKET_CATEGORY)
        if not category:
            return {"ok": False, "error": "category_not_configured"}

        # Ищем автора по привязке steam→discord (через панельную БД).
        author = None
        try:
            import database as db
            user = db.seed_get_user_by_steam(steam_id)
            if user and user.get("discord_id"):
                author = guild.get_member(int(user["discord_id"]))
        except Exception:
            pass

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
        }
        if author:
            overwrites[author] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                attach_files=True, embed_links=True,
            )
        for staff_role_id in config.CLAN_TICKET_STAFF_ROLES:
            staff = guild.get_role(staff_role_id)
            if staff:
                overwrites[staff] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                )

        safe_tag = "".join(c for c in tag if c.isalnum())[:16] or "clan"
        try:
            channel = await guild.create_text_channel(
                name=f"клан-{safe_tag}",
                category=category,
                overwrites=overwrites,
                topic=f"Заявка с панели: [{tag}] {name} | Steam: {steam_id}",
            )
        except discord.HTTPException as e:
            log.warning("Ошибка создания кланового тикета из панели: %s", e)
            return {"ok": False, "error": "discord_error"}

        embed = discord.Embed(
            title=f"📝 ЗАЯВКА НА РЕГИСТРАЦИЮ КЛАНА [{tag}] {name}",
            description=(
                f"**Название:** {name}\n"
                f"**Тег:** {tag}\n"
                f"**Лидер:** {leader}\n"
                f"**SteamID:** `{steam_id}`\n"
                f"**Discord:** {discord_name}\n"
                f"**ID заявки на панели:** `{ticket_id}`\n\n"
                f"**Описание:**\n{description}"
            ),
            color=discord.Color.gold(),
            timestamp=datetime.utcnow(),
        )
        if author:
            embed.set_author(name=author.display_name, icon_url=author.display_avatar.url)
            author_mention = author.mention
        else:
            author_mention = f"`{steam_id}`"

        staff_mention = " ".join(f"<@&{r}>" for r in config.CLAN_TICKET_STAFF_ROLES)
        await channel.send(
            content=f"{staff_mention} {author_mention}",
            embed=embed,
            view=ClanTicketCloseView(),
        )

        conn = get_conn()
        conn.execute(
            "INSERT INTO tickets (user_id, channel_id, ticket_type, status, created_at) VALUES (?, ?, 'clan', 'open', ?)",
            (author.id if author else 0, channel.id, datetime.utcnow().isoformat()),
        )
        conn.commit()

        if config.CLAN_TICKET_LOG_CHANNEL:
            log_ch = guild.get_channel(config.CLAN_TICKET_LOG_CHANNEL)
            if log_ch:
                await log_ch.send(f"📝 Заявка на клан с панели: {channel.mention} | [{tag}] {name} | {leader}")

        return {"ok": True, "channel_id": channel.id}


async def setup(bot: commands.Bot):
    await bot.add_cog(ClanTicketCog(bot))
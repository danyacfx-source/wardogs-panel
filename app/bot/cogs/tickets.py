import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import logging
from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime
from io import BytesIO
import importlib

import config
import database
from database import get_conn

log = logging.getLogger("tickets")

# Один пользователь не может открыть два тикета параллельно: проверка «уже есть
# открытый тикет» и запись в БД разделены созданием канала (await), и двойной
# клик иначе проходил проверку дважды.
_ticket_locks: dict = defaultdict(asyncio.Lock)
# Закрытие/удаление одного канала и выгрузка его транскрипта не должны идти
# параллельно при двойном клике (дубли комментариев, повторный delete).
_channel_locks: dict = defaultdict(asyncio.Lock)
_transcript_locks: dict = defaultdict(asyncio.Lock)

_panel_db_mod = None


def _panel_db():
    """Ленивый импорт пакета данных панели (app.db) из кога бота."""
    global _panel_db_mod
    if _panel_db_mod is None:
        _panel_db_mod = importlib.import_module("app.db")
    return _panel_db_mod


def _row_get(row, key, default=None):
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


async def _build_transcript(channel) -> str:
    """Собирает текстовый транскрипт канала. Ошибки — лог, частичный результат."""
    lines = []
    try:
        async for msg in channel.history(oldest_first=True):
            ts = msg.created_at.strftime("%d.%m.%Y %H:%M:%S")
            content = msg.content or ""
            if msg.embeds:
                for e in msg.embeds:
                    if e.description:
                        content += f" {e.description}"
            if msg.attachments:
                content += " " + " ".join(a.url for a in msg.attachments)
            lines.append(f"[{ts}] {msg.author}: {content}")
    except Exception:
        log.exception("Сбор транскрипта канала %s прерван", channel.id)
    return "\n".join(lines)


def _transcript_chunks(text: str, size: int = 3900) -> list:
    """Режет транскрипт на куски по границам строк (комментарии сайта ≤4000)."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        if cur and len(cur) + len(line) + 1 > size:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks or ["(пустой канал)"]


async def _ensure_site_ticket(row, channel):
    """Возвращает site_ticket_id бот-записи, создавая сайт-тикет при необходимости."""
    site_id = _row_get(row, "site_ticket_id")
    if site_id:
        return site_id
    db = _panel_db()
    steam = await db.get_steam_by_discord(str(row["user_id"])) or ""
    ttype = _get_type(_row_get(row, "ticket_type") or "complaint")
    label = ttype.get("label") or "Тикет"
    created = await db.create_ticket(
        title=f"Тикет Discord — {label}",
        description=(
            f"Discord-канал: #{channel.name}\n"
            f"channel_id: {channel.id}\n"
            f"Автор: discord:{row['user_id']}\n"
            f"Тип: {label}"
        ),
        priority="normal",
        player_steam_id=steam or None,
        created_by=steam or f"discord:{row['user_id']}",
    )
    site_id = created["id"]
    conn = get_conn()
    conn.execute(
        "UPDATE tickets SET site_ticket_id = ? WHERE channel_id = ?",
        (site_id, channel.id),
    )
    conn.commit()
    return site_id


async def _sync_transcript(channel, row=None, by_id=None):
    """Автопуш транскрипта закрытого тикета в сайт-тикет (идемпотентно)."""
    async with _transcript_locks[channel.id]:
        await _sync_transcript_locked(channel, by_id)


async def _sync_transcript_locked(channel, by_id=None):
    try:
        db = _panel_db()
        conn = get_conn()
        # Перечитываем строку под замком: флаг transcript_saved мог измениться.
        row = conn.execute(
            "SELECT * FROM tickets WHERE channel_id = ?", (channel.id,)
        ).fetchone()
        if not row:
            return
        if int(_row_get(row, "transcript_saved") or 0):
            return
        site_id = await _ensure_site_ticket(row, channel)
        transcript = await _build_transcript(channel)
        author = None
        if by_id:
            author = await db.get_steam_by_discord(str(by_id))
        if not author:
            author = await db.get_steam_by_discord(str(row["user_id"]))
        author = author or f"discord:{row['user_id']}"
        chunks = _transcript_chunks(transcript)
        for i, chunk in enumerate(chunks, 1):
            header = f"[Транскрипт #{channel.name}"
            if len(chunks) > 1:
                header += f", часть {i}/{len(chunks)}"
            header += "]"
            await db.add_ticket_comment(site_id, author, f"{header}\n{chunk}")
        conn.execute(
            "UPDATE tickets SET transcript_saved = 1 WHERE channel_id = ?",
            (channel.id,),
        )
        conn.commit()
        try:
            await db.log_site_audit(
                author, None, "ticket.transcript",
                f"{site_id} · discord channel {channel.id}",
            )
        except Exception:
            pass
        log.info("Транскрипт канала %s сохранён в сайт-тикет %s", channel.id, site_id)
    except Exception:
        log.exception("Не удалось сохранить транскрипт канала %s в панель", channel.id)

_FALLBACK_TYPE = {
    "type_id": "complaint",
    "label": "Жалоба",
    "description": "Отправьте жалобу — администрация разберётся в ближайшее время.",
    "emoji": "📢",
    "color": 9109504,
}


def _load_types():
    """Типы тикетов из БД (правятся в панели). Пусто/ошибка — фолбэк.

    'clan' исключён: у регистрации клана своя кнопка и своя категория.
    """
    try:
        rows = [r for r in database.list_ticket_types() if r.get("type_id") != "clan"]
    except Exception:
        log.exception("ticket_types: не удалось прочитать типы из БД")
        rows = []
    return rows or [dict(_FALLBACK_TYPE)]


def _get_type(type_id: str):
    try:
        row = database.get_ticket_type(type_id)
    except Exception:
        log.exception("ticket_types: не удалось прочитать тип %s", type_id)
        row = None
    return row or dict(_FALLBACK_TYPE)


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Создать тикет", style=discord.ButtonStyle.green, custom_id="tk2_create")
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open_type_selector(interaction)

    async def _open_type_selector(self, interaction: discord.Interaction):
        view = discord.ui.View(timeout=60)
        options = []
        for row in _load_types()[:25]:
            emoji = (row.get("emoji") or "").strip() or None
            options.append(discord.SelectOption(
                label=(row.get("label") or row["type_id"])[:100],
                value=row["type_id"][:100],
                description=(row.get("description") or "")[:100] or None,
                emoji=emoji,
            ))
        select = discord.ui.Select(
            placeholder="Выберите тип тикета...",
            options=options,
        )
        select.callback = self._on_type
        view.add_item(select)
        await interaction.response.send_message("**Выберите тип тикета:**", view=view, ephemeral=True)

    async def _on_type(self, interaction: discord.Interaction):
        ticket_type = interaction.data["values"][0]
        await self._create_ticket(interaction, ticket_type)

    async def _create_ticket(self, interaction: discord.Interaction, ticket_type: str):
        await interaction.response.defer(ephemeral=True)
        async with _ticket_locks[interaction.user.id]:
            await self._create_ticket_locked(interaction, ticket_type)

    async def _create_ticket_locked(self, interaction: discord.Interaction, ticket_type: str):
        conn = get_conn()
        existing = conn.execute(
            "SELECT 1 FROM tickets WHERE user_id = ? AND status = 'open'",
            (interaction.user.id,),
        ).fetchone()
        if existing:
            return await interaction.followup.send("У вас уже есть открытый тикет.", ephemeral=True)

        guild = interaction.guild
        user = interaction.user
        category = guild.get_channel(config.TICKET_CATEGORY)
        if not category:
            return await interaction.followup.send("❌ Категория тикетов не настроена.", ephemeral=True)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                attach_files=True, embed_links=True,
            ),
        }
        for staff_role_id in config.TICKET_STAFF_ROLES:
            staff = guild.get_role(staff_role_id)
            if staff:
                overwrites[staff] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                )

        ttype = _get_type(ticket_type)
        label = ttype.get("label") or "Тикет"
        desc = ttype.get("description") or ""
        color = discord.Color(int(ttype.get("color") or 0x5865F2) & 0xFFFFFF)
        try:
            channel = await guild.create_text_channel(
                name=f"тикет-{user.name}",
                category=category,
                overwrites=overwrites,
                topic=f"{label} от {user} ({user.id})",
            )
        except discord.HTTPException as e:
            log.warning("Ошибка создания тикета для %s: %s", user, e)
            return await interaction.followup.send(
                "❌ Не удалось создать тикет (ошибка Discord).", ephemeral=True
            )
        except Exception as e:
            log.exception("Непредвиденная ошибка создания тикета: %s", e)
            return await interaction.followup.send(
                "❌ Не удалось создать тикет.", ephemeral=True
            )

        embed = discord.Embed(title=label, description=desc, color=color, timestamp=datetime.utcnow())
        embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)
        embed.set_footer(text=f"ID: {user.id}")

        staff_mention = " ".join(f"<@&{r}>" for r in config.TICKET_STAFF_ROLES)
        # Пингуем только персонал и автора; @everyone и прочие роли не трогаем.
        mentions = discord.AllowedMentions(everyone=False, roles=True, users=True)
        # Канал уже создан: при любом сбое после этого удаляем его, иначе останется сирота.
        try:
            await channel.send(
                content=f"{staff_mention} {user.mention}", embed=embed,
                view=TicketCloseView(), allowed_mentions=mentions,
            )
            conn.execute(
                "INSERT INTO tickets (user_id, channel_id, ticket_type, status, created_at) VALUES (?, ?, ?, 'open', ?)",
                (user.id, channel.id, ticket_type, datetime.utcnow().isoformat()),
            )
            conn.commit()
        except Exception:
            log.exception("Ошибка после создания канала тикета %s, откат", channel.id)
            try:
                await channel.delete()
            except Exception:
                log.exception("Не удалось удалить сиротский канал тикета %s", channel.id)
            return await interaction.followup.send("❌ Не удалось создать тикет.", ephemeral=True)

        site_row = conn.execute(
            "SELECT * FROM tickets WHERE channel_id = ?", (channel.id,)
        ).fetchone()
        try:
            await _ensure_site_ticket(site_row, channel)
        except Exception:
            log.exception("Не удалось создать сайт-тикет для канала %s", channel.id)

        await interaction.followup.send(f"Тикет создан: {channel.mention}", ephemeral=True)

        if config.TICKET_LOG_CHANNEL:
            log_ch = guild.get_channel(config.TICKET_LOG_CHANNEL)
            if log_ch:
                await log_ch.send(
                    f"🎫 Тикет создан: {channel.mention} | Пользователь: {user} | Тип: {label}",
                    allowed_mentions=discord.AllowedMentions.none(),
                )


class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Закрыть тикет", style=discord.ButtonStyle.red, custom_id="tk2_close", emoji="🔒")
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Вы уверены?", view=TicketCloseConfirmView(), ephemeral=True
        )


class TicketCloseConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="Да", style=discord.ButtonStyle.green, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        channel = interaction.channel
        async with _channel_locks[channel.id]:
            conn = get_conn()
            row = conn.execute(
                "SELECT * FROM tickets WHERE channel_id = ?", (channel.id,)
            ).fetchone()
            # Двойной клик «Да»: второй проход не закрывает заново и не дублирует логи.
            already_closed = bool(row) and _row_get(row, "status") == "closed"
            if row and not already_closed:
                conn.execute(
                    "UPDATE tickets SET status = 'closed', closed_at = ?, closed_by = ? WHERE channel_id = ?",
                    (datetime.utcnow().isoformat(), interaction.user.id, channel.id),
                )
                conn.commit()
                # Транскрипт уходит в панель сразу при закрытии — до правок прав,
                # чтобы сбой прав не лишил нас автосохранения.
                await _sync_transcript(channel, row, by_id=interaction.user.id)
                try:
                    member = interaction.guild.get_member(row["user_id"])
                    if member:
                        # Автор тикета больше не видит канал.
                        await channel.set_permissions(member, overwrite=None)
                    # Дублируем «слепоту» через @everyone-перезапись как страховку
                    # (если права давались не только явным override).
                    await channel.set_permissions(
                        interaction.guild.default_role, view_channel=False
                    )
                    # Персонал оставляет доступ после закрытия.
                    for staff_role_id in config.TICKET_STAFF_ROLES:
                        staff = interaction.guild.get_role(staff_role_id)
                        if staff:
                            await channel.set_permissions(
                                staff, view_channel=True, send_messages=False, read_message_history=True
                            )
                except Exception:
                    log.exception("Не удалось обновить права закрытого тикета %s", channel.id)

        embed = discord.Embed(
            title="Тикет закрыт",
            description=f"Закрыл: {interaction.user.mention}\nСохраните транскрипт или удалите канал.",
            color=discord.Color.greyple(),
        )
        await interaction.message.edit(embed=embed, view=TicketClosedView())

        if not already_closed and config.TICKET_LOG_CHANNEL:
            log_ch = interaction.guild.get_channel(config.TICKET_LOG_CHANNEL)
            if log_ch:
                await log_ch.send(
                    f"🎫 Тикет закрыт: {channel.mention} | Закрыл: {interaction.user}",
                    allowed_mentions=discord.AllowedMentions.none(),
                )

    @discord.ui.button(label="Нет", style=discord.ButtonStyle.red, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Закрытие отменено.", view=None)


class TicketClosedView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    def _is_staff(self, interaction: discord.Interaction) -> bool:
        """True если пользователь имеет одну из ролей персонала или manage_channels."""
        member = interaction.user
        if member.guild_permissions.manage_channels:
            return True
        member_role_ids = {r.id for r in getattr(member, "roles", [])}
        return bool(member_role_ids & set(config.TICKET_STAFF_ROLES))

    @discord.ui.button(label="Удалить канал", style=discord.ButtonStyle.danger, custom_id="tk2_delete", emoji="🗑️")
    async def delete_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_staff(interaction):
            return await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
        await interaction.response.defer()
        channel = interaction.channel
        async with _channel_locks[channel.id]:
            # История удаляется вместе с каналом — транскрипт сохраняем до удаления.
            await _sync_transcript(channel, by_id=interaction.user.id)
            try:
                await channel.delete()
            except discord.NotFound:
                pass  # второй клик: канал уже удалён
            except discord.HTTPException as e:
                log.warning("Не удалось удалить канал тикета %s: %s", channel.id, e)
                # Запись не трогаем: канал жив, и тикет должен остаться в БД.
                return await interaction.followup.send("❌ Не удалось удалить канал.", ephemeral=True)
            # Запись удаляем только после успешного удаления канала.
            conn = get_conn()
            conn.execute("DELETE FROM tickets WHERE channel_id = ?", (channel.id,))
            conn.commit()

    @discord.ui.button(label="Сохранить транскрипт", style=discord.ButtonStyle.blurple, custom_id="tk2_transcript", emoji="📄")
    async def save_transcript(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_staff(interaction):
            return await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
        await interaction.response.defer()
        transcript = await _build_transcript(interaction.channel)
        file = discord.File(
            fp=BytesIO(transcript.encode("utf-8")),
            filename=f"transcript-{interaction.channel.name}.txt",
        )

        # Дублируем транскрипт в панель (идемпотентно — при закрытии уже мог уйти).
        await _sync_transcript(interaction.channel, by_id=interaction.user.id)

        try:
            if config.TICKET_TRANSCRIPT_CHANNEL:
                tr_ch = interaction.guild.get_channel(config.TICKET_TRANSCRIPT_CHANNEL)
                if tr_ch:
                    await tr_ch.send(file=file, allowed_mentions=discord.AllowedMentions.none())
                    return await interaction.followup.send("Транскрипт сохранён.", ephemeral=True)
            await interaction.followup.send(file=file, ephemeral=True)
        except discord.HTTPException:
            log.exception("Не удалось отправить транскрипт канала %s", interaction.channel.id)
            await interaction.followup.send("❌ Не удалось отправить транскрипт.", ephemeral=True)


class TicketCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    ticket_group = app_commands.Group(name="tickets", description="Управление тикетами")

    @ticket_group.command(name="panel", description="Отправить панель создания тикетов")
    @app_commands.describe(channel="Канал для панели")
    @app_commands.guild_only()
    async def ticket_panel(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❌ Нет прав.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        target = channel or interaction.channel

        lines = ["Нажмите кнопку ниже и выберите тип обращения."]
        for row in _load_types():
            emoji = (row.get("emoji") or "").strip()
            prefix = f"{emoji} " if emoji else ""
            lines.append(f"**{prefix}{row.get('label') or row['type_id']}** — {row.get('description') or '—'}")
        embed = discord.Embed(
            title="📨 Создать тикет",
            description="\n".join(lines),
            color=discord.Color.dark_red(),
        )
        try:
            await target.send(embed=embed, view=TicketPanelView())
        except discord.HTTPException:
            log.exception("Не удалось отправить панель тикетов в %s", target.id)
            return await interaction.followup.send("❌ Не удалось отправить панель.", ephemeral=True)
        await interaction.followup.send(f"Панель отправлена в {target.mention}.", ephemeral=True)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        # Канал удалён вручную (не через кнопку): открытая запись блокировала бы
        # автору создание нового тикета навсегда. Помечаем закрытой.
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            conn = get_conn()
            conn.execute(
                "UPDATE tickets SET status = 'closed', closed_at = ? WHERE channel_id = ? AND status = 'open'",
                (datetime.utcnow().isoformat(), channel.id),
            )
            conn.commit()
        except Exception:
            log.exception("Не удалось пометить тикет канала %s закрытым", channel.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(TicketCog(bot))
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
from database import get_conn

log = logging.getLogger("clan_tickets")

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

CLAN_EMBED_DESCRIPTION = CLAN_APPLICATION_FORM


class ClanTicketPanelView(discord.ui.View):
    """Persistent-панель регистрации клана."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Зарегистрировать клан", style=discord.ButtonStyle.primary, custom_id="ck_create", emoji="📝")
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
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

        embed = discord.Embed(
            title="📝 ЗАЯВКА НА РЕГИСТРАЦИЮ КЛАНА",
            description=CLAN_EMBED_DESCRIPTION,
            color=discord.Color.gold(),
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

    clan_group = app_commands.Group(name="clan", description="Управление заявками на регистрацию клана")

    @clan_group.command(name="panel", description="Отправить панель регистрации кланов")
    @app_commands.describe(channel="Канал для панели")
    @app_commands.guild_only()
    async def clan_panel(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❌ Нет прав.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        target = channel or interaction.channel

        embed = discord.Embed(
            title="⚔️ ЗАЯВКА НА РЕГИСТРАЦИЮ КЛАНА",
            description=CLAN_APPLICATION_FORM,
            color=discord.Color.gold(),
        )
        await target.send(embed=embed, view=ClanTicketPanelView())
        await interaction.followup.send(f"Панель отправлена в {target.mention}.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ClanTicketCog(bot))
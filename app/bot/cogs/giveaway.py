import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("giveaway")

from database import (
    add_participant,
    create_giveaway,
    get_active_giveaways,
    get_giveaway,
    get_giveaway_by_message,
    get_participant_count,
    get_participants,
    insert_recovered_giveaway,
    remove_participant,
    set_giveaway_done,
    set_giveaway_message_id,
)


def _embed_for(g: dict) -> discord.Embed:
    end_dt = datetime.fromisoformat(g["ends_at"])
    ts = int(end_dt.timestamp())
    embed = discord.Embed(
        title=f"🎉 {g['prize']}",
        description=(
            "Нажми **🎉 Участвовать**, чтобы принять участие в розыгрыше!\n\n"
            f"⏰ До окончания: <t:{ts}:R>\n"
            f"📅 Завершится: <t:{ts}:f>\n"
            f"🎁 Кол-во победителей: **{g['winners']}**\n"
            f"👑 Устроил: <@{g['created_by']}>"
        ),
        color=discord.Color.green(),
    )
    count = get_participant_count(g["id"])
    embed.set_footer(text=f"Участников: {count} • ID розыгрыша: {g['id']}")
    return embed


class JoinButton(discord.ui.Button):
    def __init__(self, giveaway_id: int):
        super().__init__(
            label="🎉 Участвовать",
            style=discord.ButtonStyle.success,
            custom_id=f"gwa_join:{giveaway_id}",
        )

    async def callback(self, interaction: discord.Interaction):
        await self.view.on_join(interaction)


class GiveawayView(discord.ui.View):
    def __init__(self, giveaway_id: int):
        super().__init__(timeout=None)
        self.giveaway_id = giveaway_id
        self.add_item(JoinButton(giveaway_id))

    async def on_join(self, interaction: discord.Interaction):
        gid = self.giveaway_id
        g = get_giveaway(gid)
        if g is None or g["done"]:
            await interaction.response.send_message("❌ Этот розыгрыш уже завершён.", ephemeral=True)
            return
        now = datetime.now(timezone.utc)
        if datetime.fromisoformat(g["ends_at"]) <= now:
            await interaction.response.send_message("❌ Розыгрыш уже закончился.", ephemeral=True)
            return
        entered = add_participant(self.giveaway_id, interaction.user.id)
        if not entered:
            await interaction.response.send_message("ℹ️ Ты уже участвуешь в этом розыгрыше!", ephemeral=True)
            return
        try:
            await interaction.message.edit(embed=_embed_for(g))
        except Exception:
            pass
        await interaction.response.send_message("✅ Ты участвуешь! Удачи! 🍀", ephemeral=True)


class GiveawayCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._check_task: asyncio.Task | None = None
        self._views: dict[int, GiveawayView] = {}

    # ---------- Автоподведение итогов ----------

    async def cog_load(self):
        await self.reload_views()
        self._check_task = asyncio.create_task(self._check_loop())

    async def reload_views(self):
        """Пере-регистрирует persistent-кнопки активных розыгрышей.
        Нужно вызывать заново после восстановления БД из канала в on_ready,
        иначе кнопки на восстановленных розыгрышах не отвечают."""
        for gid, view in list(self._views.items()):
            try:
                self.bot.remove_view(view)
            except Exception:
                pass
        self._views.clear()
        for g in get_active_giveaways():
            view = GiveawayView(g["id"])
            self._views[g["id"]] = view
            try:
                self.bot.add_view(view)
            except Exception:
                pass
        if self._views:
            log.info("Зарегистрировано кнопок розыгрышей: %d", len(self._views))

    async def _check_loop(self):
        await self.bot.wait_until_ready()
        while True:
            try:
                await self._check_giveaways()
            except Exception:
                pass
            await asyncio.sleep(15)

    async def _finish(self, g: dict):
        participants = get_participants(g["id"])
        winners_to_pick = min(int(g["winners"]), len(participants))
        winners = random.sample(participants, winners_to_pick) if winners_to_pick else []

        channel = self.bot.get_channel(int(g["channel_id"]))
        if channel is None:
            set_giveaway_done(g["id"])
            return

        end_dt = datetime.fromisoformat(g["ends_at"])
        embed = discord.Embed(
            title=f"🎉 Розыгрыш завершён: {g['prize']}",
            description=f"📅 Завершён: <t:{int(end_dt.timestamp())}:f>",
            color=discord.Color.orange(),
        )
        embed.set_footer(text=f"Участников было: {len(participants)}")

        if winners:
            win_mentions = " ".join(f"<@{w}>" for w in winners)
            if len(winners) > 1:
                desc = f"Победители: {win_mentions}\n\n🎁 Поздравляем! Вы выиграли **{g['prize']}**!"
            else:
                desc = f"Победитель: {win_mentions}\n\n🎁 Поздравляем! Ты выиграл **{g['prize']}**!"
            embed.description = desc
            embed.color = discord.Color.gold()
        else:
            embed.description = "😔 В розыгрыше никто не участвовал. Победитель не выбран."

        await channel.send(embed=embed)
        for w in winners:
            try:
                user = await self.bot.fetch_user(w)
                await user.send(f"🎉 Поздравляем, **{user.display_name}**! Ты выиграл **{g['prize']}** в розыгрыше на сервере **{channel.guild.name}**!")
            except Exception:
                pass

        try:
            msg = await channel.fetch_message(int(g["message_id"]))
            await msg.edit(view=None)
        except Exception:
            pass

        set_giveaway_done(g["id"])

    async def _check_giveaways(self):
        now = datetime.now(timezone.utc)
        for g in get_active_giveaways():
            end_dt = datetime.fromisoformat(g["ends_at"])
            if end_dt <= now:
                await self._finish(g)

    # ---------- Восстановление розыгрыша из канала ----------

    async def recover_giveaways(self, guild: discord.Guild, limit: int = 200):
        """Сканирует каналы гильдии в поисках эмбедов розыгрышей, которых нет в БД,
        и воссоздаёт их активными (с персистентной кнопкой).

        Бывших участников восстановить нельзя — только тех, кто нажмёт заново."""
        recovered = 0
        text_channels = [c for c in guild.text_channels]
        for ch in text_channels:
            try:
                async for msg in ch.history(limit=limit):
                    if msg.author.id != self.bot.user.id:
                        continue
                    embed = msg.embeds[0] if msg.embeds else None
                    if embed is None or embed.title is None or "🎉" not in embed.title:
                        continue
                    footer_text = embed.footer.text if embed.footer else ""
                    if "ID розыгрыша" not in footer_text:
                        continue
                    if get_giveaway_by_message(ch.id, msg.id):
                        continue

                    # Уже восстановлен и стоит кнопка — пропускаем.
                    if msg.components and any(any(
                        isinstance(c, discord.ui.Button) for c in row.children
                    ) for row in msg.components):
                        continue

                    data = self._parse_giveaway_embed(embed)
                    if data is None:
                        continue
                    gid = insert_recovered_giveaway(
                        channel_id=ch.id,
                        guild_id=guild.id,
                        prize=data["prize"],
                        ends_at=data["ends_at"],
                        winners=data["winners"],
                        created_by=data["created_by"],
                        message_id=msg.id,
                    )
                    view = GiveawayView(gid)
                    self._views[gid] = view
                    try:
                        await msg.edit(embed=_embed_for(get_giveaway(gid)), view=view)
                        recovered += 1
                    except Exception:
                        pass
            except (discord.Forbidden, discord.NotFound):
                continue
            except Exception:
                continue
        if recovered:
            log.info("Восстановлено розыгрышей из каналов %s: %s", guild.name, recovered)
        return recovered

    @staticmethod
    def _parse_giveaway_embed(embed: discord.Embed) -> dict | None:
        """Разбирает эмбед розыгрыша: приз, окончание, победители, устроитель."""
        title = embed.title or ""
        if "🎉" in title:
            prize = title.replace("🎉", "").strip()
        else:
            return None
        desc = embed.description or ""
        ends_at = None
        winners = 1
        created_by = None
        import re
        for line in desc.splitlines():
            line = line.strip()
            m = re.search(r"<t:(\d+):[RtfFdD]>", line)
            if m and ends_at is None:
                ends_at = datetime.fromtimestamp(int(m.group(1)), timezone.utc).isoformat()
            m = re.search(r"Кол-во победителей: \*\*(\d+)\*\*", line)
            if m:
                winners = max(1, min(int(m.group(1)), 50))
            m = re.search(r"Устроил: <@(\d+)>", line)
            if m:
                created_by = int(m.group(1))
        if not ends_at or not created_by:
            return None
        return {
            "prize": prize,
            "ends_at": ends_at,
            "winners": winners,
            "created_by": created_by,
        }

    # ---------- Команды ----------

    giveaway = app_commands.Group(name="giveaway", description="Розыгрыши", guild_only=True, default_permissions=discord.Permissions(manage_guild=True))

    @giveaway.command(name="create", description="Создать розыгрыш")
    @app_commands.describe(
        prize="Что разыгрываем",
        length="Длительность (число)",
        unit="Единица времени: минуты / часы / дни",
        winners="Количество призовых мест (победителей)",
        channel="Где создать розыгрыш (по умолчанию текущий канал)",
    )
    @app_commands.choices(unit=[
        app_commands.Choice(name="минуты", value="minutes"),
        app_commands.Choice(name="часы", value="hours"),
        app_commands.Choice(name="дни", value="days"),
    ])
    async def giveaway_create(
        self,
        interaction: discord.Interaction,
        prize: str,
        length: int,
        winners: int,
        unit: app_commands.Choice[str] = None,
        channel: discord.TextChannel = None,
    ):
        if unit is None:
            unit = app_commands.Choice(name="часы", value="hours")
        length = max(1, abs(length))
        winners = max(1, min(winners, 50))
        td = timedelta(
            minutes=length if unit.value == "minutes" else 0,
            hours=length if unit.value == "hours" else 0,
            days=length if unit.value == "days" else 0,
        )
        if td.total_seconds() <= 0:
            td = timedelta(hours=length)
        td = min(td, timedelta(days=365))
        ends_at = (datetime.now(timezone.utc) + td).isoformat()
        gid = create_giveaway(
            channel_id=(channel or interaction.channel).id,
            guild_id=interaction.guild.id,
            prize=prize,
            ends_at=ends_at,
            winners=winners,
            created_by=interaction.user.id,
        )
        g = get_giveaway(gid)

        target = channel or interaction.channel
        view = GiveawayView(gid)
        self._views[gid] = view

        if interaction.channel == target:
            await interaction.response.defer(ephemeral=True)
            sent_msg = await target.send(embed=_embed_for(g), view=view)
        else:
            await interaction.response.send_message(f"✅ Розыгрыш создан в {target.mention}!", ephemeral=True)
            sent_msg = await target.send(embed=_embed_for(g), view=view)

        set_giveaway_message_id(gid, sent_msg.id)

    @giveaway.command(name="end", description="Досрочно завершить розыгрыш (выбрать победителя сейчас)")
    @app_commands.describe(giveaway_id="ID розыгрыша (внизу сообщения розыгрыша)")
    async def giveaway_end(self, interaction: discord.Interaction, giveaway_id: int):
        g = get_giveaway(giveaway_id)
        if g is None:
            await interaction.response.send_message("❌ Розыгрыш не найден.", ephemeral=True)
            return
        if g["done"]:
            await interaction.response.send_message("ℹ️ Розыгрыш уже завершён.", ephemeral=True)
            return
        await interaction.response.send_message("⏳ Завершаю розыгрыш и выбираю победителя...", ephemeral=True)
        set_giveaway_done(g["id"])
        await self._finish(g)

    @giveaway.command(name="list", description="Список активных розыгрышей")
    async def giveaway_list(self, interaction: discord.Interaction):
        gws = get_active_giveaways()
        if not gws:
            await interaction.response.send_message("📭 Активных розыгрышей нет.", ephemeral=True)
            return
        lines = []
        for g in gws:
            end_dt = datetime.fromisoformat(g["ends_at"])
            ts = int(end_dt.timestamp())
            count = get_participant_count(g["id"])
            lines.append(f"**{g['id']}.** {g['prize']} — <t:{ts}:R> • 🎁 {g['winners']} • 👥 {count}")
        await interaction.response.send_message("**Активные розыгрыши:**\n" + "\n".join(lines), ephemeral=True)

    @giveaway.command(name="leave", description="Выйти из розыгрыша (снять своё участие)")
    @app_commands.describe(giveaway_id="ID розыгрыша (внизу сообщения розыгрыша)")
    async def giveaway_leave(self, interaction: discord.Interaction, giveaway_id: int):
        g = get_giveaway(giveaway_id)
        if g is None:
            await interaction.response.send_message("❌ Розыгрыш не найден.", ephemeral=True)
            return
        if g["done"]:
            await interaction.response.send_message("ℹ️ Розыгрыш уже завершён.", ephemeral=True)
            return
        now = datetime.now(timezone.utc)
        if datetime.fromisoformat(g["ends_at"]) <= now:
            await interaction.response.send_message("❌ Розыгрыш уже закончился.", ephemeral=True)
            return
        if not remove_participant(giveaway_id, interaction.user.id):
            await interaction.response.send_message("ℹ️ Ты не участвуешь в этом розыгрыше.", ephemeral=True)
            return
        try:
            await self._refresh_message(interaction.channel, g)
        except Exception:
            pass
        await interaction.response.send_message("✅ Ты вышел из розыгрыша.", ephemeral=True)

    @giveaway.command(name="remove", description="Снять участника с розыгрыша (модерация)")
    @app_commands.describe(
        giveaway_id="ID розыгрыша (внизу сообщения розыгрыша)",
        user="Пользователь, которого снять",
    )
    @app_commands.default_permissions(manage_messages=True)
    async def giveaway_remove(self, interaction: discord.Interaction, giveaway_id: int, user: discord.User):
        g = get_giveaway(giveaway_id)
        if g is None:
            await interaction.response.send_message("❌ Розыгрыш не найден.", ephemeral=True)
            return
        if g["done"]:
            await interaction.response.send_message("ℹ️ Розыгрыш уже завершён.", ephemeral=True)
            return
        if not remove_participant(giveaway_id, user.id):
            await interaction.response.send_message("ℹ️ Этот пользователь не участвует в розыгрыше.", ephemeral=True)
            return
        try:
            await self._refresh_message(interaction.channel, g)
        except Exception:
            pass
        await interaction.response.send_message(f"✅ Участник <@{user.id}> снят с розыгрыша.", ephemeral=True)

    async def _refresh_message(self, channel: discord.TextChannel, g: dict):
        """Перерисовать эмбед розыгрыша с актуальным счётчиком участников."""
        if not g.get("message_id"):
            return
        try:
            msg = await channel.fetch_message(int(g["message_id"]))
            await msg.edit(embed=_embed_for(g))
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(GiveawayCog(bot))
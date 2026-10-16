import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import logging
import discord
from discord.ext import commands, tasks

import config

log = logging.getLogger("temp_voice")

temp_channel_owners: dict[int, int] = {}
_channel_locks: dict[int, asyncio.Lock] = {}


def _is_trigger(vc) -> bool:
    return vc.id == config.VC_TRIGGER_CHANNEL


def _is_managed(category_id: int) -> bool:
    return category_id == config.VC_CATEGORY


def _lock(channel_id: int) -> asyncio.Lock:
    if channel_id not in _channel_locks:
        _channel_locks[channel_id] = asyncio.Lock()
    return _channel_locks[channel_id]


def _get_managed_vc(interaction: discord.Interaction):
    """Возвращает временный канал пользователя, если он владелец и находится в нём."""
    if not interaction.user.voice or not interaction.user.voice.channel:
        return None, "Вы не в голосовом канале."
    vc = interaction.user.voice.channel
    if _is_trigger(vc) or not _is_managed(vc.category_id):
        return None, "Этот канал не управляется."
    owner_id = temp_channel_owners.get(vc.id)
    if owner_id is None:
        # Владелец мог пропасть после рестарта бота — управлять может любой в канале.
        return vc, None
    if owner_id != interaction.user.id:
        return None, "Только владелец канала может управлять."
    return vc, None


# ---------- Модалки ----------

class Modals:
    pass


def _make_modals(vc):
    class RenameModal(discord.ui.Modal, title="Переименовать канал"):
        name = discord.ui.TextInput(label="Название", required=True, max_length=100)

        async def on_submit(self, interaction: discord.Interaction):
            old = vc.name
            await vc.edit(name=self.name.value, reason="Переименован владельцем")
            await interaction.response.send_message(f"✅ `{old}` → `{self.name.value}`", ephemeral=True)

    class LimitModal(discord.ui.Modal, title="Лимит участников"):
        limit = discord.ui.TextInput(label="Лимит (0 = без лимита, до 99)", required=True)

        async def on_submit(self, interaction: discord.Interaction):
            try:
                n = int(self.limit.value)
            except ValueError:
                return await interaction.response.send_message("Введите число.", ephemeral=True)
            if not 0 <= n <= 99:
                return await interaction.response.send_message("От 0 до 99.", ephemeral=True)
            await vc.edit(user_limit=n, reason="Лимит изменён")
            await interaction.response.send_message(
                f"✅ Лимит: **{n}**" if n else "✅ Лимит снят.", ephemeral=True
            )

    class InviteModal(discord.ui.Modal, title="Пригласить участника"):
        user = discord.ui.TextInput(label="ID или упоминание (можно несколько)", required=False)

        async def on_submit(self, interaction: discord.Interaction):
            raw = self.user.value.strip().replace("<@!", "").replace("<@", "").replace(">", "")
            ids = [x for x in raw.replace(",", " ").split() if x.strip().isdigit()]
            if not ids:
                bot = interaction.client
                # Если не введён ID — даём ссылку-приглашение
                invite = await vc.create_invite(max_age=0, max_uses=0, reason="Приглашение владельцем")
                return await interaction.response.send_message(f"🔗 Ссылка: {invite.url}", ephemeral=True)

            ok = []
            bad = []
            for uid in ids:
                member = interaction.guild.get_member(int(uid))
                if not member:
                    try:
                        member = await interaction.guild.fetch_member(int(uid))
                    except discord.NotFound:
                        bad.append(uid)
                        continue
                await vc.set_permissions(member, view_channel=True, connect=True)
                ok.append(member.display_name)
            msg = "✅ Доступ выдано: " + ", ".join(ok) if ok else "❌ Никого не нашёл"
            if bad:
                msg += f"\n⚠️ Не найдены: {', '.join(bad)}"
            await interaction.response.send_message(msg, ephemeral=True)

    class KickEnum:
        pass

    class KickModal(discord.ui.Modal, title="Выгнать участника"):
        user = discord.ui.TextInput(label="ID или упоминание участника", required=True)

        async def on_submit(self, interaction: discord.Interaction):
            raw = self.user.value.strip().strip("<@!>")
            try:
                uid = int(raw)
            except ValueError:
                return await interaction.response.send_message("Укажите ID.", ephemeral=True)
            member = interaction.guild.get_member(uid)
            if not member:
                return await interaction.response.send_message("Участник не найден.", ephemeral=True)
            if member.voice and member.voice.channel and member.voice.channel.id == vc.id:
                await member.move_to(None, reason="Выгнан из временного канала")
                await interaction.response.send_message(f"✅ {member.mention} выгнан.", ephemeral=True)
            else:
                await interaction.response.send_message("Участник не в вашем канале.", ephemeral=True)

    return RenameModal, LimitModal, InviteModal, KickModal


# ---------- Панель ----------

class VoiceControlPanelView(discord.ui.View):
    """Постоянная панель в VC_CONTROL_CHANNEL."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _vc(self, interaction: discord.Interaction):
        return _get_managed_vc(interaction)

    @discord.ui.button(label="🔒 Закрыть/Открыть", style=discord.ButtonStyle.danger, custom_id="vc2_panel_lock")
    async def panel_lock(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Сначала мгновенно подтверждаем взаимодействие: set_permissions может
        # не уложиться в 3 секунды окна ответа Discord.
        await interaction.response.defer(ephemeral=True)
        vc, err = await self._vc(interaction)
        if err:
            return await interaction.followup.send(err, ephemeral=True)

        everyone = interaction.guild.default_role
        current = vc.overwrites_for(everyone)
        is_locked = current.connect is False

        if is_locked:
            current.connect = None
            await vc.set_permissions(everyone, overwrite=current, reason="Канал открыт")
            text = "✅ Канал открыт."
        else:
            current.connect = False
            await vc.set_permissions(everyone, overwrite=current, reason="Канал закрыт")
            text = "🔒 Канал закрыт."
        await interaction.followup.send(f"{text} (`{vc.name}`)", ephemeral=True)

    @discord.ui.button(label="✦ Видимость", style=discord.ButtonStyle.secondary, custom_id="vc2_panel_visibility")
    async def panel_visibility(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        vc, err = await self._vc(interaction)
        if err:
            return await interaction.followup.send(err, ephemeral=True)
        everyone = interaction.guild.default_role
        current = vc.overwrites_for(everyone)
        is_hidden = current.view_channel is False
        if is_hidden:
            current.view_channel = None
            await vc.set_permissions(everyone, overwrite=current, reason="Канал видимый")
            text = "✅ Канал стал видимым."
        else:
            current.view_channel = False
            await vc.set_permissions(everyone, overwrite=current, reason="Канал скрыт")
            text = "🙈 Канал скрыт (приватно)."
        await interaction.followup.send(text, ephemeral=True)

    @discord.ui.button(label="👢 Выгнать", style=discord.ButtonStyle.secondary, custom_id="vc2_panel_kick")
    async def panel_kick(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc, err = await self._vc(interaction)
        if err:
            return await interaction.response.send_message(err, ephemeral=True)
        _, _, _, KickModal = _make_modals(vc)
        await interaction.response.send_modal(KickModal())

    @discord.ui.button(label="✏️ Название", style=discord.ButtonStyle.primary, custom_id="vc2_panel_rename")
    async def panel_rename(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc, err = await self._vc(interaction)
        if err:
            return await interaction.response.send_message(err, ephemeral=True)
        RenameModal, _, _, _ = _make_modals(vc)
        await interaction.response.send_modal(RenameModal())

    @discord.ui.button(label="👥 Лимит", style=discord.ButtonStyle.success, custom_id="vc2_panel_limit")
    async def panel_limit(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc, err = await self._vc(interaction)
        if err:
            return await interaction.response.send_message(err, ephemeral=True)
        _, LimitModal, _, _ = _make_modals(vc)
        await interaction.response.send_modal(LimitModal())

    @discord.ui.button(label="🎟 Пригласить", style=discord.ButtonStyle.primary, custom_id="vc2_panel_invite")
    async def panel_invite(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc, err = await self._vc(interaction)
        if err:
            return await interaction.response.send_message(err, ephemeral=True)
        _, _, InviteModal, _ = _make_modals(vc)
        await interaction.response.send_modal(InviteModal())


# ---------- Ког ----------

class TempVoiceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @tasks.loop(seconds=60)
    async def cleanup_empty(self):
        for guild in self.bot.guilds:
            category = guild.get_channel(config.VC_CATEGORY)
            if not category:
                continue
            for ch in list(category.voice_channels):
                if _is_trigger(ch):
                    continue
                if ch.id not in temp_channel_owners:
                    continue  # удаляем только временные каналы, созданные ботом
                humans = [m for m in ch.members if not m.bot]
                if not humans:
                    async with _lock(ch.id):
                        try:
                            temp_channel_owners.pop(ch.id, None)
                            await ch.delete(reason="Очистка пустого временного канала")
                        except (discord.NotFound, Exception):
                            temp_channel_owners.pop(ch.id, None)

    @cleanup_empty.before_loop
    async def before_cleanup(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.cleanup_empty.is_running():
            self.cleanup_empty.start()

    def _is_temp_channel(self, ch) -> bool:
        return ch.id in temp_channel_owners

    async def _reap_empty_temp(self, guild):
        category = guild.get_channel(config.VC_CATEGORY)
        if not category:
            return
        for ch in list(category.voice_channels):
            if _is_trigger(ch) or ch.id not in temp_channel_owners:
                continue
            humans = [m for m in ch.members if not m.bot]
            if not humans:
                async with _lock(ch.id):
                    try:
                        temp_channel_owners.pop(ch.id, None)
                        await ch.delete(reason="Очистка")
                    except (discord.NotFound, Exception):
                        temp_channel_owners.pop(ch.id, None)

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.cleanup_empty.is_running():
            self.cleanup_empty.start()

        for guild in self.bot.guilds:
            await self._reap_empty_temp(guild)
            # Восстанавливаем владельцев переживших рестарт временных каналов.
            category = guild.get_channel(config.VC_CATEGORY)
            if category:
                for ch in category.voice_channels:
                    if _is_trigger(ch) or ch.id in temp_channel_owners:
                        continue
                    humans = [m for m in ch.members if not m.bot]
                    if humans:
                        temp_channel_owners[ch.id] = humans[0].id
                        log.info("Владелец восстановлен для %s → %s", ch.name, humans[0].display_name)
            trigger = guild.get_channel(config.VC_TRIGGER_CHANNEL)
            if trigger and any(not m.bot for m in trigger.members):
                first = next(m for m in trigger.members if not m.bot)
                await self._create_temp_channel(first, trigger)

    async def _create_temp_channel(self, member: discord.Member, trigger_channel):
        category = member.guild.get_channel(config.VC_CATEGORY)
        try:
            vc = await member.guild.create_voice_channel(
                name=member.display_name,
                category=category,
                reason="Временный канал",
            )
            temp_channel_owners[vc.id] = member.id
            moved = False
            for m in list(trigger_channel.members):
                if m.bot:
                    continue
                try:
                    await m.move_to(vc, reason="Перемещение")
                    moved = True
                except Exception:
                    pass
            if not moved:
                temp_channel_owners.pop(vc.id, None)
                await vc.delete(reason="Никто не перемещён")
        except Exception as e:
            log.error("Ошибка создания temp VC: %s", e)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before, after):
        if member.bot:
            return

        if before.channel and not _is_trigger(before.channel) and _is_managed(before.channel.category_id):
            if after.channel and after.channel.id == before.channel.id:
                return
            vc = before.channel
            # Управляем только временными каналами (созданными ботом).
            if vc.id not in temp_channel_owners:
                return
            async with _lock(vc.id):
                remaining = [m for m in vc.members if not m.bot]
                if not remaining:
                    try:
                        temp_channel_owners.pop(vc.id, None)
                        await vc.delete(reason="Пустой канал")
                    except (discord.NotFound, Exception):
                        temp_channel_owners.pop(vc.id, None)
                elif temp_channel_owners.get(vc.id) == member.id:
                    new_owner = remaining[0]
                    temp_channel_owners[vc.id] = new_owner.id
                    embed = discord.Embed(
                        title="👑 Права переданы",
                        description=f"Новый владелец: **{new_owner.display_name}**",
                        color=discord.Color.gold(),
                    )
                    try:
                        await vc.send(embed=embed)
                    except Exception:
                        pass

        if after.channel and _is_trigger(after.channel):
            await self._create_temp_channel(member, after.channel)

    @discord.app_commands.command(name="voice_panel", description="Отправить панель управления войсами в канал")
    @discord.app_commands.describe(channel="Канал для панели (по умолчанию канал настроек)")
    @discord.app_commands.guild_only()
    async def voice_panel(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❌ Нет прав.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        target = channel or interaction.guild.get_channel(config.VC_CONTROL_CHANNEL)
        if not target:
            return await interaction.followup.send("❌ Канал не найден.", ephemeral=True)

        embed = discord.Embed(
            title="🎙️ Панель управления войсами",
            description=(
                "Управляйте своим временным голосовым каналом.\n\n"
                "**Будучи в своём войсе:**\n"
                "🔒 Закрыть/открыть канал\n"
                "✦ Сделать видимым или приватным\n"
                "👢 Выгнать участника\n"
                "✏️ Переименовать\n"
                "👥 Установить лимит участников\n"
                "🎟 Пригласить участника (ID/упоминание) или получить ссылку"
            ),
            color=discord.Color.blurple(),
        )
        await target.send(embed=embed, view=VoiceControlPanelView())
        await interaction.followup.send(f"Панель отправлена в {target.mention}.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(TempVoiceCog(bot))
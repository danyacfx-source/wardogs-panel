import logging
import re
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands


HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")

COLOR_NAMES = {
    "red": 0xE74C3C,
    "orange": 0xE67E22,
    "yellow": 0xF1C40F,
    "green": 0x2ECC71,
    "teal": 0x1ABC9C,
    "blue": 0x3498DB,
    "darkblue": 0x206694,
    "purple": 0x9B59B6,
    "pink": 0xE91E63,
    "white": 0xFFFFFF,
    "gray": 0x95A5A6,
    "dark": 0x2C2F33,
}


class EmbedDraft:
    def __init__(self):
        self.title = None
        self.description = None
        self.color = None
        self.author_name = None
        self.author_icon = None
        self.author_url = None
        self.footer_text = None
        self.footer_icon = None
        self.thumbnail = None
        self.image = None
        self.timestamp = False
        self.fields = []
        self.target_channel_id = None

    def to_embed(self) -> discord.Embed:
        e = discord.Embed()
        if self.title:
            e.title = self.title
        if self.description:
            e.description = self.description
        if self.color is not None:
            e.color = self.color
        if self.author_name:
            e.set_author(
                name=self.author_name,
                url=self.author_url or None,
                icon_url=self.author_icon or None,
            )
        if self.footer_text:
            e.set_footer(
                text=self.footer_text,
                icon_url=self.footer_icon or None,
            )
        if self.thumbnail:
            e.set_thumbnail(url=self.thumbnail)
        if self.image:
            e.set_image(url=self.image)
        if self.timestamp:
            e.timestamp = datetime.now(timezone.utc)
        for f in self.fields:
            if f["name"] and f["value"] is not None:
                e.add_field(name=f["name"], value=f["value"], inline=f["inline"])
        return e

    def is_empty(self) -> bool:
        return not any(
            [
                self.title,
                self.description,
                self.color is not None,
                self.author_name,
                self.footer_text,
                self.thumbnail,
                self.image,
                self.timestamp,
                self.fields,
            ]
        )


class TitleModal(discord.ui.Modal, title="Заголовок эмбеда"):
    title_input = discord.ui.TextInput(label="Заголовок", required=False, max_length=256)

    async def on_submit(self, interaction: discord.Interaction):
        draft = interaction.client.get_cog("EmbedBuilder").get_draft(interaction.user.id)
        draft.title = self.title_input.value.strip() or None
        await _refresh(interaction, draft)


class DescriptionModal(discord.ui.Modal, title="Описание эмбеда"):
    description = discord.ui.TextInput(
        label="Описание", style=discord.TextStyle.paragraph, required=False, max_length=4000
    )

    async def on_submit(self, interaction: discord.Interaction):
        draft = interaction.client.get_cog("EmbedBuilder").get_draft(interaction.user.id)
        draft.description = self.description.value.strip() or None
        await _refresh(interaction, draft)


class ColorModal(discord.ui.Modal, title="Цвет эмбеда"):
    color = discord.ui.TextInput(
        label="Цвет (hex или имя)", required=False, max_length=16, placeholder="#ff0000, red, 3498DB"
    )

    async def on_submit(self, interaction: discord.Interaction):
        draft = interaction.client.get_cog("EmbedBuilder").get_draft(interaction.user.id)
        raw = self.color.value.strip().lower()
        if not raw:
            draft.color = None
        elif raw in COLOR_NAMES:
            draft.color = COLOR_NAMES[raw]
        elif HEX_RE.match(raw):
            draft.color = int(HEX_RE.match(raw).group(1), 16)
        else:
            return await interaction.response.send_message(
                "Неверный цвет. Примеры: `#ff0000`, `red`, `3498DB`.",
                ephemeral=True,
            )
        await _refresh(interaction, draft)


class AuthorModal(discord.ui.Modal, title="Автор эмбеда"):
    name = discord.ui.TextInput(label="Имя автора", required=False, max_length=256)
    icon = discord.ui.TextInput(label="Ссылка на иконку (необязательно)", required=False, max_length=1024)
    url = discord.ui.TextInput(label="Ссылка при клике (необязательно)", required=False, max_length=1024)

    async def on_submit(self, interaction: discord.Interaction):
        draft = interaction.client.get_cog("EmbedBuilder").get_draft(interaction.user.id)
        name = self.name.value.strip() or None
        icon = self.icon.value.strip() or None
        url = self.url.value.strip() or None
        for v in (icon, url):
            if v is not None and not v.startswith(("http://", "https://")):
                return await interaction.response.send_message(
                    "Ссылка должна начинаться с http:// или https://", ephemeral=True
                )
        draft.author_name = name
        draft.author_icon = icon
        draft.author_url = url
        await _refresh(interaction, draft)


class FooterModal(discord.ui.Modal, title="Футер эмбеда"):
    text = discord.ui.TextInput(label="Текст футера", required=False, max_length=2048)
    icon = discord.ui.TextInput(label="Ссылка на иконку (необязательно)", required=False, max_length=1024)

    async def on_submit(self, interaction: discord.Interaction):
        draft = interaction.client.get_cog("EmbedBuilder").get_draft(interaction.user.id)
        text = self.text.value.strip() or None
        icon = self.icon.value.strip() or None
        if icon is not None and not icon.startswith(("http://", "https://")):
            return await interaction.response.send_message(
                "Ссылка должна начинаться с http:// или https://", ephemeral=True
            )
        draft.footer_text = text
        draft.footer_icon = icon
        await _refresh(interaction, draft)


class MediaModal(discord.ui.Modal, title="Медиа эмбеда"):
    image = discord.ui.TextInput(label="Ссылка на изображение", required=False, max_length=1024)
    thumbnail = discord.ui.TextInput(label="Ссылка на миниатюру", required=False, max_length=1024)

    async def on_submit(self, interaction: discord.Interaction):
        draft = interaction.client.get_cog("EmbedBuilder").get_draft(interaction.user.id)
        image = self.image.value.strip() or None
        thumbnail = self.thumbnail.value.strip() or None
        for v in (image, thumbnail):
            if v is not None and not v.startswith(("http://", "https://")):
                return await interaction.response.send_message(
                    "Ссылка должна начинаться с http:// или https://", ephemeral=True
                )
        draft.image = image
        draft.thumbnail = thumbnail
        await _refresh(interaction, draft)


class FieldModal(discord.ui.Modal, title="Новое поле"):
    name = discord.ui.TextInput(label="Название поля", required=False, max_length=256)
    value = discord.ui.TextInput(label="Значение поля", required=False, max_length=1024)
    inline = discord.ui.TextInput(label="В одну строку? (да/нет)", required=False, max_length=3, default="да")

    async def on_submit(self, interaction: discord.Interaction):
        draft = interaction.client.get_cog("EmbedBuilder").get_draft(interaction.user.id)
        if len(draft.fields) >= 25:
            return await interaction.response.send_message(
                "Максимум 25 полей.", ephemeral=True
            )
        name = self.name.value.strip()
        value = self.value.value.strip()
        if not name:
            return await interaction.response.send_message(
                "Название поля не может быть пустым.", ephemeral=True
            )
        draft.fields.append(
            {
                "name": name,
                "value": value,
                "inline": self.inline.value.strip().lower().startswith("д"),
            }
        )
        await _refresh(interaction, draft)


async def _refresh(interaction: discord.Interaction, draft: EmbedDraft):
    cog = interaction.client.get_cog("EmbedBuilder")
    embed, view = cog.render(draft, interaction.guild, owner_id=interaction.user.id)
    await interaction.response.edit_message(embed=embed, view=view)


class EmbedBuilderView(discord.ui.View):
    def __init__(self, draft: EmbedDraft | None = None, guild: discord.Guild | None = None, owner_id: int = 0):
        super().__init__(timeout=None)
        self.owner_id = owner_id

        if draft and draft.fields:
            options = [
                discord.SelectOption(
                    label=(f["name"][:60] if f["name"] else f"Поле {i + 1}"),
                    value=str(i),
                )
                for i, f in enumerate(draft.fields)
            ]
        else:
            options = [
                discord.SelectOption(label="Нет полей", value="0", default=True)
            ]
        delete = discord.ui.Select(
            custom_id="embed_delete_field",
            placeholder="🗑 Удалить поле...",
            min_values=1,
            max_values=1,
            options=options,
            disabled=not (draft and draft.fields),
            row=3,
        )
        delete.callback = self._on_delete_field
        self.add_item(delete)

        target = discord.ui.ChannelSelect(
            custom_id="embed_target_channel",
            placeholder="📨 Куда отправить...",
            channel_types=[discord.ChannelType.text],
            row=4,
        )
        if draft and draft.target_channel_id:
            try:
                target.default_values = [
                    discord.SelectDefaultValue(
                        id=draft.target_channel_id,
                        type=discord.SelectDefaultValueType.channel,
                    )
                ]
            except Exception:
                pass
        target.callback = self._on_target_channel
        self.add_item(target)

    def _draft(self, interaction: discord.Interaction) -> EmbedDraft:
        return interaction.client.get_cog("EmbedBuilder").get_draft(interaction.user.id)

    async def _check_owner(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Это не ваш черновик.", ephemeral=True
            )
            return False
        return True

    async def _on_target_channel(self, interaction: discord.Interaction):
        if not await self._check_owner(interaction):
            return
        values = (interaction.data or {}).get("values") or []
        draft = self._draft(interaction)
        if values:
            draft.target_channel_id = int(values[0])
        await _refresh(interaction, draft)

    async def _on_delete_field(self, interaction: discord.Interaction):
        if not await self._check_owner(interaction):
            return
        values = (interaction.data or {}).get("values") or []
        draft = self._draft(interaction)
        if values:
            idx = int(values[0])
            if 0 <= idx < len(draft.fields):
                draft.fields.pop(idx)
        await _refresh(interaction, draft)

    @discord.ui.button(label="✏️ Заголовок", style=discord.ButtonStyle.secondary, custom_id="embed_title", row=0)
    async def btn_title(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_owner(interaction):
            return
        await interaction.response.send_modal(TitleModal())

    @discord.ui.button(label="📝 Описание", style=discord.ButtonStyle.secondary, custom_id="embed_desc", row=0)
    async def btn_desc(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_owner(interaction):
            return
        await interaction.response.send_modal(DescriptionModal())

    @discord.ui.button(label="🎨 Цвет", style=discord.ButtonStyle.secondary, custom_id="embed_color", row=0)
    async def btn_color(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_owner(interaction):
            return
        await interaction.response.send_modal(ColorModal())

    @discord.ui.button(label="🕒 Время", style=discord.ButtonStyle.secondary, custom_id="embed_time", row=0)
    async def btn_time(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_owner(interaction):
            return
        draft = self._draft(interaction)
        draft.timestamp = not draft.timestamp
        await _refresh(interaction, draft)

    @discord.ui.button(label="🧩 Поле", style=discord.ButtonStyle.primary, custom_id="embed_add_field", row=1)
    async def btn_add_field(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_owner(interaction):
            return
        await interaction.response.send_modal(FieldModal())

    @discord.ui.button(label="🖼 Медиа", style=discord.ButtonStyle.secondary, custom_id="embed_media", row=1)
    async def btn_media(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_owner(interaction):
            return
        await interaction.response.send_modal(MediaModal())

    @discord.ui.button(label="👤 Автор", style=discord.ButtonStyle.secondary, custom_id="embed_author", row=1)
    async def btn_author(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_owner(interaction):
            return
        await interaction.response.send_modal(AuthorModal())

    @discord.ui.button(label="📌 Футер", style=discord.ButtonStyle.secondary, custom_id="embed_footer", row=1)
    async def btn_footer(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_owner(interaction):
            return
        await interaction.response.send_modal(FooterModal())

    @discord.ui.button(label="🗑 Сброс", style=discord.ButtonStyle.danger, custom_id="embed_reset", row=2)
    async def btn_reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_owner(interaction):
            return
        cog = interaction.client.get_cog("EmbedBuilder")
        cog.drafts[interaction.user.id] = EmbedDraft()
        await _refresh(interaction, cog.drafts[interaction.user.id])

    @discord.ui.button(label="🚀 Отправить", style=discord.ButtonStyle.success, custom_id="embed_send", row=2)
    async def btn_send(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_owner(interaction):
            return
        draft = self._draft(interaction)
        if draft.is_empty():
            await interaction.response.send_message(
                "Эмбед пуст — добавьте хотя бы заголовок или описание.",
                ephemeral=True,
            )
            return
        embed = draft.to_embed()
        target_id = draft.target_channel_id or interaction.channel_id
        target = interaction.guild.get_channel(target_id)
        if target is None:
            await interaction.response.send_message(
                "Канал не найден.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await target.send(embed=embed)
        except Exception as e:
            logging.error("Не удалось отправить эмбед: %s", e, exc_info=True)
            await interaction.followup.send(
                f"❌ Не удалось отправить эмбед: {e}", ephemeral=True
            )
            return
        await interaction.followup.send(
            f"✅ Эмбед отправлен в {target.mention}.", ephemeral=True
        )


class EmbedBuilder(commands.Cog):
    MAX_DRAFTS = 200

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.drafts: dict[int, EmbedDraft] = {}

    def get_draft(self, user_id: int) -> EmbedDraft:
        if len(self.drafts) > self.MAX_DRAFTS and user_id not in self.drafts:
            try:
                del self.drafts[next(iter(self.drafts))]
            except StopIteration:
                pass
        return self.drafts.setdefault(user_id, EmbedDraft())

    def render(self, draft: EmbedDraft, guild: discord.Guild, owner_id: int = 0):
        if draft.is_empty():
            embed = discord.Embed(
                title="🎨 Конструктор эмбеда",
                description=(
                    "Эмбед пока пуст. Настраивайте его кнопками ниже — "
                    "превью обновится автоматически."
                ),
                color=discord.Color.blurple(),
            )
        else:
            embed = draft.to_embed()
        view = EmbedBuilderView(draft=draft, guild=guild, owner_id=owner_id)
        return embed, view

    @app_commands.command(name="embed", description="Конструктор эмбеда (как message.style)")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def embed(self, interaction: discord.Interaction):
        draft = self.get_draft(interaction.user.id)
        embed, view = self.render(draft, interaction.guild, owner_id=interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(EmbedBuilder(bot))

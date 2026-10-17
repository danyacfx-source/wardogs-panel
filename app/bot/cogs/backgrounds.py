import os
import sys
import asyncio
from io import BytesIO

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont

from database import add_background, remove_background, get_backgrounds, clear_backgrounds, set_bg_pos, get_conn

import logging

log = logging.getLogger("backgrounds")


def _bg_rows() -> list[dict]:
    """Список фонов с реальными id (в БД), для команд remove/setpos/preview."""
    conn = get_conn()
    return [
        dict(r)
        for r in conn.execute("SELECT id, path FROM backgrounds ORDER BY id").fetchall()
    ]


class BackgroundsCog(commands.Cog):
    """Управление фонами rank-карточки прямо из Discord (удобно на хостинге)."""

    ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        os.makedirs(self.ASSETS_DIR, exist_ok=True)

    def _admin(self, interaction: discord.Interaction) -> bool:
        return interaction.user.guild_permissions.administrator or interaction.user.guild_permissions.manage_guild

    bg = app_commands.Group(name="bg", description="Фоны rank-карточки")

    @bg.command(name="add", description="Добавить картинку-фон (вложением к команде)")
    @app_commands.describe(file="Картинка (PNG/JPG)")
    @app_commands.guild_only()
    async def bg_add(self, interaction: discord.Interaction, file: discord.Attachment):
        if not self._admin(interaction):
            return await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
        if not (file.content_type or "").startswith("image/"):
            return await interaction.response.send_message("❌ Это не изображение.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        ext = os.path.splitext(file.filename or "")[1].lower()
        if ext not in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
            ext = ".png"
        dest = os.path.join(
            self.ASSETS_DIR,
            f"bg-{interaction.user.id}-{int(discord.utils.utcnow().timestamp())}{ext}",
        )
        await file.save(dest)
        add_background(dest, interaction.user.id)
        await interaction.followup.send(f"✅ Фон добавлен: `{os.path.basename(dest)}`.", ephemeral=True)

    @bg.command(name="list", description="Показать все фоны")
    @app_commands.guild_only()
    async def bg_list(self, interaction: discord.Interaction):
        if not self._admin(interaction):
            return await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
        bgs = get_backgrounds()
        if not bgs:
            return await interaction.response.send_message("📭 Фонов нет. Добавьте через `/bg add`.", ephemeral=True)
        lines = [f"**{i}.** `{os.path.basename(p)}`" for i, p in enumerate(bgs, 1)]
        await interaction.response.send_message(f"**Фоны ({len(bgs)}):**\n" + "\n".join(lines), ephemeral=True)

    @bg.command(name="remove", description="Удалить фон по номеру из /bg list")
    @app_commands.describe(n="Номер фона")
    @app_commands.guild_only()
    async def bg_remove(self, interaction: discord.Interaction, n: int):
        if not self._admin(interaction):
            return await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
        bgs = _bg_rows()
        if not (1 <= n <= len(bgs)):
            return await interaction.response.send_message(f"❌ Фон #{n} не найден.", ephemeral=True)
        remove_background(bgs[n - 1]["id"])
        await interaction.response.send_message(f"✅ Фон #{n} удалён.", ephemeral=True)

    @bg.command(name="clear", description="Удалить все фоны")
    @app_commands.guild_only()
    async def bg_clear(self, interaction: discord.Interaction):
        if not self._admin(interaction):
            return await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
        n = clear_backgrounds()
        await interaction.response.send_message(f"🗑 Удалено фонов: {n}.", ephemeral=True)


    @bg.command(name="setpos", description="Задать позицию круга аватара на фоне (номер из /bg list)")
    @app_commands.describe(n="Номер фона", cx="Центр X круга", cy="Центр Y круга", r="Радиус круга")
    @app_commands.guild_only()
    async def bg_setpos(self, interaction: discord.Interaction, n: int, cx: int, cy: int, r: int):
        if not self._admin(interaction):
            return await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
        bgs = _bg_rows()
        if not (1 <= n <= len(bgs)):
            return await interaction.response.send_message(f"❌ Фон #{n} не найден.", ephemeral=True)
        set_bg_pos(bgs[n - 1]["id"], cx, cy, r)
        await interaction.response.send_message(
            f"✅ Фон #{n}: аватар-круг center=({cx},{cy}) r={r}", ephemeral=True
        )

    @bg.command(name="preview", description="Показать фон с сеткой — для подсчёта координат")
    @app_commands.describe(n="Номер фона")
    @app_commands.guild_only()
    async def bg_preview(self, interaction: discord.Interaction, n: int):
        if not self._admin(interaction):
            return await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
        bgs = _bg_rows()
        if not (1 <= n <= len(bgs)):
            return await interaction.response.send_message(f"❌ Фон #{n} не найден.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        bg_path = bgs[n - 1]["path"]

        def _make_grid():
            img = Image.open(bg_path).convert("RGB")
            # crop same as _load_background
            target_ratio = 900 / 280
            cur_ratio = img.width / img.height
            if cur_ratio > target_ratio:
                nw = int(img.height * target_ratio)
                l = (img.width - nw) // 2
                img = img.crop((l, 0, l + nw, img.height))
            elif cur_ratio < target_ratio:
                nh = int(img.width / target_ratio)
                t = (img.height - nh) // 2
                img = img.crop((0, t, img.width, t + nh))
            img = img.resize((900, 280), Image.LANCZOS)

            draw = ImageDraw.Draw(img)
            font = ImageFont.truetype("C:\\Windows\\Fonts\\arial.ttf", 12)
            # Vertical grid every 50px
            for x in range(0, 900, 50):
                draw.line([(x, 0), (x, 280)], fill=(255, 255, 0, 128), width=1)
                draw.text((x + 2, 2), str(x), fill=(255, 255, 0), font=font)
            # Horizontal grid every 50px
            for y in range(0, 280, 50):
                draw.line([(0, y), (900, y)], fill=(255, 255, 0, 128), width=1)
                draw.text((2, y + 2), str(y), fill=(255, 255, 0), font=font)

            buf = BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            return buf

        try:
            buf = await asyncio.to_thread(_make_grid)
        except Exception:
            log.exception("bg preview: не удалось открыть %s", bg_path)
            return await interaction.followup.send(
                f"❌ Не удалось открыть фон: `{os.path.basename(bg_path)}`.", ephemeral=True
            )
        file = discord.File(buf, filename=f"bg-{n}-grid.png")
        await interaction.followup.send(
            f"**Фон #{n}** с сеткой (шаг 50px). Считай координаты центра круга, потом: `/bg setpos n:{n} cx:... cy:... r:...`",
            file=file,
            ephemeral=True,
        )

    @bg.command(name="info", description="Показать все фоны с позициями")
    @app_commands.guild_only()
    async def bg_info(self, interaction: discord.Interaction):
        if not self._admin(interaction):
            return await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
        conn = get_conn()
        rows = conn.execute("SELECT id, path, cx, cy, radius FROM backgrounds ORDER BY id").fetchall()
        if not rows:
            return await interaction.response.send_message("📭 Фонов нет.", ephemeral=True)
        lines = []
        for r in rows:
            name = os.path.basename(r["path"])
            lines.append(f"**{r['id']}.** `{name}` — center=({r['cx']},{r['cy']}) r={r['radius']}")
        await interaction.response.send_message(
            "**Фоны:**\n" + "\n".join(lines), ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(BackgroundsCog(bot))
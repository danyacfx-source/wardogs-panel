"""Карточка «Сервер запущен»: итог сбора. Фон — app/bot/assets/start_card_bg.webp, текст рисует Pillow."""
from __future__ import annotations

import io
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ASSET_DIR = Path(__file__).resolve().parent / "assets"
FONT_DIR = Path(__file__).resolve().parent / "fonts"
BACKGROUND = ASSET_DIR / "start_card_bg.webp"

# Палитра фона: почти чёрный, кровавый красный, хромированное серебро.
TEXT = (232, 232, 238)
MUTED = (150, 142, 142)
RED = (222, 36, 36)
SILVER = (206, 210, 222)
RED_HOT = (246, 64, 52)

MAX_NAMES = 8  # в сетке 3×3 последняя ячейка — «и ещё N»
GRID_COLUMNS = (72, 432, 792)
GRID_ROW_Y = (330, 392, 454)

# Внутренние области панелей фона (в пикселях фонового изображения).
BIG_PANEL = (42, 250, 1155, 700)
TOP_RIGHT_PANEL = (1202, 250, 1630, 434)
MID_RIGHT_PANEL = (1202, 486, 1630, 700)
BOTTOM_LEFT_PANEL = (42, 752, 812, 875)
BOTTOM_RIGHT_PANEL = (860, 752, 1630, 875)


@dataclass(frozen=True)
class StartCardData:
    server_label: str
    duration_seconds: int
    population: int
    capacity: int
    sitter_names: tuple[str, ...]
    sitter_total: int
    points: int
    first_name: str
    longest_name: str
    longest_seconds: int


@lru_cache(maxsize=None)
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    return ImageFont.truetype(str(FONT_DIR / name), size)


@lru_cache(maxsize=1)
def _background() -> Image.Image:
    return Image.open(BACKGROUND).convert("RGB")


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, minutes = divmod(seconds // 60, 60)
    return f"{hours} ч {minutes} мин" if hours else f"{minutes} мин"


def format_points(value: int) -> str:
    return f"{int(value):,}".replace(",", " ")


def _fit(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text + "…"


def sitter_cells(names: tuple[str, ...]) -> list[str]:
    """Ячейки сетки: до MAX_NAMES ников и «и ещё N», если сидящих больше."""
    if len(names) <= MAX_NAMES + 1:
        return list(names)
    return list(names[:MAX_NAMES]) + [f"и ещё {len(names) - MAX_NAMES}"]


def render_start_card(card: StartCardData) -> bytes:
    img = _background().copy()
    draw = ImageDraw.Draw(img)

    # Шапка: над панелями, места хватает.
    draw.text((60, 48), "WARDOGS RU // СИДИНГ", font=_font(26, True), fill=RED)
    draw.text((60, 80), "СЕРВЕР ЗАПУЩЕН", font=_font(80, True), fill=TEXT)
    subtitle = f"{card.server_label} · собрали за {format_duration(card.duration_seconds)}"
    draw.text((60, 178), _fit(draw, subtitle, _font(34), 1500), font=_font(34), fill=SILVER)

    # Большая панель: сидящие.
    draw.text((72, 272), f"СПАСИБО СИДЕРАМ · {card.sitter_total}", font=_font(24, True), fill=MUTED)
    start_text = f"НА СТАРТЕ · {card.population}"
    start_w = draw.textlength(start_text, font=_font(24, True))
    draw.text((1125 - start_w, 272), start_text, font=_font(24, True), fill=RED)
    for index, cell in enumerate(sitter_cells(card.sitter_names)[:9]):
        column = GRID_COLUMNS[index % 3]
        row_y = GRID_ROW_Y[index // 3]
        text = cell if cell.startswith("и ещё") else f"◆ {cell}"
        draw.text((column, row_y), _fit(draw, text, _font(34), 340), font=_font(34), fill=TEXT)

    # Правая верхняя: длительность сбора.
    draw.text((1232, 274), "СБОР ДЛИЛСЯ", font=_font(22, True), fill=MUTED)
    draw.text((1232, 316), format_duration(card.duration_seconds), font=_font(52, True), fill=SILVER)

    # Правая средняя: начислено.
    draw.text((1232, 510), "НАЧИСЛЕНО", font=_font(22, True), fill=MUTED)
    draw.text((1232, 546), format_points(card.points), font=_font(96, True), fill=RED_HOT)
    draw.text((1232, 660), "баллов всем сидящим", font=_font(26), fill=MUTED)

    # Нижние панели: первый и самый долгий.
    draw.text((72, 774), "ПЕРВЫЙ ЗАШЁЛ НА СИДИНГ", font=_font(22, True), fill=MUTED)
    draw.text((72, 806), _fit(draw, card.first_name or "—", _font(46, True), 700), font=_font(46, True), fill=TEXT)
    draw.text((890, 774), "ДОЛЬШЕ ВСЕХ", font=_font(22, True), fill=MUTED)
    if card.longest_seconds:
        longest = format_duration(card.longest_seconds)
        longest_w = draw.textlength(longest, font=_font(28, True))
        draw.text((1600 - longest_w, 772), longest, font=_font(28, True), fill=RED)
    draw.text((890, 806), _fit(draw, card.longest_name or "—", _font(46, True), 690), font=_font(46, True), fill=TEXT)

    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()

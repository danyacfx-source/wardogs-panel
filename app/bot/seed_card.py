"""Карточка сидинга для одного сервера: PNG, который рисует Pillow.

Фон, рамки и уголки — готовая картинка app/bot/assets/seed_card_bg.webp
(без текста). Текст, полоска и списки кладутся внутрь рамок по координатам
ниже. Рендер не ходит в сеть и не трогает Discord, поэтому тестируется локально.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ASSET_DIR = Path(__file__).resolve().parent / "assets"
FONT_DIR = Path(__file__).resolve().parent / "fonts"
BACKGROUND = ASSET_DIR / "seed_card_bg.webp"

WIDTH = 1756
HEIGHT = 896

TEXT = (236, 236, 236)
MUTED = (165, 160, 160)
ACCENT = (232, 40, 40)
CELL_ON = (232, 40, 40)
CELL_OFF = (52, 46, 48)

MAX_NAMES = 3
STATES = ("active", "limit", "closed", "offline")

# Внутренние области рамок в координатах фонового изображения.
LEFT_INNER = (84, 128, 972, 552)
RIGHT_INNER = (1106, 128, 1676, 552)
FOOTER_INNER = (120, 716, 1636, 812)


@dataclass(frozen=True)
class CardData:
    server_label: str
    state: str  # active | limit | closed | offline
    population: int = 0
    capacity: int = 20
    seeder_names: tuple[str, ...] = ()
    window_text: str = ""
    rates: tuple[str, ...] = ()


@lru_cache(maxsize=None)
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    return ImageFont.truetype(str(FONT_DIR / name), size)


@lru_cache(maxsize=1)
def _background() -> Image.Image:
    img = Image.open(BACKGROUND).convert("RGB")
    if img.size != (WIDTH, HEIGHT):
        img = img.resize((WIDTH, HEIGHT))
    return img


def format_seeders(names: tuple[str, ...] | list[str], limit: int = MAX_NAMES) -> list[str]:
    """Строки для колонки «Уже сидят»: до `limit` ников и строка «и ещё N»."""
    shown = list(names[:limit])
    rest = len(names) - len(shown)
    if rest > 0:
        shown.append(f"и ещё {rest}")
    return shown


def _fit(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text + "…"


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
    if current:
        lines.append(current)
    return [_fit(draw, line, font, max_width) for line in lines]


def _draw_bar(draw: ImageDraw.ImageDraw, x0: int, y: int, width: int, filled: int, total: int) -> None:
    cells = 20
    gap = 8
    cell_w = (width - gap * (cells - 1)) // cells
    lit = 0 if total <= 0 else min(cells, round(cells * filled / total))
    for i in range(cells):
        cx = x0 + i * (cell_w + gap)
        draw.rectangle([cx, y, cx + cell_w, y + 30], fill=CELL_ON if i < lit else CELL_OFF)


def _left_panel(draw: ImageDraw.ImageDraw, card: CardData) -> None:
    x0, y0, x1, _ = LEFT_INNER
    pad = 24
    left_x = x0 + pad
    inner_w = x1 - x0 - pad * 2

    draw.text((left_x, y0 + 4), "WARDOGS RU // СИДИНГ", font=_font(26, True), fill=ACCENT)
    draw.text((left_x, y0 + 38), "ЗОВЁМ СИДЕРОВ", font=_font(70, True), fill=TEXT)
    draw.text((left_x, y0 + 122), _fit(draw, card.server_label, _font(34), inner_w), font=_font(34), fill=MUTED)

    body_y = y0 + 168  # «НА СЕРВЕРЕ»
    bar_y = y0 + 338
    if card.state in ("active", "limit"):
        draw.text((left_x, body_y), "НА СЕРВЕРЕ", font=_font(24, True), fill=MUTED)
        draw.text((left_x, body_y + 22), f"{card.population}/{card.capacity}", font=_font(110, True), fill=ACCENT)
        if card.state == "active":
            left = max(0, card.capacity - card.population)
            draw.text((left_x, body_y + 124), f"ещё {left} игроков до старта", font=_font(32), fill=TEXT)
            _draw_bar(draw, left_x, bar_y, inner_w, card.population, card.capacity)
            if card.seeder_names:
                line = "Уже сидят: " + ", ".join(format_seeders(card.seeder_names))
            else:
                line = "Пока никто не сидит — заходи первым"
            draw.text((left_x, bar_y + 40), _fit(draw, line, _font(26), inner_w), font=_font(26), fill=TEXT)
        else:
            draw.text((left_x, body_y + 124), "Сервер набран — матч идёт", font=_font(32), fill=TEXT)
            _draw_bar(draw, left_x, bar_y, inner_w, card.capacity, card.capacity)
    elif card.state == "closed":
        draw.text((left_x, body_y + 22), "СИДИНГ ЗАКРЫТ", font=_font(80, True), fill=MUTED)
        start = card.window_text.split("–")[0] if card.window_text else ""
        text = f"Откроется в {start} МСК" if start else "Сидинг вне окна"
        draw.text((left_x, body_y + 128), text, font=_font(32), fill=TEXT)
        _draw_bar(draw, left_x, bar_y, inner_w, 0, card.capacity)
    else:  # offline
        draw.text((left_x, body_y + 22), "НЕТ СВЯЗИ", font=_font(80, True), fill=MUTED)
        draw.text((left_x, body_y + 128), "Сервер недоступен, сидинг не ведётся", font=_font(32), fill=TEXT)
        _draw_bar(draw, left_x, bar_y, inner_w, 0, card.capacity)


def _right_panel(draw: ImageDraw.ImageDraw, card: CardData) -> None:
    x0, y0, x1, y1 = RIGHT_INNER
    pad = 28
    left_x = x0 + pad
    inner_w = x1 - x0 - pad * 2
    draw.text((left_x, y0 + 4), "НАГРАДЫ", font=_font(26, True), fill=MUTED)
    y = y0 + 60
    rate_font = _font(28, True)
    for line in card.rates:
        for wrapped in _wrap(draw, line, rate_font, inner_w):
            draw.text((left_x, y), wrapped, font=rate_font, fill=TEXT)
            y += 40
        y += 10
    if card.window_text:
        window = _fit(draw, f"Окно: {card.window_text} МСК", _font(28), inner_w)
        draw.text((left_x, y1 - 50), window, font=_font(28), fill=MUTED)


def _footer(draw: ImageDraw.ImageDraw) -> None:
    x0, y0, _x1, _y1 = FOOTER_INNER
    draw.text((x0, y0 + 4), "Зайди и выбери сторону — участие засчитывается сразу", font=_font(40, True), fill=TEXT)
    draw.text((x0, y0 + 56), "Баллы за минуты сидинга остаются, даже если сервер не запустится",
              font=_font(26), fill=MUTED)


def render_server_card(card: CardData) -> bytes:
    if card.state not in STATES:
        raise ValueError(f"unknown card state: {card.state}")

    img = _background().copy()
    draw = ImageDraw.Draw(img)
    _left_panel(draw, card)
    _right_panel(draw, card)
    _footer(draw)

    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()

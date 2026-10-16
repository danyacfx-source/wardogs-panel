"""Рендер rank-карточки в виде картинки (Juniper-style)."""

import io
import os
import urllib.request

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# Цветовая схема
BG_TOP = (47, 49, 54)        # dark grey
BG_BOTTOM = (32, 34, 37)
ACCENT = (0xF3, 0x9C, 0x12)  # оранжевый Juniper
TEXT_WHITE = (240, 240, 240)
TEXT_GREY = (160, 165, 175)
BAR_BG = (60, 63, 70)
BAR_FILL = ACCENT

WIDTH = 900
HEIGHT = 280
AVATAR_OFFSET = (50, 60)
AVATAR_SIZE = 150
TEXT_X = 270

_FONT_CACHE: dict[int, ImageFont.FreeTypeFont] = {}

# Кандидаты на шрифты для Windows / Linux / macOS + шрифты репозитория (fonts/).
_FONT_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("fonts/DejaVuSans-Bold.ttf", "fonts/DejaVuSans.ttf"),
    ("C:\\Windows\\Fonts\\arialbd.ttf", "C:\\Windows\\Fonts\\arial.ttf"),
    ("C:\\Windows\\Fonts\\segoeuib.ttf", "C:\\Windows\\Fonts\\segoeui.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ("/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf"),
    ("/System/Library/Fonts/SFNS.ttf", "/System/Library/Fonts/SFNS.ttf"),
    ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf"),
)


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    resolved = None
    for bold_path, normal_path in _FONT_CANDIDATES:
        path = bold_path if bold else normal_path
        if os.path.isfile(path):
            resolved = path
            break
    try:
        if resolved:
            _FONT_CACHE[key] = ImageFont.truetype(resolved, size)
        else:
            _FONT_CACHE[key] = ImageFont.load_default()
    except Exception:
        _FONT_CACHE[key] = ImageFont.load_default()
    return _FONT_CACHE[key]


def _load_background(raw: Image.Image) -> Image.Image:
    """Обрезает фоновое изображение под размер карточки (cover-поведение)."""
    scaled = raw
    target_ratio = WIDTH / HEIGHT
    cur_ratio = raw.width / raw.height
    if cur_ratio > target_ratio:
        # слишком широкое — режем по ширине
        new_w = int(raw.height * target_ratio)
        left = (raw.width - new_w) // 2
        scaled = raw.crop((left, 0, left + new_w, raw.height))
    elif cur_ratio < target_ratio:
        # слишком высокое — режем по высоте
        new_h = int(raw.width / target_ratio)
        top = (raw.height - new_h) // 2
        scaled = raw.crop((0, top, raw.width, top + new_h))
    return scaled.resize((WIDTH, HEIGHT), Image.LANCZOS)


def _resolve_backgrounds() -> list[str]:
    """Фоны: из .env (файл/список/папка) + из БД (загруженные через /bg add)."""
    import config
    from database import get_backgrounds

    paths: list[str] = list(get_backgrounds())

    raw = (config.RANK_BACKGROUND or "").strip()
    if raw:
        for part in raw.replace(";", ",").split(","):
            p = part.strip()
            if not p:
                continue
            if os.path.isdir(p):
                for f in sorted(os.listdir(p)):
                    if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp")):
                        paths.append(os.path.join(p, f))
            else:
                paths.append(p)

    return [p for p in paths if os.path.isfile(p)]


def _background() -> tuple[Image.Image, tuple[int, int, int]]:
    """Фон карточки + позиция аватара (cx, cy, radius).

    Приоритет: БД (с позицией) > .env пути (дефолт позиция) > градиент.
    """
    from database import get_random_bg

    # 1) БД (с позицией)
    bg_info = get_random_bg()
    if bg_info:
        try:
            raw = Image.open(bg_info["path"]).convert("RGB")
            bg = _load_background(raw)
            dark = Image.new("RGB", bg.size, (0, 0, 0))
            bg = Image.blend(bg, dark, 0.3)
            cx = bg_info.get("cx") or 125
            cy = bg_info.get("cy") or 135
            r = bg_info.get("radius") or 75
            return bg, (cx, cy, r)
        except Exception:
            pass

    # 2) .env пути (дефолт позиция)
    paths = _resolve_backgrounds()
    if paths:
        import random
        path = random.choice(paths)
        try:
            raw = Image.open(path).convert("RGB")
            bg = _load_background(raw)
            dark = Image.new("RGB", bg.size, (0, 0, 0))
            bg = Image.blend(bg, dark, 0.3)
            return bg, (125, 135, 75)
        except Exception:
            pass

    # 3) Градиент
    img = Image.new("RGB", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(img)
    for y in range(HEIGHT):
        t = y / HEIGHT
        color = tuple(int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3))
        draw.line([(0, y), (WIDTH, y)], fill=color)
    return img, (125, 135, 75)


# Цветовая схема


def _load_avatar(url: str) -> Image.Image:
    """Скачиваем аватар, на выходе круглая картинка (онлайн-фолбэк на цвет)."""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read()
        img = Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception:
        img = Image.new("RGBA", (256, 256), ACCENT)
        d = ImageDraw.Draw(img)
        d.ellipse([60, 60, 196, 196], fill=(255, 255, 255))

    size = AVATAR_SIZE
    img = img.resize((size, size), Image.LANCZOS)

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def _text_width(text: str, font: ImageFont.FreeTypeFont) -> int:
    d = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    return d.textlength(text, font=font)


def _draw_progress_bar(draw: ImageDraw.ImageDraw, x: int, y: int, xp: int, need: int):
    bar_w = 350
    bar_h = 18
    ratio = min(1.0, xp / need) if need else 1.0
    rad = bar_h // 2

    # фон
    draw.rounded_rectangle(
        [x, y, x + bar_w, y + bar_h], radius=rad, fill=BAR_BG
    )
    fill_w = int(bar_w * ratio)
    if fill_w > rad:
        draw.rounded_rectangle(
            [x, y, x + fill_w, y + bar_h], radius=rad, fill=BAR_FILL
        )

    # подпись справа
    f = _font(20, bold=True)
    label = f" {xp}/{need} XP"
    draw.text((x + bar_w + 12, y + 0), label, font=f, fill=TEXT_WHITE)


def render_rank_card(
    member_name: str,
    avatar_url: str,
    level: int,
    rank_pos: int,
    total_users: int,
    cur_xp: int,
    need_xp: int,
    messages: int,
    voice_minutes: int,
) -> io.BytesIO:
    img, (avatar_cx, avatar_cy, avatar_r) = _background()
    draw = ImageDraw.Draw(img)

    # аватар — позиционируется по центру и радиусу круга на фоне
    avatar = _load_avatar(avatar_url)
    avatar_size = avatar_r * 2
    avatar = avatar.resize((avatar_size, avatar_size), Image.LANCZOS)
    avatar_x = avatar_cx - avatar_r
    avatar_y = avatar_cy - avatar_r
    img.paste(avatar, (avatar_x, avatar_y), avatar)

    y = 62

    # имя
    name_font = _font(44, bold=True)
    name = (member_name or "???")[:28]
    draw.text((TEXT_X, y), name, font=name_font, fill=TEXT_WHITE)

    # уровень + позиция
    y += 66
    stat_font = _font(24, bold=True)
    lbl_font = _font(20)
    draw.text((TEXT_X, y), f"Уровень {level}", font=stat_font, fill=TEXT_WHITE)
    pos_text = f"#{rank_pos} из {total_users}"
    w = _text_width(pos_text, stat_font)
    draw.text((WIDTH - 50 - w, y), pos_text, font=stat_font, fill=ACCENT)

    # прогресс-бар
    y += 46
    _draw_progress_bar(draw, TEXT_X, y, cur_xp, need_xp)

    # статистика внизу
    y += 70
    small = _font(26, bold=True)
    small_lbl = _font(19)
    draw.text((TEXT_X, y), "💬", font=small, fill=TEXT_WHITE)
    draw.text((TEXT_X + 45, y + 4), f"{messages}", font=small, fill=TEXT_WHITE)
    draw.text((TEXT_X + 45 + _text_width(str(messages), small) + 12, y + 6),
              "сообщений", font=small_lbl, fill=TEXT_GREY)

    vx = TEXT_X + 240
    draw.text((vx, y), "🎙️", font=small, fill=TEXT_WHITE)
    draw.text((vx + 50, y + 4), f"{voice_minutes}", font=small, fill=TEXT_WHITE)
    draw.text((vx + 50 + _text_width(str(voice_minutes), small) + 12, y + 6),
              "мин в голосе", font=small_lbl, fill=TEXT_GREY)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf
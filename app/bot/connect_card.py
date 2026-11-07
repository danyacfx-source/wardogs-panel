"""Карточки «Как подключиться» в канале подключения: логотип сервера и ID для поиска в игре."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import discord

ASSET_DIR = Path(__file__).resolve().parent / "assets"
EMBED_COLOR = 0xB00000


@dataclass(frozen=True)
class ConnectServer:
    key: str    # ключ сервера: main / second / third (см. config.SEED_SERVER_ID_*)
    label: str  # подпись в Discord
    logo: str   # файл логотипа в app/bot/assets


CONNECT_SERVERS = (
    ConnectServer("main", "#1 WDRU", "server_logo_1.webp"),
    ConnectServer("second", "#2 WDRU", "server_logo_2.webp"),
    ConnectServer("third", "#3 WDRU", "server_logo_3.webp"),
)


def connect_description(label: str, public_id: str) -> str:
    return (
        f"Подключение к серверу **{label}**\n\n"
        "1. Запустите **WARDOGS**.\n"
        f"2. Откройте список игровых серверов и найдите **{label}**.\n"
        "3. Подключитесь к серверу и дождитесь загрузки.\n\n"
        f"🆔 ID сервера:\n`{public_id}`"
    )


def connect_digest(logo: bytes, description: str) -> str:
    """Отпечаток содержимого: при смене логотипа или текста сообщение обновляется, иначе пропускается."""
    return hashlib.sha256(logo + b"\0" + description.encode("utf-8")).hexdigest()


def build_connect_embed(server: ConnectServer, description: str, filename: str) -> discord.Embed:
    embed = discord.Embed(color=EMBED_COLOR, title=f"🎮 Как подключиться · {server.label}", description=description)
    embed.set_thumbnail(url=f"attachment://{filename}")
    embed.set_footer(text="WARDOGS RU")
    return embed

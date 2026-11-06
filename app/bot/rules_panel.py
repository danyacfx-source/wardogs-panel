"""Правила WARDOGS | RU: тексты разбиваются на эфемерные сообщения, которые игрок может скрыть.

Исходники лежат в app/bot/rules/*.md. Разбиение идёт по разделам (заголовки ##),
чтобы раздел не резался посередине. Каждая часть укладывается в лимит описания embed.
"""
from __future__ import annotations

from pathlib import Path

RULES_DIR = Path(__file__).resolve().parent / "rules"
BANNER = Path(__file__).resolve().parent / "assets" / "rules_banner.webp"

# Лимит описания embed в Discord 4096; берём с запасом под заголовок и номер части.
PART_LIMIT = 3800

SOURCES = {
    "discord": ("Правила Discord", RULES_DIR / "discord_rules.md"),
    "game": ("Правила игровых серверов", RULES_DIR / "game_rules.md"),
    "seeding": ("О сидинге", RULES_DIR / "seeding_about.md"),
}


def _split_sections(text: str) -> list[str]:
    sections: list[str] = []
    current: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.startswith("## ") and current:
            sections.append("".join(current))
            current = []
        current.append(line)
    if current:
        sections.append("".join(current))
    return sections


def _hard_split(block: str, limit: int) -> list[str]:
    parts: list[str] = []
    rest = block
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        parts.append(rest)
    return parts


def split_rules(text: str, limit: int = PART_LIMIT) -> list[str]:
    """Режет текст на части не длиннее limit. Склейка частей даёт исходный текст."""
    blocks: list[str] = []
    for section in _split_sections(text):
        if len(section) <= limit:
            blocks.append(section)
        else:
            blocks.extend(_hard_split(section, limit))
    parts: list[str] = []
    current = ""
    for block in blocks:
        if current and len(current) + len(block) > limit:
            parts.append(current)
            current = ""
        current += block
    if current:
        parts.append(current)
    return parts


def load_parts(kind: str) -> list[str]:
    _title, path = SOURCES[kind]
    return split_rules(path.read_text(encoding="utf-8"))

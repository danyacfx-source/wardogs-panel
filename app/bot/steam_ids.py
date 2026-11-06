"""SteamID64 из того, что вставил игрок: номер или ссылка на профиль."""
from __future__ import annotations

import re

STEAM_ID_RE = re.compile(r"^7656119[0-9]{10}\Z")
PROFILE_RE = re.compile(r"steamcommunity\.com/profiles/(7656119[0-9]{10})")


def extract_steam_id(text: str) -> str:
    """SteamID64 из номера или ссылки вида steamcommunity.com/profiles/<номер>; иначе пустая строка."""
    value = (text or "").strip()
    match = PROFILE_RE.search(value)
    if match:
        return match.group(1)
    candidate = value.strip("/ ")
    return candidate if STEAM_ID_RE.match(candidate) else ""

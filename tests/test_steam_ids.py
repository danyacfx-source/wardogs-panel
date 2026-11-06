"""Разбор SteamID64 из номера или ссылки на профиль."""
import pytest

from app.bot.steam_ids import extract_steam_id


@pytest.mark.parametrize("raw", [
    "76561198012345678",
    "  76561198012345678  ",
    "https://steamcommunity.com/profiles/76561198012345678",
    "steamcommunity.com/profiles/76561198012345678/",
])
def test_accepts_number_or_profile_link(raw):
    assert extract_steam_id(raw) == "76561198012345678"


@pytest.mark.parametrize("raw", [
    "",
    "123",
    "76561198012345678999",
    "https://steamcommunity.com/id/some_nick",
    "не номер",
])
def test_rejects_everything_else(raw):
    assert extract_steam_id(raw) == ""

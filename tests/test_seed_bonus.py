"""Бонусы при старте матча: +40 сидящим, +25 вышедшим за 10 минут."""
import pytest

from app.bot import database as bot_db
from app.bot import seed_rates


def test_present_players_get_start_bonus_without_voice_check():
    got = seed_rates.start_bonus_recipients({"A", "B"}, {}, now=10_000_000)
    assert got == {"A": 40, "B": 40}


def test_leaver_within_window_gets_25_and_not_double_counted():
    now = 10_000_000
    previous = {"A": now - 60_000, "C": now - 9 * 60_000}
    got = seed_rates.start_bonus_recipients({"A"}, previous, now)
    assert got == {"A": 40, "C": 25}


def test_leaver_outside_window_gets_nothing():
    now = 10_000_000
    got = seed_rates.start_bonus_recipients({"A"}, {"D": now - 11 * 60_000}, now)
    assert got == {"A": 40}


@pytest.fixture
def bot_database(monkeypatch, tmp_path):
    monkeypatch.setattr(bot_db, "DB_FILE", str(tmp_path / "bonus.db"))
    if hasattr(bot_db, "_local"):
        bot_db._local.conn = None
    bot_db.init_db()
    yield bot_db
    if hasattr(bot_db, "_local"):
        bot_db._local.conn = None


def test_grant_bonus_once_per_key(bot_database):
    bot_database.seed_link_steam_direct("111", "76561190000000001", "Player")
    assert bot_database.seed_grant_bonus("76561190000000001", 40, "server_start_bonus", "k1") is True
    assert bot_database.seed_grant_bonus("76561190000000001", 40, "server_start_bonus", "k1") is False
    assert bot_database.seed_get_user_by_discord("111")["balance"] == 40


def test_grant_bonus_skips_unlinked_steam(bot_database):
    assert bot_database.seed_grant_bonus("76561190000000009", 40, "server_start_bonus", "k2") is False

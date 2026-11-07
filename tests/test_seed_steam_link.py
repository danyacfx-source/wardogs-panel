"""Привязка Steam через модалку: без кода, с защитой от двойной привязки."""
import pytest

from app.bot import database as bot_db


@pytest.fixture
def bot_database(monkeypatch, tmp_path):
    monkeypatch.setattr(bot_db, "DB_FILE", str(tmp_path / "bot.db"))
    if hasattr(bot_db, "_local"):
        bot_db._local.conn = None
    bot_db.init_db()
    yield bot_db
    if hasattr(bot_db, "_local"):
        bot_db._local.conn = None


def test_direct_link_creates_linked_user(bot_database):
    user = bot_database.seed_link_steam_direct("111", "76561190000000001", "Player")
    assert user["linked"] is True
    assert user["steam_id"] == "76561190000000001"
    stored = bot_database.seed_get_user_by_discord("111")
    assert stored and stored["steam_id"] == "76561190000000001"


def test_second_link_for_same_discord_is_refused(bot_database):
    bot_database.seed_link_steam_direct("111", "76561190000000001", "Player")
    with pytest.raises(bot_database.SeedDomainError) as err:
        bot_database.seed_link_steam_direct("111", "76561190000000002", "Player")
    assert err.value.code == "already_linked"


def test_steam_already_taken_is_refused(bot_database):
    bot_database.seed_link_steam_direct("111", "76561190000000001", "Player")
    with pytest.raises(bot_database.SeedDomainError) as err:
        bot_database.seed_link_steam_direct("222", "76561190000000001", "Other")
    assert err.value.code == "steam_taken"


def test_staff_unlink_by_steam_frees_steam_and_keeps_points(bot_database):
    from app import seed_store

    bot_database.seed_link_steam_direct("111", "76561190000000001", "Player")
    bot_database.seed_grant_bonus("76561190000000001", 40, "server_start_bonus", "k-unlink")
    result = seed_store.unlink_by_steam("76561190000000001", "76561190000000099")
    assert result["ok"] is True
    assert bot_database.seed_get_user_by_steam("76561190000000001") is None
    # Баллы остаются на той же записи без привязки, SteamID сохраняется.
    record = bot_database.get_conn().execute(
        "SELECT steam_id, balance, linked FROM seed_users WHERE steam_id = ?", ("76561190000000001",)
    ).fetchone()
    assert record["balance"] == 40
    assert record["linked"] == 0
    # Повторная отвязка уже ничего не находит.
    again = seed_store.unlink_by_steam("76561190000000001", "76561190000000099")
    assert again["ok"] is False


def test_relink_after_unlink_restores_current_balance(bot_database):
    steam = "76561190000000001"
    bot_database.seed_link_steam_direct("111", steam, "Player")
    bot_database.seed_grant_bonus(steam, 40, "server_start_bonus", "k-before")
    bot_database.seed_unlink_discord("111")
    # После отвязки игра продолжает копить баллы на ту же запись.
    t0 = 1_000_000_000_000
    bot_database.seed_apply_passive_tick([steam], t0, 7_200, 10_000)
    bot_database.seed_apply_passive_tick([steam], t0 + 3_600_000, 7_200, 10_000)
    user = bot_database.seed_link_steam_direct("111", steam, "Player")
    assert user["balance"] == 50
    assert bot_database.seed_get_user_by_discord("111")["balance"] == 50
    rows = bot_database.get_conn().execute(
        "SELECT COUNT(*) AS c FROM seed_users WHERE steam_id = ?", (steam,)
    ).fetchone()
    assert rows["c"] == 1

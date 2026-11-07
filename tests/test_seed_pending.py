"""Баллы сидинга до привязки: копятся по SteamID и переходят на аккаунт при привязке Discord."""
import pytest

from app.bot import database as bot_db

STEAM = "76561190000000001"
OTHER_STEAM = "76561190000000002"
T0 = 1_000_000_000_000


@pytest.fixture
def bot_database(monkeypatch, tmp_path):
    monkeypatch.setattr(bot_db, "DB_FILE", str(tmp_path / "pending.db"))
    if hasattr(bot_db, "_local"):
        bot_db._local.conn = None
    bot_db.init_db()
    yield bot_db
    if hasattr(bot_db, "_local"):
        bot_db._local.conn = None


def _pending_row(bot_database, steam=STEAM):
    return bot_database.get_conn().execute(
        "SELECT * FROM seed_users WHERE discord_id = ?", (f"pending:{steam}",)
    ).fetchone()


def test_passive_play_without_link_creates_pending_and_credits_points(bot_database):
    assert bot_database.seed_apply_passive_tick([STEAM], T0, 7_200, 10_000) == 0
    assert bot_database.seed_apply_passive_tick([STEAM], T0 + 3_600_000, 7_200, 10_000) == 1
    row = _pending_row(bot_database)
    assert row is not None
    assert row["linked"] == 0
    assert row["balance"] == 10


def test_pending_is_hidden_from_default_user_list(bot_database):
    bot_database.seed_apply_passive_tick([STEAM], T0, 7_200, 10_000)
    assert bot_database.seed_list_users() == []
    assert [u["steam_id"] for u in bot_database.seed_list_users(include_pending=True)] == [STEAM]


def test_link_claims_pending_balance_on_same_account(bot_database):
    bot_database.seed_apply_passive_tick([STEAM], T0, 7_200, 10_000)
    bot_database.seed_apply_passive_tick([STEAM], T0 + 3_600_000, 7_200, 10_000)
    pending_id = _pending_row(bot_database)["id"]

    user = bot_database.seed_link_steam_direct("111", STEAM, "Player")

    assert user["id"] == pending_id
    linked = bot_database.seed_get_user_by_discord("111")
    assert linked["balance"] == 10
    assert linked["linked"] == 1
    assert _pending_row(bot_database) is None


def test_link_with_other_steam_does_not_take_pending_points(bot_database):
    bot_database.seed_apply_passive_tick([STEAM], T0, 7_200, 10_000)
    bot_database.seed_apply_passive_tick([STEAM], T0 + 3_600_000, 7_200, 10_000)

    bot_database.seed_link_steam_direct("111", OTHER_STEAM, "Other")

    assert bot_database.seed_get_user_by_discord("111")["balance"] == 0
    assert _pending_row(bot_database)["balance"] == 10


def test_seeding_tick_credits_pending_member(bot_database):
    pending = bot_database.seed_ensure_pending(STEAM)
    member = {"discord_id": pending["discord_id"], "display_name": "Игрок из игры"}
    week = bot_database.seed_week_key(T0)
    bot_database.seed_apply_seeding_tick([member], T0, week, 180, 600)
    awards = bot_database.seed_apply_seeding_tick([member], T0 + 600_000, week, 180, 600)
    assert awards and awards[0]["discord_id"] == pending["discord_id"]
    assert bot_database.seed_list_users(include_pending=True)[0]["balance"] > 0


def test_bonus_for_unlinked_steam_is_kept_until_link(bot_database):
    assert bot_database.seed_grant_bonus(STEAM, 40, "server_start_bonus", "pending-k1") is True
    bot_database.seed_link_steam_direct("111", STEAM, "Player")
    assert bot_database.seed_get_user_by_discord("111")["balance"] == 40


def test_ensure_pending_is_idempotent(bot_database):
    first = bot_database.seed_ensure_pending(STEAM)
    second = bot_database.seed_ensure_pending(STEAM)
    assert first["id"] == second["id"]


DAY_MS = 86_400_000


def test_purge_removes_pending_after_30_days_without_activity(bot_database):
    bot_database.seed_apply_passive_tick([STEAM], T0, 7_200, 10_000)
    last_play = T0 + 3_600_000
    bot_database.seed_apply_passive_tick([STEAM], last_play, 7_200, 10_000)

    assert bot_database.seed_purge_stale_pending(last_play + 29 * DAY_MS) == 0
    assert _pending_row(bot_database) is not None

    assert bot_database.seed_purge_stale_pending(last_play + 31 * DAY_MS) == 1
    assert _pending_row(bot_database) is None
    conn = bot_database.get_conn()
    for table in ("seed_point_ledger", "seed_passive_sessions", "seed_weekly_stats", "seed_active_sessions"):
        assert conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"] == 0, table


def test_purge_never_touches_linked_accounts(bot_database):
    bot_database.seed_link_steam_direct("111", STEAM, "Player")
    assert bot_database.seed_purge_stale_pending(T0 + 365 * DAY_MS) == 0
    assert bot_database.seed_get_user_by_discord("111") is not None

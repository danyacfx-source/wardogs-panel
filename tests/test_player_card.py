"""Карточка игрока: сбор сессий и времени по дням, данные вкладок, права, подробности банов."""
import asyncio
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from app import api, config, db, main, steam
from app.db import player_card
from app.main import app

TARGET = "76561190000000555"
OTHER = "76561190000000556"
STAFF = "76561190000000321"


def _player(steam_id=TARGET, name="Медведь"):
    return {"steamId": steam_id, "name": name, "kills": 1, "deaths": 1, "cash": 100, "pingMs": 30, "faction": "A"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "card.db")
    main._rate_windows.clear()
    api._recent_actions.clear()
    with TestClient(app) as test_client:
        yield test_client


def _sign_in(client, permissions):
    async def setup():
        await db.upsert_user(STAFF, "Сотрудник")
        await db.upsert_binding(STAFF, "321", "tester", [], True)
        await db.set_role_perms("user:321", permissions)
        return await db.create_session(STAFF)

    client.cookies.set(api.SESSION_COOKIE, asyncio.run(setup()))
    client.get("/api/session")
    return {"X-CSRF-Token": client.cookies.get(api.CSRF_COOKIE) or ""}


def _tick(monkeypatch, moment, current, players):
    monkeypatch.setattr("app.db.core._now", lambda: moment)
    asyncio.run(db.record_monitoring("ru1", current, 64, players))


def test_sessions_and_daily_time_are_collected(client, monkeypatch):
    start = time.time()
    _tick(monkeypatch, start - 300, 30, [_player()])
    _tick(monkeypatch, start - 240, 30, [_player()])
    _tick(monkeypatch, start - 180, 30, [_player()])
    card = asyncio.run(db.get_player_card_activity(TARGET))
    assert len(card["sessions"]) == 1
    assert card["sessions"][0]["duration_seconds"] == 120
    assert sum(day["play_seconds"] for day in card["daily"]) == 120
    assert card["seed_seconds"] == 0


def test_seed_time_counts_only_on_a_low_population_server(client, monkeypatch):
    start = time.time()
    _tick(monkeypatch, start - 300, 5, [_player()])
    _tick(monkeypatch, start - 240, 5, [_player()])
    _tick(monkeypatch, start - 180, 30, [_player()])
    card = asyncio.run(db.get_player_card_activity(TARGET))
    assert card["seed_seconds"] == 60
    assert sum(day["play_seconds"] for day in card["daily"]) == 120
    assert card["sessions"][0]["seed_seconds"] == 60


def test_a_gap_starts_a_new_session(client, monkeypatch):
    start = time.time()
    _tick(monkeypatch, start - 2000, 30, [_player()])
    _tick(monkeypatch, start - 1940, 30, [_player()])
    _tick(monkeypatch, start - 1000, 30, [_player()])
    card = asyncio.run(db.get_player_card_activity(TARGET))
    assert len(card["sessions"]) == 2
    assert sum(day["play_seconds"] for day in card["daily"]) == 60


def test_missing_player_name_is_stored_readably(client):
    asyncio.run(db.record_monitoring("ru1", 1, 2, [{"steamId": TARGET}]))
    profile = asyncio.run(db.get_player_profile(TARGET))
    assert profile["name"] == "Игрок"


def _kill(event_id, killer, victim, match="m1", **extra):
    return {
        "event_id": event_id, "match_id": match, "event_time": time.time(), "map": "Bakurani",
        "killer_id": killer, "killer_name": "K", "killer_faction": "A", "victim_id": victim, "victim_name": "V",
        "victim_faction": "B", "cause": "AK", "distance_m": 10.0, "headshot": extra.get("headshot", False),
        "suicide": extra.get("suicide", False), "teamkill": extra.get("teamkill", False), "tags": [],
    }


def test_kill_statistics_and_matches(client):
    asyncio.run(db.record_kill_events("ru1", [
        _kill("1", TARGET, OTHER, headshot=True),
        _kill("2", TARGET, OTHER),
        _kill("3", OTHER, TARGET),
        _kill("4", TARGET, OTHER, teamkill=True),
        _kill("5", TARGET, TARGET, suicide=True),
        _kill("6", TARGET, OTHER, match="m2"),
    ]))
    card = asyncio.run(db.get_player_card_activity(TARGET))
    assert card["stats"] == {"kills": 3, "deaths": 2, "teamkills": 1, "headshots": 1}
    assert len(card["matches"]) == 2
    first_match = next(item for item in card["matches"] if item["match_id"] == "m1")
    assert (first_match["kills"], first_match["deaths"], first_match["teamkills"]) == (2, 2, 1)
    assert [item["event_id"] for item in card["teamkills"]] == ["4"]


def test_activity_route_requires_players_view(client):
    assert client.get(f"/api/players/{TARGET}/activity").status_code == 401
    _sign_in(client, ["tickets_view"])
    assert client.get(f"/api/players/{TARGET}/activity").status_code == 403
    _sign_in(client, ["players_view"])
    data = client.get(f"/api/players/{TARGET}/activity").json()
    assert data["ok"] is True and data["seed_balance"] is None


def test_bonuses_and_clan_vip_need_vip_permission(client, monkeypatch):
    summary = {"balance": 640, "seed_seconds": 50400, "clan_vip_until": 1893456000.0, "paid_tier": 0, "paid_until": None}
    monkeypatch.setattr(api.seed_store, "get_player_summary", lambda steam_id, discord_id="": summary)
    _sign_in(client, ["players_view"])
    data = client.get(f"/api/players/{TARGET}/activity").json()
    assert data["seed_balance"] is None and data["clan_vip_until"] is None
    assert data["seed_seconds"] == 50400
    _sign_in(client, ["players_view", "vip_view"])
    data = client.get(f"/api/players/{TARGET}/activity").json()
    assert data["seed_balance"] == 640 and data["clan_vip_until"] == 1893456000.0


def test_missing_bot_tables_do_not_break_the_card(client, monkeypatch):
    def broken(steam_id, discord_id=""):
        raise RuntimeError("no such table: donations_vip")

    monkeypatch.setattr(api.seed_store, "get_player_summary", broken)
    _sign_in(client, ["players_view", "vip_view"])
    response = client.get(f"/api/players/{TARGET}/activity")
    assert response.status_code == 200
    assert response.json()["seed_balance"] is None


def _seed_database(tmp_path, monkeypatch, ends_at):
    path = tmp_path / "bot.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE seed_users (discord_id TEXT, steam_id TEXT, balance INTEGER, total_seconds INTEGER, linked INTEGER);"
        "CREATE TABLE donations_vip (id TEXT, user_id INTEGER, buyer_id INTEGER, product_id TEXT, starts_at INTEGER, ends_at INTEGER);"
        "CREATE TABLE seed_system_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
    )
    connection.execute("INSERT INTO seed_users VALUES ('555', ?, 640, 50400, 1)", (TARGET,))
    connection.execute("INSERT INTO donations_vip VALUES ('a', 555, 1, 'clan_10', 0, ?)", (ends_at,))
    connection.execute("INSERT INTO donations_vip VALUES ('b', 555, 1, 'personal_12', 0, ?)", (ends_at + 9_000_000,))
    connection.commit()
    connection.close()
    monkeypatch.setattr(api.seed_store, "SEED_DB", str(path))
    monkeypatch.setattr(api.seed_store, "_seed_conn", _sqlite_opener(path))


def _sqlite_opener(path):
    def open_connection():
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        return connection

    return open_connection


def test_summary_reads_bonuses_and_only_clan_vip(tmp_path, monkeypatch):
    ends_at = int((time.time() + 86400) * 1000)
    _seed_database(tmp_path, monkeypatch, ends_at)
    summary = api.seed_store.get_player_summary(TARGET)
    assert (summary["balance"], summary["seed_seconds"], summary["clan_vip_until"]) == (640, 50400, ends_at / 1000)


def test_expired_clan_vip_is_not_shown(tmp_path, monkeypatch):
    _seed_database(tmp_path, monkeypatch, int((time.time() - 86400) * 1000))
    assert api.seed_store.get_player_summary(TARGET)["clan_vip_until"] is None


def _ended(event_id="end-1", winner="Bluefor", match_id="m9", moment=None):
    return {
        "type": "match_ended", "eventId": event_id, "eventTime": moment or time.time(), "matchId": match_id,
        "mapName": "Bakurani", "players": 2, "winningFaction": winner,
        "factionScores": [{"faction": "Bluefor", "score": 500}, {"faction": "Redfor", "score": 300}],
    }


class _Roster:
    def __init__(self, players):
        self.listing = {"players": players}

    async def players(self):
        return self.listing


def _server_with(players):
    return type("Server", (), {"api": _Roster(players)})()


ROSTER = [
    {"steamId": TARGET, "faction": "Bluefor"},
    {"steamId": OTHER, "faction": "Redfor"},
]


def _finish(results, roster=ROSTER):
    asyncio.run(api._record_match_results("ru1", _server_with(roster), results))


def test_win_rate_counts_winners_and_losers(client):
    _finish(api._parse_match_results({"events": [_ended("e1"), _ended("e2", winner="Redfor", match_id="m10"), _ended("e3", match_id="m11")]}))
    target = asyncio.run(db.get_player_card_activity(TARGET))
    assert (target["wins"], target["losses"], target["win_rate"]) == (2, 1, 67)
    other = asyncio.run(db.get_player_card_activity(OTHER))
    assert (other["wins"], other["losses"], other["win_rate"]) == (1, 2, 33)


def test_redelivered_match_is_counted_once(client):
    for _ in range(2):
        _finish(api._parse_match_results({"events": [_ended("same")]}))
    assert asyncio.run(db.get_player_card_activity(TARGET))["wins"] == 1


def test_draw_is_not_counted_in_win_rate(client):
    _finish(api._parse_match_results({"events": [_ended("tie", winner="")]}))
    card = asyncio.run(db.get_player_card_activity(TARGET))
    assert card["win_rate"] is None and card["matches"][0]["won"] is None


def test_faction_comparison_ignores_case(client):
    _finish(api._parse_match_results({"events": [_ended("case", winner="BLUEFOR")]}))
    assert asyncio.run(db.get_player_card_activity(TARGET))["wins"] == 1


def test_finished_match_without_kills_appears_in_games(client):
    _finish(api._parse_match_results({"events": [_ended("nokills", match_id="m77")]}))
    matches = asyncio.run(db.get_player_card_activity(TARGET))["matches"]
    assert [(item["match_id"], item["won"], item["kills"]) for item in matches] == [("m77", True, 0)]


def test_match_result_joins_kills_of_the_same_match(client):
    asyncio.run(db.record_kill_events("ru1", [_kill("k1", TARGET, OTHER, match="m5")]))
    _finish(api._parse_match_results({"events": [_ended("joined", match_id="m5")]}))
    matches = asyncio.run(db.get_player_card_activity(TARGET))["matches"]
    assert len(matches) == 1
    assert (matches[0]["kills"], matches[0]["won"]) == (1, True)


def test_roster_failure_falls_back_to_recent_activity(client):
    asyncio.run(db.record_monitoring("ru1", 30, 64, [_player()]))

    class Broken:
        async def players(self):
            raise RuntimeError("нет связи")

    server = type("Server", (), {"api": Broken()})()
    asyncio.run(api._record_match_results("ru1", server, api._parse_match_results({"events": [_ended("fb", winner="A")]})))
    assert asyncio.run(db.get_player_card_activity(TARGET))["wins"] == 1


def test_bad_match_events_are_ignored():
    assert api._parse_match_results({"events": [{"type": "match_ended"}, {"type": "killed"}, "мусор", {"type": "match_ended", "eventTime": True}]}) == []


def test_killfeed_ingest_records_match_end(client, monkeypatch):
    monkeypatch.setattr(config, "KILLFEED_TOKEN", "token")
    server = api.Server({"id": "ru1", "name": "Тест", "host": "127.0.0.1", "port": 1})
    monkeypatch.setattr(server.api, "players", _Roster(ROSTER).players)
    monkeypatch.setitem(api.POOL, "ru1", server)
    response = client.post(
        "/api/killfeed/ru1", headers={"Authorization": "Bearer token"},
        json={"serverId": "x", "serverName": "y", "events": [_ended("wire")]},
    )
    assert response.status_code == 200, response.text
    assert asyncio.run(db.get_player_card_activity(TARGET))["wins"] == 1


def _action_server(monkeypatch):
    server = api.Server({"id": "ru1", "name": "Тест", "host": "127.0.0.1", "port": 1})
    sent = []

    async def request(method, path, json_body=None, **kwargs):
        sent.append((method, path, json_body))
        return {}

    monkeypatch.setattr(server.api, "request", request)
    monkeypatch.setitem(api.POOL, "ru1", server)
    return sent


def test_warning_is_sent_and_recorded_with_text(client, monkeypatch):
    sent = _action_server(monkeypatch)
    headers = _sign_in(client, ["players_view", "message", "ban_view"])
    response = client.post(f"/api/server/ru1/players/{TARGET}/message", headers=headers, json={"message": "Не тимкиль"})
    assert response.status_code == 200, response.text
    assert sent == [("POST", f"/v1/players/{TARGET}/message", {"message": "Не тимкиль"})]
    rows = api._with_punishment_details(
        asyncio.run(db.list_player_punishments(TARGET)), asyncio.run(db.list_punishment_details(TARGET)),
    )
    assert [(row["event"], row["reason"]) for row in rows] == [("rcon.message", "Не тимкиль")]


def test_two_warnings_keep_their_own_text():
    now = time.time()
    rows = [_entry("rcon.message", now - 10), _entry("rcon.message", now - 20)]
    history = [
        {"kind": "warn", "server_id": "ru1", "reason": "второй", "expires_utc": None, "created_utc": now - 10},
        {"kind": "warn", "server_id": "ru1", "reason": "первый", "expires_utc": None, "created_utc": now - 20},
    ]
    result = api._with_punishment_details(rows, history)
    assert [row["reason"] for row in result] == ["второй", "первый"]


def test_kick_reason_is_recorded(client, monkeypatch):
    _action_server(monkeypatch)
    headers = _sign_in(client, ["kick", "ban_view"])
    assert client.post(f"/api/server/ru1/players/{TARGET}/kick", headers=headers, json={"reason": "АФК"}).status_code == 200
    details = asyncio.run(db.list_punishment_details(TARGET))
    assert [(item["kind"], item["reason"]) for item in details] == [("kick", "АФК")]


def test_failed_warning_is_not_recorded(client, monkeypatch):
    server = api.Server({"id": "ru1", "name": "Тест", "host": "127.0.0.1", "port": 1})

    async def request(method, path, json_body=None, **kwargs):
        raise api.RCONError(404, "not_online", "игрок не в сети")

    monkeypatch.setattr(server.api, "request", request)
    monkeypatch.setitem(api.POOL, "ru1", server)
    headers = _sign_in(client, ["message"])
    assert client.post(f"/api/server/ru1/players/{TARGET}/message", headers=headers, json={"message": "x"}).status_code == 404
    assert asyncio.run(db.list_punishment_details(TARGET)) == []


def test_steam_hours_use_the_wardogs_app_id(monkeypatch):
    monkeypatch.setattr(config, "STEAM_API_KEY", "key")
    monkeypatch.setattr(steam, "_hours_cache", {})
    seen = {}

    class Response:
        status = 200

        async def json(self):
            return {"response": {"game_count": 1, "games": [{"appid": 1867240, "playtime_forever": 71220}]}}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class Session:
        def __init__(self, **kwargs):
            pass

        def get(self, url, params=None):
            seen["url"], seen["params"] = url, params
            return Response()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(steam.aiohttp, "ClientSession", Session)
    assert asyncio.run(steam.fetch_game_hours(TARGET)) == 1187
    assert seen["params"]["appids_filter[0]"] == "1867240"
    assert "GetOwnedGames" in seen["url"]


def test_private_steam_profile_has_no_hours(monkeypatch):
    monkeypatch.setattr(config, "STEAM_API_KEY", "key")
    monkeypatch.setattr(steam, "_hours_cache", {})

    async def private(ids):
        return {}

    monkeypatch.setattr(steam, "_fetch_game_minutes", private)
    assert asyncio.run(steam.fetch_game_hours(TARGET)) is None


def _entry(event, stamp, server="ru1"):
    return {"id": stamp, "event": event, "timestamp_utc": stamp, "server_id": server}


def test_ban_status_and_reason():
    now = time.time()
    rows = [
        _entry("ban.add", now - 10),
        _entry("ban.remove", now - 100),
        _entry("ban.add", now - 200),
        _entry("ban.add", now - 90000),
    ]
    history = [
        {"kind": "ban", "server_id": "ru1", "reason": "Читы", "expires_utc": None, "created_utc": now - 10},
        {"kind": "ban", "server_id": "ru1", "reason": "Баг", "expires_utc": now + 500, "created_utc": now - 200},
        {"kind": "ban", "server_id": "ru1", "reason": "Старый", "expires_utc": now - 80000, "created_utc": now - 90000},
    ]
    result = api._with_punishment_details(rows, history)
    assert (result[0]["reason"], result[0]["status"]) == ("Читы", "active")
    assert result[1]["status"] == ""
    assert (result[2]["reason"], result[2]["status"]) == ("Баг", "removed")
    assert (result[3]["reason"], result[3]["status"]) == ("Старый", "removed")


def test_expired_ban_is_marked_expired():
    now = time.time()
    rows = [_entry("ban.add", now - 1000)]
    history = [{"kind": "ban", "server_id": "ru1", "reason": "x", "expires_utc": now - 10, "created_utc": now - 1000}]
    assert api._with_punishment_details(rows, history)[0]["status"] == "expired"


def test_old_ban_without_details_keeps_empty_reason():
    now = time.time()
    result = api._with_punishment_details([_entry("ban.add", now - 1000)], [])
    assert result[0]["reason"] == "" and result[0]["status"] == "active"


def _roster(*sides):
    return [{"steamId": steam_id, "faction": faction} for steam_id, faction in sides]


def test_equal_teams_trade_sixteen_points():
    changes = player_card.rating_changes({"a": 1500.0, "b": 1500.0}, ["a"], ["b"])
    assert changes == {"a": 16.0, "b": -16.0}


def test_upset_win_is_worth_more_than_expected_win():
    upset = player_card.rating_changes({"a": 1300.0, "b": 1700.0}, ["a"], ["b"])
    expected = player_card.rating_changes({"a": 1700.0, "b": 1300.0}, ["a"], ["b"])
    assert upset["a"] > 25 > 5 > expected["a"] > 0


@pytest.mark.parametrize("percentile,band", [
    (0, "Низкий"), (9, "Низкий"), (10, "Ниже среднего"), (22, "Ниже среднего"), (40, "Средний"),
    (59, "Средний"), (60, "Выше среднего"), (85, "Высокий"), (100, "Высокий"),
])
def test_rating_bands(percentile, band):
    assert player_card.rating_band(percentile) == band


def test_match_updates_rating_of_both_sides(client):
    _finish(api._parse_match_results({"events": [_ended("r1")]}))
    assert asyncio.run(db.get_player_card_activity(TARGET))["rating"] == {
        "value": 1516, "games": 1, "percentile": 100, "band": "Высокий",
    }
    assert asyncio.run(db.get_player_card_activity(OTHER))["rating"] == {
        "value": 1484, "games": 1, "percentile": 0, "band": "Низкий",
    }


def test_percentile_among_many_players(client):
    roster = _roster((TARGET, "Bluefor"), *[(f"7656119000000{1000 + i}", "Redfor") for i in range(4)])
    _finish(api._parse_match_results({"events": [_ended("many", winner="Redfor")]}), roster)
    rating = asyncio.run(db.get_player_card_activity(TARGET))["rating"]
    assert rating["value"] < 1500 and rating["percentile"] == 0


def test_redelivered_match_does_not_change_rating_twice(client):
    for _ in range(2):
        _finish(api._parse_match_results({"events": [_ended("again")]}))
    assert asyncio.run(db.get_player_card_activity(TARGET))["rating"]["games"] == 1


@pytest.mark.parametrize("winner,roster", [
    ("", ROSTER),
    ("Bluefor", _roster((TARGET, "Bluefor"))),
    ("Bluefor", _roster((OTHER, "Redfor"))),
])
def test_draw_or_one_sided_match_leaves_rating_alone(client, winner, roster):
    _finish(api._parse_match_results({"events": [_ended("none", winner=winner)]}), roster)
    assert asyncio.run(db.get_player_card_activity(TARGET))["rating"] is None
    assert asyncio.run(db.get_player_card_activity(OTHER))["rating"] is None


def test_player_with_unknown_side_is_not_rated(client):
    roster = _roster((TARGET, "Bluefor"), (OTHER, "Redfor"), ("76561190000000999", ""))
    _finish(api._parse_match_results({"events": [_ended("blank")]}), roster)
    assert asyncio.run(db.get_player_card_activity("76561190000000999"))["rating"] is None


def test_new_player_has_no_rating_on_card(client):
    assert asyncio.run(db.get_player_card_activity(TARGET))["rating"] is None

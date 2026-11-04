"""Отчёты: статистика за период, ники, матчи, поиск в журнале, таблица VIP."""
import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import api, config, db, main
from app.main import app

A = "76561190000000701"
B = "76561190000000702"
STAFF = "76561190000000321"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "reports.db")
    monkeypatch.setattr(config, "OWNER_STEAM_IDS", set())
    main._rate_windows.clear()
    api._cache.clear()
    api._failures.clear()
    with TestClient(app) as test_client:
        yield test_client


def _sign_in(client, permissions):
    async def setup():
        await db.upsert_user(STAFF, "Сотрудник")
        await db.upsert_binding(STAFF, "321", "tester", [], True)
        await db.set_role_perms("user:321", permissions)
        return await db.create_session(STAFF)

    client.cookies.set(api.SESSION_COOKIE, asyncio.run(setup()))


def _player(steam_id, name, cash=100):
    return {"steamId": steam_id, "name": name, "kills": 0, "deaths": 0, "cash": cash, "pingMs": 1, "faction": "A"}


def _day(offset=0):
    return (datetime.now(timezone.utc) - timedelta(days=offset)).strftime("%Y-%m-%d")


def _kill(event_id, killer, victim, moment, server="ru1", **extra):
    return {
        "event_id": event_id, "match_id": "m1", "event_time": moment, "map": "Bakurani", "killer_id": killer,
        "killer_name": "K", "killer_faction": "A", "victim_id": victim, "victim_name": "V", "victim_faction": "B",
        "cause": "AK", "distance_m": 1.0, "headshot": False, "suicide": False, "teamkill": extra.get("teamkill", False), "tags": [],
    }


def _seed_stats():
    async def run():
        await db.record_monitoring("ru1", 30, 64, [_player(A, "Волк", 500), _player(B, "Медведь", 900)])
        now = time.time()
        await db.record_kill_events("ru1", [
            _kill("today-1", A, B, now), _kill("today-2", A, B, now), _kill("today-3", B, A, now),
            _kill("tk", A, B, now, teamkill=True), _kill("old", B, A, now - 10 * 86400),
        ])
        await db.record_kill_events("ru2", [_kill("other", A, B, now)])

    asyncio.run(run())


def _ended(event_id, winner, moment=None, **extra):
    return {
        "type": "match_ended", "eventId": event_id, "eventTime": moment or time.time(), "matchId": event_id,
        "mapName": "Ozeti", "players": 39, "winningFaction": winner,
        "factionScores": [{"faction": "Bluefor", "score": 56}, {"faction": "Redfor", "score": 100}], **extra,
    }


class _Roster:
    def __init__(self, players):
        self.listing = {"players": players}

    async def players(self):
        return self.listing


def _finish(raw, server="ru1"):
    roster = [{"steamId": A, "faction": "Redfor"}, {"steamId": B, "faction": "Bluefor"}]
    target = type("Server", (), {"api": _Roster(roster)})()
    asyncio.run(api._record_match_results(server, target, api._parse_match_results({"events": [raw]})))


def _rows(client, **params):
    response = client.get("/api/players/stats", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_stats_merge_all_sources(client):
    _seed_stats()
    _finish(_ended("win", "Redfor"))
    _sign_in(client, ["players_view"])
    data = _rows(client, server="ru1", first_day=_day(), last_day=_day())
    by_id = {row["steam_id"]: row for row in data["items"]}
    assert data["total"] == 2
    assert (by_id[A]["name"], by_id[A]["kills"], by_id[A]["deaths"], by_id[A]["teamkills"]) == ("Волк", 2, 1, 1)
    assert (by_id[A]["wins"], by_id[A]["losses"], by_id[A]["matches"]) == (1, 0, 1)
    assert (by_id[B]["wins"], by_id[B]["losses"]) == (0, 1)
    assert by_id[A]["kd"] == 2.0
    assert by_id[B]["cash"] == 900


def test_period_excludes_older_events(client):
    _seed_stats()
    _sign_in(client, ["players_view"])
    today = {row["steam_id"]: row for row in _rows(client, server="ru1", first_day=_day())["items"]}
    everything = {row["steam_id"]: row for row in _rows(client, server="ru1")["items"]}
    assert today[B]["kills"] == 1 and everything[B]["kills"] == 2


def test_server_filter_separates_servers(client):
    _seed_stats()
    _sign_in(client, ["players_view"])
    rows = {row["steam_id"]: row for row in _rows(client, server="ru2")["items"]}
    assert rows[A]["kills"] == 1 and rows[B]["deaths"] == 1
    assert client.get("/api/players/stats", params={"server": "nope"}).status_code == 404


def test_search_is_case_insensitive_and_finds_discord_id(client):
    _seed_stats()
    asyncio.run(db.upsert_binding(B, "424242", "m", [], True))
    _sign_in(client, ["players_view"])
    assert [row["steam_id"] for row in _rows(client, q="волк")["items"]] == [A]
    assert [row["steam_id"] for row in _rows(client, q="424242")["items"]] == [B]


def test_sorting_and_paging(client):
    _seed_stats()
    _sign_in(client, ["players_view"])
    asc = _rows(client, sort="kills", direction="asc", server="ru1")["items"]
    assert asc[0]["kills"] <= asc[1]["kills"]
    assert _rows(client, sort="name", direction="asc")["items"][0]["name"] == "Волк"
    page = _rows(client, per_page=25, page=99)
    assert page["items"] == [] and page["total"] == 2
    assert _rows(client, sort="; DROP TABLE users", per_page=999)["per_page"] == 50


def test_admin_scope_keeps_only_staff(client):
    _seed_stats()

    async def bind():
        await db.upsert_binding(A, "111", "a", [], True)
        await db.set_role_perms("user:111", ["kick"])
        await db.upsert_binding(B, "222", "b", [], True)

    asyncio.run(bind())
    _sign_in(client, ["players_view"])
    assert [row["steam_id"] for row in _rows(client, scope="admins")["items"]] == [A]


def test_bad_date_is_rejected(client):
    _sign_in(client, ["players_view"])
    assert client.get("/api/players/stats", params={"first_day": "вчера"}).status_code == 400


@pytest.mark.parametrize("path", ["/api/players/stats", "/api/players/names", "/api/matches", "/api/audit/search"])
def test_reports_need_permissions(client, path):
    assert client.get(path).status_code == 401
    _sign_in(client, ["tickets_view"])
    assert client.get(path).status_code == 403


def test_name_history_keeps_old_names(client):
    async def run():
        await db.record_monitoring("ru1", 1, 2, [_player(A, "СтароеИмя")])
        await db.record_monitoring("ru1", 1, 2, [_player(A, "НовоеИмя")])

    asyncio.run(run())
    _sign_in(client, ["players_view"])
    names = client.get("/api/players/names", params={"q": "Старое"}).json()
    assert [item["steam_id"] for item in names["items"]] == [A]
    activity = client.get(f"/api/players/{A}/activity").json()
    assert {item["name"] for item in activity["names"]} == {"СтароеИмя", "НовоеИмя"}


def test_percent_in_search_is_literal(client):
    asyncio.run(db.record_monitoring("ru1", 1, 2, [_player(A, "Волк")]))
    _sign_in(client, ["players_view"])
    assert client.get("/api/players/names", params={"q": "%"}).json()["total"] == 0


def _no_live(monkeypatch):
    async def none(server_filter):
        return []

    monkeypatch.setattr(api, "_live_matches", none)


def test_matches_list_with_duration_score_and_filters(client, monkeypatch):
    _no_live(monkeypatch)
    now = time.time()
    asyncio.run(db.note_match_started("ru1", now - 1800))
    asyncio.run(db.note_match_started("ru1", now - 100))
    _finish(_ended("m-a", "Redfor", now))
    _finish(_ended("m-b", "Bluefor", now - 5000), "ru2")
    _sign_in(client, ["players_view"])
    data = client.get("/api/matches").json()
    assert data["total"] == 2
    first = data["items"][0]
    assert first["started_utc"] == pytest.approx(now - 1800)
    assert first["winning_faction"] == "Redfor" and first["players"] == 39
    assert first["scores"][1] == {"faction": "Redfor", "score": 100}
    assert data["items"][1]["started_utc"] is None
    assert client.get("/api/matches", params={"server": "ru2"}).json()["total"] == 1
    assert client.get("/api/matches", params={"player": A}).json()["total"] == 2
    assert client.get("/api/matches", params={"player": "76561190000000999"}).json()["total"] == 0


def test_ended_match_clears_start_for_the_next_one(client):
    now = time.time()
    asyncio.run(db.note_match_started("ru1", now - 100))
    _finish(_ended("first", "Redfor", now))
    assert asyncio.run(db.current_match_starts()) == {}


def test_feed_events_track_match_start_in_order(client, monkeypatch):
    monkeypatch.setattr(config, "KILLFEED_TOKEN", "token")
    server = api.Server({"id": "ru1", "name": "Тест", "host": "127.0.0.1", "port": 1})
    monkeypatch.setattr(server.api, "players", _Roster([{"steamId": A, "faction": "Redfor"}]).players)
    monkeypatch.setitem(api.POOL, "ru1", server)
    now = time.time()
    events = [
        {"type": "match_state", "eventTime": now - 900, "state": "MatchState.PreMatch"},
        {"type": "match_state", "eventTime": now - 800, "state": "MatchState.Playing.KOTH"},
        {"type": "match_state", "eventTime": now - 700, "state": "MatchState.Playing"},
        _ended("fed", "Redfor", now),
        {"type": "match_state", "eventTime": now + 5, "state": "MatchState.Playing.KOTH"},
    ]
    response = client.post("/api/killfeed/ru1", headers={"Authorization": "Bearer token"}, json={"events": events})
    assert response.status_code == 200, response.text
    assert asyncio.run(db.list_matches())["items"][0]["started_utc"] == pytest.approx(now - 800)
    assert asyncio.run(db.current_match_starts())["ru1"] == pytest.approx(now + 5)


def test_live_match_is_listed_first_and_hung_server_is_skipped(client, monkeypatch):
    live = api.Server({"id": "ru1", "name": "Живой", "host": "127.0.0.1", "port": 1})
    dead = api.Server({"id": "ru2", "name": "Мёртвый", "host": "127.0.0.1", "port": 1})

    async def status():
        return {"map": "Bakurani", "matchSeconds": 600, "players": {"current": 38},
                "factionScores": [{"name": "Bluefor", "score": 29, "colorHex": "#2244ff"}]}

    async def down():
        raise api.RCONError(0, "timeout", "нет ответа")

    monkeypatch.setattr(live.api, "status", status)
    monkeypatch.setattr(dead.api, "status", down)
    monkeypatch.setattr(api, "POOL", {"ru1": live, "ru2": dead})
    _sign_in(client, ["players_view"])
    data = client.get("/api/matches").json()
    assert [item["server_id"] for item in data["live"]] == ["ru1"]
    row = data["live"][0]
    assert row["in_progress"] is True and row["players"] == 38
    assert row["scores"] == [{"faction": "Bluefor", "score": 29, "color": "#2244ff"}]
    assert time.time() - row["started_utc"] == pytest.approx(600, abs=30)
    assert client.get("/api/matches", params={"page": 2}).json()["live"] == []


def _seed_audit():
    async def run():
        await db.upsert_user(A, "Волк")
        await db.log_site_audit(STAFF, "ru1", "ban.add", A)
        await db.log_site_audit(STAFF, "ru2", "rcon.kick", B)
        await db.log_site_audit(STAFF, None, "clan.create", "abc")
        await db.log_site_audit(STAFF, "ru1", "rcon.change_map", "")

    asyncio.run(run())


def _audit(client, **params):
    response = client.get("/api/audit/search", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_audit_search_filters(client):
    _seed_audit()
    _sign_in(client, ["audit"])
    assert _audit(client)["total"] >= 4
    assert [item["event"] for item in _audit(client, section="bans")["items"]] == ["ban.add"]
    assert {item["event"] for item in _audit(client, section="players")["items"]} >= {"rcon.kick"}
    assert all(item["server_id"] == "ru2" for item in _audit(client, server="ru2")["items"])
    assert [item["event"] for item in _audit(client, q=B)["items"]] == ["rcon.kick"]


def test_audit_shows_target_name_and_finds_by_it(client):
    _seed_audit()
    _sign_in(client, ["audit"])
    found = _audit(client, q="Волк")["items"]
    assert [item["event"] for item in found] == ["ban.add"]
    assert found[0]["target_name"] == "Волк" and found[0]["actor_name"] == "Сотрудник"


def test_audit_paging_keeps_newest_first(client):
    _seed_audit()
    _sign_in(client, ["audit"])
    assert _audit(client, per_page=25)["items"][0]["event"] == "rcon.change_map"
    assert _audit(client, per_page=25, page=50)["items"] == []


def test_vip_list_names_who_changed_it(client):
    async def run():
        await db.upsert_user(STAFF, "Сотрудник")
        await db.save_vip_slot("ru1", A, "Волк", None, "", STAFF)

    asyncio.run(run())
    _sign_in(client, ["vip_view"])
    items = client.get("/api/server/ru1/vip-slots").json()["items"]
    assert items[0]["changed_by_name"] == "Сотрудник"


def _roster_server(monkeypatch, players):
    server = api.Server({"id": "ru1", "name": "Тест", "host": "127.0.0.1", "port": 1})

    async def listing():
        return {"count": len(players), "players": players}

    monkeypatch.setattr(server.api, "players", listing)
    monkeypatch.setitem(api.POOL, "ru1", server)
    return server


def test_players_meta_combines_hours_clan_vip_and_admins(client, monkeypatch):
    _roster_server(monkeypatch, [{"steamId": A, "name": "Волк"}, {"steamId": B, "name": "Медведь"}])
    now = time.time()
    monkeypatch.setattr("app.db.core._now", lambda: now - 60)
    asyncio.run(db.record_monitoring("ru1", 30, 64, [_player(A, "Волк")]))
    monkeypatch.setattr("app.db.core._now", lambda: now)
    asyncio.run(db.record_monitoring("ru1", 30, 64, [_player(A, "Волк")]))

    async def setup():
        await db.save_vip_slot("ru1", B, "Медведь", None, "", STAFF)
        await db.upsert_binding(A, "111", "a", [], True)
        await db.set_role_perms("user:111", ["kick"])

    asyncio.run(setup())
    monkeypatch.setattr(api.steam_mod, "cached_game_hours", lambda ids: {A: 1187})
    monkeypatch.setattr(api.steam_mod, "start_game_hours_fill", lambda ids: None)
    _sign_in(client, ["kick"])
    players = client.get("/api/server/ru1/players/meta").json()["players"]
    assert players[A]["play_seconds"] == 60 and players[A]["admin"] is True and players[A]["game_hours"] == 1187
    assert players[B]["vip"] is True and players[B]["admin"] is False and players[B]["game_hours"] is None


def test_players_meta_needs_player_permissions(client):
    assert client.get("/api/server/ru1/players/meta").status_code == 401
    _sign_in(client, ["tickets_view"])
    assert client.get("/api/server/ru1/players/meta").status_code == 403


def test_players_meta_survives_rcon_failure(client, monkeypatch):
    server = _roster_server(monkeypatch, [])

    async def down():
        raise api.RCONError(0, "timeout", "нет ответа")

    monkeypatch.setattr(server.api, "players", down)
    _sign_in(client, ["players_view"])
    response = client.get("/api/server/ru1/players/meta")
    assert response.status_code == 200 and response.json()["players"] == {}


def test_players_meta_does_not_wait_for_steam(client, monkeypatch):
    _roster_server(monkeypatch, [{"steamId": A, "name": "Волк"}])
    monkeypatch.setattr(config, "STEAM_API_KEY", "key")
    monkeypatch.setattr(api.steam_mod, "_hours_cache", {})
    started = []

    async def slow(ids):
        started.append(list(ids))
        await asyncio.sleep(5)
        return {}

    monkeypatch.setattr(api.steam_mod, "_fetch_game_minutes", slow)
    monkeypatch.setattr(api.steam_mod, "_hours_fill_task", None)
    _sign_in(client, ["players_view"])
    begin = time.perf_counter()
    response = client.get("/api/server/ru1/players/meta")
    assert response.status_code == 200
    assert time.perf_counter() - begin < 3
    assert response.json()["players"][A]["game_hours"] is None


def test_regular_scope_hides_admins_and_active_vip(client):
    _seed_stats()
    third = "76561190000000703"

    async def setup():
        await db.record_monitoring("ru1", 30, 64, [_player(third, "Обычный")])
        await db.upsert_binding(A, "111", "a", [], True)
        await db.set_role_perms("user:111", ["kick"])
        await db.save_vip_slot("ru1", B, "Медведь", None, "", STAFF)

    asyncio.run(setup())
    _sign_in(client, ["players_view"])
    assert [row["steam_id"] for row in _rows(client, scope="regular")["items"]] == [third]


def test_expired_vip_counts_as_regular(client):
    _seed_stats()

    async def setup():
        await db.save_vip_slot("ru1", B, "Медведь", time.time() - 10, "", STAFF)

    asyncio.run(setup())
    _sign_in(client, ["players_view"])
    assert B in {row["steam_id"] for row in _rows(client, scope="regular")["items"]}

"""Скорость: параллельный RCON, кэш, зависший сервер, Steam отдельно, сжатие и кэш файлов."""
import asyncio
import gzip
import time

import pytest
from fastapi.testclient import TestClient

from app import api, config, db, main, steam
from app import discord as dmod
from app.main import app
from app.rcon_client import RCONError, WardogsRCON

STEAM_ID = "76561190000000321"


@pytest.fixture(autouse=True)
def clean_state():
    api._cache.clear()
    api._failures.clear()
    api._refresh_tasks.clear()
    steam._profile_cache.clear()
    steam._bans_cache.clear()
    main._rate_windows.clear()
    yield
    api._cache.clear()
    api._failures.clear()


def _server(monkeypatch, delay=0.0, fail=False):
    server = api.Server({"id": "ru1", "name": "Тест", "host": "127.0.0.1", "port": 1})
    tracker = {"active": 0, "peak": 0, "calls": 0}

    def endpoint(payload):
        async def call():
            tracker["active"] += 1
            tracker["calls"] += 1
            tracker["peak"] = max(tracker["peak"], tracker["active"])
            try:
                await asyncio.sleep(delay)
                if tracker.get("fail", fail):
                    raise RCONError(0, "timeout", "нет ответа")
                return payload
            finally:
                tracker["active"] -= 1
        return call

    monkeypatch.setattr(server.api, "status", endpoint({"map": "Bakurani", "players": {"current": 1, "max": 2}}))
    monkeypatch.setattr(server.api, "players", endpoint({"count": 1, "players": []}))
    monkeypatch.setattr(server.api, "rotation", endpoint({"entries": []}))
    monkeypatch.setattr(server.api, "health", endpoint({"ok": True}))
    monkeypatch.setattr(server.api, "bans", endpoint({"count": 0, "bans": []}))

    async def capabilities():
        return {"routes": []}

    monkeypatch.setattr(server.api, "capabilities", capabilities)
    monkeypatch.setitem(api.POOL, "ru1", server)
    return server, tracker


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "speed.db")
    with TestClient(app) as test_client:
        yield test_client


def _sign_in(client, permissions):
    async def setup():
        await db.upsert_user(STEAM_ID, "Игрок")
        await db.upsert_binding(STEAM_ID, "321", "tester", [], True)
        await db.set_role_perms("user:321", permissions)
        return await db.create_session(STEAM_ID)

    client.cookies.set(api.SESSION_COOKIE, asyncio.run(setup()))


def test_overview_queries_rcon_in_parallel(client, monkeypatch):
    _, tracker = _server(monkeypatch, delay=0.05)
    assert client.get("/api/server/ru1/overview").status_code == 200
    assert tracker["peak"] == 5


def test_expired_cache_answers_immediately_and_refreshes_in_background(client, monkeypatch):
    _, tracker = _server(monkeypatch, delay=0.3)
    assert client.get("/api/server/ru1/overview").status_code == 200
    calls = tracker["calls"]
    for key, (expires, data) in list(api._cache.items()):
        api._cache[key] = (time.monotonic() - 1, data)
    start = time.perf_counter()
    assert client.get("/api/server/ru1/overview").status_code == 200
    assert time.perf_counter() - start < 0.25
    deadline = time.monotonic() + 3
    while tracker["calls"] == calls and time.monotonic() < deadline:
        client.get("/api/health")
        time.sleep(0.02)
    assert tracker["calls"] > calls


def test_fresh_flag_bypasses_stale_data(client, monkeypatch):
    _, tracker = _server(monkeypatch, delay=0.01)
    client.get("/api/server/ru1/overview")
    calls = tracker["calls"]
    for key, (expires, data) in list(api._cache.items()):
        api._cache[key] = (time.monotonic() - 1, data)
    client.get("/api/server/ru1/overview?fresh=1")
    assert tracker["calls"] == calls + 5


def test_hung_server_is_not_waited_for_twice(client, monkeypatch):
    _, tracker = _server(monkeypatch, fail=True)
    assert client.get("/api/server/ru1/overview").status_code == 502
    calls = tracker["calls"]
    assert client.get("/api/server/ru1/overview").status_code == 502
    assert tracker["calls"] == calls


def test_command_invalidation_drops_stale_data_and_late_refresh():
    async def run():
        versions = iter(["до команды", "после команды"])

        async def loader():
            return next(versions)

        assert await api._cached("g", "v", loader, stale=20.0) == "до команды"
        generation_loader_started = asyncio.Event()
        release = asyncio.Event()

        async def slow_old():
            generation_loader_started.set()
            await release.wait()
            return "старый ответ"

        task = asyncio.create_task(api._load_cached(("g", "v"), slow_old, 3.0, True))
        await generation_loader_started.wait()
        await api._invalidate("g")
        release.set()
        await task
        assert ("g", "v") not in api._cache
        assert await api._cached("g", "v", loader, stale=20.0) == "после команды"

    asyncio.run(run())


def test_read_timeout_is_short_but_commands_keep_full_timeout():
    rcon = WardogsRCON({"timeout": 45})
    assert rcon.read_timeout.total == 12
    assert rcon.timeout.total == 45
    assert WardogsRCON({"timeout": 5}).read_timeout.total == 5


def test_player_card_does_not_wait_for_steam(client, monkeypatch):
    _sign_in(client, ["players_view"])

    async def setup():
        await db.record_monitoring("ru1", 1, 2, [{"steamId": STEAM_ID, "name": "Игрок", "kills": 1, "deaths": 1, "cash": 0, "pingMs": 1, "faction": "A"}])

    asyncio.run(setup())

    async def must_not_run(ids):
        raise AssertionError("карточка игрока не должна ждать Steam")

    monkeypatch.setattr(steam, "fetch_profiles", must_not_run)
    monkeypatch.setattr(steam, "fetch_player_bans", must_not_run)
    response = client.get(f"/api/players/{STEAM_ID}")
    assert response.status_code == 200
    assert response.json()["profile"]["steam"] is None


def test_steam_details_endpoint_and_permissions(client, monkeypatch):
    _sign_in(client, ["players_view"])

    async def profiles(ids):
        return {ids[0]: {"avatar_url": "https://a/x.jpg", "profile_url": "https://p"}}

    async def bans(ids):
        return {ids[0]: {"vac_banned": True, "vac_bans": 2, "game_bans": 0, "days_since_last_ban": 5}}

    monkeypatch.setattr(steam, "fetch_profiles", profiles)
    monkeypatch.setattr(steam, "fetch_player_bans", bans)
    data = client.get(f"/api/players/{STEAM_ID}/steam").json()
    assert data["avatar_url"] == "https://a/x.jpg"
    assert data["steam"]["vac_bans"] == 2 and data["steam"]["available"] is True

    _sign_in(client, ["tickets_view"])
    assert client.get(f"/api/players/{STEAM_ID}/steam").status_code == 403


def test_steam_results_are_cached(monkeypatch):
    monkeypatch.setattr(config, "STEAM_API_KEY", "key")
    calls = []

    async def fetch(ids):
        calls.append(list(ids))
        return {"1": {"avatar_url": "a"}}

    monkeypatch.setattr(steam, "_fetch_profiles", fetch)

    async def run():
        first = await steam.fetch_profiles(["1", "2"])
        second = await steam.fetch_profiles(["1", "2"])
        return first, second

    first, second = asyncio.run(run())
    assert first == second == {"1": {"avatar_url": "a"}}
    assert calls == [["1", "2"]]


def test_steam_miss_expires_sooner_than_hit(monkeypatch):
    monkeypatch.setattr(config, "STEAM_API_KEY", "key")

    async def fetch(ids):
        return {"1": {"avatar_url": "a"}}

    monkeypatch.setattr(steam, "_fetch_profiles", fetch)
    asyncio.run(steam.fetch_profiles(["1", "2"]))
    assert steam._profile_cache["1"][0] > steam._profile_cache["2"][0]


def test_static_files_are_gzipped(client):
    response = client.get("/static/panel.render.js", headers={"Accept-Encoding": "gzip"})
    assert response.headers["content-encoding"] == "gzip"
    assert len(gzip.compress(response.content)) < 200_000


def test_versioned_assets_are_cached_for_a_year(client):
    response = client.get("/static/app.js?v=abc")
    assert "immutable" in response.headers["cache-control"]
    assert "immutable" not in client.get("/static/app.js").headers.get("cache-control", "")


@pytest.mark.parametrize("path,name", [("/panel", "panel.html"), ("/", "index.html")])
def test_pages_drop_compiled_template_and_version_assets_by_content(client, path, name):
    page = client.get(path)
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert "WARDOGS_APP_TEMPLATE_START" not in page.text
    assert 'id="app"' in page.text
    assert len(page.content) < 12_000
    assert "/static/app.js?v=" in page.text
    assert "20261104" not in page.text


def test_asset_version_follows_file_content(tmp_path, monkeypatch):
    (tmp_path / "x.js").write_text("one", encoding="utf-8")
    (tmp_path / "page.html").write_text('<script src="/static/x.js?v=old"></script>', encoding="utf-8")
    monkeypatch.setattr(main, "STATIC_DIR", tmp_path)
    main._asset_hashes.clear()
    first = main._page("page.html").body.decode()
    assert "v=old" not in first
    (tmp_path / "x.js").write_text("two!", encoding="utf-8")
    second = main._page("page.html").body.decode()
    assert first != second
    main._asset_hashes.clear()
    main._page_sources.clear()

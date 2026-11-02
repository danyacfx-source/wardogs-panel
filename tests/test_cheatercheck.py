import asyncio

import aiohttp
import pytest
from fastapi.testclient import TestClient

from app import api, cheatercheck, config
from app.main import app

STEAM_ID = "76561190000000021"

RAW_OK = {
    "steamId64": STEAM_ID,
    "status": "ok",
    "profileUrl": "https://cheatercheck.io/player/76561190000000021",
    "cheatScore": {"score": 42},
    "bans": {"vacBanned": True, "vacBans": 2, "gameBans": 1, "economyBan": "none"},
    "reports": {"total": 5, "last30Days": {"total": 3}},
    "scannedAt": "2026-11-01T10:00:00Z",
}


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch):
    cheatercheck._cache.clear()
    monkeypatch.setattr(config, "CHEATERCHECK_API_KEY", "cc_live_test")
    yield
    cheatercheck._cache.clear()


def _fake_get(monkeypatch, responses):
    """responses: список (статус, тело) или исключение; возвращает список вызовов."""
    calls = []

    async def fake(steam_id, key):
        calls.append((steam_id, key))
        item = responses[min(len(calls), len(responses)) - 1]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(cheatercheck, "_get", fake)
    return calls


def test_normalize_keeps_only_card_fields():
    player = cheatercheck._normalize(STEAM_ID, RAW_OK)
    assert player == {
        "steam_id": STEAM_ID,
        "status": "ok",
        "score": 42,
        "scanned_at": "2026-11-01T10:00:00Z",
        "vac_bans": 2,
        "game_bans": 1,
        "economy_ban": "none",
        "reports_total": 5,
        "reports_30d": 3,
        "profile_url": "https://cheatercheck.io/player/76561190000000021",
    }


def test_normalize_tolerates_missing_and_bad_values():
    player = cheatercheck._normalize(STEAM_ID, {"cheatScore": {"score": "high"}, "bans": None})
    assert player["score"] is None
    assert player["vac_bans"] == 0
    assert player["reports_total"] == 0
    assert player["profile_url"] == cheatercheck.SITE_URL


def test_without_key_no_request_is_made(monkeypatch):
    monkeypatch.setattr(config, "CHEATERCHECK_API_KEY", "")
    calls = _fake_get(monkeypatch, [(200, RAW_OK)])
    result = asyncio.run(cheatercheck.fetch_player(STEAM_ID))
    assert result == {"configured": False, "reason": "not_configured", "player": None}
    assert calls == []


def test_successful_lookup_is_cached(monkeypatch):
    calls = _fake_get(monkeypatch, [(200, RAW_OK)])

    async def run():
        first = await cheatercheck.fetch_player(STEAM_ID)
        second = await cheatercheck.fetch_player(STEAM_ID)
        return first, second

    first, second = asyncio.run(run())
    assert first == second
    assert first["player"]["score"] == 42
    assert len(calls) == 1


def test_unknown_player_is_cached_as_not_found(monkeypatch):
    calls = _fake_get(monkeypatch, [(404, None)])

    async def run():
        return await cheatercheck.fetch_player(STEAM_ID), await cheatercheck.fetch_player(STEAM_ID)

    first, _ = asyncio.run(run())
    assert first == {"configured": True, "reason": "not_found", "player": None}
    assert len(calls) == 1


def test_scanning_status_is_not_cached(monkeypatch):
    scanning = {**RAW_OK, "status": "scanning"}
    calls = _fake_get(monkeypatch, [(200, scanning), (200, RAW_OK)])

    async def run():
        return await cheatercheck.fetch_player(STEAM_ID), await cheatercheck.fetch_player(STEAM_ID)

    first, second = asyncio.run(run())
    assert first["player"]["status"] == "scanning"
    assert second["player"]["status"] == "ok"
    assert len(calls) == 2


def test_errors_degrade_to_unavailable_and_are_not_cached(monkeypatch):
    calls = _fake_get(monkeypatch, [aiohttp.ClientError("boom"), (500, None), (200, RAW_OK)])

    async def run():
        return [await cheatercheck.fetch_player(STEAM_ID) for _ in range(3)]

    results = asyncio.run(run())
    assert [r["reason"] for r in results[:2]] == ["unavailable", "unavailable"]
    assert results[2]["player"]["score"] == 42
    assert len(calls) == 3


def test_endpoint_requires_permission():
    with TestClient(app) as client:
        response = client.get(f"/api/players/{STEAM_ID}/cheatercheck")
    assert response.status_code == 401


def test_endpoint_returns_unavailable_when_service_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "cc.db")
    monkeypatch.setattr(config, "DEV_AUTH", True)
    monkeypatch.setattr(api, "_dev_auth_allowed", lambda request: True)
    _fake_get(monkeypatch, [(503, None)])
    with TestClient(app) as client:
        client.post("/api/auth/dev")
        client.get("/api/session")
        response = client.get(f"/api/players/{STEAM_ID}/cheatercheck")
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "configured": True, "reason": "unavailable", "player": None}

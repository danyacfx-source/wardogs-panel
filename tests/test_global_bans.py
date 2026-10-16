from fastapi.testclient import TestClient

from app import api
from app.main import app

BANNED_STEAM_ID = "76561190000000001"


class FakeAPI:
    def __init__(self, sid, calls):
        self.sid = sid
        self.calls = calls

    async def request(self, method, path, json_body=None):
        self.calls.setdefault(self.sid, []).append((method, path, json_body))

    async def close(self):
        pass


class FakeServer:
    def __init__(self, sid, name, calls):
        self.id = sid
        self.name = name
        self.api = FakeAPI(sid, calls)


def _dev_login(client, monkeypatch):
    monkeypatch.setattr(api, "_dev_auth_allowed", lambda request: True)
    client.post("/api/auth/dev")
    client.get("/api/session")
    return {"X-CSRF-Token": client.cookies.get("wds_csrf") or ""}


def test_ban_and_unban_mirror_across_servers(monkeypatch, tmp_path):
    monkeypatch.setattr(api.config, "DB_PATH", tmp_path / "bans.db")
    monkeypatch.setattr(api.config, "DEV_AUTH", True)
    calls = {}
    with TestClient(app) as client:
        api.POOL.clear()
        api.POOL["ru1"] = FakeServer("ru1", "WARDOGS RU #1", calls)
        api.POOL["ru2"] = FakeServer("ru2", "WARDOGS RU #2", calls)
        headers = _dev_login(client, monkeypatch)

        # Бан на ru1 дублируется на ru2.
        response = client.post(
            "/api/server/ru1/bans",
            json={"steamId": BANNED_STEAM_ID, "reason": "mirror test"},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["mirrored"] == ["ru2"]
        payload = {"steamId": BANNED_STEAM_ID, "reason": "mirror test"}
        assert calls["ru1"] == [("POST", "/v1/bans", payload)]
        assert calls["ru2"] == [("POST", "/v1/bans", payload)]

        # Снятие с ru1 снимает бан и на ru2.
        response = client.delete(f"/api/server/ru1/bans/{BANNED_STEAM_ID}", headers=headers)
        assert response.status_code == 200, response.text
        assert calls["ru1"][-1] == ("DELETE", f"/v1/bans/{BANNED_STEAM_ID}", None)
        assert calls["ru2"][-1] == ("DELETE", f"/v1/bans/{BANNED_STEAM_ID}", None)

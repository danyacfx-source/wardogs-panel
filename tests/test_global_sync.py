from fastapi.testclient import TestClient

from app import api
from app.main import app

BANNED_STEAM_ID = "76561190000000001"
VIP_STEAM_ID = "76561190000000002"


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
        self.routes = [
            "GET /v1/status",
            "POST /v1/reserved-slots",
            "DELETE /v1/reserved-slots/{steamId}",
        ]
        self.config_writable = False

    async def ensure_caps(self):
        pass


def _setup_pool(calls):
    api.POOL.clear()
    api.POOL["ru1"] = FakeServer("ru1", "WARDOGS RU #1", calls)
    api.POOL["ru2"] = FakeServer("ru2", "WARDOGS RU #2", calls)


def _dev_login(client, monkeypatch):
    monkeypatch.setattr(api, "_dev_auth_allowed", lambda request: True)
    client.post("/api/auth/dev")
    client.get("/api/session")
    return {"X-CSRF-Token": client.cookies.get("wds_csrf") or ""}


def _base(monkeypatch, tmp_path):
    monkeypatch.setattr(api.config, "DB_PATH", tmp_path / "panel.db")
    monkeypatch.setattr(api.config, "DEV_AUTH", True)


def test_ban_and_unban_mirror_across_servers(monkeypatch, tmp_path):
    _base(monkeypatch, tmp_path)
    calls = {}
    with TestClient(app) as client:
        _setup_pool(calls)
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


def test_vip_slot_grant_and_delete_mirror_across_servers(monkeypatch, tmp_path):
    _base(monkeypatch, tmp_path)
    calls = {}
    with TestClient(app) as client:
        _setup_pool(calls)
        headers = _dev_login(client, monkeypatch)

        # Выдача VIP на ru1 дублируется на ru2.
        response = client.post(
            "/api/server/ru1/vip-slots",
            json={"steam_id": VIP_STEAM_ID, "display_name": "Mirror VIP"},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["synced"] is True
        assert body["mirrored"] == ["ru2"]
        assert ("POST", "/v1/reserved-slots", {"steamId": VIP_STEAM_ID}) in calls["ru1"]
        assert ("POST", "/v1/reserved-slots", {"steamId": VIP_STEAM_ID}) in calls["ru2"]

        # Удаление VIP с ru1 снимает entitlement и на ru2.
        vip_id = body["item"]["id"]
        response = client.delete(f"/api/server/ru1/vip-slots/{vip_id}", headers=headers)
        assert response.status_code == 200, response.text
        assert calls["ru1"][-1] == ("DELETE", f"/v1/reserved-slots/{VIP_STEAM_ID}", None)
        assert calls["ru2"][-1] == ("DELETE", f"/v1/reserved-slots/{VIP_STEAM_ID}", None)

        # Записи зеркального сервера вычищены.
        mirrored = [
            item for item in client.get("/api/server/ru2/vip-slots").json()["items"]
            if item["steam_id"] == VIP_STEAM_ID
        ]
        assert mirrored == []

import asyncio

from fastapi.testclient import TestClient

from app import api, db
from app.main import app

BANNED_STEAM_ID = "76561190000000011"
STAFF_STEAM_ID = "76561190000000012"


class BansAPI:
    """Upstream, который после своего рестарта отдаёт бан без причины."""

    def __init__(self, calls):
        self.calls = calls

    async def bans(self):
        return {"ok": True, "count": 1, "bans": [{"steamId": BANNED_STEAM_ID, "reason": ""}]}

    async def request(self, method, path, json_body=None):
        self.calls.append((method, path, json_body))

    async def close(self):
        pass


class FakeServer:
    def __init__(self, sid, calls):
        self.id = sid
        self.name = f"WARDOGS {sid}"
        self.api = BansAPI(calls)


def _dev_login(client, monkeypatch):
    monkeypatch.setattr(api, "_dev_auth_allowed", lambda request: True)
    client.post("/api/auth/dev")
    client.get("/api/session")
    return {"X-CSRF-Token": client.cookies.get("wds_csrf") or ""}


def test_managed_ban_reason_is_shown_when_upstream_lost_it(monkeypatch, tmp_path):
    monkeypatch.setattr(api.config, "DB_PATH", tmp_path / "panel.db")
    monkeypatch.setattr(api.config, "DEV_AUTH", True)
    api.POOL.clear()
    api.POOL["ru1"] = FakeServer("ru1", [])
    with TestClient(app) as client:
        headers = _dev_login(client, monkeypatch)

        async def remember():
            await db.save_managed_ban("ru1", BANNED_STEAM_ID, "Игрок", "нарушение правил", None, STAFF_STEAM_ID)

        asyncio.run(remember())
        response = client.get("/api/server/ru1/bans", headers=headers)
        assert response.status_code == 200, response.text
        rows = response.json()["bans"]
        assert rows[0]["steamId"] == BANNED_STEAM_ID
        assert rows[0]["reason"] == "нарушение правил"


def test_retry_queue_resends_ban_reason_after_restart(monkeypatch, tmp_path):
    monkeypatch.setattr(api.config, "DB_PATH", tmp_path / "panel.db")
    calls = []
    api.POOL.clear()
    api.POOL["ru1"] = FakeServer("ru1", calls)

    async def run():
        await db.init()
        # Бан не дошёл до ru1 до перезапуска: намерение лежит в очереди в БД.
        await db.enqueue_remote_op("ban_add", "ru1", BANNED_STEAM_ID, {
            "reason": "нарушение правил", "display_name": "Игрок",
            "expires_utc": None, "created_by": STAFF_STEAM_ID,
        })
        await api.process_remote_ops()
        assert await db.list_due_remote_ops() == []

    asyncio.run(run())
    assert calls == [("POST", "/v1/bans", {"steamId": BANNED_STEAM_ID, "reason": "нарушение правил"})]

"""CRUD типов тикетов Discord-бота через панельный API."""
from fastapi.testclient import TestClient

from app.main import app
from app import api
from app.bot import database as bot_db


def _base(monkeypatch, tmp_path):
    monkeypatch.setattr(api.config, "DB_PATH", tmp_path / "panel.db")
    monkeypatch.setattr(api.config, "DEV_AUTH", True)
    monkeypatch.setattr(api, "_dev_auth_allowed", lambda request: True)

    async def _perm(request, perms):
        user = await api._current_user(request)
        if not user:
            raise api.HTTPException(status_code=401, detail="Нужен вход")
        return user

    monkeypatch.setattr(api, "_require_perm", _perm)
    monkeypatch.setattr(api, "_require_any_perm", _perm)

    bot_file = tmp_path / "bot.db"
    monkeypatch.setattr(bot_db, "DB_FILE", str(bot_file))
    if hasattr(bot_db, "_local"):
        try:
            bot_db._local.conn = None
        except Exception:
            pass
    bot_db.init_db()


def _login(client):
    client.post("/api/auth/dev")
    client.get("/api/session")
    return {"X-CSRF-Token": client.cookies.get("wds_csrf") or ""}


def test_ticket_types_crud(monkeypatch, tmp_path):
    _base(monkeypatch, tmp_path)
    with TestClient(app) as client:
        headers = _login(client)

        response = client.get("/api/ticket-types")
        assert response.status_code == 200, response.text
        defaults = {t["type_id"]: t for t in response.json()["types"]}
        assert {"complaint", "suggestion", "vip_bonuses", "clan"} <= set(defaults)
        assert "Магазина" in defaults["vip_bonuses"]["description"] or "магазина" in defaults["vip_bonuses"]["description"]
        assert "ЗАЯВКА НА РЕГИСТРАЦИЮ КЛАНА" in defaults["clan"]["description"]
        assert defaults["clan"]["label"] == "Зарегистрировать клан"

        long_desc = "Т" * 1990
        response = client.post("/api/ticket-types", json={
            "type_id": "support", "label": "Поддержка", "description": long_desc,
            "emoji": "🎧", "color": "#00AAFF", "position": 5, "enabled": True,
        }, headers=headers)
        assert response.status_code == 200, response.text
        created = response.json()["type"]
        assert created["label"] == "Поддержка"
        assert created["description"] == long_desc
        assert created["color"] == "#00AAFF"
        assert created["enabled"] is True

        response = client.post("/api/ticket-types", json={"type_id": "support", "label": "Дубль"}, headers=headers)
        assert response.status_code == 409

        response = client.post("/api/ticket-types", json={"type_id": "BAD ID!", "label": "X"}, headers=headers)
        assert response.status_code == 400

        response = client.patch("/api/ticket-types/support", json={
            "label": "Поддержка игроков", "enabled": False, "color": "#FF0000",
        }, headers=headers)
        assert response.status_code == 200, response.text
        updated = response.json()["type"]
        assert updated["label"] == "Поддержка игроков"
        assert updated["enabled"] is False
        assert updated["color"] == "#FF0000"
        assert updated["description"] == long_desc

        response = client.get("/api/ticket-types")
        visible = {t["type_id"] for t in response.json()["types"] if t["enabled"]}
        assert "support" not in visible

        response = client.patch("/api/ticket-types/missing", json={"label": "X"}, headers=headers)
        assert response.status_code == 404

        response = client.delete("/api/ticket-types/support", headers=headers)
        assert response.status_code == 200
        response = client.delete("/api/ticket-types/support", headers=headers)
        assert response.status_code == 404

        response = client.get("/api/ticket-types")
        assert "support" not in {t["type_id"] for t in response.json()["types"]}

from fastapi.testclient import TestClient

from app.api import _csv_response
from app.main import app


def test_unknown_server_returns_404():
    with TestClient(app) as client:
        response = client.get("/api/server/not-a-server/overview")
    assert response.status_code == 404


def test_rotation_is_not_public():
    with TestClient(app) as client:
        response = client.get("/api/server/ru1/rotation")
    # Без входа — 401: аноним не должен узнавать, какие эндпоинты
    # закрыты ролями (403 до авторизации — утечка карты прав).
    assert response.status_code == 401


def test_player_lists_are_not_public():
    with TestClient(app) as client:
        assert client.get("/api/server/ru1/players").status_code == 401
        assert client.get("/api/stats/players").status_code == 401


def test_audit_export_requires_permission():
    with TestClient(app) as client:
        response = client.get("/api/audit/export")
    assert response.status_code == 401


def test_csv_exports_escape_spreadsheet_formulas():
    response = _csv_response("audit.csv", ["detail"], [{"detail": "=HYPERLINK(\"https://evil.example\")"}])
    assert b"\'=HYPERLINK" in response.body

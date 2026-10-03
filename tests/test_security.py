import asyncio
import ipaddress
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import api, config, steam
from app.main import app


def test_client_ip_trusts_proxy_headers():
    # Приватный пир (reverse proxy) — берём реального клиента из X-Forwarded-For.
    assert api.client_ip({"x-forwarded-for": "203.0.113.7, 10.0.0.2"}, "10.0.0.1") == "203.0.113.7"
    # Публичный пир — поддельный заголовок игнорируется, берём пира.
    assert api.client_ip({"x-forwarded-for": "198.51.100.99"}, "198.51.100.1") == "198.51.100.1"
    # Приватные hop'ы внутри пропускаем, мусор не роняет проверку.
    assert api.client_ip({"x-forwarded-for": "garbage, 203.0.113.8"}, "172.16.0.5") == "203.0.113.8"
    # Заголовка нет — пир.
    assert api.client_ip({}, "172.16.0.5") == "172.16.0.5"
    # Нечитаемый пир возвращаем как есть.
    assert api.client_ip({}, "testclient") == "testclient"


def test_admin_ip_allowlist(monkeypatch):
    def request_factory(xff, peer):
        return SimpleNamespace(
            headers={"x-forwarded-for": xff} if xff else {},
            client=SimpleNamespace(host=peer),
        )

    monkeypatch.setattr(config, "ADMIN_IPS", [ipaddress.ip_network("203.0.113.0/24")])
    assert api.admin_ip_allowed(request_factory("203.0.113.4", "10.0.0.9")) is True
    assert api.admin_ip_allowed(request_factory("198.51.100.4", "10.0.0.9")) is False
    assert api.admin_ip_allowed(request_factory("", "10.0.0.9")) is False
    monkeypatch.setattr(config, "ADMIN_IPS", [])
    assert api.admin_ip_allowed(request_factory("198.51.100.4", "10.0.0.9")) is True


def test_admin_endpoints_reject_foreign_ip(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_IPS", [ipaddress.ip_network("203.0.113.0/24")])
    with TestClient(app) as client:
        assert client.get("/api/discord/overview").status_code == 403
        assert client.get("/api/roles").status_code == 403
        assert client.delete("/api/discord/bots/some-bot").status_code == 403


def test_health_is_available():
    with TestClient(app) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"]
    assert response.headers["cross-origin-opener-policy"] == "same-origin"
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert "'unsafe-eval'" not in response.headers["content-security-policy"]


def test_readiness_is_available():
    with TestClient(app) as client:
        response = client.get("/api/ready")
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["audit"]["ok"] is True


def test_session_sets_csrf_cookie():
    with TestClient(app) as client:
        response = client.get("/api/session")
    assert response.status_code == 200
    assert "wds_csrf" in response.cookies


def test_sensitive_server_data_is_not_public():
    with TestClient(app) as client:
        assert client.get("/api/server/ru1/bans").status_code == 401
        assert client.get("/api/server/ru1/catalog").status_code == 401
        assert client.get("/api/players/76561190000000001/notes").status_code == 401
        assert client.get("/api/clans").status_code == 401
        assert client.get("/api/server/ru1/vip-slots").status_code == 401
        assert client.get("/api/tickets").status_code == 401
        assert client.get('/api/vip/orders').status_code == 401
        assert client.post('/api/vip/orders', json={}).status_code == 401
        assert client.get("/api/discord/overview").status_code == 401
        assert client.get("/api/discord/bots").status_code == 401
        assert client.post("/api/discord/bots", json={"name": "x"}).status_code == 401
        assert client.delete("/api/discord/bots/some-bot").status_code == 401
        assert client.post("/api/bot/v1/heartbeat", json={}).status_code == 401
        assert client.get("/api/server/ru1/bans/export").status_code == 401
        assert client.get("/api/players/76561190000000001/history/export").status_code == 401
        assert client.get("/api/system/status").status_code == 401
        assert client.get("/api/roles/123/history").status_code == 401
        assert client.get("/api/roles/preview/user/76561190000000001").status_code == 401
        assert client.post("/api/discord/tempvoice/123/delete").status_code == 401
        assert client.patch("/api/server/ru1/bans/76561190000000001").status_code == 401
        assert client.post("/api/server/ru1/bans/bulk").status_code == 401


def test_unknown_host_is_rejected():
    with TestClient(app) as client:
        response = client.get("/api/health", headers={"host": "evil.example"})
    assert response.status_code == 400


def test_request_id_is_sanitized():
    with TestClient(app) as client:
        response = client.get("/api/health", headers={"X-Request-ID": "bad id\r\nX-Injected: yes"})
    assert "X-Injected" not in response.headers
    assert response.headers["x-request-id"]


def test_steam_openid_response_is_bound_to_callback_state():
    params = {
        "openid.mode": "id_res",
        "openid.op_endpoint": steam.OPENID_SERVER,
        "openid.claimed_id": "http://steamcommunity.com/openid/id/76561190000000000",
        "openid.return_to": "http://127.0.0.1:8236/api/auth/steam/callback?state=wrong",
    }
    assert asyncio.run(steam.validate_callback(params, expected_state="expected")) is None


def test_request_body_limit_is_enforced(monkeypatch):
    monkeypatch.setattr(config, "MAX_BODY_BYTES", 32)
    with TestClient(app) as client:
        response = client.post("/api/auth/dev", content=b"x" * 33)
    assert response.status_code == 413


def test_early_security_rejection_keeps_security_headers():
    with TestClient(app) as client:
        client.cookies.set(api.SESSION_COOKIE, "invalid-session")
        response = client.post("/api/auth/logout")
    assert response.status_code == 403
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"]

"""Уровни VIP: сохранение, личный тикет для Tier 2 и объявление о входе для Tier 3."""
import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from app import api, config, db, main
from app import discord as dmod
from app.main import app

TARGET = "76561190000000811"
STAFF = "76561190000000321"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "tiers.db")
    main._rate_windows.clear()
    api._vip_join_announced.clear()
    api._vip_join_roster.clear()
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


class _Rcon:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def request(self, method, path, json_body=None, **kwargs):
        if self.fail:
            raise api.RCONError(0, "timeout", "нет ответа")
        self.sent.append((method, path, json_body))
        return {}


def _server(fail=False, sid="ru1"):
    return type("Server", (), {"id": sid, "api": _Rcon(fail)})()


def _slot(tier, server="ru1", expires=None, steam_id=TARGET):
    return asyncio.run(db.admin_set_vip_slot(server, steam_id, "Игрок", expires, "", STAFF, tier))


def _no_sync(monkeypatch):
    async def synced(item, add=True):
        return True, None

    monkeypatch.setattr(api, "_sync_vip_record", synced)


def _save(client, headers, **body):
    return client.post("/api/server/ru1/vip-slots", headers=headers, json={"steam_id": TARGET, **body})


def test_tier_is_saved_on_every_server(client, monkeypatch):
    _no_sync(monkeypatch)
    headers = _sign_in(client, ["vip_edit", "vip_view"])
    response = _save(client, headers, tier=1)
    assert response.status_code == 200, response.text
    assert response.json()["item"]["tier"] == 1
    _save(client, headers, tier=3)
    for server in api.POOL:
        items = client.get(f"/api/server/{server}/vip-slots").json()["items"]
        assert [item["tier"] for item in items if item["steam_id"] == TARGET] == [3], server


def test_saving_without_tier_keeps_current_one(client, monkeypatch):
    _no_sync(monkeypatch)
    headers = _sign_in(client, ["vip_edit"])
    async def no_ticket(steam_id, tier):
        return {"ok": True, "created": False}
    monkeypatch.setattr(api, "_open_vip_ticket", no_ticket)
    _save(client, headers, tier=2)
    assert _save(client, headers, note="продление").json()["item"]["tier"] == 2


def test_purchase_does_not_change_tier(client):
    _slot(3)
    item = asyncio.run(db.save_vip_slot("ru1", TARGET, "Игрок", None, "покупка", STAFF))
    assert item["tier"] == 3


def test_new_purchase_starts_at_tier_one(client):
    assert asyncio.run(db.save_vip_slot("ru1", TARGET, "Игрок", None, "", STAFF))["tier"] == 1


@pytest.mark.parametrize("tier", [0, 4, "много"])
def test_bad_tier_is_rejected(client, tier):
    headers = _sign_in(client, ["vip_edit"])
    assert _save(client, headers, tier=tier).status_code == 400


def test_active_tier_ignores_expired_and_other_servers(client):
    _slot(3, expires=time.time() - 10)
    _slot(2, server="ru2")
    assert asyncio.run(db.get_active_vip_tier(TARGET)) == 2
    assert asyncio.run(db.get_active_vip_tier(TARGET, "ru1")) == 0
    assert asyncio.run(db.get_active_vip_tier("76561190000000999")) == 0


@pytest.mark.parametrize("tier,expected_calls", [(1, 0), (2, 1), (3, 1)])
def test_ticket_opens_only_from_tier_two(client, monkeypatch, tier, expected_calls):
    _no_sync(monkeypatch)
    headers = _sign_in(client, ["vip_edit"])
    asyncio.run(db.upsert_binding(TARGET, "555", "t", [], True))
    calls = []

    async def bridge(method, path, payload=None):
        calls.append((path, payload))
        return {"ok": True, "created": True, "channel_id": "9"}, ""

    monkeypatch.setattr(dmod, "bot_panel_request", bridge)
    body = _save(client, headers, tier=tier).json()
    assert len(calls) == expected_calls
    if expected_calls:
        assert calls[0] == ("/api/vip-ticket", {"discord_id": "555", "tier": tier})
        assert body["ticket"]["created"] is True
    else:
        assert body["ticket"] is None


def test_vip_is_kept_when_ticket_fails(client, monkeypatch):
    _no_sync(monkeypatch)
    headers = _sign_in(client, ["vip_edit"])
    response = _save(client, headers, tier=2)
    assert response.status_code == 200
    assert response.json()["item"]["tier"] == 2
    assert response.json()["ticket"] == {"ok": False, "error": "Игрок не привязал Discord"}


def test_retry_route_needs_tier_two_and_permission(client, monkeypatch):
    path = f"/api/players/{TARGET}/vip-ticket"
    assert client.post(path, json={}).status_code == 401
    headers = _sign_in(client, ["vip_view"])
    assert client.post(path, headers=headers, json={}).status_code == 403
    headers = _sign_in(client, ["vip_edit"])
    _slot(1)
    assert client.post(path, headers=headers, json={}).status_code == 409
    _slot(2)
    asyncio.run(db.upsert_binding(TARGET, "555", "t", [], True))

    async def bridge(method, api_path, payload=None):
        return {"ok": True, "created": True, "channel_id": "9"}, ""

    monkeypatch.setattr(dmod, "bot_panel_request", bridge)
    assert client.post(path, headers=headers, json={}).json()["ticket"]["created"] is True


def _announce(server, steam_id=TARGET, name="Медведь"):
    return asyncio.run(api.announce_vip_join(server, steam_id, name))


def test_tier_three_join_is_announced_in_game(client):
    _slot(3)
    server = _server()
    assert _announce(server) is True
    assert server.api.sent == [("POST", "/v1/broadcast", {"message": "Игрок с VIP Tier 3 Медведь зашёл на сервер"})]


@pytest.mark.parametrize("tier", [1, 2])
def test_lower_tiers_are_not_announced(client, tier):
    _slot(tier)
    server = _server()
    assert _announce(server) is False and server.api.sent == []


def test_expired_tier_three_is_not_announced(client):
    _slot(3, expires=time.time() - 10)
    server = _server()
    assert _announce(server) is False and server.api.sent == []


def test_reconnect_is_announced_once(client):
    _slot(3)
    server = _server()
    assert _announce(server) is True
    assert _announce(server) is False
    assert len(server.api.sent) == 1


def test_player_name_cannot_inject_lines(client):
    _slot(3)
    server = _server()
    _announce(server, name="Имя\nАдмин: всем бан" + "x" * 100)
    message = server.api.sent[0][2]["message"]
    assert "\n" not in message and len(message) < 90


def test_empty_template_disables_announcements(client, monkeypatch):
    _slot(3)
    monkeypatch.setattr(config, "VIP_JOIN_MESSAGE", "")
    server = _server()
    assert _announce(server) is False and server.api.sent == []


def test_rcon_failure_does_not_raise(client):
    _slot(3)
    assert _announce(_server(fail=True)) is False


def test_polling_fallback_skips_first_check_and_detects_joins(client):
    _slot(3)
    server = _server()
    roster = [{"steamId": TARGET, "name": "Медведь"}]
    asyncio.run(api.announce_vip_joins(server, roster))
    assert server.api.sent == []
    asyncio.run(api.announce_vip_joins(server, []))
    asyncio.run(api.announce_vip_joins(server, roster))
    assert len(server.api.sent) == 1


def test_feed_and_polling_do_not_announce_twice(client):
    _slot(3)
    server = _server()
    asyncio.run(api.announce_vip_joins(server, []))
    _announce(server)
    asyncio.run(api.announce_vip_joins(server, [{"steamId": TARGET, "name": "Медведь"}]))
    assert len(server.api.sent) == 1


def test_feed_event_triggers_announcement(client, monkeypatch):
    monkeypatch.setattr(config, "KILLFEED_TOKEN", "token")
    _slot(3)
    server = api.Server({"id": "ru1", "name": "Тест", "host": "127.0.0.1", "port": 1})
    rcon = _Rcon()
    monkeypatch.setattr(server.api, "request", rcon.request)
    monkeypatch.setitem(api.POOL, "ru1", server)
    event = {"type": "player_joined", "eventId": "j1", "eventTime": time.time(), "playerName": "Медведь",
             "playerSteamId": TARGET, "players": 5}
    response = client.post("/api/killfeed/ru1", headers={"Authorization": "Bearer token"}, json={"events": [event]})
    assert response.status_code == 200, response.text
    assert rcon.sent[0][1] == "/v1/broadcast"


def _ticket(paid_tier=0, paid_known=True):
    return {"discord_id": "555", "channel_id": "900", "paid_tier": paid_tier, "paid_known": paid_known}


def _sweep(monkeypatch, tickets, close_result=None):
    monkeypatch.setattr(config, "DISCORD_BOT_TOKEN", "token")
    closed = []

    async def bridge(method, path, payload=None):
        if path == "/api/vip-tickets":
            return tickets, ""
        closed.append(payload["channel_id"])
        return (close_result or {"ok": True, "closed": True}), ""

    monkeypatch.setattr(dmod, "bot_panel_request", bridge)
    return asyncio.run(api.close_lapsed_vip_tickets()), closed


def _bind():
    asyncio.run(db.upsert_binding(TARGET, "555", "t", [], True))


def test_expired_vip_closes_ticket_and_writes_audit(client, monkeypatch):
    _bind()
    _slot(2, expires=time.time() - 60)
    count, closed = _sweep(monkeypatch, [_ticket()])
    assert count == 1 and closed == ["900"]
    assert asyncio.run(db.get_site_audit(1))[0]["event"] == "vip.ticket.close"


def test_renewed_vip_keeps_ticket(client, monkeypatch):
    _bind()
    _slot(2, expires=time.time() + 3600)
    assert _sweep(monkeypatch, [_ticket()]) == (0, [])


def test_downgrade_to_tier_one_closes_ticket(client, monkeypatch):
    _bind()
    _slot(1)
    assert _sweep(monkeypatch, [_ticket()])[1] == ["900"]


def test_active_tier_on_another_server_keeps_ticket(client, monkeypatch):
    _bind()
    _slot(1, server="ru1")
    _slot(3, server="ru2")
    assert _sweep(monkeypatch, [_ticket()])[1] == []


def test_paid_vip_keeps_ticket(client, monkeypatch):
    _bind()
    assert _sweep(monkeypatch, [_ticket(paid_tier=2)])[1] == []


def test_paid_tier_one_does_not_keep_ticket(client, monkeypatch):
    _bind()
    assert _sweep(monkeypatch, [_ticket(paid_tier=1)])[1] == ["900"]


def test_unknown_payment_state_keeps_ticket(client, monkeypatch):
    _bind()
    assert _sweep(monkeypatch, [_ticket(paid_known=False)])[1] == []


def test_missing_binding_keeps_ticket(client, monkeypatch):
    assert _sweep(monkeypatch, [_ticket()])[1] == []


def test_bot_offline_does_nothing(client, monkeypatch):
    assert _sweep(monkeypatch, None) == (0, [])


def test_failed_close_is_not_counted_or_audited(client, monkeypatch):
    _bind()
    _slot(2, expires=time.time() - 60)
    before = len(asyncio.run(db.get_site_audit(50)))
    count, closed = _sweep(monkeypatch, [_ticket()], close_result={"ok": False, "error": "Транскрипт не сохранён"})
    assert count == 0 and closed == ["900"]
    assert len(asyncio.run(db.get_site_audit(50))) == before


def test_without_bot_token_sweep_is_skipped(client, monkeypatch):
    monkeypatch.setattr(config, "DISCORD_BOT_TOKEN", "")

    async def must_not_run(method, path, payload=None):
        raise AssertionError("без токена бот не вызывается")

    monkeypatch.setattr(dmod, "bot_panel_request", must_not_run)
    assert asyncio.run(api.close_lapsed_vip_tickets()) == 0

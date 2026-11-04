"""Цены VIP: редактор владельца, сайт, Discord и главное — оплата после смены цены."""
import asyncio
import base64
import hashlib
import hmac
import json
import pathlib
import sys
import time

import pytest
from fastapi.testclient import TestClient

from app import api, config, db, main, vip_catalog
from app import discord as dmod
from app.main import app

BOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "app" / "bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

discord = pytest.importorskip("discord")

from cogs import donation, seeding  # noqa: E402

OWNER = "76561190000000777"
DISCORD_ID = "123456789012345678"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "prices.db")
    monkeypatch.setattr(config, "OWNER_STEAM_IDS", {OWNER})
    main._rate_windows.clear()
    vip_catalog.reset()
    with TestClient(app) as test_client:
        yield test_client
    vip_catalog.reset()


def _sign_in(client, permissions=(), steam_id=OWNER, discord_id="777"):
    async def setup():
        await db.upsert_user(steam_id, "Владелец")
        await db.upsert_binding(steam_id, discord_id, "owner", [], True)
        await db.set_role_perms(f"user:{discord_id}", list(permissions))
        return await db.create_session(steam_id)

    client.cookies.set(api.SESSION_COOKIE, asyncio.run(setup()))
    client.get("/api/session")
    return {"X-CSRF-Token": client.cookies.get(api.CSRF_COOKIE) or ""}


def _no_bot(monkeypatch, result=None):
    calls = []

    async def bridge(method, path, payload=None):
        calls.append(path)
        return (result or {"ok": True}), ""

    monkeypatch.setattr(dmod, "bot_panel_request", bridge)
    return calls


def _set(client, headers, *items):
    return client.put("/api/dev/vip-prices", headers=headers, json={"items": list(items)})


def test_defaults_match_old_prices_and_new_tiers_are_off(client):
    prices = {row["id"]: (row["rub"], row["enabled"]) for row in vip_catalog.items()}
    assert prices["personal_1"] == (500, True)
    assert prices["personal_3"] == (1200, True)
    assert prices["personal_12"] == (4800, True)
    assert prices["clan_30"] == (12000, True)
    assert prices["personal_t2_1"] == (None, False)
    assert prices["personal_t3_12"] == (None, False)


def test_public_catalog_hides_disabled_tariffs(client):
    data = client.get("/api/public/vip-catalog").json()
    ids = {item["id"] for item in data["items"]}
    assert "personal_1" in ids and "personal_t2_1" not in ids
    assert "VIP" in data["perks"]["2"] or "тикет" in data["perks"]["2"]


@pytest.mark.parametrize("method", ["get", "put"])
def test_price_editor_is_owner_only(client, method):
    assert client.request(method, "/api/dev/vip-prices", json={} if method == "put" else None).status_code == 401
    headers = _sign_in(client, ["vip_edit", "config_edit", "roles"], steam_id="76561190000000321", discord_id="321")
    kwargs = {"headers": headers, "json": {"items": []}} if method == "put" else {}
    assert client.request(method, "/api/dev/vip-prices", **kwargs).status_code == 403


def test_saving_price_reaches_site_discord_and_audit(client, monkeypatch):
    calls = _no_bot(monkeypatch)
    headers = _sign_in(client)
    response = _set(client, headers, {"id": "personal_t2_1", "rub": 900, "enabled": True}, {"id": "personal_1", "rub": 700, "enabled": True})
    assert response.status_code == 200, response.text
    assert calls == ["/api/vip-panel-refresh"]
    assert response.json()["discord"] == {"ok": True}
    public = {item["id"]: item for item in client.get("/api/public/vip-catalog").json()["items"]}
    assert public["personal_t2_1"]["rub"] == 900
    assert public["personal_1"]["rub"] == 700
    assert vip_catalog.price_kopecks("personal_1") == 70000
    audit = client.get("/api/audit/site").status_code
    assert audit == 403  # у владельца без права audit журнал закрыт, запись проверяем в базе
    rows = asyncio.run(db.get_site_audit(5))
    assert rows[0]["event"] == "dev.vip_prices" and "personal_t2_1" in rows[0]["detail"]


def test_prices_survive_restart(client, monkeypatch):
    _no_bot(monkeypatch)
    headers = _sign_in(client)
    _set(client, headers, {"id": "personal_1", "rub": 777, "enabled": True})
    vip_catalog.reset()
    asyncio.run(vip_catalog.refresh(force=True))
    assert vip_catalog.price_kopecks("personal_1") == 77700


def test_price_is_saved_even_when_discord_fails(client, monkeypatch):
    async def bridge(method, path, payload=None):
        return None, "Бот ещё не запущен"

    monkeypatch.setattr(dmod, "bot_panel_request", bridge)
    headers = _sign_in(client)
    response = _set(client, headers, {"id": "personal_1", "rub": 650, "enabled": True})
    assert response.status_code == 200
    assert response.json()["discord"] == {"ok": False, "error": "Бот ещё не запущен"}
    assert vip_catalog.price_kopecks("personal_1") == 65000


@pytest.mark.parametrize("item,fragment", [
    ({"id": "nope", "rub": 1, "enabled": True}, "Неизвестный"),
    ({"id": "personal_1", "rub": 0, "enabled": True}, "целым"),
    ({"id": "personal_1", "rub": -5, "enabled": True}, "целым"),
    ({"id": "personal_1", "rub": 10.5, "enabled": True}, "целым"),
    ({"id": "personal_1", "rub": "500", "enabled": True}, "целым"),
    ({"id": "personal_1", "rub": 99999999, "enabled": True}, "целым"),
    ({"id": "personal_t2_1", "rub": None, "enabled": True}, "укажите цену"),
    ({"id": "personal_1", "rub": 500}, "включён"),
])
def test_bad_prices_are_rejected_and_nothing_changes(client, monkeypatch, item, fragment):
    calls = _no_bot(monkeypatch)
    headers = _sign_in(client)
    response = _set(client, headers, {"id": "personal_3", "rub": 1, "enabled": True}, item)
    assert response.status_code == 400
    assert fragment in response.json()["detail"]
    assert vip_catalog.price_kopecks("personal_3") == 120000
    assert calls == []


def test_disabling_tariff_stops_new_purchases(client, monkeypatch):
    _no_bot(monkeypatch)
    monkeypatch.setattr(config, "DONATE_CHECKOUT_SECRET", "secret")
    headers = _sign_in(client)
    asyncio.run(db.upsert_binding(OWNER, DISCORD_ID, "owner", [], True))
    assert client.post("/api/donate/checkout", headers=headers, json={"product": "personal_1"}).status_code == 200
    _set(client, headers, {"id": "personal_1", "rub": 500, "enabled": False})
    assert client.post("/api/donate/checkout", headers=headers, json={"product": "personal_1"}).status_code == 400


def _token_payload(url):
    token = url.split("o=")[1]
    base = token.split(".")[0]
    return json.loads(base64.urlsafe_b64decode(base + "=" * (-len(base) % 4)))


def test_checkout_uses_current_price_and_ignores_browser_amount(client, monkeypatch):
    _no_bot(monkeypatch)
    monkeypatch.setattr(config, "DONATE_CHECKOUT_SECRET", "secret")
    headers = _sign_in(client)
    asyncio.run(db.upsert_binding(OWNER, DISCORD_ID, "owner", [], True))
    _set(client, headers, {"id": "personal_t3_3", "rub": 2500, "enabled": True})
    response = client.post("/api/donate/checkout", headers=headers, json={"product": "personal_t3_3", "amount": 1, "amountKopecks": 1})
    assert response.status_code == 200, response.text
    order = _token_payload(response.json()["url"])
    assert order["amountKopecks"] == 250000 and order["productId"] == "personal_t3_3"


def _signed_event(product_id, amount, gross=None, secret="seed-secret"):
    now = int(time.time() * 1000)
    order = {
        "id": "a" * 32, "buyerId": DISCORD_ID, "kind": vip_catalog.PRODUCTS[product_id]["kind"],
        "productId": product_id, "recipients": [DISCORD_ID], "amountKopecks": amount,
        "createdAt": now, "expiresAt": now + 7_200_000,
    }
    base = base64.urlsafe_b64encode(json.dumps(order, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()
    return {
        "orderId": order["id"], "operationId": "op-1", "currency": "643",
        "grossKopecks": amount if gross is None else gross, "timestamp": int(time.time()),
        "orderToken": f"{base}.{signature}",
    }


def test_order_at_changed_price_still_grants_vip(client):
    """Главная защита: без неё игрок заплатил бы новую цену и не получил слот."""
    grant = seeding._validated_vip_event(_signed_event("personal_1", 77700), "seed-secret")
    assert grant is not None and grant["months"] == 1 and grant["tier"] == 1


def test_order_paid_at_old_price_after_price_change_is_valid(client, monkeypatch):
    _no_bot(monkeypatch)
    headers = _sign_in(client)
    event = _signed_event("personal_1", 50000)
    _set(client, headers, {"id": "personal_1", "rub": 999, "enabled": True})
    assert seeding._validated_vip_event(event, "seed-secret") is not None


def test_wrong_payment_amount_is_rejected():
    assert seeding._validated_vip_event(_signed_event("personal_1", 50000, gross=100), "seed-secret") is None


def test_tampered_order_is_rejected():
    event = _signed_event("personal_1", 50000)
    event["orderToken"] = event["orderToken"][:-1] + ("0" if event["orderToken"][-1] != "0" else "1")
    assert seeding._validated_vip_event(event, "seed-secret") is None


def test_tier_comes_from_product_and_reaches_state(monkeypatch):
    monkeypatch.setattr(seeding.config, "DONATION_CHECKOUT_SECRET", "seed-secret")
    monkeypatch.setattr(seeding, "seed_get_state", lambda key, default=None: None)
    service = seeding.DonationFeedService()
    assert service._apply(_signed_event("personal_t3_1", 150000)) is True
    assert service.state["vip"][DISCORD_ID]["tier"] == 3
    assert service.state["pendingTickets"] == [{"discordId": DISCORD_ID, "tier": 3}]


def test_cheaper_renewal_keeps_active_higher_tier(monkeypatch):
    monkeypatch.setattr(seeding.config, "DONATION_CHECKOUT_SECRET", "seed-secret")
    monkeypatch.setattr(seeding, "seed_get_state", lambda key, default=None: None)
    service = seeding.DonationFeedService()
    service.state["vip"][DISCORD_ID] = {"endsAt": int(time.time() * 1000) + 86_400_000, "orderId": "old", "tier": 3}
    service._apply(_signed_event("personal_1", 50000))
    assert service.state["vip"][DISCORD_ID]["tier"] == 3


def test_tier_one_purchase_opens_no_ticket(monkeypatch):
    monkeypatch.setattr(seeding.config, "DONATION_CHECKOUT_SECRET", "seed-secret")
    monkeypatch.setattr(seeding, "seed_get_state", lambda key, default=None: None)
    service = seeding.DonationFeedService()
    service._apply(_signed_event("personal_1", 50000))
    assert service.state["pendingTickets"] == []


def test_panel_order_applies_tier_and_opens_ticket(client, monkeypatch):
    _no_bot(monkeypatch)
    headers = _sign_in(client)
    _set(client, headers, {"id": "personal_t2_1", "rub": 900, "enabled": True})

    async def synced(item, add=True):
        return True, None

    monkeypatch.setattr(api, "_sync_vip_record", synced)
    opened = []

    async def ticket(steam_id, tier):
        opened.append((steam_id, tier))
        return {"ok": True, "created": True}

    monkeypatch.setattr(api, "_open_vip_ticket", ticket)
    target = "76561190000000444"
    order = {
        "id": "WD-TEST", "plan_id": "personal-t2-1", "months": 1, "server_id": "ru1", "recipients": [target],
        "actor_steam_id": OWNER,
    }
    results = asyncio.run(api._grant_vip_slots(order))
    assert results[0]["synced"] is True
    assert asyncio.run(db.get_active_vip_tier(target, "ru1")) == 2


def test_lower_plan_does_not_lower_existing_tier(client):
    asyncio.run(db.admin_set_vip_slot("ru1", OWNER, "x", None, "", OWNER, 3))
    asyncio.run(db.raise_vip_tier("ru1", OWNER, 2))
    assert asyncio.run(db.get_active_vip_tier(OWNER, "ru1")) == 3


def test_old_panel_plan_ids_still_resolve():
    assert vip_catalog.tier_of_plan("personal-1") == 1
    assert vip_catalog.tier_of_plan("clan-10") == 1
    assert vip_catalog.tier_of_plan("personal-t3-12") == 3
    assert vip_catalog.tier_of_plan("неизвестный") == 1


def _cog():
    cog = donation.DonationCog.__new__(donation.DonationCog)
    cog.site_base = ""
    return cog


def test_embed_lists_current_prices_and_only_enabled_tiers(client, monkeypatch):
    _no_bot(monkeypatch)
    headers = _sign_in(client)
    _set(client, headers, {"id": "personal_1", "rub": 777, "enabled": True}, {"id": "personal_t3_12", "rub": 9000, "enabled": True})
    fields = {field.name: field.value for field in _cog()._vip_embed().fields}
    assert "777" in fields["👤 VIP Tier 1"]
    assert "9 000" in fields["👤 VIP Tier 3"]
    assert "👤 VIP Tier 2" not in fields
    assert "4 500" in fields["🛡️ VIP для клана"]


def test_menus_skip_empty_groups(client, monkeypatch):
    _no_bot(monkeypatch)
    headers = _sign_in(client)
    _set(client, headers, *[
        {"id": product_id, "rub": 100, "enabled": False}
        for product_id, product in vip_catalog.PRODUCTS.items() if product["kind"] == "clan"
    ])

    async def build():
        return _cog()._vip_components()

    views = asyncio.run(build())
    assert [item.custom_id for view in views for item in view.children] == ["vip:personal"]
    values = [option.value for option in views[0].children[0].options]
    assert values == ["personal_1", "personal_3", "personal_12"]


class _Channel:
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return type("Message", (), {"id": 42})()


def test_refresh_updates_discord_message(client, monkeypatch):
    channel = _Channel()
    guild = type("Guild", (), {"get_channel": lambda self, channel_id: channel})()
    cog = _cog()
    cog.bot = type("Bot", (), {"get_guild": lambda self, guild_id: guild})()
    monkeypatch.setattr(donation, "CHANNEL_VIP", 5)
    monkeypatch.setattr(donation, "get_donation_meta", lambda key: None)
    monkeypatch.setattr(donation, "set_donation_meta", lambda key, value: None)
    monkeypatch.setattr(donation.DonationCog, "_as_single_view", staticmethod(lambda views: None))
    assert asyncio.run(cog.panel_refresh_vip()) == {"ok": True}
    assert "VIP Tier 1" in channel.sent[0]["embed"].fields[0].name


def test_refresh_reports_missing_channel(monkeypatch):
    cog = _cog()
    cog.bot = type("Bot", (), {"get_guild": lambda self, guild_id: object()})()
    monkeypatch.setattr(donation, "CHANNEL_VIP", 0)
    assert asyncio.run(cog.panel_refresh_vip()) == {"ok": False, "error": "Канал VIP не настроен"}


def test_support_page_has_no_hardcoded_prices():
    page = pathlib.Path("static/support.html").read_text(encoding="utf-8")
    assert "4 800" not in page and 'id="supportPlans"' in page
    assert 'data-price=' not in page

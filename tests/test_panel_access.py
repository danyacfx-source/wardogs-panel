"""Кто и к чему имеет доступ: кнопка панели, все маршруты API, allowlist IP, сбои Discord."""
import asyncio
import ipaddress
import pathlib
import re
import sqlite3
import time
from types import SimpleNamespace

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import api, config, db, main, maintenance, steam
from app import discord as dmod
from app.main import app

STEAM_ID = "76561190000000123"
DISCORD_ID = "123456789012345678"

# Открыты всем, в том числе анонимам.
PUBLIC_ROUTES = {
    "/api/health", "/api/ready", "/api/session", "/api/health/servers",
    "/api/server/{sid}/overview", "/api/stats/population",
    "/api/public/giveaways", "/api/public/clans-registry",
    "/api/public/clans", "/api/public/clans/{clan_id}", "/api/public/vip-catalog",
}
# Нужен только вход: личные данные и действия самого пользователя.
SIGNED_IN_ROUTES = {
    "/api/profile", "/api/seed/balance", "/api/seed/vip", "/api/donate/checkout",
    "/api/steam/persona", "/api/public/clan-applications",
}
AUTH_FLOW_PREFIX = "/api/auth/"


def _api_routes():
    # Маршруты подключённого роутера не видны в app.routes, поэтому берём их из самого роутера.
    for route in [*api.router.routes, *app.routes]:
        if not isinstance(route, APIRoute) or not route.path.startswith("/api/"):
            continue
        if route.path.startswith(AUTH_FLOW_PREFIX):
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            yield method, route.path


def _concrete(path):
    path = path.replace("{sid}", "ru1").replace("{server_id}", "ru1").replace("{steam_id}", STEAM_ID)
    return re.sub(r"\{[^}]+\}", "1", path)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "access.db")
    monkeypatch.setattr(config, "ADMIN_IPS", [])
    main._rate_windows.clear()
    api._touch_times.clear()
    api._clan_application_times.clear()
    with TestClient(app) as test_client:
        yield test_client


def _sign_in(client, permissions, steam_id=STEAM_ID, discord_id=DISCORD_ID):
    """Создаёт пользователя с ровно этими правами и возвращает заголовки с CSRF."""

    async def setup():
        await db.upsert_user(steam_id, "Тестовый игрок")
        await db.upsert_binding(steam_id, discord_id, "tester", [], True)
        await db.set_role_perms(f"user:{discord_id}", permissions)
        return await db.create_session(steam_id)

    client.cookies.set(api.SESSION_COOKIE, asyncio.run(setup()))
    client.get("/api/session")
    return {"X-CSRF-Token": client.cookies.get(api.CSRF_COOKIE) or ""}


def _call(client, method, path, headers=None):
    kwargs = {"headers": headers or {}}
    if method in {"POST", "PUT", "PATCH"}:
        kwargs["json"] = {}
    return client.request(method, _concrete(path), **kwargs)


def test_route_table_is_not_empty():
    assert len(list(_api_routes())) > 100


def test_anonymous_cannot_reach_protected_routes(client):
    failures = []
    for method, path in _api_routes():
        if path in PUBLIC_ROUTES or path.startswith("/api/bot/v1/"):
            continue
        status = _call(client, method, path).status_code
        if status < 300 or status >= 500 and status != 503:
            failures.append((method, path, status))
    assert failures == []


def test_user_without_permissions_cannot_reach_staff_routes(client):
    headers = _sign_in(client, [])
    failures = []
    for method, path in _api_routes():
        if path in PUBLIC_ROUTES or path in SIGNED_IN_ROUTES or path.startswith("/api/bot/v1/"):
            continue
        status = _call(client, method, path, headers).status_code
        if status < 300 or status >= 500 and status != 503:
            failures.append((method, path, status))
    assert failures == []


def test_panel_button_data_for_user_without_permissions(client):
    _sign_in(client, [])
    session = client.get("/api/session").json()
    assert session["authed"] is True
    assert session["me"]["permissions"] == []
    assert session["me"]["admin"] is False


def test_anonymous_session_has_no_permissions(client):
    session = client.get("/api/session").json()
    assert session["authed"] is False
    assert session["me"] is None


def test_single_permission_opens_only_that_permission(client):
    headers = _sign_in(client, ["tickets_view"])
    assert client.get("/api/session").json()["me"]["permissions"] == ["tickets_view"]
    assert client.get("/api/tickets").status_code == 200
    denied = [
        ("POST", f"/api/server/ru1/players/{STEAM_ID}/kick"),
        ("POST", "/api/server/ru1/bans"),
        ("POST", "/api/server/ru1/match/end"),
        ("GET", "/api/server/ru1/config"),
        ("GET", "/api/roles"),
        ("PUT", "/api/roles"),
        ("GET", "/api/audit/site"),
        ("POST", "/api/tickets"),
    ]
    for method, path in denied:
        response = client.request(method, path, headers=headers, json={} if method != "GET" else None)
        assert response.status_code == 403, (method, path, response.status_code)


def test_view_only_permission_cannot_post_to_discord_log(client):
    headers = _sign_in(client, ["config_view", "audit"])
    assert client.post("/api/discord/log-test", headers=headers).status_code == 403


def test_ip_allowlist_hides_panel_and_blocks_reads(client, monkeypatch):
    _sign_in(client, sorted(dmod.ALL_PERMS))
    monkeypatch.setattr(config, "ADMIN_IPS", [ipaddress.ip_network("203.0.113.0/24")])

    me = client.get("/api/session").json()["me"]
    assert me["permissions"] == []
    assert me["admin"] is False
    assert me["ip_restricted"] is True

    assert client.get("/api/server/ru1/players").status_code == 403
    assert client.get("/api/server/ru1/rotation").status_code == 403
    assert client.get("/api/stats/players").status_code == 403
    assert client.get("/api/roles").status_code == 403
    assert client.get("/api/health/servers").json()["detail"] == "недостаточно прав"


def test_ip_allowlist_strips_private_overview_fields(client, monkeypatch):
    _sign_in(client, sorted(dmod.ALL_PERMS))

    async def status():
        return {"map": "Bakurani", "players": {"current": 1, "max": 2}}

    async def players():
        return {"count": 1, "players": [{"steamId": STEAM_ID, "name": "x"}]}

    async def rotation():
        return {"entries": []}

    async def bans():
        return {"count": 0, "bans": []}

    async def health():
        return {"ok": True}

    async def capabilities():
        return {"routes": []}

    # Свой сервер: другие тесты подменяют записи общего POOL.
    server = api.Server({"id": "ru1", "name": "Тест", "host": "127.0.0.1", "port": 1})
    for name, func in {"status": status, "players": players, "rotation": rotation, "bans": bans,
                       "health": health, "capabilities": capabilities}.items():
        monkeypatch.setattr(server.api, name, func)
    monkeypatch.setitem(api.POOL, "ru1", server)
    api._cache.clear()

    allowed = client.get("/api/server/ru1/overview").json()
    assert allowed["players"]["count"] == 1
    assert allowed["rotation"] == {"entries": []}
    assert allowed["bans"] is not None

    monkeypatch.setattr(config, "ADMIN_IPS", [ipaddress.ip_network("203.0.113.0/24")])
    blocked = client.get("/api/server/ru1/overview").json()
    assert blocked["players"] is None
    assert blocked["players_visible"] is False
    assert blocked["rotation"] is None
    assert blocked["bans"] is None
    api._cache.clear()


def _dev_request(peer="127.0.0.1", headers=None):
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers=headers or {})


def test_dev_login_refuses_requests_through_a_proxy(monkeypatch):
    monkeypatch.setattr(config, "DEV_AUTH", True)
    assert api._dev_auth_allowed(_dev_request()) is True
    assert api._dev_auth_allowed(_dev_request(headers={"x-forwarded-for": "198.51.100.7"})) is False
    assert api._dev_auth_allowed(_dev_request(headers={"forwarded": "for=198.51.100.7"})) is False
    assert api._dev_auth_allowed(_dev_request(peer="198.51.100.7")) is False


def _steam_params(**overrides):
    claimed = f"https://steamcommunity.com/openid/id/{STEAM_ID}"
    params = {
        "openid.mode": "id_res",
        "openid.claimed_id": claimed,
        "openid.identity": claimed,
        "openid.op_endpoint": steam.OPENID_SERVER,
        "openid.return_to": f"{config.PUBLIC_URL}/api/auth/steam/callback?state=expected",
        "openid.signed": "signed,op_endpoint,claimed_id,identity,return_to,response_nonce,assoc_handle",
    }
    params.update(overrides)
    return params


@pytest.mark.parametrize("overrides", [
    {"openid.claimed_id": "https://evil.example/openid/id/76561190000000123"},
    {"openid.claimed_id": "https://steamcommunity.com/openid/id/x/76561190000000123"},
    {"openid.identity": "https://steamcommunity.com/openid/id/76561190000000999"},
    {"openid.signed": "op_endpoint,identity,return_to,response_nonce"},
    {"openid.signed": ""},
])
def test_steam_rejects_forged_identity_before_contacting_steam(overrides, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("Steam не должен вызываться для подделанного ответа")

    monkeypatch.setattr(steam.aiohttp, "ClientSession", fail)
    assert asyncio.run(steam.validate_callback(_steam_params(**overrides), expected_state="expected")) is None


def _stale_binding():
    return {
        "user_id": STEAM_ID,
        "discord_id": DISCORD_ID,
        "discord_name": "tester",
        "guild_member": True,
        "roles": ["10"],
        "updated_utc": time.time() - config.ROLES_TTL_SECONDS - 10,
    }


@pytest.fixture
def discord_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "discord.db")
    dmod._member_retry_at.clear()
    dmod._member_locks.clear()
    asyncio.run(db.init())

    async def no_roles():
        return {}

    monkeypatch.setattr(dmod, "_guild_roles", no_roles)
    yield
    dmod._member_retry_at.clear()
    dmod._member_locks.clear()


def test_discord_outage_fails_closed_and_backs_off(discord_state, monkeypatch):
    calls = []

    async def outage(discord_id):
        calls.append(discord_id)
        return None, False

    monkeypatch.setattr(dmod, "fetch_member", outage)

    async def run():
        binding = _stale_binding()
        await db.upsert_user(STEAM_ID, "x")
        first = await dmod.resolve_status(binding)
        second = await dmod.resolve_status(_stale_binding())
        return binding, first, second

    binding, first, second = asyncio.run(run())
    assert len(calls) == 1
    assert binding["roles"] == [] and binding["guild_member"] is False
    assert first["unverified"] is True and second["unverified"] is True
    assert first["admin"] is False


def test_concurrent_stale_requests_share_one_discord_call(discord_state, monkeypatch):
    calls = []

    async def member(discord_id):
        calls.append(discord_id)
        await asyncio.sleep(0.01)
        return ["10"], True

    monkeypatch.setattr(dmod, "fetch_member", member)

    async def run():
        await db.upsert_user(STEAM_ID, "x")
        return await asyncio.gather(*(dmod.resolve_status(_stale_binding()) for _ in range(5)))

    results = asyncio.run(run())
    assert len(calls) == 1
    assert all(result["unverified"] is False and result["member"] is True for result in results)


def test_user_who_left_the_guild_loses_role_permissions(discord_state, monkeypatch):
    async def gone(discord_id):
        return [], False

    monkeypatch.setattr(dmod, "fetch_member", gone)

    async def run():
        await db.upsert_user(STEAM_ID, "x")
        binding = _stale_binding()
        status = await dmod.resolve_status(binding)
        return binding, status

    binding, status = asyncio.run(run())
    assert status["member"] is False and status["unverified"] is False
    assert dmod.compute_permissions(binding, {"10": "Admin"}, {"Admin": ["kick"]}) == []


def test_status_memoized_and_last_seen_throttled_per_request(client, monkeypatch):
    _sign_in(client, ["players_view"])
    writes = []
    original = db.touch_user

    async def counting(steam_id):
        writes.append(steam_id)
        await original(steam_id)

    monkeypatch.setattr(db, "touch_user", counting)
    api._touch_times.clear()
    client.get("/api/session")
    client.get("/api/session")
    assert writes == [STEAM_ID]


def test_ticket_types_do_not_crash_without_bot_database(client, monkeypatch):
    _sign_in(client, ["tickets_view"])

    def missing(**kwargs):
        raise sqlite3.OperationalError("no such table: ticket_types")

    monkeypatch.setattr(api.bot_db, "list_ticket_types", missing)
    response = client.get("/api/ticket-types")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "types": [], "available": False}


def test_clan_application_uses_real_name_and_limits_repeats(client, monkeypatch):
    headers = _sign_in(client, [])
    delivered = []

    async def bridge(method, path, payload=None):
        delivered.append(payload)
        return {"ok": True}, ""

    monkeypatch.setattr(dmod, "bot_panel_request", bridge)
    body = {"name": "Волки Рубежа", "tag": "WLF", "description": "Описание клана для заявки на регистрацию."}
    first = client.post("/api/public/clan-applications", headers=headers, json=body)
    assert first.status_code == 200, first.text
    assert delivered[0]["leader"] == "Тестовый игрок"

    second = client.post("/api/public/clan-applications", headers=headers, json=body)
    assert second.status_code == 429
    assert len(delivered) == 1


OWNER_ROUTES = [
    ("GET", "/api/discord/overview"),
    ("GET", "/api/discord/bots"),
    ("POST", "/api/discord/bots"),
    ("PATCH", "/api/discord/bots/1"),
    ("POST", "/api/discord/bots/1/rotate-token"),
    ("DELETE", "/api/discord/bots/1"),
    ("GET", "/api/discord/bots/1/events"),
    ("POST", "/api/discord/tempvoice/1/delete"),
    ("POST", "/api/discord/tempvoice/1/transfer"),
    ("POST", "/api/discord/log-test"),
    ("GET", "/api/system/status"),
    ("GET", "/api/killfeed/setup"),
]


def test_owner_route_list_covers_every_discord_route():
    declared = {(method, _concrete(path)) for method, path in _api_routes() if path.startswith("/api/discord/")}
    assert declared <= set(OWNER_ROUTES)


def test_full_staff_permissions_do_not_open_owner_sections(client, monkeypatch):
    monkeypatch.setattr(config, "OWNER_STEAM_IDS", set())
    headers = _sign_in(client, sorted(dmod.ALL_PERMS))
    assert "owner" not in client.get("/api/session").json()["me"]["permissions"]
    for method, path in OWNER_ROUTES:
        response = client.request(method, path, headers=headers, json={} if method != "GET" else None)
        assert response.status_code == 403, (method, path, response.status_code)


def test_owner_permission_cannot_be_granted_from_the_panel(client, monkeypatch):
    monkeypatch.setattr(config, "OWNER_STEAM_IDS", set())
    headers = _sign_in(client, ["roles"])
    # Через API управления ролями: право отбрасывается ещё при сохранении.
    response = client.put(
        "/api/roles", headers=headers,
        json={"role_ref": f"user:{DISCORD_ID}", "perms": ["roles", "owner"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["perms"] == ["roles"]
    assert "owner" not in client.get("/api/roles").json()["all_permissions"]
    # Даже если запись попала в базу напрямую, она не действует.
    asyncio.run(db.set_role_perms(f"user:{DISCORD_ID}", ["roles", "owner"]))
    assert client.get("/api/session").json()["me"]["permissions"] == ["roles"]
    assert dmod.compute_permissions(
        {"discord_id": "1", "guild_member": True, "roles": ["10"]}, {"10": "Admin"}, {"Admin": ["owner"]},
    ) == []


def test_owner_gets_sections_without_any_roles(client, monkeypatch):
    monkeypatch.setattr(config, "OWNER_STEAM_IDS", {STEAM_ID})
    _sign_in(client, [])
    assert client.get("/api/session").json()["me"]["permissions"] == ["owner"]
    assert client.get("/api/discord/bots").status_code == 200
    assert client.get("/api/roles").status_code == 403


def test_owner_is_still_subject_to_ip_allowlist(client, monkeypatch):
    monkeypatch.setattr(config, "OWNER_STEAM_IDS", {STEAM_ID})
    _sign_in(client, [])
    monkeypatch.setattr(config, "ADMIN_IPS", [ipaddress.ip_network("203.0.113.0/24")])
    assert client.get("/api/discord/bots").status_code == 403
    assert client.get("/api/session").json()["me"]["permissions"] == []


@pytest.fixture
def maintenance_reset(client):
    maintenance.reset_cache()
    yield
    maintenance.reset_cache()


def _close(client, headers, section, message="Чиним баны", **extra):
    response = client.put(
        f"/api/dev/sections/{section}", headers=headers, json={"closed": True, "message": message, **extra},
    )
    assert response.status_code == 200, response.text


def test_dev_routes_are_owner_only(client, maintenance_reset, monkeypatch):
    monkeypatch.setattr(config, "OWNER_STEAM_IDS", set())
    headers = _sign_in(client, sorted(dmod.ALL_PERMS))
    assert client.get("/api/dev/sections").status_code == 403
    assert client.put("/api/dev/sections/bans", headers=headers, json={"closed": True}).status_code == 403
    assert client.get("/api/maintenance").json() == {"ok": True, "closed": {}}


def test_closed_section_blocks_staff_with_owner_text(client, maintenance_reset, monkeypatch):
    monkeypatch.setattr(config, "OWNER_STEAM_IDS", {"76561190000000777"})
    owner_headers = _sign_in(client, ["tickets_view"], steam_id="76561190000000777", discord_id="777")
    _close(client, owner_headers, "tickets", message="Чиним тикеты")
    assert client.get("/api/tickets").status_code == 200  # владелец работает как обычно

    headers = _sign_in(client, sorted(dmod.ALL_PERMS))
    assert client.get("/api/maintenance").json()["closed"]["tickets"]["message"] == "Чиним тикеты"
    response = client.get("/api/tickets")
    assert response.status_code == 503
    assert response.json()["error"] == {"code": "maintenance", "message": "Чиним тикеты"}
    assert client.post("/api/tickets", headers=headers, json={}).status_code == 503
    assert client.get("/api/roles").status_code == 200  # остальные разделы не задеты


def test_reopening_section_restores_access(client, maintenance_reset, monkeypatch):
    monkeypatch.setattr(config, "OWNER_STEAM_IDS", {"76561190000000777"})
    owner_headers = _sign_in(client, [], steam_id="76561190000000777", discord_id="777")
    _close(client, owner_headers, "tickets")
    assert client.put("/api/dev/sections/tickets", headers=owner_headers, json={"closed": False}).status_code == 200
    assert client.get("/api/maintenance").json()["closed"] == {}
    _sign_in(client, ["tickets_view"])
    assert client.get("/api/tickets").status_code == 200


def test_closed_state_survives_restart(client, maintenance_reset, monkeypatch):
    monkeypatch.setattr(config, "OWNER_STEAM_IDS", {STEAM_ID})
    headers = _sign_in(client, [])
    _close(client, headers, "bans", message="Текст", until_utc=1893456000)
    maintenance.reset_cache()
    entry = client.get("/api/maintenance").json()["closed"]["bans"]
    assert entry["message"] == "Текст" and entry["until_utc"] == 1893456000


def test_closed_site_page_shows_escaped_text_and_owner_passes(client, maintenance_reset, monkeypatch):
    monkeypatch.setattr(config, "OWNER_STEAM_IDS", {STEAM_ID})
    headers = _sign_in(client, [])
    _close(client, headers, "site_stats", message="<script>alert(1)</script> Скоро вернёмся")
    assert client.get("/stats").status_code == 200

    client.cookies.delete(api.SESSION_COOKIE)
    for path in ("/stats", "/stats.html", "/static/stats.html"):
        page = client.get(path)
        assert page.status_code == 503, path
        assert "Скоро вернёмся" in page.text
        assert "<script>" not in page.text
    assert client.get("/").status_code == 200


@pytest.mark.parametrize("path", ["/api/killfeed/ru1", "/api/public/clan-sync", "/api/bot/v1/events"])
def test_maintenance_never_blocks_machine_ingest(path, client, maintenance_reset, monkeypatch):
    monkeypatch.setattr(config, "OWNER_STEAM_IDS", {STEAM_ID})
    headers = _sign_in(client, [])
    for section in maintenance.SECTIONS_BY_ID:
        _close(client, headers, section)
    client.cookies.delete(api.SESSION_COOKIE)
    response = client.post(path, json={})
    assert not (response.status_code == 503 and "maintenance" in response.text)


def test_dev_rejects_bad_input(client, maintenance_reset, monkeypatch):
    monkeypatch.setattr(config, "OWNER_STEAM_IDS", {STEAM_ID})
    headers = _sign_in(client, [])
    assert client.put("/api/dev/sections/nope", headers=headers, json={"closed": True}).status_code == 404
    assert client.put("/api/dev/sections/bans", headers=headers, json={}).status_code == 400
    too_long = {"closed": True, "message": "x" * 1001}
    assert client.put("/api/dev/sections/bans", headers=headers, json=too_long).status_code == 400
    bad_time = {"closed": True, "until_utc": "завтра"}
    assert client.put("/api/dev/sections/bans", headers=headers, json=bad_time).status_code == 400


def test_every_closable_panel_section_exists_as_a_tab():
    source = pathlib.Path("static/app.js").read_text(encoding="utf-8")
    for section in maintenance.SECTIONS:
        if section["group"] == "panel":
            assert f'id: "{section["id"]}"' in source, section["id"]


def _close_everything(client, monkeypatch):
    monkeypatch.setattr(config, "OWNER_STEAM_IDS", {"76561190000000777"})
    headers = _sign_in(client, [], steam_id="76561190000000777", discord_id="777")
    for section in maintenance.SECTIONS_BY_ID:
        _close(client, headers, section, message="Техработы")
    client.cookies.delete(api.SESSION_COOKIE)


# Боты, платежи, хостинг и служебные файлы должны работать при всех закрытых разделах.
MACHINE_REQUESTS = [
    ("GET", "/api/health"), ("GET", "/api/ready"), ("GET", "/api/session"),
    ("GET", "/api/auth/steam/start"), ("POST", "/api/auth/logout"),
    ("POST", "/api/killfeed/ru1"), ("POST", "/api/public/clan-sync"),
    ("POST", "/api/bot/v1/heartbeat"), ("POST", "/api/bot/v1/events"),
    ("GET", "/api/public/clans-registry"), ("GET", "/api/public/giveaways"),
    ("POST", "/donate/webhook.php"), ("GET", "/donate/feed.php"),
    ("GET", "/donate/index.php"), ("GET", "/donate/success"),
    ("GET", "/panel"), ("GET", "/static/style.css"), ("GET", "/static/app.js"),
    ("GET", "/static/public-shell.js"),
]


def test_all_sections_closed_never_touches_machine_routes(client, maintenance_reset, monkeypatch):
    _close_everything(client, monkeypatch)
    for method, path in MACHINE_REQUESTS:
        response = client.request(method, path, follow_redirects=False)
        assert response.status_code != 503 or "maintenance" not in response.text, (method, path)
        assert 'class="maintenance-page"' not in response.text, (method, path)
    assert client.get("/api/health").status_code == 200


@pytest.mark.parametrize("path", [
    "/", "/index.html", "/static/index.html", "/stats", "/stats.html", "/static/stats.html",
    "/activity", "/activity/top", "/clans", "/clans.html", "/support", "/calc", "/static/calc.html",
    "/community", "/community.html", "/static/community.html",
    "/static/calc-app/index.html", "/calc-app/index.html", "/calc-assets/anything",
])
def test_every_public_page_address_is_closed(path, client, maintenance_reset, monkeypatch):
    _close_everything(client, monkeypatch)
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 503, path
    assert "ТЕХНИЧЕСКИЕ РАБОТЫ" in response.text


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/server/ru1/overview"), ("GET", "/api/stats/population"),
    ("GET", "/api/public/clans"), ("GET", "/api/public/clans/1"),
    ("POST", "/api/public/clan-applications"), ("POST", "/api/donate/checkout"),
    ("GET", "/api/public/vip-catalog"),
])
def test_public_data_is_closed_for_visitors(method, path, client, maintenance_reset, monkeypatch):
    _close_everything(client, monkeypatch)
    response = client.request(method, path, json={} if method == "POST" else None)
    assert response.status_code == 503, path
    assert response.json()["error"]["code"] == "maintenance"


def test_signed_in_user_without_permissions_is_still_blocked_from_public_data(client, maintenance_reset, monkeypatch):
    _close_everything(client, monkeypatch)
    _sign_in(client, [])
    assert client.get("/api/server/ru1/overview").status_code == 503


def test_staff_keep_monitoring_data_when_home_is_closed(client, maintenance_reset, monkeypatch):
    _close_everything(client, monkeypatch)
    # Закрыт только сайт: панель продолжает работать на тех же данных.
    owner_headers = _sign_in(client, [], steam_id="76561190000000777", discord_id="777")
    for section in maintenance.SECTIONS:
        if section["group"] == "panel":
            client.put(f"/api/dev/sections/{section['id']}", headers=owner_headers, json={"closed": False})
    server = api.Server({"id": "ru1", "name": "Тест", "host": "127.0.0.1", "port": 1})

    async def status():
        return {"map": "Bakurani", "players": {"current": 0, "max": 2}}

    async def empty():
        return {}

    monkeypatch.setattr(server.api, "status", status)
    for name in ("players", "rotation", "bans", "health", "capabilities"):
        monkeypatch.setattr(server.api, name, empty)
    monkeypatch.setitem(api.POOL, "ru1", server)
    api._cache.clear()
    _sign_in(client, ["tickets_view"])
    overview = client.get("/api/server/ru1/overview")
    assert overview.status_code == 200
    assert overview.json()["status"]["map"] == "Bakurani"
    api._cache.clear()
    assert client.get("/api/tickets").status_code == 200
    home = client.get("/")
    assert home.status_code == 503
    assert 'href="/panel"' in home.text
    assert "/api/auth/steam/start" not in home.text


def test_closed_home_keeps_a_way_in_for_staff(client, maintenance_reset, monkeypatch):
    _close_everything(client, monkeypatch)
    anonymous = client.get("/")
    assert "/api/auth/steam/start" in anonymous.text
    assert 'href="/panel"' not in anonymous.text
    # Вход доступен, а владелец после него видит сайт как обычно.
    assert client.get("/api/auth/steam/start", follow_redirects=False).status_code in {302, 307}
    _sign_in(client, [], steam_id="76561190000000777", discord_id="777")
    assert client.get("/").status_code == 200
    assert client.get("/stats").status_code == 200
    assert client.get("/api/stats/population").status_code == 200


def test_other_closed_page_links_home_not_login(client, maintenance_reset, monkeypatch):
    _close_everything(client, monkeypatch)
    page = client.get("/stats")
    assert 'href="/"' in page.text
    assert "/api/auth/steam/start" not in page.text


SITE_NAV_PAGES = ["index", "stats", "calc", "activity", "clans", "community", "support"]


def test_community_is_a_public_page(client, maintenance_reset):
    page = client.get("/community")
    assert page.status_code == 200
    assert "Как попасть на сервер" in page.text
    assert "https://discord.gg/wdru-ru" in page.text


@pytest.mark.parametrize("name", SITE_NAV_PAGES)
def test_navigation_links_to_community_page_not_anchor(name):
    source = pathlib.Path(f"static/{name}.html").read_text(encoding="utf-8")
    assert 'href="/community"' in source
    assert "#community" not in source


def test_community_blocks_moved_off_the_home_page():
    source = pathlib.Path("static/index.html").read_text(encoding="utf-8")
    assert "how-to-join" not in source and "portal-welcome" not in source


def test_each_site_section_closes_on_its_own(client, maintenance_reset, monkeypatch):
    monkeypatch.setattr(config, "OWNER_STEAM_IDS", {"76561190000000777"})
    pages = {
        "site_home": "/", "site_stats": "/stats", "site_calc": "/calc", "site_activity": "/activity",
        "site_clans": "/clans", "site_community": "/community", "site_support": "/support",
    }
    assert set(pages) == {section["id"] for section in maintenance.SECTIONS if section["group"] == "site"}
    for closed_id, closed_path in pages.items():
        headers = _sign_in(client, [], steam_id="76561190000000777", discord_id="777")
        _close(client, headers, closed_id, message="Техработы")
        client.cookies.delete(api.SESSION_COOKIE)
        for section_id, path in pages.items():
            expected = 503 if section_id == closed_id else 200
            assert client.get(path).status_code == expected, (closed_id, path)
        headers = _sign_in(client, [], steam_id="76561190000000777", discord_id="777")
        client.put(f"/api/dev/sections/{closed_id}", headers=headers, json={"closed": False})
        client.cookies.delete(api.SESSION_COOKIE)

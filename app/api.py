"""REST API сайта WARDOGS: авторизация, роли, мониторинг и управление.

Чтение доступно анонимно (мониторинг), админ-действия — только тем,
кто вошёл через Steam, привязал Discord и имеет роль из списка админов.
"""

import asyncio
import csv
import base64
import hashlib
import hmac
import ipaddress
import io
import json
import logging
import re
import secrets
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

try:
    from . import config, db
    from . import discord as dmod
    from . import steam as steam_mod
    from .rcon_client import RCONError, WardogsRCON
except ImportError:
    import config
    import db
    import discord as dmod
    import steam as steam_mod
    from rcon_client import RCONError, WardogsRCON

log = logging.getLogger("api")

router = APIRouter(prefix="/api")

# A draft order never grants access: only a future verified provider receipt may do so.
VIP_PLANS = [
    {"id":"personal-1","name":"Личный · 1 месяц","amount":500,"months":1,"seats":1},
    {"id":"personal-3","name":"Личный · 3 месяца","amount":1200,"months":3,"seats":1},
    {"id":"personal-12","name":"Личный · 12 месяцев","amount":4800,"months":12,"seats":1},
    {"id":"clan-10","name":"Клан · 10 участников","amount":4500,"months":1,"seats":10},
    {"id":"clan-20","name":"Клан · 20 участников","amount":8500,"months":1,"seats":20},
    {"id":"clan-30","name":"Клан · 30 участников","amount":12000,"months":1,"seats":30},
]


@router.get('/vip/orders')
async def vip_orders(request: Request):
    await _require_any_perm(request, {'vip_view','vip_edit'})
    return {'ok':True,'plans':VIP_PLANS,'items':await db.list_vip_orders(),'provider_connected':False}


@router.post('/vip/orders')
async def vip_order_create(request: Request):
    user = await _require_perm(request, 'vip_edit')
    body = await _json_object(request)
    plan = next((p for p in VIP_PLANS if p['id'] == body.get('plan_id')), None)
    buyer = str(body.get('buyer_steam_id') or '')
    sid = str(body.get('server_id') or '')
    recipients = body.get('recipients', [])
    if not plan or not STEAM_ID_RE.fullmatch(buyer) or sid not in POOL:
        raise HTTPException(400, 'Проверьте тариф, покупателя и сервер')
    if not isinstance(recipients,list) or len(recipients)>plan['seats'] or any(not isinstance(s,str) or not STEAM_ID_RE.fullmatch(s) for s in recipients) or len(set(recipients))!=len(recipients):
        raise HTTPException(400, 'Состав должен содержать уникальные SteamID64 в пределах тарифа')
    if not recipients and plan['seats']==1:
        recipients=[buyer]
    order={'id':'WD-'+secrets.token_hex(8).upper(),'buyer_steam_id':buyer,'server_id':sid,'plan_id':plan['id'],'amount':plan['amount'],'months':plan['months'],'seats':plan['seats'],'recipients':recipients,'status':'awaiting_payment','created_utc':time.time(),'actor_steam_id':user['steam_id']}
    await db.create_vip_order(order)
    await db.log_site_audit(user['steam_id'],sid,'vip.order.create',order['id'])
    return {'ok':True,'item':order}


@router.post('/vip/orders/{order_id}/cancel')
async def vip_order_cancel(order_id: str, request: Request):
    user = await _require_perm(request,'vip_edit')
    if not await db.cancel_vip_order(order_id):
        raise HTTPException(409, 'Заявка уже закрыта или не найдена')
    await db.log_site_audit(user['steam_id'],None,'vip.order.cancel',order_id)
    return {'ok':True}

SESSION_COOKIE = "__Host-wds_session" if config.COOKIE_SECURE else "wds_session"
STEAM_STATE_COOKIE = "wds_steam_state"
DISCORD_STATE_COOKIE = "wds_discord_state"
DEV_STEAM_ID = "76561190000000000"
STEAM_ID_RE = re.compile(r"^\d{17}$")
BOT_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{2,47}$")
BOT_EVENT_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,79}$")
BOT_SCOPES = {
    "heartbeat", "events.write", "stats.write", "bindings.read", "bindings.write",
    "tickets.read", "tickets.write", "moderation.write", "voice.manage", "commands.read",
}


def _set_short_cookie(response, name, value):
    response.set_cookie(
        name,
        value,
        max_age=600,
        httponly=True,
        secure=config.COOKIE_SECURE,
        samesite="lax",
        path="/api/auth/",
    )


def _set_session_cookie(response, token, max_age=None):
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age or config.SESSION_TTL_DAYS * 86400,
        httponly=True,
        secure=config.COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


def _valid_state(request, cookie_name):
    expected = request.cookies.get(cookie_name) or ""
    received = request.query_params.get("state") or ""
    return bool(expected and received and hmac.compare_digest(expected, received))


def _dev_auth_allowed(request: Request):
    if not config.DEV_AUTH or not request.client:
        return False
    try:
        return ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        return False

# ---------- RCON-пул ----------

class Server:
    def __init__(self, cfg):
        self.id = cfg["id"]
        self.name = cfg["name"]
        self.connect_url = cfg.get("connect_url") or ""
        self.api = WardogsRCON(cfg)
        self.routes = set()
        self.config_writable = False
        self.caps_ok = False
        self._caps_checked_at = 0.0
        self._caps_lock = asyncio.Lock()

    async def ensure_caps(self):
        now = time.monotonic()
        if self.caps_ok and now - self._caps_checked_at < 300:
            return
        async with self._caps_lock:
            now = time.monotonic()
            if self.caps_ok and now - self._caps_checked_at < 300:
                return
            try:
                caps = await self.api.capabilities()
                self.routes = {
                    str(route)[:200]
                    for route in (caps.get("routes") or [])
                    if isinstance(route, str)
                }
                self.config_writable = bool((caps.get("config") or {}).get("writable"))
                self._caps_checked_at = time.monotonic()
                self.caps_ok = True
            except RCONError:
                self.routes = set()
                self.config_writable = False
                # Retry a failed capability probe after a short outage.
                self._caps_checked_at = time.monotonic() - 270
                self.caps_ok = True


POOL = {cfg["id"]: Server(cfg) for cfg in config.SERVERS}
CACHE_TTL = 3.0
_cache = {}
_cache_lock = asyncio.Lock()
_cache_key_locks = defaultdict(asyncio.Lock)
_monitor_buckets = {}
_recent_actions = {}
_recent_actions_lock = asyncio.Lock()
_service_states = {}
_service_history = []


def _record_service_state(service, ok, detail=""):
    previous = _service_states.get(service)
    current = {"ok": bool(ok), "detail": str(detail or "")[:300]}
    if previous != current:
        event = {"service": service, **current, "timestamp_utc": time.time()}
        _service_history.insert(0, event)
        del _service_history[100:]
        _service_states[service] = current


def _get_server(sid) -> Optional[Server]:
    return POOL.get(sid)


def _not_found(sid):
    return JSONResponse(
        {"error": {"code": "unknown_server", "message": f"сервер {sid} не найден"}}, status_code=404
    )


async def _cached(sid, name, func, ttl=CACHE_TTL):
    key = (sid, name)
    async with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
    # Coalesce concurrent misses so a page refresh storm does not multiply
    # identical RCON requests.
    async with _cache_key_locks[key]:
        async with _cache_lock:
            hit = _cache.get(key)
            if hit and hit[0] > time.monotonic():
                return hit[1]
        data = await func()
        async with _cache_lock:
            _cache[key] = (time.monotonic() + ttl, data)
            if len(_cache_key_locks) > 5000:
                for old_key, old_lock in list(_cache_key_locks.items()):
                    if len(_cache_key_locks) <= 4000:
                        break
                    if old_key not in _cache and not old_lock.locked():
                        _cache_key_locks.pop(old_key, None)
        return data


async def _cached_players(srv, attempts=2):
    last = None
    for i in range(attempts):
        try:
            return await _cached(srv.id, "players", srv.api.players)
        except RCONError as e:
            last = e
            if i < attempts - 1:
                await asyncio.sleep(1.5)
    raise last


async def _invalidate(sid):
    async with _cache_lock:
        for key in [k for k in _cache if k[0] == sid]:
            del _cache[key]


def _rcon_error(e):
    status = e.status if isinstance(e.status, int) and e.status >= 400 else 502
    message = e.message if isinstance(e.status, int) and e.status >= 400 else "RCON-сервер временно недоступен"
    return JSONResponse({"error": {"code": e.code, "message": message}}, status_code=status)


async def _json_object(request: Request):
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="некорректный JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="ожидался JSON-объект")
    return body


def _public_bot(item):
    if not item:
        return None
    result = dict(item)
    result.pop("token_hash", None)
    last_seen = float(result.get("last_seen_utc") or 0)
    result["online"] = bool(result.get("enabled") and last_seen and time.time() - last_seen < 180)
    return result


async def _require_bot(request: Request, scope: str):
    bot_id = str(request.headers.get("X-Bot-ID") or "").strip().lower()
    authorization = str(request.headers.get("Authorization") or "")
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else str(request.headers.get("X-Bot-Token") or "").strip()
    if not BOT_ID_RE.fullmatch(bot_id) or not token:
        raise HTTPException(status_code=401, detail="нужны идентификатор и токен бота")
    item = await db.get_bot_integration(bot_id)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if not item or not item["enabled"] or not hmac.compare_digest(item["token_hash"], digest):
        raise HTTPException(status_code=401, detail="неверный или отключённый бот")
    if scope not in set(item["scopes"]):
        raise HTTPException(status_code=403, detail=f"боту не выдано право {scope}")
    return item


def _validate_steam_id(value):
    value = str(value or "").strip()
    if not STEAM_ID_RE.fullmatch(value):
        raise HTTPException(status_code=400, detail="SteamID64 должен состоять из 17 цифр")
    return value


async def _require_any_perm(request: Request, permissions):
    user = await _current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="нужен вход через Steam")
    me = await _user_status(user)
    if not set(permissions).intersection(me["permissions"]):
        raise HTTPException(status_code=403, detail="недостаточно прав")
    return user


async def _allow_action_once(user_id, sid, action, detail=""):
    """Suppress accidental duplicate clicks/retries for a short safety window."""
    now = time.monotonic()
    key = (str(user_id), str(sid), str(action), str(detail)[:128])
    async with _recent_actions_lock:
        for old_key, timestamp in list(_recent_actions.items()):
            if now - timestamp > 60:
                _recent_actions.pop(old_key, None)
        previous = _recent_actions.get(key)
        if previous is not None and now - previous < 2:
            return False
        if len(_recent_actions) >= 10000:
            oldest_key = min(_recent_actions, key=_recent_actions.get)
            _recent_actions.pop(oldest_key, None)
        _recent_actions[key] = now
    return True


def _request_fingerprint(request: Request, body):
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(
        f"{request.method}:{request.url.path}:{canonical}".encode("utf-8")
    ).hexdigest()


async def _idempotency_begin(request: Request, user_id, fingerprint):
    key = (request.headers.get("Idempotency-Key") or "").strip()
    if not key:
        return None, None
    if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", key):
        raise HTTPException(status_code=400, detail="некорректный Idempotency-Key")
    decision = await db.claim_idempotency(str(user_id), key, fingerprint)
    if decision["kind"] == "conflict":
        raise HTTPException(status_code=409, detail="Idempotency-Key уже использован для другого запроса")
    if decision["kind"] == "in_progress":
        raise HTTPException(status_code=409, detail="предыдущий запрос с этим ключом ещё выполняется")
    if decision["kind"] == "replay":
        return key, JSONResponse(decision["response"], status_code=decision["status_code"])
    return key, None


async def _idempotency_complete(user_id, key, response, status_code=200):
    if key:
        await db.complete_idempotency(str(user_id), key, status_code, response)


async def _idempotency_abort(user_id, key):
    if key:
        await db.abort_idempotency(str(user_id), key)


# ---------- пользователь ----------

async def _current_user(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    steam_id = await db.get_session(token)
    if not steam_id:
        return None
    await db.touch_user(steam_id)
    return {"steam_id": steam_id}


def _require_user(user):
    if not user:
        raise HTTPException(status_code=401, detail="нужен вход через Steam")
    return user


async def _user_status(user):
    """Статус пользователя: steam, persona, discord-привязка, роли и права."""
    steam_id = user["steam_id"]
    if config.DEV_AUTH and steam_id == DEV_STEAM_ID:
        permissions = sorted(dmod.ALL_PERMS)
        return {
            "authed": True,
            "steam_id": steam_id,
            "persona": "Локальный администратор",
            "discord": {"bound": True, "discord_id": "dev", "discord_name": "DEV MODE", "member": True, "admin": True},
            "permissions": permissions,
            "admin": True,
            "all_permissions": permissions,
            "dev": True,
        }
    db_user = await db.get_user(steam_id)
    persona = (db_user or {}).get("persona") or ""
    binding = await db.get_binding(steam_id)
    dstatus = await dmod.resolve_status(binding)
    role_map = await dmod.guild_roles()
    perms_map = await db.get_role_perms_all()
    permissions = dmod.compute_permissions(binding, role_map, perms_map)
    return {
        "authed": True,
        "steam_id": steam_id,
        "persona": persona,
        "discord": dstatus,
        "permissions": permissions,
        "admin": bool(permissions),
        "all_permissions": sorted(dmod.ALL_PERMS),
    }


# ---------- сессия / авторизация ----------

@router.get("/session")
async def api_session(request: Request):
    user = await _current_user(request)
    me = await _user_status(user) if user else None
    return {
        "ok": True,
        "authed": bool(user),
        "refresh": config.REFRESH,
        "me": me,
        "servers": [{"id": s.id, "name": s.name} for s in POOL.values()],
        "auth_configured": bool(
            config.DISCORD_CLIENT_ID and config.DISCORD_CLIENT_SECRET and config.DISCORD_BOT_TOKEN and config.GUILD_ID
        ),
        "steam_configured": True,
        "dev_auth_enabled": _dev_auth_allowed(request),
    }


@router.get("/system/status")
async def system_status(request: Request):
    """Единый снимок зависимостей с историей смены состояния процесса."""
    await _require_any_perm(request, {"audit", "roles", "config_view"})
    services = []
    try:
        version = await db.schema_version()
        database = {"id": "database", "ok": version >= db.REQUIRED_SCHEMA_VERSION, "detail": f"schema {version}"}
    except Exception as exc:
        database = {"id": "database", "ok": False, "detail": str(exc)[:200]}
    services.append(database)
    _record_service_state("database", database["ok"], database["detail"])

    for srv in POOL.values():
        try:
            health = await srv.api.health()
            row = {"id": f"server:{srv.id}", "name": srv.name, "ok": True, "detail": f"uptime {health.get('uptimeSeconds', 0)}s"}
        except Exception as exc:
            row = {"id": f"server:{srv.id}", "name": srv.name, "ok": False, "detail": str(exc)[:200]}
        services.append(row)
        _record_service_state(row["id"], row["ok"], row["detail"])

    role_map = await dmod.guild_roles()
    discord_ok = bool(config.DISCORD_BOT_TOKEN and config.GUILD_ID and role_map)
    discord_row = {"id": "discord", "ok": discord_ok, "detail": f"ролей: {len(role_map)}" if discord_ok else "Discord API не настроен или недоступен"}
    services.append(discord_row)
    _record_service_state("discord", discord_ok, discord_row["detail"])

    bridge, bridge_error = await dmod.bot_panel_request("GET", "/api/tempvoice")
    bridge_ok = bridge is not None
    bridge_row = {"id": "asuna", "ok": bridge_ok, "detail": f"временных комнат: {len((bridge or {}).get('rooms') or [])}" if bridge_ok else bridge_error}
    services.append(bridge_row)
    _record_service_state("asuna", bridge_ok, bridge_row["detail"])

    steam_ok = bool(config.STEAM_API_KEY)
    steam_row = {"id": "steam", "ok": steam_ok, "detail": "Steam Web API настроен" if steam_ok else "STEAM_API_KEY не задан"}
    services.append(steam_row)
    _record_service_state("steam", steam_ok, steam_row["detail"])
    return {"ok": all(item["ok"] for item in services), "services": services, "history": list(_service_history)}


@router.post("/auth/dev")
async def auth_dev(request: Request):
    if not _dev_auth_allowed(request):
        raise HTTPException(status_code=404, detail="not found")
    await db.upsert_user(DEV_STEAM_ID, "Локальный администратор")
    token = await db.create_session(DEV_STEAM_ID)
    response = JSONResponse({"ok": True, "redirect": "/panel"})
    _set_session_cookie(response, token, max_age=8 * 3600)
    return response


@router.get("/auth/steam/start")
async def auth_steam_start():
    state = secrets.token_urlsafe(32)
    await db.create_oauth_state(state, "steam")
    response = RedirectResponse(steam_mod.steam_login_url(state))
    _set_short_cookie(response, STEAM_STATE_COOKIE, state)
    return response


@router.get("/auth/steam/callback")
async def auth_steam_callback(request: Request):
    if not _valid_state(request, STEAM_STATE_COOKIE):
        return RedirectResponse("/?auth=steam&fail=state", status_code=302)
    state = request.query_params.get("state") or ""
    if not await db.consume_oauth_state(state, "steam"):
        return RedirectResponse("/?auth=steam&fail=state", status_code=302)
    params = {k: v for k, v in request.query_params.items() if k.startswith("openid.")}
    steam_id = await steam_mod.validate_callback(params, expected_state=state)
    if not steam_id:
        return RedirectResponse("/?auth=steam&fail=1", status_code=302)
    persona = ""
    if config.STEAM_API_KEY:
        try:
            personas = await steam_mod.fetch_personas([steam_id])
            persona = personas.get(steam_id) or ""
        except Exception:
            persona = ""
    await db.upsert_user(steam_id, persona or None)
    token = await db.create_session(steam_id)
    resp = RedirectResponse("/", status_code=302)
    _set_session_cookie(resp, token)
    resp.delete_cookie(STEAM_STATE_COOKIE, path="/api/auth/")
    return resp


@router.get("/auth/discord/start")
async def auth_discord_start(request: Request):
    user = await _current_user(request)
    if not user:
        return RedirectResponse("/?bind=discord&need=steam", status_code=302)
    state = secrets.token_urlsafe(32)
    await db.create_oauth_state(state, "discord", user["steam_id"])
    response = RedirectResponse(dmod.authorize_url(state=state))
    _set_short_cookie(response, DISCORD_STATE_COOKIE, state)
    return response


@router.get("/auth/discord/callback")
async def auth_discord_callback(request: Request):
    user = await _current_user(request)
    if not user:
        return RedirectResponse("/?bind=discord&need=steam", status_code=302)
    if not _valid_state(request, DISCORD_STATE_COOKIE):
        return RedirectResponse("/?bind=discord&fail=state", status_code=302)
    state = request.query_params.get("state") or ""
    if not await db.consume_oauth_state(state, "discord", user["steam_id"]):
        return RedirectResponse("/?bind=discord&fail=state", status_code=302)
    error = "unknown"
    code = request.query_params.get("code")
    if request.query_params.get("error"):
        error = request.query_params["error"]
    elif code:
        discord_id, name = await dmod.exchange_code(code)
        if discord_id and name:
            other = await db.get_steam_by_discord(discord_id)
            if other and other != user["steam_id"]:
                return RedirectResponse("/?bind=discord&fail=already", status_code=302)
            roles, member = await dmod.fetch_member(discord_id)
            if roles is None:
                roles = []
                member = False
            await db.upsert_binding(user["steam_id"], discord_id, name, roles, member)
            await db.revoke_user_sessions(user["steam_id"])
            replacement_token = await db.create_session(user["steam_id"])
            response = RedirectResponse("/?bind=discord&ok=1", status_code=302)
            _set_session_cookie(response, replacement_token)
            response.delete_cookie(DISCORD_STATE_COOKIE, path="/api/auth/")
            return response
        error = "no_code"
    return RedirectResponse(f"/?bind=discord&fail={error}", status_code=302)


@router.post("/auth/refresh")
async def auth_refresh(request: Request):
    user = await _current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="нужен вход")
    session_rotated = False
    if config.DISCORD_BOT_TOKEN and config.GUILD_ID:
        binding = await db.get_binding(user["steam_id"])
        if binding and binding["discord_id"]:
            old_roles = list(binding.get("roles") or [])
            old_member = bool(binding.get("guild_member"))
            roles, member = await dmod.fetch_member(binding["discord_id"])
            if roles is not None:
                await db.upsert_binding(
                    user["steam_id"], binding["discord_id"], binding["discord_name"], roles, member
                )
                session_rotated = old_roles != list(roles or []) or old_member != bool(member)
    result = await _user_status(user)
    if not session_rotated:
        return result
    await db.revoke_user_sessions(user["steam_id"])
    replacement_token = await db.create_session(user["steam_id"])
    response = JSONResponse(result)
    _set_session_cookie(response, replacement_token)
    return response


@router.post("/auth/logout")
async def auth_logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await db.delete_session(token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


# ---------- мониторинг (анонимно) ----------

PLAYER_ACTION_PERMS = {"players_view", "kick", "kill", "message", "move_faction"}


async def _can_view_players(request: Request):
    user = await _current_user(request)
    if not user:
        return False
    me = await _user_status(user)
    return bool(PLAYER_ACTION_PERMS.intersection(me["permissions"]))


@router.get("/server/{sid}/overview")
async def server_overview(sid: str, request: Request):
    srv = _get_server(sid)
    if not srv:
        return _not_found(sid)
    st = players = rotation = bans = health = server_identity = None
    bans_count = player_count = None
    err = None
    status = 200
    try:
        st = await _cached(srv.id, "status", srv.api.status)
    except RCONError as e:
        err = {
            "code": e.code,
            "message": e.message if isinstance(e.status, int) and e.status >= 400 else "RCON-сервер временно недоступен",
        }
        status = e.status or 502
    if err is None:
        try:
            players = await _cached_players(srv)
            player_count = players.get("count", len(players.get("players") or []))
        except RCONError:
            player_count = None
        for name, f in (("rotation", srv.api.rotation), ("health", srv.api.health)):
            try:
                data = await _cached(srv.id, name, f)
                if name == "rotation":
                    rotation = data
                else:
                    health = data
            except RCONError:
                pass
        try:
            ban_meta = await _cached(srv.id, "bans", srv.api.bans)
            bans_count = ban_meta.get("count") if isinstance(ban_meta, dict) else None
        except RCONError:
            pass
    await srv.ensure_caps()
    can_view_players = await _can_view_players(request)
    current_user = await _current_user(request)
    current_status = await _user_status(current_user) if current_user else {}
    can_view_bans = bool({"ban_view", "ban_add", "ban_remove"}.intersection(current_status.get("permissions", [])))
    if can_view_bans:
        try:
            bans = await _cached(srv.id, "bans", srv.api.bans)
            bans = await _enrich_bans(srv.id, bans)
        except RCONError:
            pass
    can_view_server_id = False
    if current_user:
        can_view_server_id = bool(current_status.get("permissions"))
    if "GET /v1/server-id" in srv.routes:
        try:
            server_identity = await _cached(srv.id, "server-id", lambda: srv.api.get("/v1/server-id"), ttl=60.0)
        except RCONError:
            pass
    if st is not None:
        try:
            status_players = st.get("players") or {}
            bucket = int(time.time() // 60)
            if _monitor_buckets.get(srv.id) != bucket:
                # A public page can be refreshed by many clients. Persist at
                # most one sample per server/minute instead of turning reads
                # into unbounded SQLite writes.
                _monitor_buckets[srv.id] = bucket
                await db.record_monitoring(
                    srv.id,
                    status_players.get("current", player_count or 0),
                    status_players.get("max", 0),
                    (players or {}).get("players") or [],
                )
        except Exception:
            _monitor_buckets.pop(srv.id, None)
            log.exception("Не удалось сохранить срез мониторинга %s", srv.id)
    visible_players = None
    if can_view_players and isinstance(players, dict):
        visible_players = dict(players)
        visible_players["players"] = [
            player for player in (players.get("players") or [])[:1000]
            if isinstance(player, dict)
        ]
    payload = {
        "ok": err is None,
        "server": {
            "id": srv.id,
            "name": srv.name,
            "connect_url": srv.connect_url,
            "server_id": (server_identity or {}).get("serverId"),
        },
        "error": err,
        "status": st,
        # Количество публично, персональный список — только для разрешённых ролей.
        "players": visible_players,
        "players_visible": can_view_players,
        "player_count": player_count,
        # Полная ротация доступна только сотрудникам с правами панели.
        "rotation": rotation if can_view_server_id else None,
        "bans": bans if can_view_bans else None,
        "bans_count": bans_count,
        "health": health,
        "ts": time.time(),
    }
    return JSONResponse(payload, status_code=status)


@router.get("/stats/population")
async def stats_population(hours: int = 24):
    return {"ok": True, "hours": max(1, min(hours, 24 * 31)), "samples": await db.get_population(hours)}


@router.get("/stats/players")
async def stats_players(request: Request, limit: int = 100, sort: str = "kills"):
    if not await _can_view_players(request):
        raise HTTPException(status_code=403, detail="рейтинг игроков доступен только разрешённым ролям")
    normalized_sort = sort if sort in {"kills", "kd", "playtime", "sessions", "recent"} else "kills"
    return {
        "ok": True,
        "sort": normalized_sort,
        "players": await db.get_player_leaderboard(limit, normalized_sort),
    }


def _csv_response(filename, columns, rows):
    def safe_cell(value):
        text = "" if value is None else str(value)
        if text.startswith(("=", "+", "-", "@", "\t", "\r")):
            return "'" + text
        return text

    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=columns)
    writer.writeheader()
    writer.writerows({column: safe_cell(row.get(column)) for column in columns} for row in rows)
    return Response(out.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/stats/population/export")
async def stats_population_export(request: Request, hours: int = 24):
    await _require_perm(request, "audit")
    return _csv_response("population.csv", ["server_id", "ts", "players", "max_players"], await db.get_population(hours))


@router.get("/audit/export")
async def site_audit_export(request: Request, limit: int = 500):
    await _require_perm(request, "audit")
    return _csv_response("site-audit.csv", ["timestamp_utc", "actor_steam_id", "server_id", "event", "detail"], await db.get_site_audit(limit))


@router.get("/players")
async def players_directory(request: Request, q: str = "", limit: int = 50, offset: int = 0):
    """Search the retained player directory used by the player card drawer."""
    await _require_perm(request, "players_view")
    return {
        "ok": True,
        "query": str(q or "")[:120],
        "items": await db.search_players(q, limit, offset),
    }


@router.get("/players/{steam_id}")
async def player_profile(steam_id: str, request: Request):
    """Return an aggregated, permission-protected player profile."""
    user = await _require_perm(request, "players_view")
    steam_id = _validate_steam_id(steam_id)
    profile = await db.get_player_profile(steam_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Игрок ещё не попал в историю мониторинга")
    status = await _user_status(user)
    can_view_punishments = bool(
        {"audit", "ban_view", "ban_add", "ban_remove"}.intersection(status["permissions"])
    )
    profile["punishments"] = (
        await db.list_player_punishments(steam_id) if can_view_punishments else []
    )
    profile["can_view_punishments"] = can_view_punishments
    profile["tags"] = await db.list_player_tags(steam_id)
    profile["can_edit_tags"] = "player_tags" in status["permissions"]
    profile["clan"] = await db.get_player_clan(steam_id)
    profile["can_edit_clan"] = "clans" in status["permissions"]
    binding = await db.get_binding(steam_id)
    profile['discord_binding'] = {k: binding.get(k) for k in ('discord_id','discord_name','guild_member')} if binding else None
    can_view_vip = bool({"vip_view", "vip_edit"}.intersection(status["permissions"]))
    profile["vip_slots"] = await db.list_player_vip_slots(steam_id) if can_view_vip else []
    now = time.time()
    for vip in profile["vip_slots"]:
        vip["active"] = vip["expires_utc"] is None or vip["expires_utc"] > now
    profile["can_view_vip"] = can_view_vip
    steam_profiles = await steam_mod.fetch_profiles([steam_id])
    steam_profile = steam_profiles.get(steam_id) or {}
    steam_bans = (await steam_mod.fetch_player_bans([steam_id])).get(steam_id) or {}
    profile["avatar_url"] = steam_profile.get("avatar_url") or ""
    profile["steam_profile_url"] = steam_profile.get("profile_url") or f"https://steamcommunity.com/profiles/{steam_id}"
    profile["steam"] = {
        "visibility": steam_profile.get("visibility", 0),
        "time_created_utc": steam_profile.get("time_created_utc"),
        "vac_banned": bool(steam_bans.get("vac_banned")),
        "vac_bans": int(steam_bans.get("vac_bans") or 0),
        "game_bans": int(steam_bans.get("game_bans") or 0),
        "days_since_last_ban": int(steam_bans.get("days_since_last_ban") or 0),
        "community_banned": bool(steam_bans.get("community_banned")),
    }
    return {"ok": True, "profile": profile}


@router.get("/players/{steam_id}/history/export")
async def player_history_export(steam_id: str, request: Request):
    await _require_any_perm(request, {"audit", "ban_view", "ban_add", "ban_remove"})
    steam_id = _validate_steam_id(steam_id)
    rows = await db.list_player_punishments(steam_id, 200)
    return _csv_response(
        f"player-{steam_id}-history.csv",
        ["timestamp_utc", "server_id", "event", "actor_steam_id", "actor_name"],
        rows,
    )


@router.get("/players/{steam_id}/notes")
async def player_notes_list(steam_id: str, request: Request, limit: int = 100):
    """Return private staff notes for a retained player profile."""
    await _require_perm(request, "player_notes")
    steam_id = _validate_steam_id(steam_id)
    return {
        "ok": True,
        "notes": await db.list_player_notes(steam_id, limit),
        "can_edit": True,
    }


@router.post("/players/{steam_id}/notes")
async def player_note_add(steam_id: str, request: Request):
    user = await _require_perm(request, "player_notes")
    steam_id = _validate_steam_id(steam_id)
    body = await _json_object(request)
    note = str(body.get("note") or "").strip()
    if not note or len(note) > 2000:
        raise HTTPException(status_code=400, detail="Заметка должна содержать от 1 до 2000 символов")
    idem_key, replay = await _idempotency_begin(
        request, user["steam_id"], _request_fingerprint(request, body)
    )
    if replay:
        return replay
    try:
        item = await db.add_player_note(steam_id, user["steam_id"], note)
        item["author_name"] = user.get("persona") or ""
        await db.log_site_audit(
            user["steam_id"], None, "player.note.add", f"{steam_id}:{item['id']}"
        )
    except Exception:
        await _idempotency_abort(user["steam_id"], idem_key)
        raise
    response = {"ok": True, "note": item}
    await _idempotency_complete(user["steam_id"], idem_key, response)
    return response


@router.delete("/players/{steam_id}/notes/{note_id}")
async def player_note_delete(steam_id: str, note_id: str, request: Request):
    user = await _require_perm(request, "player_notes")
    steam_id = _validate_steam_id(steam_id)
    if not re.fullmatch(r"[0-9a-f]{24}", str(note_id or "")):
        raise HTTPException(status_code=400, detail="Некорректный ID заметки")
    idem_key, replay = await _idempotency_begin(
        request, user["steam_id"], _request_fingerprint(request, {})
    )
    if replay:
        return replay
    deleted = await db.delete_player_note(note_id, steam_id)
    if not deleted:
        await _idempotency_abort(user["steam_id"], idem_key)
        raise HTTPException(status_code=404, detail="Заметка не найдена")
    await db.log_site_audit(
        user["steam_id"], None, "player.note.delete", f"{steam_id}:{note_id}"
    )
    response = {"ok": True}
    await _idempotency_complete(user["steam_id"], idem_key, response)
    return response


@router.post("/players/{steam_id}/tags")
async def player_tag_add(steam_id: str, request: Request):
    user = await _require_perm(request, "player_tags")
    steam_id = _validate_steam_id(steam_id)
    body = await _json_object(request)
    label = " ".join(str(body.get("label") or "").split())
    color = str(body.get("color") or "gray").strip().lower()
    if not label or len(label) > 32:
        raise HTTPException(status_code=400, detail="Метка должна содержать от 1 до 32 символов")
    if color not in {"gray", "orange", "red", "green", "blue", "purple", "pink"}:
        raise HTTPException(status_code=400, detail="Некорректный цвет метки")
    idem_key, replay = await _idempotency_begin(
        request, user["steam_id"], _request_fingerprint(request, body)
    )
    if replay:
        return replay
    item = await db.add_player_tag(steam_id, label, color, user["steam_id"])
    item["created_by_name"] = user.get("persona") or ""
    if item.pop("created", False):
        await db.log_site_audit(
            user["steam_id"], None, "player.tag.add", f"{steam_id}:{item['id']}"
        )
    response = {"ok": True, "tag": item}
    await _idempotency_complete(user["steam_id"], idem_key, response)
    return response


@router.delete("/players/{steam_id}/tags/{tag_id}")
async def player_tag_delete(steam_id: str, tag_id: str, request: Request):
    user = await _require_perm(request, "player_tags")
    steam_id = _validate_steam_id(steam_id)
    if not re.fullmatch(r"[0-9a-f]{24}", str(tag_id or "")):
        raise HTTPException(status_code=400, detail="Некорректный ID метки")
    idem_key, replay = await _idempotency_begin(
        request, user["steam_id"], _request_fingerprint(request, {})
    )
    if replay:
        return replay
    deleted = await db.delete_player_tag(tag_id, steam_id)
    if not deleted:
        await _idempotency_abort(user["steam_id"], idem_key)
        raise HTTPException(status_code=404, detail="Метка не найдена")
    await db.log_site_audit(
        user["steam_id"], None, "player.tag.delete", f"{steam_id}:{tag_id}"
    )
    response = {"ok": True}
    await _idempotency_complete(user["steam_id"], idem_key, response)
    return response


def _validate_clan_payload(body):
    name = " ".join(str(body.get("name") or "").split())
    tag = str(body.get("tag") or "").strip()
    color = str(body.get("color") or "orange").strip().lower()
    description = str(body.get("description") or "").strip()
    if not name or len(name) > 80:
        raise HTTPException(status_code=400, detail="Название клана должно содержать от 1 до 80 символов")
    if not re.fullmatch(r"[\wА-Яа-яЁё-]{1,16}", tag, re.UNICODE):
        raise HTTPException(status_code=400, detail="Тег клана: 1–16 букв, цифр, _ или -")
    if color not in {"gray", "orange", "red", "green", "blue", "purple", "pink"}:
        raise HTTPException(status_code=400, detail="Некорректный цвет клана")
    if len(description) > 1000:
        raise HTTPException(status_code=400, detail="Описание клана длиннее 1000 символов")
    return name, tag, color, description


def _clan_id(value):
    if not re.fullmatch(r"[0-9a-f]{24}", str(value or "")):
        raise HTTPException(status_code=400, detail="Некорректный ID клана")
    return str(value)


async def _clan_after_change(clan_id, user, event, detail):
    item = await db.get_clan(clan_id)
    if not item:
        raise HTTPException(status_code=404, detail="Клан не найден")
    await db.log_site_audit(user["steam_id"], None, event, detail)
    return {"ok": True, "clan": item}


@router.get("/clans")
async def clans_list(request: Request):
    await _require_perm(request, "clans")
    return {"ok": True, "clans": await db.list_clans()}


def _public_clan(item, detail=False):
    """Не отдаёт SteamID и служебные поля в публичную витрину кланов."""
    result = {
        "id": item["id"], "name": item["name"], "tag": item["tag"],
        "color": item.get("color") or "#c51b32",
        "description": item.get("description") or "",
        "emblem_url": item.get("emblem_url") or "",
        "cover_url": item.get("cover_url") or "",
        "member_count": int(item.get("member_count") or 0),
    }
    if detail:
        result["leader"] = (item.get("leader") or {}).get("name") or "Не назначен"
        result["members"] = [
            {"name": member.get("name") or "Игрок", "member_role": member.get("member_role") or "Участник"}
            for member in item.get("members") or []
        ]
    return result


@router.get("/public/clans")
async def public_clans_list():
    return {"ok": True, "clans": [_public_clan(item) for item in await db.list_clans()]}


@router.get("/public/clans/{clan_id}")
async def public_clan_get(clan_id: str):
    item = await db.get_clan(_clan_id(clan_id))
    if not item:
        raise HTTPException(status_code=404, detail="Клан не найден")
    return {"ok": True, "clan": _public_clan(item, detail=True)}


@router.post("/public/clan-applications")
async def public_clan_application_create(request: Request):
    user = await _current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Войдите через Steam, чтобы отправить заявку")
    body = await _json_object(request)
    name = " ".join(str(body.get("name") or "").split())
    tag = "".join(str(body.get("tag") or "").upper().split())
    description = str(body.get("description") or "").strip()
    if not 3 <= len(name) <= 48 or not re.fullmatch(r"[A-ZА-Я0-9_-]{2,12}", tag):
        raise HTTPException(status_code=400, detail="Укажите название от 3 до 48 символов и тег из 2–12 букв или цифр")
    if not 30 <= len(description) <= 2500:
        raise HTTPException(status_code=400, detail="Опишите клан: от 30 до 2500 символов")
    title = f"Заявка на клан [{tag}] {name}"
    discord_name = ((await _user_status(user)).get("discord") or {}).get("discord_name") or "не привязан"
    ticket_text = f"{description}\n\nЛидер: {user.get('persona') or user['steam_id']}\nSteamID: {user['steam_id']}\nDiscord: {discord_name}"
    item = await db.create_ticket(title, ticket_text, "normal", user["steam_id"], user["steam_id"])
    bridge_payload = {
        "ticket_id": item["id"], "name": name, "tag": tag, "description": description,
        "leader": user.get("persona") or user["steam_id"], "steam_id": user["steam_id"],
        "discord_name": discord_name,
    }
    bridge_response, delivery_error = await dmod.bot_panel_request("POST", "/api/clan-applications", bridge_payload)
    delivered = bool(bridge_response and bridge_response.get("ok"))
    await db.log_site_audit(user["steam_id"], None, "clan.application.create", f"{item['id']}:discord={int(delivered)}")
    return {"ok": True, "ticket_id": item["id"], "discord_delivered": delivered, "discord_error": delivery_error}


@router.get("/clans/{clan_id}")
async def clan_get(clan_id: str, request: Request):
    await _require_perm(request, "clans")
    _clan_id(clan_id)
    item = await db.get_clan(clan_id)
    if not item:
        raise HTTPException(status_code=404, detail="Клан не найден")
    return {"ok": True, "clan": item}


@router.post("/clans")
async def clan_create(request: Request):
    user = await _require_perm(request, "clans")
    body = await _json_object(request)
    name, tag, color, description = _validate_clan_payload(body)
    idem_key, replay = await _idempotency_begin(
        request, user["steam_id"], _request_fingerprint(request, body)
    )
    if replay:
        return replay
    item = await db.create_clan(name, tag, color, description, user["steam_id"])
    if not item or not item.pop("created", False):
        await _idempotency_abort(user["steam_id"], idem_key)
        raise HTTPException(status_code=409, detail="Клан с таким тегом уже существует")
    await db.log_site_audit(user["steam_id"], None, "clan.create", item["id"])
    response = {"ok": True, "clan": item}
    await _idempotency_complete(user["steam_id"], idem_key, response)
    return response


@router.patch("/clans/{clan_id}")
async def clan_update(clan_id: str, request: Request):
    user = await _require_perm(request, "clans")
    _clan_id(clan_id)
    body = await _json_object(request)
    name, tag, color, description = _validate_clan_payload(body)
    idem_key, replay = await _idempotency_begin(
        request, user["steam_id"], _request_fingerprint(request, body)
    )
    if replay:
        return replay
    try:
        updated = await db.update_clan(clan_id, name, tag, color, description)
    except Exception as exc:
        await _idempotency_abort(user["steam_id"], idem_key)
        if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
            raise HTTPException(status_code=409, detail="Клан с таким тегом уже существует") from None
        raise
    if not updated:
        await _idempotency_abort(user["steam_id"], idem_key)
        raise HTTPException(status_code=404, detail="Клан не найден")
    await db.log_site_audit(user["steam_id"], None, "clan.update", clan_id)
    response = {"ok": True}
    await _idempotency_complete(user["steam_id"], idem_key, response)
    return response


@router.delete("/clans/{clan_id}")
async def clan_delete(clan_id: str, request: Request):
    user = await _require_perm(request, "clans")
    _clan_id(clan_id)
    idem_key, replay = await _idempotency_begin(
        request, user["steam_id"], _request_fingerprint(request, {})
    )
    if replay:
        return replay
    if not await db.delete_clan(clan_id):
        await _idempotency_abort(user["steam_id"], idem_key)
        raise HTTPException(status_code=404, detail="Клан не найден")
    await db.log_site_audit(user["steam_id"], None, "clan.delete", clan_id)
    response = {"ok": True}
    await _idempotency_complete(user["steam_id"], idem_key, response)
    return response


@router.post("/clans/{clan_id}/media")
async def clan_media_upload(clan_id: str, request: Request):
    user = await _require_perm(request, "clans")
    clan_id = _clan_id(clan_id)
    body = await _json_object(request)
    kind = str(body.get("kind") or "")
    data_url = str(body.get("data_url") or "")
    match = re.fullmatch(r"data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=]+)", data_url)
    if kind not in {"emblem", "cover"} or not match:
        raise HTTPException(status_code=400, detail="Нужна картинка PNG, JPG или WebP")
    try:
        content = base64.b64decode(match.group(2), validate=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Не удалось прочитать изображение") from exc
    magic = {"png": b"\x89PNG\r\n\x1a\n", "jpeg": b"\xff\xd8\xff", "webp": b"RIFF"}
    ext = match.group(1)
    if len(content) < 12 or len(content) > 2 * 1024 * 1024 or not content.startswith(magic[ext]) or (ext == "webp" and content[8:12] != b"WEBP"):
        raise HTTPException(status_code=400, detail="Файл повреждён или превышает 2 МБ")
    uploads = config.BASE_DIR / "static" / "uploads" / "clans"
    uploads.mkdir(parents=True, exist_ok=True)
    filename = f"{clan_id}-{kind}.{ {'png':'png','jpeg':'jpg','webp':'webp'}[ext] }"
    (uploads / filename).write_bytes(content)
    url = f"/static/uploads/clans/{filename}"
    if not await db.update_clan_media(clan_id, kind, url):
        raise HTTPException(status_code=404, detail="Клан не найден")
    return await _clan_after_change(clan_id, user, "clan.media.update", f"{clan_id}:{kind}")


@router.put("/clans/{clan_id}/members")
async def clan_member_set(clan_id: str, request: Request):
    user = await _require_perm(request, "clans")
    clan_id = _clan_id(clan_id)
    body = await _json_object(request)
    steam_id = _validate_steam_id(str(body.get("steam_id") or ""))
    role = str(body.get("member_role") or "Участник")
    if role not in {"Лидер", "Заместитель", "Участник"}:
        raise HTTPException(status_code=400, detail="Выберите роль из списка")
    if not await db.set_clan_member_role(clan_id, steam_id, role):
        raise HTTPException(status_code=404, detail="Клан не найден")
    return await _clan_after_change(clan_id, user, "clan.member.set", f"{clan_id}:{steam_id}:{role}")


@router.delete("/clans/{clan_id}/members/{steam_id}")
async def clan_member_delete(clan_id: str, steam_id: str, request: Request):
    user = await _require_perm(request, "clans")
    clan_id = _clan_id(clan_id)
    steam_id = _validate_steam_id(steam_id)
    if not await db.remove_clan_member(clan_id, steam_id):
        raise HTTPException(status_code=404, detail="Участник не найден")
    return await _clan_after_change(clan_id, user, "clan.member.remove", f"{clan_id}:{steam_id}")


@router.put("/players/{steam_id}/clan")
async def player_clan_set(steam_id: str, request: Request):
    user = await _require_perm(request, "clans")
    steam_id = _validate_steam_id(steam_id)
    body = await _json_object(request)
    clan_id = str(body.get("clan_id") or "").strip()
    member_role = " ".join(str(body.get("member_role") or "Участник").split())[:40]
    if not re.fullmatch(r"[0-9a-f]{24}", clan_id):
        raise HTTPException(status_code=400, detail="Некорректный ID клана")
    idem_key, replay = await _idempotency_begin(
        request, user["steam_id"], _request_fingerprint(request, body)
    )
    if replay:
        return replay
    if not await db.set_player_clan(steam_id, clan_id, member_role or "Участник"):
        await _idempotency_abort(user["steam_id"], idem_key)
        raise HTTPException(status_code=404, detail="Клан не найден")
    await db.log_site_audit(user["steam_id"], None, "clan.member.set", f"{steam_id}:{clan_id}")
    response = {"ok": True, "clan": await db.get_player_clan(steam_id)}
    await _idempotency_complete(user["steam_id"], idem_key, response)
    return response


@router.delete("/players/{steam_id}/clan")
async def player_clan_remove(steam_id: str, request: Request):
    user = await _require_perm(request, "clans")
    steam_id = _validate_steam_id(steam_id)
    idem_key, replay = await _idempotency_begin(
        request, user["steam_id"], _request_fingerprint(request, {})
    )
    if replay:
        return replay
    if not await db.remove_player_clan(steam_id):
        await _idempotency_abort(user["steam_id"], idem_key)
        raise HTTPException(status_code=404, detail="Игрок не состоит в клане")
    await db.log_site_audit(user["steam_id"], None, "clan.member.remove", steam_id)
    response = {"ok": True}
    await _idempotency_complete(user["steam_id"], idem_key, response)
    return response


TICKET_STATUSES = {"open", "in_progress", "resolved", "closed"}
TICKET_PRIORITIES = {"low", "normal", "high", "urgent"}


def _ticket_id(value):
    value = str(value or "")
    if not re.fullmatch(r"[0-9a-f]{24}", value):
        raise HTTPException(status_code=400, detail="Некорректный ID тикета")
    return value


@router.get("/tickets")
async def tickets_list(request: Request):
    await _require_any_perm(request, {"tickets_view", "tickets_edit"})
    items = await db.list_tickets()
    return {"ok": True, "items": items, "count": len(items)}


@router.get("/tickets/{ticket_id}")
async def ticket_get(ticket_id: str, request: Request):
    await _require_any_perm(request, {"tickets_view", "tickets_edit"})
    item = await db.get_ticket(_ticket_id(ticket_id))
    if not item:
        raise HTTPException(status_code=404, detail="Тикет не найден")
    item["history"] = [
        row for row in await db.get_site_audit(500)
        if str(row.get("event") or "").startswith("ticket.")
        and ticket_id in str(row.get("detail") or "")
    ]
    return {"ok": True, "ticket": item}


@router.post("/tickets")
async def ticket_create(request: Request):
    user = await _require_perm(request, "tickets_edit")
    body = await _json_object(request)
    title = " ".join(str(body.get("title") or "").split())
    description = str(body.get("description") or "").strip()
    priority = str(body.get("priority") or "normal").strip()
    player_steam_id = str(body.get("player_steam_id") or "").strip() or None
    if not title or len(title) > 160:
        raise HTTPException(status_code=400, detail="Заголовок должен содержать от 1 до 160 символов")
    if len(description) > 5000:
        raise HTTPException(status_code=400, detail="Описание длиннее 5000 символов")
    if priority not in TICKET_PRIORITIES:
        raise HTTPException(status_code=400, detail="Некорректный приоритет")
    if player_steam_id:
        player_steam_id = _validate_steam_id(player_steam_id)
    idem_key, replay = await _idempotency_begin(
        request, user["steam_id"], _request_fingerprint(request, body)
    )
    if replay:
        return replay
    item = await db.create_ticket(
        title, description, priority, player_steam_id, user["steam_id"]
    )
    await db.log_site_audit(user["steam_id"], None, "ticket.create", item["id"])
    response = {"ok": True, "ticket": item}
    await _idempotency_complete(user["steam_id"], idem_key, response)
    return response


@router.patch("/tickets/{ticket_id}")
async def ticket_update(ticket_id: str, request: Request):
    user = await _require_perm(request, "tickets_edit")
    ticket_id = _ticket_id(ticket_id)
    current = await db.get_ticket(ticket_id)
    if not current:
        raise HTTPException(status_code=404, detail="Тикет не найден")
    body = await _json_object(request)
    title = " ".join(str(body.get("title", current["title"]) or "").split())
    description = str(body.get("description", current["description"]) or "").strip()
    status = str(body.get("status", current["status"]) or "").strip()
    priority = str(body.get("priority", current["priority"]) or "").strip()
    player_steam_id = body.get("player_steam_id", current["player_steam_id"])
    assigned_to = body.get("assigned_to", current["assigned_to"])
    player_steam_id = str(player_steam_id or "").strip() or None
    assigned_to = str(assigned_to or "").strip() or None
    if not title or len(title) > 160 or len(description) > 5000:
        raise HTTPException(status_code=400, detail="Некорректный заголовок или описание")
    if status not in TICKET_STATUSES or priority not in TICKET_PRIORITIES:
        raise HTTPException(status_code=400, detail="Некорректный статус или приоритет")
    if player_steam_id:
        player_steam_id = _validate_steam_id(player_steam_id)
    if assigned_to:
        assigned_to = _validate_steam_id(assigned_to)
    idem_key, replay = await _idempotency_begin(
        request, user["steam_id"], _request_fingerprint(request, body)
    )
    if replay:
        return replay
    await db.update_ticket(
        ticket_id, title, description, status, priority, player_steam_id, assigned_to
    )
    await db.log_site_audit(
        user["steam_id"], None, "ticket.update", f"{ticket_id}:{status}:{priority}"
    )
    response = {"ok": True, "ticket": await db.get_ticket(ticket_id)}
    await _idempotency_complete(user["steam_id"], idem_key, response)
    return response


@router.post("/tickets/{ticket_id}/comments")
async def ticket_comment_add(ticket_id: str, request: Request):
    user = await _require_perm(request, "tickets_edit")
    ticket_id = _ticket_id(ticket_id)
    body = await _json_object(request)
    text = str(body.get("body") or "").strip()
    if not text or len(text) > 2000:
        raise HTTPException(status_code=400, detail="Комментарий должен содержать от 1 до 2000 символов")
    idem_key, replay = await _idempotency_begin(
        request, user["steam_id"], _request_fingerprint(request, body)
    )
    if replay:
        return replay
    comment = await db.add_ticket_comment(ticket_id, user["steam_id"], text)
    if not comment:
        await _idempotency_abort(user["steam_id"], idem_key)
        raise HTTPException(status_code=404, detail="Тикет не найден")
    comment["author_name"] = user.get("persona") or ""
    await db.log_site_audit(user["steam_id"], None, "ticket.comment", ticket_id)
    response = {"ok": True, "comment": comment}
    await _idempotency_complete(user["steam_id"], idem_key, response)
    return response


@router.delete("/tickets/{ticket_id}")
async def ticket_delete(ticket_id: str, request: Request):
    user = await _require_perm(request, "tickets_edit")
    ticket_id = _ticket_id(ticket_id)
    idem_key, replay = await _idempotency_begin(
        request, user["steam_id"], _request_fingerprint(request, {})
    )
    if replay:
        return replay
    if not await db.delete_ticket(ticket_id):
        await _idempotency_abort(user["steam_id"], idem_key)
        raise HTTPException(status_code=404, detail="Тикет не найден")
    await db.log_site_audit(user["steam_id"], None, "ticket.delete", ticket_id)
    response = {"ok": True}
    await _idempotency_complete(user["steam_id"], idem_key, response)
    return response


@router.get("/server/{sid}/players")
async def server_players(sid: str, request: Request):
    if not await _can_view_players(request):
        raise HTTPException(status_code=403, detail="список игроков доступен только разрешённым ролям")
    srv = _get_server(sid)
    if not srv:
        return _not_found(sid)
    try:
        data = await _cached_players(srv)
    except RCONError as e:
        return _rcon_error(e)
    listed_players = [
        player for player in (data.get("players") or [])[:1000]
        if isinstance(player, dict)
    ]
    return {"ok": True, "count": data.get("count", len(listed_players)), "players": listed_players}


@router.get("/server/{sid}/rotation")
async def server_rotation(sid: str, request: Request):
    user = await _current_user(request)
    if not user or not (await _user_status(user)).get("permissions"):
        raise HTTPException(status_code=403, detail="ротация доступна только разрешённым ролям")
    srv = _get_server(sid)
    if not srv:
        return _not_found(sid)
    try:
        data = await _cached(srv.id, "rotation", srv.api.rotation)
    except RCONError as e:
        return _rcon_error(e)
    return {"ok": True, **data}


@router.put("/server/{sid}/rotation")
async def server_rotation_update(sid: str, request: Request):
    """Proxy a validated rotation update to the upstream RCON API."""
    user = await _require_perm(request, "change_map")
    srv = _get_server(sid)
    if not srv:
        return _not_found(sid)
    body = await _json_object(request)
    raw_entries = body.get("entries")
    if not isinstance(raw_entries, list) or not 1 <= len(raw_entries) <= 100:
        raise HTTPException(status_code=400, detail="entries должен содержать от 1 до 100 карт")
    entries = []
    for index, entry in enumerate(raw_entries):
        if not isinstance(entry, dict):
            raise HTTPException(status_code=400, detail=f"элемент ротации #{index + 1} должен быть объектом")
        map_id = str(entry.get("map") or "").strip()[:128]
        if not map_id:
            raise HTTPException(status_code=400, detail=f"у элемента ротации #{index + 1} не указана карта")
        experiences = entry.get("experiences") or []
        if not isinstance(experiences, list) or any(not isinstance(value, str) for value in experiences):
            raise HTTPException(status_code=400, detail=f"experiences элемента #{index + 1} должен быть массивом строк")
        entries.append(
            {
                "map": map_id,
                "experiences": [value.strip()[:128] for value in experiences[:20] if value.strip()],
                "lighting": str(entry.get("lighting") or "").strip()[:128],
                "zoneAlternator": str(entry.get("zoneAlternator") or "").strip()[:128],
            }
        )
    payload = {"entries": entries}
    if body.get("mode") in {"ordered", "random"}:
        payload["mode"] = body["mode"]
    try:
        status, data = await srv.api.request_raw("PUT", "/v1/rotation", json_body=payload)
    except RCONError as e:
        return _rcon_error(e)
    if status >= 400:
        return JSONResponse(data if isinstance(data, dict) else {"detail": str(data)}, status_code=status)
    await db.log_site_audit(user["steam_id"], sid, "rotation.update", f"{len(entries)} карт")
    async with _cache_lock:
        _cache.pop((sid, "rotation"), None)
    return {"ok": True, "rotation": data, "entries": entries}


async def _enrich_bans(sid, data):
    """Merge panel-managed expiry metadata into the live upstream ban list."""
    if not isinstance(data, dict):
        return data
    managed = {
        item["steam_id"]: item
        for item in await db.list_managed_bans(sid)
        if item["sync_state"] != "expired"
    }
    result = dict(data)
    rows = []
    for raw in (data.get("bans") or [])[:5000]:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        meta = managed.get(str(item.get("steamId") or ""))
        if meta:
            item["managed"] = True
            item["expiresUtc"] = meta["expires_utc"]
            item["displayName"] = meta["display_name"]
            item["syncState"] = meta["sync_state"]
            item["syncError"] = meta["sync_error"]
        rows.append(item)
    result["bans"] = rows
    result["count"] = data.get("count", len(rows))
    return result


@router.get("/server/{sid}/bans")
async def server_bans(sid: str, request: Request):
    await _require_any_perm(request, {"ban_view", "ban_add", "ban_remove"})
    srv = _get_server(sid)
    if not srv:
        return _not_found(sid)
    try:
        data = await _cached(srv.id, "bans", srv.api.bans)
    except RCONError as e:
        return _rcon_error(e)
    data = await _enrich_bans(srv.id, data)
    return {"ok": True, **data}


@router.get("/server/{sid}/catalog")
async def server_catalog(sid: str, request: Request):
    await _require_any_perm(request, {"change_map", "change_lighting"})
    srv = _get_server(sid)
    if not srv:
        return _not_found(sid)
    try:
        maps = await _cached(srv.id, "catalog_maps", lambda: srv.api.get("/v1/catalog/maps"))
        lightings = await _cached(srv.id, "catalog_lightings", lambda: srv.api.get("/v1/catalog/lightings"))
    except RCONError as e:
        return _rcon_error(e)
    by_map = {}
    map_rows = maps.get("maps") if isinstance(maps, dict) else []
    for m in (map_rows or [])[:200]:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        mid = str(m["id"])[:128]
        encoded_mid = quote(mid, safe="")
        ex = al = {}
        try:
            ex = await _cached(
                srv.id,
                f"catalog_ex_{mid}",
                lambda path_id=encoded_mid: srv.api.get(f"/v1/catalog/maps/{path_id}/experiences"),
            )
        except RCONError:
            pass
        try:
            al = await _cached(
                srv.id,
                f"catalog_al_{mid}",
                lambda path_id=encoded_mid: srv.api.get(f"/v1/catalog/maps/{path_id}/alternators"),
            )
        except RCONError:
            pass
        by_map[mid] = {
            "display": m.get("displayName", mid),
            "experiences": list(ex.get("experiences") or [])[:100] if isinstance(ex, dict) else [],
            "alternators": [
                {"tag": a.get("tag"), "display": a.get("displayName", a.get("tag"))}
                for a in (al.get("alternators") or [])[:100]
                if isinstance(al, dict) and isinstance(a, dict) and a.get("tag")
            ],
        }
    light_rows = lightings.get("lightings") if isinstance(lightings, dict) else []
    return {
        "ok": True,
        "maps": by_map,
        "lightings": [l.get("id") for l in (light_rows or [])[:200] if isinstance(l, dict) and l.get("id")],
    }


# ---------- только для авторизованных с правами ----------

async def _require_perm(request: Request, perm: str):
    """Требует: вход через Steam + привязку Discord + право perm у роли."""
    user = await _current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="нужен вход через Steam")
    me = await _user_status(user)
    if perm not in me["permissions"]:
        raise HTTPException(status_code=403, detail="недостаточно прав")
    return user


# ---------- права ролей (настройка панели) ----------

@router.get("/roles")
async def roles_list(request: Request):
    await _require_perm(request, "roles")
    role_map = await dmod.guild_roles()
    perms_map = await db.get_role_perms_all()
    roles = []
    for rid, name in sorted(role_map.items()):
        perms = perms_map.get(rid)
        configured = rid in perms_map
        if perms is None and not configured:
            perms = perms_map.get(name)
            configured = name in perms_map
        roles.append({
            "id": rid,
            "name": name,
            "perms": perms or [],
            "configured": configured,
            "legacy": rid in config.ADMIN_ROLE_IDS or name in config.ADMIN_ROLE_NAMES,
        })
    return {"ok": True, "roles": roles, "all_permissions": sorted(dmod.ALL_PERMS)}


def _staff_group(role_names, permissions):
    """Keep the staff page useful even when Discord role names differ."""
    text = " ".join(role_names).lower()
    if any(word in text for word in ("разработ", "developer", "dev")):
        return "Разработчики"
    if any(word in text for word in ("основател", "owner", "founder")):
        return "Основатели"
    if any(word in text for word in ("админ", "admin")):
        return "Администраторы"
    if any(word in text for word in ("модер", "moderator", "mod")):
        return "Модераторы"
    if "roles" in permissions or "config_edit" in permissions:
        return "Администраторы"
    return "Сотрудники"


@router.get("/staff")
async def staff_list(request: Request):
    """Permission-protected staff directory for the administration screen."""
    user = await _require_perm(request, "roles")
    del user
    role_map = await dmod.guild_roles()
    perms_map = await db.get_role_perms_all()
    staff = []
    for item in await db.list_staff_users():
        role_names = [role_map.get(role_id, role_id) for role_id in item["role_ids"]]
        binding = {
            "guild_member": True,
            "roles": item["role_ids"],
        }
        permissions = dmod.compute_permissions(binding, role_map, perms_map)
        staff.append(
            {
                **item,
                "role_names": role_names[:20],
                "group": _staff_group(role_names, permissions),
                "permissions": permissions,
            }
        )
    return {"ok": True, "count": len(staff), "staff": staff}


@router.get("/discord/bots")
async def discord_bots_list(request: Request):
    await _require_perm(request, "roles")
    items = [_public_bot(item) for item in await db.list_bot_integrations()]
    return {"ok": True, "items": items, "capacity": 64, "scopes": sorted(BOT_SCOPES)}


@router.post("/discord/bots")
async def discord_bots_create(request: Request):
    user = await _require_perm(request, "roles")
    body = await _json_object(request)
    name = str(body.get("name") or "").strip()[:80]
    requested_id = str(body.get("bot_id") or "").strip().lower()
    bot_id = requested_id or re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")[:40]
    if not name or not BOT_ID_RE.fullmatch(bot_id):
        raise HTTPException(status_code=400, detail="Укажите имя и ID: 3–48 латинских символов")
    if len(await db.list_bot_integrations()) >= 64:
        raise HTTPException(status_code=409, detail="Достигнут запас реестра: 64 бота")
    guild_id = str(body.get("guild_id") or "").strip()
    if guild_id and not re.fullmatch(r"\d{5,20}", guild_id):
        raise HTTPException(status_code=400, detail="Некорректный Discord Guild ID")
    base_url = str(body.get("base_url") or "").strip().rstrip("/")[:500]
    if base_url:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise HTTPException(status_code=400, detail="Некорректный URL бота")
    scopes = sorted(set(str(value) for value in (body.get("scopes") or [])) & BOT_SCOPES)
    if not scopes:
        scopes = ["events.write", "heartbeat"]
    token = f"wdb_{bot_id}_{secrets.token_urlsafe(32)}"
    try:
        item = await db.create_bot_integration(
            bot_id, name, hashlib.sha256(token.encode("utf-8")).hexdigest(), scopes, guild_id, base_url
        )
    except Exception as exc:
        if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
            raise HTTPException(status_code=409, detail="Бот с таким ID уже существует") from exc
        raise
    await db.log_site_audit(user["steam_id"], None, "discord.bot.create", bot_id)
    return {"ok": True, "item": _public_bot(item), "token": token, "token_notice": "Сохраните токен: повторно он не показывается"}


@router.patch("/discord/bots/{bot_id}")
async def discord_bots_update(bot_id: str, request: Request):
    user = await _require_perm(request, "roles")
    if not BOT_ID_RE.fullmatch(bot_id):
        raise HTTPException(status_code=400, detail="Некорректный ID бота")
    body = await _json_object(request)
    changes = {}
    for key, limit in (("name", 80), ("guild_id", 20), ("bot_user_id", 20), ("base_url", 500)):
        if key in body:
            changes[key] = str(body.get(key) or "").strip()[:limit]
    if "enabled" in body:
        changes["enabled"] = bool(body["enabled"])
        if not changes["enabled"]:
            changes.update(status="disabled", detail="отключён администратором")
    if "scopes" in body:
        changes["scopes"] = sorted(set(str(value) for value in (body.get("scopes") or [])) & BOT_SCOPES)
    item = await db.update_bot_integration(bot_id, **changes)
    if not item:
        raise HTTPException(status_code=404, detail="Бот не найден")
    await db.log_site_audit(user["steam_id"], None, "discord.bot.update", bot_id)
    return {"ok": True, "item": _public_bot(item)}


@router.post("/discord/bots/{bot_id}/rotate-token")
async def discord_bot_rotate_token(bot_id: str, request: Request):
    user = await _require_perm(request, "roles")
    item = await db.get_bot_integration(bot_id)
    if not item:
        raise HTTPException(status_code=404, detail="Бот не найден")
    token = f"wdb_{bot_id}_{secrets.token_urlsafe(32)}"
    await db.update_bot_integration(bot_id, token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(), status="pending", detail="токен обновлён")
    await db.log_site_audit(user["steam_id"], None, "discord.bot.token.rotate", bot_id)
    return {"ok": True, "token": token, "token_notice": "Старый токен уже недействителен"}


@router.delete("/discord/bots/{bot_id}")
async def discord_bots_delete(bot_id: str, request: Request):
    user = await _require_perm(request, "roles")
    if not BOT_ID_RE.fullmatch(bot_id):
        raise HTTPException(status_code=400, detail="Некорректный ID бота")
    if not await db.get_bot_integration(bot_id):
        raise HTTPException(status_code=404, detail="Бот не найден")
    await db.delete_bot_integration(bot_id)
    await db.log_site_audit(user["steam_id"], None, "discord.bot.delete", bot_id)
    return {"ok": True}


@router.get("/discord/bots/{bot_id}/events")
async def discord_bot_events(bot_id: str, request: Request, limit: int = 100):
    await _require_perm(request, "roles")
    if not await db.get_bot_integration(bot_id):
        raise HTTPException(status_code=404, detail="Бот не найден")
    return {"ok": True, "items": await db.list_bot_events(bot_id, limit)}


@router.post("/bot/v1/heartbeat")
async def bot_v1_heartbeat(request: Request):
    item = await _require_bot(request, "heartbeat")
    body = await _json_object(request)
    metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    encoded = json.dumps(metadata, ensure_ascii=False)
    if len(encoded.encode("utf-8")) > 8192:
        raise HTTPException(status_code=413, detail="Слишком большие метаданные")
    updated = await db.update_bot_integration(
        item["bot_id"], status="online", detail=str(body.get("detail") or "")[:500],
        version=str(body.get("version") or "")[:80], bot_user_id=str(body.get("bot_user_id") or item["bot_user_id"])[:20],
        metadata=metadata, last_seen_utc=time.time(),
    )
    return {"ok": True, "bot": _public_bot(updated), "server_time": time.time(), "api_version": 1}


@router.post("/bot/v1/events")
async def bot_v1_events(request: Request):
    item = await _require_bot(request, "events.write")
    body = await _json_object(request)
    event_id = str(body.get("event_id") or "").strip()[:96]
    event_type = str(body.get("event_type") or "").strip().lower()[:80]
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
    if not event_id or not BOT_EVENT_RE.fullmatch(event_type):
        raise HTTPException(status_code=400, detail="Нужны event_id и корректный event_type")
    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > 32768:
        raise HTTPException(status_code=413, detail="Событие слишком большое")
    created = await db.append_bot_event(item["bot_id"], event_id, event_type, payload)
    await db.update_bot_integration(item["bot_id"], status="online", last_seen_utc=time.time())
    return {"ok": True, "accepted": created, "duplicate": not created}


@router.get("/discord/overview")
async def discord_overview(request: Request):
    """Единая диагностика привязок, ролей и голосовых комнат Discord."""
    await _require_perm(request, "roles")
    role_map = await dmod.guild_roles()
    perms_map = await db.get_role_perms_all()
    bindings = []
    diagnostics = []
    for item in await db.list_bindings():
        role_names = [role_map.get(str(role_id), str(role_id)) for role_id in item["role_ids"]]
        permissions = dmod.compute_permissions(
            {"guild_member": item["guild_member"], "roles": item["role_ids"]},
            role_map,
            perms_map,
        )
        enriched = {**item, "role_names": role_names, "permissions": permissions}
        bindings.append(enriched)
        problems = []
        if not item["guild_member"]:
            problems.append("пользователь не состоит в гильдии")
        if item["guild_member"] and not item["role_ids"]:
            problems.append("нет Discord-ролей")
        if item["guild_member"] and item["role_ids"] and not permissions:
            problems.append("роли не дают прав панели")
        if time.time() - float(item["updated_utc"] or 0) > config.ROLES_TTL_SECONDS * 2:
            problems.append("данные ролей устарели")
        if problems:
            diagnostics.append({**enriched, "problems": problems})
    voice_channels = await dmod.guild_voice_channels()
    tempvoice, tempvoice_error = await dmod.bot_panel_request("GET", "/api/tempvoice")
    actions = [
        row for row in await db.get_site_audit(200)
        if str(row.get("event") or "").startswith(("roles.", "auth.discord", "discord."))
    ]
    bots = [_public_bot(item) for item in await db.list_bot_integrations()]
    return {
        "ok": True,
        "bindings": bindings,
        "diagnostics": diagnostics,
        "voice_channels": voice_channels,
        "tempvoice": tempvoice or {"rooms": [], "members": []},
        "tempvoice_error": tempvoice_error,
        "actions": actions,
        "bots": bots,
        "summary": {
            "bindings": len(bindings), "problems": len(diagnostics),
            "voice_channels": len(voice_channels), "configured_roles": len(perms_map),
            "bots": len(bots), "bots_online": sum(1 for item in bots if item["online"]),
        },
    }


@router.post("/discord/tempvoice/{channel_id}/delete")
async def discord_tempvoice_delete(channel_id: str, request: Request):
    user = await _require_perm(request, "roles")
    if not re.fullmatch(r"\d{1,20}", channel_id):
        raise HTTPException(status_code=400, detail="Некорректный ID комнаты")
    data, error = await dmod.bot_panel_request("POST", f"/api/tempvoice/{channel_id}/delete", {})
    if error:
        raise HTTPException(status_code=502, detail=error)
    await db.log_site_audit(user["steam_id"], None, "discord.tempvoice.delete", channel_id)
    return data


@router.post("/discord/tempvoice/{channel_id}/transfer")
async def discord_tempvoice_transfer(channel_id: str, request: Request):
    user = await _require_perm(request, "roles")
    body = await _json_object(request)
    owner_id = str(body.get("owner_id") or "")
    if not re.fullmatch(r"\d{1,20}", channel_id) or not re.fullmatch(r"\d{1,20}", owner_id):
        raise HTTPException(status_code=400, detail="Некорректные ID комнаты или владельца")
    data, error = await dmod.bot_panel_request(
        "POST", f"/api/tempvoice/{channel_id}/transfer", {"owner_id": owner_id}
    )
    if error:
        raise HTTPException(status_code=502, detail=error)
    await db.log_site_audit(
        user["steam_id"], None, "discord.tempvoice.transfer", f"{channel_id}:{owner_id}"
    )
    return data


@router.get("/server/{sid}/bans/export")
async def bans_export(sid: str, request: Request):
    await _require_perm(request, "ban_view")
    if not _get_server(sid):
        return _not_found(sid)
    rows = await db.list_managed_bans(sid)
    return _csv_response(
        f"bans-{sid}.csv",
        ["server_id", "steam_id", "display_name", "reason", "expires_utc", "created_by", "created_utc", "sync_state"],
        rows,
    )


@router.get("/server/{sid}/vip-slots/history")
async def vip_history(sid: str, request: Request):
    await _require_any_perm(request, {"vip_view", "vip_edit"})
    rows = [
        row for row in await db.get_site_audit(500)
        if row.get("server_id") == sid and str(row.get("event") or "").startswith("vip.")
    ]
    return {"ok": True, "items": rows}


@router.put("/roles")
async def roles_put(request: Request):
    user = await _require_perm(request, "roles")
    body = await _json_object(request)
    role_ref = str(body.get("role_ref") or "").strip()[:128]
    if not role_ref:
        return JSONResponse({"error": {"code": "missing_field", "message": "поле role_ref"}}, status_code=400)
    raw_perms = body.get("perms") or []
    if not isinstance(raw_perms, list):
        raise HTTPException(status_code=400, detail="поле perms должно быть массивом")
    perms = [p for p in raw_perms if isinstance(p, str) and p in dmod.ALL_PERMS]
    idem_key, replay = await _idempotency_begin(request, user["steam_id"], _request_fingerprint(request, body))
    if replay:
        return replay
    old_perms = await db.get_role_perms(role_ref) or []
    try:
        await db.set_role_perms(role_ref, perms)
        added = sorted(set(perms) - set(old_perms))
        removed = sorted(set(old_perms) - set(perms))
        await db.log_site_audit(
            user["steam_id"], None, "roles.update",
            json.dumps({"role_ref": role_ref, "before": old_perms, "after": perms, "added": added, "removed": removed}, ensure_ascii=False),
        )
    except Exception:
        await _idempotency_abort(user["steam_id"], idem_key)
        raise
    log.info("Права роли %s обновлены: %s", role_ref, perms)
    response = {"ok": True, "role_ref": role_ref, "perms": perms}
    await _idempotency_complete(user["steam_id"], idem_key, response)
    return response


@router.get("/roles/{role_ref}/history")
async def role_history(role_ref: str, request: Request):
    await _require_perm(request, "roles")
    role_ref = str(role_ref or "")[:128]
    items = []
    for row in await db.get_site_audit(1000):
        if row.get("event") != "roles.update":
            continue
        detail = str(row.get("detail") or "")
        try:
            parsed = json.loads(detail)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = {"role_ref": detail.split(":", 1)[0], "legacy": detail}
        if parsed.get("role_ref") == role_ref:
            items.append({**row, "change": parsed})
    return {"ok": True, "items": items}


@router.get("/roles/preview/user/{steam_id}")
async def role_user_preview(steam_id: str, request: Request):
    await _require_perm(request, "roles")
    steam_id = _validate_steam_id(steam_id)
    binding = await db.get_binding(steam_id)
    if not binding:
        raise HTTPException(status_code=404, detail="Discord не привязан к этому игроку")
    role_map = await dmod.guild_roles()
    perms_map = await db.get_role_perms_all()
    permissions = dmod.compute_permissions(binding, role_map, perms_map)
    return {
        "ok": True, "steam_id": steam_id,
        "discord_id": binding.get("discord_id"),
        "discord_name": binding.get("discord_name"),
        "roles": [{"id": role_id, "name": role_map.get(role_id, role_id)} for role_id in binding.get("roles") or []],
        "permissions": permissions,
    }


@router.get("/server/{sid}/audit")
async def server_audit(sid: str, request: Request, limit: int = 50):
    await _require_perm(request, "audit")
    srv = _get_server(sid)
    if not srv:
        return _not_found(sid)
    limit = max(1, min(limit, 500))
    try:
        data = await srv.api.get("/v1/audit", params={"limit": limit})
    except RCONError as e:
        return _rcon_error(e)
    return {"ok": True, "entries": data.get("entries") or []}


@router.get("/audit/site")
async def site_audit_list(request: Request, limit: int = 100):
    await _require_perm(request, "audit")
    return {"ok": True, "entries": await db.get_site_audit(limit)}


@router.get("/audit/integrity")
async def site_audit_integrity(request: Request):
    await _require_perm(request, "audit")
    return {"ok": True, "audit": await db.verify_site_audit_chain(force=True)}


def _parse_vip_expiry(value):
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            raise HTTPException(status_code=400, detail="Некорректная дата окончания VIP") from None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        timestamp = parsed.timestamp()
    if timestamp <= time.time() + 60:
        raise HTTPException(status_code=400, detail="Окончание VIP должно быть в будущем")
    if timestamp > time.time() + 10 * 365 * 86400:
        raise HTTPException(status_code=400, detail="Срок VIP не может превышать 10 лет")
    return timestamp


async def _apply_reserved_access(srv: Server, steam_id: str, add: bool):
    """Apply one VIP entitlement through native reserved slots or config fallback."""
    await srv.ensure_caps()
    native_supported = (
        "POST /v1/reserved-slots" in srv.routes
        if add
        else any(route.startswith("DELETE /v1/reserved-slots/") for route in srv.routes)
    )
    config_supported = (
        srv.config_writable
        and "GET /v1/config" in srv.routes
        and "PUT /v1/config" in srv.routes
    )
    if not (native_supported or config_supported):
        raise HTTPException(status_code=501, detail="Сервер не поддерживает изменение резервных игроков")
    if native_supported:
        method = "POST" if add else "DELETE"
        path = "/v1/reserved-slots" if add else f"/v1/reserved-slots/{steam_id}"
        try:
            data = await srv.api.request(
                method,
                path,
                json_body={"steamId": steam_id} if add else None,
            )
        except RCONError as exc:
            # Native implementations commonly report an already-applied
            # idempotent state as 409/404. For VIP reconciliation that state
            # is the desired outcome.
            if (add and exc.status == 409) or (not add and exc.status == 404):
                data = {"ok": True, "unchanged": True}
            else:
                raise
    else:
        status, data = await _reserved_config_update(srv, steam_id, add)
        if status != 200:
            message = data.get("error", {}).get("message") if isinstance(data, dict) else str(data)
            raise RCONError(status, "reserved_sync", message or "Не удалось синхронизировать VIP")
    await _invalidate(srv.id)
    return data


async def _sync_vip_record(item, add=True):
    srv = _get_server(item["server_id"])
    if not srv:
        await db.mark_vip_sync(item["id"], "error", "Сервер не найден")
        return False, "Сервер не найден"
    try:
        await _apply_reserved_access(srv, item["steam_id"], add)
    except Exception as exc:
        message = exc.detail if isinstance(exc, HTTPException) else (
            exc.message if isinstance(exc, RCONError) else type(exc).__name__
        )
        await db.mark_vip_sync(item["id"], "error", message)
        return False, str(message)
    state = "synced" if add else "expired"
    await db.mark_vip_sync(item["id"], state, "")
    return True, ""


async def expire_vip_slots():
    """Background-safe expiry pass called by the application maintenance loop."""
    for item in await db.list_expired_vip_slots():
        ok, error = await _sync_vip_record(item, add=False)
        if ok:
            await db.log_site_audit(None, item["server_id"], "vip.expire", item["steam_id"])
        else:
            log.warning("VIP expiry sync failed for %s/%s: %s", item["server_id"], item["steam_id"], error)


async def expire_managed_bans():
    """Remove expired panel-managed bans from their upstream servers."""
    for item in await db.list_expired_managed_bans():
        srv = _get_server(item["server_id"])
        if not srv:
            await db.mark_managed_ban(item["id"], "error", "Сервер не найден")
            continue
        try:
            await srv.api.request("DELETE", f"/v1/bans/{item['steam_id']}")
        except RCONError as exc:
            if exc.status != 404:
                await db.mark_managed_ban(item["id"], "error", exc.message)
                log.warning(
                    "Timed ban expiry failed for %s/%s: %s",
                    item["server_id"], item["steam_id"], exc.message,
                )
                continue
        await db.mark_managed_ban(item["id"], "expired", "")
        await _invalidate(srv.id)
        await db.log_site_audit(None, item["server_id"], "ban.expire", item["steam_id"])


@router.get("/server/{sid}/vip-slots")
async def vip_slots_list(sid: str, request: Request):
    await _require_any_perm(request, {"vip_view", "vip_edit"})
    if not _get_server(sid):
        return _not_found(sid)
    now = time.time()
    items = await db.list_vip_slots(sid)
    for item in items:
        item["active"] = item["expires_utc"] is None or item["expires_utc"] > now
    return {"ok": True, "items": items, "count": len(items)}


@router.post("/server/{sid}/vip-slots")
async def vip_slot_save(sid: str, request: Request):
    user = await _require_perm(request, "vip_edit")
    if not _get_server(sid):
        return _not_found(sid)
    body = await _json_object(request)
    steam_id = _validate_steam_id(body.get("steam_id") or body.get("steamId"))
    display_name = " ".join(str(body.get("display_name") or "").split())[:120]
    note = str(body.get("note") or "").strip()[:500]
    expires_utc = _parse_vip_expiry(body.get("expires_utc"))
    idem_key, replay = await _idempotency_begin(
        request, user["steam_id"], _request_fingerprint(request, body)
    )
    if replay:
        return replay
    item = await db.save_vip_slot(
        sid, steam_id, display_name, expires_utc, note, user["steam_id"]
    )
    synced, error = await _sync_vip_record(item, add=True)
    item = await db.get_vip_slot(item["id"], sid)
    await db.log_site_audit(
        user["steam_id"], sid, "vip.save", f"{steam_id}:{'synced' if synced else 'error'}"
    )
    response = {"ok": True, "item": item, "synced": synced, "warning": error or None}
    await _idempotency_complete(user["steam_id"], idem_key, response)
    return response


@router.post("/server/{sid}/vip-slots/{vip_id}/sync")
async def vip_slot_sync(sid: str, vip_id: str, request: Request):
    user = await _require_perm(request, "vip_edit")
    if not re.fullmatch(r"[0-9a-f]{24}", str(vip_id or "")):
        raise HTTPException(status_code=400, detail="Некорректный ID VIP")
    item = await db.get_vip_slot(vip_id, sid)
    if not item:
        raise HTTPException(status_code=404, detail="VIP-запись не найдена")
    active = item["expires_utc"] is None or item["expires_utc"] > time.time()
    synced, error = await _sync_vip_record(item, add=active)
    await db.log_site_audit(
        user["steam_id"], sid, "vip.sync", f"{item['steam_id']}:{'ok' if synced else 'error'}"
    )
    return {
        "ok": synced,
        "item": await db.get_vip_slot(vip_id, sid),
        "detail": error or None,
    }


@router.delete("/server/{sid}/vip-slots/{vip_id}")
async def vip_slot_delete(sid: str, vip_id: str, request: Request):
    user = await _require_perm(request, "vip_edit")
    if not re.fullmatch(r"[0-9a-f]{24}", str(vip_id or "")):
        raise HTTPException(status_code=400, detail="Некорректный ID VIP")
    item = await db.get_vip_slot(vip_id, sid)
    if not item:
        raise HTTPException(status_code=404, detail="VIP-запись не найдена")
    synced, error = await _sync_vip_record(item, add=False)
    if not synced:
        return JSONResponse(
            {"ok": False, "detail": error, "item": await db.get_vip_slot(vip_id, sid)},
            status_code=502,
        )
    await db.delete_vip_slot(vip_id, sid)
    await db.log_site_audit(user["steam_id"], sid, "vip.delete", item["steam_id"])
    return {"ok": True}


@router.get("/server/{sid}/reserved-slots")
async def reserved_slots_list(sid: str, request: Request):
    await _require_perm(request, "reserved_view")
    srv = _get_server(sid)
    if not srv:
        return _not_found(sid)
    await srv.ensure_caps()
    try:
        data = await srv.api.get("/v1/reserved-slots")
    except RCONError as e:
        return _rcon_error(e)
    slots = [str(x) for x in (data.get("reservedSlots") or [])]
    max_reserved_slots = None
    max_players = None
    if srv.config_writable and "GET /v1/config" in srv.routes:
        try:
            cfg_status, cfg_data = await srv.api.request_raw("GET", "/v1/config")
            if cfg_status == 200 and isinstance(cfg_data, dict):
                cfg_text = str(cfg_data.get("text") or "")
                for key, target in (("MaxReservedSlots", "reserved"), ("MaxPlayers", "players")):
                    match = re.search(rf"(?im)^\s*{key}\s*=\s*(\d+)\s*$", cfg_text)
                    if match:
                        if target == "reserved":
                            max_reserved_slots = int(match.group(1))
                        else:
                            max_players = int(match.group(1))
        except RCONError:
            pass
    config_live = srv.config_writable and "GET /v1/config" in srv.routes and "PUT /v1/config" in srv.routes
    return {"ok": True, "reserved_slots": slots, "count": data.get("count", len(slots)),
            "max_reserved_slots": max_reserved_slots, "max_players": max_players,
            "limit_supported": config_live,
            "add_supported": "POST /v1/reserved-slots" in srv.routes or config_live,
            "remove_supported": any(r.startswith("DELETE /v1/reserved-slots/") for r in srv.routes) or config_live,
            "player_list_live_via_config": config_live}


async def _reserved_config_update(srv: Server, steam_id: str, add: bool):
    """Fallback для сборок без POST/DELETE reserved-slots.

    PUT /v1/config у актуального Dark RCON применяет DefaultReservedPlayerIds
    в работающий сервер, поэтому рестарт процесса не нужен.
    """
    status, cfg = await srv.api.request_raw("GET", "/v1/config")
    if status != 200 or not isinstance(cfg, dict):
        return status, cfg
    text = str(cfg.get("text") or "")
    entry_re = re.compile(rf"(?im)^\s*\.DefaultReservedPlayerIds\s*=\s*{re.escape(steam_id)}\s*(?:\r?\n|$)")
    exists = bool(entry_re.search(text))
    if add and exists:
        return 200, {"ok": True, "unchanged": True}
    if not add and not exists:
        return 200, {"ok": True, "unchanged": True}
    if add:
        newline = "\r\n" if "\r\n" in text else "\n"
        clear_re = re.compile(r"(?im)^\s*!DefaultReservedPlayerIds\s*=\s*ClearArray\s*$")
        match = clear_re.search(text)
        entry = f".DefaultReservedPlayerIds={steam_id}"
        if match:
            updated = text[:match.end()] + newline + entry + text[match.end():]
        else:
            updated = text.rstrip("\r\n") + f"{newline}!DefaultReservedPlayerIds=ClearArray{newline}{entry}{newline}"
    else:
        updated = entry_re.sub("", text, count=1)
    revision = str(cfg.get("revision") or "").strip()
    headers = {"If-Match": f'"{revision}"'} if revision else {}
    return await srv.api.request_raw(
        "PUT", "/v1/config", raw_body=updated, content_type="text/plain", extra_headers=headers
    )


@router.put("/server/{sid}/reserved-slots/limit")
async def reserved_slots_limit(sid: str, request: Request):
    """Меняет MaxReservedSlots через живое применение конфигурации."""
    user = await _require_perm(request, "reserved_edit")
    srv = _get_server(sid)
    if not srv:
        return _not_found(sid)
    try:
        body = await _json_object(request)
        value = int(body.get("max_reserved_slots"))
    except (TypeError, ValueError, AttributeError, HTTPException):
        raise HTTPException(status_code=400, detail="Количество резервных слотов должно быть целым числом")
    if value < 0 or value > 100:
        raise HTTPException(status_code=400, detail="Количество резервных слотов должно быть от 0 до 100")
    idem_key, replay = await _idempotency_begin(request, user["steam_id"], _request_fingerprint(request, body))
    if replay:
        return replay
    try:
        await srv.ensure_caps()
        if not (srv.config_writable and "GET /v1/config" in srv.routes and "PUT /v1/config" in srv.routes):
            await _idempotency_abort(user["steam_id"], idem_key)
            raise HTTPException(status_code=501, detail="Сервер не поддерживает живое изменение резервных слотов")
        status, cfg = await srv.api.request_raw("GET", "/v1/config")
        if status != 200 or not isinstance(cfg, dict):
            await _idempotency_abort(user["steam_id"], idem_key)
            return JSONResponse(cfg if isinstance(cfg, dict) else {"error": {"message": "Не удалось прочитать конфиг"}}, status_code=status)
        text = str(cfg.get("text") or "")
        max_match = re.search(r"(?im)^\s*MaxPlayers\s*=\s*(\d+)\s*$", text)
        if max_match and value > int(max_match.group(1)):
            await _idempotency_abort(user["steam_id"], idem_key)
            raise HTTPException(status_code=400, detail="Резерв не может превышать максимальное число игроков")
        pattern = re.compile(r"(?im)^(\s*MaxReservedSlots\s*=\s*)\d+(\s*)$")
        if pattern.search(text):
            updated = pattern.sub(lambda m: f"{m.group(1)}{value}{m.group(2)}", text, count=1)
        else:
            newline = "\r\n" if "\r\n" in text else "\n"
            updated = text.rstrip("\r\n") + f"{newline}MaxReservedSlots={value}{newline}"
    except RCONError as e:
        await _idempotency_abort(user["steam_id"], idem_key)
        return _rcon_error(e)
    if updated == text:
        response = {"ok": True, "max_reserved_slots": value, "unchanged": True}
        await _idempotency_complete(user["steam_id"], idem_key, response)
        return response

    try:
        revision = str(cfg.get("revision") or "").strip()
        headers = {"If-Match": f'"{revision}"'} if revision else {}
        put_status, result = await srv.api.request_raw(
            "PUT", "/v1/config", raw_body=updated, content_type="text/plain", extra_headers=headers
        )
    except RCONError as e:
        await _idempotency_abort(user["steam_id"], idem_key)
        return _rcon_error(e)
    if put_status != 200:
        await _idempotency_abort(user["steam_id"], idem_key)
        return JSONResponse(result if isinstance(result, dict) else {"error": {"message": str(result)}}, status_code=put_status)
    await _invalidate(srv.id)
    await db.log_site_audit(user["steam_id"], sid, "reserved.limit", f"MaxReservedSlots={value}")
    response = {"ok": True, "max_reserved_slots": value, "result": result}
    await _idempotency_complete(user["steam_id"], idem_key, response)
    return response


@router.post("/server/{sid}/reserved-slots")
async def reserved_slots_add(sid: str, request: Request):
    user = await _require_perm(request, "reserved_edit")
    srv = _get_server(sid)
    if not srv:
        return _not_found(sid)
    body = await _json_object(request)
    steam_id = _validate_steam_id(body.get("steam_id") or body.get("steamId"))
    idem_key, replay = await _idempotency_begin(request, user["steam_id"], _request_fingerprint(request, body))
    if replay:
        return replay
    try:
        await srv.ensure_caps()
        native_supported = "POST /v1/reserved-slots" in srv.routes
        config_supported = srv.config_writable and "GET /v1/config" in srv.routes and "PUT /v1/config" in srv.routes
        if not (native_supported or config_supported):
            await _idempotency_abort(user["steam_id"], idem_key)
            raise HTTPException(status_code=501, detail="Сервер не поддерживает изменение списка резервных игроков")
        if native_supported:
            data = await srv.api.request("POST", "/v1/reserved-slots", json_body={"steamId": steam_id})
        else:
            status, data = await _reserved_config_update(srv, steam_id, True)
            if status != 200:
                await _idempotency_abort(user["steam_id"], idem_key)
                return JSONResponse(data if isinstance(data, dict) else {"error": {"message": str(data)}}, status_code=status)
    except RCONError as e:
        await _idempotency_abort(user["steam_id"], idem_key)
        return _rcon_error(e)
    await _invalidate(srv.id)
    await db.log_site_audit(user["steam_id"], sid, "reserved.add", steam_id)
    response = {"ok": True, "result": data}
    await _idempotency_complete(user["steam_id"], idem_key, response)
    return response


@router.delete("/server/{sid}/reserved-slots/{steam_id}")
async def reserved_slots_remove(sid: str, steam_id: str, request: Request):
    user = await _require_perm(request, "reserved_edit")
    srv = _get_server(sid)
    if not srv:
        return _not_found(sid)
    steam_id = _validate_steam_id(steam_id)
    idem_key, replay = await _idempotency_begin(request, user["steam_id"], _request_fingerprint(request, {}))
    if replay:
        return replay
    try:
        await srv.ensure_caps()
        native_supported = any(r.startswith("DELETE /v1/reserved-slots/") for r in srv.routes)
        config_supported = srv.config_writable and "GET /v1/config" in srv.routes and "PUT /v1/config" in srv.routes
        if not (native_supported or config_supported):
            await _idempotency_abort(user["steam_id"], idem_key)
            raise HTTPException(status_code=501, detail="Сервер не поддерживает изменение списка резервных игроков")
        if native_supported:
            data = await srv.api.request("DELETE", f"/v1/reserved-slots/{steam_id}")
        else:
            status, data = await _reserved_config_update(srv, steam_id, False)
            if status != 200:
                await _idempotency_abort(user["steam_id"], idem_key)
                return JSONResponse(data if isinstance(data, dict) else {"error": {"message": str(data)}}, status_code=status)
    except RCONError as e:
        await _idempotency_abort(user["steam_id"], idem_key)
        return _rcon_error(e)
    await _invalidate(srv.id)
    await db.log_site_audit(user["steam_id"], sid, "reserved.remove", steam_id)
    response = {"ok": True, "result": data}
    await _idempotency_complete(user["steam_id"], idem_key, response)
    return response


@router.get("/server/{sid}/config")
async def server_config_get(sid: str, request: Request):
    await _require_perm(request, "config_view")
    srv = _get_server(sid)
    if not srv:
        return _not_found(sid)
    try:
        status, data = await srv.api.request_raw("GET", "/v1/config")
    except RCONError as e:
        return _rcon_error(e)
    if status >= 400:
        return JSONResponse(data if isinstance(data, dict) else {"error": {"code": "http", "message": str(data)}}, status_code=status)
    return {"ok": True, "revision": data.get("revision"), "writable": data.get("writable"), "text": data.get("text"), "warnings": data.get("warnings") or []}


@router.put("/server/{sid}/config")
async def server_config_put(sid: str, request: Request):
    user = await _require_perm(request, "config_edit")
    srv = _get_server(sid)
    if not srv:
        return _not_found(sid)
    raw_body = await request.body()
    if len(raw_body) > config.MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="конфиг слишком большой")
    body = raw_body.decode("utf-8", errors="replace")
    if not body.strip():
        return JSONResponse({"error": {"code": "empty", "message": "пустой конфиг"}}, status_code=400)
    revision = (request.headers.get("If-Match") or "").strip().strip('"')
    extra = {"If-Match": f'"{revision}"'} if revision else {}
    idem_key, replay = await _idempotency_begin(
        request,
        user["steam_id"],
        hashlib.sha256(
            f"{request.method}:{request.url.path}:{request.headers.get('If-Match', '')}:".encode()
            + raw_body
        ).hexdigest(),
    )
    if replay:
        return replay
    try:
        status, data = await srv.api.request_raw(
            "PUT", "/v1/config", raw_body=body, content_type="text/plain", extra_headers=extra
        )
    except RCONError as e:
        await _idempotency_abort(user["steam_id"], idem_key)
        return _rcon_error(e)
    if status == 200:
        await _invalidate(srv.id)
        await db.log_site_audit(user["steam_id"], sid, "config.put", f"bytes={len(body)}")
    if status == 200 and isinstance(data, dict):
        await _idempotency_complete(user["steam_id"], idem_key, data, status)
        return data
    await _idempotency_abort(user["steam_id"], idem_key)
    return JSONResponse(data if isinstance(data, dict) else {"error": {"code": "http", "message": str(data)}}, status_code=status)


@router.post("/server/{sid}/broadcast")
async def server_broadcast(sid: str, request: Request):
    user = await _require_perm(request, "broadcast")
    srv = _get_server(sid)
    if not srv:
        return _not_found(sid)
    body = await _json_object(request)
    message = str(body.get("message") or "").strip()
    if not message or len(message) > 2000:
        return JSONResponse({"error": {"code": "missing_field", "message": "поле message"}}, status_code=400)
    idem_key, replay = await _idempotency_begin(request, user["steam_id"], _request_fingerprint(request, body))
    if replay:
        return replay
    if not await _allow_action_once(user["steam_id"], sid, "broadcast", message):
        await _idempotency_abort(user["steam_id"], idem_key)
        return JSONResponse({"error": {"code": "duplicate_action", "message": "Повторите действие через секунду"}}, status_code=409)
    try:
        await srv.api.request("POST", "/v1/broadcast", json_body={"message": message})
    except RCONError as e:
        await _idempotency_abort(user["steam_id"], idem_key)
        return _rcon_error(e)
    await db.log_site_audit(user["steam_id"], sid, "broadcast", f"bytes={len(message.encode('utf-8'))}")
    await _idempotency_complete(user["steam_id"], idem_key, {"ok": True})
    return {"ok": True}


@router.post("/server/{sid}/bans")
async def server_ban_add(sid: str, request: Request):
    user = await _require_perm(request, "ban_add")
    srv = _get_server(sid)
    if not srv:
        return _not_found(sid)
    body = await _json_object(request)
    try:
        steam_id = _validate_steam_id(body.get("steamId"))
    except HTTPException:
        return JSONResponse({"error": {"code": "invalid_steam_id", "message": "SteamID64 должен состоять из 17 цифр"}}, status_code=400)
    payload = {"steamId": steam_id}
    reason = str(body.get("reason") or "").strip()[:500]
    display_name = " ".join(str(body.get("display_name") or "").split())[:120]
    expires_utc = _parse_vip_expiry(body.get("expires_utc"))
    if reason:
        payload["reason"] = reason
    idem_key, replay = await _idempotency_begin(request, user["steam_id"], _request_fingerprint(request, body))
    if replay:
        return replay
    if not await _allow_action_once(user["steam_id"], sid, "ban.add", steam_id):
        await _idempotency_abort(user["steam_id"], idem_key)
        return JSONResponse({"error": {"code": "duplicate_action", "message": "Повторите действие через секунду"}}, status_code=409)
    try:
        await srv.api.request("POST", "/v1/bans", json_body=payload)
    except RCONError as e:
        if e.status != 409:
            await _idempotency_abort(user["steam_id"], idem_key)
            return _rcon_error(e)
    managed = await db.save_managed_ban(
        sid, steam_id, display_name, reason, expires_utc, user["steam_id"]
    )
    await _invalidate(srv.id)
    await db.log_site_audit(user["steam_id"], sid, "ban.add", steam_id)
    response = {"ok": True, "managed_ban": managed}
    await _idempotency_complete(user["steam_id"], idem_key, response)
    return response


@router.delete("/server/{sid}/bans/{steam_id}")
async def server_ban_del(sid: str, steam_id: str, request: Request):
    user = await _require_perm(request, "ban_remove")
    steam_id = _validate_steam_id(steam_id)
    srv = _get_server(sid)
    if not srv:
        return _not_found(sid)
    idem_key, replay = await _idempotency_begin(request, user["steam_id"], _request_fingerprint(request, {}))
    if replay:
        return replay
    if not await _allow_action_once(user["steam_id"], sid, "ban.remove", steam_id):
        await _idempotency_abort(user["steam_id"], idem_key)
        return JSONResponse({"error": {"code": "duplicate_action", "message": "Повторите действие через секунду"}}, status_code=409)
    try:
        await srv.api.request("DELETE", f"/v1/bans/{steam_id}")
    except RCONError as e:
        if e.status != 404:
            await _idempotency_abort(user["steam_id"], idem_key)
            return _rcon_error(e)
    await db.delete_managed_ban(sid, steam_id)
    await _invalidate(srv.id)
    await db.log_site_audit(user["steam_id"], sid, "ban.remove", steam_id)
    await _idempotency_complete(user["steam_id"], idem_key, {"ok": True})
    return {"ok": True}


@router.patch("/server/{sid}/bans/{steam_id}")
async def server_ban_update(sid: str, steam_id: str, request: Request):
    """Обновляет причину/срок через контролируемое снятие и повторную выдачу."""
    user = await _require_perm(request, "ban_add")
    await _require_perm(request, "ban_remove")
    steam_id = _validate_steam_id(steam_id)
    srv = _get_server(sid)
    if not srv:
        return _not_found(sid)
    current = next((row for row in await db.list_managed_bans(sid) if row["steam_id"] == steam_id), None)
    if not current:
        raise HTTPException(status_code=404, detail="Бан не управляется этой панелью")
    body = await _json_object(request)
    reason = str(body.get("reason", current["reason"]) or "").strip()[:500]
    expires_utc = _parse_vip_expiry(body.get("expires_utc")) if "expires_utc" in body else current["expires_utc"]
    try:
        await srv.api.request("DELETE", f"/v1/bans/{steam_id}")
        await srv.api.request("POST", "/v1/bans", json_body={"steamId": steam_id, "reason": reason})
    except RCONError as exc:
        try:
            await srv.api.request(
                "POST", "/v1/bans", json_body={"steamId": steam_id, "reason": current["reason"]}
            )
        except RCONError:
            log.exception("Не удалось восстановить бан %s после ошибки обновления", steam_id)
        return _rcon_error(exc)
    managed = await db.save_managed_ban(
        sid, steam_id, current["display_name"], reason, expires_utc, user["steam_id"]
    )
    await _invalidate(sid)
    await db.log_site_audit(user["steam_id"], sid, "ban.update", steam_id)
    return {"ok": True, "managed_ban": managed}


@router.post("/server/{sid}/bans/bulk")
async def server_bans_bulk(sid: str, request: Request):
    """Ограниченная последовательная пакетная операция с отчётом по каждой записи."""
    user = await _require_perm(request, "ban_remove")
    srv = _get_server(sid)
    if not srv:
        return _not_found(sid)
    body = await _json_object(request)
    action = str(body.get("action") or "")
    steam_ids = list(dict.fromkeys(str(value) for value in (body.get("steam_ids") or [])))
    if action != "unban" or not steam_ids or len(steam_ids) > 50:
        raise HTTPException(status_code=400, detail="Поддерживается снятие от 1 до 50 банов")
    steam_ids = [_validate_steam_id(value) for value in steam_ids]
    results = []
    for steam_id in steam_ids:
        try:
            await srv.api.request("DELETE", f"/v1/bans/{steam_id}")
            await db.delete_managed_ban(sid, steam_id)
            results.append({"steam_id": steam_id, "ok": True})
        except RCONError as exc:
            if exc.status == 404:
                await db.delete_managed_ban(sid, steam_id)
                results.append({"steam_id": steam_id, "ok": True, "already_absent": True})
            else:
                results.append({"steam_id": steam_id, "ok": False, "error": exc.message})
    await _invalidate(sid)
    succeeded = sum(1 for result in results if result["ok"])
    await db.log_site_audit(
        user["steam_id"], sid, "ban.bulk.remove", f"{succeeded}/{len(results)}"
    )
    return {"ok": succeeded == len(results), "succeeded": succeeded, "failed": len(results) - succeeded, "results": results}


async def _server_action(sid, request, perm, action, detail=""):
    """Обёртка действия панели: проверяет право perm и вызывает action(srv)."""
    user = await _require_perm(request, perm)
    srv = _get_server(sid)
    if not srv:
        return _not_found(sid)
    raw_body = await request.body()
    try:
        fingerprint_body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        fingerprint_body = {"raw_sha256": hashlib.sha256(raw_body).hexdigest()}
    idem_key, replay = await _idempotency_begin(
        request,
        user["steam_id"],
        _request_fingerprint(request, fingerprint_body),
    )
    if replay:
        return replay
    if not await _allow_action_once(user["steam_id"], sid, perm, detail):
        await _idempotency_abort(user["steam_id"], idem_key)
        return JSONResponse({"error": {"code": "duplicate_action", "message": "Повторите действие через секунду"}}, status_code=409)
    try:
        result = await action(srv)
    except RCONError as e:
        await _idempotency_abort(user["steam_id"], idem_key)
        return _rcon_error(e)
    if result is None:
        await _invalidate(srv.id)
        await db.log_site_audit(user["steam_id"], sid, f"rcon.{perm}", detail)
        await _idempotency_complete(user["steam_id"], idem_key, {"ok": True})
        return JSONResponse({"ok": True})
    if isinstance(result, JSONResponse) and result.status_code >= 400:
        await _idempotency_abort(user["steam_id"], idem_key)
    return result


@router.post("/server/{sid}/players/{steam_id}/kick")
async def server_kick(sid: str, steam_id: str, request: Request):
    steam_id = _validate_steam_id(steam_id)
    async def action(srv):
        body = await _json_object(request)
        payload = {}
        reason = str(body.get("reason") or "").strip()[:500]
        if reason:
            payload["reason"] = reason
        await srv.api.request("POST", f"/v1/players/{steam_id}/kick", json_body=payload)

    return await _server_action(sid, request, "kick", action, steam_id)


@router.post("/server/{sid}/players/{steam_id}/kill")
async def server_kill(sid: str, steam_id: str, request: Request):
    steam_id = _validate_steam_id(steam_id)
    async def action(srv):
        await srv.api.request("POST", f"/v1/players/{steam_id}/kill")

    return await _server_action(sid, request, "kill", action, steam_id)


@router.post("/server/{sid}/players/{steam_id}/message")
async def server_message(sid: str, steam_id: str, request: Request):
    steam_id = _validate_steam_id(steam_id)
    async def action(srv):
        body = await _json_object(request)
        message = str(body.get("message") or "").strip()
        if not message or len(message) > 2000:
            return JSONResponse({"error": {"code": "missing_field", "message": "поле message"}}, status_code=400)
        await srv.api.request("POST", f"/v1/players/{steam_id}/message", json_body={"message": message})

    return await _server_action(sid, request, "message", action, steam_id)


@router.patch("/server/{sid}/players/{steam_id}")
async def server_player_patch(sid: str, steam_id: str, request: Request):
    steam_id = _validate_steam_id(steam_id)
    async def action(srv):
        body = await _json_object(request)
        faction = str(body.get("faction") or "").strip()
        if not faction or len(faction) > 80:
            return JSONResponse({"error": {"code": "missing_field", "message": "поле faction"}}, status_code=400)
        await srv.api.request("PATCH", f"/v1/players/{steam_id}", json_body={"faction": faction})

    return await _server_action(sid, request, "move_faction", action, steam_id)


@router.post("/server/{sid}/match/map")
async def server_match_map(sid: str, request: Request):
    async def action(srv):
        body = await _json_object(request)
        map_id = str(body.get("map") or "").strip()
        if not map_id or len(map_id) > 128:
            return JSONResponse({"error": {"code": "missing_field", "message": "поле map"}}, status_code=400)
        payload = {"map": map_id}
        experiences = body.get("experiences")
        if isinstance(experiences, list):
            payload["experiences"] = [str(e).strip()[:128] for e in experiences[:20] if str(e).strip()]
        lighting = str(body.get("lighting") or "").strip()[:128]
        if lighting:
            payload["lighting"] = lighting
        zone = str(body.get("zoneAlternator") or "").strip()[:128]
        if zone:
            payload["zoneAlternator"] = zone
        await srv.api.request("POST", "/v1/match/map", json_body=payload)

    return await _server_action(sid, request, "change_map", action)


@router.post("/server/{sid}/match/end")
async def server_match_end(sid: str, request: Request):
    async def action(srv):
        await srv.api.request("POST", "/v1/match/end")

    return await _server_action(sid, request, "match_end", action)


@router.post("/server/{sid}/match/restart")
async def server_match_restart(sid: str, request: Request):
    async def action(srv):
        await srv.api.request("POST", "/v1/match/restart")

    return await _server_action(sid, request, "match_restart", action)


@router.put("/server/{sid}/world/lighting")
async def server_lighting(sid: str, request: Request):
    async def action(srv):
        body = await _json_object(request)
        lighting = str(body.get("lighting") or "").strip()
        if not lighting or len(lighting) > 128:
            return JSONResponse({"error": {"code": "missing_field", "message": "поле lighting"}}, status_code=400)
        await srv.api.request("PUT", "/v1/world/lighting", json_body={"lighting": lighting})

    return await _server_action(sid, request, "change_lighting", action)


# ---------- команда произвольная (для админа, если сервер поддерживает) ----------
@router.get("/server/{sid}/routes")
async def server_routes(sid: str, request: Request):
    """Список роутов сервера — для отладки (требует право config_view)."""
    await _require_perm(request, "config_view")
    srv = _get_server(sid)
    if not srv:
        return _not_found(sid)
    await srv.ensure_caps()
    return {"ok": True, "routes": sorted(srv.routes), "config_writable": srv.config_writable}

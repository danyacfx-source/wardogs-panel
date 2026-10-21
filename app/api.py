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
    from . import donate
    from . import disclog
    from . import seed_store
    from . import steam as steam_mod
    from .bot import database as bot_db
    from .rcon_client import RCONError, WardogsRCON
    from .schemas import (
        VipOrderCreate, VipSlotSave, DonateCheckoutRequest,
        PlayerNoteCreate, PlayerTagCreate,
        ClanPayload, ClanMemberSet, PlayerClanSet, PublicClanApplication, ClanMediaUpload,
        TicketCreate, TicketUpdate, TicketCommentCreate,
        RotationUpdate, ReservedSlotsLimit, ReservedSlotsAdd,
        BroadcastRequest, BanAdd, BanUpdate, BansBulkRequest,
        KickRequest, MessageRequest, PlayerPatchRequest, PlayerSwapRequest,
        MatchMapRequest, LightingRequest,
        DiscordBotCreate, DiscordBotUpdate, BotHeartbeat, BotEvent,
        RolesPut, TempvoiceTransfer,
    )
except ImportError:
    import config
    import db
    import discord as dmod
    import donate
    import disclog
    import seed_store
    import steam as steam_mod
    from bot import database as bot_db
    from rcon_client import RCONError, WardogsRCON
    from schemas import (
        VipOrderCreate, VipSlotSave, DonateCheckoutRequest,
        PlayerNoteCreate, PlayerTagCreate,
        ClanPayload, ClanMemberSet, PlayerClanSet, PublicClanApplication, ClanMediaUpload,
        TicketCreate, TicketUpdate, TicketCommentCreate,
        RotationUpdate, ReservedSlotsLimit, ReservedSlotsAdd,
        BroadcastRequest, BanAdd, BanUpdate, BansBulkRequest,
        KickRequest, MessageRequest, PlayerPatchRequest, PlayerSwapRequest,
        MatchMapRequest, LightingRequest,
        DiscordBotCreate, DiscordBotUpdate, BotHeartbeat, BotEvent,
        RolesPut, TempvoiceTransfer,
    )

from pydantic import ValidationError

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
    items = await db.list_vip_orders()
    for item in items:
        item['pay_url'] = _vip_pay_url(item) if item['status'] == 'awaiting_payment' else ''
    return {
        'ok': True,
        'plans': VIP_PLANS,
        'items': items,
        'provider_connected': bool(config.YOOMONEY_WALLET),
        'pay_page': f'{config.PUBLIC_URL}/donate/vip/',
    }


def _vip_pay_url(order) -> str:
    """Публичная страница оплаты заявки панели: quickpay с меткой WD-<id>."""
    if not config.YOOMONEY_WALLET or not order:
        return ''
    return f'{config.PUBLIC_URL}/donate/vip/{order["id"]}'


async def fulfill_vip_order(order, operation_id: str = ""):
    """Помечает заявку оплаченной и выдаёт VIP-слоты её получателям.

    Вызывается из webhook ЮMoney (app/donate.py). Каждый шаг ограничен
    переходом статуса, поэтому повторное уведомление ничего не дублирует.
    """
    if not order or not await db.mark_vip_order(order['id'], 'paid', expected='awaiting_payment'):
        return False
    expires_utc = time.time() + float(order.get('months') or 1) * 30 * 86400
    results = []
    for steam_id in order.get('recipients') or []:
        db_user = await db.get_user(steam_id)
        item = await db.save_vip_slot(
            order['server_id'],
            steam_id,
            (db_user or {}).get('persona') or '',
            expires_utc,
            f"заказ {order['id']}",
            order.get('actor_steam_id') or '',
        )
        synced, error = await _sync_vip_record(item, add=True)
        results.append({'steam_id': steam_id, 'synced': synced, 'error': error})
        # VIP действует на все серверы: дублируем entitlement и managed-запись.
        for other in _other_servers(order['server_id']):
            try:
                other_item = await db.save_vip_slot(
                    other.id, steam_id,
                    (db_user or {}).get('persona') or '',
                    expires_utc,
                    f"заказ {order['id']}",
                    order.get('actor_steam_id') or '',
                )
                other_synced, other_error = await _sync_vip_record(other_item, add=True)
                if not other_synced:
                    log.warning(
                        "Зеркальный VIP %s (заказ %s) не синхронизирован на %s: %s",
                        steam_id, order['id'], other.id, other_error,
                    )
            except Exception:
                log.exception(
                    "Зеркальный VIP %s (заказ %s) не сохранён на %s",
                    steam_id, order['id'], other.id,
                )
        log.info(
            "VIP-заказ %s: слот %s на %s — %s",
            order['id'], steam_id, order['server_id'], 'выдан' if synced else error,
        )
    issued = all(item['synced'] for item in results)
    if issued:
        await db.mark_vip_order(order['id'], 'fulfilled', expected='paid')
    detail = order['id'] + (f':{operation_id}' if operation_id else '')
    if issued:
        detail += f":{len(results)} выдано"
    else:
        failed = [item['steam_id'] for item in results if not item['synced']]
        detail += ':sync_error ' + ','.join(failed)
    await db.log_site_audit(order.get('actor_steam_id') or None, order['server_id'], 'vip.order.paid', detail)
    _osrv = _get_server(order['server_id'])
    disclog.emit(
        "vip.order.paid", actor=order.get('actor_steam_id') or "донат",
        server=(_osrv.name if _osrv else order['server_id']),
        steam_id=order.get('buyer_steam_id') or '',
        detail=f"{order['id']} · {len(results)} слотов",
    )
    return True


@router.post('/vip/orders')
async def vip_order_create(request: Request):
    user = await _require_perm(request, 'vip_edit')
    payload = await _validated(request, VipOrderCreate)
    plan = next((p for p in VIP_PLANS if p['id'] == payload.plan_id), None)
    buyer = payload.buyer_steam_id
    sid = str(payload.server_id or '')
    recipients = list(payload.recipients or [])
    if not plan or sid not in POOL:
        raise HTTPException(400, 'Проверьте тариф, покупателя и сервер')
    if len(recipients) > plan['seats']:
        raise HTTPException(400, 'Состав должен содержать уникальные SteamID64 в пределах тарифа')
    if not recipients and plan['seats']==1:
        recipients=[buyer]
    order={'id':'WD-'+secrets.token_hex(8).upper(),'buyer_steam_id':buyer,'server_id':sid,'plan_id':plan['id'],'amount':plan['amount'],'months':plan['months'],'seats':plan['seats'],'recipients':recipients,'status':'awaiting_payment','created_utc':time.time(),'actor_steam_id':user['steam_id']}
    await db.create_vip_order(order)
    await db.log_site_audit(user['steam_id'],sid,'vip.order.create',order['id'])
    _srv = _get_server(sid)
    disclog.emit(
        "vip.order.create", actor=user['steam_id'],
        server=(_srv.name if _srv else sid), steam_id=buyer or "",
        detail=f"{order['id']} · {plan['amount']} ₽",
    )
    return {'ok':True,'item':order}


@router.post('/vip/orders/{order_id}/cancel')
async def vip_order_cancel(order_id: str, request: Request):
    user = await _require_perm(request,'vip_edit')
    if not await db.cancel_vip_order(order_id):
        raise HTTPException(409, 'Заявка уже закрыта или не найдена')
    await db.log_site_audit(user['steam_id'],None,'vip.order.cancel',order_id)
    disclog.emit("vip.order.cancel", actor=user['steam_id'], steam_id="", detail=order_id)
    return {'ok':True}

SESSION_COOKIE = "__Host-wds_session" if config.COOKIE_SECURE else "wds_session"
CSRF_COOKIE = "__Host-wds_csrf" if config.COOKIE_SECURE else "wds_csrf"
STEAM_STATE_COOKIE = ("__Secure-wds_steam_state" if config.COOKIE_SECURE else "wds_steam_state")
DISCORD_STATE_COOKIE = ("__Secure-wds_discord_state" if config.COOKIE_SECURE else "wds_discord_state")
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


# Trusted reverse-proxy ranges. Explicit list: ipaddress.is_private semantics
# differ across Python versions (documentation ranges became "private" in 3.13).
_INTERNAL_NETS = [
    ipaddress.ip_network(net)
    for net in (
        "127.0.0.0/8", "::1/128",
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
        "100.64.0.0/10", "169.254.0.0/16",
        "fc00::/7", "fe80::/10", "0.0.0.0/8",
    )
]


def _is_internal(addr) -> bool:
    return any(addr in net for net in _INTERNAL_NETS)


def client_ip(headers, peer):
    """Реальный IP клиента.

    Заголовок X-Forwarded-For доверяем только от приватного/loopback-пира —
    это наш reverse proxy. Прямое подключение к origin подделать заголовок не
    позволяет: публичный пир игнорируется и берётся как есть.
    """
    try:
        peer_ip = ipaddress.ip_address(str(peer or ""))
    except ValueError:
        return str(peer or "")
    if not _is_internal(peer_ip):
        return str(peer_ip)
    hops = [hop.strip() for hop in (headers.get("x-forwarded-for") or "").split(",") if hop.strip()]
    for hop in reversed(hops):
        try:
            hop_ip = ipaddress.ip_address(hop)
        except ValueError:
            continue
        if _is_internal(hop_ip):
            continue
        return str(hop_ip)
    return str(peer_ip)


def admin_ip_allowed(request: Request) -> bool:
    if not config.ADMIN_IPS:
        return True
    ip = client_ip(request.headers, request.client.host if request.client else "")
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in config.ADMIN_IPS)

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
        self.info = {}  # version/build/feed с /v1/version и /v1/feed (новые билды)
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
            await self._refresh_info()

    async def _refresh_info(self):
        """Версия/билд и feed — роуты, появившиеся в сборке CL-516586+."""
        info = dict(self.info)
        if "GET /v1/version" in self.routes:
            try:
                version = await self.api.version()
                if isinstance(version, dict):
                    info["version"] = version
            except RCONError:
                pass
        else:
            info.pop("version", None)
        if "GET /v1/feed" in self.routes:
            try:
                feed = await self.api.feed()
                if isinstance(feed, dict):
                    info["feed"] = feed
            except RCONError:
                pass
        else:
            info.pop("feed", None)
        self.info = info

    @property
    def build(self):
        version = self.info.get("version")
        if isinstance(version, dict):
            return str(version.get("build") or "")
        return ""


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


async def _validated(request: Request, model_cls):
    body = await _json_object(request)
    try:
        return model_cls(**body)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        msg = first.get("msg", "invalid body")
        loc = ".".join(str(x) for x in first.get("loc", []) if x != "body")
        raise HTTPException(status_code=400, detail=f"{loc}: {msg}" if loc else msg) from exc


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
    if not admin_ip_allowed(request):
        raise HTTPException(status_code=403, detail="IP не в списке разрешённых (WARDOGS_ADMIN_IPS)")
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


@router.get("/profile")
async def my_profile(request: Request):
    """Личный профиль: вход, права, свои VIP-слоты, тикеты и последние действия."""
    user = _require_user(await _current_user(request))
    steam_id = user["steam_id"]
    me = await _user_status(user)
    binding = await db.get_binding(steam_id)
    activity = await db.get_player_profile(steam_id)
    steam_profile = (await steam_mod.fetch_profiles([steam_id])).get(steam_id) or {}
    now = time.time()
    vip_slots = await db.list_player_vip_slots(steam_id)
    server_names = {srv.id: srv.name for srv in POOL.values()}
    for vip in vip_slots:
        vip["active"] = vip["expires_utc"] is None or vip["expires_utc"] > now
        vip["server_name"] = server_names.get(vip["server_id"], vip["server_id"])
    tickets = [
        ticket
        for ticket in await db.list_tickets(limit=500)
        if steam_id in (ticket.get("player_steam_id"), ticket.get("created_by"), ticket.get("assigned_to"))
    ]
    tickets.sort(key=lambda ticket: ticket.get("updated_utc") or 0, reverse=True)
    audit = [
        item
        for item in await db.get_site_audit(limit=500)
        if item.get("actor_steam_id") == steam_id
    ][:20]
    return {
        "ok": True,
        "me": me,
        "binding": {
            "discord_id": (binding or {}).get("discord_id"),
            "discord_name": (binding or {}).get("discord_name"),
            "guild_member": bool((binding or {}).get("guild_member")),
            "roles": (binding or {}).get("roles") or [],
            "updated_utc": (binding or {}).get("updated_utc") or 0,
        }
        if binding
        else None,
        "steam": {
            "avatar_url": steam_profile.get("avatar_url") or "",
            "profile_url": steam_profile.get("profile_url") or f"https://steamcommunity.com/profiles/{steam_id}",
            "visibility": steam_profile.get("visibility", 0),
            "time_created_utc": steam_profile.get("time_created_utc"),
        },
        "activity": activity,
        "vip_slots": vip_slots,
        "tickets": tickets[:20],
        "tickets_total": len(tickets),
        "audit": audit,
        "server_names": server_names,
    }


@router.get("/seed/balance")
async def seed_balance(request: Request):
    """Баланс баллов сидинга + доступные SKU для покупки VIP."""
    user = _require_user(await _current_user(request))
    binding = await db.get_binding(user["steam_id"])
    discord_id = (binding or {}).get("discord_id") or ""
    if not discord_id:
        return {"ok": False, "error": "Привяжите Discord в профиле, чтобы купить VIP за баллы."}
    info = seed_store.get_balance(discord_id)
    if info is None:
        return {"ok": False, "error": "Привязка Discord↔Steam не найдена. Используйте /seed link в Discord."}
    shop = await seed_store.get_shop_config()
    return {
        "ok": True,
        "balance": info["balance"],
        "steam_id": info.get("steam_id") or "",
        "display_name": info.get("display_name") or "",
        "skus": [
            {"sku": sku, "label": e["label"], "cost": e["cost"], "enabled": e["enabled"]}
            for sku, e in shop.items()
        ],
    }


@router.post("/seed/vip")
async def seed_vip_purchase(request: Request):
    """Покупка VIP за баллы сидинга: списывает баллы, создаёт VIP-слот на всех серверах."""
    user = _require_user(await _current_user(request))
    body = await _json_object(request)
    sku = str(body.get("sku") or "").strip()
    shop = await seed_store.get_shop_config()
    entry = shop.get(sku)
    if not entry or not entry["enabled"]:
        raise HTTPException(status_code=400, detail="Неизвестный SKU или продажа закрыта")
    binding = await db.get_binding(user["steam_id"])
    discord_id = (binding or {}).get("discord_id") or ""
    if not discord_id:
        raise HTTPException(status_code=400, detail="Привяжите Discord в профиле")
    result = seed_store.purchase_vip(discord_id, sku, entry["cost"], entry["duration"])
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["error"])
    duration = result["duration"]
    expires_utc = time.time() + duration
    steam_id = user["steam_id"]
    display_name = user.get("steam_name") or steam_id
    note = f"покупка за баллы сидинга ({sku})"
    created = []
    for srv in POOL.values():
        item = await db.save_vip_slot(srv.id, steam_id, display_name, expires_utc, note, steam_id)
        ok, error = await _sync_vip_record(item, add=True)
        created.append({"server": srv.id, "ok": ok, "error": error or None})
    await db.log_site_audit(steam_id, None, "vip.save", f"{steam_id}:seed:{sku}")
    disclog.emit("vip.save", actor=steam_id, steam_id=steam_id,
                 detail=f"VIP за баллы сидинга ({sku}), баланс: {result['balance']}")
    return {"ok": True, "balance": result["balance"], "sku": sku, "expires_utc": expires_utc, "servers": created}


@router.get("/config/seed-shop")
async def seed_shop_get(request: Request):
    """Настройки магазина VIP за баллы (админ)."""
    await _require_perm(request, "config_edit")
    return {"ok": True, "config": await seed_store.get_shop_config()}


@router.put("/config/seed-shop")
async def seed_shop_put(request: Request):
    """Обновляет цены/названия/доступность SKU (админ)."""
    user = await _require_perm(request, "config_edit")
    body = await _json_object(request)
    config_in = body.get("config")
    if not isinstance(config_in, dict):
        raise HTTPException(status_code=400, detail="поле config")
    updated = await seed_store.save_shop_config(config_in)
    await db.log_site_audit(user["steam_id"], None, "config.edit", "seed_shop")
    return {"ok": True, "config": updated}


@router.post("/donate/checkout")
async def donate_checkout(request: Request):
    """Создаёт подписанный заказ на личный VIP для входившего пользователя.

    Заказ подписывается тем же секретом, что и заказы донат-бота, поэтому
    после оплаты оба бота работают с ним как с обычным: сидер проверяет
    подпись и выдаёт слот, донат-бот разбирает заказ в /donation-admin review
    (он не создавал его сам, поэтому автоподтверждения ролей нет).
    """
    user = _require_user(await _current_user(request))
    try:
        body = await request.json()
    except Exception:
        body = {}
    product = str((body or {}).get("product") or "")
    if product not in donate.SITE_PRODUCTS:
        raise HTTPException(status_code=400, detail="неизвестный тариф")
    binding = await db.get_binding(user["steam_id"])
    discord_id = str((binding or {}).get("discord_id") or "")
    if not re.fullmatch(r"\d{17,20}", discord_id):
        raise HTTPException(
            status_code=409,
            detail="Привяжите Discord-аккаунт: оплаченные роли выдаются на него",
        )
    try:
        token = donate.create_site_order(product, discord_id)
    except ValueError:
        raise HTTPException(status_code=503, detail="оплата временно не настроена")
    await db.log_site_audit(user["steam_id"], None, "donate.checkout", product)
    return {"url": f"/donate/index.php?o={token}"}


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

    bridge, _bridge_error = await dmod.bot_panel_request("GET", "/api/tempvoice")
    if bridge is not None:
        bridge_row = {"id": "panel_bot", "name": "Панельный бот", "ok": True, "detail": f"временных комнат: {len((bridge or {}).get('rooms') or [])}"}
    else:
        bridge_row = {"id": "panel_bot", "name": "Панельный бот", "ok": False, "detail": _bridge_error or "бот недоступен"}
    services.append(bridge_row)
    _record_service_state("panel_bot", bridge_row["ok"], bridge_row["detail"])

    steam_ok = bool(config.STEAM_API_KEY)
    steam_row = {"id": "steam", "ok": steam_ok, "detail": "Steam Web API настроен" if steam_ok else "STEAM_API_KEY не задан"}
    services.append(steam_row)
    _record_service_state("steam", steam_ok, steam_row["detail"])
    return {"ok": all(item["ok"] for item in services), "services": services, "history": list(_service_history)}


@router.post("/discord/log-test")
async def discord_log_test(request: Request):
    """Диагностика Discord-логов: токен бота, доступ к каналу, тестовая отправка."""
    await _require_any_perm(request, {"audit", "roles", "config_view"})
    result = await disclog.test_channel()
    return JSONResponse(result, status_code=200 if result["ok"] else 400)


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
    resp.delete_cookie(CSRF_COOKIE, path="/")
    return resp


# ---------- мониторинг (анонимно) ----------

PLAYER_ACTION_PERMS = {"players_view", "kick", "kill", "message", "move_faction"}


async def _can_view_players(request: Request):
    user = await _current_user(request)
    if not user:
        return False
    me = await _user_status(user)
    return bool(PLAYER_ACTION_PERMS.intersection(me["permissions"]))


async def _require_view_players(request: Request):
    """Список игроков: сначала 401 (нет входа), затем 403 (нет права).

    Проверка прав до проверки сессии светила анонимному клиенту 403 и
    раскрывала, какие эндпоинты закрыты ролями. Без входа отвечаем 401.
    """
    user = await _current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="нужен вход через Steam")
    me = await _user_status(user)
    if not PLAYER_ACTION_PERMS.intersection(me["permissions"]):
        raise HTTPException(status_code=403, detail="список игроков доступен только разрешённым ролям")
    return user


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
            "build": srv.build,
            "feed": srv.info.get("feed") or {},
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
    await _require_view_players(request)
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


@router.get("/steam/persona")
async def steam_persona(request: Request, steam_id: str = ""):
    """Resolve a SteamID64 to its current persona name (best effort, never fails)."""
    _require_user(await _current_user(request))
    sid = _validate_steam_id(steam_id)
    if not sid:
        return {"ok": False, "name": ""}
    name = ""
    if config.STEAM_API_KEY:
        try:
            personas = await steam_mod.fetch_personas([sid])
            name = (personas.get(sid) or "").strip()[:120]
        except Exception:
            name = ""
    return {"ok": bool(name), "steam_id": sid, "name": name}


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
    payload = await _validated(request, PlayerNoteCreate)
    note = payload.note
    idem_key, replay = await _idempotency_begin(
        request, user["steam_id"], _request_fingerprint(request, await _json_object(request))
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
    payload = await _validated(request, PlayerTagCreate)
    label = payload.label
    color = payload.color
    idem_key, replay = await _idempotency_begin(
        request, user["steam_id"], _request_fingerprint(request, await _json_object(request))
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


@router.get("/public/giveaways")
async def public_giveaways_feed():
    """Публичный фид розыгрышов (для страницы /giveaways/ на сайте)."""
    entry = await db.load_public_registry("giveaways")
    if not entry:
        return {"version": 1, "generated_at": None, "source": {}, "giveaways": []}
    feed = entry["payload"]
    feed["updated_utc"] = entry["updated_utc"]
    return feed


@router.get("/public/clans-registry")
async def public_clans_registry():
    """Публичный реестр кланов, накачанный clans-bot через /public/clan-sync."""
    entry = await db.load_public_registry("clans")
    if not entry:
        return {"version": 1, "generated_at": None, "source": {}, "clans": []}
    feed = entry["payload"]
    feed["updated_utc"] = entry["updated_utc"]
    return feed


@router.post("/public/clan-sync")
async def public_clan_sync(request: Request):
    """Приём реестра кланов от clans-bot (HMAC-SHA256: timestamp.body)."""
    if not config.CLAN_SYNC_SECRET:
        raise HTTPException(status_code=404, detail="clan-sync disabled")
    timestamp = (request.headers.get("x-wardogs-timestamp") or "").strip()
    signature = (request.headers.get("x-wardogs-signature") or "").strip().lower()
    if not timestamp.isdigit() or not re.fullmatch(r"[0-9a-f]{64}", signature):
        raise HTTPException(status_code=403, detail="bad signature")
    ts = int(timestamp)
    if abs(time.time() - ts) > 300:
        raise HTTPException(status_code=403, detail="stale timestamp")
    raw = await request.body()
    if len(raw) > config.MAX_BODY_BYTES * 4:
        raise HTTPException(status_code=413, detail="payload too large")
    expected = hmac.new(
        config.CLAN_SYNC_SECRET.encode(), f"{ts}.".encode() + raw, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=403, detail="bad signature")
    try:
        body = json.loads(raw)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid JSON") from exc
    if not isinstance(body, dict) or not isinstance(body.get("clans"), list) or len(body["clans"]) > 100:
        raise HTTPException(status_code=400, detail="invalid payload")
    clans = []
    for item in body["clans"]:
        if not isinstance(item, dict):
            continue
        clan_id = str(item.get("id") or "")
        name = str(item.get("name") or "").strip()
        if not re.fullmatch(r"\d{17,20}", clan_id) or not 2 <= len(name) <= 48:
            continue
        clans.append(item)
    feed = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "source": body.get("source") if isinstance(body.get("source"), dict) else {},
        "clans": clans,
    }
    await db.save_public_registry("clans", json.dumps(feed, ensure_ascii=False))
    return {"ok": True, "clans": len(clans)}


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
    payload = await _validated(request, PublicClanApplication)
    name = payload.name
    tag = payload.tag
    description = payload.description
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
    payload = await _validated(request, ClanPayload)
    name, tag, color, description = payload.name, payload.tag, payload.color, payload.description
    idem_key, replay = await _idempotency_begin(
        request, user["steam_id"], _request_fingerprint(request, await _json_object(request))
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
    payload = await _validated(request, ClanPayload)
    name, tag, color, description = payload.name, payload.tag, payload.color, payload.description
    idem_key, replay = await _idempotency_begin(
        request, user["steam_id"], _request_fingerprint(request, await _json_object(request))
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
    payload = await _validated(request, ClanMediaUpload)
    kind = payload.kind
    data_url = payload.data_url
    match = re.fullmatch(r"data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=]+)", data_url)
    if not match:
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
    payload = await _validated(request, ClanMemberSet)
    steam_id = payload.steam_id
    role = payload.member_role
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
    payload = await _validated(request, PlayerClanSet)
    clan_id = payload.clan_id
    member_role = " ".join(str(payload.member_role or "Участник").split())[:40]
    idem_key, replay = await _idempotency_begin(
        request, user["steam_id"], _request_fingerprint(request, await _json_object(request))
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
    payload = await _validated(request, TicketCreate)
    title = " ".join(str(payload.title or "").split())
    description = str(payload.description or "").strip()
    priority = str(payload.priority or "normal").strip()
    player_steam_id = str(payload.player_steam_id or "").strip() or None
    if player_steam_id:
        player_steam_id = _validate_steam_id(player_steam_id)
    idem_key, replay = await _idempotency_begin(
        request, user["steam_id"], _request_fingerprint(request, await _json_object(request))
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
    payload = await _validated(request, TicketUpdate)
    title = " ".join(str((payload.title or current["title"]) or "").split())
    description = str((payload.description or current["description"]) or "").strip()
    status = str((payload.status or current["status"]) or "").strip()
    priority = str((payload.priority or current["priority"]) or "").strip()
    player_steam_id = payload.player_steam_id if payload.player_steam_id is not None else current["player_steam_id"]
    assigned_to = payload.assigned_to if payload.assigned_to is not None else current["assigned_to"]
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
        request, user["steam_id"], _request_fingerprint(request, await _json_object(request))
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
    payload = await _validated(request, TicketCommentCreate)
    text = payload.body
    idem_key, replay = await _idempotency_begin(
        request, user["steam_id"], _request_fingerprint(request, await _json_object(request))
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


# --- Типы тикетов Discord-бота (селект «Создать тикет») ---

_TICKET_TYPE_ID_RE = re.compile(r"^[a-z0-9_-]{1,48}$")


async def _ticket_type_body(request: Request) -> dict:
    body = await _json_object(request)
    return body if isinstance(body, dict) else {}


def _ticket_type_color(value) -> int:
    if isinstance(value, str):
        text = value.strip().lstrip("#")
        if not re.fullmatch(r"[0-9a-fA-F]{6}", text):
            raise HTTPException(status_code=400, detail="Цвет должен быть в формате #RRGGBB")
        return int(text, 16)
    number = int(value)
    if not 0 <= number <= 0xFFFFFF:
        raise HTTPException(status_code=400, detail="Цвет вне диапазона 0..FFFFFF")
    return number


def _public_ticket_type(row: dict) -> dict:
    color = int(row.get("color") or 0)
    return {
        "type_id": row.get("type_id"),
        "label": row.get("label") or "",
        "description": row.get("description") or "",
        "emoji": row.get("emoji") or "",
        "color": f"#{color:06X}",
        "position": int(row.get("position") or 0),
        "enabled": bool(row.get("enabled")),
    }


@router.get("/ticket-types")
async def ticket_types_list(request: Request):
    await _require_any_perm(request, {"tickets_view", "tickets_edit"})
    return {"ok": True, "types": [_public_ticket_type(r) for r in bot_db.list_ticket_types(include_disabled=True)]}


@router.post("/ticket-types")
async def ticket_type_create(request: Request):
    user = await _require_perm(request, "tickets_edit")
    body = await _ticket_type_body(request)
    type_id = str(body.get("type_id") or "").strip().lower()
    if not _TICKET_TYPE_ID_RE.fullmatch(type_id):
        raise HTTPException(status_code=400, detail="ID типа: латиница, цифры, _ и -, до 48 символов")
    label = str(body.get("label") or "").strip()
    if not 1 <= len(label) <= 100:
        raise HTTPException(status_code=400, detail="Название: 1–100 символов")
    if bot_db.get_ticket_type(type_id):
        raise HTTPException(status_code=409, detail="Такой ID типа уже существует")
    description = str(body.get("description") or "").strip()[:2000]
    emoji = str(body.get("emoji") or "").strip()[:16]
    color = _ticket_type_color(body.get("color") or "#5865F2")
    position = int(body.get("position") or 0)
    enabled = bool(body.get("enabled", True))
    bot_db.upsert_ticket_type(type_id, label, description, emoji, color, position, enabled)
    await db.log_site_audit(user["steam_id"], None, "ticket_type.create", type_id)
    return {"ok": True, "type": _public_ticket_type(bot_db.get_ticket_type(type_id))}


@router.patch("/ticket-types/{type_id}")
async def ticket_type_update(type_id: str, request: Request):
    user = await _require_perm(request, "tickets_edit")
    current = bot_db.get_ticket_type(type_id)
    if not current:
        raise HTTPException(status_code=404, detail="Тип не найден")
    body = await _ticket_type_body(request)
    label = str(body.get("label", current["label"])).strip()
    if not 1 <= len(label) <= 100:
        raise HTTPException(status_code=400, detail="Название: 1–100 символов")
    description = str(body.get("description", current["description"]) or "").strip()[:2000]
    emoji = str(body.get("emoji", current["emoji"]) or "").strip()[:16]
    color = _ticket_type_color(body.get("color") or f"#{int(current['color'] or 0):06X}")
    position = int(body.get("position", current["position"] or 0))
    enabled = bool(body.get("enabled", bool(current.get("enabled", 1))))
    bot_db.upsert_ticket_type(type_id, label, description, emoji, color, position, enabled)
    await db.log_site_audit(user["steam_id"], None, "ticket_type.update", type_id)
    return {"ok": True, "type": _public_ticket_type(bot_db.get_ticket_type(type_id))}


@router.delete("/ticket-types/{type_id}")
async def ticket_type_delete(type_id: str, request: Request):
    user = await _require_perm(request, "tickets_edit")
    if not bot_db.delete_ticket_type(type_id):
        raise HTTPException(status_code=404, detail="Тип не найден")
    await db.log_site_audit(user["steam_id"], None, "ticket_type.delete", type_id)
    return {"ok": True}


@router.get("/server/{sid}/players")
async def server_players(sid: str, request: Request):
    await _require_view_players(request)
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
    if not user:
        raise HTTPException(status_code=401, detail="нужен вход через Steam")
    if not (await _user_status(user)).get("permissions"):
        raise HTTPException(status_code=403, detail="ротация доступна только разрешённым ролям")
    srv = _get_server(sid)
    if not srv:
        return _not_found(sid)
    try:
        data = await _cached(srv.id, "rotation", srv.api.rotation)
    except RCONError as e:
        return _rcon_error(e)
    return {"ok": True, **data}


_ROTATION_SECTION_RE = re.compile(
    r"(?ims)^\[/Script/WDGame\.WDServerMapRotationSettings\][^\[]*"
)
_ROTATION_ENTRY_RE = re.compile(r"(?im)^\s*[.+!]RotationEntries\s*=")


def _cfg_str(value):
    """Безопасное значение для ServerSettings.ini (без кавычек и переводов)."""
    cleaned = str(value or "").replace('"', "").replace("\r", "").replace("\n", "")
    return cleaned.strip()[:128]


def _rotation_entry_line(entry):
    parts = [f'Map="{_cfg_str(entry.get("map"))}"']
    experiences = [str(x).strip() for x in (entry.get("experiences") or []) if str(x).strip()]
    if experiences:
        parts.append('Experiences="' + "+".join(_cfg_str(x) for x in experiences) + '"')
    lighting = _cfg_str(entry.get("lighting"))
    if lighting:
        parts.append(f'Lighting="{lighting}"')
    parts.append(f'ZoneAlternator="{_cfg_str(entry.get("zoneAlternator")) or "None"}"')
    return ".RotationEntries=(" + ", ".join(parts) + ")"


def _rotation_config_text(text, entries, mode=None):
    """Собирает секцию ротации для PUT /v1/config.

    PUT заменяет документ целиком, поэтому правим только секцию ротации,
    сохраняя остальные ключи (bEnabled и незнакомые — как есть).
    """
    newline = "\r\n" if "\r\n" in text else "\n"
    header = "[/Script/WDGame.WDServerMapRotationSettings]"
    kept = []
    b_enabled = None
    rotation_mode = None
    match = _ROTATION_SECTION_RE.search(text)
    if match:
        for line in match.group(0).splitlines()[1:]:
            if not line.strip():
                continue
            if re.match(r"(?i)^\s*bEnabled\s*=", line):
                b_enabled = line.strip()
            elif re.match(r"(?i)^\s*RotationMode\s*=", line):
                rotation_mode = line.strip()
            elif _ROTATION_ENTRY_RE.match(line):
                continue
            else:
                kept.append(line)
    if mode in {"ordered", "random"}:
        rotation_mode = f"RotationMode={'Ordered' if mode == 'ordered' else 'Random'}"
    lines = [header]
    lines.append(b_enabled or "bEnabled=True")
    lines.append(rotation_mode or "RotationMode=Ordered")
    lines.extend(kept)
    lines.append("!RotationEntries=ClearArray")
    lines.extend(_rotation_entry_line(entry) for entry in entries)
    section = newline.join(lines) + newline
    if match:
        return text[: match.start()] + section + text[match.end():]
    return text.rstrip("\r\n") + newline + newline + section


@router.put("/server/{sid}/rotation")
async def server_rotation_update(sid: str, request: Request):
    """Proxy a validated rotation update to the upstream RCON API."""
    user = await _require_perm(request, "change_map")
    srv = _get_server(sid)
    if not srv:
        return _not_found(sid)
    payload = await _validated(request, RotationUpdate)
    entries = [entry.model_dump() for entry in payload.entries]
    rcon_body = {"entries": entries}
    if payload.mode in {"ordered", "random"}:
        rcon_body["mode"] = payload.mode
    try:
        await srv.ensure_caps()
        if "PUT /v1/rotation" in srv.routes:
            status, data = await srv.api.request_raw("PUT", "/v1/rotation", json_body=rcon_body)
            if status >= 400:
                return JSONResponse(data if isinstance(data, dict) else {"detail": str(data)}, status_code=status)
        elif srv.config_writable and "GET /v1/config" in srv.routes and "PUT /v1/config" in srv.routes:
            # Сборка CL-516586+: запись ротации только через документ конфига.
            status, cfg = await srv.api.request_raw("GET", "/v1/config")
            if status != 200 or not isinstance(cfg, dict):
                return JSONResponse(
                    cfg if isinstance(cfg, dict) else {"error": {"message": "не удалось прочитать конфиг"}},
                    status_code=status,
                )
            updated = _rotation_config_text(str(cfg.get("text") or ""), entries, rcon_body.get("mode"))
            if updated.encode("utf-8") > 65536:
                raise HTTPException(status_code=413, detail="документ конфига больше лимита сервера (64 КБ)")
            revision = str(cfg.get("revision") or "").strip()
            headers = {"If-Match": f'"{revision}"'} if revision else {}
            put_status, result = await srv.api.request_raw(
                "PUT", "/v1/config", raw_body=updated, content_type="text/plain", extra_headers=headers
            )
            if put_status == 412:
                return JSONResponse(
                    {"error": {"code": "conflict", "message": "конфиг изменился на сервере — обновите и повторите"}},
                    status_code=409,
                )
            if put_status != 200:
                message = "конфиг отклонён сервером"
                if isinstance(result, dict):
                    errors = result.get("errors") or []
                    if errors:
                        first = errors[0]
                        message = f"{first.get('section')}.{first.get('key')}: {first.get('message')}"
                    elif isinstance(result.get("error"), dict):
                        message = result["error"].get("message") or message
                return JSONResponse(
                    {"error": {"code": "config_rejected", "message": message}, "result": result},
                    status_code=put_status,
                )
            data = await srv.api.rotation()
        else:
            raise HTTPException(status_code=501, detail="Сервер не поддерживает изменение ротации")
    except RCONError as e:
        return _rcon_error(e)
    await db.log_site_audit(user["steam_id"], sid, "rotation.update", f"{len(entries)} карт")
    async with _cache_lock:
        _cache.pop((sid, "rotation"), None)
    return {"ok": True, "rotation": data, "entries": entries}


def _other_servers(sid):
    """Все серверы панели, кроме указанного (для зеркальных банов)."""
    return [server for server in POOL.values() if server.id != sid]


async def _apply_ban_across(primary_sid, steam_id, reason):
    """Выдаёт POST /v1/bans на всех серверах панели.

    Возвращает (applied, pending, primary_error): applied — серверы с
    подтверждённым баном (включая 409 «уже есть»); pending — серверы, где
    игрока нет онлайн (там бан поставит очередь при входе); primary_error —
    жёсткая ошибка основного сервера, выдача прервана.
    """
    reason = str(reason or "").strip()[:500]
    body = {"steamId": steam_id}
    if reason:
        body["reason"] = reason
    targets = [t for t in [_get_server(primary_sid)] + _other_servers(primary_sid) if t]
    applied, pending = [], []
    primary_error = None
    for target in targets:
        try:
            await target.api.request("POST", "/v1/bans", json_body=body)
            applied.append(target.id)
        except RCONError as e:
            if e.status == 409:
                applied.append(target.id)
            elif e.status == 404 and e.code == "player_not_found":
                pending.append(target.id)
            elif target.id == primary_sid:
                primary_error = e
                break
            else:
                log.warning("Зеркальный бан %s не удался на %s: %s", steam_id, target.id, e.message)
                pending.append(target.id)
    return applied, pending, primary_error


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
    # Очередь панели: оффлайн-баны, которые выдадутся при входе игрока.
    base_count = data.get("count", len(rows))
    live_ids = {str(item.get("steamId") or "") for item in rows}
    queued_rows = 0
    for q in await db.list_queued_bans():
        if q["steam_id"] in live_ids:
            continue
        rows.append({
            "steamId": q["steam_id"],
            "displayName": q["display_name"],
            "reason": q["reason"],
            "queued": True,
            "expiresUtc": q["expires_utc"],
            "bannedBy": q["created_by"],
            "bannedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(q["created_utc"])),
            "appliedServers": q["applied_servers"],
        })
        queued_rows += 1
    result["bans"] = rows
    result["count"] = base_count + queued_rows
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
    if not admin_ip_allowed(request):
        raise HTTPException(status_code=403, detail="IP не в списке разрешённых (WARDOGS_ADMIN_IPS)")
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
    for ref in sorted(perms_map):
        if str(ref).startswith("user:"):
            roles.append({
                "id": ref,
                "name": f"Личный доступ · {str(ref)[5:]}",
                "perms": perms_map.get(ref) or [],
                "configured": True,
                "legacy": False,
            })
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
    payload = await _validated(request, DiscordBotCreate)
    name = str(payload.name or "").strip()[:80]
    requested_id = str(payload.bot_id or "").strip().lower()
    bot_id = requested_id or re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")[:40]
    if not name or not BOT_ID_RE.fullmatch(bot_id):
        raise HTTPException(status_code=400, detail="Укажите имя и ID: 3–48 латинских символов")
    if len(await db.list_bot_integrations()) >= 64:
        raise HTTPException(status_code=409, detail="Достигнут запас реестра: 64 бота")
    guild_id = str(payload.guild_id or "").strip()
    if guild_id and not re.fullmatch(r"\d{5,20}", guild_id):
        raise HTTPException(status_code=400, detail="Некорректный Discord Guild ID")
    base_url = str(payload.base_url or "").strip().rstrip("/")[:500]
    if base_url:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise HTTPException(status_code=400, detail="Некорректный URL бота")
    scopes = sorted(set(str(value) for value in payload.scopes or []) & BOT_SCOPES)
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
    payload = await _validated(request, DiscordBotUpdate)
    changes = {}
    for key, limit in (("name", 80), ("guild_id", 20), ("bot_user_id", 20), ("base_url", 500)):
        val = getattr(payload, key, None)
        if val is not None:
            changes[key] = str(val).strip()[:limit]
    if payload.enabled is not None:
        changes["enabled"] = payload.enabled
        if not payload.enabled:
            changes.update(status="disabled", detail="отключён администратором")
    if payload.scopes is not None:
        changes["scopes"] = sorted(set(payload.scopes) & BOT_SCOPES)
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
    payload = await _validated(request, BotHeartbeat)
    metadata = payload.metadata if isinstance(payload.metadata, dict) else {}
    encoded = json.dumps(metadata, ensure_ascii=False)
    if len(encoded.encode("utf-8")) > 8192:
        raise HTTPException(status_code=413, detail="Слишком большие метаданные")
    updated = await db.update_bot_integration(
        item["bot_id"], status="online", detail=str(payload.detail or "")[:500],
        version=str(payload.version or "")[:80], bot_user_id=str(payload.bot_user_id or item["bot_user_id"])[:20],
        metadata=metadata, last_seen_utc=time.time(),
    )
    return {"ok": True, "bot": _public_bot(updated), "server_time": time.time(), "api_version": 1}


@router.post("/bot/v1/events")
async def bot_v1_events(request: Request):
    item = await _require_bot(request, "events.write")
    payload = await _validated(request, BotEvent)
    event_id = payload.event_id
    event_type = payload.event_type
    event_payload = payload.payload if isinstance(payload.payload, dict) else {}
    if not event_id or not BOT_EVENT_RE.fullmatch(event_type):
        raise HTTPException(status_code=400, detail="Нужны event_id и корректный event_type")
    if len(json.dumps(event_payload, ensure_ascii=False).encode("utf-8")) > 32768:
        raise HTTPException(status_code=413, detail="Событие слишком большое")
    created = await db.append_bot_event(item["bot_id"], event_id, event_type, event_payload)
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
    payload = await _validated(request, TempvoiceTransfer)
    owner_id = payload.owner_id
    if not re.fullmatch(r"\d{1,20}", channel_id):
        raise HTTPException(status_code=400, detail="Некорректный ID комнаты или владельца")
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
    payload = await _validated(request, RolesPut)
    role_ref = str(payload.role_ref or "").strip()[:128]
    if not role_ref:
        return JSONResponse({"error": {"code": "missing_field", "message": "поле role_ref"}}, status_code=400)
    perms = [p for p in payload.perms if p in dmod.ALL_PERMS]
    idem_key, replay = await _idempotency_begin(request, user["steam_id"], _request_fingerprint(request, await _json_object(request)))
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


# ---------- kill feed ----------

_KILLFEED_MAX_BODY = 64 * 1024
_KILLFEED_MAX_KILLS = 200
_STEAM_ID_RE = re.compile(r"^\d{17}$")
_KILL_TAG_PREFIXES = (
    "Meta.Progression.Context.Player.KillContext.",
    "Meta.PlayerKillFlag.Player.",
)


def _short_kill_tags(raw):
    """Контекстные теги в короткой форме (префиксы сняты, шум отброшен)."""
    if not isinstance(raw, list):
        return []
    out = []
    for tag in raw[:32]:
        if not isinstance(tag, str):
            continue
        short = tag
        for prefix in _KILL_TAG_PREFIXES:
            if short.startswith(prefix):
                short = short[len(prefix):]
        short = short[:60]
        if short not in ("Local.Kill", "Local.Death") and short not in out:
            out.append(short)
    return out


def _parse_killfeed_batch(body):
    """Тело батча килфида: {serverId, serverName, events: [...]}."""
    if not isinstance(body, dict) or not isinstance(body.get("events"), list):
        raise ValueError("ожидался объект { events: [...] }")
    kills = []
    other = 0
    skipped = 0
    for raw in body["events"][:1000]:
        if not isinstance(raw, dict):
            skipped += 1
            continue
        etype = str(raw.get("type") or "")[:32]
        if etype == "killed":
            event_id = str(raw.get("eventId") or "")[:64]
            victim_id = str(raw.get("victimSteamId") or "")
            event_time = raw.get("eventTime")
            if not event_id or not _STEAM_ID_RE.match(victim_id) or not isinstance(event_time, (int, float)):
                skipped += 1
                continue
            tags = _short_kill_tags(raw.get("contextTags"))
            killer_id = str(raw.get("killerSteamId") or "")
            killer_id = killer_id if _STEAM_ID_RE.match(killer_id) else None
            suicide = "Suicide" in tags or (killer_id is not None and killer_id == victim_id)
            distance = raw.get("distance")
            distance_m = round(float(distance)) / 100 if isinstance(distance, (int, float)) else None
            kills.append({
                "event_id": event_id,
                "match_id": str(raw.get("matchId") or "")[:64],
                "event_time": float(event_time),
                "map": str(raw.get("mapName") or "")[:64],
                "killer_id": killer_id,
                "killer_name": (str(raw.get("killerName") or "")[:80] or None) if killer_id else None,
                "victim_id": victim_id[:17],
                "victim_name": str(raw.get("victimName") or "")[:80],
                "cause": str(raw.get("cause") or "")[:200],
                "distance_m": distance_m,
                "headshot": "Headshot" in tags,
                "suicide": suicide,
                "tags": [t for t in tags if t not in ("Headshot", "Suicide")][:12],
            })
        elif etype in ("player_joined", "player_left", "match_state", "match_ended"):
            other += 1
        else:
            skipped += 1
    return kills, other, skipped


async def _killfeed_factions(server_id, srv, kills):
    """Фракции сторон убийства: player_activity, при необходимости — живой список игроков."""
    wanted = list({sid for k in kills for sid in (k["killer_id"], k["victim_id"]) if sid})
    factions = await db.get_known_factions(server_id, wanted)
    if not any(factions.values()):
        try:
            players = await _cached_players(srv)
            for player in players.get("players") or []:
                steam_id = str(player.get("steamId") or "")
                if steam_id in wanted:
                    factions[steam_id] = player.get("faction") or None
        except Exception:
            pass
    return factions


@router.post("/killfeed/{server_id}")
async def killfeed_ingest(server_id: str, request: Request):
    """Приём батча килфида от игрового сервера (Bearer = WARDOGS_KILLFEED_TOKEN)."""
    if not config.KILLFEED_TOKEN:
        raise HTTPException(status_code=503, detail="приём килфида не настроен")
    auth_header = request.headers.get("authorization") or ""
    offered = auth_header[7:].strip() if auth_header[:7].lower() == "bearer " else ""
    if not offered or not hmac.compare_digest(offered, config.KILLFEED_TOKEN):
        raise HTTPException(status_code=401, detail="неизвестный токен килфида")
    srv = _get_server(server_id)
    if not srv:
        raise _not_found(server_id)
    raw = await request.body()
    if len(raw) > _KILLFEED_MAX_BODY:
        raise HTTPException(status_code=413, detail="батч слишком большой")
    try:
        body = json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="некорректный JSON")
    try:
        kills, other, skipped = _parse_killfeed_batch(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if len(kills) > _KILLFEED_MAX_KILLS:
        raise HTTPException(status_code=413, detail="слишком много убийств в батче")
    if kills:
        factions = await _killfeed_factions(server_id, srv, kills)
        for kill in kills:
            kill["killer_faction"] = factions.get(kill["killer_id"]) if kill["killer_id"] else None
            kill["victim_faction"] = factions.get(kill["victim_id"])
            kill["teamkill"] = bool(
                not kill["suicide"]
                and kill["killer_id"]
                and kill["killer_faction"]
                and kill["killer_faction"] == kill["victim_faction"]
            )
    accepted = await db.record_kill_events(server_id, kills)
    return {
        "ok": True,
        "accepted": accepted,
        "duplicates": len(kills) - accepted,
        "other": other,
        "skipped": skipped,
    }


@router.get("/killfeed")
async def killfeed_list(request: Request, server: str = "", limit: int = 200, kind: str = "all"):
    """Лента убийств сервера: все, союзные, хедшоты, суициды, окружение."""
    await _require_perm(request, "players_view")
    srv = _get_server(server)
    if not srv:
        raise _not_found(server)
    entries = await db.recent_kills(srv.id, limit, kind)
    stats = await db.killfeed_stats(srv.id)
    return {"ok": True, "entries": entries, "stats": stats}


@router.get("/killfeed/status")
async def killfeed_status(request: Request, server: str = ""):
    """Жив ли килфид сервера и куда он должен слать события."""
    await _require_perm(request, "players_view")
    srv = _get_server(server)
    if not srv:
        raise _not_found(server)
    stats = await db.killfeed_stats(srv.id)
    return {
        "ok": True,
        "configured": bool(config.KILLFEED_TOKEN),
        "url": f"{config.PUBLIC_URL}/api/killfeed/{srv.id}",
        "last_received_utc": stats["last_received_utc"],
        "total": stats["total"],
    }


@router.get("/killfeed/setup")
async def killfeed_setup(request: Request, server: str = ""):
    """Строки для ServerSettings.ini, чтобы сервер начал слать килфид на панель."""
    await _require_perm(request, "config_edit")
    srv = _get_server(server)
    if not srv:
        raise _not_found(server)
    url = f"{config.PUBLIC_URL}/api/killfeed/{srv.id}"
    lines = [
        "bEventFeedEnabled=True",
        f'+EventFeedEndpoints=(Url="{url}",Token="{config.KILLFEED_TOKEN}")',
        f'EventFeedServerName="{srv.name.replace(chr(34), "").replace(chr(13), "").replace(chr(10), "")}"',
    ]
    return {"ok": True, "url": url, "lines": lines}


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
    # Discord-роль VIP: выдаём при добавлении, снимаем при истечении
    # (только если у игрока не осталось других активных слотов).
    try:
        if add:
            await dmod.grant_vip_role(item["steam_id"])
        else:
            if not await _has_active_vip_slot_anywhere(item["steam_id"]):
                await dmod.revoke_vip_role(item["steam_id"])
    except Exception:
        log.exception("VIP role sync failed for %s", item["steam_id"])
    return True, ""


async def _has_active_vip_slot_anywhere(steam_id: str) -> bool:
    """Есть ли у игрока хотя бы один активный VIP-слот на любом сервере."""
    now = time.time()
    for srv in POOL.values():
        try:
            for slot in await db.list_vip_slots(srv.id):
                if slot["steam_id"] == steam_id and (
                    slot["expires_utc"] is None or slot["expires_utc"] > now
                ):
                    return True
        except Exception:
            pass
    return False


async def expire_vip_slots():
    """Background-safe expiry pass called by the application maintenance loop."""
    for item in await db.list_expired_vip_slots():
        ok, error = await _sync_vip_record(item, add=False)
        if ok:
            await db.log_site_audit(None, item["server_id"], "vip.expire", item["steam_id"])
            _srv = _get_server(item["server_id"])
            disclog.emit(
                "vip.expire", actor="автоматически",
                server=(_srv.name if _srv else item["server_id"]),
                steam_id=item["steam_id"],
            )
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
        disclog.emit(
            "ban.expire", actor="автоматически", server=srv.name, steam_id=item["steam_id"],
        )


async def process_queued_bans():
    """Выдаёт отложенные оффлайн-баны, как только игрок появился в /v1/players."""
    entries = await db.list_queued_bans()
    if not entries:
        return
    now = time.time()
    online = {}
    for srv in POOL.values():
        try:
            data = await srv.api.players()
            online[srv.id] = {
                str(p.get("steamId") or "")
                for p in (data.get("players") or [])
                if isinstance(p, dict)
            }
        except RCONError as e:
            log.debug("Очередь банов: не удалось получить игроков %s: %s", srv.id, e.message)
            online[srv.id] = None
    for entry in entries:
        steam_id = entry["steam_id"]
        if entry["expires_utc"] is not None and entry["expires_utc"] <= now:
            # Срок истёк до появления игрока — бан просто снимаем из очереди.
            await db.delete_queued_ban(steam_id)
            audit_sid = entry["applied_servers"][0] if entry["applied_servers"] else next(iter(POOL), "queue")
            await db.log_site_audit(None, audit_sid, "ban.expire", steam_id)
            disclog.emit(
                "ban.expire", actor="автоматически",
                server=(_get_server(audit_sid).name if _get_server(audit_sid) else audit_sid),
                steam_id=steam_id, detail="очередь: срок истёк до входа игрока",
            )
            continue
        applied = set(entry["applied_servers"])
        issued = False
        for srv in POOL.values():
            if srv.id in applied or not online.get(srv.id):
                continue
            if steam_id not in online[srv.id]:
                continue
            body = {"steamId": steam_id}
            if entry["reason"]:
                body["reason"] = entry["reason"]
            try:
                await srv.api.request("POST", "/v1/bans", json_body=body)
            except RCONError as e:
                if e.status == 409:
                    pass
                else:
                    log.warning("Очередь банов: выдача %s на %s не удалась: %s", steam_id, srv.id, e.message)
                    continue
            applied.add(srv.id)
            issued = True
            await db.save_managed_ban(
                srv.id, steam_id, entry["display_name"], entry["reason"],
                entry["expires_utc"], entry["created_by"],
            )
            await _invalidate(srv.id)
        if not issued:
            continue
        await db.mark_queued_ban_applied(steam_id, sorted(applied))
        if set(POOL.keys()) and applied >= set(POOL.keys()):
            # Бан выдан на всех серверах панели — очередь больше не нужна.
            await db.delete_queued_ban(steam_id)
        first_sid = sorted(applied)[0]
        first_srv = _get_server(first_sid)
        await db.log_site_audit(entry["created_by"], first_sid, "ban.add", steam_id)
        disclog.emit(
            "ban.add", actor=entry["created_by"],
            server=(first_srv.name if first_srv else first_sid),
            steam_id=steam_id,
            detail=" · ".join(x for x in (
                entry["reason"], entry["display_name"],
                "применён из очереди (вошёл в игру)",
                f"не выдан: {', '.join(sorted(set(POOL.keys()) - applied))}" if set(POOL.keys()) - applied else "",
            ) if x),
        )


async def ban_queue_loop(stop_event):
    """Фоновый цикл: применение очереди оффлайн-банов каждые 15 секунд."""
    while not stop_event.is_set():
        try:
            await process_queued_bans()
        except Exception:
            log.exception("Ошибка обработки очереди банов")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=15)
        except asyncio.TimeoutError:
            pass


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
    srv = _get_server(sid)
    if not srv:
        return _not_found(sid)
    payload = await _validated(request, VipSlotSave)
    steam_id = payload.steam
    display_name = " ".join(str(payload.display_name or "").split())[:120]
    note = str(payload.note or "").strip()[:500]
    expires_utc = _parse_vip_expiry(payload.expires_utc)
    idem_key, replay = await _idempotency_begin(
        request, user["steam_id"], _request_fingerprint(request, await _json_object(request))
    )
    if replay:
        return replay
    item = await db.save_vip_slot(
        sid, steam_id, display_name, expires_utc, note, user["steam_id"]
    )
    synced, error = await _sync_vip_record(item, add=True)
    # VIP действует на все серверы: дублируем entitlement и managed-запись.
    mirrored = []
    for other in _other_servers(sid):
        other_item = await db.save_vip_slot(
            other.id, steam_id, display_name, expires_utc, note, user["steam_id"]
        )
        other_synced, other_error = await _sync_vip_record(other_item, add=True)
        if other_synced:
            mirrored.append(other.id)
        else:
            log.warning(
                "Зеркальный VIP %s не синхронизирован на %s: %s",
                steam_id, other.id, other_error,
            )
    item = await db.get_vip_slot(item["id"], sid)
    await db.log_site_audit(
        user["steam_id"], sid, "vip.save", f"{steam_id}:{'synced' if synced else 'error'}"
    )
    disclog.emit(
        "vip.save", actor=user["steam_id"], server=srv.name, steam_id=steam_id,
        detail=" · ".join(x for x in (
            note or ("синхронизировано" if synced else f"ошибка: {error}"),
            f"зеркало: {', '.join(mirrored)}" if mirrored else "",
        ) if x),
    )
    response = {
        "ok": True, "item": item, "synced": synced,
        "warning": error or None, "mirrored": mirrored,
    }
    await _idempotency_complete(user["steam_id"], idem_key, response)
    return response


@router.post("/server/{sid}/vip-slots/{vip_id}/sync")
async def vip_slot_sync(sid: str, vip_id: str, request: Request):
    user = await _require_perm(request, "vip_edit")
    srv = _get_server(sid)
    if not srv:
        return _not_found(sid)
    if not re.fullmatch(r"[0-9a-f]{24}", str(vip_id or "")):
        raise HTTPException(status_code=400, detail="Некорректный ID VIP")
    item = await db.get_vip_slot(vip_id, sid)
    if not item:
        raise HTTPException(status_code=404, detail="VIP-запись не найдена")
    active = item["expires_utc"] is None or item["expires_utc"] > time.time()
    synced, error = await _sync_vip_record(item, add=active)
    # Ручной re-sync зеркалим на остальные серверы по steam_id.
    for other in _other_servers(sid):
        other_item = next(
            (row for row in await db.list_vip_slots(other.id) if row["steam_id"] == item["steam_id"]),
            None,
        )
        if other_item:
            await _sync_vip_record(other_item, add=active)
    await db.log_site_audit(
        user["steam_id"], sid, "vip.sync", f"{item['steam_id']}:{'ok' if synced else 'error'}"
    )
    disclog.emit(
        "vip.sync", actor=user["steam_id"], server=srv.name, steam_id=item["steam_id"],
        detail=("синхронизировано" if synced else f"ошибка: {error}"),
    )
    return {
        "ok": synced,
        "item": await db.get_vip_slot(vip_id, sid),
        "detail": error or None,
    }


@router.delete("/server/{sid}/vip-slots/{vip_id}")
async def vip_slot_delete(sid: str, vip_id: str, request: Request):
    user = await _require_perm(request, "vip_edit")
    srv = _get_server(sid)
    if not srv:
        return _not_found(sid)
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
    # Снятие VIP тоже глобальное: снимаем entitlement и чистим записи зеркальных серверов.
    for other in _other_servers(sid):
        other_item = next(
            (row for row in await db.list_vip_slots(other.id) if row["steam_id"] == item["steam_id"]),
            None,
        )
        if not other_item:
            continue
        other_synced, other_error = await _sync_vip_record(other_item, add=False)
        if other_synced:
            await db.delete_vip_slot(other_item["id"], other.id)
        else:
            log.warning(
                "Зеркальное снятие VIP %s не удалось на %s: %s",
                item["steam_id"], other.id, other_error,
            )
    await db.delete_vip_slot(vip_id, sid)
    await db.log_site_audit(user["steam_id"], sid, "vip.delete", item["steam_id"])
    disclog.emit(
        "vip.delete", actor=user["steam_id"], server=srv.name, steam_id=item["steam_id"],
    )
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
    payload = await _validated(request, ReservedSlotsLimit)
    value = payload.max_reserved_slots
    idem_key, replay = await _idempotency_begin(request, user["steam_id"], _request_fingerprint(request, await _json_object(request)))
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
    payload = await _validated(request, ReservedSlotsAdd)
    steam_id = payload.steam
    idem_key, replay = await _idempotency_begin(request, user["steam_id"], _request_fingerprint(request, await _json_object(request)))
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
    payload = await _validated(request, BroadcastRequest)
    message = payload.message
    idem_key, replay = await _idempotency_begin(request, user["steam_id"], _request_fingerprint(request, await _json_object(request)))
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
    payload = await _validated(request, BanAdd)
    steam_id = payload.steamId
    reason = str(payload.reason or "").strip()[:500]
    display_name = " ".join(str(payload.display_name or "").split())[:120]
    if not display_name:
        try:
            user_row = await db.get_user(steam_id)
            if user_row and (user_row.get("persona") or "").strip():
                display_name = user_row["persona"].strip()[:120]
        except Exception:
            pass
    if not display_name and config.STEAM_API_KEY:
        try:
            personas = await steam_mod.fetch_personas([steam_id])
            persona = (personas.get(steam_id) or "").strip()[:120]
            if persona:
                display_name = persona
        except Exception:
            pass
    if display_name:
        try:
            await db.upsert_user(steam_id, display_name)
        except Exception:
            pass
    expires_utc = _parse_vip_expiry(payload.expires_utc)
    idem_key, replay = await _idempotency_begin(request, user["steam_id"], _request_fingerprint(request, await _json_object(request)))
    if replay:
        return replay
    if not await _allow_action_once(user["steam_id"], sid, "ban.add", steam_id):
        await _idempotency_abort(user["steam_id"], idem_key)
        return JSONResponse({"error": {"code": "duplicate_action", "message": "Повторите действие через секунду"}}, status_code=409)
    applied, pending, primary_error = await _apply_ban_across(sid, steam_id, reason)
    if primary_error is not None:
        await _idempotency_abort(user["steam_id"], idem_key)
        return _rcon_error(primary_error)
    # Игрока нет ни на одном сервере — бан уходит в очередь панели:
    # выдадим его при первом же появлении игрока в /v1/players.
    if pending:
        await db.enqueue_queued_ban(
            steam_id, display_name, reason, expires_utc, user["steam_id"], applied
        )
    # Бан действует на все серверы: сохраняем managed-записи по применённым.
    managed = None
    for applied_sid in applied:
        saved = await db.save_managed_ban(
            applied_sid, steam_id, display_name, reason, expires_utc, user["steam_id"]
        )
        if applied_sid == sid:
            managed = saved
        await _invalidate(applied_sid)
    await db.log_site_audit(user["steam_id"], sid, "ban.add", steam_id)
    mirrored = [s for s in applied if s != sid]
    disclog.emit(
        "ban.add", actor=user["steam_id"], server=srv.name, steam_id=steam_id,
        detail=" · ".join(x for x in (
            reason, display_name,
            f"зеркало: {', '.join(mirrored)}" if mirrored else "",
            "в очереди до входа игрока" if pending else "",
        ) if x),
    )
    response = {
        "ok": True, "managed_ban": managed, "mirrored": mirrored,
        "queued": bool(pending), "queued_servers": pending,
    }
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
    # Запись могла жить только в очереди (игрока не было онлайн) — снимаем её.
    queued_removed = await db.delete_queued_ban(steam_id)
    try:
        await srv.api.request("DELETE", f"/v1/bans/{steam_id}")
    except RCONError as e:
        if e.status != 404:
            await _idempotency_abort(user["steam_id"], idem_key)
            return _rcon_error(e)
    # Снятие тоже глобальное: чистим остальные серверы и их managed-записи.
    others = _other_servers(sid)
    for other in others:
        try:
            await other.api.request("DELETE", f"/v1/bans/{steam_id}")
        except RCONError as e:
            if e.status != 404:
                log.warning("Зеркальное снятие банa %s не удалось на %s: %s", steam_id, other.id, e.message)
    for applied_sid in [sid] + [s.id for s in others]:
        await db.delete_managed_ban(applied_sid, steam_id)
        await _invalidate(applied_sid)
    await db.log_site_audit(user["steam_id"], sid, "ban.remove", steam_id)
    disclog.emit("ban.remove", actor=user["steam_id"], server=srv.name, steam_id=steam_id)
    await _idempotency_complete(user["steam_id"], idem_key, {"ok": True, "queued_removed": queued_removed})
    return {"ok": True, "queued_removed": queued_removed}


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
    queued = await db.get_queued_ban(steam_id)
    if current is None and queued is None:
        raise HTTPException(status_code=404, detail="Бан не управляется этой панелью")
    payload = await _validated(request, BanUpdate)
    base = current or queued
    reason = " ".join(str((payload.reason or base["reason"]) or "").split())[:500]
    display_name = base["display_name"]
    if payload.expires_utc is not None:
        expires_utc = _parse_vip_expiry(payload.expires_utc)
    else:
        expires_utc = base["expires_utc"]
    if current is None:
        # Бан ещё не выдавался (игрока не было онлайн) — правим только очередь.
        await db.update_queued_ban(steam_id, reason, expires_utc)
        await db.log_site_audit(user["steam_id"], sid, "ban.update", steam_id)
        disclog.emit(
            "ban.update", actor=user["steam_id"], server=srv.name,
            steam_id=steam_id, detail=f"{reason} (в очереди)",
        )
        return {"ok": True, "queued": True}
    # Живой бан: снимаем на всех серверах и выдаём заново с новыми полями.
    try:
        await srv.api.request("DELETE", f"/v1/bans/{steam_id}")
    except RCONError as exc:
        if exc.status != 404:
            return _rcon_error(exc)
    for other in _other_servers(sid):
        try:
            await other.api.request("DELETE", f"/v1/bans/{steam_id}")
        except RCONError as exc:
            if exc.status != 404:
                log.warning("Зеркальное снятие бана %s не удалось на %s: %s", steam_id, other.id, exc.message)
    applied, pending, primary_error = await _apply_ban_across(sid, steam_id, reason)
    if primary_error is not None:
        # Восстановление со старой причиной (best-effort).
        await _apply_ban_across(sid, steam_id, current["reason"])
        return _rcon_error(primary_error)
    if pending:
        await db.enqueue_queued_ban(
            steam_id, display_name, reason, expires_utc, user["steam_id"], applied
        )
    else:
        await db.delete_queued_ban(steam_id)
    managed = None
    for applied_sid in applied:
        saved = await db.save_managed_ban(
            applied_sid, steam_id, display_name, reason, expires_utc, user["steam_id"]
        )
        if applied_sid == sid:
            managed = saved
        await _invalidate(applied_sid)
    await db.log_site_audit(user["steam_id"], sid, "ban.update", steam_id)
    disclog.emit(
        "ban.update", actor=user["steam_id"],
        server=(srv.name if srv else sid), steam_id=steam_id,
        detail=" · ".join(x for x in (reason, "в очереди до входа игрока" if pending else "") if x),
    )
    return {"ok": True, "managed_ban": managed, "queued": bool(pending)}


@router.post("/server/{sid}/bans/bulk")
async def server_bans_bulk(sid: str, request: Request):
    """Ограниченная последовательная пакетная операция с отчётом по каждой записи."""
    user = await _require_perm(request, "ban_remove")
    srv = _get_server(sid)
    if not srv:
        return _not_found(sid)
    payload = await _validated(request, BansBulkRequest)
    action = payload.action
    steam_ids = list(dict.fromkeys(str(value) for value in payload.steam_ids))
    if action != "unban" or not steam_ids or len(steam_ids) > 50:
        raise HTTPException(status_code=400, detail="Поддерживается снятие от 1 до 50 банов")
    steam_ids = [_validate_steam_id(value) for value in steam_ids]
    results = []
    others = _other_servers(sid)
    for steam_id in steam_ids:
        try:
            await srv.api.request("DELETE", f"/v1/bans/{steam_id}")
            ok = True
        except RCONError as exc:
            if exc.status == 404:
                ok = True
            else:
                results.append({"steam_id": steam_id, "ok": False, "error": exc.message})
                continue
        # Снятие зеркалим на остальные серверы (best-effort).
        for other in others:
            try:
                await other.api.request("DELETE", f"/v1/bans/{steam_id}")
            except RCONError as exc:
                if exc.status != 404:
                    log.warning("Зеркальное снятие банa %s не удалось на %s: %s", steam_id, other.id, exc.message)
        for applied_sid in [sid] + [s.id for s in others]:
            await db.delete_managed_ban(applied_sid, steam_id)
        await db.delete_queued_ban(steam_id)
        results.append({"steam_id": steam_id, "ok": ok})
    for applied_sid in [sid] + [s.id for s in others]:
        await _invalidate(applied_sid)
    succeeded = sum(1 for result in results if result["ok"])
    await db.log_site_audit(
        user["steam_id"], sid, "ban.bulk.remove", f"{succeeded}/{len(results)}"
    )
    disclog.emit(
        "ban.bulk.remove", actor=user["steam_id"], server=srv.name,
        detail=f"снято {succeeded} из {len(results)}",
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


def _swap_allowed(diff: int, has_vip: bool) -> bool:
    """Штатный баланс: перевес до +2 всегда, до +4 — только с активным VIP-слотом."""
    return diff <= (4 if has_vip else 2)


async def _has_active_vip_slot(sid, steam_id):
    now = time.time()
    for slot in await db.list_vip_slots(sid):
        if slot["steam_id"] == steam_id and (slot["expires_utc"] is None or slot["expires_utc"] > now):
            return True
    return False


# SW cooldown: одна смена стороны за игру. Ключ — (sid, steam_id),
# значение — map, на которой был сделан свап. Смена карты = новая игра.
_sw_cooldown: dict[tuple[str, str], str] = {}


async def swap_player(srv, sid, steam_id, faction):
    """Ядро SW: (ok, code, message). Проверка перевеса ДО свапа, затем PATCH."""
    if not faction or len(faction) > 80:
        return False, "missing_field", "поле faction"
    data = await srv.api.players()
    players = (data or {}).get("players") or []
    current = next((p for p in players if str(p.get("steamId") or "") == steam_id), None)
    if current is None:
        return False, "not_online", "игрок не в сети"
    source = str(current.get("faction") or "")
    if source.lower() == faction.lower():
        return False, "same_faction", "игрок уже в этой стороне"
    # Кулдаун: 1 свап за игру (игра = текущая карта).
    try:
        status = await srv.api.status()
        game_key = str((status or {}).get("map") or "")
    except Exception:
        game_key = ""
    if game_key:
        prev = _sw_cooldown.get((sid, steam_id))
        if prev == game_key:
            return False, "cooldown", "уже был свап на этой карте — 1 раз за игру"
    def count(name):
        return sum(1 for p in players if str(p.get("faction") or "").lower() == name.lower())
    diff = abs(count(source) - count(faction))
    if not _swap_allowed(diff, await _has_active_vip_slot(sid, steam_id)):
        return False, "balance", f"перевес {diff} больше допустимого (до +2, с VIP-слотом до +4)"
    await srv.api.request("PATCH", f"/v1/players/{steam_id}", json_body={"faction": faction})
    if game_key:
        _sw_cooldown[(sid, steam_id)] = game_key
    return True, "swapped", f"{steam_id} переведён в «{faction}» (перевес был {diff})"


@router.post("/server/{sid}/players/{steam_id}/swap")
async def server_player_swap(sid: str, steam_id: str, request: Request):
    """SW: смена стороны с проверкой перевеса (до свапа)."""
    steam_id = _validate_steam_id(steam_id)
    payload = await _validated(request, PlayerSwapRequest)
    faction = str(payload.faction or "").strip()
    async def action(srv):
        ok, code, message = await swap_player(srv, sid, steam_id, faction)
        if not ok:
            status = 409 if code in {"not_online", "balance", "cooldown"} else 400
            return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)

    return await _server_action(sid, request, "move_faction", action, f"{steam_id} -> {faction}")


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
    version = srv.info.get("version") if isinstance(srv.info.get("version"), dict) else {}
    return {
        "ok": True,
        "routes": sorted(srv.routes),
        "config_writable": srv.config_writable,
        "build": srv.build,
        "project": version.get("project") or {},
        "engine": version.get("engine") or {},
        "feed": srv.info.get("feed") or {},
    }

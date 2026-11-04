"""Discord OAuth2 (привязка аккаунта) и чтение ролей из гильдии."""

import logging
import time
import asyncio
import re
from urllib.parse import urlencode

import aiohttp

try:
    from . import config, db
except ImportError:
    import config
    import db

log = logging.getLogger("discord")
API = "https://discord.com/api/v10"
OAUTH_AUTH = "https://discord.com/api/oauth2/authorize"
OAUTH_TOKEN = "https://discord.com/api/v10/oauth2/token"

_guild_roles_cache = {"ts": 0.0, "map": {}, "retry_at": 0.0}

# После сбоя Discord не повторяем запрос сразу: при недоступном API каждый запрос
# панели иначе ждал бы таймаут, а overview стал бы висеть десятки секунд.
DISCORD_RETRY_SECONDS = 30
_guild_roles_lock = asyncio.Lock()
_member_locks = {}
_member_retry_at = {}

ALL_PERMS = {
    "players_view", "reserved_view", "reserved_edit",
    "vip_view", "vip_edit",
    "tickets_view", "tickets_edit",
    "broadcast", "kick", "kill", "message", "move_faction",
    "player_notes", "player_tags", "clans",
    "ban_view", "ban_add", "ban_remove",
    "change_map", "change_lighting", "match_end", "match_restart",
    "audit", "config_view", "config_edit", "roles",
}
# Не входит в ALL_PERMS намеренно: роли и личный доступ это право выдать не могут.
OWNER_PERM = "owner"
DISCORD_ID_RE = re.compile(r"^\d{1,20}\Z")


def redirect_uri():
    return f"{config.PUBLIC_URL}/api/auth/discord/callback"


def authorize_url(state="", prompt_none=False):
    params = {
        "client_id": config.DISCORD_CLIENT_ID,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": "identify",
    }
    if state:
        params["state"] = state
    if prompt_none:
        params["prompt"] = "none"
    return f"{OAUTH_AUTH}?{urlencode(params)}"


async def _post_form(url, data, headers=None):
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, data=data, headers=headers) as resp:
            return resp.status, await resp.text()


async def exchange_code(code):
    """Обменивает code на токен и возвращает (discord_id, name)."""
    if not (config.DISCORD_CLIENT_ID and config.DISCORD_CLIENT_SECRET):
        return None, None
    data = {
        "client_id": config.DISCORD_CLIENT_ID,
        "client_secret": config.DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri(),
    }
    try:
        status, text = await _post_form(OAUTH_TOKEN, data, headers={"Accept": "application/json"})
    except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
        log.warning("Discord token exchange failed")
        return None, None
    if status != 200:
        log.warning("Discord token exchange: HTTP %s", status)
        return None, None
    import json as _json

    try:
        token = _json.loads(text).get("access_token", "")
    except (TypeError, ValueError):
        return None, None
    if not token:
        return None, None
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                f"{API}/users/@me",
                headers={"Authorization": f"Bearer {token}"},
                allow_redirects=False,
            ) as resp:
                if resp.status != 200:
                    return None, None
                me = await resp.json()
                if not isinstance(me, dict):
                    return None, None
    except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
        log.warning("Discord user lookup failed")
        return None, None
    discord_id = str(me.get("id") or "")
    if not DISCORD_ID_RE.fullmatch(discord_id):
        return None, None
    name = str(me.get("global_name") or me.get("username") or "")[:120]
    return discord_id, name


async def _guild_roles():
    """Карта role_id -> name (кэш 10 минут)."""
    now = time.monotonic()
    if now - _guild_roles_cache["ts"] < 600 and _guild_roles_cache["map"]:
        return _guild_roles_cache["map"]
    if not (config.DISCORD_BOT_TOKEN and config.GUILD_ID):
        return {}
    # Под блокировкой: иначе при холодном старте параллельный запрос получил бы пустую
    # карту ролей, и права, выданные по имени роли, на мгновение пропали бы.
    async with _guild_roles_lock:
        now = time.monotonic()
        if now - _guild_roles_cache["ts"] < 600 and _guild_roles_cache["map"]:
            return _guild_roles_cache["map"]
        if now < _guild_roles_cache.get("retry_at", 0.0):
            return _guild_roles_cache["map"]
        # Успешный ответ обновит кэш, а при сбое пауза останется в силе.
        _guild_roles_cache["retry_at"] = now + DISCORD_RETRY_SECONDS
        return await _fetch_guild_roles(now)


async def _fetch_guild_roles(now):
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                f"{API}/guilds/{config.GUILD_ID}/roles",
                headers={"Authorization": f"Bot {config.DISCORD_BOT_TOKEN}"},
            ) as resp:
                if resp.status != 200:
                    log.warning("roles: HTTP %s", resp.status)
                    return _guild_roles_cache["map"]
                roles = await resp.json()
    except (aiohttp.ClientError, OSError) as e:
        log.warning("roles fetch failed: %s", e)
        return _guild_roles_cache["map"]
    if not isinstance(roles, list):
        return _guild_roles_cache["map"]
    role_map = {}
    for role in roles:
        if not isinstance(role, dict) or not role.get("id"):
            continue
        role_map[str(role["id"])[:64]] = str(role.get("name") or "")[:100]
    _guild_roles_cache["ts"] = now
    _guild_roles_cache["map"] = role_map
    return _guild_roles_cache["map"]


async def fetch_member(discord_id):
    """Возвращает (member_roles_ids, guild_member_bool) или (None, False) при ошибке."""
    if not (config.DISCORD_BOT_TOKEN and config.GUILD_ID) or not DISCORD_ID_RE.fullmatch(str(discord_id or "")):
        return None, False
    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                f"{API}/guilds/{config.GUILD_ID}/members/{discord_id}",
                headers={"Authorization": f"Bot {config.DISCORD_BOT_TOKEN}"},
            ) as resp:
                if resp.status == 200:
                    member = await resp.json()
                    if not isinstance(member, dict):
                        return None, False
                    roles = [str(r)[:64] for r in (member.get("roles") or []) if str(r).strip()][:100]
                    return roles, True
                if resp.status == 404:
                    return [], False
                log.warning("member: HTTP %s", resp.status)
                return None, False
    except (aiohttp.ClientError, OSError, ValueError) as e:
        log.warning("member fetch failed: %s", e)
        return None, False


def is_admin_role(role_id, name):
    if role_id in config.ADMIN_ROLE_IDS:
        return True
    return name in config.ADMIN_ROLE_NAMES


async def resolve_status(binding):
    """Обновлённая роль пользователя по привязке: {discord_id, name, member, admin}.

    Перечит из Discord, если кэш в БД устарел (ROLES_TTL).
    """
    if not binding:
        return {"bound": False, "discord_id": None, "discord_name": "", "member": False, "admin": False}
    unverified = False
    if _roles_stale(binding):
        unverified = not await _refresh_binding(binding)
    status = await _status(binding)
    status["unverified"] = unverified
    return status


def _roles_stale(binding):
    return time.time() - (binding.get("updated_utc") or 0) > config.ROLES_TTL_SECONDS


async def _refresh_binding(binding):
    """Перечитывает роли устаревшей привязки. False — Discord их не подтвердил.

    Одновременные запросы одного пользователя делают один поход в Discord.
    """
    discord_id = binding["discord_id"]
    lock = _member_locks.setdefault(discord_id, asyncio.Lock())
    async with lock:
        if binding.get("user_id"):
            current = await db.get_binding(binding["user_id"])
            if current and current["discord_id"] == discord_id and not _roles_stale(current):
                binding.update(current)
                return True
        if time.monotonic() >= _member_retry_at.get(discord_id, 0.0):
            roles, member = await fetch_member(discord_id)
            if roles is not None:
                _member_retry_at.pop(discord_id, None)
                stored_roles = roles if member else []
                await db.upsert_binding(binding.get("user_id") or "", discord_id, binding["discord_name"], stored_roles, member)
                binding["roles"] = roles
                binding["guild_member"] = member
                binding["updated_utc"] = time.time()
                return True
            _member_retry_at[discord_id] = time.monotonic() + DISCORD_RETRY_SECONDS
    # Fail closed if Discord cannot re-confirm a stale membership.
    binding["roles"] = []
    binding["guild_member"] = False
    return False


async def _status(binding):
    role_map = await _guild_roles()
    member = bool(binding.get("guild_member"))
    granted = False
    for rid in binding.get("roles") or []:
        if is_admin_role(rid, role_map.get(rid, "")):
            granted = True
            break
    return {
        "bound": True,
        "discord_id": binding.get("discord_id"),
        "discord_name": binding.get("discord_name") or "",
        "member": member,
        "admin": member and granted,
    }


async def guild_roles():
    """Публичная обёртка над кэшем ролей гильдии (id -> name)."""
    return await _guild_roles()


async def guild_voice_channels():
    """Голосовые каналы гильдии для диагностики временных комнат."""
    if not (config.DISCORD_BOT_TOKEN and config.GUILD_ID):
        return []
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                f"{API}/guilds/{config.GUILD_ID}/channels",
                headers={"Authorization": f"Bot {config.DISCORD_BOT_TOKEN}"},
            ) as resp:
                if resp.status != 200:
                    return []
                channels = await resp.json()
    except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
        return []
    return [
        {
            "id": str(channel.get("id") or ""),
            "name": str(channel.get("name") or "")[:100],
            "parent_id": str(channel.get("parent_id") or ""),
            "user_limit": int(channel.get("user_limit") or 0),
            "position": int(channel.get("position") or 0),
        }
        for channel in channels
        if isinstance(channel, dict) and channel.get("type") in {2, 13} and channel.get("id")
    ]


async def bot_panel_request(method, path, payload=None):
    """Вызов функционала бота напрямую (in-process) — бот встроен в панель."""
    from .bot.runner import bot as _bot

    if not _bot.is_ready():
        return None, "Бот ещё не запущен"
    try:
        if path == "/api/tempvoice" and method == "GET":
            cog = _bot.get_cog("TempVoiceCog")
            if cog and hasattr(cog, "panel_list_channels"):
                return await cog.panel_list_channels(), ""
            return None, "TempVoiceCog недоступен"
        if path.startswith("/api/tempvoice/") and path.endswith("/delete") and method == "POST":
            channel_id = path.split("/")[3]
            cog = _bot.get_cog("TempVoiceCog")
            if cog and hasattr(cog, "panel_delete_channel"):
                return await cog.panel_delete_channel(int(channel_id)), ""
            return None, "TempVoiceCog недоступен"
        if path.startswith("/api/tempvoice/") and path.endswith("/transfer") and method == "POST":
            channel_id = path.split("/")[3]
            owner_id = str((payload or {}).get("owner_id") or "")
            cog = _bot.get_cog("TempVoiceCog")
            if cog and hasattr(cog, "panel_transfer_channel"):
                return await cog.panel_transfer_channel(int(channel_id), int(owner_id)), ""
            return None, "TempVoiceCog недоступен"
        if path == "/api/vip-panel-refresh" and method == "POST":
            cog = _bot.get_cog("DonationCog")
            if cog and hasattr(cog, "panel_refresh_vip"):
                return await cog.panel_refresh_vip(), ""
            return None, "DonationCog недоступен"
        if path == "/api/vip-tickets" and method == "GET":
            cog = _bot.get_cog("TicketCog")
            if cog and hasattr(cog, "panel_list_vip_tickets"):
                return cog.panel_list_vip_tickets(), ""
            return None, "TicketCog недоступен"
        if path == "/api/vip-ticket/close" and method == "POST":
            cog = _bot.get_cog("TicketCog")
            if cog and hasattr(cog, "panel_close_vip_ticket"):
                return await cog.panel_close_vip_ticket(payload or {}), ""
            return None, "TicketCog недоступен"
        if path == "/api/vip-ticket" and method == "POST":
            cog = _bot.get_cog("TicketCog")
            if cog and hasattr(cog, "panel_open_vip_ticket"):
                return await cog.panel_open_vip_ticket(payload or {}), ""
            return None, "TicketCog недоступен"
        if path == "/api/clan-applications" and method == "POST":
            cog = _bot.get_cog("ClanTicketCog")
            if cog and hasattr(cog, "panel_submit_application"):
                return await cog.panel_submit_application(payload or {}), ""
            return None, "ClanTicketCog недоступен"
        return None, "Недопустимый маршрут API бота"
    except Exception as exc:
        log.warning("bot in-process call failed: %s", exc, exc_info=True)
        return None, "Ошибка вызова бота"


async def grant_vip_role(steam_id: str) -> bool:
    """Выдаёт VIP-роль в гильдии по привязке steam→discord.

    Возвращает True, если роль выдана (или уже была). False — ошибка/нет привязки.
    """
    if not config.VIP_ROLE_ID or not config.DISCORD_BOT_TOKEN or not config.GUILD_ID:
        return False
    binding = await db.get_binding(steam_id)
    if not binding or not binding.get("discord_id") or not binding.get("guild_member"):
        return False
    discord_id = str(binding["discord_id"])
    if not DISCORD_ID_RE.fullmatch(discord_id):
        return False
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.put(
                f"{API}/guilds/{config.GUILD_ID}/members/{discord_id}/roles/{config.VIP_ROLE_ID}",
                headers={"Authorization": f"Bot {config.DISCORD_BOT_TOKEN}"},
            ) as resp:
                if resp.status in (204, 200):
                    return True
                if resp.status == 204 or resp.status == 404:
                    # 404 = роль уже есть или участник не найден — проверяем
                    pass
                log.warning("vip role grant: HTTP %s for %s", resp.status, steam_id)
                return False
    except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
        log.warning("vip role grant failed for %s", steam_id)
        return False


async def revoke_vip_role(steam_id: str) -> bool:
    """Снимает VIP-роль в гильдии по привязке steam→discord."""
    if not config.VIP_ROLE_ID or not config.DISCORD_BOT_TOKEN or not config.GUILD_ID:
        return False
    binding = await db.get_binding(steam_id)
    if not binding or not binding.get("discord_id"):
        return False
    discord_id = str(binding["discord_id"])
    if not DISCORD_ID_RE.fullmatch(discord_id):
        return False
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.delete(
                f"{API}/guilds/{config.GUILD_ID}/members/{discord_id}/roles/{config.VIP_ROLE_ID}",
                headers={"Authorization": f"Bot {config.DISCORD_BOT_TOKEN}"},
            ) as resp:
                if resp.status in (204, 200, 404):
                    return True
                log.warning("vip role revoke: HTTP %s for %s", resp.status, steam_id)
                return False
    except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
        log.warning("vip role revoke failed for %s", steam_id)
        return False


def compute_permissions(binding, role_map, perms_map):
    """Права пользователя по его ролям в гильдии.

    perms_map: {role_ref: [perm, ...]} из DB (role_ref = id или имя роли).
    Роль без настроенного набора прав и попадающая в legacy ADMIN_ROLES
    получает все права (бутстрап, пока права не выданы явно).
    Личный доступ: ref "user:<discord_id>" выдаёт права только этому
    аккаунту Discord — независимо от ролей и состояния членства
    (работает даже когда Discord не подтверждает гильдию).
    """
    if not binding:
        return []
    granted = set()
    discord_id = str(binding.get("discord_id") or "")
    personal = perms_map.get(f"user:{discord_id}") if discord_id else None
    if personal is not None:
        granted.update(p for p in (personal or []) if p in ALL_PERMS)
    if not binding.get("guild_member"):
        return sorted(granted)
    for rid in binding.get("roles") or []:
        name = role_map.get(rid, "")
        configured = False
        for ref in (rid, name):
            if not ref:
                continue
            p = perms_map.get(ref)
            if p is not None:
                configured = True
                granted.update(p for p in (p or []) if p in ALL_PERMS)
        if not configured and (rid in config.ADMIN_ROLE_IDS or name in config.ADMIN_ROLE_NAMES):
            granted.update(ALL_PERMS)
    return sorted(granted)

"""Discord Interactions: slash-команда /sw без стороннего бота.

Панель сама принимает interactions (Ed25519-подпись через nacl),
регистрирует команду в гильдии и отвечает deferred-сообщением.
"""

import asyncio
import json
import logging
import re

import aiohttp
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

try:
    from . import config, db, discord as dmod
except ImportError:
    import config
    import db
    import discord as dmod

log = logging.getLogger("interactions")
API = "https://discord.com/api/v10"
EPHEMERAL = 64
STEAM64_RE = re.compile(r"^\d{17}$")

_app_cache = {"id": "", "public_key": "", "endpoint_url": ""}

SW_COMMAND = {
    "name": "sw",
    "description": "Смена стороны с проверкой перевеса (до +2, с VIP до +4)",
    "dm_permission": False,
    "options": [
        {
            "type": 3,
            "name": "server",
            "description": "Сервер",
            "required": True,
            "choices": [
                {"name": str(s.get("name") or s["id"])[:100], "value": str(s["id"])}
                for s in config.SERVERS
            ][:25],
        },
        {"type": 3, "name": "side", "description": "Новая сторона", "required": True, "autocomplete": True},
        {"type": 3, "name": "player", "description": "Игрок (SteamID64 или имя)", "required": True, "autocomplete": True},
    ],
}


async def application_info(force=False):
    """Кэш данных приложения: application_id, public_key, interactions URL."""
    if _app_cache["public_key"] and not force:
        return _app_cache
    if not config.DISCORD_BOT_TOKEN:
        return _app_cache
    timeout = aiohttp.ClientTimeout(total=15, connect=5)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                f"{API}/oauth2/applications/@me",
                headers={"Authorization": f"Bot {config.DISCORD_BOT_TOKEN}"},
            ) as resp:
                if resp.status != 200:
                    log.warning("Discord application info: HTTP %s", resp.status)
                    return _app_cache
                data = await resp.json()
    except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
        log.warning("Discord application info недоступен")
        return _app_cache
    _app_cache["id"] = str(data.get("id") or "")
    _app_cache["public_key"] = str(data.get("public_key") or "")
    _app_cache["endpoint_url"] = str(data.get("interactions_endpoint_url") or "")
    return _app_cache


async def public_key():
    if config.DISCORD_PUBLIC_KEY:
        return config.DISCORD_PUBLIC_KEY
    return (await application_info())["public_key"]


def verify(headers, body: bytes, key_hex: str) -> bool:
    sig = headers.get("x-signature-ed25519")
    ts = headers.get("x-signature-timestamp")
    if not (key_hex and sig and ts):
        return False
    try:
        VerifyKey(bytes.fromhex(key_hex)).verify(ts.encode() + body, bytes.fromhex(sig))
        return True
    except (BadSignatureError, ValueError, TypeError):
        return False


def _choices(items, query=""):
    """Ограничения Discord: до 25 choices, name/value <= 100 символов."""
    q = (query or "").lower()
    out = []
    for name, value in items:
        if q and q not in str(name).lower() and q not in str(value).lower():
            continue
        out.append({"name": str(name)[:100], "value": str(value)[:100]})
        if len(out) >= 25:
            break
    return out


def _get_pool_server(sid):
    from . import api
    return api._get_server(sid)


async def _autocomplete(payload):
    data = payload.get("data") or {}
    options = data.get("options") or []
    focused = next((o for o in options if o.get("focused")), None)
    if not focused:
        return {"type": 8, "data": {"choices": []}}
    sid = str(next((o.get("value") for o in options if o.get("name") == "server"), "") or "")
    srv = _get_pool_server(sid) if sid else None
    query = str(focused.get("value") or "")
    if not srv:
        return {"type": 8, "data": {"choices": []}}
    name = focused.get("name")
    try:
        if name == "side":
            status = await srv.api.status()
            factions = (status or {}).get("factionScores") or []
            return {
                "type": 8,
                "data": {"choices": _choices([(f.get("name", ""), f.get("name", "")) for f in factions], query)},
            }
        if name == "player":
            data_players = await srv.api.players()
            players = (data_players or {}).get("players") or []
            items = [
                (f"{p.get('name') or '?'} · {p.get('steamId', '')}", p.get("steamId", ""))
                for p in players
            ]
            return {"type": 8, "data": {"choices": _choices(items, query)}}
    except Exception:
        log.exception("SW autocomplete failed (%s)", name)
    return {"type": 8, "data": {"choices": []}}


async def _has_move_faction(member):
    """Право move_faction по РОЛЯМ из payload (без обращения к привязке)."""
    if not member:
        return False, None
    roles = member.get("roles") or []
    role_map = await dmod.guild_roles()
    perms_map = await db.get_role_perms_all()
    binding = {"guild_member": True, "roles": roles}
    perms = dmod.compute_permissions(binding, role_map, perms_map)
    user = member.get("user") or {}
    return "move_faction" in perms, str(user.get("id") or "") or None


async def _followup(payload, content):
    app_id = str(payload.get("application_id") or "")
    token = str(payload.get("token") or "")
    if not (app_id and token and config.DISCORD_BOT_TOKEN):
        return
    timeout = aiohttp.ClientTimeout(total=20, connect=5)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.patch(
                f"{API}/webhooks/{app_id}/{token}",
                headers={"Authorization": f"Bot {config.DISCORD_BOT_TOKEN}"},
                json={"content": str(content)[:1900], "flags": EPHEMERAL},
            ) as resp:
                if resp.status >= 400:
                    log.warning("SW followup: HTTP %s", resp.status)
    except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
        log.warning("SW followup не доставлен")


async def _execute(payload, opts, actor_steam_id):
    from . import api
    sid = str(opts.get("server") or "")
    side = str(opts.get("side") or "").strip()
    target = str(opts.get("player") or "").strip()
    message = ""
    try:
        steam_id = target if STEAM64_RE.fullmatch(target) else ""
        if not steam_id:
            message = "в autocomplete нужно выбрать игрока из списка"
        else:
            srv = _get_pool_server(sid)
            if not srv:
                message = "сервер не найден"
            else:
                try:
                    ok, code, msg = await api.swap_player(srv, sid, steam_id, side)
                except api.RCONError as exc:
                    ok, code, msg = False, "rcon", f"сервер недоступен: {exc}"
                message = msg
                if ok:
                    await db.log_site_audit(
                        actor_steam_id, sid, "rcon.move_faction",
                        f"discord /sw {steam_id} -> {side}",
                    )
    except Exception:
        log.exception("SW execution failed")
        message = "внутренняя ошибка панели"
    await _followup(payload, message)


async def _command(payload):
    data = payload.get("data") or {}
    member = payload.get("member") or {}
    if data.get("name") != "sw":
        return {"type": 4, "data": {"content": "Неизвестная команда", "flags": EPHEMERAL}}
    allowed, discord_id = await _has_move_faction(member)
    if not allowed:
        return {
            "type": 4,
            "data": {"content": "Нет права move_faction (проверьте роли в панели)", "flags": EPHEMERAL},
        }
    opts = {o.get("name"): o.get("value") for o in (data.get("options") or [])}
    side = str(opts.get("side") or "").strip()
    if not side or len(side) > 80:
        return {"type": 4, "data": {"content": "Укажите сторону", "flags": EPHEMERAL}}
    actor = await db.get_steam_by_discord(discord_id) if discord_id else None
    asyncio.create_task(_execute(payload, opts, actor))
    return {"type": 5, "data": {"flags": EPHEMERAL}}


async def handle(payload):
    kind = payload.get("type")
    if kind == 1:  # PING — проверка эндпоинта в Discord Dev Portal
        return {"type": 1}
    if kind == 4:  # AUTOCOMPLETE
        return await _autocomplete(payload)
    if kind == 2:  # APPLICATION_COMMAND
        return await _command(payload)
    return {"type": 4, "data": {"content": "Неподдерживаемый тип", "flags": EPHEMERAL}}


async def register_sw_command():
    """Регистрирует /sw в гильдии; ставит interactions endpoint, если он пуст."""
    if not (config.DISCORD_BOT_TOKEN and config.GUILD_ID):
        log.info("Discord /sw: не настроено (нет токена или GUILD_ID)")
        return False
    info = await application_info(force=True)
    app_id = info["id"] or config.DISCORD_CLIENT_ID
    if not app_id:
        log.warning("Discord /sw: application_id неизвестен")
        return False
    headers = {"Authorization": f"Bot {config.DISCORD_BOT_TOKEN}", "Content-Type": "application/json"}
    wanted = f"{config.PUBLIC_URL}/api/discord/interactions"
    timeout = aiohttp.ClientTimeout(total=20, connect=5)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if info["endpoint_url"] in ("", wanted):
                if info["endpoint_url"] != wanted:
                    async with session.patch(
                        f"{API}/applications/{app_id}",
                        headers=headers,
                        json={"interactions_endpoint_url": wanted},
                    ) as resp:
                        if resp.status >= 400:
                            log.warning(
                                "Discord /sw: не удалось выставить interactions URL (HTTP %s) — "
                                "укажите вручную: Developer Portal → Interactions Endpoint URL → %s",
                                resp.status, wanted,
                            )
            else:
                log.warning(
                    "Discord /sw: interactions URL уже занят (%s) — не трогаю, "
                    "команда может не сработать", info["endpoint_url"],
                )
            guild_url = f"{API}/applications/{app_id}/guilds/{config.GUILD_ID}/commands"
            async with session.get(guild_url, headers=headers) as resp:
                if resp.status != 200:
                    log.warning("Discord /sw: список команд HTTP %s", resp.status)
                    return False
                existing = await resp.json()
            current = next((c for c in existing if c.get("name") == "sw"), None)
            if current:
                async with session.patch(
                    f"{guild_url}/{current['id']}", headers=headers, json=SW_COMMAND
                ) as resp:
                    ok = resp.status == 200
            else:
                async with session.post(guild_url, headers=headers, json=SW_COMMAND) as resp:
                    ok = resp.status == 201
            log.info("Discord /sw: регистрация %s", "ok" if ok else "ошибка")
            return ok
    except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
        log.warning("Discord /sw: Discord API недоступен")
        return False


async def startup():
    await application_info(force=True)
    await register_sw_command()

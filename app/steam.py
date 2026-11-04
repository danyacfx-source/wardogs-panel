"""Сервисная часть Steam OpenID (вход через Steam) и Steam Web API (ник)."""

import asyncio
import logging
import re
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import aiohttp

try:
    from . import config
except ImportError:
    import config

log = logging.getLogger("steam")

# Аватар и баны меняются редко, а Steam отвечает долго. Неудачу помним недолго, чтобы
# сбой Steam не держал пустые данные и не заставлял каждый раз ждать таймаут.
CACHE_SECONDS = 600
MISS_CACHE_SECONDS = 60
CACHE_MAX_ENTRIES = 5000
STEAM_TIMEOUT_SECONDS = 6
_profile_cache = {}
_bans_cache = {}
_hours_cache = {}
_hours_fill_task = None


async def _through_cache(cache, steam_ids, fetch):
    now = time.monotonic()
    ids = [str(steam_id) for steam_id in steam_ids]
    result = {}
    missing = []
    for steam_id in ids:
        entry = cache.get(steam_id)
        if entry and entry[0] > now:
            if entry[1] is not None:
                result[steam_id] = entry[1]
        else:
            missing.append(steam_id)
    if missing:
        fetched = await fetch(missing)
        if len(cache) >= CACHE_MAX_ENTRIES:
            cache.clear()
        for steam_id in missing:
            value = fetched.get(steam_id)
            ttl = CACHE_SECONDS if value else MISS_CACHE_SECONDS
            cache[steam_id] = (now + ttl, value or None)
            if value:
                result[steam_id] = value
    return result


async def fetch_profiles(steam_ids):
    """Публичные профили Steam с кэшем: {steam_id: profile}."""
    if not config.STEAM_API_KEY or not steam_ids:
        return {}
    return await _through_cache(_profile_cache, steam_ids, _fetch_profiles)


async def _fetch_game_minutes(steam_ids):
    """Наигранные минуты. Steam отдаёт их только при открытых данных об играх в профиле."""
    result = {}
    timeout = aiohttp.ClientTimeout(total=STEAM_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for steam_id in steam_ids:
                async with session.get(
                    "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/",
                    params={
                        "key": config.STEAM_API_KEY,
                        "steamid": steam_id,
                        "include_played_free_games": "1",
                        "appids_filter[0]": config.STEAM_APP_ID,
                    },
                ) as response:
                    if response.status != 200:
                        continue
                    payload = await response.json()
                games = ((payload or {}).get("response") or {}).get("games") or []
                if games and isinstance(games[0], dict):
                    result[steam_id] = {"minutes": int(games[0].get("playtime_forever") or 0)}
    except (aiohttp.ClientError, OSError, TimeoutError, ValueError):
        pass
    return result


async def fetch_game_hours(steam_id):
    """Часы в игре по данным Steam или None, если профиль их скрывает."""
    if not config.STEAM_API_KEY or not config.STEAM_APP_ID:
        return None
    entry = (await _through_cache(_hours_cache, [steam_id], _fetch_game_minutes)).get(str(steam_id))
    return round(entry["minutes"] / 60) if entry else None


def cached_game_hours(steam_ids):
    """Часы из кэша, без обращения к Steam: список игроков не должен ждать внешний сервис."""
    now = time.monotonic()
    result = {}
    for steam_id in steam_ids:
        entry = _hours_cache.get(str(steam_id))
        if entry and entry[0] > now and entry[1]:
            result[str(steam_id)] = round(entry[1]["minutes"] / 60)
    return result


def start_game_hours_fill(steam_ids):
    """Подгружает недостающие часы в фоне, по одной задаче за раз: запрос идёт на каждого игрока."""
    global _hours_fill_task
    if not config.STEAM_API_KEY or not config.STEAM_APP_ID:
        return
    if _hours_fill_task is not None and not _hours_fill_task.done():
        return
    now = time.monotonic()
    missing = [
        str(steam_id) for steam_id in steam_ids
        if not (_hours_cache.get(str(steam_id)) and _hours_cache[str(steam_id)][0] > now)
    ][:100]
    if missing:
        _hours_fill_task = asyncio.create_task(_through_cache(_hours_cache, missing, _fetch_game_minutes))
        _hours_fill_task.add_done_callback(lambda task: task.cancelled() or task.exception())


async def fetch_player_bans(steam_ids):
    """Сводка банов Steam с кэшем: {steam_id: bans}."""
    if not config.STEAM_API_KEY or not steam_ids:
        return {}
    return await _through_cache(_bans_cache, steam_ids, _fetch_player_bans)

OPENID_NS = "http://specs.openid.net/auth/2.0"
OPENID_SERVER = "https://steamcommunity.com/openid/login"
ID_SELECT = "http://specs.openid.net/auth/2.0/identifier_select"
CLAIMED_ID_RE = re.compile(r"https?://steamcommunity\.com/openid/id/(\d{17})")
REQUIRED_SIGNED_FIELDS = {"claimed_id", "identity", "return_to", "op_endpoint", "response_nonce"}


def realm():
    parts = urlsplit(config.PUBLIC_URL)
    return f"{parts.scheme}://{parts.netloc}"


def steam_login_url(state=""):
    return_to = f"{config.PUBLIC_URL}/api/auth/steam/callback"
    if state:
        return_to = f"{return_to}?{urlencode({'state': state})}"
    params = {
        "openid.ns": OPENID_NS,
        "openid.mode": "checkid_setup",
        "openid.return_to": return_to,
        "openid.realm": realm(),
        "openid.identity": ID_SELECT,
        "openid.claimed_id": ID_SELECT,
    }
    return f"{OPENID_SERVER}?{urlencode(params)}"


async def validate_callback(params, expected_state=""):
    """Проверяет ответ Steam и возвращает steamId64 (или None)."""
    claimed = params.get("openid.claimed_id")
    if params.get("openid.mode") != "id_res" or not claimed:
        log.warning("steam verify: нет id_res/claimed_id (mode=%r)", params.get("openid.mode"))
        return None
    claimed_match = CLAIMED_ID_RE.fullmatch(claimed)
    if not claimed_match or params.get("openid.identity") != claimed:
        log.warning("steam verify: неожиданный claimed_id %r", claimed)
        return None
    # check_authentication подтверждает только перечисленные в openid.signed поля.
    signed_fields = set((params.get("openid.signed") or "").split(","))
    if not REQUIRED_SIGNED_FIELDS <= signed_fields:
        log.warning("steam verify: не подписаны обязательные поля: %r", params.get("openid.signed"))
        return None
    if params.get("openid.op_endpoint") != OPENID_SERVER:
        log.warning("steam verify: неожиданный op_endpoint %r", params.get("openid.op_endpoint"))
        return None
    return_to = params.get("openid.return_to") or ""
    return_parts = urlsplit(return_to)
    public_parts = urlsplit(config.PUBLIC_URL)
    if (
        return_parts.scheme != public_parts.scheme
        or return_parts.netloc.lower() != public_parts.netloc.lower()
        or return_parts.path != "/api/auth/steam/callback"
    ):
        log.warning(
            "steam verify: return_to не совпал: %r != %r",
            return_to,
            config.PUBLIC_URL,
        )
        return None
    returned_state = (parse_qs(return_parts.query).get("state") or [""])[0]
    if not expected_state or returned_state != expected_state:
        log.warning("steam verify: state не совпал (returned=%r expected=%r)", returned_state, expected_state)
        return None
    verify = dict(params)
    verify["openid.mode"] = "check_authentication"
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(OPENID_SERVER, data=verify, allow_redirects=True) as resp:
                body = await resp.text()
    except (aiohttp.ClientError, OSError, TimeoutError) as e:
        log.warning("steam verify: сбой запроса к steamcommunity: %r", e)
        return None
    if not any(line.strip() == "is_valid:true" for line in body.splitlines()):
        log.warning("steam verify: rejected: status=%s body=%r", resp.status, body[:300])
        return None
    return claimed_match.group(1)


async def _fetch_profiles(steam_ids):
    """Публичные профили Steam по steamId64. API возвращает не более 100 за раз."""
    if not config.STEAM_API_KEY or not steam_ids:
        return {}
    out = {}
    timeout = aiohttp.ClientTimeout(total=STEAM_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for i in range(0, len(steam_ids), 100):
                chunk = steam_ids[i : i + 100]
                async with session.get(
                    "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/",
                    params={"key": config.STEAM_API_KEY, "steamids": ",".join(chunk)},
                ) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                for player in (data.get("response") or {}).get("players") or []:
                    steam_id = str(player.get("steamid") or "")
                    if steam_id:
                        out[steam_id] = {
                            "name": str(player.get("personaname") or "")[:120],
                            "avatar_url": str(player.get("avatarfull") or player.get("avatarmedium") or "")[:500],
                            "profile_url": str(player.get("profileurl") or "")[:500],
                            "visibility": int(player.get("communityvisibilitystate") or 0),
                            "time_created_utc": int(player.get("timecreated") or 0) or None,
                        }
    except (aiohttp.ClientError, OSError):
        pass
    return out


async def fetch_personas(steam_ids):
    """Совместимая обёртка для мест, которым нужны только ники."""
    profiles = await fetch_profiles(steam_ids)
    return {steam_id: profile["name"] for steam_id, profile in profiles.items()}


async def _fetch_player_bans(steam_ids):
    """Сводка VAC/game-ban из Steam Web API, без сохранения чувствительных данных."""
    if not config.STEAM_API_KEY or not steam_ids:
        return {}
    ids = [str(steam_id) for steam_id in steam_ids if str(steam_id).isdigit()]
    if not ids:
        return {}
    timeout = aiohttp.ClientTimeout(total=STEAM_TIMEOUT_SECONDS)
    result = {}
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for index in range(0, len(ids), 100):
                chunk = ids[index:index + 100]
                async with session.get(
                    "https://api.steampowered.com/ISteamUser/GetPlayerBans/v1/",
                    params={"key": config.STEAM_API_KEY, "steamids": ",".join(chunk)},
                ) as response:
                    if response.status != 200:
                        continue
                    payload = await response.json()
                for player in (payload.get("players") or []):
                    steam_id = str(player.get("SteamId") or "")
                    if steam_id:
                        result[steam_id] = {
                            "vac_banned": bool(player.get("VACBanned")),
                            "vac_bans": int(player.get("NumberOfVACBans") or 0),
                            "game_bans": int(player.get("NumberOfGameBans") or 0),
                            "days_since_last_ban": int(player.get("DaysSinceLastBan") or 0),
                            "community_banned": bool(player.get("CommunityBanned")),
                        }
    except (aiohttp.ClientError, OSError, TimeoutError, ValueError):
        pass
    return result

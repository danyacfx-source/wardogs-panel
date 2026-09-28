"""Сервисная часть Steam OpenID (вход через Steam) и Steam Web API (ник)."""

import re
from urllib.parse import parse_qs, urlencode, urlsplit

import aiohttp

try:
    from . import config
except ImportError:
    import config

OPENID_NS = "http://specs.openid.net/auth/2.0"
OPENID_SERVER = "https://steamcommunity.com/openid/login"
ID_SELECT = "http://specs.openid.net/auth/2.0/identifier_select"


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
        return None
    if not claimed.startswith("http://steamcommunity.com/openid/id/"):
        return None
    if params.get("openid.op_endpoint") != OPENID_SERVER:
        return None
    return_to = params.get("openid.return_to") or ""
    return_parts = urlsplit(return_to)
    public_parts = urlsplit(config.PUBLIC_URL)
    if (
        return_parts.scheme != public_parts.scheme
        or return_parts.netloc.lower() != public_parts.netloc.lower()
        or return_parts.path != "/api/auth/steam/callback"
    ):
        return None
    returned_state = (parse_qs(return_parts.query).get("state") or [""])[0]
    if not expected_state or returned_state != expected_state:
        return None
    verify = dict(params)
    verify["openid.mode"] = "check_authentication"
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(OPENID_SERVER, data=verify, allow_redirects=True) as resp:
                body = await resp.text()
    except (aiohttp.ClientError, OSError, TimeoutError):
        return None
    if not any(line.strip() == "is_valid:true" for line in body.splitlines()):
        return None
    steam_id = claimed.rsplit("/", 1)[-1]
    return steam_id if re.fullmatch(r"\d{17}", steam_id) else None


async def fetch_profiles(steam_ids):
    """Публичные профили Steam по steamId64. API возвращает не более 100 за раз."""
    if not config.STEAM_API_KEY or not steam_ids:
        return {}
    out = {}
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for i in range(0, len(steam_ids), 100):
                chunk = steam_ids[i : i + 100]
                url = (
                    "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/"
                    f"?key={config.STEAM_API_KEY}&steamids={','.join(chunk)}"
                )
                async with session.get(url) as resp:
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


async def fetch_player_bans(steam_ids):
    """Сводка VAC/game-ban из Steam Web API, без сохранения чувствительных данных."""
    if not config.STEAM_API_KEY or not steam_ids:
        return {}
    ids = [str(steam_id) for steam_id in steam_ids if str(steam_id).isdigit()]
    if not ids:
        return {}
    timeout = aiohttp.ClientTimeout(total=15)
    result = {}
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for index in range(0, len(ids), 100):
                chunk = ids[index:index + 100]
                url = (
                    "https://api.steampowered.com/ISteamUser/GetPlayerBans/v1/"
                    f"?key={config.STEAM_API_KEY}&steamids={','.join(chunk)}"
                )
                async with session.get(url) as response:
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

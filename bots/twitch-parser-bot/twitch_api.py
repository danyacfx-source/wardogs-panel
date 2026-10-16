import aiohttp
import config

_oauth_token: str | None = None
_oauth_expires_at: float = 0
_GQL_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"

async def _get_helix_token(session: aiohttp.ClientSession) -> str:
    global _oauth_token, _oauth_expires_at
    import time
    if _oauth_token and time.time() < _oauth_expires_at - 60:
        return _oauth_token
    if not config.TWITCH_CLIENT_ID or not config.TWITCH_CLIENT_SECRET:
        raise RuntimeError("TWITCH_CLIENT_ID/SECRET не заданы")
    url = f"https://id.twitch.tv/oauth2/token?client_id={config.TWITCH_CLIENT_ID}&client_secret={config.TWITCH_CLIENT_SECRET}&grant_type=client_credentials"
    async with session.post(url, timeout=aiohttp.ClientTimeout(total=12)) as r:
        data = await r.json()
        if r.status != 200:
            raise RuntimeError(data.get("message", f"oauth {r.status}"))
        _oauth_token = data["access_token"]
        _oauth_expires_at = __import__("time").time() + int(data.get("expires_in", 3600))
        return _oauth_token

async def _fetch_helix_stream(session: aiohttp.ClientSession, channel: str):
    token = await _get_helix_token(session)
    url = f"https://api.twitch.tv/helix/streams?user_login={channel}"
    headers = {"Client-Id": config.TWITCH_CLIENT_ID, "Authorization": f"Bearer {token}"}
    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=12)) as r:
        if r.status == 401:
            global _oauth_token
            _oauth_token = None
            raise RuntimeError("helix 401")
        if r.status != 200:
            raise RuntimeError(f"helix {r.status}")
        data = await r.json()
        return (data.get("data") or [None])[0]

async def _fetch_helix_user(session: aiohttp.ClientSession, channel: str):
    try:
        token = await _get_helix_token(session)
        url = f"https://api.twitch.tv/helix/users?login={channel}"
        headers = {"Client-Id": config.TWITCH_CLIENT_ID, "Authorization": f"Bearer {token}"}
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=12)) as r:
            if r.status != 200:
                return None
            data = await r.json()
            return (data.get("data") or [None])[0]
    except Exception:
        return None

async def _fetch_gql(session: aiohttp.ClientSession, channel: str):
    url = "https://gql.twitch.tv/gql"
    payload = {"query": f'query{{user(login:"{channel}"){{stream{{id type title}} broadcastSettings{{isLive}} displayName login profileImageURL}}}}'}
    headers = {"Client-Id": _GQL_CLIENT_ID, "Content-Type": "application/json"}
    async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=12)) as r:
        if r.status != 200:
            raise RuntimeError(f"gql {r.status}")
        data = await r.json()
        u = (data.get("data") or {}).get("user")
        if not u:
            return None
        s = u.get("stream")
        if not s:
            return None
        return {
            "id": s.get("id"),
            "type": s.get("type", "live"),
            "title": s.get("title", ""),
            "viewer_count": 0,
            "game_name": "",
            "user_name": u.get("displayName") or channel,
            "user_login": u.get("login") or channel,
            "profile_image_url": u.get("profileImageURL"),
            "thumbnail_url": None,
            "is_gql": True,
        }

async def fetch_status(session: aiohttp.ClientSession, channel: str):
    # Пытаемся Helix (полные данные) -> GQL fallback
    stream = None
    user = None
    try:
        stream = await _fetch_helix_stream(session, channel)
    except Exception:
        stream = None
    if stream:
        user = await _fetch_helix_user(session, channel)
        return {
            "id": stream.get("id"),
            "user_id": stream.get("user_id"),
            "user_login": stream.get("user_login") or channel,
            "user_name": stream.get("user_name") or channel,
            "game_id": stream.get("game_id"),
            "game_name": stream.get("game_name") or "—",
            "title": stream.get("title") or "",
            "viewer_count": stream.get("viewer_count", 0),
            "thumbnail_url": stream.get("thumbnail_url"),
            "profile_image_url": (user or {}).get("profile_image_url"),
            "type": stream.get("type", "live"),
        }
    # Нет Helix стрима — пробуем GQL (оффлайн vs live без деталей)
    try:
        return await _fetch_gql(session, channel)
    except Exception:
        return None

async def fetch_user_avatar(session: aiohttp.ClientSession, channel: str) -> str | None:
    u = await _fetch_helix_user(session, channel)
    return u.get("profile_image_url") if u else None

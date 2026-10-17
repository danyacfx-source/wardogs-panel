import json
from datetime import datetime, timezone as dt_timezone

import aiohttp

TWITCH_GQL_URL = "https://gql.twitch.tv/gql"
TWITCH_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"

_session: aiohttp.ClientSession | None = None


class TwitchStream:
    def __init__(self, data: dict):
        user = data.get("data", {}).get("user") or {}
        stream = user.get("stream") or {}

        self.login = user.get("login", "")
        self.display_name = user.get("displayName") or self.login
        self.channel_title = (user.get("broadcastSettings") or {}).get("title", "")
        self.avatar = user.get("profileImageURL") or ""

        self.is_live = bool(stream.get("id"))
        self.stream_title = (stream.get("title") or "").strip()
        self.viewers = stream.get("viewersCount") or 0
        self.game = (stream.get("game") or {}).get("displayName") or "Без категории"
        self.thumbnail = stream.get("previewImageURL") or ""
        self.created_at = stream.get("createdAt") or ""

    @property
    def url(self) -> str:
        return f"https://www.twitch.tv/{self.login}"


class TwitchError(Exception):
    pass


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        timeout = aiohttp.ClientTimeout(total=25)
        _session = aiohttp.ClientSession(timeout=timeout)
    return _session


async def close_session():
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


async def _gql(query: str, variables: dict | None = None) -> dict:
    payload = {"query": query, "variables": variables or {}}
    headers = {
        "Content-Type": "application/json",
        "Client-ID": TWITCH_CLIENT_ID,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    }
    session = await _get_session()
    try:
        async with session.post(TWITCH_GQL_URL, json=payload, headers=headers) as resp:
            if resp.status >= 400:
                raise TwitchError(f"HTTP {resp.status}")
            return await resp.json(content_type=None)
    except TwitchError:
        raise
    except Exception as e:
        raise TwitchError(str(e)) from e


_QUERY = (
    "query { user(login: \"__LOGIN__\") { id login displayName profileImageURL "
    "broadcastSettings { title } "
    "stream { id type viewersCount game { displayName } "
    "previewImageURL(width: 640, height: 360) title createdAt } } }"
)


async def fetch_stream(login: str) -> TwitchStream:
    query = _QUERY.replace("__LOGIN__", login)
    data = await _gql(query)
    return TwitchStream(data)


_VIEWERS_QUERY = "query { user(login: \"__LOGIN__\") { stream { viewersCount } } }"
_ID_QUERY = "query { user(login: \"__LOGIN__\") { id } }"
_SCHEDULE_QUERY = (
    "query($id: ID!) { channel(id: $id) { schedule { nextSegment { startAt } } } }"
)


def _parse_iso(value: str):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            dt_timezone.utc
        )
    except Exception:
        return None


async def fetch_viewer_count(login: str) -> int | None:
    data = await _gql(_VIEWERS_QUERY.replace("__LOGIN__", login))
    stream = (data.get("data", {}).get("user") or {}).get("stream")
    if not stream:
        return None
    return stream.get("viewersCount") or 0


async def fetch_next_start(login: str) -> datetime | None:
    data = await _gql(_ID_QUERY.replace("__LOGIN__", login))
    channel_id = (data.get("data", {}).get("user") or {}).get("id")
    if not channel_id:
        return None
    data = await _gql(_SCHEDULE_QUERY, variables={"id": channel_id})
    segment = (
        (data.get("data", {}).get("channel") or {})
        .get("schedule") or {}
    ).get("nextSegment") or {}
    start = segment.get("startAt") or ""
    return _parse_iso(start) if start else None

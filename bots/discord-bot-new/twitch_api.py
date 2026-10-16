import asyncio
import json
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timezone as dt_timezone

TWITCH_GQL_URL = "https://gql.twitch.tv/gql"
TWITCH_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"


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


def _gql(query: str, variables: dict | None = None) -> dict:
    payload = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    req = urllib.request.Request(
        TWITCH_GQL_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Client-ID": TWITCH_CLIENT_ID,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        },
        method="POST",
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=25, context=ctx) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise TwitchError(f"HTTP {e.code}") from e
    except Exception as e:
        raise TwitchError(str(e)) from e


def _request(query: str) -> dict:
    return _gql(query)


_QUERY = (
    "query { user(login: \"__LOGIN__\") { id login displayName profileImageURL "
    "broadcastSettings { title } "
    "stream { id type viewersCount game { displayName } "
    "previewImageURL(width: 640, height: 360) title createdAt } } }"
)


def fetch_stream_sync(login: str) -> TwitchStream:
    query = _QUERY.replace("__LOGIN__", login)
    data = _request(query)
    return TwitchStream(data)


async def fetch_stream(login: str) -> TwitchStream:
    return await asyncio.to_thread(fetch_stream_sync, login)


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


def fetch_viewer_count_sync(login: str) -> int | None:
    """Вернёт число зрителей, если стрим идёт; None если канал офлайн/не найден."""
    data = _request(_VIEWERS_QUERY.replace("__LOGIN__", login))
    stream = (data.get("data", {}).get("user") or {}).get("stream")
    if not stream:
        return None
    return stream.get("viewersCount") or 0


def fetch_next_start_sync(login: str) -> datetime | None:
    """Ближайший запланированный старт стрима по расписанию (UTC) или None."""
    data = _request(_ID_QUERY.replace("__LOGIN__", login))
    channel_id = (data.get("data", {}).get("user") or {}).get("id")
    if not channel_id:
        return None
    data = _gql(_SCHEDULE_QUERY, variables={"id": channel_id})
    segment = (
        (data.get("data", {}).get("channel") or {})
        .get("schedule") or {}
    ).get("nextSegment") or {}
    start = segment.get("startAt") or ""
    return _parse_iso(start) if start else None


async def fetch_viewer_count(login: str) -> int | None:
    return await asyncio.to_thread(fetch_viewer_count_sync, login)


async def fetch_next_start(login: str) -> datetime | None:
    return await asyncio.to_thread(fetch_next_start_sync, login)
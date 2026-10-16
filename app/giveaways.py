"""Публичный фид розыгрышов: парсинг канала Discord и JSON для сайта.

Замена PHP-синка (bots/giveaways-bot). Панель сама читает канал розыгрышов
через бот-токен, парсит сообщения бота WARDOGS RU и кладёт готовый фид
в public_registry (ключ "giveaways"). Страница /giveaways/ сайта читает
GET /api/public/giveaways.
"""

import json
import logging
import re
import time
from datetime import datetime, timezone

import aiohttp

try:
    from . import config, db
except ImportError:
    import config
    import db

log = logging.getLogger("giveaways")

API = "https://discord.com/api/v10"
REGISTRY_KEY = "giveaways"
MAX_ITEMS = 20

KEYWORD_RE = re.compile(r"(?:розыгрыш|участв|победител|копи[яий])", re.I)
SNOWFLAKE_RE = re.compile(r"^\d{17,20}$")
MENTION_RE = re.compile(r"<@!?(\d{17,20})>")
EMOJI_RE = re.compile(r"<a?:[A-Za-z0-9_]+:\d{17,20}>")
TIMESTAMP_TAG_RE = re.compile(r"<t:\d{9,12}(?::[A-Za-z])?>")
TIMESTAMP_VALUE_RE = re.compile(r"<t:(\d{9,12})(?::[A-Za-z])?>")
GIVEAWAY_ID_RE = re.compile(r"(?:ID\s*розыгрыша|розыгрыш\s*№?)\s*[:#№]?\s*(\d{1,12})", re.I)
WINNERS_RE = re.compile(r"(?:кол-во\s+победителей|победител(?:ей|я))\D{0,20}(\d{1,6})", re.I)
PARTICIPANTS_RE = re.compile(r"участников\D{0,12}(\d{1,8})", re.I)
HOST_RE = re.compile(r"(?:устроил|организатор)\s*:\s*([^\r\n]+)", re.I)
WS_RE = re.compile(r"[ \t]+")
CONTROL_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")

MAX_TITLE = 180
MAX_DESCRIPTION = 600
MIN_SNOWFLAKE_TS = 946684800
MAX_SNOWFLAKE_TS = 4102444800


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _clean(value, mentions, limit):
    if not isinstance(value, str):
        return ""
    value = MENTION_RE.sub(lambda m: mentions.get(m.group(1), "участник Discord"), value)
    value = EMOJI_RE.sub("", value)
    value = TIMESTAMP_TAG_RE.sub("", value)
    for junk in ("**", "__", "~~", "`"):
        value = value.replace(junk, "")
    value = CONTROL_RE.sub("", value)
    value = WS_RE.sub(" ", value).strip()
    return value[:limit]


def _description(value, mentions):
    if not isinstance(value, str):
        return ""
    kept = []
    for line in value.splitlines():
        line = line.strip()
        if not line or re.match(r"^(?:⏰|📅|🏆|👑|участников|ID\s*розыгрыша)", line, re.I):
            continue
        kept.append(line)
        if len(kept) == 3:
            break
    return _clean(" ".join(kept), mentions, MAX_DESCRIPTION)


def parse_message(message, guild_id, channel_id, now):
    if not isinstance(message, dict):
        return None
    message_id = str(message.get("id") or "")
    if not SNOWFLAKE_RE.fullmatch(message_id):
        return None
    embeds = message.get("embeds")
    embed = embeds[0] if isinstance(embeds, list) and embeds and isinstance(embeds[0], dict) else {}
    parts = [message.get("content"), embed.get("title"), embed.get("description")]
    for field in embed.get("fields") or []:
        if isinstance(field, dict):
            parts.append(field.get("name"))
            parts.append(field.get("value"))
    footer = embed.get("footer")
    if isinstance(footer, dict):
        parts.append(footer.get("text"))
    text = "\n".join(str(p) for p in parts if isinstance(p, str) and p)
    if not text or not KEYWORD_RE.search(text):
        return None
    mentions = {}
    for mention in message.get("mentions") or []:
        if not isinstance(mention, dict):
            continue
        mid = str(mention.get("id") or "")
        if SNOWFLAKE_RE.fullmatch(mid):
            name = str(mention.get("global_name") or mention.get("display_name") or mention.get("username") or "")
            if name:
                mentions[mid] = name
    title = _clean(embed.get("title"), mentions, MAX_TITLE) or "Розыгрыш WARDOGS RU"
    description = _description(embed.get("description") or message.get("content"), mentions) or (
        "Откройте сообщение в Discord, чтобы узнать условия и принять участие."
    )
    ends_at = None
    for match in TIMESTAMP_VALUE_RE.findall(text):
        ts = int(match)
        if MIN_SNOWFLAKE_TS <= ts <= MAX_SNOWFLAKE_TS:
            ends_at = datetime.fromtimestamp(ts, tz=timezone.utc)
            break
    has_button = False
    button_open = False
    button_label = "Участвовать"
    for row in message.get("components") or []:
        if not isinstance(row, dict):
            continue
        for component in row.get("components") or []:
            if not isinstance(component, dict) or component.get("type") != 2:
                continue
            has_button = True
            if component.get("disabled") is not True:
                button_open = True
            label = _clean(component.get("label"), mentions, 40)
            if label:
                button_label = label
    ended = (has_button and not button_open) or (ends_at is not None and ends_at <= now)
    status = "ended" if ended else "active"
    host_match = HOST_RE.search(text)
    host = _clean(host_match.group(1), mentions, 100) if host_match else None
    giveaway_id_match = GIVEAWAY_ID_RE.search(text)
    winners_match = WINNERS_RE.search(text)
    participants_match = PARTICIPANTS_RE.search(text)
    updated_raw = message.get("edited_timestamp") or message.get("timestamp")
    try:
        updated = datetime.fromisoformat(updated_raw.replace("Z", "+00:00")) if isinstance(updated_raw, str) else now
    except ValueError:
        updated = now
    return {
        "id": message_id,
        "giveaway_id": giveaway_match_str(giveaway_id_match),
        "title": title,
        "description": description,
        "ends_at": _iso(ends_at) if ends_at else None,
        "winners_count": int(winners_match.group(1)) if winners_match else None,
        "participants_count": int(participants_match.group(1)) if participants_match else None,
        "host": host or None,
        "status": status,
        "message_url": f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}",
        "button_label": button_label,
        "updated_at": _iso(updated),
    }


def giveaway_match_str(match):
    return match.group(1) if match else None


async def fetch_messages(token, channel_id, session=None):
    url = f"{API}/channels/{channel_id}/messages?limit=100"
    headers = {
        "Authorization": f"Bot {token}",
        "Accept": "application/json",
        "User-Agent": "WARDOGS-Panel-Giveaway-Sync/1.0",
    }
    own = session is None
    if own:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25))
    try:
        async with session.get(url, headers=headers, allow_redirects=False) as response:
            if response.status != 200:
                log.warning("giveaways: Discord HTTP %s", response.status)
                return None
            payload = await response.json()
            return payload if isinstance(payload, list) else None
    finally:
        if own:
            await session.close()


async def sync_giveaways(session=None):
    if not (config.DISCORD_BOT_TOKEN and config.GIVEAWAY_CHANNEL_ID and config.GIVEAWAY_AUTHOR_ID):
        return False
    messages = await fetch_messages(config.DISCORD_BOT_TOKEN, config.GIVEAWAY_CHANNEL_ID, session=session)
    if messages is None:
        return False
    now = datetime.now(timezone.utc)
    giveaways = []
    author_visible = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        author = message.get("author") if isinstance(message.get("author"), dict) else {}
        if author.get("bot") is not True or str(author.get("id") or "") != config.GIVEAWAY_AUTHOR_ID:
            continue
        author_visible += 1
        parsed = parse_message(message, config.GUILD_ID, config.GIVEAWAY_CHANNEL_ID, now)
        if parsed:
            giveaways.append(parsed)
            if len(giveaways) >= MAX_ITEMS:
                break
    if author_visible == 0:
        log.warning("giveaways: нет сообщений автора %s в канале %s", config.GIVEAWAY_AUTHOR_ID, config.GIVEAWAY_CHANNEL_ID)
        return False
    giveaways.sort(key=lambda item: item.get("ends_at") or "", reverse=True)
    giveaways.sort(key=lambda item: item["status"] != "active")
    feed = {
        "version": 1,
        "generated_at": _iso(now),
        "source": {"guild_id": config.GUILD_ID, "channel_id": config.GIVEAWAY_CHANNEL_ID},
        "giveaways": giveaways,
    }
    await db.save_public_registry(REGISTRY_KEY, json.dumps(feed, ensure_ascii=False))
    log.info("giveaways: синхронизировано %d", len(giveaways))
    return True

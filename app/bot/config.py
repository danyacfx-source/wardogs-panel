"""Конфигурация Discord-бота (встроенного в панель).

Читает тот же .env, что и панель. Имена переменных — как у старого
discord-bot-new/config.py, чтобы не менять .env на Bothost.
"""

import json
import logging
import os

try:
    from .. import config as _panel  # package mode: app.bot.config
except ImportError:
    import importlib
    _panel = importlib.import_module("app.config")

log = logging.getLogger("bot.config")

# Панель уже загрузила .env — просто берём оттуда.
TOKEN = _panel.DISCORD_BOT_TOKEN
GUILD_ID = int(_panel.GUILD_ID) if _panel.GUILD_ID.isdigit() else 0
BACKUP_CHANNEL_ID = int(_panel.BACKUP_CHANNEL_ID) if _panel.BACKUP_CHANNEL_ID else 0


def _int(name: str, default: int = 0) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("Переменная %s=%r не является числом, использую %d", name, raw, default)
        return default


def _ints(name: str) -> list[int]:
    return [int(x) for x in os.getenv(name, "").replace(" ", "").split(",") if x.strip().isdigit()]


def _json_list(name: str) -> list:
    try:
        val = json.loads(os.getenv(name) or "[]")
    except json.JSONDecodeError:
        val = []
    return val if isinstance(val, list) else []


# Тикеты
TICKET_GUILD_ID = _int("TICKET_GUILD_ID") or GUILD_ID
TICKET_CATEGORY = _int("TICKET_CATEGORY")
TICKET_STAFF_ROLES = _ints("TICKET_STAFF_ROLES")
TICKET_LOG_CHANNEL = _int("TICKET_LOG_CHANNEL")
TICKET_TRANSCRIPT_CHANNEL = _int("TICKET_TRANSCRIPT_CHANNEL")
ALLOWED_EVERYONE_OPEN = _int("TICKET_ALLOWED_EVERYONE_OPEN", 5)

# Регистрация кланов (отдельные тикеты).
CLAN_TICKET_CATEGORY = _int("CLAN_TICKET_CATEGORY")
CLAN_TICKET_STAFF_ROLES = _ints("CLAN_TICKET_STAFF_ROLES")
CLAN_TICKET_LOG_CHANNEL = _int("CLAN_TICKET_LOG_CHANNEL")
CLAN_TICKET_TRANSCRIPT_CHANNEL = _int("CLAN_TICKET_TRANSCRIPT_CHANNEL")

# Голосовые.
VC_TRIGGER_CHANNEL = _int("VC_TRIGGER_CHANNEL")
VC_CATEGORY = _int("VC_CATEGORY")
VC_CONTROL_CHANNEL = _int("VC_CONTROL_CHANNEL")

LOG_CHANNEL = _int("LOG_CHANNEL")

# ---- Перевод ----
TRANSLATE_CHANNELS = _ints("TRANSLATE_CHANNELS")
TRANSLATE_TARGET = os.getenv("TRANSLATE_TARGET", "ru")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

# Фон rank-карточки
RANK_BACKGROUND = os.getenv("RANK_BACKGROUND", "")

# Стримеры / Twitch.
TWITCH_CHANNELS = [x.strip().lower().lstrip("@") for x in os.getenv("TWITCH_CHANNELS", "").split(",") if x.strip()]
TWITCH_ANNOUNCE_CHANNEL = _int("TWITCH_ANNOUNCE_CHANNEL")
TWITCH_CHECK_INTERVAL = _int("TWITCH_CHECK_INTERVAL", 60)
OWNER_ID = _int("OWNER_ID")

MIRROR_ADD_COMMAND_USERS = _ints("MIRROR_ADMINS")

# Каналы-ловушки
TRAP_CHANNEL_IDS = [int(x) for x in os.getenv("TRAP_CHANNEL_IDS", "").replace(" ", "").split(",") if x.strip().isdigit()]
TRAP_LOG_CHANNEL = _int("TRAP_LOG_CHANNEL")

# Активности (роли за активность).
ACTIVITY_ROLES_CONFIG = _json_list("ACTIVITY_ROLES_CONFIG")

# ---- Донат ----
DONATION_SITE_BASE_URL = os.getenv("SITE_BASE_URL", os.getenv("DONATION_SITE_BASE_URL", ""))
DONATION_CHECKOUT_SECRET = os.getenv("CHECKOUT_SECRET", os.getenv("DONATION_CHECKOUT_SECRET", ""))
DONATION_FEED_TOKEN = os.getenv("FEED_TOKEN", os.getenv("DONATION_FEED_TOKEN", ""))
DONATION_POLL_SECONDS = _int("POLL_SECONDS", 45)

# ---- Клановая система ----
CLAN_SYNC_URL = os.getenv("CLAN_SYNC_URL", "")
CLAN_SYNC_SECRET = os.getenv("CLAN_SYNC_SECRET", os.getenv("SITE_SYNC_SECRET", ""))
CLAN_BACKUP_SECRET = os.getenv("CLAN_BACKUP_SECRET", "")
CLAN_BACKUP_INTERVAL_HOURS = _int("CLAN_BACKUP_INTERVAL_HOURS", 24)
CLAN_BACKUP_RETENTION = _int("CLAN_BACKUP_RETENTION", 30)
CLAN_ADMIN_ROLE_ID = _int("CLAN_ADMIN_ROLE_ID")
CLAN_BACKUP_CHANNEL_ID = _int("CLAN_BACKUP_CHANNEL_ID")

# ---- Seeding ----
SEED_RCON_URL = os.getenv("SEED_RCON_URL", os.getenv("WARDOGS_RCON_URL", ""))
SEED_RCON_PASSWORD = os.getenv("SEED_RCON_PASSWORD", os.getenv("WARDOGS_RCON_PASSWORD", ""))
SEED_RCON_2_URL = os.getenv("SEED_RCON_2_URL", os.getenv("WARDOGS_RCON_2_URL", ""))
SEED_RCON_2_PASSWORD = os.getenv("SEED_RCON_2_PASSWORD", os.getenv("WARDOGS_RCON_2_PASSWORD", ""))
SEED_RCON_TIMEOUT_MS = _int("SEED_RCON_TIMEOUT_MS", _int("RCON_TIMEOUT_MS", 6000))
SEED_RCON_RETRY = _int("SEED_RCON_RETRY", _int("RCON_RETRY_ATTEMPTS", 1))

SEED_START_TIME = os.getenv("SEED_START_TIME", "08:00")
SEED_END_TIME = os.getenv("SEED_END_TIME", "22:00")
SEED_STOP_PLAYER_COUNT = _int("SEED_STOP_PLAYER_COUNT", 20)
SEED_RESUME_BELOW_SECONDS = _int("SEED_RESUME_BELOW_SECONDS", 300)
SEED_TICK_SECONDS = _int("SEED_TICK_SECONDS", 30)
SEED_SESSION_GRACE_SECONDS = _int("SEED_SESSION_GRACE_SECONDS", 180)
LEADERBOARD_REFRESH_SECONDS = _int("LEADERBOARD_REFRESH_SECONDS", 60)
WEEKLY_AWARD_RECONCILE_SECONDS = _int("WEEKLY_AWARD_RECONCILE_SECONDS", 300)
VIP_RECONCILE_SECONDS = _int("VIP_RECONCILE_SECONDS", 120)
DISCORD_ROLE_RECONCILE_SECONDS = _int("DISCORD_ROLE_RECONCILE_SECONDS", 300)
DONATION_POLL_SECONDS_SEED = _int("DONATION_POLL_SECONDS_SEED", 45)

VIP_1_DAY_COST = _int("VIP_1_DAY_COST", 120)
VIP_7_DAY_COST = _int("VIP_7_DAY_COST", 360)
VIP_30_DAY_COST = _int("VIP_30_DAY_COST", 720)

SEEDER_ROLE_ID = _int("SEEDER_ROLE_ID")
SUPER_SEEDER_ROLE_ID = _int("SUPER_SEEDER_ROLE_ID")
VIP_ROLE_ID_SEED = _int("VIP_ROLE_ID_SEED")
SEED_VOICE_CHANNEL_ID = _int("SEED_VOICE_CHANNEL_ID")
SEED_RANK_CHANNEL_ID = _int("SEED_RANK_CHANNEL_ID")
SEED_SHOP_CHANNEL_ID = _int("SEED_SHOP_CHANNEL_ID")
SEED_RULES_CHANNEL_ID = _int("SEED_RULES_CHANNEL_ID")
SEED_AUDIT_CHANNEL_ID = _int("SEED_AUDIT_CHANNEL_ID")
SEED_BACKUP_CHANNEL_ID = _int("SEED_BACKUP_CHANNEL_ID")

LINK_CHALLENGE_MINUTES = _int("LINK_CHALLENGE_MINUTES", 5)
LINK_CHALLENGE_MAX_ATTEMPTS = _int("LINK_CHALLENGE_MAX_ATTEMPTS", 5)
SEED_CHALLENGE_KEY = os.getenv("SEED_CHALLENGE_KEY", "")
SEED_GATHER_ROLE_ID = _int("SEED_GATHER_ROLE_ID")

# ---- Донат: каналы и роли ----
DONATION_VIP_ROLE_ID = _int("DONATION_VIP_ROLE_ID")
DONATION_CHANNEL_SUPPORT = _int("DONATION_CHANNEL_SUPPORT")
DONATION_CHANNEL_VIP = _int("DONATION_CHANNEL_VIP")
DONATION_CHANNEL_THANKS = _int("DONATION_CHANNEL_THANKS")
DONATION_CHANNEL_TOP = _int("DONATION_CHANNEL_TOP")
DONATION_TIERS = _json_list("DONATION_TIERS")

# ---- SW ----
SW_SERVERS = _json_list("SW_SERVERS")
if not SW_SERVERS:
    SW_SERVERS = [{"id": s["id"], "name": s["name"]} for s in _panel.SERVERS]
SERVERS = SW_SERVERS
SW_ADMIN_ROLE_IDS = _ints("SW_ADMIN_ROLE_IDS")

# Intents
import discord  # noqa: E402

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.voice_states = True

EMBED_COLOR = discord.Color(0x5865F2)

import json
import logging
import os
import discord
from dotenv import load_dotenv

os.chdir(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(override=True)

log = logging.getLogger("config")


def _int(name: str, default: int = 0) -> int:
    """Чтение int-переменной из .env без краша на нечисловом значении."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("Переменная %s=%r не является числом, использую %d", name, raw, default)
        return default


def _ints(name: str) -> list[int]:
    """Список int-переменной: нечисловые токены отбрасываются."""
    return [
        int(x)
        for x in os.getenv(name, "").replace(" ", "").split(",")
        if x.strip().isdigit()
    ]


TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID = _int("GUILD_ID") or 403186008579112973

# Бэкап БД: канал, куда при каждом запуске и каждые 6-8 часов отправляется копия wardogs_v2.db.
# Захардкожен, чтобы переживать обновы (кнопка на хостинге сносит .env).
# Можно переопределить через BACKUP_CHANNEL_ID в .env.
BACKUP_CHANNEL_ID = _int("BACKUP_CHANNEL_ID", 1547050639677202433)

# Тикеты
TICKET_GUILD_ID = _int("TICKET_GUILD_ID", GUILD_ID)
TICKET_CATEGORY = _int("TICKET_CATEGORY") or 1543615499924021279
TICKET_STAFF_ROLES = _ints("TICKET_STAFF_ROLES") or [815605974412558376, 805429609457254450]
TICKET_LOG_CHANNEL = _int("TICKET_LOG_CHANNEL", 1545113128625246310)
TICKET_TRANSCRIPT_CHANNEL = _int("TICKET_TRANSCRIPT_CHANNEL") or 1545113241783377950
ALLOWED_EVERYONE_OPEN = _int("TICKET_ALLOWED_EVERYONE_OPEN", 5)

# Регистрация кланов (отдельные тикеты).
# Значения захардкожены, чтобы переживать обновы (кнопка на хостинге сносит .env).
# При желании можно переопределить через .env.
CLAN_TICKET_CATEGORY = _int("CLAN_TICKET_CATEGORY", 1543615499924021279)
CLAN_TICKET_STAFF_ROLES = _ints("CLAN_TICKET_STAFF_ROLES") or [815605974412558376, 805429609457254450]
CLAN_TICKET_LOG_CHANNEL = _int("CLAN_TICKET_LOG_CHANNEL", 1545113128625246310)
CLAN_TICKET_TRANSCRIPT_CHANNEL = _int("CLAN_TICKET_TRANSCRIPT_CHANNEL", 1545113241783377950)

# Голосовые. Захардкожены, чтобы переживать обновы (кнопка на хостинге сносит .env).
VC_TRIGGER_CHANNEL = _int("VC_TRIGGER_CHANNEL") or 1543612443614384208
VC_CATEGORY = _int("VC_CATEGORY") or 1543612442393837719
VC_CONTROL_CHANNEL = _int("VC_CONTROL_CHANNEL") or 1543612538384818217

LOG_CHANNEL = _int("LOG_CHANNEL") or 1543615646447837206

# ---- Перевод ----
# Каналы для автоперевода. Захардкоженные каналы работают всегда (переживут
# обновы и .env), из .env можно добавлять свои — они объединяются.
_default_translate_channels = [1543603551564079204, 1547304972054691900]
TRANSLATE_CHANNELS = list(dict.fromkeys(
    _ints("TRANSLATE_CHANNELS") + _default_translate_channels
)) or list(_default_translate_channels)
TRANSLATE_TARGET = os.getenv("TRANSLATE_TARGET", "ru")

# ИИ-переводчик Gemini (бесплатный API, aistudio.google.com).
# Ключ задаётся через GEMINI_API_KEY в .env.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
# Точный id модели проверять в консоли Google AI Studio.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

# Резервные AI-движки (используются, когда Gemini недоступен).
# Groq: бесплатный API, console.groq.com; не работает из РФ, работает из EU.
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

# Фон rank-карточки (путь к картинке; пусто = градиент)
RANK_BACKGROUND = os.getenv("RANK_BACKGROUND", "")

# Стримеры / Twitch. Каналы захардкожены, чтобы переживать обновы (сносят .env).
_default_twitch_channels = ["f_a_n_e", "tohu_game", "blackraventtv", "kupidon_ttv"]
TWITCH_CHANNELS = [
    x.strip().lower().lstrip("@")
    for x in os.getenv("TWITCH_CHANNELS", "").split(",")
    if x.strip()
] or list(_default_twitch_channels)
TWITCH_ANNOUNCE_CHANNEL = _int("TWITCH_ANNOUNCE_CHANNEL") or 859716166699188244
TWITCH_CHECK_INTERVAL = _int("TWITCH_CHECK_INTERVAL", 60)

# Пункт 6: зеркало чужого канала → наши каналы
# MIRROR_SOURCES = "chan_id@guild_id:dest_id,dest2; ..." но основной способ —
# через БД и слэш-команду, чтобы менять без правки кода.
MIRROR_ADD_COMMAND_USERS = _ints("MIRROR_ADMINS")

# Каналы-ловушки: сообщение в канале = удаление + кик. Основной способ
# назначения — /trap (хранится в БД, переживает обновы). Список ниже —
# жёстко зашитые каналы, переживающие даже потерю БД.
TRAP_CHANNEL_IDS = [
    int(x) for x in os.getenv("TRAP_CHANNEL_IDS", "").replace(" ", "").split(",") if x.strip().isdigit()
] or [1547577542180077699]
# Канал, куда пишется лог срабатывания ловушки (по умолчанию — логи модерации).
TRAP_LOG_CHANNEL = _int("TRAP_LOG_CHANNEL", 1546556555036721252)

# Активности (роли за активность). Клиент захардкожен, чтобы переживать обновы
# (кнопка на хостинге сносит .env). Можно переопределить через .env.
_default_activity_roles = json.dumps([
    {"role_id": 861667759418245170, "type": "voice_hours", "hours": 10},
    {"role_id": 861667759418245170, "type": "messages", "count": 100},
    {"role_id": 1543911320682889286, "type": "voice_hours", "hours": 30, "prev_role_id": 861667759418245170},
    {"role_id": 1543911320682889286, "type": "messages", "count": 1500, "prev_role_id": 861667759418245170},
    {"role_id": 862364195659579422, "type": "voice_hours", "hours": 80, "prev_role_id": 1543911320682889286},
    {"role_id": 862364195659579422, "type": "messages", "count": 5000, "prev_role_id": 1543911320682889286},
])
try:
    ACTIVITY_ROLES_CONFIG = json.loads(os.getenv("ACTIVITY_ROLES_CONFIG") or _default_activity_roles)
except json.JSONDecodeError:
    ACTIVITY_ROLES_CONFIG = json.loads(_default_activity_roles)
if not isinstance(ACTIVITY_ROLES_CONFIG, list):
    ACTIVITY_ROLES_CONFIG = []

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.voice_states = True

EMBED_COLOR = discord.Color(0x5865F2)

# ---- Донат-бот (портируется из donation-bot) ----
DONATION_SITE_BASE_URL = os.getenv("SITE_BASE_URL", os.getenv("DONATION_SITE_BASE_URL", ""))
DONATION_CHECKOUT_SECRET = os.getenv("CHECKOUT_SECRET", os.getenv("DONATION_CHECKOUT_SECRET", ""))
DONATION_FEED_TOKEN = os.getenv("FEED_TOKEN", os.getenv("DONATION_FEED_TOKEN", ""))
DONATION_POLL_SECONDS = _int("POLL_SECONDS", 45)

# ---- Клановая система (порт clans-bot) ----
CLAN_SYNC_URL = os.getenv("CLAN_SYNC_URL", "https://panel.dendich.online/api/public/clan-sync")
CLAN_SYNC_SECRET = os.getenv("CLAN_SYNC_SECRET", os.getenv("SITE_SYNC_SECRET", ""))
CLAN_BACKUP_SECRET = os.getenv("CLAN_BACKUP_SECRET", "")
CLAN_BACKUP_INTERVAL_HOURS = _int("CLAN_BACKUP_INTERVAL_HOURS", 24)
CLAN_BACKUP_RETENTION = _int("CLAN_BACKUP_RETENTION", 30)
CLAN_ADMIN_ROLE_ID = _int("CLAN_ADMIN_ROLE_ID", 0)
CLAN_BACKUP_CHANNEL_ID = _int("CLAN_BACKUP_CHANNEL_ID", 0)

# ---- Seeding (порт seeding-bot) ----
# Старые имена из seeding-bot/.env поддерживаются как fallback.
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
VIP_30_DAY_COST = _int("VIP_30_DAY_COST", 760)

SEEDER_ROLE_ID = _int("SEEDER_ROLE_ID", 1552728825568174090)
SUPER_SEEDER_ROLE_ID = _int("SUPER_SEEDER_ROLE_ID", 1552841261713719440)
VIP_ROLE_ID_SEED = _int("VIP_ROLE_ID_SEED", 1552725871830503484)
SEED_VOICE_CHANNEL_ID = _int("SEED_VOICE_CHANNEL_ID", 1552253117586739210)
SEED_RANK_CHANNEL_ID = _int("SEED_RANK_CHANNEL_ID", 1552254053109727262)
SEED_SHOP_CHANNEL_ID = _int("SEED_SHOP_CHANNEL_ID", 1552255043757936721)
SEED_RULES_CHANNEL_ID = _int("SEED_RULES_CHANNEL_ID", 1552254112060411944)
SEED_AUDIT_CHANNEL_ID = _int("SEED_AUDIT_CHANNEL_ID", 1552402077068820611)
SEED_BACKUP_CHANNEL_ID = _int("SEED_BACKUP_CHANNEL_ID", 1552401803184701502)

LINK_CHALLENGE_MINUTES = _int("LINK_CHALLENGE_MINUTES", 5)
LINK_CHALLENGE_MAX_ATTEMPTS = _int("LINK_CHALLENGE_MAX_ATTEMPTS", 5)
SEED_CHALLENGE_KEY = os.getenv("SEED_CHALLENGE_KEY", "wardogs-seeding-v1")

SEED_GATHER_ROLE_ID = _int("SEED_GATHER_ROLE_ID", 859738052666327040)
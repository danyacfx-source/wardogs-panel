import os
from dotenv import load_dotenv

load_dotenv(override=True)

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = int(os.getenv("GUILD_ID", "0") or 0)
ANNOUNCE_CHANNEL_ID = int(os.getenv("ANNOUNCE_CHANNEL_ID", "0") or 0)
PING_ROLE_ID = int(os.getenv("PING_ROLE_ID", "0") or 0)

TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "").strip()
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET", "").strip()
TWITCH_CHANNELS = [s.strip().lower().lstrip("@") for s in os.getenv("TWITCH_CHANNELS", "").split(",") if s.strip()]
TWITCH_POLL_SECONDS = max(30, int(os.getenv("TWITCH_POLL_SECONDS", "60") or 60))

# Ручной + автоматический перевод (как в discord-bot-new) — через Gemini
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash").strip()
TRANSLATE_TARGET = os.getenv("TRANSLATE_TARGET", "ru").strip() or "ru"
TRANSLATE_CHANNELS = [int(x.strip()) for x in os.getenv("TRANSLATE_CHANNELS", "").split(",") if x.strip()]

if not TOKEN:
    print("[config] DISCORD_TOKEN пуст")
if not TWITCH_CHANNELS:
    print("[config] TWITCH_CHANNELS пуст")
if not ANNOUNCE_CHANNEL_ID:
    print("[config] ANNOUNCE_CHANNEL_ID пуст")

# twitch-parser-bot (Python) — только анонсы

Минимальный бот: чекает `TWITCH_CHANNELS` каждые `TWITCH_POLL_SECONDS` и пишет в `ANNOUNCE_CHANNEL_ID` когда стрим переходит offline→live.

## Запуск
1. `cp .env.example .env` и заполни `DISCORD_TOKEN`, `TWITCH_CLIENT_ID/SECRET`, `TWITCH_CHANNELS`, `ANNOUNCE_CHANNEL_ID`
2. `pip install -r requirements.txt`
3. `python bot.py`

## Twitch API
https://dev.twitch.tv/console/apps → Create app → Client ID + Client Secret. Без них работает GQL-фолбэк.

## Docker
```bash
docker build -t twitch-parser-bot .
docker run -d --env-file .env --name twitch-parser-bot twitch-parser-bot
```

# Подключение ботов к WARDOGS Control Center

Панель является единственным владельцем общих данных. Боты не подключаются к базе
напрямую: каждый использует индивидуальный ключ и версионированный HTTP API.

## Регистрация

1. Откройте `Панель → Discord → Реестр ботов`.
2. Укажите название, стабильный системный ID и Discord Guild ID.
3. Сохраните показанный токен — позднее панель покажет только новый токен после ротации.
4. Передавайте в каждом запросе:

```http
X-Bot-ID: moderation-bot
Authorization: Bearer wdb_moderation-bot_...
Content-Type: application/json
```

Ключи в базе хранятся только как SHA-256-хеши. Отключение интеграции не удаляет её
события. Ротация ключа сразу отзывает предыдущий ключ.

## Heartbeat

`POST /api/bot/v1/heartbeat`, рекомендуемый интервал — 60 секунд:

```json
{
  "version": "2.4.0",
  "bot_user_id": "123456789012345678",
  "detail": "gateway ready",
  "metadata": {"guilds": 1, "shards": 1, "latency_ms": 48}
}
```

Бот считается offline, если heartbeat не поступал 180 секунд.

## События

`POST /api/bot/v1/events`:

```json
{
  "event_id": "01J8Y7M7Q0S2FJ4P6A6T4D2K9B",
  "event_type": "moderation.case.created",
  "payload": {"discord_id": "123456789012345678", "case_id": "42"}
}
```

`event_id` должен быть уникальным внутри одного бота. Повторная доставка безопасна:
панель вернёт `duplicate: true` и не создаст вторую запись. Размер payload ограничен
32 КБ, метаданных heartbeat — 8 КБ.

## Права интеграции

- `heartbeat` — состояние подключения;
- `events.write` — отправка событий;
- `stats.write` — статистика;
- `bindings.read`, `bindings.write` — привязки Steam ↔ Discord;
- `tickets.read`, `tickets.write` — обращения;
- `moderation.write` — наказания;
- `voice.manage` — временные голосовые комнаты;
- `commands.read` — получение заданий панели.

Новые API-операции должны проверять соответствующее право через реестр. Лимит текущей
схемы — 64 интеграции; таблицы и API не зависят от фиксированного числа Discord-ботов.

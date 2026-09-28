# Импорт функций ZAVOD Admin

## Что найдено в `zavod_dump.zip`

Архив содержит авторизованный UI-дамп, скриншоты, текстовые слепки страниц и карту сетевых запросов. Исходного Next.js/React-кода, backend-кода и тел запросов в архиве нет.

Найденные разделы:

- `/admin/servers` — мониторинг серверов;
- `/admin/players` — общий реестр игроков, поиск, K/D, время на серверах, наблюдение;
- `/admin/servers/bans` — реестр банов, синхронизация с серверами, снятие банов, ник-баны;
- `/admin/servers/vip` — VIP/reserved slots, роль-связанные слоты, срок действия;
- `/admin/tickets` — тикеты, фильтры статуса, поиск, назначение и закрытие;
- `/admin/discord-logs` — фильтры Discord-событий и пагинация;
- `/admin/temp-voice` — личные voice-комнаты и удаление комнат;
- `/admin/clans` — заявки и состояния кланов;
- `/admin/staff` — персонал, Steam/Discord ID и роли.

Зафиксированные API-контракты дампа:

- `GET /api/game-players?q=&page=1&pageSize=50`
- `GET /api/game-moderation/vip`
- `GET /api/game-moderation/nickname-bans`
- `GET /api/game-moderation/bans`
- `GET /api/tickets?page=1&pageSize=30`
- `GET /api/discord-logs?page=1&pageSize=30`
- `GET /api/temp-voice`
- `GET /api/admin/clans`
- `GET /api/staff`
- realtime negotiation: `POST /api/hubs/live/negotiate?negotiateVersion=1`

## Уже есть в RUBEZH Panel

Текущая панель уже покрывает основной RCON-контур:

- мониторинг и health-check;
- игроки на сервере;
- kick/kill/message/move faction;
- bans;
- reserved slots;
- карта, освещение и управление матчем;
- broadcast;
- конфигурация серверов;
- server audit и site audit;
- управление разрешениями Discord-ролей.

## Что нужно добавить нативно

Пять модулей отсутствуют в текущем backend-контуре: тикеты, Discord-логи, личные voice-комнаты, кланы и расширенный staff-раздел. Их нельзя корректно «скопировать» только по скриншотам: нужны собственные источники данных и mutation-контракты.

План реализации:

1. Добавить отдельные PostgreSQL-таблицы и миграции для tickets, clans и staff snapshots.
2. Добавить read-only API с пагинацией, фильтрами и лимитами.
3. Добавить явные permission scopes для каждого модуля.
4. Добавить Discord adapter с минимальными разрешениями и fail-closed поведением.
5. Добавить Vue-разделы в панели без импорта персональных данных из дампа.
6. Для операций изменения добавить CSRF, idempotency и site-audit до включения кнопок.

Дамп не содержит секретов, cookies или Authorization-заголовков. Снимки игроков и Discord-событий используются только как UI-референс и не импортируются в базу проекта.

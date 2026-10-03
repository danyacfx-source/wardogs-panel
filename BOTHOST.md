# Развёртывание панели на Bothost

Bothost запускает приложение в одном Docker-контейнере. Этот проект уже
подготовлен: `Dockerfile` слушает `0.0.0.0` и автоматически использует порт,
который Bothost передаёт в переменной `PORT`.

## Что создать в Bothost

1. Создайте Python-проект из Git-репозитория с этой папкой в корне.
2. Включите «Использовать свой Dockerfile».
3. В настройке домена укажите `panel.dendich.online` и внутренний порт `8000`.
   Если Bothost подставит другое значение, приложение получит его через `PORT`.
4. В DNS домена создайте запись по подсказке Bothost и дождитесь выпуска HTTPS.

## Переменные окружения

Добавьте в интерфейсе Bothost, не в репозитории:

```dotenv
WARDOGS_ENV=production
WARDOGS_PUBLIC_URL=https://panel.dendich.online
WARDOGS_ALLOWED_HOSTS=panel.dendich.online
WARDOGS_DB=/app/data/site.db
WARDOGS_ALLOW_SQLITE_PRODUCTION=1
WARDOGS_ALLOW_INSECURE_RCON=1

STEAM_API_KEY=
DISCORD_CLIENT_ID=
DISCORD_CLIENT_SECRET=
DISCORD_BOT_TOKEN=
WARDOGS_GUILD_ID=
WARDOGS_ADMIN_ROLES=
RCON_TOKEN_RU1=
```

`WARDOGS_ALLOW_INSECURE_RCON=1` нужен только пока игровой RCON доступен по
обычному HTTP. Когда у RCON появится HTTPS, поставьте `0`.

Для конфигурации серверов есть два варианта:

* загрузить закрытый `servers.json` в корень проекта;
* безопаснее — создать секрет `WARDOGS_SERVERS_JSON` с содержимым `servers.json`.

Токен в JSON не нужен: для сервера `ru1` он берётся из `RCON_TOKEN_RU1`.

## После первого запуска

Проверьте адрес `https://panel.dendich.online/api/ready`. В Discord Developer
Portal добавьте redirect URI:

`https://panel.dendich.online/api/auth/discord/callback`

SQLite лежит в `/app/data/site.db`, поэтому сохраняется между обновлениями
проекта. Не запускайте вторую копию панели с той же SQLite-базой.

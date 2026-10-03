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
# Allowlist действий админки: IP/CIDR через запятую. Пусто = без ограничений.
WARDOGS_ADMIN_IPS=
RCON_TOKEN_RU1=

# Донаты (сайт-часть донатилки)
DONATE_CHECKOUT_SECRET=
DONATE_FEED_TOKEN=
YOOMONEY_WALLET=
YOOMONEY_NOTIFICATION_SECRET=
```

`WARDOGS_ALLOW_INSECURE_RCON=1` нужен только пока игровой RCON доступен по
обычному HTTP. Когда у RCON появится HTTPS, поставьте `0`.

Для конфигурации серверов используйте `servers.json` из корня репозитория.
Секрет `WARDOGS_SERVERS_JSON` не применяйте: Bothost обрезает длинные значения
переменных окружения и JSON ломается.

Токен в JSON не нужен: для сервера `ru1` он берётся из `RCON_TOKEN_RU1`.

## После первого запуска

Проверьте адрес `https://panel.dendich.online/api/ready`. В Discord Developer
Portal добавьте redirect URI:

`https://panel.dendich.online/api/auth/discord/callback`

SQLite лежит в `/app/data/site.db`, поэтому сохраняется между обновлениями
проекта. Не запускайте вторую копию панели с той же SQLite-базой.

## Донаты (ЮMoney)

Панель хранит сайт-часть донатилки:

- `GET /donate/index.php?o=<подпись>` — страница перевода (ссылки формирует
  донатный бот, секрет CHECKOUT_SECRET бота = `DONATE_CHECKOUT_SECRET` панели);
- `POST /donate/webhook.php` — уведомления ЮMoney, проверка подписи `sign`;
- `GET /donate/feed.php?offset=N` c заголовком `X-WD-Feed-Token` — закрытый
  журнал операций для донатного бота и бота сидеров
  (токен = `DONATE_FEED_TOKEN` панели = `FEED_TOKEN` бота).

Данные (`orders/` и `events.jsonl`) лежат рядом с базой: `/app/data/donate/`.

Порядок включения:

1. Сгенерируйте две случайные строки от 32 символов — они станут
   `DONATE_CHECKOUT_SECRET` и `DONATE_FEED_TOKEN` панели и одновременно
   `CHECKOUT_SECRET`/`FEED_TOKEN` донатного бота (и `DONATION_CHECKOUT_SECRET`
   / `DONATION_FEED_TOKEN` бота сидеров).
2. Пропишите все четыре переменные в настройках Bothost и у ботов.
3. В настройках ЮMoney укажите адрес HTTP-уведомлений
   `https://panel.dendich.online/donate/webhook.php` и перенесите
   полученный секретный ключ в `YOOMONEY_NOTIFICATION_SECRET`.
4. `YOOMONEY_WALLET` — номер кошелька, на который идут переводы.
5. В настройках донатного бота: `SITE_BASE_URL=https://panel.dendich.online`.

Форма оплаты — classic quickpay ЮMoney с меткой `WD2-<id заказа>`; метка
попадает в уведомление и связывает платёж с сохранённым заказом. Тестовые
уведомления (`test_notification=true`) принимаются и не записываются в журнал.

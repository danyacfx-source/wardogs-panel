# WARDOGS bots

Шесть ботов WARDOGS в одном репозитории панели. Каждый живёт в своём каталоге со своим окружением — рантаймы, зависимости, токены и данные полностью изолированы, запускаются отдельными процессами и не мешают друг другу.

| Каталог | Рантайм | Вход | Хранилище | Назначение |
|---|---|---|---|---|
| `discord-bot-new/` | Python 3.12 | `bot.py` | SQLite `wardogs_v2.db` в своей папке | Основной Discord-бот: тикеты, розыгрыши, уровни/XP, автоперевод, Twitch, зеркала, автомод, войсы, логи, бэкапы |
| `twitch-parser-bot/` | Python 3.12 | `bot.py` | нет (в памяти) | Парсер Twitch: анонс перехода offline→live + перевод |
| `clans-bot/` | Node ≥20.11 | `src/index.js` | JSON `state.json` в `DATA_DIR` | Клановая система: заявки, 5 клановых ролей с иконками, штабы, реестр на сайте, шифрованные бэкапы |
| `giveaways-bot/` | PHP 8.1+ CLI/cron | `sync.php` | JSON `giveaways.json` | Read-only синк розыгрышей из Discord-канала для страницы сайта |
| `donation-bot/` | Node ≥22.12 | `src/index.js` | JSON `donations.json` в `DATA_DIR` | Донаты и оплаченный VIP: карточки, благодарности, топ-10, импорт платежей с сайта |
| `seeding-bot/` | Node 20.18–24 | `index.js` | шифрованная SQLite `seeding.sqlite` в `DATA_DIR` | Подтверждённый сидинг: баллы за голос/присутствие, рейтинг, магазин VIP, привязка Discord↔Steam, RCON |

## Правила изоляции

- Каждый бот запускается **отдельным процессом** со своим `.env` в своей папке — токены и ID не пересекаются.
- Данные каждого бота живут в `bots/<имя>/data/` (в git не попадают).
- Python-боты получают **свой venv** в своей папке, Node-боты — свой `node_modules`, PHP-бот — свой `config.php`.
- `*.env`, `*.db`, `node_modules/`, `data/`, `__pycache__/` исключены из git — секреты в репозиторий не попадают.
- Все боты работают только на **исходящих** соединениях (Discord Gateway, API, RCON) — входящие порты не нужны, конфликтов портов нет.

## Запуск

Общая схема: скопировать пример окружения в `.env`, заполнить, поставить зависимости рантайма, запустить точку входа из папки бота.

```powershell
# Python-боты (свой venv на каждый)
cd bots\discord-bot-new
py -3.12 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
# заполнить .env (уже скопирован из исходников)
.\.venv\Scripts\python.exe bot.py

# Node-боты (свой node_modules на каждый)
cd bots\clans-bot        # или donation-bot, seeding-bot
npm install
npm start                # seeding-bot: npm start / node index.js

# PHP-бот (CLI/cron, веб-сервер не нужен)
cd bots\giveaways-bot
copy config.example.php config.php   # заполнить
php sync.php --check
php sync.php
```

Подробности каждого бота — в его `README.md` / `INSTALL-RU.md` внутри каталога.

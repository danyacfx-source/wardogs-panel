# Запуск на виртуальном хостинге REG.RU через Passenger

Этот вариант предназначен для тестового или малонагруженного запуска. Для
постоянного мониторинга RCON и ботов предпочтителен Bothost или VPS.

## Установка зависимостей

В Shell-клиенте из папки проекта:

```sh
cd ~/panel
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## Настройка сайта в ISPmanager

Создайте отдельный сайт или поддомен, например `panel.dendich.online`, затем
в настройках Python/Passenger укажите:

* корень приложения: абсолютный путь к `~/panel`;
* Python interpreter: `~/panel/.venv/bin/python`;
* startup file: `passenger_wsgi.py`;
* callable: `application`.

Если интерфейс ISPmanager просит `.htaccess`, положите его в каталог сайта:

```apache
PassengerEnabled on
PassengerAppRoot /absolute/path/to/panel
PassengerPython /absolute/path/to/panel/.venv/bin/python
PassengerStartupFile passenger_wsgi.py
```

Не добавляйте секреты в Git. Создайте `.env` только на хостинге по примеру
`.env.example`, а `servers.json` скопируйте из локальной панели.

## Фоновые задачи и встроенный бот

Passenger не прокидывает ASGI lifespan-события, поэтому `passenger_wsgi.py`
запускает lifespan вручную на рабочем цикле `a2wsgi`. На нём же стартуют:

* инициализация базы и `prune_sessions`;
* health-check, maintenance и бэкапы;
* встроенный Discord-бот (`app/bot/runner.py`) — один процесс, один `.env`.

Два замечания:

1. Бот требует `DISCORD_BOT_TOKEN` в `.env`. Привилегированные намерения
   (SERVER MEMBERS INTENT, MESSAGE CONTENT INTENT) включаются в Discord
   Developer Portal -> Bot; без них подключение отклоняется, а в логе
   появляется сообщение с инструкцией.
2. Несколько процессов Passenger безопасны: бот защищён lease-блокировкой в
   базе, второй инстанс не запускается.

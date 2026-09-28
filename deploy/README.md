# Production deployment

## Docker

1. Скопируйте `.env.example` в защищённый `.env`, оставьте `WARDOGS_ENV=production`,
   укажите HTTPS-адрес сайта, PostgreSQL URL, Discord-секреты и токены `RCON_TOKEN_<ID>`.
2. Скопируйте `servers.example.json` в `servers.json` и внесите реальные адреса серверов.
3. Проверьте права: `chmod 600 .env servers.json`.
4. Запустите `docker compose up -d --build`.
5. Настройте Nginx из `nginx.conf` или Caddy из `Caddyfile`.

Для локальной проверки PostgreSQL можно использовать примерный override:

```sh
cp docker-compose.postgres.example.yml docker-compose.postgres.yml
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d --build
```

В production используйте отдельную PostgreSQL-инсталляцию или managed service,
а не пароль из локального compose-файла.

После запуска можно проверить хост:

```sh
./deploy/validate-production.sh
./deploy/verify-host.sh
```

Контейнер публикует порт только на `127.0.0.1:8236`. RCON-порты не публикуются контейнером и должны быть доступны VPS только по исходящему доступу к игровым серверам.

## systemd

Создайте пользователя `wardogs`, установите исходники в `/opt/wardogs-site`,
секреты и `WARDOGS_DATABASE_URL` — в `/etc/wardogs/wardogs.env`. Затем установите
`wardogs.service` в `/etc/systemd/system/` и включите:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now wardogs
```

Для SQLite backup создайте каталог и включите timer:

```sh
sudo install -d -o wardogs -g wardogs -m 700 /var/backups/wardogs
sudo cp deploy/wardogs-backup.service deploy/wardogs-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wardogs-backup.timer
```

`backup-site-db.sh` делает SQLite online-backup, проверяет `PRAGMA integrity_check` и хранит
копии за последние 30 дней. Путь и retention можно переопределить через
`WARDOGS_DB_PATH`, `WARDOGS_BACKUP_DIR`, `WARDOGS_BACKUP_RETENTION_DAYS`.

Для production PostgreSQL используйте `backup-postgres.sh`, `restore-postgres.sh`,
`wardogs-postgres-backup.service` и `wardogs-postgres-backup.timer`. Нужны клиентские
утилиты `pg_dump` и `pg_restore`; URL базы берётся из `WARDOGS_DATABASE_URL`.

Перенос существующей SQLite-базы:

```sh
export WARDOGS_DATABASE_URL='postgresql://user:password@db.example/wardogs'
python3 deploy/migrate-sqlite-to-postgres.py --sqlite /var/lib/wardogs/site.db --dry-run
python3 deploy/migrate-sqlite-to-postgres.py --sqlite /var/lib/wardogs/site.db
```

Скрипт не удаляет существующую PostgreSQL-базу и рассчитан на пустую целевую базу.
Перед переключением приложения сравните количество строк и проверьте `/api/ready`.
Новые изменения схемы добавляйте парой файлов: в `migrations/` и
`migrations/postgres/` с одинаковым трёхзначным номером.

Восстановление выполняйте после остановки приложения:

```sh
sudo systemctl stop wardogs
sudo -u wardogs env WARDOGS_RESTORE_CONFIRM=YES \
  /opt/wardogs-site/deploy/restore-site-db.sh /var/backups/wardogs/site-YYYY-MM-DD.db
sudo systemctl start wardogs
```

## Firewall

Открывайте извне только `80/tcp` и `443/tcp`. Порт приложения `8236` остаётся на loopback. Не открывайте RCON-порты игровым серверам в интернет без отдельной необходимости и IP allowlist.
Шаблон Nginx дополнительно ограничивает `/api/` и OAuth-маршруты по IP; при использовании
другого reverse proxy перенесите эти rate limits на его уровень.

## Перед релизом

```sh
python -m compileall -q app
node --check static/app.js
pip install -r requirements-dev.txt
pytest -q
```

В production-образ намеренно не копируются тесты, документация, `.env` и реальные настройки серверов.
Перед обновлением сохраните `.env`, `servers.json` и резервную копию базы; после обновления проверьте
`docker compose ps` и `curl -fsS http://127.0.0.1:8236/api/health`.
Для проверки готовности базы используйте `curl -fsS http://127.0.0.1:8236/api/ready`.

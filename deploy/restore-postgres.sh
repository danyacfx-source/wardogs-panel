#!/usr/bin/env sh
set -eu
umask 077

[ "${WARDOGS_RESTORE_CONFIRM:-}" = "YES" ] || {
  echo "Для восстановления установите WARDOGS_RESTORE_CONFIRM=YES" >&2
  exit 1
}

DATABASE_URL=${WARDOGS_DATABASE_URL:-${DATABASE_URL:-}}
BACKUP_PATH=${1:-}

[ -n "$DATABASE_URL" ] || { echo "WARDOGS_DATABASE_URL не задан" >&2; exit 1; }
[ -n "$BACKUP_PATH" ] || { echo "Укажите путь к backup-файлу" >&2; exit 1; }
[ -r "$BACKUP_PATH" ] || { echo "Backup не найден: $BACKUP_PATH" >&2; exit 1; }
command -v pg_restore >/dev/null 2>&1 || { echo "pg_restore не найден" >&2; exit 1; }

pg_restore --list "$BACKUP_PATH" >/dev/null
pg_restore \
  --clean \
  --if-exists \
  --exit-on-error \
  --single-transaction \
  --no-owner \
  --no-acl \
  --dbname="$DATABASE_URL" \
  "$BACKUP_PATH"

echo "PostgreSQL восстановлен. Перезапустите приложение и проверьте /api/ready."
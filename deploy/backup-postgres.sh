#!/usr/bin/env sh
set -eu
umask 077

DATABASE_URL=${WARDOGS_DATABASE_URL:-${DATABASE_URL:-}}
BACKUP_DIR=${WARDOGS_BACKUP_DIR:-/var/backups/wardogs}
RETENTION_DAYS=${WARDOGS_BACKUP_RETENTION_DAYS:-30}
stamp=$(date +%F)
target="$BACKUP_DIR/site-$stamp.dump"
temporary="$BACKUP_DIR/.site-$stamp.dump.tmp"

[ -n "$DATABASE_URL" ] || { echo "WARDOGS_DATABASE_URL не задан" >&2; exit 1; }
command -v pg_dump >/dev/null 2>&1 || { echo "pg_dump не найден" >&2; exit 1; }
command -v pg_restore >/dev/null 2>&1 || { echo "pg_restore не найден" >&2; exit 1; }

mkdir -p "$BACKUP_DIR"
trap 'rm -f "$temporary"' EXIT
pg_dump --format=custom --no-owner --no-acl --file="$temporary" "$DATABASE_URL"
pg_restore --list "$temporary" >/dev/null
mv -f "$temporary" "$target"
find "$BACKUP_DIR" -type f -name 'site-*.dump' -mtime +"$RETENTION_DAYS" -delete
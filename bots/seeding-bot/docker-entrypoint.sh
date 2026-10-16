#!/bin/sh
set -eu

umask 077

data_dir="${DATA_DIR:-/app/data}"
backup_dir="${BACKUP_DIR:-/app/data/backups}"

if [ "$data_dir" != "/app/data" ]; then
  echo "BotHost deployment requires DATA_DIR=/app/data" >&2
  exit 1
fi

case "$backup_dir" in
  /app/data/*) ;;
  *)
    echo "BACKUP_DIR must be inside /app/data" >&2
    exit 1
    ;;
esac

mkdir -p "$data_dir" "$backup_dir"
chown -R node:node "$data_dir"
chmod 700 "$data_dir" "$backup_dir"

exec gosu node:node "$@"

#!/usr/bin/env sh
set -eu
umask 077

[ -z "${WARDOGS_DATABASE_URL:-}" ] || {
  echo "Для PostgreSQL используйте backup-postgres.sh" >&2
  exit 1
}

DB_PATH=${WARDOGS_DB_PATH:-/var/lib/wardogs/site.db}
BACKUP_DIR=${WARDOGS_BACKUP_DIR:-/var/backups/wardogs}
RETENTION_DAYS=${WARDOGS_BACKUP_RETENTION_DAYS:-30}
PYTHON=${WARDOGS_PYTHON:-python3}
stamp=$(date +%F)
target="$BACKUP_DIR/site-$stamp.db"
temporary="$BACKUP_DIR/.site-$stamp.db.tmp"

[ -r "$DB_PATH" ] || { echo "Database is not readable: $DB_PATH" >&2; exit 1; }
mkdir -p "$BACKUP_DIR"
trap 'rm -f "$temporary"' EXIT

"$PYTHON" - "$DB_PATH" "$temporary" <<'PY'
import sqlite3
import sys

source_path, target_path = sys.argv[1:3]
with sqlite3.connect(source_path) as source, sqlite3.connect(target_path) as target:
    source.backup(target)
    result = target.execute("PRAGMA integrity_check").fetchone()[0]
if result != "ok":
    raise SystemExit(f"Backup integrity check failed: {result}")
PY
mv -f "$temporary" "$target"
find "$BACKUP_DIR" -type f -name 'site-*.db' -mtime +"$RETENTION_DAYS" -delete

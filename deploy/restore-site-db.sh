#!/usr/bin/env sh
set -eu
umask 077

[ -z "${WARDOGS_DATABASE_URL:-}" ] || {
  echo "Для PostgreSQL используйте restore-postgres.sh" >&2
  exit 1
}

[ "${WARDOGS_RESTORE_CONFIRM:-}" = "YES" ] || {
  echo "Для восстановления установите WARDOGS_RESTORE_CONFIRM=YES" >&2
  exit 1
}

DB_PATH=${WARDOGS_DB_PATH:-/var/lib/wardogs/site.db}
BACKUP_PATH=${1:-}
PYTHON=${WARDOGS_PYTHON:-python3}

[ -n "$BACKUP_PATH" ] || { echo "Укажите путь к backup-файлу" >&2; exit 1; }
[ -r "$BACKUP_PATH" ] || { echo "Backup не найден: $BACKUP_PATH" >&2; exit 1; }
[ -w "$(dirname "$DB_PATH")" ] || { echo "Каталог базы недоступен для записи" >&2; exit 1; }

"$PYTHON" - "$BACKUP_PATH" "$DB_PATH" <<'PY'
import os
import shutil
import sqlite3
import sys
import time

source_path, target_path = sys.argv[1:3]
with sqlite3.connect(source_path) as source:
    if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise SystemExit("Backup integrity check failed")

if os.path.exists(target_path):
    safety_copy = f"{target_path}.before-restore-{int(time.time())}.db"
    shutil.copy2(target_path, safety_copy)

temporary = f"{target_path}.restore.tmp"
try:
    with sqlite3.connect(source_path) as source, sqlite3.connect(temporary) as target:
        source.backup(target)
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SystemExit("Restored database integrity check failed")
    os.replace(temporary, target_path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY

echo "Database restored. Restart the application and run verify-host.sh."
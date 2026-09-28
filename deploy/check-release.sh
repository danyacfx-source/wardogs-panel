#!/usr/bin/env sh
set -eu

root_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root_dir"
test_db="${TMPDIR:-/tmp}/wardogs-release-check-$$.db"
trap 'rm -rf app/__pycache__ tests/__pycache__ .pytest_cache "$test_db" "$test_db-shm" "$test_db-wal"' EXIT

python -m compileall -q app
node --check static/app.js
node --check static/portal.js

if command -v python >/dev/null 2>&1; then
  WARDOGS_ENV=development WARDOGS_DB="$test_db" python -m pytest -q
else
  echo "python не найден: установите requirements-dev.txt" >&2
  exit 1
fi

if [ -e .env ] || [ -e servers.json ] || [ -e site.db ]; then
  echo "В исходном каталоге найдены runtime-секреты; не добавляйте их в архив." >&2
  exit 1
fi

echo "Release checks passed."
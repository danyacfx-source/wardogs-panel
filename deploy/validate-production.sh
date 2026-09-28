#!/usr/bin/env sh
set -eu

root_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root_dir"

die() {
  echo "ERROR: $*" >&2
  exit 1
}

[ -f .env ] || die ".env не найден"
[ -f servers.json ] || die "servers.json не найден"
[ "$(stat -c '%a' .env)" = "600" ] || die "права .env должны быть 600"
[ "$(stat -c '%a' servers.json)" = "600" ] || die "права servers.json должны быть 600"

grep -Eq '^WARDOGS_ENV=production([[:space:]]|$)' .env || die "WARDOGS_ENV должен быть production"
grep -Eq '^WARDOGS_PUBLIC_URL=https://' .env || die "WARDOGS_PUBLIC_URL должен быть HTTPS"
grep -Eq '^WARDOGS_DATABASE_URL=(postgres|postgresql)://' .env || die "нужен PostgreSQL URL"
if grep -Eq '^WARDOGS_DEV_AUTH=(1|true|yes|on)([[:space:]]|$)' .env; then
  die "WARDOGS_DEV_AUTH включён"
fi

python -m compileall -q app
node --check static/app.js
node --check static/portal.js
sh -n deploy/*.sh

if command -v docker >/dev/null 2>&1; then
  docker compose config --quiet
fi

if command -v pg_isready >/dev/null 2>&1; then
  database_url=$(sed -n 's/^WARDOGS_DATABASE_URL=//p' .env | head -n 1)
  [ -n "$database_url" ] || die "WARDOGS_DATABASE_URL пуст"
  pg_isready --dbname="$database_url" >/dev/null || die "PostgreSQL недоступен"
else
  echo "WARNING: pg_isready не найден; доступность PostgreSQL не проверена" >&2
fi

echo "Production preflight passed."
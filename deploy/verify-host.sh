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
command -v docker >/dev/null 2>&1 || die "docker не найден"
command -v curl >/dev/null 2>&1 || die "curl не найден"

[ "$(stat -c '%a' .env)" = "600" ] || die "права .env должны быть 600"
[ "$(stat -c '%a' servers.json)" = "600" ] || die "права servers.json должны быть 600"

grep -Eq '^WARDOGS_ENV=production([[:space:]]|$)' .env || die "WARDOGS_ENV должен быть production"
grep -Eq '^WARDOGS_PUBLIC_URL=https://' .env || die "WARDOGS_PUBLIC_URL должен начинаться с https://"
grep -Eq '^WARDOGS_DATABASE_URL=(postgres|postgresql)://' .env || die "WARDOGS_DATABASE_URL должен указывать на PostgreSQL"
if grep -Eq '^WARDOGS_DEV_AUTH=(1|true|yes|on)([[:space:]]|$)' .env; then
  die "WARDOGS_DEV_AUTH включён"
fi

docker compose config --quiet
docker compose ps --status running --services | grep -qx wardogs || die "контейнер wardogs не запущен"

ready=$(curl -fsS --max-time 10 http://127.0.0.1:8236/api/ready)
echo "$ready" | grep -q '"ok":true' || die "приложение не готово: $ready"

headers=$(curl -fsSI --max-time 10 http://127.0.0.1:8236/api/health)
echo "$headers" | grep -qi '^x-content-type-options: nosniff' || die "нет X-Content-Type-Options"
echo "$headers" | grep -qi '^x-frame-options: DENY' || die "нет X-Frame-Options"

echo "Host verification passed."
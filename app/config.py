import json
import ipaddress
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

BASE_DIR = Path(__file__).resolve().parent.parent

def _load_dotenv(path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())

_load_dotenv(BASE_DIR / ".env")

ENVIRONMENT = (os.environ.get("WARDOGS_ENV") or "development").strip().lower()
PUBLIC_URL = (os.environ.get("WARDOGS_PUBLIC_URL") or "http://127.0.0.1:8236").rstrip("/")
# When a managed platform supplies PORT it runs behind a reverse proxy and
# needs the application to listen on the container interface.  Local runs
# retain the safer loopback default.
HOST = os.environ.get("WARDOGS_HOST") or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
# Bothost supplies the internal web port through PORT.  Keep the WARDOGS_*
# variant for local and VPS deployments.
PORT = int(os.environ.get("PORT") or os.environ.get("WARDOGS_PORT") or 8236)
COOKIE_SECURE = PUBLIC_URL.lower().startswith("https://")
DEV_AUTH = (os.environ.get("WARDOGS_DEV_AUTH") or "").strip().lower() in {"1", "true", "yes", "on"}
# Allowlist for the admin API surface (_require_perm/_require_any_perm).
# Plain IPs and CIDR networks are accepted; empty value disables the check.
ADMIN_IPS = []
for _token in re.split(r"[,;\s]+", os.environ.get("WARDOGS_ADMIN_IPS") or ""):
    _token = _token.strip()
    if not _token:
        continue
    try:
        ADMIN_IPS.append(ipaddress.ip_network(_token, strict=False))
    except ValueError:
        print(f"WARNING: WARDOGS_ADMIN_IPS: skipped invalid entry {_token!r}", file=sys.stderr)
DB_PATH = BASE_DIR / (os.environ.get("WARDOGS_DB") or "site.db")


def _pick_writable_db_path(path):
    """Возвращает путь к sqlite-файлу в каталоге, доступном для записи.

    Платформы вроде Bothost монтируют /app/data томом с правами, которые
    не позволяют текущему пользователю контейнера писать файлы. Падать
    из-за этого нельзя — пробуем запасные каталоги и громко пишем в лог.
    """
    candidates = [
        path,
        BASE_DIR / path.name,
        Path(tempfile.gettempdir()) / "wardogs" / path.name,
    ]
    for candidate in candidates:
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            probe = candidate.parent / ".wardogs-write-probe"
            probe.touch(exist_ok=True)
            probe.unlink()
            if candidate != path:
                print(
                    f"WARNING: каталог базы {path.parent} недоступен для записи; "
                    f"используется {candidate.parent}",
                    file=sys.stderr,
                )
            return candidate
        except OSError:
            continue
    print(
        f"ERROR: нет записываемого каталога для базы {path}; "
        "приложение упадёт при первом обращении к БД",
        file=sys.stderr,
    )
    return path


if not (os.environ.get("WARDOGS_DATABASE_URL") or os.environ.get("DATABASE_URL")):
    DB_PATH = _pick_writable_db_path(DB_PATH)
DATABASE_URL = (
    os.environ.get("WARDOGS_DATABASE_URL")
    or os.environ.get("DATABASE_URL")
    or ""
).strip()
DB_BACKEND = "postgres" if DATABASE_URL else "sqlite"
ALLOW_SQLITE_PRODUCTION = (os.environ.get("WARDOGS_ALLOW_SQLITE_PRODUCTION") or "").strip().lower() in {
    "1", "true", "yes", "on"
}
if DATABASE_URL:
    _database_parts = urlsplit(DATABASE_URL)
    if _database_parts.scheme.lower() not in {"postgres", "postgresql"} or not _database_parts.netloc:
        raise RuntimeError("WARDOGS_DATABASE_URL должен быть PostgreSQL URL")
DB_POOL_MIN = max(1, min(10, int(os.environ.get("WARDOGS_DB_POOL_MIN") or 1)))
DB_POOL_MAX = max(DB_POOL_MIN, min(50, int(os.environ.get("WARDOGS_DB_POOL_MAX") or 10)))
DB_COMMAND_TIMEOUT = max(
    5, min(120, int(os.environ.get("WARDOGS_DB_COMMAND_TIMEOUT") or 30))
)
REFRESH = max(2.0, float(os.environ.get("WARDOGS_REFRESH") or 5))
MAX_BODY_BYTES = max(16 * 1024, int(os.environ.get("WARDOGS_MAX_BODY_BYTES") or 256 * 1024))
MAX_RCON_RESPONSE_BYTES = max(
    64 * 1024,
    min(8 * 1024 * 1024, int(os.environ.get("WARDOGS_MAX_RCON_RESPONSE_BYTES") or 2 * 1024 * 1024)),
)
PLAYER_ACTIVITY_RETENTION_DAYS = max(
    30, min(730, int(os.environ.get("WARDOGS_PLAYER_ACTIVITY_RETENTION_DAYS") or 90))
)
DISABLE_BACKGROUND = (os.environ.get("WARDOGS_DISABLE_BACKGROUND") or "").strip().lower() in {
    "1", "true", "yes", "on"
}
ALLOW_INSECURE_RCON = (os.environ.get("WARDOGS_ALLOW_INSECURE_RCON") or "").strip().lower() in {
    "1", "true", "yes", "on"
}

_public_url = urlsplit(PUBLIC_URL)
if _public_url.scheme not in {"http", "https"} or not _public_url.netloc:
    raise RuntimeError("WARDOGS_PUBLIC_URL должен быть абсолютным URL с http:// или https://")
if _public_url.query or _public_url.fragment or _public_url.path not in {"", "/"}:
    raise RuntimeError("WARDOGS_PUBLIC_URL должен содержать только схему и домен без пути")

_configured_hosts = [
    host.strip().lower()
    for host in (os.environ.get("WARDOGS_ALLOWED_HOSTS") or "").split(",")
    if host.strip()
]
ALLOWED_HOSTS = list(dict.fromkeys(
    _configured_hosts
    + [_public_url.hostname or "", "127.0.0.1", "localhost"]
    + (["testserver"] if ENVIRONMENT != "production" else [])
))
ALLOWED_HOSTS = [host for host in ALLOWED_HOSTS if host]

STEAM_API_KEY = (os.environ.get("STEAM_API_KEY") or "").strip()
STEAM_REGRESSION = int(os.environ.get("STEAM_REGRESSION") or 0)

DISCORD_CLIENT_ID = (os.environ.get("DISCORD_CLIENT_ID") or "").strip()
DISCORD_CLIENT_SECRET = (os.environ.get("DISCORD_CLIENT_SECRET") or "").strip()
DISCORD_BOT_TOKEN = (os.environ.get("DISCORD_BOT_TOKEN") or "").strip()
DISCORD_BOT_PANEL_URL = (os.environ.get("DISCORD_BOT_PANEL_URL") or "").strip().rstrip("/")
DISCORD_BOT_PANEL_TOKEN = (os.environ.get("DISCORD_BOT_PANEL_TOKEN") or "").strip()
GUILD_ID = (os.environ.get("WARDOGS_GUILD_ID") or "").strip()

ADMIN_ROLES = [
    part.strip()
    for part in (os.environ.get("WARDOGS_ADMIN_ROLES") or "").split(",")
    if part.strip()
]
ADMIN_ROLE_IDS = {r for r in ADMIN_ROLES if r.isdigit()}
ADMIN_ROLE_NAMES = {r for r in ADMIN_ROLES if not r.isdigit()}

SESSION_TTL_DAYS = max(1, min(90, int(os.environ.get("WARDOGS_SESSION_TTL_DAYS") or 30)))
ROLES_TTL_SECONDS = max(60, min(3600, int(os.environ.get("WARDOGS_ROLES_TTL_SECONDS") or 300)))
HEALTHCHECK_INTERVAL = max(15, int(os.environ.get("WARDOGS_HEALTHCHECK_INTERVAL") or 60))
ALERT_WEBHOOK_URL = (os.environ.get("WARDOGS_ALERT_WEBHOOK_URL") or "").strip()
if ALERT_WEBHOOK_URL:
    _alert_url = urlsplit(ALERT_WEBHOOK_URL)
    if (
        _alert_url.scheme.lower() not in {"http", "https"}
        or not _alert_url.netloc
        or _alert_url.username
        or _alert_url.password
        or any(ch in ALERT_WEBHOOK_URL for ch in "\r\n")
    ):
        raise RuntimeError("WARDOGS_ALERT_WEBHOOK_URL должен быть безопасным HTTP(S)-URL")
    if ENVIRONMENT == "production" and _alert_url.scheme.lower() != "https":
        raise RuntimeError("В production webhook должен использовать HTTPS")


SERVERS_FILE = BASE_DIR / (os.environ.get("WARDOGS_SERVERS_FILE") or "servers.json")
_servers_json = (os.environ.get("WARDOGS_SERVERS_JSON") or "").strip()
if _servers_json:
    try:
        _raw = json.loads(_servers_json)
    except json.JSONDecodeError as exc:
        raise RuntimeError("WARDOGS_SERVERS_JSON должен содержать корректный JSON") from exc
elif SERVERS_FILE.exists():
    with open(SERVERS_FILE, "r", encoding="utf-8") as f:
        _raw = json.load(f)
elif ENVIRONMENT != "production" and (BASE_DIR / "servers.example.json").exists():
    SERVERS_FILE = BASE_DIR / "servers.example.json"
    with open(SERVERS_FILE, "r", encoding="utf-8") as f:
        _raw = json.load(f)
else:
    raise RuntimeError(f"Файл серверов не найден: {SERVERS_FILE}")

if not isinstance(_raw, dict):
    raise RuntimeError("Конфигурация servers.json должна быть JSON-объектом")
REFRESH = max(2.0, min(300.0, float(_raw.get("refresh_seconds") or REFRESH)))

SERVERS = []
_server_id_re = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
for i, s in enumerate(_raw.get("servers") or [], 1):
    if not isinstance(s, dict):
        raise RuntimeError(f"Конфигурация сервера #{i} должна быть объектом")
    sid = str((s.get("id") or "").strip() or f"srv{i}")
    if not _server_id_re.fullmatch(sid):
        raise RuntimeError(f"Некорректный id RCON-сервера: {sid!r}")
    srv = dict(s)
    srv["id"] = sid
    srv["name"] = str(srv.get("name") or f"WARDOGS #{i}")[:120]
    env_token = os.environ.get(f"RCON_TOKEN_{sid.upper().replace('-', '_')}")
    srv["token"] = env_token or srv.get("token") or ""
    if len(str(srv["token"])) > 4096:
        raise RuntimeError(f"RCON-токен слишком длинный для {sid}")
    srv["scheme"] = str(srv.get("scheme") or "http").lower()
    if srv["scheme"] not in {"http", "https"}:
        raise RuntimeError(f"Некорректная схема RCON для {sid}: {srv['scheme']}")
    srv["host"] = str(srv.get("host") or "").strip()
    srv["port"] = int(srv.get("port") or 0)
    if (
        not srv["host"]
        or len(srv["host"]) > 253
        or any(ch.isspace() for ch in srv["host"])
        or any(ch in srv["host"] for ch in "/?#@\\")
        or not 1 <= srv["port"] <= 65535
    ):
        raise RuntimeError(f"Некорректный адрес RCON для {sid}")
    srv["timeout"] = max(2, min(120, int(srv.get("timeout") or 45)))
    srv["max_response_bytes"] = MAX_RCON_RESPONSE_BYTES
    srv["connect_url"] = str(srv.get("connect_url") or "").strip()[:500]
    if srv["connect_url"]:
        connect_parts = urlsplit(srv["connect_url"])
        if connect_parts.scheme.lower() not in {"http", "https", "steam"}:
            raise RuntimeError(f"Некорректная схема connect_url для {sid}")
        if not connect_parts.netloc or any(ch in srv["connect_url"] for ch in "\r\n"):
            raise RuntimeError(f"Некорректный connect_url для {sid}")
    SERVERS.append(srv)


def validate_runtime():
    """Fail closed on unsafe production configuration before serving requests."""
    if DISCORD_CLIENT_ID and not re.fullmatch(r"\d{1,30}", DISCORD_CLIENT_ID):
        raise RuntimeError("DISCORD_CLIENT_ID должен быть числовым идентификатором")
    if GUILD_ID and not re.fullmatch(r"\d{1,20}", GUILD_ID):
        raise RuntimeError("WARDOGS_GUILD_ID должен быть числовым идентификатором")
    if len(DISCORD_CLIENT_SECRET) > 512 or len(DISCORD_BOT_TOKEN) > 512:
        raise RuntimeError("Discord-секрет слишком длинный")
    if DISCORD_BOT_PANEL_URL:
        bridge = urlsplit(DISCORD_BOT_PANEL_URL)
        if bridge.scheme not in {"http", "https"} or not bridge.netloc or bridge.query or bridge.fragment:
            raise RuntimeError("DISCORD_BOT_PANEL_URL должен быть безопасным HTTP(S)-URL")
        if bridge.scheme == "http" and bridge.hostname not in {"127.0.0.1", "localhost", "discord-bot"}:
            raise RuntimeError("Незашифрованный API бота разрешён только в локальной сети")
    if len(DISCORD_BOT_PANEL_TOKEN) > 512:
        raise RuntimeError("DISCORD_BOT_PANEL_TOKEN слишком длинный")
    if ENVIRONMENT != "production":
        return
    if not COOKIE_SECURE:
        raise RuntimeError("В production WARDOGS_PUBLIC_URL должен использовать HTTPS")
    if (_public_url.hostname or "").lower() not in ALLOWED_HOSTS:
        raise RuntimeError("WARDOGS_ALLOWED_HOSTS должен включать домен из WARDOGS_PUBLIC_URL")
    if DEV_AUTH:
        raise RuntimeError("WARDOGS_DEV_AUTH должен быть выключен в production")
    if DISABLE_BACKGROUND:
        raise RuntimeError("WARDOGS_DISABLE_BACKGROUND нельзя включать в production")
    if not DATABASE_URL and not ALLOW_SQLITE_PRODUCTION:
        raise RuntimeError(
            "В production нужен WARDOGS_DATABASE_URL для PostgreSQL; "
            "для одного контейнера с постоянным диском можно явно включить WARDOGS_ALLOW_SQLITE_PRODUCTION=1"
        )
    if not _servers_json and SERVERS_FILE.name == "servers.example.json":
        raise RuntimeError("В production нужен отдельный servers.json, а не servers.example.json")
    if not SERVERS:
        raise RuntimeError("В production должен быть настроен хотя бы один RCON-сервер")
    if any(not srv["token"] for srv in SERVERS):
        raise RuntimeError("Для каждого RCON-сервера нужен токен в env или servers.json")
    insecure_hosts = []
    for srv in SERVERS:
        if srv["scheme"] != "http" or ALLOW_INSECURE_RCON:
            continue
        try:
            local_or_private = ipaddress.ip_address(srv["host"]).is_private or ipaddress.ip_address(srv["host"]).is_loopback
        except ValueError:
            local_or_private = srv["host"].lower() in {"localhost", "rcon", "wardogs-rcon"}
        if not local_or_private:
            insecure_hosts.append(srv["id"])
    if insecure_hosts:
        raise RuntimeError(
            "Незашифрованный RCON к внешним хостам запрещён: "
            + ", ".join(insecure_hosts)
            + "; используйте https или явно подтвердите WARDOGS_ALLOW_INSECURE_RCON=1"
        )
    required = {
        "DISCORD_CLIENT_ID": DISCORD_CLIENT_ID,
        "DISCORD_CLIENT_SECRET": DISCORD_CLIENT_SECRET,
        "DISCORD_BOT_TOKEN": DISCORD_BOT_TOKEN,
        "WARDOGS_GUILD_ID": GUILD_ID,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError("Не настроены production-секреты: " + ", ".join(missing))
    if not ADMIN_ROLES:
        raise RuntimeError("WARDOGS_ADMIN_ROLES не должен быть пустым в production")

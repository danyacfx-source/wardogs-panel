"""Точка входа WARDOGS-сайта: FastAPI + статика + RCON-прокси."""

import asyncio
import hashlib
import logging
import re
import secrets
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

import aiohttp

try:
    from . import api, backup, config, db, donate, maintenance, monitor, vip_catalog
    from . import discord as dmod
except ImportError:  # Поддержка прямого запуска: python app/main.py
    import api
    import backup
    import config
    import db
    import discord as dmod
    import donate
    import maintenance
    import monitor
    import vip_catalog

log = logging.getLogger("main")

STATIC_DIR = config.BASE_DIR / "static"

# The upstream calculator's high-resolution tile pyramid is too large to
# vendor into the application archive. Serve only the exact, read-only map
# asset shapes the calculator needs through a fixed-origin proxy. This keeps
# the browser same-origin (canvas exports and CSP remain safe) and prevents
# the endpoint from becoming an open SSRF proxy.
#
# Upstream moved to wardogs.zone (Next.js). Tile URL shape:
#   /game/maps/tiles/{mapId}/{hash}/{z}/{x}_{y}.webp
# Our public path stays the historical zoom_N form so the vendored
# calculator client does not need a rewrite:
#   /calc-assets/maps/tiles/{map}/zoom_{z}/{x}_{y}.webp
CALC_TILE_MAPS = {
    "bakurani": ("kavkazi", "863d1ca7", 5),
    "ozeti": ("europe", "5463cef3", 6),
    "zestafona": ("northamerica", "d0486ad2", 6),
}
CALC_ASSET_PATH = re.compile(
    r"^maps/(?:tiles|tiles-color)/(bakurani|ozeti|zestafona)/"
    r"zoom_([0-7])/(\d+)_(\d+)\.webp$"
)
CALC_ASSET_ORIGIN = "https://wardogs.zone/game/maps/tiles/"


async def maintenance_loop(stop_event):
    """Удаляет истёкшие сессии, OAuth-state и idempotency keys."""
    try:
        from . import giveaways as _giveaways
    except ImportError:
        import giveaways as _giveaways
    while not stop_event.is_set():
        try:
            await db.prune_sessions()
            # Backfill RU3 for existing VIP holders and retry pending RCON sync
            # before the expiry sweep. Repeats every maintenance interval.
            await api.reconcile_vip_slots()
            await api.expire_vip_slots()
            await api.expire_managed_bans()
        except Exception:
            log.exception("Ошибка очистки временных записей базы")
        try:
            await api.close_lapsed_vip_tickets()
        except Exception:
            log.exception("Ошибка автозакрытия личных тикетов VIP")
        try:
            await _giveaways.sync_giveaways()
        except Exception:
            log.exception("Ошибка синхронизации розыгрышов")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=600)
        except asyncio.TimeoutError:
            pass


@asynccontextmanager
async def lifespan(app):
    config.validate_runtime()
    # Bothost при обновлении пересоздаёт контейнер и /app/data может быть
    # пуст — восстанавливаем site.db из резервного Discord-канала до init().
    if config.BACKUP_CHANNEL_ID and config.DISCORD_BOT_TOKEN:
        try:
            restored = await backup.restore_backup()
            if restored:
                log.info("site.db восстановлена из резервного канала")
        except Exception:
            log.exception("Ошибка восстановления site.db из Discord")
    await db.init()
    await db.prune_sessions()
    log.info(
        "База данных: %s (%s, %d байт)",
        config.DB_PATH,
        config.DB_BACKEND,
        config.DB_PATH.stat().st_size if config.DB_PATH.exists() else 0,
    )
    await vip_catalog.refresh(force=True)
    stop_health = asyncio.Event()
    health_task = None
    maintenance_task = None
    backup_task = None
    bot_task = None
    ban_queue_task = None
    if not config.DISABLE_BACKGROUND:
        monitor.players_hook = api.announce_vip_joins
        health_task = asyncio.create_task(monitor.health_loop(api.POOL, stop_health))
        maintenance_task = asyncio.create_task(maintenance_loop(stop_health))
        ban_queue_task = asyncio.create_task(api.ban_queue_loop(stop_health))
        if config.BACKUP_CHANNEL_ID and config.DISCORD_BOT_TOKEN:
            async def _initial_backup():
                try:
                    await asyncio.sleep(30)
                    await backup.send_backup()
                except Exception:
                    log.exception("Ошибка стартового бэкапа")
            backup_task = asyncio.create_task(backup.backup_loop(stop_health))
            asyncio.create_task(_initial_backup())
        # Discord-бот работает внутри панели (один процесс, один .env).
        if config.DISCORD_BOT_TOKEN:
            from .bot.runner import start_bot

            async def _run_bot():
                try:
                    await start_bot()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("Бот упал при запуске или работе")

            bot_task = asyncio.create_task(_run_bot())
            log.info("Discord-бот запускается как фоновая задача панели")
    log.info("WARDOGS-site запущен: %s (servers: %s)", config.PUBLIC_URL, ", ".join(s["id"] for s in config.SERVERS))
    yield
    stop_health.set()
    if bot_task is not None:
        bot_task.cancel()
    if backup_task is not None:
        try:
            await asyncio.wait_for(backup.send_backup(), timeout=20)
        except Exception:
            log.exception("Ошибка финального бэкапа")
    for task in (health_task, maintenance_task, ban_queue_task, backup_task, bot_task):
        if task is None:
            continue
        try:
            await asyncio.wait_for(task, timeout=10)
        except asyncio.TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    await asyncio.gather(*[s.api.close() for s in api.POOL.values()], return_exceptions=True)
    await db.close()


app = FastAPI(
    title="WARDOGS control panel",
    lifespan=lifespan,
    # Автодокументация отдаёт полную карту API без авторизации — это
    # разведывательная утечка. В production выключаем полностью; в dev
    # остаётся для отладки.
    docs_url=None if config.ENVIRONMENT == "production" else "/docs",
    redoc_url=None if config.ENVIRONMENT == "production" else "/redoc",
    openapi_url=None if config.ENVIRONMENT == "production" else "/openapi.json",
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=config.ALLOWED_HOSTS)
app.add_middleware(GZipMiddleware, minimum_size=1024)

_rate_windows = defaultdict(deque)


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    """Security headers, bounded request size, rate limit and double-submit CSRF."""
    client = api.client_ip(request.headers, request.client.host if request.client else "")
    supplied_request_id = request.headers.get("X-Request-ID") or ""
    request_id = (
        supplied_request_id
        if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", supplied_request_id)
        else uuid.uuid4().hex
    )

    def finish(response):
        # Vue templates are precompiled into static render functions, so the
        # browser never needs eval()/Function() and CSP can stay strict.
        script_src = "script-src 'self'"
        content_security_policy = (
            "default-src 'self'; object-src 'none'; connect-src 'self'; "
            "img-src 'self' data: https://avatars.steamstatic.com https://avatars.cloudflare.steamstatic.com https://steamcdn-a.akamaihd.net https://cdn.discordapp.com https://media.discordapp.net; style-src 'self'; "
            "style-src-attr 'unsafe-inline'; "
            f"{script_src}; font-src 'self' data:; "
            "base-uri 'self'; frame-ancestors 'self'; "
            "form-action 'self' https://steamcommunity.com https://discord.com https://yoomoney.ru"
        )
        response.headers.setdefault("X-Request-ID", request_id)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault(
            "X-Frame-Options",
            "SAMEORIGIN" if request.url.path.startswith("/static/calc-app/") else "DENY",
        )
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        response.headers.setdefault("X-Permitted-Cross-Domain-Policies", "none")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault(
            "Content-Security-Policy",
            content_security_policy,
        )
        if config.COOKIE_SECURE:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        elif request.url.path.startswith("/static/") and response.status_code == 200:
            # ?v= — хэш содержимого, поэтому такой файл не меняется никогда.
            if request.query_params.get("v"):
                response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
            elif request.url.path.startswith("/static/wardogs-theme/"):
                response.headers.setdefault("Cache-Control", "public, max-age=86400")
        if request.url.path == "/api/session" and not request.cookies.get(api.CSRF_COOKIE):
            response.set_cookie(
                api.CSRF_COOKIE,
                secrets.token_urlsafe(24),
                secure=config.COOKIE_SECURE,
                samesite="strict",
                httponly=False,
                path="/",
            )
        return response

    now = time.monotonic()
    path = request.url.path
    is_static = path.startswith("/static/")
    is_asset = path.startswith("/calc-assets/")
    is_auth = path.startswith("/api/auth/")
    is_mutation = request.method in {"POST", "PUT", "PATCH", "DELETE"}

    # A calculator page can legitimately request dozens of JS, CSS, icon and
    # map resources during one render. Rate limiting those immutable GETs
    # caused false 429s and blank screens after a few reloads. API traffic and
    # the allow-listed tile proxy remain bounded; static files are protected by
    # the fixed filesystem routes and the proxy path allowlist instead.
    if not is_static:
        scope = "asset" if is_asset else ("auth" if is_auth else ("write" if is_mutation else "read"))
        key = (client, scope)
        window = _rate_windows[key]
        while window and now - window[0] >= 60:
            window.popleft()
        limit = {"auth": 20, "write": 90, "read": 240, "asset": 600}[scope]
        if len(window) >= limit:
            return finish(JSONResponse({"error": {"code": "rate_limited", "message": "Слишком много запросов, попробуйте позже"}}, status_code=429))
        window.append(now)
        if len(_rate_windows) > 5000:
            for old_key, old_window in list(_rate_windows.items()):
                if not old_window or now - old_window[-1] > 300:
                    _rate_windows.pop(old_key, None)

    content_length = request.headers.get("content-length")
    try:
        declared_length = int(content_length) if content_length is not None else 0
    except (TypeError, ValueError):
        return finish(JSONResponse(
            {"error": {"code": "invalid_content_length", "message": "Некорректный Content-Length"}},
            status_code=400,
        ))
    if declared_length < 0:
        return finish(JSONResponse(
            {"error": {"code": "invalid_content_length", "message": "Некорректный Content-Length"}},
            status_code=400,
        ))
    if declared_length > config.MAX_BODY_BYTES:
        return finish(JSONResponse(
            {"error": {"code": "request_too_large", "message": "Слишком большой запрос"}},
            status_code=413,
        ))
    if content_length is None and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        # Content-Length is optional for chunked requests. Read those bodies
        # in bounded chunks and cache the result for downstream handlers.
        chunks = []
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > config.MAX_BODY_BYTES:
                return finish(JSONResponse(
                    {"error": {"code": "request_too_large", "message": "Слишком большой запрос"}},
                    status_code=413,
                ))
            chunks.append(chunk)
        request._body = b"".join(chunks)

    path = request.url.path
    protected = path.startswith("/api/") and request.method in {"POST", "PUT", "PATCH", "DELETE"}
    # Routes that authenticate without browser cookies (bearer/HMAC tokens, game servers, bots)
    # or that are intentionally anonymous. They skip Origin and CSRF checks.
    token_exempt = {
        "/api/auth/dev", "/api/auth/steam/callback", "/api/auth/discord/callback",
        "/api/public/clan-sync",
        "/api/bot/v1/heartbeat", "/api/bot/v1/events",
    }
    is_killfeed_ingest = re.fullmatch(r"/api/killfeed/[^/]+", path) is not None
    if protected and path not in token_exempt and not is_killfeed_ingest:
        fetch_site = (request.headers.get("sec-fetch-site") or "").lower()
        if fetch_site == "cross-site":
            return finish(JSONResponse({"error": {"code": "origin", "message": "Недопустимый источник запроса"}}, status_code=403))
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != config.PUBLIC_URL:
            return finish(JSONResponse({"error": {"code": "origin", "message": "Недопустимый источник запроса"}}, status_code=403))
        # Double-submit CSRF applies only when the request carries the ambient session cookie.
        if request.cookies.get(api.SESSION_COOKIE):
            csrf_cookie = request.cookies.get(api.CSRF_COOKIE) or ""
            csrf_header = request.headers.get("X-CSRF-Token") or ""
            if not csrf_cookie or not secrets.compare_digest(csrf_cookie, csrf_header):
                return finish(JSONResponse({"error": {"code": "csrf", "message": "Обновите страницу и повторите действие"}}, status_code=403))
    closed = await maintenance.closed_section_for(path, request.method)
    if closed:
        section, entry, is_data = closed
        user = await api._current_user(request)
        me = await api._client_status(request, user) if user else None
        permissions = (me or {}).get("permissions") or []
        if dmod.OWNER_PERM not in permissions and not (is_data and permissions):
            if section.get("page") and not is_data:
                return finish(HTMLResponse(
                    maintenance.page_html(section, entry, me), status_code=503,
                    headers={"Cache-Control": "no-store"},
                ))
            return finish(JSONResponse(
                {"detail": entry["message"], "error": {"code": "maintenance", "message": entry["message"]}},
                status_code=503,
            ))
    response = await call_next(request)
    return finish(response)

app.include_router(api.router)
app.include_router(donate.router)



@app.get("/calc-assets/{asset_path:path}")
async def calculator_asset_proxy(asset_path: str):
    """Fetch only allow-listed calculator map tiles from the upstream CDN."""
    match = CALC_ASSET_PATH.fullmatch(asset_path)
    if not match:
        return JSONResponse({"error": "asset_not_found"}, status_code=404)

    map_key, zoom_s, x_s, y_s = match.groups()
    upstream_map, tile_hash, max_zoom = CALC_TILE_MAPS[map_key]
    zoom = int(zoom_s)
    if zoom > max_zoom:
        return JSONResponse({"error": "asset_not_found"}, status_code=404)

    # tiles-color is not published upstream — fall back to the grayscale pyramid.
    upstream_path = f"{upstream_map}/{tile_hash}/{zoom}/{x_s}_{y_s}.webp"

    try:
        timeout = aiohttp.ClientTimeout(total=20, connect=5)
        async with aiohttp.ClientSession(timeout=timeout) as client:
            async with client.get(
                CALC_ASSET_ORIGIN + upstream_path,
                headers={
                    "Accept": "image/webp, application/json, application/octet-stream",
                    "Referer": "https://wardogs.zone/",
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                },
            ) as upstream:
                if upstream.status != 200:
                    return JSONResponse({"error": "asset_unavailable"}, status_code=404)
                body = await upstream.read()
                if len(body) > 2 * 1024 * 1024:
                    return JSONResponse({"error": "asset_too_large"}, status_code=502)
                content_type = upstream.headers.get("Content-Type", "application/octet-stream").split(";", 1)[0]
    except (aiohttp.ClientError, asyncio.TimeoutError):
        log.warning("Calculator asset upstream unavailable: %s", upstream_path)
        return JSONResponse({"error": "asset_unavailable"}, status_code=502)

    return Response(
        content=body,
        media_type=content_type,
        headers={
            "Cache-Control": "public, max-age=604800, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )

@app.get("/api/health")
async def health():
    return {"ok": True}


@app.get("/api/ready")
async def ready():
    """Readiness probe: the process is alive and the database is usable."""
    try:
        version = await db.schema_version()
        audit = await db.verify_site_audit_chain()
    except Exception:
        log.exception("Readiness check failed")
        return JSONResponse({"ok": False, "database": False}, status_code=503)
    if version < db.REQUIRED_SCHEMA_VERSION or not audit["ok"]:
        return JSONResponse(
            {
                "ok": False,
                "database": True,
                "database_backend": config.DB_BACKEND,
                "audit": audit,
                "schema_version": version,
                "required_schema_version": db.REQUIRED_SCHEMA_VERSION,
            },
            status_code=503,
        )
    return {
        "ok": True,
        "database": True,
        "database_backend": config.DB_BACKEND,
        "audit": audit,
        "schema_version": version,
    }


@app.get("/api/health/servers")
async def servers_health(request: Request):
    """Безопасный status-check: не раскрывает RCON-токены и конфигурацию."""
    user = await api._current_user(request)
    if not user:
        return {"ok": False, "servers": [], "detail": "нужен вход"}
    if not await api._request_permissions(request, user):
        return {"ok": False, "servers": [], "detail": "недостаточно прав"}
    rows = []
    for srv in api.POOL.values():
        try:
            data = await srv.api.health()
            rows.append({"id": srv.id, "ok": True, "uptime_seconds": data.get("uptimeSeconds")})
        except Exception:
            rows.append({"id": srv.id, "ok": False})
    return {"ok": all(row["ok"] for row in rows), "servers": rows}


_ASSET_RE = re.compile(r"""(/static/[\w./-]+\.(?:js|css))(?:\?v=[^"'\s>]*)?""")
_TEMPLATE_RE = re.compile(r"<!-- WARDOGS_APP_TEMPLATE_START -->.*?<!-- WARDOGS_APP_TEMPLATE_END -->", re.S)
_asset_hashes = {}
_page_sources = {}


def _asset_url(match):
    """Версия файла — хэш содержимого: браузер кэширует его надолго и сам видит обновление."""
    url = match.group(1)
    path = STATIC_DIR / url[len("/static/"):]
    try:
        stat = path.stat()
    except OSError:
        return url
    stamp = (stat.st_mtime_ns, stat.st_size)
    cached = _asset_hashes.get(url)
    if not cached or cached[0] != stamp:
        cached = (stamp, hashlib.sha256(path.read_bytes()).hexdigest()[:12])
        _asset_hashes[url] = cached
    return f"{url}?v={cached[1]}"


def _page(name):
    """HTML страницы. Шаблон Vue уже скомпилирован в *.render.js, браузеру он не нужен."""
    path = STATIC_DIR / name
    stamp = path.stat().st_mtime_ns
    cached = _page_sources.get(name)
    if not cached or cached[0] != stamp:
        cached = (stamp, _TEMPLATE_RE.sub("", path.read_text(encoding="utf-8")))
        _page_sources[name] = cached
    return Response(
        _ASSET_RE.sub(_asset_url, cached[1]),
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/")
async def index():
    idx = STATIC_DIR / "index.html"
    if not idx.exists():
        return {"ok": True, "note": "статики нет, иди по /api"}
    return _page("index.html")


@app.get("/panel")
async def panel():
    p = STATIC_DIR / "panel.html"
    if not p.exists():
        return {"ok": True, "note": "панели нет"}
    return _page("panel.html")


@app.get("/stats")
async def stats_page():
    return _page("stats.html")


@app.get("/activity")
@app.get("/activity/top")
async def activity_page():
    return _page("activity.html")


@app.get("/clans")
async def clans_page():
    return _page("clans.html")


@app.get("/community")
async def community_page():
    return _page("community.html")


@app.get("/support")
async def support_page():
    return _page("support.html")


@app.get("/calc")
async def calc_page():
    return _page("calc.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="root")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=config.HOST, port=config.PORT, server_header=False)

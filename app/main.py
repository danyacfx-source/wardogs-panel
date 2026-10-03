"""Точка входа WARDOGS-сайта: FastAPI + статика + RCON-прокси."""

import asyncio
import logging
import re
import secrets
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

import aiohttp

try:
    from . import api, config, db, donate, monitor
except ImportError:  # Поддержка прямого запуска: python app/main.py
    import api
    import config
    import db
    import donate
    import monitor

log = logging.getLogger("main")

STATIC_DIR = config.BASE_DIR / "static"

# The upstream calculator's high-resolution tile pyramid is too large to
# vendor into the application archive. Serve only the exact, read-only map
# asset shapes the calculator needs through a fixed-origin proxy. This keeps
# the browser same-origin (canvas exports and CSP remain safe) and prevents
# the endpoint from becoming an open SSRF proxy.
CALC_ASSET_PATH = re.compile(
    r"^(?:maps/(?:tiles|tiles-color)/(?:bakurani|ozeti|zestafona)/"
    r"zoom_[0-7]/\d+_\d+\.webp|"
    r"data/terrain/(?:bakurani|ozeti|zestafona)/"
    r"(?:manifest\.json|chunks/\d+_\d+\.bin))$"
)
CALC_ASSET_ORIGIN = "https://zavod-wardogs.ru/calc-assets/"


async def maintenance_loop(stop_event):
    """Удаляет истёкшие сессии, OAuth-state и idempotency keys."""
    while not stop_event.is_set():
        try:
            await db.prune_sessions()
            await api.expire_vip_slots()
            await api.expire_managed_bans()
        except Exception:
            log.exception("Ошибка очистки временных записей базы")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=600)
        except asyncio.TimeoutError:
            pass


@asynccontextmanager
async def lifespan(app):
    config.validate_runtime()
    await db.init()
    await db.prune_sessions()
    stop_health = asyncio.Event()
    health_task = None
    maintenance_task = None
    if not config.DISABLE_BACKGROUND:
        health_task = asyncio.create_task(monitor.health_loop(api.POOL, stop_health))
        maintenance_task = asyncio.create_task(maintenance_loop(stop_health))
    log.info("WARDOGS-site запущен: %s (servers: %s)", config.PUBLIC_URL, ", ".join(s["id"] for s in config.SERVERS))
    yield
    stop_health.set()
    for task in (health_task, maintenance_task):
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
            "img-src 'self' data:; style-src 'self'; "
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
        if request.url.path == "/api/session" and not request.cookies.get("wds_csrf"):
            response.set_cookie(
                "wds_csrf",
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
            from fastapi.responses import JSONResponse
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
        from fastapi.responses import JSONResponse
        return finish(JSONResponse(
            {"error": {"code": "invalid_content_length", "message": "Некорректный Content-Length"}},
            status_code=400,
        ))
    if declared_length < 0:
        from fastapi.responses import JSONResponse
        return finish(JSONResponse(
            {"error": {"code": "invalid_content_length", "message": "Некорректный Content-Length"}},
            status_code=400,
        ))
    if declared_length > config.MAX_BODY_BYTES:
        from fastapi.responses import JSONResponse
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
                from fastapi.responses import JSONResponse
                return finish(JSONResponse(
                    {"error": {"code": "request_too_large", "message": "Слишком большой запрос"}},
                    status_code=413,
                ))
            chunks.append(chunk)
        request._body = b"".join(chunks)

    protected = request.url.path.startswith("/api/") and request.method in {"POST", "PUT", "PATCH", "DELETE"}
    exempt = {"/api/auth/dev", "/api/auth/steam/callback", "/api/auth/discord/callback"}
    if protected and request.url.path not in exempt and request.cookies.get(api.SESSION_COOKIE):
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != config.PUBLIC_URL:
            from fastapi.responses import JSONResponse
            return finish(JSONResponse({"error": {"code": "origin", "message": "Недопустимый источник запроса"}}, status_code=403))
        csrf_cookie = request.cookies.get("wds_csrf") or ""
        csrf_header = request.headers.get("X-CSRF-Token") or ""
        if not csrf_cookie or not secrets.compare_digest(csrf_cookie, csrf_header):
            from fastapi.responses import JSONResponse
            return finish(JSONResponse({"error": {"code": "csrf", "message": "Обновите страницу и повторите действие"}}, status_code=403))
    response = await call_next(request)
    return finish(response)

app.include_router(api.router)
app.include_router(donate.router)



@app.get("/calc-assets/{asset_path:path}")
async def calculator_asset_proxy(asset_path: str):
    """Fetch only allow-listed calculator map assets from the upstream CDN."""
    if not CALC_ASSET_PATH.fullmatch(asset_path):
        return JSONResponse({"error": "asset_not_found"}, status_code=404)

    try:
        timeout = aiohttp.ClientTimeout(total=20, connect=5)
        async with aiohttp.ClientSession(timeout=timeout) as client:
            async with client.get(
                CALC_ASSET_ORIGIN + asset_path,
                headers={"Accept": "image/webp, application/json, application/octet-stream"},
            ) as upstream:
                if upstream.status != 200:
                    return JSONResponse({"error": "asset_unavailable"}, status_code=404)
                body = await upstream.read()
                if len(body) > 2 * 1024 * 1024:
                    return JSONResponse({"error": "asset_too_large"}, status_code=502)
                content_type = upstream.headers.get("Content-Type", "application/octet-stream").split(";", 1)[0]
    except (aiohttp.ClientError, asyncio.TimeoutError):
        log.warning("Calculator asset upstream unavailable: %s", asset_path)
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
    me = await api._user_status(user)
    if not me.get("permissions"):
        return {"ok": False, "servers": [], "detail": "недостаточно прав"}
    rows = []
    for srv in api.POOL.values():
        try:
            data = await srv.api.health()
            rows.append({"id": srv.id, "ok": True, "uptime_seconds": data.get("uptimeSeconds")})
        except Exception:
            rows.append({"id": srv.id, "ok": False})
    return {"ok": all(row["ok"] for row in rows), "servers": rows}


@app.get("/")
async def index():
    idx = STATIC_DIR / "index.html"
    if not idx.exists():
        return {"ok": True, "note": "статики нет, иди по /api"}
    return FileResponse(idx, headers={"Cache-Control": "no-store"})


@app.get("/panel")
async def panel():
    p = STATIC_DIR / "panel.html"
    if not p.exists():
        return {"ok": True, "note": "панели нет"}
    return FileResponse(p)


@app.get("/stats")
async def stats_page():
    return FileResponse(STATIC_DIR / "stats.html", headers={"Cache-Control": "no-store"})


@app.get("/activity")
@app.get("/activity/top")
async def activity_page():
    return FileResponse(STATIC_DIR / "activity.html", headers={"Cache-Control": "no-store"})


@app.get("/clans")
async def clans_page():
    return FileResponse(STATIC_DIR / "clans.html", headers={"Cache-Control": "no-store"})


@app.get("/support")
async def support_page():
    return FileResponse(STATIC_DIR / "support.html", headers={"Cache-Control": "no-store"})


@app.get("/calc")
async def calc_page():
    return FileResponse(STATIC_DIR / "calc.html", headers={"Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="root")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=config.HOST, port=config.PORT, server_header=False)

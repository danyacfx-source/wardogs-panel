"""WSGI entry point for shared hosting running Phusion Passenger.

The application itself is ASGI (FastAPI).  Passenger accepts WSGI only, so
``a2wsgi`` provides the narrow compatibility layer.  Bothost and VPS/Docker
deployments do not use this file.
"""

import asyncio
import atexit
import logging

from a2wsgi import ASGIMiddleware

from app.main import app as asgi_app

log = logging.getLogger("passenger")

# Passenger never sends ASGI lifespan events, so uvicorn's startup never runs:
# no database init, no health/maintenance/backup loops, no embedded Discord bot.
# Drive the lifespan by hand on the middleware's own event loop — the same loop
# that serves requests, so in-process calls into bot cogs stay single-loop.
application = ASGIMiddleware(asgi_app)

_lifespan = asgi_app.router.lifespan_context(asgi_app)


async def _enter_lifespan():
    await _lifespan.__aenter__()


asyncio.run_coroutine_threadsafe(_enter_lifespan(), application.loop).result()


@atexit.register
def _leave_lifespan():
    async def _close():
        try:
            await _lifespan.__aexit__(None, None, None)
        except Exception:
            log.exception("Ошибка остановки lifespan панели")

    try:
        asyncio.run_coroutine_threadsafe(_close(), application.loop).result(timeout=20)
    except Exception:
        log.exception("Не удалось дождаться остановки lifespan панели")

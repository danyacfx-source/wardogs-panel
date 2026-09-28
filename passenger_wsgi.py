"""WSGI entry point for shared hosting running Phusion Passenger.

The application itself is ASGI (FastAPI).  Passenger accepts WSGI only, so
``a2wsgi`` provides the narrow compatibility layer.  Bothost and VPS/Docker
deployments do not use this file.
"""

import asyncio

from a2wsgi import ASGIMiddleware

from app import config, db
from app.main import app as asgi_app


async def _bootstrap_database():
    """Passenger does not emit ASGI lifespan events, initialise storage once."""
    config.validate_runtime()
    await db.init()


asyncio.run(_bootstrap_database())

# Passenger requires this exact module-level WSGI callable name.
application = ASGIMiddleware(asgi_app)

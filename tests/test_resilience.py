import asyncio

import pytest

from app import api
from app.rcon_client import WardogsRCON


def test_concurrent_cache_misses_are_coalesced():
    async def run():
        key = ("test", "coalesced")
        api._cache.pop(key, None)
        api._cache_key_locks.pop(key, None)
        calls = 0

        async def loader():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            return {"ok": True}

        result = await asyncio.gather(
            api._cached("test", "coalesced", loader),
            api._cached("test", "coalesced", loader),
            api._cached("test", "coalesced", loader),
        )
        assert calls == 1
        assert result == [{"ok": True}] * 3

    asyncio.run(run())


def test_rcon_paths_cannot_escape_the_api_namespace():
    with pytest.raises(ValueError):
        WardogsRCON._validate_path("https://127.0.0.1/admin")
    with pytest.raises(ValueError):
        WardogsRCON._validate_path("/v1/../admin")
    assert WardogsRCON._validate_path("/v1/status") == "/v1/status"
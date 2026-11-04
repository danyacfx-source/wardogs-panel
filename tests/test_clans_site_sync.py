import asyncio
import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "app" / "bot"))

from cogs import clans as clans_module  # noqa: E402


def _cog(state):
    cog = clans_module.ClansCog.__new__(clans_module.ClansCog)
    cog.store = SimpleNamespace(state=state)
    return cog


def _record_sync(monkeypatch, failures=0):
    calls = []

    async def fake_sync(clans):
        calls.append(clans)
        if len(calls) <= failures:
            raise RuntimeError("site is down")
        return {"skipped": False, "status": 200}

    monkeypatch.setattr(clans_module, "sync_site", fake_sync)
    return calls


def test_startup_sync_skips_empty_state(monkeypatch):
    calls = _record_sync(monkeypatch)
    asyncio.run(_cog({"clans": {}})._site_sync_with_retries(delay=0))
    asyncio.run(_cog({})._site_sync_with_retries(delay=0))
    assert calls == []


def test_startup_sync_sends_existing_clans(monkeypatch):
    calls = _record_sync(monkeypatch)
    clan = {"id": "12345678901234567", "name": "Волки"}
    asyncio.run(_cog({"clans": {clan["id"]: clan}})._site_sync_with_retries(delay=0))
    assert calls == [[clan]]


def test_startup_sync_still_retries_when_site_is_down(monkeypatch):
    calls = _record_sync(monkeypatch, failures=2)
    clan = {"id": "12345678901234567", "name": "Волки"}
    asyncio.run(_cog({"clans": {clan["id"]: clan}})._site_sync_with_retries(delay=0))
    assert len(calls) == 3

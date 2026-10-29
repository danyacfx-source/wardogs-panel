"""Existing RU1/RU2 VIP entitlements are automatically mirrored to RU3."""

import asyncio
import time
from types import SimpleNamespace

from app import api, config, db


def test_vip_backfill_preserves_paid_expiry_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "three-vip.db")
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(
        api, "POOL",
        {sid: SimpleNamespace(id=sid) for sid in ("ru1", "ru2", "ru3")},
    )
    synced = []

    async def fake_sync(item, add=True):
        synced.append((item["server_id"], item["steam_id"], add))
        await db.mark_vip_sync(item["id"], "synced")
        return True, ""

    monkeypatch.setattr(api, "_sync_vip_record", fake_sync)

    async def run():
        await db.init()
        steam = "76561190000000001"
        expiry = time.time() + 86400
        await db.save_vip_slot("ru1", steam, "Player", expiry, "Paid", "admin")
        await db.save_vip_slot("ru2", steam, "Player", expiry, "Paid", "admin")
        # Existing paid entitlements must not be changed or synced again.
        before = await db.list_player_vip_slots(steam)
        result = await api.reconcile_vip_slots()
        assert result == {"created": 1, "synced": 3, "failed": 0}
        slots = await db.list_player_vip_slots(steam)
        assert [s["server_id"] for s in slots] == ["ru1", "ru2", "ru3"]
        assert all(s["expires_utc"] == expiry for s in slots)
        assert before[0]["id"] == slots[0]["id"]
        assert before[1]["id"] == slots[1]["id"]
        assert [x[0] for x in synced].count("ru3") == 1
        # No changes on the next maintenance tick.
        assert await api.reconcile_vip_slots() == {
            "created": 0, "synced": 0, "failed": 0
        }
        await db.close()

    asyncio.run(run())


def test_vip_backfill_retries_failed_rcon_and_preserves_permanent_vip(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "permanent-vip.db")
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(
        api, "POOL",
        {sid: SimpleNamespace(id=sid) for sid in ("ru1", "ru2", "ru3")},
    )
    attempts = []

    async def fake_sync(item, add=True):
        attempts.append(item["server_id"])
        if item["server_id"] == "ru3" and attempts.count("ru3") == 1:
            await db.mark_vip_sync(item["id"], "error", "offline")
            return False, "offline"
        await db.mark_vip_sync(item["id"], "synced")
        return True, ""

    monkeypatch.setattr(api, "_sync_vip_record", fake_sync)

    async def run():
        await db.init()
        steam = "76561190000000002"
        first = await db.save_vip_slot("ru1", steam, "Player", None, "VIP", "admin")
        await db.mark_vip_sync(first["id"], "synced")
        result = await api.reconcile_vip_slots()
        assert result["created"] == 2 and result["failed"] == 1
        slots = await db.list_player_vip_slots(steam)
        assert len(slots) == 3
        assert all(s["expires_utc"] is None for s in slots)
        assert next(s for s in slots if s["server_id"] == "ru3")["sync_state"] == "error"
        retry = await api.reconcile_vip_slots()
        assert retry == {"created": 0, "synced": 1, "failed": 0}
        slots = await db.list_player_vip_slots(steam)
        assert all(s["sync_state"] == "synced" for s in slots)
        await db.close()

    asyncio.run(run())


def test_vip_backfill_does_not_resurrect_fully_expired_vip(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "expired-vip.db")
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(
        api, "POOL",
        {sid: SimpleNamespace(id=sid) for sid in ("ru1", "ru2", "ru3")},
    )

    async def run():
        await db.init()
        steam = "76561190000000003"
        await db.save_vip_slot("ru1", steam, "Player", time.time() - 60, "Old", "admin")
        assert await api.reconcile_vip_slots() == {
            "created": 0, "synced": 0, "failed": 0
        }
        assert await db.list_vip_slots("ru3") == []
        await db.close()

    asyncio.run(run())

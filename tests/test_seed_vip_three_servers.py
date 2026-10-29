"""The embedded seeding bot grants RU3 VIP while preserving external slots."""

import asyncio
from types import SimpleNamespace

from app.bot import runner  # noqa: F401
from cogs import seeding


STEAM = "76561190000000001"


class Rcon:
    def __init__(self, live=None):
        self.live = set(live or [])
        self.calls = []

    async def reserved_snapshot(self):
        return {"live": list(self.live), "document": []}

    async def set_reserved(self, steam, present):
        self.calls.append((steam, present))
        if present:
            self.live.add(steam)
        else:
            self.live.discard(steam)
        return {"active": present}


def test_seed_vip_secondary_slots_and_safe_cleanup(monkeypatch):
    saved = {}
    membership = {"state": "active", "active_until": 100000}
    monkeypatch.setattr(seeding, "seed_get_state", lambda k, default=None: saved.get(k, default))
    monkeypatch.setattr(seeding, "seed_set_state", lambda k, v: saved.update({k: v}))
    monkeypatch.setattr(
        seeding, "seed_get_vip_identity_by_id",
        lambda user_id: {"steam_id": STEAM, "discord_id": "123", "linked": True},
    )
    monkeypatch.setattr(seeding, "seed_get_vip_membership", lambda user_id: membership)
    monkeypatch.setattr(
        seeding.ReservedProtectionService, "is_protected",
        lambda *args: False,
    )
    monkeypatch.setattr(seeding, "_panel_vip_active", lambda steam: False)
    main = Rcon()
    second = Rcon([STEAM])
    third = Rcon()
    cog = SimpleNamespace(
        _servers=lambda: [
            ("main", "RU1", main), ("second", "RU2", second), ("third", "RU3", third)
        ],
        donations=SimpleNamespace(active=lambda *args: False),
    )

    async def run():
        await seeding.SeedingCog._vip_reconcile_secondary_servers(cog, {"u1"}, 1000)
        assert STEAM in third.live
        assert STEAM in second.live
        entry = saved["vip_secondary_slot_ownership_v1"][STEAM]
        assert entry["servers"] == {"second": "external", "third": "bot"}

        membership["state"] = "inactive"
        await seeding.SeedingCog._vip_reconcile_secondary_servers(cog, set(), 1100)
        assert STEAM not in third.live
        assert STEAM in second.live
        assert second.calls == []
        assert saved["vip_secondary_slot_ownership_v1"] == {}

    asyncio.run(run())


def test_weekly_award_secondary_cleanup_only_owned_slots(monkeypatch):
    monkeypatch.setattr(
        seeding.ReservedProtectionService, "is_protected",
        lambda *args: False,
    )
    monkeypatch.setattr(seeding, "seed_get_vip_membership", lambda user_id: None)
    monkeypatch.setattr(seeding, "_panel_vip_active", lambda steam: False)
    monkeypatch.setattr(seeding, "seed_set_state", lambda *args: None)

    second = Rcon([STEAM])
    third = Rcon()
    cog = SimpleNamespace(
        _servers=lambda: [
            ("second", "RU2", second), ("third", "RU3", third),
        ],
        donations=SimpleNamespace(active=lambda *args: False),
    )
    winner = {
        "steamId": STEAM, "userId": "u1", "discordId": "123",
        "active": True, "slotOwnership": "external",
    }
    state = {"winners": [winner], "cleanup": []}

    async def run():
        await seeding.SeedingCog._weekly_reconcile_secondary_awards(cog, state)
        assert winner["slotOwnershipByServer"] == {
            "second": "external", "third": "award"
        }
        assert STEAM in third.live
        state["cleanup"].append(dict(winner))
        await seeding.SeedingCog._weekly_reconcile_cleanup(cog, state)
        assert not state["cleanup"]
        assert STEAM in second.live
        assert STEAM not in third.live

    asyncio.run(run())

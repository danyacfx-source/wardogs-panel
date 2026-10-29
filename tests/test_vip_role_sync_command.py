"""Command-level regression: one failed RCON server cannot mark its VIP synced."""

import asyncio
import sqlite3
from types import SimpleNamespace

from app.bot import runner  # noqa: F401
from cogs import seeding


def test_vip_role_sync_updates_third_server_independently(monkeypatch):
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.row_factory = sqlite3.Row
    db.executescript(
        "CREATE TABLE vip_slots (id TEXT PRIMARY KEY, server_id TEXT, steam_id TEXT,"
        "display_name TEXT, expires_utc REAL, note TEXT, created_by TEXT,"
        "created_utc REAL, updated_utc REAL, synced_utc REAL, sync_state TEXT,"
        "sync_error TEXT, UNIQUE(server_id, steam_id));"
        "CREATE TABLE bindings (user_id TEXT PRIMARY KEY, discord_id TEXT);"
    )
    steam = "76561190000000001"
    db.execute("INSERT INTO bindings VALUES (?, ?)", (steam, "100"))
    for sid in ("ru1", "ru2"):
        db.execute(
            "INSERT INTO vip_slots VALUES (?, ?, ?, '', NULL, '', '', 1, 1, 1, 'synced', '')",
            (sid, sid, steam),
        )
    role = object()
    member = SimpleNamespace(id=100, display_name="Player", bot=False, roles=[role])
    guild = SimpleNamespace(members=[member], get_role=lambda _id: role)
    messages = []

    async def defer(**kwargs):
        return None

    async def send(**kwargs):
        messages.append(kwargs["embed"])

    async def allowed(_interaction):
        return True

    interaction = SimpleNamespace(
        guild=guild, user=SimpleNamespace(id=200),
        response=SimpleNamespace(defer=defer), followup=SimpleNamespace(send=send),
    )

    class FakeRcon:
        def __init__(self, fail=False):
            self.fail = fail
            self.calls = 0

        async def set_reserved(self, steam_id, desired):
            assert steam_id == steam and desired
            self.calls += 1
            if self.fail:
                raise RuntimeError("server unavailable")
            return {"active": True}

    first, second, third = FakeRcon(), FakeRcon(), FakeRcon(fail=True)
    cog = SimpleNamespace(
        _check_admin=allowed, rcon=first, rcon2=second, rcon3=third
    )
    audits = []
    monkeypatch.setattr(seeding, "get_panel_conn", lambda: db)
    monkeypatch.setattr(seeding, "seed_audit", lambda *args: audits.append(args))
    monkeypatch.setattr(seeding.config, "VIP_ROLE_ID_SEED", 1)
    monkeypatch.setattr(seeding.config, "VIP_PANEL_SERVER_IDS", ("ru1", "ru2", "ru3"))

    asyncio.run(
        seeding.SeedingCog.admin_vip_role_sync.callback(
            cog, interaction, reserved=True
        )
    )
    rows = db.execute(
        "SELECT server_id, sync_state FROM vip_slots ORDER BY server_id"
    ).fetchall()
    assert [(r["server_id"], r["sync_state"]) for r in rows] == [
        ("ru1", "synced"), ("ru2", "synced"), ("ru3", "pending")
    ]
    assert (first.calls, second.calls, third.calls) == (1, 1, 1)
    assert "серверов: **3**" in messages[0].description
    assert "Сервер 3: server unavailable" in messages[0].description
    assert audits[0][2]["created_slots"] == 1
    db.close()

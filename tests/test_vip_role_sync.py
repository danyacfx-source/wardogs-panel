"""Tests for VIP role sync across all configured servers."""

import sqlite3

from app.bot import runner  # noqa: F401 - register embedded bot imports
from cogs import seeding


SERVERS = ("ru1", "ru2", "ru3")
STEAM = "76561190000000001"


def _db():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE vip_slots ("
        "id TEXT PRIMARY KEY, server_id TEXT, steam_id TEXT, "
        "display_name TEXT, expires_utc REAL, note TEXT, created_by TEXT, "
        "created_utc REAL, updated_utc REAL, synced_utc REAL, "
        "sync_state TEXT, sync_error TEXT, UNIQUE(server_id, steam_id));"
    )
    return conn


def _existing(conn, sid, expires=None):
    conn.execute(
        "INSERT INTO vip_slots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (sid, sid, STEAM, "Player", expires, "Paid", "original",
         1, 1, 1, "synced", ""),
    )


def _ensure(conn, now=100):
    return seeding._ensure_vip_role_site_slots(
        conn, STEAM, "Player", SERVERS, now, "admin"
    )


def test_new_role_sync_creates_three_permanent_slots():
    with _db() as conn:
        assert _ensure(conn) == (["ru1", "ru2", "ru3"], set())
        rows = conn.execute(
            "SELECT server_id, expires_utc, sync_state FROM vip_slots ORDER BY server_id"
        ).fetchall()
        assert [(r["server_id"], r["expires_utc"], r["sync_state"]) for r in rows] == [
            ("ru1", None, "pending"), ("ru2", None, "pending"), ("ru3", None, "pending")
        ]


def test_existing_paid_vip_gets_missing_third_server_without_becoming_permanent():
    with _db() as conn:
        _existing(conn, "ru1", 200)
        _existing(conn, "ru2", 300)
        assert _ensure(conn) == (["ru3"], {"ru1", "ru2"})
        rows = conn.execute(
            "SELECT server_id, expires_utc, sync_state FROM vip_slots ORDER BY server_id"
        ).fetchall()
        assert [(r["server_id"], r["expires_utc"], r["sync_state"]) for r in rows] == [
            ("ru1", 200, "synced"), ("ru2", 300, "synced"), ("ru3", 300, "pending")
        ]
        assert _ensure(conn) == ([], set(SERVERS))
        assert conn.execute("SELECT count(*) FROM vip_slots").fetchone()[0] == 3


def test_expired_slot_recreated_without_touching_active_slots():
    with _db() as conn:
        _existing(conn, "ru1", 200)
        _existing(conn, "ru2", 50)
        assert _ensure(conn) == (["ru2", "ru3"], {"ru1"})
        rows = conn.execute(
            "SELECT server_id, expires_utc, sync_state FROM vip_slots ORDER BY server_id"
        ).fetchall()
        assert [(r["server_id"], r["expires_utc"], r["sync_state"]) for r in rows] == [
            ("ru1", 200, "synced"), ("ru2", 200, "pending"), ("ru3", 200, "pending")
        ]

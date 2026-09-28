import asyncio
import sqlite3

from app import config, db


def test_upgrade_from_previous_release(tmp_path, monkeypatch):
    database = tmp_path / "old.db"
    with sqlite3.connect(database) as raw:
        raw.executescript(
            """
            CREATE TABLE users (steam_id TEXT PRIMARY KEY, persona TEXT, last_seen_utc REAL);
            CREATE TABLE sessions (token TEXT PRIMARY KEY, user_id TEXT, created_utc REAL, expires_utc REAL);
            CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_utc REAL NOT NULL);
            CREATE TABLE site_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc REAL NOT NULL,
                actor_steam_id TEXT,
                server_id TEXT,
                event TEXT NOT NULL,
                detail TEXT DEFAULT ''
            );
            INSERT INTO schema_migrations VALUES (1, 0);
            INSERT INTO sessions VALUES ('legacy-plaintext-token', '76561190000000000', 0, 9999999999);
            """
        )

    monkeypatch.setattr(config, "DB_PATH", database)

    async def run():
        await db.init()
        assert await db.schema_version() == 16
        with sqlite3.connect(database) as raw:
            assert raw.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
            columns = {row[1] for row in raw.execute("PRAGMA table_info(site_audit)")}
            assert {"prev_hash", "entry_hash"} <= columns
            idem_columns = {row[1] for row in raw.execute("PRAGMA table_info(idempotency_keys)")}
            assert {"idem_key", "fingerprint", "response_json"} <= idem_columns
            note_columns = {row[1] for row in raw.execute("PRAGMA table_info(player_notes)")}
            assert {"id", "steam_id", "author_steam_id", "note", "created_utc"} <= note_columns
            clan_columns = {row[1] for row in raw.execute("PRAGMA table_info(clans)")}
            assert {"emblem_url", "cover_url"} <= clan_columns
            indexes = {row[1] for row in raw.execute("PRAGMA index_list(site_audit)")}
            assert "idx_site_audit_player_lookup" in indexes
            assert raw.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN "
                "('player_tags', 'clans', 'clan_members', 'vip_slots', 'tickets', "
                "'ticket_comments', 'managed_bans', 'player_presence', 'bot_integrations', "
                "'bot_events')"
            ).fetchone()[0] == 10

    asyncio.run(run())

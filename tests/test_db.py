import asyncio
import hashlib
import sqlite3

from app import config, db


def test_migration_and_site_audit(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")

    async def run():
        await db.init()
        assert await db.schema_version() == 16
        await db.upsert_user("76561190000000000", "Test")
        session_token = await db.create_session("76561190000000000")
        with sqlite3.connect(tmp_path / "test.db") as raw:
            stored = raw.execute("SELECT token FROM sessions").fetchone()[0]
        assert stored == hashlib.sha256(session_token.encode()).hexdigest()
        assert stored != session_token
        assert await db.get_session(session_token) == "76561190000000000"
        await db.revoke_user_sessions("76561190000000000")
        assert await db.get_session(session_token) is None
        await db.create_oauth_state("state-1", "steam")
        assert await db.consume_oauth_state("state-1", "steam") is True
        assert await db.consume_oauth_state("state-1", "steam") is False
        await db.upsert_binding("76561190000000000", "discord", "Test", ["role"], True)
        with sqlite3.connect(tmp_path / "test.db") as raw:
            raw.execute("UPDATE bindings SET roles = ? WHERE user_id = ?", ("{broken", "76561190000000000"))
            raw.commit()
        assert (await db.get_binding("76561190000000000"))["roles"] == []
        clan = await db.create_clan("Test clan", "TST", "red", "", "76561190000000000")
        await db.set_clan_member_role(clan["id"], "76561190000000001", "Лидер")
        await db.set_clan_member_role(clan["id"], "76561190000000002", "Лидер")
        detail = await db.get_clan(clan["id"])
        assert detail["leader"]["steam_id"] == "76561190000000002"
        assert {member["member_role"] for member in detail["members"]} == {"Лидер", "Участник"}
        assert await db.update_clan_media(clan["id"], "emblem", "/static/uploads/clans/test.png")
        assert (await db.get_clan(clan["id"]))["emblem_url"].endswith("test.png")
        await db.log_site_audit("76561190000000000", "ru1", "reserved.add", "76561190000000001")
        rows = await db.get_site_audit()
        assert rows[0]["event"] == "reserved.add"
        assert rows[0]["server_id"] == "ru1"
        assert (await db.verify_site_audit_chain())["ok"] is True
        await db.log_site_audit("76561190000000000", "ru1", "ban.add", "76561190000000001")
        assert (await db.verify_site_audit_chain())["checked"] == 2
        with sqlite3.connect(tmp_path / "test.db") as raw:
            raw.execute("UPDATE site_audit SET detail = ? WHERE event = ?", ("tampered", "ban.add"))
            raw.commit()
        assert (await db.verify_site_audit_chain(force=True))["ok"] is False
        claim = await db.claim_idempotency("76561190000000000", "idem-1234", "fp")
        assert claim["kind"] == "claimed"
        await db.complete_idempotency("76561190000000000", "idem-1234", 200, {"ok": True})
        replay = await db.claim_idempotency("76561190000000000", "idem-1234", "fp")
        assert replay["kind"] == "replay"
        assert replay["response"] == {"ok": True}
        assert (await db.claim_idempotency("76561190000000000", "idem-1234", "different"))["kind"] == "conflict"
        await db.claim_idempotency("76561190000000000", "idem-large", "fp-large")
        await db.complete_idempotency("76561190000000000", "idem-large", 200, {"data": "x" * (256 * 1024)})
        large_replay = await db.claim_idempotency("76561190000000000", "idem-large", "fp-large")
        assert large_replay["response"]["response_truncated"] is True

    asyncio.run(run())


def test_postgres_adapter_translates_sqlite_placeholders():
    class Transaction:
        async def start(self):
            pass

        async def commit(self):
            pass

        async def rollback(self):
            pass

    class Connection:
        def __init__(self):
            self.queries = []

        def transaction(self):
            return Transaction()

        async def fetch(self, query, *params):
            self.queries.append(("fetch", query, params))
            return []

        async def execute(self, query, *params):
            self.queries.append(("execute", query, params))

    async def run():
        connection = Connection()
        database = db._PostgresDatabase(connection)
        await database.execute("BEGIN IMMEDIATE")
        await database.execute("SELECT 1 WHERE value = ? AND other = ?", (1, 2))
        await database.commit()
        assert connection.queries == [
            ("fetch", "SELECT 1 WHERE value = $1 AND other = $2", (1, 2)),
        ]

    asyncio.run(run())


def test_player_profile_aggregation(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "players.db")

    async def run():
        await db.init()
        await db.record_monitoring(
            "ru1", 1, 100,
            [{"steamId": "76561190000000001", "name": "Игрок", "kills": 4, "deaths": 2, "cash": 10, "pingMs": 40, "faction": "LONESTAR"}],
        )
        profile = await db.get_player_profile("76561190000000001")
        assert profile["name"] == "Игрок"
        assert profile["kills"] == 4
        assert profile["deaths"] == 2
        assert profile["server_count"] == 1
        assert await db.search_players("76561190000000001", limit=5)

    asyncio.run(run())


def test_player_presence_tracks_time_and_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "presence.db")
    clock = {"now": 1_000.0}
    monkeypatch.setattr(db, "_now", lambda: clock["now"])
    player = {
        "steamId": "76561190000000001",
        "name": "Игрок",
        "kills": 4,
        "deaths": 2,
    }

    async def run():
        await db.init()
        await db.record_monitoring("ru1", 1, 100, [player])
        first = await db.get_player_presence(player["steamId"])
        assert first["total_seconds"] == 0
        assert first["session_count"] == 1
        assert first["current_session_seconds"] == 0

        clock["now"] = 1_060.0
        await db.record_monitoring("ru1", 1, 100, [player])
        continued = await db.get_player_presence(player["steamId"])
        assert continued["total_seconds"] == 60
        assert continued["session_count"] == 1
        assert continued["current_session_seconds"] == 60

        clock["now"] = 1_120.0
        await db.record_monitoring("ru1", 0, 100, [])
        offline = await db.get_player_presence(player["steamId"])
        assert offline["current_session_seconds"] == 0

        clock["now"] = 1_180.0
        await db.record_monitoring("ru1", 1, 100, [player])
        returned = await db.get_player_presence(player["steamId"])
        assert returned["total_seconds"] == 60
        assert returned["session_count"] == 2
        assert returned["servers"][0]["online"] is True

    asyncio.run(run())


def test_player_leaderboard_supports_multiple_sorts(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "leaderboard.db")
    clock = {"now": 2_000.0}
    monkeypatch.setattr(db, "_now", lambda: clock["now"])
    veteran = {"steamId": "76561190000000001", "name": "Ветеран", "kills": 4, "deaths": 2}
    fragger = {"steamId": "76561190000000002", "name": "Фраггер", "kills": 10, "deaths": 5}

    async def run():
        await db.init()
        await db.record_monitoring("ru1", 2, 100, [veteran, fragger])
        clock["now"] = 2_060.0
        await db.record_monitoring("ru1", 1, 100, [veteran])

        by_kills = await db.get_player_leaderboard(10, "kills")
        assert [item["steam_id"] for item in by_kills] == [
            fragger["steamId"],
            veteran["steamId"],
        ]
        by_playtime = await db.get_player_leaderboard(10, "playtime")
        assert by_playtime[0]["steam_id"] == veteran["steamId"]
        assert by_playtime[0]["total_seconds"] == 60
        assert by_playtime[0]["session_count"] == 1
        assert by_playtime[0]["online"] is True
        assert by_playtime[0]["server_count"] == 1

    asyncio.run(run())


def test_player_notes_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "notes.db")

    async def run():
        await db.init()
        await db.upsert_user("76561190000000000", "Администратор")
        note = await db.add_player_note(
            "76561190000000001",
            "76561190000000000",
            "Проверить повторное нарушение",
        )
        notes = await db.list_player_notes("76561190000000001")
        assert len(notes) == 1
        assert notes[0]["id"] == note["id"]
        assert notes[0]["author_name"] == "Администратор"
        assert notes[0]["note"] == "Проверить повторное нарушение"
        assert await db.delete_player_note(note["id"], "76561190000000002") is False
        assert await db.delete_player_note(note["id"], "76561190000000001") is True
        assert await db.list_player_notes("76561190000000001") == []

    asyncio.run(run())


def test_player_punishment_history_is_scoped(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "punishments.db")

    async def run():
        await db.init()
        actor = "76561190000000000"
        target = "76561190000000001"
        other = "76561190000000002"
        await db.upsert_user(actor, "Модератор")
        await db.log_site_audit(actor, "ru1", "rcon.kick", target)
        await db.log_site_audit(actor, "ru1", "ban.add", target)
        await db.log_site_audit(actor, "ru1", "ban.add", other)
        await db.log_site_audit(actor, "ru1", "broadcast", target)
        rows = await db.list_player_punishments(target)
        assert [row["event"] for row in rows] == ["ban.add", "rcon.kick"]
        assert all(row["actor_name"] == "Модератор" for row in rows)
        assert all(row["server_id"] == "ru1" for row in rows)

    asyncio.run(run())


def test_player_tags_and_clans_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "community.db")

    async def run():
        await db.init()
        actor = "76561190000000000"
        player = "76561190000000001"
        await db.upsert_user(actor, "Куратор")

        tag = await db.add_player_tag(player, "Под наблюдением", "red", actor)
        assert tag["created"] is True
        duplicate = await db.add_player_tag(player, "Под наблюдением", "blue", actor)
        assert duplicate["created"] is False
        tags = await db.list_player_tags(player)
        assert len(tags) == 1
        assert tags[0]["color"] == "red"
        assert await db.delete_player_tag(tag["id"], player) is True

        clan = await db.create_clan("Рубеж", "RUBEZH", "orange", "Основной клан", actor)
        assert clan["created"] is True
        assert (await db.create_clan("Другой", "RUBEZH", "red", "", actor))["created"] is False
        assert await db.set_player_clan(player, clan["id"], "Лидер") is True
        membership = await db.get_player_clan(player)
        assert membership["tag"] == "RUBEZH"
        assert membership["member_role"] == "Лидер"
        assert (await db.list_clans())[0]["member_count"] == 1
        clan_detail = await db.get_clan(clan["id"])
        assert clan_detail["leader"]["steam_id"] == player
        assert clan_detail["members"][0]["member_role"] == "Лидер"
        assert await db.update_clan(clan["id"], "Новый Рубеж", "RBZ", "blue", "Обновлено") is True
        assert (await db.get_player_clan(player))["tag"] == "RBZ"
        assert await db.remove_player_clan(player) is True
        assert await db.delete_clan(clan["id"]) is True

    asyncio.run(run())


def test_discord_bindings_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "bindings.db")

    async def run():
        await db.init()
        steam_id = "76561190000000001"
        await db.upsert_user(steam_id, "Игрок")
        await db.upsert_binding(steam_id, "123456", "Discord", ["role-1"], True)
        rows = await db.list_bindings()
        assert rows[0]["steam_id"] == steam_id
        assert rows[0]["role_ids"] == ["role-1"]
        assert rows[0]["guild_member"] is True

    asyncio.run(run())


def test_vip_slots_lifecycle_and_expiry(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "vip.db")

    async def run():
        await db.init()
        now = 2_000_000_000.0
        item = await db.save_vip_slot(
            "ru1",
            "76561190000000001",
            "VIP игрок",
            now + 3600,
            "Тест",
            "76561190000000000",
        )
        assert item["sync_state"] == "pending"
        assert (await db.list_player_vip_slots("76561190000000001"))[0]["id"] == item["id"]
        await db.mark_vip_sync(item["id"], "synced")
        assert (await db.get_vip_slot(item["id"], "ru1"))["sync_state"] == "synced"
        updated = await db.save_vip_slot(
            "ru1",
            "76561190000000001",
            "VIP игрок 2",
            now - 1,
            "Продление",
            "76561190000000000",
        )
        assert updated["id"] == item["id"]
        expired = await db.list_expired_vip_slots(now)
        assert [row["id"] for row in expired] == [item["id"]]
        await db.mark_vip_sync(item["id"], "expired")
        assert await db.list_expired_vip_slots(now) == []
        assert await db.delete_vip_slot(item["id"], "ru1") is True
        assert await db.get_vip_slot(item["id"], "ru1") is None

    asyncio.run(run())


def test_tickets_lifecycle_and_comments(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "tickets.db")

    async def run():
        await db.init()
        author = "76561190000000000"
        player = "76561190000000001"
        await db.upsert_user(author, "Администратор")
        ticket = await db.create_ticket(
            "Проверить жалобу",
            "Описание обращения",
            "high",
            player,
            author,
        )
        assert ticket["status"] == "open"
        assert ticket["created_by_name"] == "Администратор"
        comment = await db.add_ticket_comment(ticket["id"], author, "Взято в работу")
        assert comment["body"] == "Взято в работу"
        detail = await db.get_ticket(ticket["id"])
        assert detail["comment_count"] == 1
        assert detail["comments"][0]["author_name"] == "Администратор"
        assert await db.update_ticket(
            ticket["id"],
            ticket["title"],
            ticket["description"],
            "resolved",
            "urgent",
            player,
            author,
        ) is True
        updated = await db.get_ticket(ticket["id"])
        assert updated["status"] == "resolved"
        assert updated["priority"] == "urgent"
        assert updated["closed_utc"] is not None
        assert (await db.list_tickets())[0]["comment_count"] == 1
        assert await db.delete_ticket(ticket["id"]) is True
        assert await db.get_ticket(ticket["id"]) is None

    asyncio.run(run())


def test_managed_bans_lifecycle_and_expiry(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "managed-bans.db")

    async def run():
        await db.init()
        now = 2_000_000_000.0
        item = await db.save_managed_ban(
            "ru1",
            "76561190000000001",
            "Игрок",
            "Нарушение",
            now - 1,
            "76561190000000000",
        )
        assert item["sync_state"] == "active"
        assert (await db.list_managed_bans("ru1"))[0]["id"] == item["id"]
        expired = await db.list_expired_managed_bans(now)
        assert [row["id"] for row in expired] == [item["id"]]
        await db.mark_managed_ban(item["id"], "expired")
        assert await db.list_expired_managed_bans(now) == []
        await db.delete_managed_ban("ru1", "76561190000000001")
        assert await db.list_managed_bans("ru1") == []

    asyncio.run(run())


def test_bot_registry_and_idempotent_events(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "bots.db")

    async def run():
        await db.init()
        item = await db.create_bot_integration(
            "moderation-bot", "Модератор", "token-hash",
            ["heartbeat", "events.write"], "1234567890",
        )
        assert item["enabled"] is True
        assert item["scopes"] == ["heartbeat", "events.write"]
        updated = await db.update_bot_integration(
            item["bot_id"], status="online", version="2.0", last_seen_utc=123.0,
            metadata={"shards": 2},
        )
        assert updated["metadata"] == {"shards": 2}
        assert await db.append_bot_event(item["bot_id"], "evt-1", "member.updated", {"id": "42"}) is True
        assert await db.append_bot_event(item["bot_id"], "evt-1", "member.updated", {"id": "42"}) is False
        events = await db.list_bot_events(item["bot_id"])
        assert len(events) == 1
        assert events[0]["payload"] == {"id": "42"}
        await db.delete_bot_integration(item["bot_id"])
        assert await db.get_bot_integration(item["bot_id"]) is None
        assert await db.list_bot_events(item["bot_id"]) == []

    asyncio.run(run())

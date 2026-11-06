"""VIP клана: места нельзя выдать сверх пакета, чужим и без срока; выход из клана снимает место."""
import asyncio
import time

import pytest

from app import config, db

LEADER = "76561190000000001"
DEPUTY = "76561190000000002"
MEMBERS = [f"7656119000000010{i}" for i in range(12)]


@pytest.fixture
def clan_db(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "clan_vip.db")

    async def setup():
        await db.init()
        clan = await db.create_clan("Тестовый клан", "TST", "red", "", LEADER)
        await db.set_clan_member_role(clan["id"], LEADER, "Лидер")
        await db.set_clan_member_role(clan["id"], DEPUTY, "Заместитель")
        for steam in MEMBERS:
            await db.set_clan_member_role(clan["id"], steam, "Участник")
        return clan["id"]

    return asyncio.run(setup())


def _run(coro):
    return asyncio.run(coro)


def test_grants_never_exceed_purchased_seats(clan_db):
    async def scenario():
        await db.credit_clan_pool(clan_db, 10)
        results = [await db.add_clan_grant(clan_db, steam, LEADER) for steam in MEMBERS]
        return results, await db.clan_pool_usage(clan_db)

    results, used = _run(scenario())
    assert results[:10] == ["ok"] * 10
    assert results[10:] == ["full", "full"]
    assert used == 10


def test_grant_refused_for_non_member_duplicate_and_without_pool(clan_db):
    async def scenario():
        before = await db.add_clan_grant(clan_db, MEMBERS[0], LEADER)
        await db.credit_clan_pool(clan_db, 5)
        not_member = await db.add_clan_grant(clan_db, "76561190000000999", LEADER)
        ok = await db.add_clan_grant(clan_db, MEMBERS[0], LEADER)
        duplicate = await db.add_clan_grant(clan_db, MEMBERS[0], LEADER)
        return before, not_member, ok, duplicate

    before, not_member, ok, duplicate = _run(scenario())
    assert before == "no_pool"
    assert not_member == "not_member"
    assert ok == "ok"
    assert duplicate == "exists"


def test_removed_member_frees_seat_and_is_listed_for_revoke(clan_db):
    async def scenario():
        await db.credit_clan_pool(clan_db, 1)
        await db.add_clan_grant(clan_db, MEMBERS[0], LEADER)
        assert await db.clan_pool_usage(clan_db) == 1
        await db.remove_clan_member(clan_db, MEMBERS[0])
        usage_after = await db.clan_pool_usage(clan_db)
        orphans = await db.list_orphan_clan_grants()
        return usage_after, orphans

    usage_after, orphans = _run(scenario())
    assert usage_after == 0
    assert {"clan_id": clan_db, "steam_id": MEMBERS[0]} in orphans


def test_renewal_adds_month_to_remaining_term(clan_db):
    async def scenario():
        first = await db.credit_clan_pool(clan_db, 10)
        second = await db.credit_clan_pool(clan_db, 10)
        return first, second

    first, second = _run(scenario())
    month = 30 * 86400
    assert second["seats"] == 20
    assert abs(second["expires_utc"] - (first["expires_utc"] + month)) < 5


def test_renewal_after_expiry_starts_from_now(clan_db):
    async def scenario():
        await db.credit_clan_pool(clan_db, 1)
        await db.credit_clan_pool(clan_db, 1)
        pool = await db.get_clan_pool(clan_db)
        return pool

    pool = _run(scenario())
    assert pool["expires_utc"] > time.time()


def test_paid_order_is_credited_once(clan_db):
    order = {"id": "WDC-TEST1", "clan_id": clan_db, "buyer_steam_id": LEADER,
             "product_id": "clan_10", "pack_seats": 10, "amount": 4500}

    async def scenario():
        await db.create_clan_order(order)
        first = await db.mark_clan_order_paid(order["id"])
        second = await db.mark_clan_order_paid(order["id"])
        await db.reopen_clan_order(order["id"])
        third = await db.mark_clan_order_paid(order["id"])
        return first, second, third

    assert _run(scenario()) == (True, False, True)


def test_personal_vip_is_not_overwritten_by_clan_seat(clan_db, monkeypatch):
    from app import api

    monkeypatch.setattr(api, "POOL", {"ru1": object(), "ru2": object()})

    async def fake_sync(item, add=True, touch_role=True):
        return True, ""

    monkeypatch.setattr(api, "_sync_vip_record", fake_sync)

    async def scenario():
        await db.save_vip_slot("ru1", MEMBERS[0], "p", None, "личный", "admin")
        results = await api._grant_pool_slot(MEMBERS[0], time.time() + 1000, clan_db, LEADER)
        return results, await api._slot_on("ru1", MEMBERS[0]), await api._slot_on("ru2", MEMBERS[0])

    results, ru1, ru2 = asyncio.run(scenario())
    assert ru1["note"] == "личный"
    assert ru2["note"] == f"клан:{clan_db}"
    assert [r["server"] for r in results if r.get("skipped")] == ["ru1"]

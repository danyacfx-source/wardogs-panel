"""Состояние кланов хранится в БД: записи попадают в БД, файл clan_state.json переносится однократно."""
import asyncio
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "app" / "bot"))

from cogs import clans_store  # noqa: E402


@pytest.fixture
def db(monkeypatch, tmp_path):
    storage: dict = {}
    monkeypatch.setattr(clans_store, "seed_get_state_strict", lambda key, default=None: storage.get(key, default))

    def set_state(key, value):
        storage[key] = json.loads(json.dumps(value))

    monkeypatch.setattr(clans_store, "seed_set_state", set_state)
    monkeypatch.setattr(clans_store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(clans_store, "STATE_FILE", tmp_path / "clan_state.json")
    return storage


def test_first_load_creates_state_in_db(db):
    store = clans_store.ClanStore()
    asyncio.run(store.load())
    assert clans_store.CLAN_STATE_KEY in db
    assert db[clans_store.CLAN_STATE_KEY]["clans"] == {}


def test_update_is_saved_to_db(db):
    store = clans_store.ClanStore()
    asyncio.run(store.load())

    def add(state):
        state["clans"]["c1"] = {"id": "c1", "name": "Тест", "roleIds": {}}

    asyncio.run(store.update(add))
    assert "c1" in db[clans_store.CLAN_STATE_KEY]["clans"]


def test_db_state_is_loaded_on_restart(db):
    first = clans_store.ClanStore()
    asyncio.run(first.load())
    asyncio.run(first.update(lambda s: s["clans"].update({"c2": {"id": "c2", "name": "Два", "roleIds": {}}})))

    restarted = clans_store.ClanStore()
    asyncio.run(restarted.load())
    assert "c2" in restarted.state["clans"]


def test_legacy_file_is_migrated_once(db, tmp_path):
    legacy = clans_store.blank_state()
    legacy["clans"]["old"] = {"id": "old", "name": "Старый", "roleIds": {}}
    (tmp_path / "clan_state.json").write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    store = clans_store.ClanStore()
    asyncio.run(store.load())

    assert "old" in db[clans_store.CLAN_STATE_KEY]["clans"]
    assert "old" in store.state["clans"]

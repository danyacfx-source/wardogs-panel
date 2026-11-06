"""Литеральный % в SQL ломает PostgreSQL (psycopg): шаблоны LIKE передаются параметрами."""
import asyncio
from contextlib import asynccontextmanager

from app import seed_store
from app.db import reports


class _SyncCursor:
    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _AsyncCursor:
    async def fetchone(self):
        return (0,)

    async def fetchall(self):
        return []


class _Recorder:
    """Заглушка синхронного соединения: запоминает SQL и параметры."""

    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return _SyncCursor()

    def close(self):
        pass


class _AsyncRecorder:
    """Заглушка асинхронного соединения (как в app/db/core.py): запоминает SQL и параметры."""

    def __init__(self):
        self.calls = []

    async def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return _AsyncCursor()


def test_player_summary_sql_has_no_literal_percent(monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(seed_store, "_seed_conn", lambda: recorder)

    seed_store.get_player_summary("76561190000000001", "123456789012345678")

    assert recorder.calls
    for sql, _params in recorder.calls:
        assert "%" not in sql, sql
    vip_params = [params for sql, params in recorder.calls if "donations_vip" in sql]
    assert vip_params and "clan_%" in vip_params[0]


def test_staff_audit_seed_filter_uses_parameter(monkeypatch):
    recorder = _AsyncRecorder()

    @asynccontextmanager
    async def fake_conn():
        yield recorder

    monkeypatch.setattr(reports, "_conn", fake_conn)

    asyncio.run(reports.search_site_audit(staff_only=True))

    assert recorder.calls
    for sql, _params in recorder.calls:
        assert "%" not in sql, sql
    assert any(params and "%:seed:%" in params for _sql, params in recorder.calls)

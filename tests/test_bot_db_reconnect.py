"""Regression tests for stale PostgreSQL connections in the embedded bot."""

import sqlite3
import sys
from types import ModuleType, SimpleNamespace

import pytest

from app.bot import db_backend


@pytest.fixture
def fake_postgres(monkeypatch):
    """Use a fake psycopg connection; no external database is required."""
    db_backend.close_all()
    connections = []

    class PgError(Exception):
        pass

    class PgIntegrityError(PgError):
        pass

    class PgUniqueViolation(PgIntegrityError):
        pass

    class PgOperationalError(PgError):
        pass

    class DriverConnection:
        def __init__(self):
            self.closed = False
            self.disconnect_on_next = False
            self.calls = []
            self.row_factory = None

        def execute(self, sql, params=None):
            if self.closed:
                raise PgOperationalError("the connection is closed")
            if self.disconnect_on_next:
                self.disconnect_on_next = False
                self.closed = True
                raise PgOperationalError("the connection is closed")
            self.calls.append((sql, params))
            return self

        def cursor(self):
            driver = self

            class Cursor:
                def __enter__(self):
                    if driver.closed:
                        raise PgOperationalError("the connection is closed")
                    return self

                def __exit__(self, *_):
                    return False

                def executemany(self, sql, seq):
                    if driver.closed:
                        raise PgOperationalError("the connection is closed")
                    driver.calls.append((sql, list(seq)))

            return Cursor()

        def close(self):
            self.closed = True

    def connect(dsn, *, autocommit):
        assert dsn == "postgresql://example"
        assert autocommit is True
        conn = DriverConnection()
        connections.append(conn)
        return conn

    psycopg = ModuleType("psycopg")
    psycopg.connect = connect
    psycopg.errors = SimpleNamespace(
        Error=PgError,
        OperationalError=PgOperationalError,
        IntegrityError=PgIntegrityError,
        UniqueViolation=PgUniqueViolation,
    )
    rows = ModuleType("psycopg.rows")
    rows.dict_row = object()
    monkeypatch.setitem(sys.modules, "psycopg", psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.rows", rows)
    monkeypatch.setenv("BOT_DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("WARDOGS_PANEL_DATABASE_URL", "postgresql://example")
    yield connections
    db_backend.close_all()


def test_closed_bot_connection_reopens_and_restores_schema(fake_postgres):
    conn = db_backend.get_bot_conn()
    first = conn._conn
    first.close()

    assert db_backend.get_bot_conn() is conn
    conn.execute("SELECT 1")

    assert conn._conn is not first
    assert len(fake_postgres) == 2
    assert conn._conn.calls[0][0] == 'CREATE SCHEMA IF NOT EXISTS "bot"'
    assert conn._conn.calls[1][0] == 'SET search_path TO "bot", public'
    assert conn._conn.calls[2][0] == "SELECT 1"


def test_disconnected_select_retries_once(fake_postgres):
    conn = db_backend.get_bot_conn()
    conn._conn.disconnect_on_next = True

    conn.execute("SELECT value FROM seed_system_state WHERE key = ?", ("x",))

    assert len(fake_postgres) == 2
    assert fake_postgres[-1].calls[-1] == (
        "SELECT value FROM seed_system_state WHERE key = %s", ("x",)
    )


def test_disconnected_write_is_not_replayed(fake_postgres):
    conn = db_backend.get_bot_conn()
    conn._conn.disconnect_on_next = True

    with pytest.raises(sqlite3.OperationalError, match="connection is closed"):
        conn.execute(
            "INSERT INTO seed_system_state (key, value) VALUES (?, ?)", ("x", "y")
        )

    # The outcome of a failed write is unknown: do not execute it again.
    assert len(fake_postgres) == 1
    conn.execute("SELECT 1")
    assert len(fake_postgres) == 2


def test_panel_connection_and_executemany_recover_when_closed(fake_postgres):
    panel = db_backend.get_panel_conn()
    panel._conn.close()
    panel.execute("SELECT 1")

    bot = db_backend.get_bot_conn()
    bot._conn.close()
    bot.executemany("INSERT INTO seed_system_state VALUES (?, ?)", [("k", "v")])

    assert len(fake_postgres) == 4
    assert fake_postgres[-1].calls[-1] == (
        "INSERT INTO seed_system_state VALUES (%s, %s)", [("k", "v")]
    )

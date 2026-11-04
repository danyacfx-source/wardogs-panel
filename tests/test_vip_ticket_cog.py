"""Личный тикет VIP в боте: создание канала, защита от дублей и отказов Discord."""
import asyncio
import pathlib
import sqlite3
import sys

import pytest

BOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "app" / "bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

discord = pytest.importorskip("discord")

from cogs import tickets  # noqa: E402


class _Channel:
    id = 900

    def __init__(self, send_error=None):
        self.sent = []
        self.deleted = False
        self.send_error = send_error

    async def send(self, **kwargs):
        if self.send_error:
            raise self.send_error
        self.sent.append(kwargs)

    async def delete(self):
        self.deleted = True


class _Member:
    id = 555
    name = "bear"
    mention = "<@555>"

    def __str__(self):
        return "bear"


class _Guild:
    default_role = object()

    def __init__(self, member=True, channel=None, category=True):
        self.member = _Member() if member else None
        self.channel = channel or _Channel()
        self.category = object() if category else None
        self.created = []

    def get_member(self, discord_id):
        return self.member

    async def fetch_member(self, discord_id):
        response = type("Response", (), {"status": 404, "reason": "Not Found"})()
        raise discord.NotFound(response, "Unknown Member")

    def get_channel(self, channel_id):
        return self.category

    def get_role(self, role_id):
        return None

    async def create_text_channel(self, **kwargs):
        self.created.append(kwargs)
        return self.channel


@pytest.fixture
def database(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE tickets (id INTEGER PRIMARY KEY, user_id INTEGER, channel_id INTEGER, ticket_type TEXT, "
        "status TEXT, created_at TEXT, closed_at TEXT, closed_by INTEGER, transcript_saved INTEGER DEFAULT 0, site_ticket_id TEXT)"
    )
    monkeypatch.setattr(tickets, "get_conn", lambda: connection)

    async def no_site(row, channel):
        return None

    monkeypatch.setattr(tickets, "_ensure_site_ticket", no_site)
    monkeypatch.setattr(tickets.TicketCloseView, "__init__", lambda self: None)
    monkeypatch.setattr(tickets.config, "TICKET_STAFF_ROLES", [])
    return connection


def _open(guild, tier=2):
    bot = type("Bot", (), {"get_guild": lambda self, guild_id: guild})()
    cog = tickets.TicketCog.__new__(tickets.TicketCog)
    cog.bot = bot
    tickets._ticket_locks.clear()
    return asyncio.run(cog.panel_open_vip_ticket({"discord_id": "555", "tier": tier}))


def test_creates_private_channel_and_record(database):
    guild = _Guild()
    assert _open(guild, 3) == {"ok": True, "created": True, "channel_id": "900"}
    created = guild.created[0]
    assert created["name"] == "vip-bear" and "VIP Tier 3" in created["topic"]
    assert created["overwrites"][guild.default_role].view_channel is False
    row = database.execute("SELECT user_id, ticket_type, status FROM tickets").fetchone()
    assert tuple(row) == (555, "vip_personal", "open")
    assert "Tier 3" in guild.channel.sent[0]["embed"].title


def test_second_call_does_not_create_another_channel(database):
    guild = _Guild()
    _open(guild)
    assert _open(guild) == {"ok": True, "created": False, "channel_id": "900"}
    assert len(guild.created) == 1


def test_regular_open_ticket_does_not_block_personal_one(database):
    database.execute("INSERT INTO tickets (user_id, channel_id, ticket_type, status) VALUES (555, 1, 'support', 'open')")
    assert _open(_Guild())["created"] is True


def test_closed_ticket_allows_a_new_one(database):
    database.execute("INSERT INTO tickets (user_id, channel_id, ticket_type, status) VALUES (555, 1, 'vip_personal', 'closed')")
    assert _open(_Guild())["created"] is True


def test_member_not_in_guild(database):
    result = _open(_Guild(member=False))
    assert result == {"ok": False, "error": "Игрока нет на Discord-сервере"}


def test_missing_category(database):
    result = _open(_Guild(category=False))
    assert result == {"ok": False, "error": "Категория тикетов не настроена"}


def test_failed_first_message_removes_orphan_channel(database):
    channel = _Channel(send_error=RuntimeError("Discord упал"))
    result = _open(_Guild(channel=channel))
    assert result["ok"] is False
    assert channel.deleted is True
    assert database.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0


class _HistoryChannel(_Channel):
    name = "vip-bear"
    mention = "#vip-bear"

    def __init__(self, messages=()):
        super().__init__()
        self.messages = list(messages)
        self.history_limits = []
        self.permissions = []

    def history(self, limit=100, oldest_first=False):
        self.history_limits.append(limit)
        messages = self.messages if limit is None else self.messages[:limit]

        async def generate():
            for message in messages:
                yield message

        return generate()

    async def set_permissions(self, target, **kwargs):
        self.permissions.append((target, kwargs))


class _CloseGuild(_Guild):
    def __init__(self, channel):
        super().__init__(channel=channel)

    def get_channel(self, channel_id):
        return self.channel if channel_id == self.channel.id else None


def _cog(guild):
    bot = type("Bot", (), {"get_guild": lambda self, guild_id: guild, "user": type("U", (), {"id": 1})()})()
    cog = tickets.TicketCog.__new__(tickets.TicketCog)
    cog.bot = bot
    tickets._channel_locks.clear()
    tickets._transcript_locks.clear()
    return cog


def _insert_open(database, channel_id=900):
    database.execute(
        "INSERT INTO tickets (user_id, channel_id, ticket_type, status) VALUES (555, ?, 'vip_personal', 'open')",
        (channel_id,),
    )


def _save_ok(database, monkeypatch):
    calls = []

    async def sync(channel, row=None, by_id=None):
        calls.append(channel.id)
        database.execute("UPDATE tickets SET transcript_saved = 1 WHERE channel_id = ?", (channel.id,))

    monkeypatch.setattr(tickets, "_sync_transcript", sync)
    monkeypatch.setattr(tickets.TicketClosedView, "__init__", lambda self: None)
    return calls


def test_transcript_reads_the_whole_channel():
    moment = type("Moment", (), {"strftime": lambda self, fmt: "01.01.2027"})()
    messages = [
        type("Message", (), {"created_at": moment, "content": f"m{i}", "embeds": [], "attachments": [], "author": "bear"})()
        for i in range(250)
    ]
    channel = _HistoryChannel(messages)
    text = asyncio.run(tickets._build_transcript(channel))
    assert channel.history_limits == [None]
    assert text.count("\n") == 249 and "m249" in text


def test_close_saves_transcript_then_closes(database, monkeypatch):
    calls = _save_ok(database, monkeypatch)
    _insert_open(database)
    channel = _HistoryChannel()
    guild = _CloseGuild(channel)
    result = asyncio.run(_cog(guild).panel_close_vip_ticket({"channel_id": "900"}))
    assert result == {"ok": True, "closed": True}
    assert calls == [900]
    row = database.execute("SELECT status, closed_by FROM tickets").fetchone()
    assert tuple(row) == ("closed", 1)
    assert channel.permissions[-1] == (guild.default_role, {"view_channel": False})
    assert "автоматически" in channel.sent[0]["embed"].title


def test_ticket_stays_open_when_transcript_fails(database, monkeypatch):
    async def failing(channel, row=None, by_id=None):
        return None

    monkeypatch.setattr(tickets, "_sync_transcript", failing)
    _insert_open(database)
    channel = _HistoryChannel()
    result = asyncio.run(_cog(_CloseGuild(channel)).panel_close_vip_ticket({"channel_id": "900"}))
    assert result == {"ok": False, "error": "Транскрипт не сохранён"}
    assert database.execute("SELECT status FROM tickets").fetchone()[0] == "open"
    assert channel.permissions == [] and channel.sent == []


def test_close_twice_is_harmless(database, monkeypatch):
    calls = _save_ok(database, monkeypatch)
    _insert_open(database)
    channel = _HistoryChannel()
    guild = _CloseGuild(channel)
    cog = _cog(guild)
    asyncio.run(cog.panel_close_vip_ticket({"channel_id": "900"}))
    assert asyncio.run(cog.panel_close_vip_ticket({"channel_id": "900"})) == {"ok": True, "closed": False}
    assert len(calls) == 1 and len(channel.sent) == 1


def test_deleted_channel_just_clears_record(database):
    _insert_open(database, channel_id=123)
    result = asyncio.run(_cog(_CloseGuild(_HistoryChannel())).panel_close_vip_ticket({"channel_id": "123"}))
    assert result == {"ok": True, "closed": True}
    assert database.execute("SELECT status FROM tickets").fetchone()[0] == "closed"


class _Donations:
    def __init__(self, vip, reachable=True):
        self.state = {"vip": vip}
        self.reachable = reachable

    def can_remove(self):
        return self.reachable

    def active(self, discord_id):
        return discord_id in self.state["vip"]


def _listing(database, seeding_cog):
    _insert_open(database)
    bot = type("Bot", (), {"get_cog": lambda self, name: seeding_cog})()
    cog = tickets.TicketCog.__new__(tickets.TicketCog)
    cog.bot = bot
    return cog.panel_list_vip_tickets()


def test_list_reports_paid_tier(database):
    seeding_cog = type("Seeding", (), {"donations": _Donations({"555": {"tier": 3}})})()
    assert _listing(database, seeding_cog) == [
        {"discord_id": "555", "channel_id": "900", "paid_tier": 3, "paid_known": True},
    ]


def test_list_is_unknown_when_payment_log_is_unreachable(database):
    seeding_cog = type("Seeding", (), {"donations": _Donations({}, reachable=False)})()
    assert _listing(database, seeding_cog)[0]["paid_known"] is False


def test_list_is_unknown_without_seeding_cog(database):
    assert _listing(database, None)[0]["paid_known"] is False

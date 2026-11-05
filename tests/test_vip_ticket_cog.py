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


class _TicketMessage:
    def __init__(self, error=None):
        self.edits = []
        self.error = error

    async def edit(self, **kwargs):
        if self.error:
            raise self.error
        self.edits.append(kwargs)


def _not_found():
    response = type("Response", (), {"status": 404, "reason": "Not Found"})()
    return discord.NotFound(response, "Unknown Message")


class _Interaction:
    def __init__(self, guild):
        self.guild = guild
        self.channel = guild.channel
        self.user = type("User", (), {"id": 7, "mention": "<@7>", "__str__": lambda self: "mod"})()
        # Подтверждение эфемерное: Discord не даёт менять его обычным запросом.
        self.message = _TicketMessage(error=_not_found())
        self.original_edits = []
        self.response = type("Response", (), {"defer": self._defer})()

    async def _defer(self, **kwargs):
        pass

    async def edit_original_response(self, **kwargs):
        self.original_edits.append(kwargs)


def _confirm_close(guild, ticket_message):
    interaction = _Interaction(guild)

    async def run():
        view = tickets.TicketCloseConfirmView(ticket_message)
        await view.confirm.callback(interaction)

    tickets._channel_locks.clear()
    asyncio.run(run())
    return interaction


class _PanelDb:
    def __init__(self, status="open"):
        self.ticket = {
            "title": "Тикет Discord", "description": "текст", "status": status,
            "priority": "normal", "player_steam_id": None, "assigned_to": None,
        }
        self.updates = []

    async def get_ticket(self, site_id):
        return self.ticket

    async def update_ticket(self, *args):
        self.updates.append(args)


def _button_setup(database, monkeypatch, panel=None):
    _save_ok(database, monkeypatch)
    monkeypatch.setattr(tickets.config, "TICKET_LOG_CHANNEL", 0)
    monkeypatch.setattr(tickets, "_panel_db", lambda: panel or _PanelDb())
    _insert_open(database)
    return _CloseGuild(_HistoryChannel())


def test_close_button_posts_public_message_with_buttons_at_the_bottom(database, monkeypatch):
    guild = _button_setup(database, monkeypatch)
    message = _TicketMessage()
    interaction = _confirm_close(guild, message)
    assert database.execute("SELECT status, closed_by FROM tickets").fetchone()[0] == "closed"
    sent = guild.channel.sent[0]
    assert sent["embed"].title == "Тикет закрыт" and "ephemeral" not in sent
    assert isinstance(sent["view"], tickets.TicketClosedView)
    assert message.edits == [{"view": None}]
    assert interaction.original_edits == [{"content": "Тикет закрыт.", "view": None}]


def test_close_button_works_when_first_message_is_gone(database, monkeypatch):
    guild = _button_setup(database, monkeypatch)
    interaction = _confirm_close(guild, _TicketMessage(error=_not_found()))
    assert guild.channel.sent[0]["embed"].title == "Тикет закрыт"
    assert interaction.original_edits


def test_double_click_posts_buttons_once(database, monkeypatch):
    guild = _button_setup(database, monkeypatch)
    interaction = _Interaction(guild)

    async def run():
        view = tickets.TicketCloseConfirmView(_TicketMessage())
        await view.confirm.callback(interaction)
        await view.confirm.callback(interaction)

    tickets._channel_locks.clear()
    asyncio.run(run())
    assert len(guild.channel.sent) == 1


def test_close_button_closes_panel_ticket(database, monkeypatch):
    panel = _PanelDb()
    guild = _button_setup(database, monkeypatch, panel)
    database.execute("UPDATE tickets SET site_ticket_id = 'site-1'")
    _confirm_close(guild, _TicketMessage())
    assert panel.updates == [("site-1", "Тикет Discord", "текст", "closed", "normal", None, None)]


def test_second_close_does_not_touch_panel_ticket_again(database, monkeypatch):
    panel = _PanelDb()
    guild = _button_setup(database, monkeypatch, panel)
    database.execute("UPDATE tickets SET site_ticket_id = 'site-1', status = 'closed'")
    message = _TicketMessage()
    _confirm_close(guild, message)
    assert panel.updates == [] and message.edits


def test_panel_ticket_failure_does_not_stop_closing(database, monkeypatch):
    class Broken(_PanelDb):
        async def get_ticket(self, site_id):
            raise RuntimeError("панель недоступна")

    guild = _button_setup(database, monkeypatch, Broken())
    database.execute("UPDATE tickets SET site_ticket_id = 'site-1'")
    message = _TicketMessage()
    _confirm_close(guild, message)
    assert message.edits and database.execute("SELECT status FROM tickets").fetchone()[0] == "closed"


def test_resolved_panel_ticket_keeps_its_status(database, monkeypatch):
    panel = _PanelDb(status="resolved")
    guild = _button_setup(database, monkeypatch, panel)
    database.execute("UPDATE tickets SET site_ticket_id = 'site-1'")
    _confirm_close(guild, _TicketMessage())
    assert panel.updates == []


def test_automatic_close_closes_panel_ticket(database, monkeypatch):
    panel = _PanelDb()
    guild = _button_setup(database, monkeypatch, panel)
    database.execute("UPDATE tickets SET site_ticket_id = 'site-1'")
    assert asyncio.run(_cog(guild).panel_close_vip_ticket({"channel_id": "900"}))["closed"] is True
    assert panel.updates[0][3] == "closed"


OPEN_CATEGORY = 111
CLOSED_CATEGORY = 222


class _MovingChannel(_HistoryChannel):
    def __init__(self, channel_id=900, category_id=1, edit_error=None):
        super().__init__()
        self.id = channel_id
        self.category_id = category_id
        self.edit_error = edit_error
        self.moves = []

    async def edit(self, category, sync_permissions):
        if self.edit_error:
            raise self.edit_error
        self.moves.append((category, sync_permissions))
        self.category_id = category


class _CategoryGuild(_CloseGuild):
    def __init__(self, *channels):
        super().__init__(channels[0])
        self.channels = {channel.id: channel for channel in channels}

    def get_channel(self, channel_id):
        # Вместо объекта категории хватает её id: так тест видит, куда перенесён канал.
        if channel_id in (OPEN_CATEGORY, CLOSED_CATEGORY):
            return channel_id
        return self.channels.get(channel_id)


@pytest.fixture
def categories(monkeypatch):
    monkeypatch.setattr(tickets.config, "TICKET_CATEGORY", OPEN_CATEGORY)
    monkeypatch.setattr(tickets.config, "TICKET_CLOSED_CATEGORY", CLOSED_CATEGORY)
    monkeypatch.setattr(tickets.config, "TICKET_LOG_CHANNEL", 0)
    tickets._channel_locks.clear()



def test_close_button_moves_channel_and_keeps_permissions(database, monkeypatch, categories):
    _save_ok(database, monkeypatch)
    monkeypatch.setattr(tickets, "_panel_db", lambda: _PanelDb())
    _insert_open(database)
    channel = _MovingChannel()
    _confirm_close(_CategoryGuild(channel), _TicketMessage())
    assert channel.moves == [(CLOSED_CATEGORY, False)]


def test_automatic_close_moves_channel(database, monkeypatch, categories):
    _save_ok(database, monkeypatch)
    _insert_open(database)
    channel = _MovingChannel()
    guild = _CategoryGuild(channel)
    assert asyncio.run(_cog(guild).panel_close_vip_ticket({"channel_id": "900"}))["closed"] is True
    assert channel.moves == [(CLOSED_CATEGORY, False)]


def test_failed_move_does_not_stop_closing(database, monkeypatch, categories):
    _save_ok(database, monkeypatch)
    monkeypatch.setattr(tickets, "_panel_db", lambda: _PanelDb())
    _insert_open(database)
    response = type("Response", (), {"status": 400, "reason": "Maximum channels"})()
    channel = _MovingChannel(edit_error=discord.HTTPException(response, "category full"))
    message = _TicketMessage()
    _confirm_close(_CategoryGuild(channel), message)
    assert message.edits and database.execute("SELECT status FROM tickets").fetchone()[0] == "closed"


def test_existing_tickets_are_sorted_once_and_clans_stay(database, categories):
    rows = [(1, "support", "open"), (2, "vip_personal", "closed"), (3, "clan", "open"), (4, "support", "open")]
    for channel_id, ticket_type, status in rows:
        database.execute(
            "INSERT INTO tickets (user_id, channel_id, ticket_type, status) VALUES (555, ?, ?, ?)",
            (channel_id, ticket_type, status),
        )
    opened, closed, clan = _MovingChannel(1), _MovingChannel(2), _MovingChannel(3)
    already_there = _MovingChannel(4, category_id=OPEN_CATEGORY)
    guild = _CategoryGuild(opened, closed, clan, already_there)
    asyncio.run(tickets._move_existing_tickets(guild))
    asyncio.run(tickets._move_existing_tickets(guild))
    assert opened.moves == [(OPEN_CATEGORY, False)]
    assert closed.moves == [(CLOSED_CATEGORY, False)]
    assert clan.moves == [] and already_there.moves == []


class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, text=None, **kwargs):
        self.sent.append((text, kwargs))


def _reopen(guild, staff=True):
    interaction = _Interaction(guild)
    interaction.message = _TicketMessage()
    interaction.followup = _Followup()
    permissions = type("Permissions", (), {"manage_channels": staff})()
    interaction.user.guild_permissions = permissions
    interaction.user.roles = []
    sent = []

    async def send_message(text, **kwargs):
        sent.append(text)

    interaction.response.send_message = send_message

    async def run():
        view = tickets.TicketClosedView()
        await view.reopen.callback(interaction)

    tickets._channel_locks.clear()
    asyncio.run(run())
    return interaction, sent


def _closed_ticket(database, channel_id=900, ticket_type="support"):
    database.execute(
        "INSERT INTO tickets (user_id, channel_id, ticket_type, status, closed_by, closed_at, transcript_saved, site_ticket_id) "
        "VALUES (555, ?, ?, 'closed', 7, 'x', 1, 'site-1')",
        (channel_id, ticket_type),
    )


def _reopen_setup(monkeypatch, categories_panel=None):
    panel = categories_panel or _PanelDb(status="closed")
    monkeypatch.setattr(tickets, "_panel_db", lambda: panel)
    return panel


def test_reopen_restores_ticket_everywhere(database, monkeypatch, categories):
    panel = _reopen_setup(monkeypatch)
    _closed_ticket(database)
    channel = _MovingChannel(category_id=CLOSED_CATEGORY)
    guild = _CategoryGuild(channel)
    interaction, _ = _reopen(guild)
    row = database.execute("SELECT status, closed_at, closed_by, transcript_saved FROM tickets").fetchone()
    assert tuple(row) == ("open", None, None, 0)
    assert panel.updates[0][3] == "open"
    assert channel.moves == [(OPEN_CATEGORY, False)]
    target, overwrite = channel.permissions[0]
    assert target is guild.member and overwrite["view_channel"] and overwrite["send_messages"]
    assert channel.sent[0]["embed"].title == "Тикет снова открыт" and "ephemeral" not in channel.sent[0]
    assert interaction.message.edits == [{"view": None}]


def test_reopen_needs_staff(database, monkeypatch, categories):
    _reopen_setup(monkeypatch)
    _closed_ticket(database)
    _, answers = _reopen(_CategoryGuild(_MovingChannel()), staff=False)
    assert answers == ["❌ Нет прав."]
    assert database.execute("SELECT status FROM tickets").fetchone()[0] == "closed"


def test_reopen_refuses_second_open_ticket(database, monkeypatch, categories):
    _reopen_setup(monkeypatch)
    _closed_ticket(database)
    database.execute("INSERT INTO tickets (user_id, channel_id, ticket_type, status) VALUES (555, 901, 'support', 'open')")
    channel = _MovingChannel()
    interaction, _ = _reopen(_CategoryGuild(channel))
    assert "другой открытый" in interaction.followup.sent[0][0]
    assert database.execute("SELECT status FROM tickets WHERE channel_id = 900").fetchone()[0] == "closed"
    assert channel.moves == [] and channel.sent == []


def test_reopen_twice_is_harmless(database, monkeypatch, categories):
    _reopen_setup(monkeypatch)
    _closed_ticket(database)
    channel = _MovingChannel()
    guild = _CategoryGuild(channel)
    _reopen(guild)
    interaction, _ = _reopen(guild)
    assert interaction.followup.sent[0][0] == "Тикет уже открыт."
    assert len(channel.sent) == 1


def test_reopen_when_author_left_says_so(database, monkeypatch, categories):
    _reopen_setup(monkeypatch)
    _closed_ticket(database)
    channel = _MovingChannel()
    guild = _CategoryGuild(channel)
    guild.member = None
    _reopen(guild)
    assert "нет на сервере" in channel.sent[0]["embed"].description
    assert database.execute("SELECT status FROM tickets").fetchone()[0] == "open"


def test_closed_view_has_all_three_buttons():
    async def run():
        return {item.custom_id for item in tickets.TicketClosedView().children}

    assert asyncio.run(run()) == {"tk2_reopen", "tk2_delete", "tk2_transcript"}

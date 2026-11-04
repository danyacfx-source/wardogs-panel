"""Смещение больше длины журнала: читатели начинают сначала и не теряют новые оплаты."""
import asyncio
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "app" / "bot"))

from cogs import donation, seeding  # noqa: E402


def _page(events, offset, lines):
    page = lines[offset:offset + 100]
    return {"events": page, "nextOffset": offset + len(page), "total": len(lines)} if events else None


def test_donation_cog_rereads_after_journal_restart(monkeypatch):
    lines = [{"operationId": "new-1"}, {"operationId": "new-2"}]
    offset = {"value": 2256}
    applied = []
    cog = donation.DonationCog.__new__(donation.DonationCog)
    cog.checkout_secret = "s"

    async def read_feed(start):
        return _page(True, start, lines)

    def apply_payment(event, _now, _secret):
        applied.append(event["operationId"])
        return {"status": "credited"}

    cog._read_feed = read_feed
    cog._apply_payment = apply_payment
    monkeypatch.setattr(donation, "get_feed_offset", lambda: offset["value"])
    monkeypatch.setattr(donation, "set_feed_offset", lambda value: offset.update(value=value))
    result = asyncio.run(cog._import_feed())
    assert applied == ["new-1", "new-2"] and result["added"] == 2 and offset["value"] == 2


def test_donation_cog_keeps_offset_at_end_of_journal(monkeypatch):
    lines = [{"operationId": "old"}]
    offset = {"value": 1}
    cog = donation.DonationCog.__new__(donation.DonationCog)
    cog.checkout_secret = "s"

    async def read_feed(start):
        return _page(True, start, lines)

    cog._read_feed = read_feed
    cog._apply_payment = lambda *_: (_ for _ in ()).throw(AssertionError("старая оплата прочитана заново"))
    monkeypatch.setattr(donation, "get_feed_offset", lambda: offset["value"])
    monkeypatch.setattr(donation, "set_feed_offset", lambda value: offset.update(value=value))
    assert asyncio.run(cog._import_feed())["seen"] == 0 and offset["value"] == 1


def test_donation_cog_works_with_feed_without_total(monkeypatch):
    offset = {"value": 2256}
    cog = donation.DonationCog.__new__(donation.DonationCog)
    cog.checkout_secret = "s"

    async def read_feed(start):
        return {"events": [], "nextOffset": start, "total": None}

    cog._read_feed = read_feed
    monkeypatch.setattr(donation, "get_feed_offset", lambda: offset["value"])
    monkeypatch.setattr(donation, "set_feed_offset", lambda value: offset.update(value=value))
    asyncio.run(cog._import_feed())
    assert offset["value"] == 2256


class _Response:
    status = 200

    def __init__(self, body):
        self.body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def text(self):
        return json.dumps(self.body)


class _Session:
    def __init__(self, lines):
        self.lines = lines
        self.offsets = []

    def get(self, url, **_):
        offset = int(url.rsplit("=", 1)[1])
        self.offsets.append(offset)
        return _Response(_page(True, offset, self.lines))


def _service(monkeypatch, offset):
    service = seeding.DonationFeedService.__new__(seeding.DonationFeedService)
    service.state = {"offset": offset, "orders": {}, "vip": {}, "pendingTickets": []}
    service.last_success = 0.0
    service.running = False
    saved = []
    monkeypatch.setattr(seeding.config, "DONATION_SITE_BASE_URL", "https://example.test")
    monkeypatch.setattr(seeding.config, "DONATION_FEED_TOKEN", "t")
    monkeypatch.setattr(seeding.config, "DONATION_CHECKOUT_SECRET", "s")
    monkeypatch.setattr(seeding, "seed_set_state", lambda _key, state: saved.append(state["offset"]))
    return service, saved


def test_seeding_feed_rereads_after_journal_restart(monkeypatch):
    service, saved = _service(monkeypatch, 2256)
    applied = []
    service._apply = lambda event: applied.append(event["operationId"]) or True
    session = _Session([{"operationId": "new-1"}])
    assert asyncio.run(service.refresh(session)) == 1
    assert session.offsets == [2256, 0] and applied == ["new-1"]
    assert saved == [0, 1] and service.state["offset"] == 1


def test_seeding_feed_keeps_offset_at_end_of_journal(monkeypatch):
    service, _saved = _service(monkeypatch, 1)
    service._apply = lambda event: (_ for _ in ()).throw(AssertionError("старая оплата прочитана заново"))
    session = _Session([{"operationId": "old"}])
    assert asyncio.run(service.refresh(session)) == 0
    assert session.offsets == [1] and service.state["offset"] == 1

"""Кланы от бота на публичной странице: объединение, безопасность адресов, защита от затирания."""
import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import api, config, db, main
from app.main import app

SECRET = "b" * 64
LOGO = "https://cdn.discordapp.com/attachments/12345678901234567/12345678901234568/logo.png?ex=abc&is=def"
BOT_CLAN = {
    "id": "12345678901234567", "name": "Волки Рубежа", "tag": "WLF", "leader_name": "Лидер",
    "declared_members": 20, "server_members": 7, "discord_url": "https://discord.gg/wolves",
    "logo_source_url": LOGO, "description": "Описание", "goals": "Цели клана",
    "recruitment_status": "open", "recruitment_url": "https://example.com/join",
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "bot-clans.db")
    monkeypatch.setattr(config, "CLAN_SYNC_SECRET", SECRET)
    main._rate_windows.clear()
    api._clan_sync_seen.clear()
    with TestClient(app) as test_client:
        yield test_client


def _sync(client, clans):
    body = json.dumps({"version": 1, "clans": clans}, ensure_ascii=False).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(SECRET.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return client.post("/api/public/clan-sync", content=body, headers={
        "x-wardogs-timestamp": timestamp, "x-wardogs-signature": signature,
    })


def test_bot_clan_appears_on_public_list_and_detail(client):
    assert _sync(client, [BOT_CLAN]).json() == {"ok": True, "clans": 1, "rejected": 0}
    clans = client.get("/api/public/clans").json()["clans"]
    assert [clan["id"] for clan in clans] == ["bot-12345678901234567"]
    assert clans[0]["member_count"] == 7
    assert clans[0]["emblem_url"] == LOGO
    detail = client.get("/api/public/clans/bot-12345678901234567").json()["clan"]
    assert detail["leader"] == "Лидер"
    assert detail["members"] == []
    assert detail["discord_url"] == "https://discord.gg/wolves"
    assert detail["recruitment_open"] is True


def test_declared_members_used_when_server_count_missing(client):
    _sync(client, [{**BOT_CLAN, "server_members": 0}])
    assert client.get("/api/public/clans").json()["clans"][0]["member_count"] == 20


def test_panel_clan_wins_over_bot_clan(client, monkeypatch):
    panel = {"id": "a" * 24, "name": "Другое имя", "tag": "wlf", "member_count": 3}

    async def list_clans():
        return [panel]

    monkeypatch.setattr(db, "list_clans", list_clans)
    _sync(client, [BOT_CLAN, {**BOT_CLAN, "id": "12345678901234569", "name": "Отдельный", "tag": "SOLO"}])
    clans = client.get("/api/public/clans").json()["clans"]
    assert [clan["tag"] for clan in clans] == ["wlf", "SOLO"]


def test_unknown_bot_clan_is_404(client):
    assert client.get("/api/public/clans/bot-999").status_code == 404
    assert client.get("/api/public/clans/not-valid").status_code == 400


@pytest.mark.parametrize("field,value,result_field", [
    ("logo_source_url", "https://evil.example/x.png", "emblem_url"),
    ("logo_source_url", "javascript:alert(1)", "emblem_url"),
    ("logo_source_url", LOGO + "');background:url('https://evil.example", "emblem_url"),
    ("logo_source_url", 12345, "emblem_url"),
    ("discord_url", "javascript:alert(1)", "discord_url"),
    ("discord_url", "https://evil.example/invite", "discord_url"),
    ("recruitment_url", "javascript:alert(1)", "recruitment_url"),
    ("recruitment_url", "http://insecure.example", "recruitment_url"),
    ("recruitment_url", 'https://x.example/"onclick="alert(1)', "recruitment_url"),
])
def test_unsafe_urls_from_bot_are_dropped(client, field, value, result_field):
    _sync(client, [{**BOT_CLAN, field: value}])
    assert client.get("/api/public/clans").json()["clans"][0][result_field] == ""


def test_odd_member_counts_do_not_break_the_page(client):
    _sync(client, [{**BOT_CLAN, "server_members": "много", "declared_members": None}])
    assert client.get("/api/public/clans").json()["clans"][0]["member_count"] == 0


def test_payload_with_only_invalid_clans_keeps_current_list(client):
    _sync(client, [BOT_CLAN])
    response = _sync(client, [{"id": "not-a-snowflake", "name": "Плохой"}])
    assert response.status_code == 422
    assert len(client.get("/api/public/clans").json()["clans"]) == 1


def test_empty_list_from_bot_clears_clans(client):
    _sync(client, [BOT_CLAN])
    assert _sync(client, []).status_code == 200
    assert client.get("/api/public/clans").json()["clans"] == []


def test_partly_invalid_payload_reports_rejected_count(client):
    response = _sync(client, [BOT_CLAN, {"id": "x", "name": "Плохой"}])
    assert response.json() == {"ok": True, "clans": 1, "rejected": 1}


def test_public_response_has_no_private_fields(client):
    _sync(client, [{**BOT_CLAN, "leader_steam_id": "76561190000000001", "secret": "x"}])
    text = client.get("/api/public/clans/bot-12345678901234567").text
    assert "76561190000000001" not in text and "secret" not in text


def test_csp_allows_discord_logos(client):
    assert "https://cdn.discordapp.com" in client.get("/clans").headers["content-security-policy"]

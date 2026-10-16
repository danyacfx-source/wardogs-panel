"""Тесты публичных фидов: розыгрыши (парсер) и клановый реестр (HMAC)."""

import hashlib
import hmac
import json
import time

from fastapi.testclient import TestClient

from app import api, giveaways
from app.main import app


def _base(monkeypatch, tmp_path, secret=""):
    monkeypatch.setattr(api.config, "DB_PATH", tmp_path / "panel.db")
    monkeypatch.setattr(api.config, "CLAN_SYNC_SECRET", secret)
    monkeypatch.setattr(api.config, "GIVEAWAY_CHANNEL_ID", "")
    monkeypatch.setattr(api.config, "GIVEAWAY_AUTHOR_ID", "")


def _sign(secret, timestamp, body):
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return {"x-wardogs-timestamp": timestamp, "x-wardogs-signature": mac.hexdigest()}


CLAN_BODY = json.dumps({
    "version": 1,
    "generated_at": "2026-10-16T10:00:00.000Z",
    "source": {"guild_id": "403186008579112973"},
    "clans": [
        {"id": "76561190000000001", "name": "WARDOGS", "tag": "WD", "leader_name": "x"},
        {"id": "not-a-snowflake", "name": "Bad"},
        {"id": "76561190000000002", "name": "A", "tag": "A"},
    ],
}).encode()


def test_clan_sync_hmac_roundtrip(monkeypatch, tmp_path):
    secret = "a" * 64
    _base(monkeypatch, tmp_path, secret)
    with TestClient(app) as client:
        # неверная подпись
        response = client.post("/api/public/clan-sync", content=CLAN_BODY, headers={
            "x-wardogs-timestamp": str(int(time.time())),
            "x-wardogs-signature": "0" * 64,
        })
        assert response.status_code == 403

        # протухший timestamp
        stale = _sign(secret, str(int(time.time()) - 4000), CLAN_BODY)
        response = client.post("/api/public/clan-sync", content=CLAN_BODY, headers=stale)
        assert response.status_code == 403

        # валидная подпись, мусорные кланы отфильтрованы
        good = _sign(secret, str(int(time.time())), CLAN_BODY)
        response = client.post("/api/public/clan-sync", content=CLAN_BODY, headers=good)
        assert response.status_code == 200, response.text
        assert response.json() == {"ok": True, "clans": 1}

        # публичная отдача
        response = client.get("/api/public/clans-registry")
        assert response.status_code == 200
        feed = response.json()
        assert feed["version"] == 1
        assert len(feed["clans"]) == 1
        assert feed["clans"][0]["name"] == "WARDOGS"


def test_clan_sync_disabled_without_secret(monkeypatch, tmp_path):
    _base(monkeypatch, tmp_path, secret="")
    with TestClient(app) as client:
        response = client.post("/api/public/clan-sync", content=CLAN_BODY)
        assert response.status_code == 404


def test_public_giveaways_empty_feed(monkeypatch, tmp_path):
    _base(monkeypatch, tmp_path)
    with TestClient(app) as client:
        response = client.get("/api/public/giveaways")
        assert response.status_code == 200
        feed = response.json()
        assert feed["version"] == 1
        assert feed["giveaways"] == []


def test_giveaway_parser_end_and_filters():
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    ended = {
        "id": "1550409999999999998",
        "content": "Розыгрыш завершён",
        "author": {"bot": True, "id": "1"},
        "embeds": [{"title": "Розыгрыш", "description": "Участвуйте <t:1000000000:R>"}],
        "components": [{"components": [{"type": 2, "label": "Завершён", "disabled": True}]}],
        "mentions": [],
    }
    parsed = giveaways.parse_message(ended, "1", "2", now)
    assert parsed["status"] == "ended"

    # сообщение без ключевых слов — не розыгрыш
    plain = {"id": "1550409999999999997", "content": "просто привет", "embeds": [], "components": []}
    assert giveaways.parse_message(plain, "1", "2", now) is None

import json
import time

from nacl.signing import SigningKey
from fastapi.testclient import TestClient

from app import config, interactions
from app.main import app

KEY = SigningKey.generate()
PUB_HEX = KEY.verify_key.encode().hex()


def _sign(body: bytes, ts=None):
    ts = str(int(time.time()))
    sig = KEY.sign(ts.encode() + body).signature.hex()
    return {"x-signature-ed25519": sig, "x-signature-timestamp": ts,
            "content-type": "application/json"}


def test_verify_accepts_valid_signature():
    body = b'{"type":1}'
    assert interactions.verify(_sign(body), body, PUB_HEX) is True


def test_verify_rejects_bad_signature():
    body = b'{"type":1}'
    headers = _sign(body)
    headers["x-signature-ed25519"] = "00" * 64
    assert interactions.verify(headers, body, PUB_HEX) is False
    assert interactions.verify(headers, body, "") is False


def test_interactions_ping(monkeypatch):
    monkeypatch.setattr(config, "DISCORD_PUBLIC_KEY", PUB_HEX)
    body = json.dumps({"type": 1, "application_id": "1", "id": "1", "token": "t"}).encode()
    with TestClient(app) as client:
        response = client.post("/api/discord/interactions", content=body, headers=_sign(body))
    assert response.status_code == 200
    assert response.json() == {"type": 1}


def test_interactions_bad_signature_401(monkeypatch):
    monkeypatch.setattr(config, "DISCORD_PUBLIC_KEY", PUB_HEX)
    body = b'{"type":1}'
    headers = _sign(body)
    headers["x-signature-ed25519"] = "11" * 64
    with TestClient(app) as client:
        response = client.post("/api/discord/interactions", content=body, headers=headers)
    assert response.status_code == 401


def test_sw_command_registered_in_payload():
    names = [o["name"] for o in interactions.SW_COMMAND["options"]]
    assert interactions.SW_COMMAND["name"] == "sw"
    assert names == ["server", "side", "player"]
    assert interactions.SW_COMMAND["options"][1]["autocomplete"] is True

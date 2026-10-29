"""The seeding bot shares RCON settings with all three panel servers."""

import pytest

from app.bot import config as bot_config


@pytest.fixture
def panel_rcon_servers(monkeypatch):
    servers = [
        {"id": "ru1", "scheme": "http", "host": "192.0.2.1", "port": 7839, "token": "token-one"},
        {"id": "ru2", "scheme": "https", "host": "server-two.example", "port": 7789, "token": "token-two"},
        {"id": "ru3", "scheme": "http", "host": "192.0.2.3", "port": 9011, "token": "token-three"},
    ]
    monkeypatch.setattr(bot_config._panel, "SERVERS", servers)
    for key in (
        "SEED_RCON_URL", "SEED_RCON_PASSWORD",
        "SEED_RCON_2_URL", "SEED_RCON_2_PASSWORD",
        "SEED_RCON_3_URL", "SEED_RCON_3_PASSWORD",
        "WARDOGS_RCON_URL", "WARDOGS_RCON_PASSWORD",
        "WARDOGS_RCON_2_URL", "WARDOGS_RCON_2_PASSWORD",
        "WARDOGS_RCON_3_URL", "WARDOGS_RCON_3_PASSWORD",
    ):
        monkeypatch.delenv(key, raising=False)
    return servers


@pytest.mark.parametrize(
    "server_id,url_key,password_key,legacy_url,legacy_password,expected",
    [
        ("ru1", "SEED_RCON_URL", "SEED_RCON_PASSWORD",
         "WARDOGS_RCON_URL", "WARDOGS_RCON_PASSWORD",
         ("http://192.0.2.1:7839", "token-one")),
        ("ru2", "SEED_RCON_2_URL", "SEED_RCON_2_PASSWORD",
         "WARDOGS_RCON_2_URL", "WARDOGS_RCON_2_PASSWORD",
         ("https://server-two.example:7789", "token-two")),
        ("ru3", "SEED_RCON_3_URL", "SEED_RCON_3_PASSWORD",
         "WARDOGS_RCON_3_URL", "WARDOGS_RCON_3_PASSWORD",
         ("http://192.0.2.3:9011", "token-three")),
    ],
)
def test_seed_rcon_automatically_uses_all_three_panel_servers(
    panel_rcon_servers, server_id, url_key, password_key,
    legacy_url, legacy_password, expected,
):
    assert bot_config._seed_rcon_config(
        server_id, url_key, password_key, legacy_url, legacy_password
    ) == expected


def test_seed_rcon_custom_url_does_not_leak_panel_token(panel_rcon_servers, monkeypatch):
    monkeypatch.setenv("SEED_RCON_2_URL", "https://other.example:443")
    assert bot_config._seed_rcon_config(
        "ru2", "SEED_RCON_2_URL", "SEED_RCON_2_PASSWORD",
        "WARDOGS_RCON_2_URL", "WARDOGS_RCON_2_PASSWORD",
    ) == ("https://other.example:443", "")

    monkeypatch.setenv("SEED_RCON_2_PASSWORD", "different-token")
    assert bot_config._seed_rcon_config(
        "ru2", "SEED_RCON_2_URL", "SEED_RCON_2_PASSWORD",
        "WARDOGS_RCON_2_URL", "WARDOGS_RCON_2_PASSWORD",
    ) == ("https://other.example:443", "different-token")


def test_seed_rcon_custom_password_with_default_url(panel_rcon_servers, monkeypatch):
    monkeypatch.setenv("SEED_RCON_3_PASSWORD", "custom-token")
    assert bot_config._seed_rcon_config(
        "ru3", "SEED_RCON_3_URL", "SEED_RCON_3_PASSWORD",
        "WARDOGS_RCON_3_URL", "WARDOGS_RCON_3_PASSWORD",
    ) == ("http://192.0.2.3:9011", "custom-token")


def test_seed_rcon_missing_server_remains_disabled(panel_rcon_servers, monkeypatch):
    monkeypatch.setattr(bot_config._panel, "SERVERS", panel_rcon_servers[:2])
    assert bot_config._seed_rcon_config(
        "ru3", "SEED_RCON_3_URL", "SEED_RCON_3_PASSWORD",
        "WARDOGS_RCON_3_URL", "WARDOGS_RCON_3_PASSWORD",
    ) == ("", "")

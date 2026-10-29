"""All three swap cards use the correct RCON connection."""

from app.bot import runner  # noqa: F401
from cogs import swap


def test_swap_accepts_third_server_and_aliases(monkeypatch):
    monkeypatch.setattr(
        swap.config, "SERVERS",
        [{"id": "ru1"}, {"id": "ru2"}, {"id": "ru3"}],
    )
    monkeypatch.setattr(swap.config, "SEED_RCON_3_URL", "http://127.0.0.1:9011")
    monkeypatch.setattr(swap.config, "SEED_RCON_3_PASSWORD", "third-password")
    assert swap._server_index("ru1") == 0
    assert swap._server_index("ru2") == 1
    assert swap._server_index("ru3") == 2
    assert swap._server_index("3") == 2
    assert swap._server_index("third") == 2
    client = swap._get_rcon_client("ru3")
    assert client is not None
    assert swap._get_rcon_client("third") is not None


def test_swap_ru3_uses_third_rcon_even_when_sw_cards_are_reordered(monkeypatch):
    monkeypatch.setattr(
        swap.config, "SERVERS",
        [{"id": "ru3"}, {"id": "ru1"}, {"id": "ru2"}],
    )
    monkeypatch.setattr(swap.config, "SEED_RCON_3_URL", "http://127.0.0.1:9011")
    monkeypatch.setattr(swap.config, "SEED_RCON_3_PASSWORD", "third-password")
    monkeypatch.setattr(swap.config, "SEED_RCON_URL", "http://127.0.0.1:7839")
    monkeypatch.setattr(swap.config, "SEED_RCON_PASSWORD", "first-password")
    assert swap._server_index("ru3") == 2
    assert swap._server_index("ru1") == 0
    assert swap._server_index("ru2") == 1
    assert swap._get_rcon_client("ru3") is not None

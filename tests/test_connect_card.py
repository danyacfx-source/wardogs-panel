"""Карточки «Как подключиться»: логотипы на месте, текст содержит ID сервера, отпечаток меняется при правках."""
from PIL import Image

from app.bot import config
from app.bot import connect_card

PUBLIC_ID = "c89b3266-aed5-40d1-b022-6da6f6eba377"


def test_default_channel_is_connect_channel():
    assert config.SEED_CONNECT_CHANNEL_ID == 1551777090653921280


def test_each_server_has_logo_file_of_expected_size():
    assert [s.key for s in connect_card.CONNECT_SERVERS] == ["main", "second", "third"]
    for server in connect_card.CONNECT_SERVERS:
        path = connect_card.ASSET_DIR / server.logo
        assert path.exists(), server.logo
        with Image.open(path) as image:
            assert image.format == "WEBP"
            assert image.size == (1254, 1254)


def test_description_contains_label_and_public_id():
    text = connect_card.connect_description("#1 WDRU", PUBLIC_ID)
    assert "**#1 WDRU**" in text
    assert f"`{PUBLIC_ID}`" in text


def test_digest_changes_when_logo_or_text_changes():
    logo = (connect_card.ASSET_DIR / "server_logo_1.webp").read_bytes()
    text = connect_card.connect_description("#1 WDRU", PUBLIC_ID)
    base = connect_card.connect_digest(logo, text)
    assert connect_card.connect_digest(logo, text) == base
    assert connect_card.connect_digest(logo + b"x", text) != base
    assert connect_card.connect_digest(logo, text + " ") != base


def test_embed_points_thumbnail_at_attached_logo():
    server = connect_card.CONNECT_SERVERS[0]
    embed = connect_card.build_connect_embed(server, "text", "server_logo_main.webp")
    assert embed.thumbnail.url == "attachment://server_logo_main.webp"
    assert embed.title == "🎮 Как подключиться · #1 WDRU"

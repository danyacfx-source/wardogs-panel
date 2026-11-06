"""Логи сообщений: вложения выводятся именами и ссылками, а не словом «Вложение»."""
import pathlib
import sys
from types import SimpleNamespace

import pytest

BOT_DIR = pathlib.Path(__file__).resolve().parents[1] / "app" / "bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

discord = pytest.importorskip("discord")
from cogs import logging as logging_cog  # noqa: E402


def _cog():
    cog = logging_cog.LoggingCog.__new__(logging_cog.LoggingCog)
    return cog


def test_attachments_listed_with_name_size_and_url():
    cog = _cog()
    message = SimpleNamespace(attachments=[
        SimpleNamespace(filename="shot.png", size=2048, url="https://cdn.example/shot.png"),
        SimpleNamespace(filename="report.pdf", size=500, url="https://cdn.example/report.pdf"),
    ])
    text = cog._attachments_text(message)
    assert "shot.png (2 КБ)" in text
    assert "https://cdn.example/report.pdf" in text
    assert "Вложение" != text


def test_no_attachments_gives_dash():
    cog = _cog()
    assert cog._attachments_text(SimpleNamespace(attachments=[])) == "—"

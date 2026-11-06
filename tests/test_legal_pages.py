"""Политика конфиденциальности и условия cookie доступны публично и ссылаются друг на друга."""
from fastapi.testclient import TestClient

from app.main import app


def test_privacy_and_cookie_pages_are_public():
    with TestClient(app) as client:
        privacy = client.get("/privacy")
        cookies = client.get("/cookies")
    assert privacy.status_code == 200
    assert "Политика конфиденциальности" in privacy.text
    assert "152-ФЗ" in privacy.text
    assert cookies.status_code == 200
    assert "wds_session" in cookies.text
    assert "wds_csrf" in cookies.text
    assert 'href="/cookies"' in privacy.text
    assert 'href="/privacy"' in cookies.text


def test_shell_script_has_cookie_banner_and_settings_hook():
    from pathlib import Path

    shell = (Path(__file__).resolve().parents[1] / "static" / "public-shell.js").read_text(encoding="utf-8")
    assert "wds_cookie_choice" in shell
    assert "cookie-banner" in shell
    assert "data-cookie-settings" in shell

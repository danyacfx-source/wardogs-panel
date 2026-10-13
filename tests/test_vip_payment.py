"""Оплата заявок панели: страница перевода /donate/vip/<id> и webhook ЮMoney.

Заявка живёт в базе (метка WD-<16 hex>), в отличие от заказов донат-бота,
которые лежат в подписанных файлах. Поэтому webhook разбирает такие метки
отдельной веткой: сверяет сумму с тарифом и закрывает заявку через
fulfill_vip_order.
"""

import asyncio
import hashlib
import hmac
import time
from urllib.parse import quote

from fastapi.testclient import TestClient

from app import api, config, db, donate
from app.main import app

NOTIFY_SECRET = "notify-secret-vip-test-0123456789abcdef"
WALLET = "41003188981230"


def _configure(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "YOOMONEY_WALLET", WALLET)
    monkeypatch.setattr(config, "YOOMONEY_NOTIFICATION_SECRET", NOTIFY_SECRET)
    monkeypatch.setattr(config, "DONATE_DIR", tmp_path / "donate")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "vip_pay.db")
    monkeypatch.setattr(config, "DATABASE_URL", "")


def _create_order(sid, amount=500, status="awaiting_payment"):
    order = {
        "id": "WD-0123456789ABCDEF",
        "buyer_steam_id": "76561190000000001",
        "server_id": sid,
        "plan_id": "personal-1",
        "amount": amount,
        "months": 1,
        "seats": 1,
        "recipients": ["76561190000000001"],
        "status": status,
        "created_utc": time.time(),
        "actor_steam_id": "76561190000000000",
    }
    return order


def _signed_params(order_id, amount, operation_id="op-panel-0001"):
    params = {
        "notification_type": "p2p-incoming",
        "operation_id": operation_id,
        "amount": amount,
        "currency": "643",
        "datetime": "2026-10-13T09:00:00Z",
        "label": order_id,
        "test_notification": "false",
    }
    items = sorted(params.items())
    message = "&".join(f"{key}={quote(str(value), safe='', encoding='utf-8')}" for key, value in items)
    params["sign"] = hmac.new(NOTIFY_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()
    return params


def _seed(sid):
    async def run():
        await db.init()
        await db.create_vip_order(_create_order(sid))
        await db.close()

    asyncio.run(run())


def test_vip_pay_page_renders_quickpay_form(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    sid = next(iter(api.POOL))
    _seed(sid)
    with TestClient(app) as client:
        page = client.get("/donate/vip/WD-0123456789ABCDEF")
        assert page.status_code == 200
        assert "yoomoney.ru/quickpay/confirm" in page.text
        assert 'name="label" value="WD-0123456789ABCDEF"' in page.text
        assert 'name="sum" value="500.00"' in page.text
        assert f'name="receiver" value="{WALLET}"' in page.text

        assert client.get("/donate/vip/WD-ffffffffffffffff").status_code == 404
        assert client.get("/donate/vip/").status_code == 200


def test_vip_pay_page_reports_closed_order(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    sid = next(iter(api.POOL))
    order = _create_order(sid, status="cancelled")
    async def run():
        await db.init()
        await db.create_vip_order(order)
        await db.close()

    asyncio.run(run())
    with TestClient(app) as client:
        page = client.get("/donate/vip/WD-0123456789ABCDEF")
        assert page.status_code == 409
        assert "отменена" in page.text


def test_vip_pay_page_requires_wallet(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "YOOMONEY_WALLET", "")
    sid = next(iter(api.POOL))
    _seed(sid)
    with TestClient(app) as client:
        page = client.get("/donate/vip/WD-0123456789ABCDEF")
        assert page.status_code == 503


def test_webhook_settles_panel_order(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    sid = next(iter(api.POOL))
    _seed(sid)
    with TestClient(app) as client:
        response = client.post("/donate/webhook.php", data=_signed_params("WD-0123456789ABCDEF", "500.00"))
        assert response.status_code == 200

        async def status():
            await db.init()
            order = await db.get_vip_order("WD-0123456789ABCDEF")
            slots = await db.list_vip_slots(sid)
            await db.close()
            return order["status"], slots

        order_status, slots = asyncio.run(status())
        assert order_status in {"paid", "fulfilled"}
        assert [slot["steam_id"] for slot in slots] == ["76561190000000001"]

        # Повторное уведомление ничего не дублирует.
        again = client.post("/donate/webhook.php", data=_signed_params("WD-0123456789ABCDEF", "500.00"))
        assert again.status_code == 200
        assert asyncio.run(status())[0] in {"paid", "fulfilled"}


def test_webhook_ignores_amount_mismatch(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    sid = next(iter(api.POOL))
    _seed(sid)
    with TestClient(app) as client:
        response = client.post(
            "/donate/webhook.php",
            data=_signed_params("WD-0123456789ABCDEF", "1.00", operation_id="op-panel-0002"),
        )
        assert response.status_code == 200

        async def status():
            await db.init()
            order = await db.get_vip_order("WD-0123456789ABCDEF")
            slots = await db.list_vip_slots(sid)
            await db.close()
            return order["status"], slots

        order_status, slots = asyncio.run(status())
        assert order_status == "awaiting_payment"
        assert slots == []


def test_webhook_ignores_unknown_panel_label(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/donate/webhook.php",
            data=_signed_params("WD-ffffffffffffffff", "500.00", operation_id="op-panel-0003"),
        )
        assert response.status_code == 200


def test_orders_endpoint_returns_pay_url(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    sid = next(iter(api.POOL))
    _seed(sid)

    async def allow(*args):
        return {"steam_id": "76561190000000000"}

    monkeypatch.setattr(api, "_require_any_perm", allow)
    result = asyncio.run(api.vip_orders(None))
    assert result["provider_connected"] is True
    assert result["items"][0]["pay_url"].endswith("/donate/vip/WD-0123456789ABCDEF")

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import quote, unquote

from fastapi.testclient import TestClient

from app import api, config, db, donate
from app.main import app

CHECKOUT_SECRET = "checkout-secret-test-0123456789abcdef"
FEED_TOKEN = "feed-token-test-0123456789abcdef0123456789abcdef"
NOTIFY_SECRET = "notify-secret-test-0123456789abcdef"
WALLET = "41003188981230"

# Строка шага 1 из документации ЮMoney (пример подписи для ключа secret123).
PREPARED_MESSAGE = (
    "amount=0.99&building=12"
    "&city=%7B%25%20translate%20%25%7D%D0%9C%D0%BE%D1%81%D0%BA%D0%B2%D0%B0%7B%25%20%2Ftranslate%20%25%7D"
    "&codepro=false&currency=643&datetime=2014-04-28T16%3A31%3A28Z"
    "&email=address%40example.ru"
    "&fathersname=%7B%25%20translate%20%25%7D%D0%98%D0%B2%D0%B0%D0%BD%D0%BE%D0%B2%D0%B8%D1%87%7B%25%20%2Ftranslate%20%25%7D"
    "&firstname=%7B%25%20translate%20%25%7D%D0%98%D0%B2%D0%B0%D0%BD%7B%25%20%2Ftranslate%20%25%7D"
    "&flat=10&label=YM.label.12345"
    "&lastname=%7B%25%20translate%20%25%7D%D0%98%D0%B2%D0%B0%D0%BD%D0%BE%D0%B2%7B%25%20%2Ftranslate%20%25%7D"
    "&notification_type=p2p-incoming&operation_id=904035776918098009"
    "&phone=%2B79253332211&sender=41003188981230"
    "&sha1_hash=8693ddf402fe5dcc4c4744d466cabada2628148c"
    "&street=%7B%25%20translate%20%25%7D%D0%A2%D0%B2%D0%B5%D1%80%D1%81%D0%BA%D0%B0%D1%8F%7B%25%20%2Ftranslate%20%25%7D"
    "&suite=10&test_notification=false&unaccepted=false"
    "&withdraw_amount=1.00&zip=125075"
)
PREPARED_SIGN = "092318d12a1249b8ff5cb7b93e1b409a35bfe01eadee9525a147dc977b4eb056"


def _configure(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DONATE_CHECKOUT_SECRET", CHECKOUT_SECRET)
    monkeypatch.setattr(config, "DONATE_FEED_TOKEN", FEED_TOKEN)
    monkeypatch.setattr(config, "YOOMONEY_WALLET", WALLET)
    monkeypatch.setattr(config, "YOOMONEY_NOTIFICATION_SECRET", NOTIFY_SECRET)
    monkeypatch.setattr(config, "DONATE_DIR", tmp_path)


def _make_order(kind="donation", amount=150000, product_id=None, recipients=None):
    now = int(time.time() * 1000)
    order = {
        "id": secrets.token_hex(16),
        "buyerId": "1553944475926269982",
        "kind": kind,
        "amountKopecks": amount,
        "recipients": recipients if recipients is not None else [],
        "createdAt": now,
        "expiresAt": now + 7200 * 1000,
    }
    if product_id is not None:
        order["productId"] = product_id
    return order


def _make_token(order, secret=CHECKOUT_SECRET):
    keys = ("id", "buyerId", "kind", "productId", "recipients", "amountKopecks", "createdAt", "expiresAt")
    payload = {key: order[key] for key in keys if key in order}
    base = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()
    return f"{base}.{signature}"


def _wm_sign(params, secret=NOTIFY_SECRET):
    items = sorted((key, value) for key, value in params.items() if key != "sign")
    message = "&".join(f"{key}={quote(value, safe='', encoding='utf-8')}" for key, value in items)
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def _webhook_params(order, amount="1500.00", operation_id="904035776918098009"):
    params = {
        "notification_type": "p2p-incoming",
        "operation_id": operation_id,
        "amount": amount,
        "withdraw_amount": amount,
        "currency": "643",
        "datetime": "2026-10-03T12:00:00Z",
        "codepro": "false",
        "label": f"WD2-{order['id']}",
        "test_notification": "false",
        "unaccepted": "false",
    }
    params["sign"] = _wm_sign(params)
    return params


def test_notification_sign_matches_docs_example():
    params = {}
    for part in PREPARED_MESSAGE.split("&"):
        key, _, value = part.partition("=")
        params[key] = unquote(value)
    params["sign"] = PREPARED_SIGN
    assert donate.notification_sign_ok(params, "secret123") is True
    params["amount"] = "1.99"
    assert donate.notification_sign_ok(params, "secret123") is False
    assert donate.notification_sign_ok({}, "secret123") is False


def test_checkout_token_roundtrip(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    order = _make_order("donation", 150000)
    token = _make_token(order)
    payload = donate.verify_checkout_token(token)
    assert payload is not None
    assert payload["id"] == order["id"]
    assert payload["amountKopecks"] == 150000

    # Испорченная подпись и мусор отклоняются.
    broken = token[:-4] + ("0000" if token[-4:] != "0000" else "1111")
    assert donate.verify_checkout_token(broken) is None
    assert donate.verify_checkout_token("garbage") is None
    assert donate.verify_checkout_token("") is None

    # Неверный секрет — заказ не принимается.
    monkeypatch.setattr(config, "DONATE_CHECKOUT_SECRET", "other-secret")
    assert donate.verify_checkout_token(token) is None
    monkeypatch.setattr(config, "DONATE_CHECKOUT_SECRET", CHECKOUT_SECRET)

    # Структурный отказ: личный VIP с чужим получателем.
    bad = _make_order("personal", 50000, product_id="personal_1", recipients=["1553944475926269983"])
    assert donate.verify_checkout_token(_make_token(bad)) is None
    # Донат ниже минимума.
    tiny = _make_order("donation", 100)
    assert donate.verify_checkout_token(_make_token(tiny)) is None


def test_checkout_page_renders_and_saves_order(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    order = _make_order("donation", 150000)
    token = _make_token(order)
    with TestClient(app) as client:
        response = client.get("/donate/index.php", params={"o": token})
        assert response.status_code == 200
        assert "yoomoney.ru/quickpay/confirm" in response.text
        assert f'name="label" value="WD2-{order["id"]}"' in response.text
        assert f'name="sum" value="1500.00"' in response.text
        assert "https://yoomoney.ru" in response.headers["content-security-policy"]
        saved = tmp_path / "orders" / f"{order['id']}.json"
        assert saved.exists()
        stored = json.loads(saved.read_text(encoding="utf-8"))
        assert stored["token"] == token
        assert stored["order"]["id"] == order["id"]

        # Alias той же страницы.
        alias = client.get("/donate/", params={"o": token})
        assert alias.status_code == 200

        # Битая подпись и пустой параметр — отказ.
        assert client.get("/donate/index.php", params={"o": token + "x"}).status_code == 400
        assert client.get("/donate/index.php").status_code == 400

        # Истёкший заказ — уведомление без формы.
        old = _make_order("donation", 150000)
        start = old["createdAt"] - 4 * 3600 * 1000
        old["createdAt"] = start
        old["expiresAt"] = start + 2 * 3600 * 1000
        expired = client.get("/donate/index.php", params={"o": _make_token(old)})
        assert expired.status_code == 200
        assert "quickpay" not in expired.text
        assert "Срок оплаты истёк" in expired.text

    # Без настроенной формы — 503.
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "YOOMONEY_WALLET", "")
    with TestClient(app) as client:
        assert client.get("/donate/index.php", params={"o": token}).status_code == 503


def test_webhook_records_event_and_feed_serves_it(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    order = _make_order("donation", 150000)
    token = _make_token(order)
    headers = {"X-WD-Feed-Token": FEED_TOKEN}
    with TestClient(app) as client:
        assert client.get("/donate/index.php", params={"o": token}).status_code == 200

        params = _webhook_params(order)
        response = client.post("/donate/webhook.php", data=params)
        assert response.status_code == 200
        assert response.text == "OK"

        # Повтор той же операции не создаёт дубликат.
        assert client.post("/donate/webhook.php", data=params).status_code == 200

        # Фид отдаёт событие с подписанным заказом.
        feed = client.get("/donate/feed.php", params={"offset": 0}, headers=headers)
        assert feed.status_code == 200
        body = feed.json()
        assert body["nextOffset"] == 1
        assert len(body["events"]) == 1
        event = body["events"][0]
        assert event["orderId"] == order["id"]
        assert event["operationId"] == params["operation_id"]
        assert event["grossKopecks"] == 150000
        assert event["currency"] == "643"
        assert event["orderToken"] == token

        # Пустой хвост: nextOffset >= offset, events <= 100.
        tail = client.get("/donate/feed.php", params={"offset": 1}, headers=headers)
        assert tail.status_code == 200
        assert tail.json() == {"events": [], "nextOffset": 1}

        # Тестовое уведомление принимается, но не пишется в журнал.
        test_params = dict(params)
        test_params.pop("sign")
        test_params["test_notification"] = "true"
        test_params["sign"] = _wm_sign(test_params)
        assert client.post("/donate/webhook.php", data=test_params).status_code == 200
        again = client.get("/donate/feed.php", params={"offset": 0}, headers=headers).json()
        assert len(again["events"]) == 1


def test_webhook_rejects_bad_sign_and_foreign_label(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    order = _make_order("donation", 150000)
    token = _make_token(order)
    headers = {"X-WD-Feed-Token": FEED_TOKEN}
    with TestClient(app) as client:
        assert client.get("/donate/index.php", params={"o": token}).status_code == 200

        params = _webhook_params(order)
        forged = dict(params)
        forged["amount"] = "9999.00"
        forged["sign"] = params["sign"]
        assert client.post("/donate/webhook.php", data=forged).status_code == 403

        foreign = _webhook_params(order)
        foreign["label"] = "OTHER-label"
        foreign["sign"] = _wm_sign(foreign)
        assert client.post("/donate/webhook.php", data=foreign).status_code == 200
        assert client.get("/donate/feed.php", params={"offset": 0}, headers=headers).json()["events"] == []

        # Заказ, которого нет на сайте, — событие не записывается.
        stranger = _make_order("donation", 150000)
        stranger_params = _webhook_params(stranger)
        assert client.post("/donate/webhook.php", data=stranger_params).status_code == 200
        assert client.get("/donate/feed.php", params={"offset": 0}, headers=headers).json()["events"] == []


def test_feed_requires_token(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    with TestClient(app) as client:
        assert client.get("/donate/feed.php", params={"offset": 0}).status_code == 401
        bad = client.get("/donate/feed.php", params={"offset": 0}, headers={"X-WD-Feed-Token": "wrong"})
        assert bad.status_code == 401
        spaces = client.get(
            "/donate/feed.php", params={"offset": 0}, headers={"X-WD-Feed-Token": "bad token"}
        )
        assert spaces.status_code == 401

    monkeypatch.setattr(config, "DONATE_FEED_TOKEN", "")
    with TestClient(app) as client:
        empty = client.get("/donate/feed.php", params={"offset": 0}, headers={"X-WD-Feed-Token": ""})
        assert empty.status_code == 401


def test_webhook_requires_notification_secret(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "YOOMONEY_NOTIFICATION_SECRET", "")
    order = _make_order("donation", 150000)
    with TestClient(app) as client:
        response = client.post("/donate/webhook.php", data=_webhook_params(order))
        assert response.status_code == 400


def test_feed_paging_stays_monotonic(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    headers = {"X-WD-Feed-Token": FEED_TOKEN}
    for index in range(donate.FEED_PAGE_LIMIT + 5):
        donate.append_event(
            {
                "orderId": "0" * 32,
                "operationId": f"op-{index}",
                "currency": "643",
                "grossKopecks": 5000,
                "timestamp": 1,
                "orderToken": "",
            }
        )
    with TestClient(app) as client:
        first = client.get("/donate/feed.php", params={"offset": 0}, headers=headers).json()
        assert len(first["events"]) == donate.FEED_PAGE_LIMIT
        assert first["nextOffset"] == donate.FEED_PAGE_LIMIT
        second = client.get(
            "/donate/feed.php", params={"offset": first["nextOffset"]}, headers=headers
        ).json()
        assert len(second["events"]) == 5
        assert second["nextOffset"] == donate.FEED_PAGE_LIMIT + 5
        assert second["nextOffset"] >= donate.FEED_PAGE_LIMIT


def test_site_checkout_creates_signed_order(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "checkout.db")
    steam_id = "76561190000000001"
    discord_id = "1553944475926269982"

    async def seed():
        await db.init()
        await db.upsert_user(steam_id, "Buyer")
        token = await db.create_session(steam_id)
        await db.upsert_binding(steam_id, discord_id, "Buyer", ["role-1"], True)
        await db.close()
        return token

    session_token = asyncio.run(seed())
    with TestClient(app) as client:
        # Без входа — 401.
        assert client.post("/api/donate/checkout", json={"product": "personal_1"}).status_code == 401

        client.cookies.set(api.SESSION_COOKIE, session_token)
        client.get("/api/session")  # ставит wds_csrf
        headers = {"X-CSRF-Token": client.cookies.get("wds_csrf") or ""}

        # Неизвестный тариф — 400.
        bad = client.post("/api/donate/checkout", json={"product": "clan_10"}, headers=headers)
        assert bad.status_code == 400

        response = client.post("/api/donate/checkout", json={"product": "personal_1"}, headers=headers)
        assert response.status_code == 200
        url = response.json()["url"]
        assert url.startswith("/donate/index.php?o=")

        # Заказ подписан секретом бота и лежит в хранилище.
        payload = donate.verify_checkout_token(url.split("?o=", 1)[1])
        assert payload is not None
        assert payload["kind"] == "personal"
        assert payload["productId"] == "personal_1"
        assert payload["amountKopecks"] == 50000
        assert payload["buyerId"] == discord_id
        assert payload["recipients"] == [discord_id]
        assert (tmp_path / "orders" / f"{payload['id']}.json").exists()

        # Страница перевода открывается по выданной ссылке.
        page = client.get(url)
        assert page.status_code == 200
        assert "yoomoney.ru/quickpay/confirm" in page.text


def test_site_checkout_requires_discord_binding(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "unbound.db")
    steam_id = "76561190000000002"

    async def seed():
        await db.init()
        await db.upsert_user(steam_id, "NoDiscord")
        token = await db.create_session(steam_id)
        await db.close()
        return token

    session_token = asyncio.run(seed())
    with TestClient(app) as client:
        client.cookies.set(api.SESSION_COOKIE, session_token)
        client.get("/api/session")
        headers = {"X-CSRF-Token": client.cookies.get("wds_csrf") or ""}
        response = client.post(
            "/api/donate/checkout", json={"product": "personal_1"}, headers=headers
        )
        assert response.status_code == 409
        assert "Discord" in response.json()["detail"]

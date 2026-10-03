"""Сайт-часть донатов WARDOGS: страница перевода, webhook ЮMoney и фид ботов.

Контракт воспроизведён по боту донатилки v1.2.5 (API донатилки/src/*):

- GET  /donate/index.php?o=<payload>.<hmac> — страница перевода; ссылку
  формирует бот, сайт проверяет подпись заказа и сохраняет его;
- POST /donate/webhook.php — уведомление HTTP-уведомлений ЮMoney
  (application/x-www-form-urlencoded, проверка подписи sign);
- GET  /donate/feed.php?offset=N с заголовком X-WD-Feed-Token — закрытый
  журнал операций для донатного бота и бота сидеров.

Заказы хранятся в <DONATE_DIR>/orders/<id>.json, журнал — в
<DONATE_DIR>/events.jsonl (offset = номер строки, 0-based).
"""

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

try:
    from . import config
except ImportError:  # Поддержка прямого запуска: python app/donate.py
    import config

log = logging.getLogger("donate")

router = APIRouter(prefix="/donate")

LABEL_PREFIX = "WD2-"
FEED_PAGE_LIMIT = 100
MIN_DONATION_KOPECKS = 5000
MAX_DONATION_KOPECKS = 10_000_000
ORDER_ID_RE = re.compile(r"^[a-f0-9]{32}$")
BUYER_ID_RE = re.compile(r"^\d{17,20}$")
OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,80}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
KINDS = {"donation", "personal", "clan"}
# Имена продуктов повторяют src/catalog.js бота — используются только для
# отображения, решения о платеже принимаются по подписи.
PRODUCTS = {
    "personal_1": "Личный VIP · 1 месяц",
    "personal_3": "Личный VIP · 3 месяца",
    "personal_12": "Личный VIP · 12 месяцев",
    "clan_10": "VIP для клана · 10 мест · 1 месяц",
    "clan_20": "VIP для клана · 20 мест · 1 месяц",
    "clan_30": "VIP для клана · 30 мест · 1 месяц",
}
# Тарифы, доступные для покупки с сайта. Клановый VIP здесь не продаётся:
# для него нужны Discord-идентификаторы получателей, их выбирают в боте.
SITE_PRODUCTS = {
    "personal_1": ("personal", 50000),
    "personal_3": ("personal", 120000),
    "personal_12": ("personal", 480000),
}
ORDER_LIFETIME_MS = 7200 * 1000
_LOCK = threading.Lock()
_MSK = timezone(timedelta(hours=3))


# --------------------------------------------------------------------------
# Хранилище
# --------------------------------------------------------------------------

def _orders_dir() -> Path:
    path = config.DONATE_DIR / "orders"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _events_path() -> Path:
    config.DONATE_DIR.mkdir(parents=True, exist_ok=True)
    return config.DONATE_DIR / "events.jsonl"


def _secure_write(path: Path, data: str) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(data, encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(path)


def save_order(order: dict, token: str) -> None:
    """Сохраняет подписанный заказ при открытии страницы перевода."""
    path = _orders_dir() / f"{order['id']}.json"
    with _LOCK:
        if path.exists():
            return
        payload = {"order": order, "token": token, "savedAt": int(time.time() * 1000)}
        _secure_write(path, json.dumps(payload, ensure_ascii=False))


def load_order(order_id: str) -> Optional[dict]:
    if not ORDER_ID_RE.fullmatch(order_id or ""):
        return None
    path = _orders_dir() / f"{order_id}.json"
    with _LOCK:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


def append_event(event: dict) -> None:
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
    with _LOCK:
        path = _events_path()
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
        try:
            path.chmod(0o600)
        except OSError:
            pass


def _read_lines() -> list:
    with _LOCK:
        path = _events_path()
        if not path.exists():
            return []
        return path.read_text(encoding="utf-8").splitlines()


def read_feed(offset: int) -> dict:
    """Возвращает страницу журнала: events[offset:offset+100] и nextOffset.

    nextOffset всегда >= offset (требование бота); счётчик строк не зависит
    от битых записей — они пропускаются, но двигают offset.
    """
    lines = _read_lines()
    page = lines[offset : offset + FEED_PAGE_LIMIT]
    events = []
    for raw in page:
        try:
            events.append(json.loads(raw))
        except ValueError:
            log.warning("Фид донатов: пропущена некорректная строка журнала")
    return {"events": events, "nextOffset": offset + len(page)}


def _operation_seen(operation_id: str) -> bool:
    for raw in _read_lines():
        try:
            if json.loads(raw).get("operationId") == operation_id:
                return True
        except ValueError:
            continue
    return False


# --------------------------------------------------------------------------
# Подписи
# --------------------------------------------------------------------------

def _payload_valid(payload) -> bool:
    """Структурная проверка заказа бота (после успешной HMAC)."""
    if not isinstance(payload, dict):
        return False
    if not isinstance(payload.get("id"), str) or not ORDER_ID_RE.fullmatch(payload["id"]):
        return False
    if not isinstance(payload.get("buyerId"), str) or not BUYER_ID_RE.fullmatch(payload["buyerId"]):
        return False
    if payload.get("kind") not in KINDS:
        return False
    amount = payload.get("amountKopecks")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        return False
    if payload["kind"] == "donation" and not MIN_DONATION_KOPECKS <= amount <= MAX_DONATION_KOPECKS:
        return False
    recipients = payload.get("recipients")
    if not isinstance(recipients, list):
        return False
    if not all(isinstance(item, str) and BUYER_ID_RE.fullmatch(item) for item in recipients):
        return False
    if payload["kind"] == "donation" and recipients:
        return False
    if payload["kind"] == "personal" and (len(recipients) != 1 or recipients[0] != payload["buyerId"]):
        return False
    if payload["kind"] == "clan" and not recipients:
        return False
    for key in ("createdAt", "expiresAt"):
        value = payload.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
    if payload["expiresAt"] <= payload["createdAt"]:
        return False
    if payload["kind"] != "donation" and not isinstance(payload.get("productId"), str):
        return False
    return True


def verify_checkout_token(token: str) -> Optional[dict]:
    """Проверяет токен бота base64url(payload).hmac_sha256_hex(secret, base)."""
    secret = config.DONATE_CHECKOUT_SECRET
    if not secret or not token or "." not in token:
        return None
    base, _, signature = token.partition(".")
    if not base or not HEX64_RE.fullmatch(signature):
        return None
    expected = hmac.new(secret.encode("utf-8"), base.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected.encode(), signature.encode()):
        return None
    try:
        padding = "=" * (-len(base) % 4)
        payload = json.loads(base64.urlsafe_b64decode(base + padding).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return payload if _payload_valid(payload) else None


def sign_checkout_order(order: dict) -> str:
    """Собирает токен заказа ровно так, как checkoutToken() в боте."""
    if not config.DONATE_CHECKOUT_SECRET:
        raise ValueError("DONATE_CHECKOUT_SECRET не задан")
    keys = ("id", "buyerId", "kind", "productId", "recipients", "amountKopecks", "createdAt", "expiresAt")
    payload = {key: order[key] for key in keys if key in order}
    base = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")
    base = base.rstrip("=")
    signature = hmac.new(
        config.DONATE_CHECKOUT_SECRET.encode("utf-8"), base.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{base}.{signature}"


def create_site_order(product_id: str, buyer_discord_id: str) -> str:
    """Заказ, созданный самим сайтом, с подписью бота. Возвращает токен o."""
    if product_id not in SITE_PRODUCTS:
        raise ValueError("неизвестный тариф")
    if not BUYER_ID_RE.fullmatch(buyer_discord_id or ""):
        raise ValueError("некорректный Discord ID покупателя")
    kind, amount = SITE_PRODUCTS[product_id]
    now = int(time.time() * 1000)
    order = {
        "id": secrets.token_hex(16),
        "buyerId": buyer_discord_id,
        "kind": kind,
        "productId": product_id,
        "recipients": [buyer_discord_id],
        "amountKopecks": amount,
        "createdAt": now,
        "expiresAt": now + ORDER_LIFETIME_MS,
    }
    token = sign_checkout_order(order)
    save_order(order, token)
    return token


def notification_sign_ok(params: dict, secret: str) -> bool:
    """Проверяет подпись уведомления ЮMoney (sign, HMAC-SHA256).

    Алгоритм из документации: все параметры кроме sign, по алфавиту,
    значения URL-кодированы по RFC 3986 (UTF-8), склеены через '&'.
    """
    items = sorted((key, value) for key, value in params.items() if key != "sign")
    message = "&".join(f"{key}={quote(str(value), safe='', encoding='utf-8')}" for key, value in items)
    mac = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    supplied = str(params.get("sign") or "").lower()
    return hmac.compare_digest(mac.encode("ascii", "ignore"), supplied.encode("ascii", "ignore"))


# --------------------------------------------------------------------------
# Страницы
# --------------------------------------------------------------------------

def _fmt_rub(kopecks: int) -> str:
    if kopecks % 100 == 0:
        body = f"{kopecks // 100:,}".replace(",", " ")
    else:
        body = f"{kopecks / 100:,.2f}".replace(",", " ").replace(".", ",")
    return f"{body} ₽"


def _order_title(order: dict) -> str:
    if order["kind"] == "donation":
        return "Поддержка сообщества WARDOGS"
    return PRODUCTS.get(order.get("productId") or "", f"Заказ {order.get('productId')}")


def _page(body: str) -> str:
    return (
        "<!doctype html><html lang=\"ru\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<meta name=\"robots\" content=\"noindex, nofollow\">"
        + body
        + "</head></html>"
    )


def _notice_page(title: str, text: str, status: int = 200) -> HTMLResponse:
    body = _page(
        f"<title>{html.escape(title)} · WARDOGS</title>"
        "<link rel=\"stylesheet\" href=\"/static/donate.css\">"
        "</head><body class=\"donate-body\"><main class=\"donate-card\">"
        "<p class=\"donate-brand\">WARDOGS</p>"
        f"<h1>{html.escape(title)}</h1>"
        f"<p class=\"donate-note\">{html.escape(text)}</p>"
        "<p class=\"donate-note\"><a href=\"/\">На главную</a></p>"
        "</main></body>"
    )
    return HTMLResponse(body, status_code=status)


def _checkout_html(order: dict, token: str) -> str:
    amount = order["amountKopecks"]
    rub = _fmt_rub(amount)
    title = _order_title(order)
    expires = datetime.fromtimestamp(order["expiresAt"] / 1000, tz=_MSK).strftime("%d.%m.%Y %H:%M (МСК)")
    order_short = order["id"][:8]
    wallet = html.escape(config.YOOMONEY_WALLET, quote=True)
    label = html.escape(f"{LABEL_PREFIX}{order['id']}", quote=True)
    sum_value = f"{amount / 100:.2f}"
    success_url = html.escape(f"{config.PUBLIC_URL}/donate/success", quote=True)
    escaped_title = html.escape(title, quote=True)

    if order["kind"] == "clan":
        details = f"<p class=\"donate-meta\">Получателей: {len(order['recipients'])}</p>"
    elif order["kind"] == "personal":
        details = f"<p class=\"donate-meta\">Привилегия для Discord {html.escape(order['buyerId'])}</p>"
    else:
        details = ""

    return _page(
        f"<title>{escaped_title} — оплата · WARDOGS</title>"
        "<link rel=\"stylesheet\" href=\"/static/donate.css\">"
        "</head><body class=\"donate-body\"><main class=\"donate-card\">"
        "<p class=\"donate-brand\">WARDOGS</p>"
        f"<h1>{escaped_title}</h1>"
        f"<p class=\"donate-amount\">{html.escape(rub)}</p>"
        f"<p class=\"donate-meta\">Заказ <code>{order_short}</code> · оплатить до {html.escape(expires)}</p>"
        f"{details}"
        "<form class=\"donate-form\" action=\"https://yoomoney.ru/quickpay/confirm\" method=\"post\" target=\"_top\">"
        f"<input type=\"hidden\" name=\"receiver\" value=\"{wallet}\">"
        f"<input type=\"hidden\" name=\"sum\" value=\"{sum_value}\">"
        f"<input type=\"hidden\" name=\"label\" value=\"{label}\">"
        f"<input type=\"hidden\" name=\"formcomment\" value=\"{escaped_title}\">"
        f"<input type=\"hidden\" name=\"short-dest\" value=\"{escaped_title}\">"
        "<input type=\"hidden\" name=\"quickpay-form\" value=\"shop\">"
        f"<input type=\"hidden\" name=\"successURL\" value=\"{success_url}\">"
        f"<button type=\"submit\" class=\"donate-button\">Перевести {html.escape(rub)} через ЮMoney</button>"
        "</form>"
        "<p class=\"donate-note\">После перевода роли и благодарность обновятся автоматически. "
        "Тестовое уведомление ЮMoney не начисляет платёж.</p>"
        "<p class=\"donate-note\"><a href=\"/support\">Вернуться на сайт</a></p>"
        "</main></body>"
    )


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------

@router.get("/index.php", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def checkout_page(o: str = ""):
    order = verify_checkout_token(o)
    if order is None:
        log.warning("Чекаут: недействительная ссылка оплаты")
        return _notice_page(
            "Ссылка недействительна",
            "Подпись заказа не прошла проверку. Откройте ссылку из Discord заново или запросите новую.",
            status=400,
        )
    if not config.YOOMONEY_WALLET or not config.DONATE_CHECKOUT_SECRET:
        log.error("Чекаут: YOOMONEY_WALLET или DONATE_CHECKOUT_SECRET не заданы")
        return _notice_page(
            "Оплата временно недоступна",
            "Платёжная форма не настроена. Сообщите администратору.",
            status=503,
        )
    save_order(order, o)
    if int(time.time() * 1000) > int(order["expiresAt"]):
        return _notice_page(
            "Срок оплаты истёк",
            "Заказ действует 2 часа. Запросите новую ссылку в Discord.",
        )
    return HTMLResponse(_checkout_html(order, o))


@router.get("/success", response_class=HTMLResponse)
async def success_page():
    return _notice_page(
        "Перевод отправлен",
        "Если платёж прошёл, роли и благодарность обновятся автоматически в течение минуты.",
    )


@router.post("/webhook.php")
async def yoomoney_webhook(request: Request):
    # Тело — application/x-www-form-urlencoded; парсим сами, чтобы не тянуть
    # python-multipart только ради формы.
    raw = await request.body()
    params = {
        key: value
        for key, value in parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True)
    }
    if not params:
        raise HTTPException(status_code=400, detail="empty notification")
    secret = config.YOOMONEY_NOTIFICATION_SECRET
    if not secret:
        log.error("Webhook ЮMoney: YOOMONEY_NOTIFICATION_SECRET не задан")
        raise HTTPException(status_code=400, detail="notification secret is not configured")
    if not notification_sign_ok(params, secret):
        log.warning("Webhook ЮMoney: неверная подпись (operation_id=%s)", params.get("operation_id"))
        raise HTTPException(status_code=403, detail="bad signature")
    if params.get("test_notification") == "true":
        log.info("Webhook ЮMoney: принято тестовое уведомление")
        return PlainTextResponse("OK")

    label = params.get("label") or ""
    if not label.startswith(LABEL_PREFIX):
        log.info("Webhook ЮMoney: чужая метка %r — пропуск", label[:64])
        return PlainTextResponse("OK")
    order_id = label[len(LABEL_PREFIX) :]
    stored = load_order(order_id)
    if stored is None:
        log.warning("Webhook ЮMoney: заказ %s не сохранён на сайте — пропуск", order_id)
        return PlainTextResponse("OK")

    notification_type = params.get("notification_type") or ""
    if notification_type not in {"p2p-incoming", "card-incoming"}:
        log.info("Webhook ЮMoney: тип уведомления %r не поддерживается", notification_type)
        return PlainTextResponse("OK")
    if (params.get("currency") or "") != "643":
        log.info("Webhook ЮMoney: валюта %r вместо 643 — пропуск", params.get("currency"))
        return PlainTextResponse("OK")
    operation_id = params.get("operation_id") or ""
    if not OPERATION_ID_RE.fullmatch(operation_id):
        log.warning("Webhook ЮMoney: некорректный operation_id %r", operation_id[:80])
        return PlainTextResponse("OK")
    try:
        kopecks = int((Decimal(params.get("amount") or "") * 100).to_integral_value())
        timestamp = int(datetime.fromisoformat(params["datetime"].replace("Z", "+00:00")).timestamp())
    except (InvalidOperation, ValueError, KeyError):
        log.warning("Webhook ЮMoney: не разобраны amount/datetime")
        return PlainTextResponse("OK")
    if kopecks <= 0:
        log.warning("Webhook ЮMoney: неположительная сумма %s — пропуск", kopecks)
        return PlainTextResponse("OK")
    if _operation_seen(operation_id):
        log.info("Webhook ЮMoney: операция %s уже в журнале", operation_id)
        return PlainTextResponse("OK")

    # Сумму и время сайт не отбрасывает: несовпадения разбирает бот
    # (paymentProblem -> /donation-admin review), здесь важна подлинность
    # уведомления и сохранённый подписанный заказ.
    append_event(
        {
            "orderId": order_id,
            "operationId": operation_id,
            "currency": "643",
            "grossKopecks": kopecks,
            "timestamp": timestamp,
            "orderToken": stored.get("token") or "",
        }
    )
    log.info("Webhook ЮMoney: операция %s на %s коп. записана (заказ %s)", operation_id, kopecks, order_id)
    return PlainTextResponse("OK")


@router.get("/feed.php")
async def donation_feed(request: Request, offset: int = 0):
    supplied = request.headers.get("X-WD-Feed-Token") or ""
    expected = config.DONATE_FEED_TOKEN
    if not expected or not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        log.warning("Фид донатов: неверный или не настроенный токен")
        raise HTTPException(status_code=401, detail="bad feed token")
    if offset < 0:
        raise HTTPException(status_code=400, detail="bad offset")
    return JSONResponse(read_feed(offset))

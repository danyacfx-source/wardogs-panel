import asyncio
import json
import logging

import aiohttp

log = logging.getLogger("rcon")


class RCONError(Exception):
    def __init__(self, status, code, message):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(f"[{status}] {code}: {message}")


class WardogsRCON:
    """Клиент HTTP RCON-API WARDOGS (Bearer-токен = пароль RCON).

    Документация: https://wardogs.tech/rcon-reference
    Роуты: /v1/status, /v1/players, /v1/capabilities, /v1/health и др.
    """

    def __init__(self, cfg):
        self.scheme = cfg.get("scheme", "http")
        self.host = cfg.get("host", "127.0.0.1")
        self.port = cfg.get("port", 7779)
        self.token = cfg.get("token", "")
        self.timeout = aiohttp.ClientTimeout(total=cfg.get("timeout", 30))
        self.max_response_bytes = max(64 * 1024, min(8 * 1024 * 1024, int(cfg.get("max_response_bytes", 2 * 1024 * 1024))))
        self._session = None

    @property
    def base_url(self):
        return f"{self.scheme}://{self.host}:{self.port}"

    def _headers(self):
        if not self.token:
            return {}
        return {"Authorization": f"Bearer {self.token}"}

    @staticmethod
    def _validate_path(api_path):
        if (
            not isinstance(api_path, str)
            or not api_path.startswith("/")
            or api_path.startswith("//")
            or len(api_path) > 512
            or any(ord(char) < 0x20 for char in api_path)
            or any(char in api_path for char in "?#\\")
            or "://" in api_path
            or any(segment in {".", ".."} for segment in api_path.split("/"))
        ):
            raise ValueError("Некорректный путь RCON API")
        return api_path

    async def _read_text(self, response):
        raw = await response.content.read(self.max_response_bytes + 1)
        if len(raw) > self.max_response_bytes:
            raise RCONError(0, "response_too_large", "RCON-ответ превышает допустимый размер")
        return raw.decode("utf-8", errors="replace")

    async def _get_session(self):
        if self._session is None or self._session.closed:
            # Игровой сервер некорректно держит keep-alive: повторный запрос
            # по тому же соединению висит ~20с. Новое соединение на каждый
            # запрос (Connection: close) — надёжнее.
            connector = aiohttp.TCPConnector(limit=16, force_close=True)
            self._session = aiohttp.ClientSession(timeout=self.timeout, connector=connector)
        return self._session

    async def get(self, api_path, params=None):
        api_path = self._validate_path(api_path)
        url = f"{self.base_url}{api_path}"
        session = await self._get_session()
        status = 0
        try:
            async with session.get(
                url, headers=self._headers(), params=params, allow_redirects=False
            ) as resp:
                status = resp.status
                text = await self._read_text(resp)
        except asyncio.TimeoutError as e:
            raise RCONError(0, "timeout", f"сервер не ответил за {self.timeout.total}s") from e
        except (aiohttp.ClientError, OSError) as e:
            raise RCONError(0, "network", str(e)) from e
        try:
            data = json.loads(text) if text.strip() else {}
        except Exception:
            data = None
        if status >= 400:
            err = data.get("error") or {} if isinstance(data, dict) else {}
            raise RCONError(
                status,
                err.get("code", "http_error"),
                err.get("message", f"HTTP {status}"),
            )
        if not isinstance(data, dict):
            raise RCONError(502, "invalid_response", "RCON вернул некорректный JSON-объект")
        return data

    async def request(self, method, api_path, json_body=None, raw_body=None,
                      content_type="application/json", params=None, extra_headers=None):
        """Generic RCON-запрос (для панели управления)."""
        api_path = self._validate_path(api_path)
        url = f"{self.base_url}{api_path}"
        session = await self._get_session()
        headers = self._headers()
        if extra_headers:
            headers.update(extra_headers)
        data = None
        if raw_body is not None:
            data = raw_body
            headers["Content-Type"] = content_type
        status = 0
        try:
            async with session.request(
                method,
                url,
                headers=headers,
                json=json_body,
                data=data,
                params=params,
                allow_redirects=False,
            ) as resp:
                status = resp.status
                text = await self._read_text(resp)
        except asyncio.TimeoutError as e:
            raise RCONError(0, "timeout", f"сервер не ответил за {self.timeout.total}s") from e
        except (aiohttp.ClientError, OSError) as e:
            raise RCONError(0, "network", str(e)) from e
        try:
            data = json.loads(text) if text.strip() else {}
        except Exception:
            data = text
        if status >= 400:
            err = (data or {}).get("error") or {} if isinstance(data, dict) else {}
            raise RCONError(
                status,
                err.get("code", "http_error"),
                err.get("message", f"HTTP {status}"),
            )
        return data

    async def request_raw(self, method, api_path, json_body=None, raw_body=None,
                          content_type="application/json", params=None, extra_headers=None):
        """Как request(), но не кидает RCONError при 4xx/5xx.

        Нужно для PUT/POST /v1/config: там 412 (устаревшая ревизия) и 422
        (значение отклонено) — это результаты проверки, а не ошибки связи.
        Возвращает (status, data).
        """
        api_path = self._validate_path(api_path)
        url = f"{self.base_url}{api_path}"
        session = await self._get_session()
        headers = self._headers()
        if extra_headers:
            headers.update(extra_headers)
        data = None
        if raw_body is not None:
            data = raw_body
            headers["Content-Type"] = content_type
        status = 0
        try:
            async with session.request(
                method,
                url,
                headers=headers,
                json=json_body,
                data=data,
                params=params,
                allow_redirects=False,
            ) as resp:
                status = resp.status
                text = await self._read_text(resp)
        except asyncio.TimeoutError as e:
            raise RCONError(0, "timeout", f"сервер не ответил за {self.timeout.total}s") from e
        except (aiohttp.ClientError, OSError) as e:
            raise RCONError(0, "network", str(e)) from e
        try:
            data = json.loads(text) if text.strip() else {}
        except Exception:
            data = text
        return status, data

    async def status(self):
        return await self.get("/v1/status")

    async def players(self):
        return await self.get("/v1/players")

    async def rotation(self):
        return await self.get("/v1/rotation")

    async def bans(self):
        return await self.get("/v1/bans")

    async def capabilities(self):
        return await self.get("/v1/capabilities")

    async def health(self):
        return await self.get("/v1/health")

    async def close(self):
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None
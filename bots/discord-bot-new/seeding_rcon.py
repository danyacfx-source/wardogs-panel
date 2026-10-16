"""HTTP-клиент WDRCON для seeding-подсистемы (порт rcon-client.js).

Работает через aiohttp, allowlist роутов, retry для GET, health-счётчик.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from typing import Any

import aiohttp

STEAM_ID_RE = re.compile(r"^7656119\d{10}$")

_ALLOWED_ROUTE_RE = re.compile(
    r"^/v1/(?:capabilities|status|players|reserved-slots|config)$"
    r"|^/v1/players/7656119\d{10}/message$"
    r"|^/v1/reserved-slots/7656119\d{10}$"
)

_TRANSIENT_STATUS = {429, 502, 503, 504}


class RconError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str = "rcon_error",
        status: int = 0,
        route: str = "",
        missing_route: bool = False,
        network_code: str = "",
    ):
        super().__init__(message)
        self.code = code
        self.status = status
        self.route = route
        self.missing_route = missing_route
        self.network_code = network_code


def _valid_route(path: str) -> bool:
    return bool(_ALLOWED_ROUTE_RE.match(path))


class WardogsRconClient:
    """Клиент WDRCON: status/players/whisper/reserved-slots/config."""

    def __init__(
        self,
        base_url: str,
        password: str,
        *,
        timeout_ms: int = 6000,
        max_response_bytes: int = 1_048_576,
        retry_attempts: int = 1,
        retry_base_ms: int = 300,
        label: str = "main",
    ):
        self.base_url = base_url.rstrip("/")
        self.password = password
        self.timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000)
        self.max_response_bytes = max_response_bytes
        self.retry_attempts = retry_attempts
        self.retry_base_ms = retry_base_ms
        self.label = label
        self._session: aiohttp.ClientSession | None = None
        self.health: dict[str, Any] = {
            "last_success_at": None,
            "last_failure_at": None,
            "consecutive_failures": 0,
            "last_error_code": "",
        }

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self.timeout,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self.password}",
                    "User-Agent": "wardogs-seeding-bot/1.5.1",
                },
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    def _record_success(self):
        import time

        self.health["last_success_at"] = time.time()
        self.health["consecutive_failures"] = 0
        self.health["last_error_code"] = ""

    def _record_failure(self, error: RconError):
        import time

        self.health["last_failure_at"] = time.time()
        self.health["consecutive_failures"] += 1
        self.health["last_error_code"] = error.code

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        allow_failure: bool = False,
    ) -> dict:
        if not _valid_route(path):
            raise RconError("RCON route is not allowlisted", code="bad_route", route=f"{method} {path}")

        session = await self._get_session()
        can_retry = method == "GET"
        attempts = self.retry_attempts if can_retry else 0
        last_error: RconError | None = None

        for attempt in range(attempts + 1):
            try:
                async with session.request(
                    method,
                    f"{self.base_url}{path}",
                    json=json_body,
                    proxy=None,
                ) as resp:
                    raw = await resp.text(errors="replace")
                    if len(raw.encode("utf-8", "replace")) > self.max_response_bytes:
                        err = RconError(
                            "RCON response exceeded the configured size limit",
                            code="response_too_large",
                            route=f"{method} {path}",
                        )
                        if allow_failure:
                            self._record_failure(err)
                            return {"ok": False, "status": resp.status, "error": err}
                        raise err

                    parsed: Any = None
                    if raw:
                        import json as _json

                        try:
                            parsed = _json.loads(raw)
                        except Exception:
                            is_html = "text/html" in (resp.headers.get("content-type") or "").lower() or raw.lstrip().lower().startswith(
                                ("<!doctype", "<html")
                            )
                            err = RconError(
                                "RCON URL points to an HTML control panel, not the WDRCON API"
                                if is_html
                                else "RCON returned malformed JSON",
                                code="wrong_endpoint" if is_html else "bad_response",
                                route=f"{method} {path}",
                            )
                            if allow_failure:
                                self._record_failure(err)
                                return {"ok": False, "status": resp.status, "error": err}
                            raise err

                    if resp.status < 200 or resp.status >= 300:
                        error_obj = (parsed or {}).get("error") if isinstance(parsed, dict) else None
                        code = (error_obj or {}).get("code", "request_failed")
                        server_message = (error_obj or {}).get("message", "")
                        missing_route = resp.status == 405 or (
                            resp.status == 404
                            and code == "not_found"
                            and re.search(r"no such endpoint", server_message, re.I)
                        )
                        err = RconError(
                            f"RCON request failed ({resp.status})",
                            status=resp.status,
                            code=code,
                            route=f"{method} {path}",
                            missing_route=missing_route,
                        )
                        if can_retry and resp.status in _TRANSIENT_STATUS and attempt < attempts:
                            await asyncio.sleep(self.retry_base_ms * (attempt + 1) / 1000)
                            continue
                        if allow_failure:
                            self._record_failure(err)
                            return {"ok": False, "status": resp.status, "parsed": parsed, "error": err}
                        self._record_failure(err)
                        raise err

                    self._record_success()
                    return {"ok": True, "status": resp.status, "parsed": parsed if parsed is not None else {}}

            except (aiohttp.ClientError, asyncio.TimeoutError) as net_err:
                err = RconError(
                    "RCON is unreachable",
                    code="unreachable",
                    route=f"{method} {path}",
                    network_code=type(net_err).__name__,
                )
                last_error = err
                if can_retry and attempt < attempts:
                    await asyncio.sleep(self.retry_base_ms * (attempt + 1) / 1000)
                    continue
                self._record_failure(err)
                if allow_failure:
                    return {"ok": False, "status": 0, "error": err}
                raise err

        if last_error:
            self._record_failure(last_error)
            raise last_error
        raise RconError("RCON request failed", code="rcon_error", route=f"{method} {path}")

    async def capabilities(self) -> dict:
        result = await self._request("GET", "/v1/capabilities")
        parsed = result.get("parsed") or {}
        routes = parsed.get("routes") or []
        return {
            "routes": [str(r) for r in routes],
            "config_writable": bool((parsed.get("config") or {}).get("writable")),
        }

    async def assert_live_reserved_writable(self):
        caps = await self.capabilities()
        normalized = [
            r.strip()
            .replace("{", "*")
            .replace("}", "*")
            for r in caps["routes"]
        ]
        # route params may appear as {steamId} or :steamId
        normalized = [
            re.sub(r"\{[^}]+\}|:[^/\s]+", "*", r).strip() for r in caps["routes"]
        ]
        required = [
            "GET /v1/reserved-slots",
            "POST /v1/reserved-slots",
            "DELETE /v1/reserved-slots/*",
        ]
        if not all(r in normalized for r in required):
            raise RconError(
                "This WDRCON build cannot change reserved slots without a server restart",
                code="live_reserved_unavailable",
                route="GET /v1/capabilities",
            )

    async def status(self) -> dict:
        result = await self._request("GET", "/v1/status")
        parsed = result.get("parsed") or {}
        players = parsed.get("players") or {}
        current = players.get("current")
        maximum = players.get("max")
        if not isinstance(current, int) or current < 0:
            raise RconError("RCON returned an invalid status", code="bad_response", route="GET /v1/status")
        return {
            "player_count": current,
            "max_players": maximum if isinstance(maximum, int) and maximum >= 0 else None,
            "server_name": str(parsed.get("serverName") or "")[:200],
            "map": str(parsed.get("map") or "")[:100],
        }

    async def players(self) -> list[dict]:
        result = await self._request("GET", "/v1/players")
        parsed = result.get("parsed") or {}
        raw_players = parsed.get("players")
        if not isinstance(raw_players, list):
            raise RconError("RCON returned an invalid player list", code="bad_response", route="GET /v1/players")
        out = []
        for player in raw_players:
            steam_id = str((player or {}).get("steamId") or "")
            if not STEAM_ID_RE.match(steam_id):
                raise RconError(
                    "RCON returned a player with an invalid SteamID64",
                    code="bad_response",
                    route="GET /v1/players",
                )
            name = str((player or {}).get("name") or "Игрок")
            name = re.sub(r"[\r\n\0]", "", name)[:80]
            out.append({"steam_id": steam_id, "name": name})
        return out

    async def whisper(self, steam_id: str, message: str):
        if not STEAM_ID_RE.match(str(steam_id)):
            raise ValueError("Invalid SteamID64")
        safe = re.sub(r"[\r\n\0]", " ", str(message)).strip()[:180]
        if not safe:
            raise ValueError("RCON whisper cannot be empty")
        await self._request("POST", f"/v1/players/{steam_id}/message", json_body={"message": safe})

    async def live_reserved_ids(self) -> list[str] | None:
        response = await self._request("GET", "/v1/reserved-slots", allow_failure=True)
        if not response.get("ok"):
            err: RconError | None = response.get("error")
            if err is not None and err.missing_route:
                return None
            if err is not None:
                raise err
            return None
        ids = (response.get("parsed") or {}).get("reservedSlots")
        if not isinstance(ids, list) or any(not STEAM_ID_RE.match(str(i)) for i in ids):
            raise RconError(
                "RCON returned an invalid reserved-slot list",
                code="bad_response",
                route="GET /v1/reserved-slots",
            )
        return list(dict.fromkeys(str(i) for i in ids))

    async def read_config_document(self) -> dict:
        result = await self._request("GET", "/v1/config")
        parsed = result.get("parsed") or {}
        text = parsed.get("text")
        revision = str(parsed.get("revision") or "")
        sections = parsed.get("sections")
        if not isinstance(text, str) or not revision or not isinstance(sections, list):
            raise RconError(
                "RCON returned an invalid configuration document",
                code="bad_response",
                route="GET /v1/config",
            )
        return {"text": text, "revision": revision, "writable": parsed.get("writable") is not False}

    async def document_reserved_ids(self) -> list[str] | None:
        try:
            document = await self.read_config_document()
        except RconError as err:
            if err.missing_route:
                return None
            raise
        return _read_reserved_ids(document["text"])

    async def reserved_snapshot(self) -> dict:
        live: list[str] | None = None
        document: list[str] | None = None
        try:
            live = await self.live_reserved_ids()
        except RconError:
            pass
        try:
            document = await self.document_reserved_ids()
        except RconError:
            document = None
        return {"live": live, "document": document}

    async def set_reserved(self, steam_id: str, desired: bool) -> dict:
        if not STEAM_ID_RE.match(str(steam_id)):
            raise ValueError("Invalid SteamID64")
        return await self._set_reserved_unlocked(steam_id, desired)

    async def _set_reserved_unlocked(self, steam_id: str, desired: bool) -> dict:
        live = await self.live_reserved_ids()
        if live is None:
            raise RconError(
                "WDRCON cannot verify the live reserved-slot list",
                code="live_reserved_unavailable",
                route="GET /v1/reserved-slots",
            )
        if (steam_id in live) == desired:
            return {"active": desired, "pending_restart": False, "changed": False, "via": "live"}

        if desired:
            direct_path = "/v1/reserved-slots"
            direct = await self._request(
                "POST", direct_path, json_body={"steamId": steam_id}, allow_failure=True
            )
        else:
            direct_path = f"/v1/reserved-slots/{steam_id}"
            direct = await self._request("DELETE", direct_path, allow_failure=True)

        if direct.get("ok"):
            return await self._confirm_live_reserved(steam_id, desired, True)

        status = direct.get("status") or 0
        err: RconError | None = direct.get("error")
        err_code = err.code if err else ""
        already_okay = (desired and status == 409 and err_code == "already_reserved") or (
            not desired and status == 404 and err_code == "reserved_not_found"
        )
        if already_okay:
            return await self._confirm_live_reserved(steam_id, desired, False)
        if err is not None and err.missing_route:
            raise RconError(
                "This WDRCON build cannot change reserved slots without a server restart",
                code="live_reserved_unavailable",
                route=f"{'POST' if desired else 'DELETE'} {direct_path}",
                status=status,
                missing_route=True,
            )
        if err is not None:
            raise err
        raise RconError("RCON set_reserved failed", code="rcon_error", status=status)

    async def _confirm_live_reserved(self, steam_id: str, desired: bool, changed: bool) -> dict:
        for attempt in range(3):
            if attempt:
                await asyncio.sleep(0.25 * attempt)
            live = await self.live_reserved_ids()
            if live is not None and (steam_id in live) == desired:
                return {"active": desired, "pending_restart": False, "changed": changed, "via": "live"}
        raise RconError(
            "RCON accepted the command but the live reserved-slot list did not change",
            code="reserved_not_applied",
            route="GET /v1/reserved-slots",
        )


def _read_reserved_ids(config_text: str) -> list[str]:
    """Парсит ReservedSlots из текста конфига WDRCON."""
    ids: list[str] = []
    for line in config_text.splitlines():
        m = re.search(r"\b(7656119\d{10})\b", line)
        if m and ("reserve" in line.lower() or "slot" in line.lower()):
            ids.append(m.group(1))
    return list(dict.fromkeys(ids))

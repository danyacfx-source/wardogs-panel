"""Pydantic-модели для REST API WARDOGS: запросы и ответы."""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

STEAM_ID_RE = re.compile(r"^\d{17}$")
CLAN_ID_RE = re.compile(r"^[0-9a-f]{24}$")
OBJECT_ID_RE = re.compile(r"^[0-9a-f]{24}$")
BOT_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{2,47}$")
BOT_EVENT_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,79}$")
CLAN_TAG_RE = re.compile(r"^[\wА-Яа-яЁё-]{1,16}$", re.UNICODE)
PUBLIC_TAG_RE = re.compile(r"^[A-ZА-Я0-9_-]{2,12}$")
DISCORD_ID_RE = re.compile(r"^\d{17,20}$")

TICKET_STATUSES = {"open", "in_progress", "resolved", "closed"}
TICKET_PRIORITIES = {"low", "normal", "high", "urgent"}
TAG_COLORS = {"gray", "orange", "red", "green", "blue", "purple", "pink"}
CLAN_ROLES = {"Лидер", "Заместитель", "Участник"}

BOT_SCOPES = {
    "heartbeat", "events.write", "stats.write", "bindings.read", "bindings.write",
    "tickets.read", "tickets.write", "moderation.write", "voice.manage", "commands.read",
}

VIP_PLANS = [
    {"id": "personal-1", "name": "Личный · 1 месяц", "amount": 500, "months": 1, "seats": 1},
    {"id": "personal-3", "name": "Личный · 3 месяца", "amount": 1200, "months": 3, "seats": 1},
    {"id": "personal-12", "name": "Личный · 12 месяцев", "amount": 4800, "months": 12, "seats": 1},
    {"id": "clan-10", "name": "Клан · 10 участников", "amount": 4500, "months": 1, "seats": 10},
    {"id": "clan-20", "name": "Клан · 20 участников", "amount": 8500, "months": 1, "seats": 20},
    {"id": "clan-30", "name": "Клан · 30 участников", "amount": 12000, "months": 1, "seats": 30},
]


def _clean_spaces(value: str) -> str:
    return " ".join(value.split())


# ── Базовые ответы ──────────────────────────────────────────────────

class OkResponse(BaseModel):
    ok: bool = True


class ErrorBody(BaseModel):
    code: str = ""
    message: str = ""


class ErrorResponse(BaseModel):
    error: ErrorBody


class OkItemResponse(BaseModel):
    ok: bool = True
    item: Optional[dict] = None


# ── VIP ─────────────────────────────────────────────────────────────

class VipOrderCreate(BaseModel):
    plan_id: str
    buyer_steam_id: str
    server_id: str
    recipients: list[str] = []

    @field_validator("buyer_steam_id")
    @classmethod
    def _buyer_valid(cls, v: str) -> str:
        if not STEAM_ID_RE.fullmatch(str(v or "")):
            raise ValueError("SteamID64 должен состоять из 17 цифр")
        return str(v)

    @field_validator("recipients")
    @classmethod
    def _recipients_valid(cls, v: list[str]) -> list[str]:
        if not isinstance(v, list):
            raise ValueError("recipients должен быть массивом")
        for s in v:
            if not isinstance(s, str) or not STEAM_ID_RE.fullmatch(s):
                raise ValueError("recipients должен содержать SteamID64")
        if len(set(v)) != len(v):
            raise ValueError("recipients должен содержать уникальные ID")
        return v


class VipOrderCreateResponse(BaseModel):
    ok: bool = True
    item: dict


class VipSlotSave(BaseModel):
    steam_id: Optional[str] = None
    steamId: Optional[str] = None
    display_name: str = ""
    note: str = ""
    expires_utc: Optional[Any] = None

    @model_validator(mode="after")
    def _check_steam(self):
        if not self.steam_id and not self.steamId:
            raise ValueError("steam_id обязателен")
        return self

    @property
    def steam(self) -> str:
        return str(self.steam_id or self.steamId)


class VipSlotSaveResponse(BaseModel):
    ok: bool = True
    item: Optional[dict] = None
    synced: bool = False
    warning: Optional[str] = None
    mirrored: list[str] = []


class VipSlotSyncResponse(BaseModel):
    ok: bool = False
    item: Optional[dict] = None
    detail: Optional[str] = None


# ── Донат ───────────────────────────────────────────────────────────

class DonateCheckoutRequest(BaseModel):
    product: str = ""


class DonateCheckoutResponse(BaseModel):
    url: str


# ── Заметки игроков ─────────────────────────────────────────────────

class PlayerNoteCreate(BaseModel):
    note: str

    @field_validator("note")
    @classmethod
    def _note_valid(cls, v: str) -> str:
        v = str(v or "").strip()
        if not v or len(v) > 2000:
            raise ValueError("Заметка должна содержать от 1 до 2000 символов")
        return v


class PlayerNoteResponse(BaseModel):
    ok: bool = True
    note: dict


# ── Метки игроков ───────────────────────────────────────────────────

class PlayerTagCreate(BaseModel):
    label: str
    color: str = "gray"

    @field_validator("label")
    @classmethod
    def _label_valid(cls, v: str) -> str:
        v = _clean_spaces(str(v or ""))
        if not v or len(v) > 32:
            raise ValueError("Метка должна содержать от 1 до 32 символов")
        return v

    @field_validator("color")
    @classmethod
    def _color_valid(cls, v: str) -> str:
        v = str(v or "gray").strip().lower()
        if v not in TAG_COLORS:
            raise ValueError("Некорректный цвет метки")
        return v


class PlayerTagResponse(BaseModel):
    ok: bool = True
    tag: dict


# ── Кланы ───────────────────────────────────────────────────────────

class ClanPayload(BaseModel):
    name: str
    tag: str
    color: str = "orange"
    description: str = ""

    @field_validator("name")
    @classmethod
    def _name_valid(cls, v: str) -> str:
        v = _clean_spaces(str(v or ""))
        if not v or len(v) > 80:
            raise ValueError("Название клана должно содержать от 1 до 80 символов")
        return v

    @field_validator("tag")
    @classmethod
    def _tag_valid(cls, v: str) -> str:
        v = str(v or "").strip()
        if not CLAN_TAG_RE.fullmatch(v):
            raise ValueError("Тег клана: 1–16 букв, цифр, _ или -")
        return v

    @field_validator("color")
    @classmethod
    def _color_valid(cls, v: str) -> str:
        v = str(v or "orange").strip().lower()
        if v not in TAG_COLORS:
            raise ValueError("Некорректный цвет клана")
        return v

    @field_validator("description")
    @classmethod
    def _desc_valid(cls, v: str) -> str:
        v = str(v or "").strip()
        if len(v) > 1000:
            raise ValueError("Описание клана длиннее 1000 символов")
        return v


class ClanMemberSet(BaseModel):
    steam_id: str
    member_role: str = "Участник"

    @field_validator("steam_id")
    @classmethod
    def _sid_valid(cls, v: str) -> str:
        if not STEAM_ID_RE.fullmatch(str(v or "")):
            raise ValueError("SteamID64 должен состоять из 17 цифр")
        return str(v)

    @field_validator("member_role")
    @classmethod
    def _role_valid(cls, v: str) -> str:
        if str(v or "Участник") not in CLAN_ROLES:
            raise ValueError("Выберите роль из списка")
        return str(v)


class PlayerClanSet(BaseModel):
    clan_id: str
    member_role: str = "Участник"

    @field_validator("clan_id")
    @classmethod
    def _cid_valid(cls, v: str) -> str:
        if not CLAN_ID_RE.fullmatch(str(v or "")):
            raise ValueError("Некорректный ID клана")
        return str(v)


class PublicClanApplication(BaseModel):
    name: str
    tag: str
    description: str

    @field_validator("name")
    @classmethod
    def _name_valid(cls, v: str) -> str:
        v = _clean_spaces(str(v or ""))
        if not 3 <= len(v) <= 48:
            raise ValueError("Укажите название от 3 до 48 символов")
        return v

    @field_validator("tag")
    @classmethod
    def _tag_valid(cls, v: str) -> str:
        v = "".join(str(v or "").upper().split())
        if not PUBLIC_TAG_RE.fullmatch(v):
            raise ValueError("тег из 2–12 букв или цифр")
        return v

    @field_validator("description")
    @classmethod
    def _desc_valid(cls, v: str) -> str:
        v = str(v or "").strip()
        if not 30 <= len(v) <= 2500:
            raise ValueError("Опишите клан: от 30 до 2500 символов")
        return v


class ClanApplicationResponse(BaseModel):
    ok: bool = True
    ticket_id: str
    discord_delivered: bool = False
    discord_error: Optional[str] = None


class ClanMediaUpload(BaseModel):
    kind: str
    data_url: str

    @field_validator("kind")
    @classmethod
    def _kind_valid(cls, v: str) -> str:
        if str(v or "") not in {"emblem", "cover"}:
            raise ValueError("kind должен быть emblem или cover")
        return str(v)


# ── Тикеты ──────────────────────────────────────────────────────────

class TicketCreate(BaseModel):
    title: str
    description: str = ""
    priority: str = "normal"
    player_steam_id: Optional[str] = None

    @field_validator("title")
    @classmethod
    def _title_valid(cls, v: str) -> str:
        v = _clean_spaces(str(v or ""))
        if not v or len(v) > 160:
            raise ValueError("Заголовок должен содержать от 1 до 160 символов")
        return v

    @field_validator("description")
    @classmethod
    def _desc_valid(cls, v: str) -> str:
        v = str(v or "").strip()
        if len(v) > 5000:
            raise ValueError("Описание длиннее 5000 символов")
        return v

    @field_validator("priority")
    @classmethod
    def _priority_valid(cls, v: str) -> str:
        if str(v or "normal").strip() not in TICKET_PRIORITIES:
            raise ValueError("Некорректный приоритет")
        return str(v).strip()


class TicketUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    player_steam_id: Optional[str] = None
    assigned_to: Optional[str] = None


class TicketCommentCreate(BaseModel):
    body: str

    @field_validator("body")
    @classmethod
    def _body_valid(cls, v: str) -> str:
        v = str(v or "").strip()
        if not v or len(v) > 2000:
            raise ValueError("Комментарий должен содержать от 1 до 2000 символов")
        return v


class TicketCommentResponse(BaseModel):
    ok: bool = True
    comment: dict


class TicketResponse(BaseModel):
    ok: bool = True
    ticket: dict


# ── Ротация ─────────────────────────────────────────────────────────

class RotationEntry(BaseModel):
    map: str
    experiences: list[str] = []
    lighting: str = ""
    zoneAlternator: str = ""

    @field_validator("map")
    @classmethod
    def _map_valid(cls, v: str) -> str:
        v = str(v or "").strip()[:128]
        if not v:
            raise ValueError("у элемента ротации не указана карта")
        return v


class RotationUpdate(BaseModel):
    entries: list[RotationEntry]
    mode: Optional[str] = None

    @field_validator("entries")
    @classmethod
    def _entries_valid(cls, v: list[RotationEntry]) -> list[RotationEntry]:
        if not 1 <= len(v) <= 100:
            raise ValueError("entries должен содержать от 1 до 100 карт")
        return v

    @field_validator("mode")
    @classmethod
    def _mode_valid(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and str(v) not in {"ordered", "random"}:
            raise ValueError("mode должен быть ordered или random")
        return str(v) if v else None


class RotationUpdateResponse(BaseModel):
    ok: bool = True
    rotation: Any = None
    entries: list[dict] = []


# ── Резервные слоты ─────────────────────────────────────────────────

class ReservedSlotsLimit(BaseModel):
    max_reserved_slots: int

    @field_validator("max_reserved_slots")
    @classmethod
    def _limit_valid(cls, v: int) -> int:
        v = int(v)
        if v < 0 or v > 100:
            raise ValueError("Количество резервных слотов должно быть от 0 до 100")
        return v


class ReservedSlotsAdd(BaseModel):
    steam_id: Optional[str] = None
    steamId: Optional[str] = None

    @model_validator(mode="after")
    def _check(self):
        sid = str(self.steam_id or self.steamId or "")
        if not STEAM_ID_RE.fullmatch(sid):
            raise ValueError("SteamID64 должен состоять из 17 цифр")
        return self

    @property
    def steam(self) -> str:
        return str(self.steam_id or self.steamId)


class ReservedSlotsLimitResponse(BaseModel):
    ok: bool = True
    max_reserved_slots: int
    unchanged: bool = False
    result: Optional[dict] = None


class ReservedSlotsListResponse(BaseModel):
    ok: bool = True
    reserved_slots: list[str] = []
    count: int = 0
    max_reserved_slots: Optional[int] = None
    max_players: Optional[int] = None
    limit_supported: bool = False
    add_supported: bool = False
    remove_supported: bool = False
    player_list_live_via_config: bool = False


# ── Броадкаст / баны ────────────────────────────────────────────────

class BroadcastRequest(BaseModel):
    message: str

    @field_validator("message")
    @classmethod
    def _msg_valid(cls, v: str) -> str:
        v = str(v or "").strip()
        if not v or len(v) > 2000:
            raise ValueError("поле message")
        return v


class BanAdd(BaseModel):
    steamId: str
    reason: str = ""
    display_name: str = ""
    expires_utc: Optional[Any] = None

    @field_validator("steamId")
    @classmethod
    def _sid_valid(cls, v: str) -> str:
        if not STEAM_ID_RE.fullmatch(str(v or "")):
            raise ValueError("SteamID64 должен состоять из 17 цифр")
        return str(v)


class BanUpdate(BaseModel):
    reason: Optional[str] = None
    expires_utc: Optional[Any] = None


class BanAddResponse(BaseModel):
    ok: bool = True
    managed_ban: Optional[dict] = None
    mirrored: list[str] = []


class BanUpdateResponse(BaseModel):
    ok: bool = True
    managed_ban: Optional[dict] = None


class BansBulkRequest(BaseModel):
    action: str
    steam_ids: list[str]

    @field_validator("action")
    @classmethod
    def _action_valid(cls, v: str) -> str:
        if str(v or "") != "unban":
            raise ValueError("Поддерживается только снятие банов")
        return str(v)

    @field_validator("steam_ids")
    @classmethod
    def _ids_valid(cls, v: list[str]) -> list[str]:
        ids = list(dict.fromkeys(str(x) for x in v))
        if not ids or len(ids) > 50:
            raise ValueError("Поддерживается снятие от 1 до 50 банов")
        for sid in ids:
            if not STEAM_ID_RE.fullmatch(sid):
                raise ValueError("SteamID64 должен состоять из 17 цифр")
        return ids


class BansBulkResult(BaseModel):
    ok: bool
    succeeded: int
    failed: int
    results: list[dict]


# ── Игроки (actions) ────────────────────────────────────────────────

class KickRequest(BaseModel):
    reason: str = ""


class MessageRequest(BaseModel):
    message: str

    @field_validator("message")
    @classmethod
    def _msg_valid(cls, v: str) -> str:
        v = str(v or "").strip()
        if not v or len(v) > 2000:
            raise ValueError("поле message")
        return v


class PlayerPatchRequest(BaseModel):
    faction: str

    @field_validator("faction")
    @classmethod
    def _faction_valid(cls, v: str) -> str:
        v = str(v or "").strip()
        if not v or len(v) > 80:
            raise ValueError("поле faction")
        return v


class PlayerSwapRequest(BaseModel):
    faction: str

    @field_validator("faction")
    @classmethod
    def _faction_valid(cls, v: str) -> str:
        return str(v or "").strip()


class MatchMapRequest(BaseModel):
    map: str
    experiences: Optional[list[str]] = None
    lighting: str = ""
    zoneAlternator: str = ""

    @field_validator("map")
    @classmethod
    def _map_valid(cls, v: str) -> str:
        v = str(v or "").strip()
        if not v or len(v) > 128:
            raise ValueError("поле map")
        return v


class LightingRequest(BaseModel):
    lighting: str

    @field_validator("lighting")
    @classmethod
    def _lighting_valid(cls, v: str) -> str:
        v = str(v or "").strip()
        if not v or len(v) > 128:
            raise ValueError("поле lighting")
        return v


# ── Discord боты ────────────────────────────────────────────────────

class DiscordBotCreate(BaseModel):
    name: str
    bot_id: str = ""
    guild_id: str = ""
    base_url: str = ""
    scopes: list[str] = []

    @field_validator("name")
    @classmethod
    def _name_valid(cls, v: str) -> str:
        v = str(v or "").strip()[:80]
        if not v:
            raise ValueError("Укажите имя")
        return v


class DiscordBotUpdate(BaseModel):
    name: Optional[str] = None
    guild_id: Optional[str] = None
    bot_user_id: Optional[str] = None
    base_url: Optional[str] = None
    enabled: Optional[bool] = None
    scopes: Optional[list[str]] = None


class DiscordBotCreateResponse(BaseModel):
    ok: bool = True
    item: Optional[dict] = None
    token: str
    token_notice: str


class DiscordBotUpdateResponse(BaseModel):
    ok: bool = True
    item: Optional[dict] = None


class BotTokenRotateResponse(BaseModel):
    ok: bool = True
    token: str
    token_notice: str


class BotHeartbeat(BaseModel):
    detail: str = ""
    version: str = ""
    bot_user_id: str = ""
    metadata: dict = {}

    @field_validator("metadata")
    @classmethod
    def _meta_valid(cls, v: dict) -> dict:
        if not isinstance(v, dict):
            raise ValueError("metadata должен быть объектом")
        return v


class BotHeartbeatResponse(BaseModel):
    ok: bool = True
    bot: Optional[dict] = None
    server_time: float
    api_version: int = 1


class BotEvent(BaseModel):
    event_id: str
    event_type: str
    payload: dict = {}

    @field_validator("event_id")
    @classmethod
    def _eid_valid(cls, v: str) -> str:
        v = str(v or "").strip()[:96]
        if not v:
            raise ValueError("event_id обязателен")
        return v

    @field_validator("event_type")
    @classmethod
    def _etype_valid(cls, v: str) -> str:
        v = str(v or "").strip().lower()[:80]
        if not BOT_EVENT_RE.fullmatch(v):
            raise ValueError("Нужен корректный event_type")
        return v


class BotEventResponse(BaseModel):
    ok: bool = True
    accepted: bool
    duplicate: bool


# ── Роли ────────────────────────────────────────────────────────────

class RolesPut(BaseModel):
    role_ref: str
    perms: list[str] = []

    @field_validator("role_ref")
    @classmethod
    def _ref_valid(cls, v: str) -> str:
        v = str(v or "").strip()[:128]
        if not v:
            raise ValueError("поле role_ref")
        return v


class RolesPutResponse(BaseModel):
    ok: bool = True
    role_ref: str
    perms: list[str] = []


class RoleListItem(BaseModel):
    id: str
    name: str
    perms: list[str] = []
    configured: bool
    legacy: bool


# ── Tempvoice ───────────────────────────────────────────────────────

class TempvoiceTransfer(BaseModel):
    owner_id: str

    @field_validator("owner_id")
    @classmethod
    def _oid_valid(cls, v: str) -> str:
        if not DISCORD_ID_RE.fullmatch(str(v or "")):
            raise ValueError("Некорректный ID владельца")
        return str(v)


# ── Session / Status ────────────────────────────────────────────────

class DiscordStatus(BaseModel):
    bound: bool = False
    discord_id: Optional[str] = None
    discord_name: Optional[str] = None
    member: bool = False
    admin: bool = False


class UserStatus(BaseModel):
    authed: bool
    steam_id: str
    persona: str = ""
    discord: Optional[dict] = None
    permissions: list[str] = []
    admin: bool = False
    all_permissions: list[str] = []
    dev: bool = False


class SessionResponse(BaseModel):
    ok: bool = True
    authed: bool
    refresh: bool
    me: Optional[dict] = None
    servers: list[dict] = []
    auth_configured: bool
    steam_configured: bool
    dev_auth_enabled: bool = False


class ServiceStatus(BaseModel):
    id: str
    ok: bool
    detail: str = ""
    name: Optional[str] = None


class SystemStatusResponse(BaseModel):
    ok: bool
    services: list[dict] = []
    history: list[dict] = []


class ProfileResponse(BaseModel):
    ok: bool = True
    me: dict
    binding: Optional[dict] = None
    steam: dict
    activity: Optional[dict] = None
    vip_slots: list[dict] = []
    tickets: list[dict] = []
    tickets_total: int = 0
    audit: list[dict] = []
    server_names: dict = {}


# ── Server overview / stats ─────────────────────────────────────────

class OverviewResponse(BaseModel):
    ok: bool
    server: dict
    error: Optional[dict] = None
    status: Any = None
    players: Any = None
    players_visible: bool = False
    player_count: Optional[int] = None
    rotation: Any = None
    bans: Any = None
    bans_count: Optional[int] = None
    health: Any = None
    ts: float


class PlayersListResponse(BaseModel):
    ok: bool = True
    count: int = 0
    players: list[dict] = []


class CatalogResponse(BaseModel):
    ok: bool = True
    maps: dict = {}
    lightings: list = []


class StatsPlayersResponse(BaseModel):
    ok: bool = True
    sort: str
    players: list[dict] = []


class PopulationResponse(BaseModel):
    ok: bool = True
    hours: int
    samples: list[dict] = []


# ── Discord overview ────────────────────────────────────────────────

class DiscordOverviewResponse(BaseModel):
    ok: bool = True
    bindings: list[dict] = []
    diagnostics: list[dict] = []
    voice_channels: list[dict] = []
    tempvoice: dict
    tempvoice_error: Optional[str] = None
    actions: list[dict] = []
    bots: list[dict] = []
    summary: dict


# ── Staff ───────────────────────────────────────────────────────────

class StaffResponse(BaseModel):
    ok: bool = True
    count: int
    staff: list[dict] = []


# ── Bots registry ───────────────────────────────────────────────────

class BotsListResponse(BaseModel):
    ok: bool = True
    items: list[dict] = []
    capacity: int = 64
    scopes: list[str] = []


# ── Audit ───────────────────────────────────────────────────────────

class AuditListResponse(BaseModel):
    ok: bool = True
    entries: list[dict] = []


class AuditIntegrityResponse(BaseModel):
    ok: bool = True
    audit: dict


class ServerAuditResponse(BaseModel):
    ok: bool = True
    entries: list[dict] = []


# ── VIP history ─────────────────────────────────────────────────────

class VipHistoryResponse(BaseModel):
    ok: bool = True
    items: list[dict] = []


# ── Server config ───────────────────────────────────────────────────

class ServerConfigResponse(BaseModel):
    ok: bool = True
    revision: Optional[str] = None
    writable: Optional[bool] = None
    text: Optional[str] = None
    warnings: list = []


# ── Routes ──────────────────────────────────────────────────────────

class ServerRoutesResponse(BaseModel):
    ok: bool = True
    routes: list[str] = []
    config_writable: bool
    build: str = ""
    project: dict = {}
    engine: dict = {}
    feed: dict = {}


# ── Role preview ────────────────────────────────────────────────────

class RolePreviewResponse(BaseModel):
    ok: bool = True
    steam_id: str
    discord_id: Optional[str] = None
    discord_name: Optional[str] = None
    roles: list[dict] = []
    permissions: list[str] = []


# ── Notes list ──────────────────────────────────────────────────────

class NotesListResponse(BaseModel):
    ok: bool = True
    notes: list[dict] = []
    can_edit: bool = True


# ── Public ──────────────────────────────────────────────────────────

class PublicClansResponse(BaseModel):
    ok: bool = True
    clans: list[dict] = []


class PublicClanResponse(BaseModel):
    ok: bool = True
    clan: dict


class PublicClanSyncResponse(BaseModel):
    ok: bool = True
    clans: int


# ── Rotation helpers (config text) ──────────────────────────────────

class RotationConfigResponse(BaseModel):
    ok: bool = True
    rotation: Any = None
    entries: list[dict] = []

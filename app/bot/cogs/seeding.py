"""Сидинг WARDOGS: баллы за voice+RCON, VIP-магазин, weekly top-3, donation feed.

Полный порт bots/seeding-bot/src/ на Python/discord.py.
RCON-клиент в seeding_rcon.py, таблицы/CRUD в database.py (seed_*).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import logging
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from database import (
    SeedDomainError,
    get_panel_conn,
    seed_activate_vip,
    seed_add_time,
    seed_add_time_all,
    seed_adjust_points,
    seed_apply_seeding_tick,
    seed_audit,
    seed_cancel_link_challenge,
    seed_get_donor_by_discord,
    seed_get_leaderboard,
    seed_get_state,
    seed_get_user_by_discord,
    seed_get_user_by_id,
    seed_get_user_by_steam,
    seed_get_vip_identity_by_discord,
    seed_get_vip_identity_by_id,
    seed_get_vip_membership,
    seed_grant_points_all,
    seed_hash_code,
    seed_is_inside_window,
    seed_list_seeders,
    seed_list_users,
    seed_list_vip_memberships,
    seed_mark_report_published,
    seed_prepare_link_challenge,
    seed_previous_week_key,
    seed_purchase_vip,
    seed_report_published,
    seed_reset_sessions,
    seed_set_state,
    seed_set_vip_error,
    seed_set_vip_ownership,
    seed_set_vip_state,
    seed_unlink_discord,
    seed_unlink_donor,
    seed_verify_link_challenge,
    seed_week_key,
)
from seeding_rcon import RconError, WardogsRconClient

log = logging.getLogger("seeding")

MSK = timezone(timedelta(hours=3))
STEAM_ID_RE = re.compile(r"^7656119\d{10}$")
SNOWFLAKE_RE = re.compile(r"^\d{17,20}$")
CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
CODE_RE = re.compile(r"^[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{8}$")

VIP_CATALOG = {
    "1d": {"sku": "1d", "label": "VIP на 1 день", "cost_attr": "VIP_1_DAY_COST", "duration_seconds": 86_400,
           "duration_text": "24 часа", "shop_label": "VIP 1 день", "shop_emoji": "🎟️",
           "shop_style": discord.ButtonStyle.primary},
    "7d": {"sku": "7d", "label": "VIP на 7 дней", "cost_attr": "VIP_7_DAY_COST", "duration_seconds": 604_800,
           "duration_text": "7 суток", "shop_label": "VIP 7 дней", "shop_emoji": "🎟️",
           "shop_style": discord.ButtonStyle.primary},
    "30d": {"sku": "30d", "label": "VIP на 30 дней", "cost_attr": "VIP_30_DAY_COST", "duration_seconds": 2_592_000,
            "duration_text": "30 суток", "shop_label": "VIP 30 дней", "shop_emoji": "💎",
            "shop_style": discord.ButtonStyle.success},
}


class SeedShopView(discord.ui.View):
    """Постоянная панель магазина: кнопки VIP покупают через SeedingCog._vip_purchase."""

    def __init__(self):
        super().__init__(timeout=None)
        for sku, product in VIP_CATALOG.items():
            button = discord.ui.Button(
                custom_id=f"seed:vip:{sku}",
                label=product["shop_label"],
                emoji=product["shop_emoji"],
                style=product["shop_style"],
            )
            button.callback = self._make_callback(sku)
            self.add_item(button)

    @staticmethod
    def _make_callback(sku: str):
        async def _callback(interaction: discord.Interaction):
            cog = interaction.client.get_cog("SeedingCog")
            if cog is None:
                await interaction.response.send_message("Магазин временно недоступен.", ephemeral=True)
                return
            await cog._vip_purchase(interaction, sku)
        return _callback

DONATION_PRODUCTS = {
    "personal_1": {"kind": "personal", "amount": 50000, "seats": 1, "months": 1},
    "personal_3": {"kind": "personal", "amount": 120000, "seats": 1, "months": 3},
    "personal_12": {"kind": "personal", "amount": 480000, "seats": 1, "months": 12},
    "clan_10": {"kind": "clan", "amount": 450000, "seats": 10, "months": 1},
    "clan_20": {"kind": "clan", "amount": 850000, "seats": 20, "months": 1},
    "clan_30": {"kind": "clan", "amount": 1200000, "seats": 30, "months": 1},
}


def _parse_clock(text: str, default_minutes: int) -> int:
    try:
        h, m = text.strip().split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return default_minutes


def _now_ms() -> int:
    return int(time.time() * 1000)


def _panel_vip_active(steam_id: str) -> bool:
    """Активный VIP-слот панели (vip_slots) для SteamID64."""
    if not steam_id:
        return False
    try:
        panel_db = get_panel_conn()
        row = panel_db.execute(
            "SELECT 1 FROM vip_slots WHERE steam_id = ? AND (expires_utc IS NULL OR expires_utc > ?) LIMIT 1",
            (str(steam_id), time.time()),
        ).fetchone()
        return bool(row)
    except Exception:
        log.exception("panel vip check failed for %s", steam_id)
        return False


def _panel_vip_active_by_discord(discord_id: str) -> bool:
    """Активный VIP-слот панели для Discord: bindings -> seed_users/donors -> vip_slots."""
    if not discord_id:
        return False
    try:
        panel_db = get_panel_conn()
        row = panel_db.execute(
            "SELECT user_id FROM bindings WHERE discord_id = ?", (str(discord_id),)
        ).fetchone()
        if row:
            return _panel_vip_active(row["user_id"])
        seeder_user = seed_get_user_by_discord(str(discord_id))
        if seeder_user and seeder_user.get("steam_id") and _panel_vip_active(seeder_user["steam_id"]):
            return True
        donor = seed_get_donor_by_discord(str(discord_id))
        return bool(donor and donor.get("steam_id") and _panel_vip_active(donor["steam_id"]))
    except Exception:
        log.exception("panel vip check failed for discord %s", discord_id)
        return False


def _fmt_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    if hours:
        return f"{hours} ч {minutes:02d} мин"
    return f"{minutes} мин"


def _human_code(length: int = 8) -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


def _calendar_months(start_ms: int, months: int) -> int:
    dt = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    day = dt.day
    month = dt.month - 1 + months
    year = dt.year + month // 12
    month = month % 12 + 1
    import calendar as _cal

    last = _cal.monthrange(year, month)[1]
    dt = dt.replace(year=year, month=month, day=min(day, last))
    return int(dt.timestamp() * 1000)


def _validated_vip_event(event: dict, secret: str) -> dict | None:
    if not event:
        return None
    op_id = event.get("operationId") or ""
    order_id = event.get("orderId") or ""
    if not re.match(r"^[A-Za-z0-9-]{1,80}$", op_id):
        return None
    if not re.match(r"^[a-f0-9]{32}$", order_id):
        return None
    if event.get("currency") != "643":
        return None
    gross = event.get("grossKopecks")
    ts = event.get("timestamp")
    token = event.get("orderToken") or ""
    if not isinstance(gross, int) or not isinstance(ts, int) or not isinstance(token, str) or len(token) > 2048:
        return None
    m = re.match(r"^([A-Za-z0-9_-]+)\.([a-f0-9]{64})$", token)
    if not m:
        return None
    payload_b64, sig_hex = m.group(1), m.group(2)
    expected = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).digest()
    try:
        actual = bytes.fromhex(sig_hex)
    except ValueError:
        return None
    if not hmac.compare_digest(actual, expected):
        return None
    try:
        import base64

        padding = "=" * (-len(payload_b64) % 4)
        order = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
    except Exception:
        return None
    product = DONATION_PRODUCTS.get(order.get("productId") or "")
    if not product:
        return None
    if (
        order.get("id") != order_id
        or order.get("kind") != product["kind"]
        or order.get("amountKopecks") != product["amount"]
        or gross != product["amount"]
        or not SNOWFLAKE_RE.match(str(order.get("buyerId") or ""))
        or not isinstance(order.get("createdAt"), int)
        or not isinstance(order.get("expiresAt"), int)
        or order.get("expiresAt") != order.get("createdAt") + 7_200_000
        or not isinstance(order.get("recipients"), list)
        or len(order["recipients"]) != product["seats"]
        or any(not SNOWFLAKE_RE.match(str(x)) for x in order["recipients"])
        or len(set(order["recipients"])) != len(order["recipients"])
        or (product["kind"] == "personal" and order["recipients"][0] != order.get("buyerId"))
        or ts * 1000 < order.get("createdAt") - 60_000
        or ts * 1000 > order.get("expiresAt") + 86_400_000
    ):
        return None
    return {
        "orderId": order["id"],
        "operationId": op_id,
        "recipients": order["recipients"],
        "months": product["months"],
        "paidAt": ts * 1000,
    }


class ReservedProtectionService:
    STATE_KEY = "protected_reserved_slots_v1"

    @staticmethod
    def snapshot() -> dict:
        state = seed_get_state(ReservedProtectionService.STATE_KEY, {"version": 1, "entries": []})
        if not state or state.get("version") != 1 or not isinstance(state.get("entries"), list):
            return {"version": 1, "entries": []}
        return state

    @staticmethod
    def list_entries() -> list[dict]:
        return sorted(ReservedProtectionService.snapshot()["entries"], key=lambda e: e.get("protectedAt", 0))

    @staticmethod
    def count() -> int:
        return len(ReservedProtectionService.snapshot()["entries"])

    @staticmethod
    def is_protected(user_id: str, steam_id: str = "") -> bool:
        for e in ReservedProtectionService.snapshot()["entries"]:
            if e.get("userId") == user_id or (steam_id and e.get("steamId") == steam_id):
                return True
        return False

    @staticmethod
    def protect(user: dict, actor_id: str):
        now = _now_ms()
        state = ReservedProtectionService.snapshot()
        matches = [
            e
            for e in state["entries"]
            if e.get("userId") == user.get("id")
            or e.get("discordId") == user.get("discord_id")
            or e.get("steamId") == user.get("steam_id")
        ]
        previous = min(matches, key=lambda e: e.get("protectedAt", 0)) if matches else None
        entry = {
            "userId": user.get("id"),
            "discordId": user.get("discord_id"),
            "steamId": user.get("steam_id"),
            "displayName": str(user.get("display_name") or "Игрок")[:100],
            "protectedAt": previous.get("protectedAt") if previous else now,
        }
        state["entries"] = [
            e
            for e in state["entries"]
            if e.get("userId") != user.get("id")
            and e.get("discordId") != user.get("discord_id")
            and e.get("steamId") != user.get("steam_id")
        ]
        state["entries"].append(entry)
        seed_set_state(ReservedProtectionService.STATE_KEY, state)
        seed_audit("reserved_slot_protected", actor_id, {"user_id": user.get("id"), "created": previous is None})
        return entry, previous is None

    @staticmethod
    def unprotect_by_discord(discord_id: str, actor_id: str):
        state = ReservedProtectionService.snapshot()
        entry = next((e for e in state["entries"] if e.get("discordId") == discord_id), None)
        if not entry:
            return None, False
        state["entries"] = [e for e in state["entries"] if e.get("discordId") != discord_id]
        seed_set_state(ReservedProtectionService.STATE_KEY, state)
        seed_audit("reserved_slot_unprotected", actor_id, {"user_id": entry.get("userId")})
        return entry, True


class DonationFeedService:
    def __init__(self):
        self.state = seed_get_state("donation_feed", {"offset": 0, "orders": {}, "vip": {}})
        if not isinstance(self.state, dict):
            self.state = {"offset": 0, "orders": {}, "vip": {}}
        self.state.setdefault("offset", 0)
        self.state.setdefault("orders", {})
        self.state.setdefault("vip", {})
        self.last_success = 0.0
        self.running = False

    @property
    def enabled(self) -> bool:
        return bool(config.DONATION_SITE_BASE_URL and config.DONATION_FEED_TOKEN and config.DONATION_CHECKOUT_SECRET)

    def until(self, discord_id: str) -> int | None:
        if not self.enabled:
            return None
        return (self.state["vip"].get(str(discord_id)) or {}).get("endsAt")

    def active(self, discord_id: str, now: int | None = None) -> bool:
        if not self.enabled:
            return False
        now = now if now is not None else _now_ms()
        return int((self.state["vip"].get(str(discord_id)) or {}).get("endsAt") or 0) > now

    def recipient_ids(self) -> list[str]:
        return list(self.state["vip"].keys()) if self.enabled else []

    def active_recipient_ids(self, now: int | None = None) -> list[str]:
        now = now if now is not None else _now_ms()
        return [i for i in self.recipient_ids() if self.active(i, now)]

    def can_remove(self, now: int | None = None) -> bool:
        now = now if now is not None else _now_ms()
        return (not self.enabled) or (now / 1000 - self.last_success) < max(180, config.DONATION_POLL_SECONDS_SEED * 3)

    def _apply(self, event: dict) -> bool:
        grant = _validated_vip_event(event, config.DONATION_CHECKOUT_SECRET)
        if not grant or grant["orderId"] in self.state["orders"]:
            return False
        self.state["orders"][grant["orderId"]] = grant["operationId"]
        for discord_id in grant["recipients"]:
            starts_at = max(grant["paidAt"], int((self.state["vip"].get(discord_id) or {}).get("endsAt") or 0))
            self.state["vip"][discord_id] = {
                "endsAt": _calendar_months(starts_at, grant["months"]),
                "orderId": grant["orderId"],
            }
        return True

    async def refresh(self, session: aiohttp.ClientSession) -> int:
        if not self.enabled or self.running:
            return 0
        self.running = True
        granted = 0
        try:
            base = config.DONATION_SITE_BASE_URL.rstrip("/")
            for _page in range(50):
                url = f"{base}/donate/feed.php?offset={self.state['offset']}"
                async with session.get(
                    url,
                    headers={"X-WD-Feed-Token": config.DONATION_FEED_TOKEN},
                    timeout=aiohttp.ClientTimeout(total=12),
                    allow_redirects=False,
                ) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"Donation feed HTTP {resp.status}")
                    raw = await resp.text()
                    if len(raw) > 1_000_000:
                        raise RuntimeError("Donation feed response is too large")
                    batch = json.loads(raw)
                    events = batch.get("events")
                    next_offset = batch.get("nextOffset")
                    if (
                        not isinstance(events, list)
                        or len(events) > 100
                        or not isinstance(next_offset, int)
                        or next_offset < self.state["offset"]
                        or (events and next_offset == self.state["offset"])
                    ):
                        raise RuntimeError("Invalid donation feed cursor")
                    for event in events:
                        if self._apply(event):
                            granted += 1
                    self.state["offset"] = next_offset
                    seed_set_state("donation_feed", self.state)
                    if len(events) < 100:
                        self.last_success = time.time()
                        return granted
            return granted
        finally:
            self.running = False


class SeedingCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.rcon: WardogsRconClient | None = None
        self.rcon2: WardogsRconClient | None = None
        self.donations = DonationFeedService()
        self._seed_running = False
        self._vip_running = False
        self._weekly_running = False
        self._role_running = False
        self._report_running = False
        self._gather_running = False
        self._last_status: dict = {"state": "starting", "updatedAt": _now_ms(), "servers": []}
        self._gather_sessions: dict = seed_get_state("seeding_gathers", {}) or {}
        self._gather_queue: asyncio.Lock = asyncio.Lock()
        seed_reset_sessions()
        self._start_loops()

    def _start_loops(self):
        if not self.seed_loop.is_running():
            self.seed_loop.start()
        if not self.vip_loop.is_running():
            self.vip_loop.start()
        if not self.weekly_loop.is_running():
            self.weekly_loop.start()
        if not self.role_loop.is_running():
            self.role_loop.start()
        if not self.report_loop.is_running():
            self.report_loop.start()
        if not self.donation_loop.is_running():
            self.donation_loop.start()
        if not self.gather_loop.is_running():
            self.gather_loop.start()
        if not self.backup_loop.is_running():
            self.backup_loop.start()

    async def cog_load(self):
        self.rcon = self._make_rcon(config.SEED_RCON_URL, config.SEED_RCON_PASSWORD, "main")
        self.rcon2 = (
            self._make_rcon(config.SEED_RCON_2_URL, config.SEED_RCON_2_PASSWORD, "second")
            if config.SEED_RCON_2_URL and config.SEED_RCON_2_PASSWORD
            else None
        )
        # Warm-up capabilities (non-fatal)
        asyncio.create_task(self._warmup_rcon())
        # Правила/гайд/магазин/рейтинг — авто-pub на старте.
        asyncio.create_task(self._publish_rules_panel())
        asyncio.create_task(self._publish_shop_panel())

    async def _publish_rules_panel(self) -> str:
        """4 эмбеда в канале правил: правила, как начать, магазин, рейтинг.
        Апсерт по сохранённому message_id — не плодит дубли."""
        await self.bot.wait_until_ready()
        if not config.SEED_RULES_CHANNEL_ID:
            return "правила: канал не настроен"
        guild = self.bot.get_guild(config.GUILD_ID)
        if guild is None:
            return "правила: гильдия не найдена"
        try:
            ch = guild.get_channel(config.SEED_RULES_CHANNEL_ID)
            if ch is None:
                ch = await guild.fetch_channel(config.SEED_RULES_CHANNEL_ID)
        except Exception as e:
            log.warning("Rules channel %s недоступен: %s", config.SEED_RULES_CHANNEL_ID, e)
            return f"правила: канал недоступен ({e})"
        if not isinstance(ch, discord.TextChannel):
            return "правила: это не текстовый канал"

        voice = f"<#{config.SEED_VOICE_CHANNEL_ID}>" if config.SEED_VOICE_CHANNEL_ID else "голосовой канал сидинга"
        shop = f"<#{config.SEED_SHOP_CHANNEL_ID}>" if config.SEED_SHOP_CHANNEL_ID else "канал магазина"
        rank = f"<#{config.SEED_RANK_CHANNEL_ID}>" if config.SEED_RANK_CHANNEL_ID else "канал рейтинга"
        rules_ch = f"<#{config.SEED_RULES_CHANNEL_ID}>"
        seeder_role = f"<@&{config.SEEDER_ROLE_ID}>" if config.SEEDER_ROLE_ID else "роль сидера"
        vip_role = f"<@&{config.VIP_ROLE_ID_SEED}>" if config.VIP_ROLE_ID_SEED else "VIP-роль"
        super_role = f"<@&{config.SUPER_SEEDER_ROLE_ID}>" if config.SUPER_SEEDER_ROLE_ID else "роль супер-сидера"
        start_h = config.SEED_START_TIME or "06:00"
        end_h = config.SEED_END_TIME or "00:00"
        stop_n = getattr(config, "SEED_STOP_PLAYER_COUNT", 60)
        resume_min = getattr(config, "SEED_RESUME_BELOW_SECONDS", 600) // 60
        grace_min = getattr(config, "SEED_SESSION_GRACE_SECONDS", 300) // 60
        vip_1 = getattr(config, "VIP_1_DAY_COST", 40)
        vip_7 = getattr(config, "VIP_7_DAY_COST", 200)
        vip_30 = getattr(config, "VIP_30_DAY_COST", 720)

        no_mention = discord.AllowedMentions.none()

        # 1. Правила
        rules = discord.Embed(color=0xE74C3C, title="📜 Правила сидинга WARDOGS | RU",
                              description="Помогайте серверу набрать онлайн, получайте баллы и обменивайте их на игровые привилегии.\n\nПопытки обхода правил, подмена аккаунта или передача учётной записи могут привести к проверке и отмене начислений.")
        rules.add_field(name="⏰ Когда начисляются баллы",
                        value=f"Ежедневно с **{start_h} до {end_h} МСК**. Вне этого времени сидинг не учитывается.")
        rules.add_field(name="🎮 Что нужно для участия",
                        value=f"Одновременно находиться на одном из двух серверов WARDOGS и в голосовом канале {voice} с привязанным SteamID64. Время и баллы общие; одновременное присутствие не удваивает начисление.")
        rules.add_field(name="⭐ Начисление",
                        value="Первый непрерывный час — **1 балл каждые 6 минут** (до 10 баллов).\nСо второго часа — **1 балл каждые 3 минуты** (20 баллов в час).")
        rules.add_field(name="🧭 Баллы за обычную игру",
                        value=f"Круглосуточно на обоих серверах, без Discord и голосового канала. Первый час — **1 балл за 12 минут** (5/час), затем **1 балл за 6 минут** (10/час). "
                              f"Если выполнены условия сидинга, начисляется только сидинг. При **{stop_n}+** игроков действует обычная игра.")
        rules.add_field(name="👝 Общий баланс магазина",
                        value="Баллы игры сохраняются по SteamID без привязки и становятся доступны после `/steam link` и `/steam verify`. "
                              "Баллы сидинга и игры можно тратить в одном магазине. В недельный рейтинг и награды топ-3 входит только сидинг.")
        rules.add_field(name="⏸️ Когда счётчик приостанавливается",
                        value=f"На каждом сервере отдельно при онлайне **{stop_n}+ игроков**. Возобновление — после {resume_min} минут ниже порога.\nТехническая пауза до {grace_min} минут сохраняет стаж непрерывной сессии, но время паузы не оплачивается.")
        rules.set_footer(text="Баллы начисляются только после проверки Discord и игрового сервера")

        # 2. Как начать
        start = discord.Embed(color=0x2ECC71, title="🚀 Как начать сидинг — пошагово",
                              description=f"**Все команды можно выполнять прямо в этом канале.** Начните вводить `/seed`, затем выберите нужный пункт из меню Discord.\n\n"
                                          f"**1. Зайдите на любой из двух игровых серверов WARDOGS.**\nОставайтесь в игре до завершения привязки — бот должен увидеть ваш SteamID среди игроков.\n\n"
                                          f"**2. Подготовьте SteamID64.**\nЭто 17 цифр, обычно начинающихся с `7656`. Укажите именно SteamID64, а не ссылку на профиль и не игровой ник.\n\n"
                                          f"**3. Выполните `/steam link`.**\nВ поле `steam_id` вставьте SteamID64. Бот отправит одноразовый восьмизначный код личным сообщением внутри игры.\n\n"
                                          f"**4. Выполните `/steam verify`.**\nВ поле `code` введите код из игры. После успешной проверки вы получите роль {seeder_role}; действующий VIP выдаётся сразу и не слетает.\n\n"
                                          f"**5. Перейдите в голосовой канал {voice}.**\nПока вы одновременно в игре на одном из двух серверов и в этом голосовом канале, бот считает общее время по правилам выше.\n\n"
                                          f"**6. Проверьте результат.**\nКоманда `/steam status` показывает баланс, время, состояние сидинга и VIP. `/seed top` показывает текущий рейтинг.\n\n"
                                          f"**Нужен только оплаченный VIP?** Та же привязка `/steam link` + `/steam verify` — она подходит и для оплаченного VIP (без начисления баллов, если вы не сидер). Отдельная `/vip-link` осталась как запасной вариант.\n\n"
                                          f"**Отвязка:** `/steam unlink confirm:true` (или `/vip-link unlink` для отдельной VIP-привязки). При действующем VIP отвязка недоступна; история сидера сохраняется.")
        start.set_footer(text="Обычные сообщения в этом канале автоматически удаляются; slash-команды работают")

        # 3. Магазин
        rewards = discord.Embed(color=0xF1C40F, title="👑 Магазин наград и VIP",
                                description=f"Откройте {shop} и нажмите кнопку нужной награды. Покупка и ответ бота видны только вам.")
        rewards.add_field(name="🎫 VIP на 1 день", value=f"**{vip_1} баллов**", inline=True)
        rewards.add_field(name="🎟️ VIP на 7 дней", value=f"**{vip_7} баллов**", inline=True)
        rewards.add_field(name="💎 VIP на 30 дней", value=f"**{vip_30} баллов**", inline=True)
        rewards.add_field(name="Что входит",
                          value=f"Роль {vip_role} и Reserved slot на обоих серверах. Срок начинается после подтверждённой активации слота на обоих.")
        rewards.add_field(name="🔒 Уже есть постоянный слот?",
                          value="Магазин не спишет баллы за дублирующий VIP. Защищённый администрацией Reserved slot сохраняется независимо от VIP и рейтинга.")
        rewards.set_footer(text="Баланс и статус: /seed status")

        # 4. Рейтинг
        ranking = discord.Embed(color=0x3498DB, title="🏆 Еженедельный рейтинг и награды",
                                description=f"Рейтинг в {rank} строится по подтверждённому времени сидинга за текущую неделю и обновляется автоматически.")
        ranking.add_field(name="🥇🥈🥉 Награда топ-3",
                          value=f"Три лучших сидера завершившейся недели получают роль {super_role} и бесплатный Reserved slot на обоих серверах.")
        ranking.add_field(name="📅 Срок награды",
                          value="Неделя считается с понедельника 00:00 до следующего понедельника 00:00 МСК. Роль и слот действуют до следующего обновления, затем бот передаёт их новой тройке лидеров.")
        ranking.add_field(name="📊 Две карточки рейтинга",
                          value="Первая показывает живой рейтинг текущей недели, вторая — трёх победителей прошлой недели и состояние их наград.")
        ranking.add_field(name="🔐 Постоянные Reserved slots",
                          value="Слоты на обоих серверах, отдельно защищённые администрацией, не снимаются при окончании VIP или при смене победителей рейтинга.")
        ranking.set_footer(text="Чем больше подтверждённого времени — тем выше место")

        payload = {"embeds": [rules, start, rewards, ranking], "allowed_mentions": no_mention}
        prior = seed_get_state("rules_message_id", "")
        if prior:
            try:
                msg = await ch.fetch_message(int(prior))
                if msg.author.id == self.bot.user.id:
                    await msg.edit(**payload)
                    log.info("Rules-панель обновлена на старте")
                    return "правила: обновлён"
            except Exception:
                pass
        try:
            msg = await ch.send(**payload)
            seed_set_state("rules_message_id", str(msg.id))
            log.info("Rules-панель опубликована на старте")
            return "правила: опубликован"
        except Exception as e:
            log.warning("Не удалось опубликовать rules-панель: %s", e)
            return f"правила: ошибка ({e})"

    async def _publish_shop_panel(self) -> str:
        """Панель магазина в SEED_SHOP_CHANNEL: эмбед + постоянные кнопки VIP.
        Апсерт по сохранённому message_id — не плодит дубли."""
        await self.bot.wait_until_ready()
        if not config.SEED_SHOP_CHANNEL_ID:
            return "магазин: канал не настроен"
        guild = self.bot.get_guild(config.GUILD_ID)
        if guild is None:
            return "магазин: гильдия не найдена"
        try:
            ch = guild.get_channel(config.SEED_SHOP_CHANNEL_ID)
            if ch is None:
                ch = await guild.fetch_channel(config.SEED_SHOP_CHANNEL_ID)
        except Exception as e:
            log.warning("Shop channel %s недоступен: %s", config.SEED_SHOP_CHANNEL_ID, e)
            return f"магазин: канал недоступен ({e})"
        if not isinstance(ch, discord.TextChannel):
            return "магазин: это не текстовый канал"

        vip_role = f"<@&{config.VIP_ROLE_ID_SEED}>" if config.VIP_ROLE_ID_SEED else "VIP-роль"
        embed = discord.Embed(
            color=0xF1C40F,
            title="👑 Магазин наград WARDOGS | RU",
            description="Обменивайте общий баланс баллов за сидинг и обычную игру на VIP: Discord-роль и Reserved slot на обоих серверах.",
        )
        for sku, product in VIP_CATALOG.items():
            cost = getattr(config, product["cost_attr"])
            embed.add_field(
                name=f"{product['shop_emoji']} {product['label']} • {cost} баллов",
                value=f"Роль {vip_role} и Reserved slot на обоих серверах на {product['duration_text']} после подтверждённой активации.",
                inline=False,
            )
        embed.add_field(
            name="ℹ️ Как купить",
            value="1. Подтвердите Steam через `/steam link` и `/steam verify`. Накопленные за игру баллы станут доступны; проверьте общий баланс через `/steam status`.\n"
                  "2. Нажмите кнопку нужного тарифа ниже.\n"
                  "3. Бот выдаст VIP-роль и синхронизирует Reserved slot на обоих серверах.\n\n"
                  "Срок VIP начинается только после появления SteamID в live reserved slots обоих серверов. Покупка и ответ бота видны только вам.",
            inline=False,
        )
        embed.set_footer(text="WARDOGS | RU • Система сидинга • Баланс и статус: /seed status")

        view = SeedShopView()
        prior = seed_get_state("shop_message_id", "")
        if prior:
            try:
                msg = await ch.fetch_message(int(prior))
                if msg.author.id == self.bot.user.id:
                    await msg.edit(embed=embed, view=view)
                    log.info("Панель магазина обновлена")
                    return "магазин: обновлён"
            except Exception:
                pass
        try:
            msg = await ch.send(embed=embed, view=view)
            seed_set_state("shop_message_id", str(msg.id))
            log.info("Панель магазина опубликована")
            return "магазин: опубликован"
        except Exception as e:
            log.warning("Не удалось опубликовать панель магазина: %s", e)
            return f"магазин: ошибка ({e})"

    async def cog_unload(self):
        for loop in (
            self.seed_loop,
            self.vip_loop,
            self.weekly_loop,
            self.role_loop,
            self.report_loop,
            self.donation_loop,
            self.gather_loop,
            self.backup_loop,
        ):
            if loop.is_running():
                loop.cancel()
        if self.rcon:
            await self.rcon.close()
        if self.rcon2:
            await self.rcon2.close()

    def _make_rcon(self, url: str, password: str, label: str) -> WardogsRconClient | None:
        if not url or not password:
            log.warning("RCON %s не настроен (SEED_RCON_URL/SEED_RCON_PASSWORD)", label)
            return None
        return WardogsRconClient(
            url,
            password,
            timeout_ms=config.SEED_RCON_TIMEOUT_MS,
            retry_attempts=config.SEED_RCON_RETRY,
            label=label,
        )

    async def _warmup_rcon(self):
        for client in (self.rcon, self.rcon2):
            if not client:
                continue
            try:
                caps = await client.capabilities()
                log.info("RCON %s capabilities: %d routes", client.label, len(caps.get("routes", [])))
            except Exception as e:
                log.warning("RCON %s warmup failed: %s", client.label, e)

    def _servers(self) -> list[tuple[str, str, WardogsRconClient]]:
        out = []
        if self.rcon:
            out.append(("main", "Сервер 1", self.rcon))
        if self.rcon2:
            out.append(("second", "Сервер 2", self.rcon2))
        return out

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if not interaction.data:
            return
        custom_id = interaction.data.get("custom_id", "")
        if not custom_id.startswith("seed:gather:connect:"):
            return
        parts = custom_id.split(":")
        # seed:gather:connect:<serverId>:<sessionId>
        if len(parts) < 5:
            return
        server_id, session_id = parts[3], parts[4]
        sessions = seed_get_state("seeding_gathers", {}) or {}
        session = sessions.get(server_id)
        if not session:
            embed = discord.Embed(color=0x3498DB, title="🎮 Подключение",
                                  description="Этот сбор уже завершён. Свяжитесь с администрацией для уточнения.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        await self._send_gather_instructions(interaction, session)

    # ---------- Seed tick loop ----------

    @tasks.loop(seconds=max(15, config.SEED_TICK_SECONDS))
    async def seed_loop(self):
        if self._seed_running:
            return
        self._seed_running = True
        try:
            await self._seed_tick()
        except Exception as e:
            log.exception("seed_tick_failed: %s", e)
        finally:
            self._seed_running = False

    @seed_loop.before_loop
    async def _before_seed_loop(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(5)

    async def _seed_tick(self):
        now = _now_ms()
        start_m = _parse_clock(config.SEED_START_TIME, 8 * 60)
        end_m = _parse_clock(config.SEED_END_TIME, 22 * 60)
        if not seed_is_inside_window(now, start_m, end_m):
            seed_apply_seeding_tick([], now, seed_week_key(now), config.SEED_SESSION_GRACE_SECONDS, config.SEED_TICK_SECONDS * 2)
            self._last_status = {"state": "closed", "updatedAt": now, "servers": [], "eligibleCount": 0}
            return

        active_servers: list[dict] = []
        all_snapshots: list[dict] = []
        for sid, label, client in self._servers():
            try:
                players, status = await asyncio.gather(client.players(), client.status())
                population = max(len(players), status["player_count"])
                gate_key = "population_gate" if sid == "main" else "population_gate_second"
                gate = seed_get_state(gate_key, {"blocked": False, "belowSince": None}) or {"blocked": False, "belowSince": None}
                if population >= config.SEED_STOP_PLAYER_COUNT:
                    gate = {"blocked": True, "belowSince": None}
                elif gate.get("blocked"):
                    if gate.get("belowSince") is None:
                        gate["belowSince"] = now
                    if now - gate["belowSince"] >= config.SEED_RESUME_BELOW_SECONDS * 1000:
                        gate = {"blocked": False, "belowSince": None}
                else:
                    gate["belowSince"] = None
                seed_set_state(gate_key, gate)
                snap = {
                    "id": sid,
                    "label": label,
                    "state": "population_limit" if gate.get("blocked") else "active",
                    "population": population,
                    "maxPlayers": status.get("max_players"),
                    "players": players,
                }
                all_snapshots.append(snap)
                if snap["state"] == "active":
                    active_servers.append(snap)
            except Exception as e:
                log.warning("RCON poll failed %s: %s", sid, e)
                all_snapshots.append({"id": sid, "label": label, "state": "rcon_unavailable", "players": [], "population": None})

        eligible: dict[str, dict] = {}
        if active_servers and config.SEED_VOICE_CHANNEL_ID:
            guild = self.bot.get_guild(config.GUILD_ID)
            if guild:
                voice_channel = guild.get_channel(config.SEED_VOICE_CHANNEL_ID)
                voice_members = {
                    member.id: member
                    for member in getattr(voice_channel, "members", [])
                    if not member.bot
                }
                for server in active_servers:
                    for player in server["players"]:
                        user = seed_get_user_by_steam(player["steam_id"])
                        if not user:
                            continue
                        member = voice_members.get(int(user["discord_id"]))
                        if not member:
                            continue
                        eligible[user["discord_id"]] = {
                            "discord_id": user["discord_id"],
                            "display_name": member.display_name or user["display_name"],
                        }

        awards = seed_apply_seeding_tick(
            list(eligible.values()),
            now,
            seed_week_key(now),
            config.SEED_SESSION_GRACE_SECONDS,
            config.SEED_TICK_SECONDS * 2,
        )
        active_count = len(active_servers)
        state = (
            "active"
            if active_count
            else ("population_limit" if any(s["state"] == "population_limit" for s in all_snapshots) else "rcon_unavailable")
        )
        prev_state = self._last_status.get("state")
        if prev_state != state:
            log.info("seed status: %s -> %s (active_servers=%d eligible=%d)",
                     prev_state, state, active_count, len(eligible))
        self._last_status = {
            "state": state,
            "updatedAt": now,
            "eligibleCount": len(eligible),
            "servers": [{k: v for k, v in s.items() if k != "players"} for s in all_snapshots],
            "population": sum(s.get("population") or 0 for s in all_snapshots),
        }
        if awards:
            log.info("seed_points_awarded users=%d points=%d", len(awards), sum(a["points"] for a in awards))

    # ---------- VIP reconcile loop ----------

    @tasks.loop(seconds=max(30, config.VIP_RECONCILE_SECONDS))
    async def vip_loop(self):
        if self._vip_running:
            return
        self._vip_running = True
        try:
            await self._vip_reconcile()
        except Exception as e:
            log.exception("vip_reconcile_failed: %s", e)
        finally:
            self._vip_running = False

    @vip_loop.before_loop
    async def _before_vip_loop(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(10)

    async def _vip_reconcile(self):
        if not self.rcon:
            return
        now = _now_ms()
        claims = seed_get_state("paid_slot_claims", {}) or {}
        ids: set[str] = set()
        for m in seed_list_vip_memberships():
            if m["state"] != "inactive":
                ids.add(m["user_id"])
        for did in self.donations.active_recipient_ids(now):
            ident = seed_get_vip_identity_by_discord(did)
            if ident:
                ids.add(ident["id"])
        for claim in claims.values():
            ident = seed_get_vip_identity_by_discord(claim.get("discordId") or "")
            if ident:
                ids.add(ident["id"])
        for user_id in ids:
            try:
                await self._vip_reconcile_user(user_id, now, claims)
            except Exception as e:
                log.warning("vip_user_reconcile_failed user=%s: %s", user_id, e)
        if claims != seed_get_state("paid_slot_claims", {}):
            seed_set_state("paid_slot_claims", claims)

    async def _vip_reconcile_user(self, user_id: str, now: int, claims: dict):
        if not self.rcon:
            return
        member = seed_get_vip_membership(user_id)
        user = seed_get_vip_identity_by_id(user_id)
        if not user or not user.get("linked"):
            return
        steam_id = user["steam_id"]
        discord_id = user["discord_id"]
        paid = self.donations.active(discord_id, now)
        protected = ReservedProtectionService.is_protected(user_id, steam_id)
        claim = claims.get(steam_id)

        if (not member or member["state"] == "inactive") and not paid and not claim:
            return

        if member:
            await self._set_vip_role(discord_id, member["state"] != "inactive")

        if paid:
            snapshot = await self.rcon.reserved_snapshot()
            live = snapshot.get("live") or []
            doc = snapshot.get("document") or []
            present = steam_id in live or steam_id in doc
            if protected:
                claims[steam_id] = {"discordId": discord_id, "owner": "external"}
            elif not claim:
                bot_owned = (not present) or (member and member.get("managed_by_bot") == 1)
                claims[steam_id] = {"discordId": discord_id, "owner": "bot" if bot_owned else "external"}
            if steam_id not in live:
                if not protected:
                    claims[steam_id] = {"discordId": discord_id, "owner": "bot"}
                result = await self.rcon.set_reserved(steam_id, True)
                if result.get("active") and member and member["state"] == "pending_add":
                    seed_activate_vip(user_id)
                elif member and member["state"] == "pending_add":
                    seed_set_vip_error(user_id, "Ожидание Reserved slot на игровом сервере.")
                elif not member:
                    # paid-only: ensure slot; no membership row needed
                    pass
            elif member and member["state"] == "pending_add":
                seed_activate_vip(user_id)
            return

        # cleanup paid claim if no longer paid
        if claim and claim.get("owner") == "bot" and not paid:
            if not member or member["state"] == "inactive":
                try:
                    await self.rcon.set_reserved(steam_id, False)
                except Exception as e:
                    log.warning("paid slot cleanup failed %s: %s", steam_id, e)
                del claims[steam_id]

        if not member:
            return

        if member["state"] == "pending_add":
            if protected:
                seed_set_vip_ownership(user_id, False)
                member = seed_get_vip_membership(user_id)
            snapshot = await self.rcon.reserved_snapshot()
            live = snapshot.get("live") or []
            if member.get("managed_by_bot") is None:
                preexisting = steam_id in live or steam_id in (snapshot.get("document") or [])
                owned = bool(claim and claim.get("owner") == "bot")
                seed_set_vip_ownership(user_id, (not protected) and ((not preexisting) or owned))
                member = seed_get_vip_membership(user_id)
            if member.get("managed_by_bot") == 0:
                if steam_id in live:
                    seed_activate_vip(user_id)
                else:
                    seed_set_vip_error(user_id, "Ожидание Reserved slot на игровом сервере.")
                return
            if steam_id in live:
                seed_activate_vip(user_id)
                return
            result = await self.rcon.set_reserved(steam_id, True)
            if result.get("active"):
                seed_activate_vip(user_id)
            else:
                seed_set_vip_error(user_id, "Ожидание Reserved slot на игровом сервере.")
            return

        if member["state"] == "active" and (member.get("active_until") or 0) <= now:
            if protected or (member.get("managed_by_bot") == 0):
                seed_set_vip_state(user_id, "inactive")
                await self._set_vip_role(discord_id, False)
                return
            seed_set_vip_state(user_id, "pending_remove")
            member = seed_get_vip_membership(user_id)

        if member and member["state"] == "pending_remove":
            if protected:
                seed_set_vip_state(user_id, "inactive")
                await self._set_vip_role(discord_id, False)
                return
            if member.get("managed_by_bot") == 0 and (not claim or claim.get("owner") != "bot"):
                seed_set_vip_state(user_id, "inactive")
                await self._set_vip_role(discord_id, False)
                return
            if self.donations.active(discord_id, now):
                if member.get("managed_by_bot") != 0:
                    claims[steam_id] = {"discordId": discord_id, "owner": "bot"}
                seed_set_vip_state(user_id, "inactive")
                await self._set_vip_role(discord_id, False)
                return
            result = await self.rcon.set_reserved(steam_id, False)
            if result.get("active") is False:
                seed_set_vip_state(user_id, "inactive")
                await self._set_vip_role(discord_id, False)
            else:
                seed_set_vip_error(user_id, "Ожидание снятия Reserved slot в игровом сервере.")

    async def _set_vip_role(self, discord_id: str, present: bool):
        guild = self.bot.get_guild(config.GUILD_ID)
        if not guild or not config.VIP_ROLE_ID_SEED:
            return
        try:
            member = guild.get_member(int(discord_id)) or await guild.fetch_member(int(discord_id))
            role = guild.get_role(config.VIP_ROLE_ID_SEED)
            if not role:
                return
            has = role in member.roles
            if present and not has:
                await member.add_roles(role, reason="WARDOGS seeding: vip")
            elif not present and has and not _panel_vip_active_by_discord(discord_id):
                await member.remove_roles(role, reason="WARDOGS seeding: vip")
        except discord.NotFound:
            pass
        except Exception as e:
            log.warning("set_vip_role failed %s: %s", discord_id, e)

    # ---------- Weekly awards loop ----------

    @tasks.loop(seconds=max(60, config.WEEKLY_AWARD_RECONCILE_SECONDS))
    async def weekly_loop(self):
        if self._weekly_running:
            return
        self._weekly_running = True
        try:
            await self._weekly_reconcile()
        except Exception as e:
            log.exception("weekly_award_reconcile_failed: %s", e)
        finally:
            self._weekly_running = False

    @weekly_loop.before_loop
    async def _before_weekly_loop(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(15)

    async def _weekly_reconcile(self):
        if not self.rcon:
            return
        now = _now_ms()
        target_week = seed_previous_week_key(now)
        state = seed_get_state("weekly_super_seeders_v1", {"version": 1, "awardWeek": "", "awardedAt": None, "winners": [], "cleanup": []})
        if not isinstance(state, dict):
            state = {"version": 1, "awardWeek": "", "awardedAt": None, "winners": [], "cleanup": []}
        state.setdefault("winners", [])
        state.setdefault("cleanup", [])

        if state.get("awardWeek") != target_week:
            state = await self._weekly_rotate(state, target_week, now)

        await self._weekly_reconcile_winners(state)
        await self._weekly_reconcile_cleanup(state)

    async def _weekly_rotate(self, previous: dict, target_week: str, now: int) -> dict:
        rows = seed_get_leaderboard(target_week)[:3]
        prior_by_user = {w.get("userId"): w for w in previous.get("winners", [])}
        selected = {r["id"] for r in rows}
        cleanup = [e for e in previous.get("cleanup", []) if e.get("userId") not in selected]
        for old in previous.get("winners", []):
            if old.get("userId") in selected:
                continue
            await self._set_super_seeder_role(old.get("discordId") or "", False)
            if old.get("slotOwnership") == "award" and not ReservedProtectionService.is_protected(old.get("userId", ""), old.get("steamId", "")):
                cleanup.append({
                    "userId": old.get("userId"),
                    "discordId": old.get("discordId"),
                    "steamId": old.get("steamId"),
                    "slotOwnership": "award",
                    "status": "pending_remove",
                    "reason": "ranking_rotated",
                    "queuedAt": now,
                })
        winners = []
        for i, row in enumerate(rows):
            prior = prior_by_user.get(row["id"])
            winners.append({
                "place": i + 1,
                "userId": row["id"],
                "discordId": row["discord_id"],
                "steamId": row["steam_id"],
                "displayName": row["display_name"],
                "weeklySeconds": row.get("weekly_seconds", 0),
                "weeklyPoints": row.get("weekly_points", 0),
                "active": True,
                "roleStatus": (prior or {}).get("roleStatus", "checking"),
                "slotOwnership": (prior or {}).get("slotOwnership", "unknown"),
                "slotStatus": (prior or {}).get("slotStatus", "checking"),
                "slotProtected": ReservedProtectionService.is_protected(row["id"], row["steam_id"]),
            })
        state = {"version": 1, "awardWeek": target_week, "awardedAt": now, "winners": winners, "cleanup": cleanup}
        seed_set_state("weekly_super_seeders_v1", state)
        seed_audit("weekly_super_seeders_rotated", "", {"week_key": target_week, "winner_count": len(winners)})
        return state

    async def _weekly_reconcile_winners(self, _state: dict):
        if not self.rcon:
            return
        state = seed_get_state("weekly_super_seeders_v1", {"winners": [], "cleanup": []})
        for winner in list(state.get("winners", [])):
            linked = (seed_get_user_by_discord(winner.get("discordId") or "") or {}).get("id") == winner.get("userId")
            await self._set_super_seeder_role(winner.get("discordId") or "", linked)
            winner["active"] = linked
            winner["roleStatus"] = "active" if linked else "revoked"
        seed_set_state("weekly_super_seeders_v1", state)

        active = [w for w in state.get("winners", []) if w.get("active") is not False]
        if not active:
            return
        try:
            snapshot = await self.rcon.reserved_snapshot()
        except Exception as e:
            log.warning("weekly_award_rcon_unavailable: %s", e)
            for w in active:
                w["slotStatus"] = "waiting_rcon"
            seed_set_state("weekly_super_seeders_v1", state)
            return

        for current in active:
            winner = next((w for w in state["winners"] if w.get("userId") == current.get("userId")), None)
            if not winner:
                continue
            steam_id = winner.get("steamId") or ""
            live = snapshot.get("live") or []
            doc = snapshot.get("document") or []
            present = steam_id in live or steam_id in doc
            protected = ReservedProtectionService.is_protected(winner.get("userId", ""), steam_id)
            winner["slotProtected"] = protected
            if protected:
                winner["slotOwnership"] = "external"
                winner["slotStatus"] = "active" if steam_id in live else ("pending_restart" if present else "applying")
                if steam_id not in live:
                    try:
                        result = await self.rcon.set_reserved(steam_id, True)
                        winner["slotStatus"] = "active" if result.get("active") else "waiting_rcon"
                    except Exception as e:
                        winner["slotStatus"] = "waiting_rcon"
                        log.warning("weekly slot rcon failed: %s", e)
                continue
            if winner.get("slotOwnership") in ("unknown", "external", "award") and present:
                winner["slotStatus"] = "active" if steam_id in live else ("pending_restart" if present else "checking")
                if steam_id in live:
                    continue
            if not present:
                winner["slotOwnership"] = "award"
                winner["slotStatus"] = "applying"
            try:
                result = await self.rcon.set_reserved(steam_id, True)
                winner["slotStatus"] = "active" if result.get("active") else "waiting_rcon"
            except Exception as e:
                winner["slotStatus"] = "waiting_rcon"
                log.warning("weekly slot rcon failed: %s", e)
        seed_set_state("weekly_super_seeders_v1", state)

    async def _weekly_reconcile_cleanup(self, state: dict):
        if not self.rcon or not state.get("cleanup"):
            return
        try:
            snapshot = await self.rcon.reserved_snapshot()
        except Exception as e:
            log.warning("weekly_cleanup_rcon_unavailable: %s", e)
            return
        live = snapshot.get("live") or []
        doc = snapshot.get("document") or []
        for entry in list(state["cleanup"]):
            steam_id = entry.get("steamId") or ""
            if ReservedProtectionService.is_protected(entry.get("userId", ""), steam_id):
                state["cleanup"] = [e for e in state["cleanup"] if e is not entry]
                continue
            membership = seed_get_vip_membership(entry.get("userId") or "")
            if membership and membership["state"] != "inactive":
                entry["status"] = "waiting_vip"
                continue
            if steam_id not in live and steam_id not in doc:
                state["cleanup"] = [e for e in state["cleanup"] if e is not entry]
                continue
            try:
                result = await self.rcon.set_reserved(steam_id, False)
                if result.get("active") is False:
                    state["cleanup"] = [e for e in state["cleanup"] if e is not entry]
                else:
                    entry["status"] = "waiting_rcon"
            except Exception as e:
                entry["status"] = "waiting_rcon"
                log.warning("weekly cleanup rcon failed: %s", e)
        seed_set_state("weekly_super_seeders_v1", state)

    async def _set_super_seeder_role(self, discord_id: str, present: bool):
        guild = self.bot.get_guild(config.GUILD_ID)
        if not guild or not config.SUPER_SEEDER_ROLE_ID or not discord_id:
            return
        try:
            member = guild.get_member(int(discord_id)) or await guild.fetch_member(int(discord_id))
            role = guild.get_role(config.SUPER_SEEDER_ROLE_ID)
            if not role:
                return
            has = role in member.roles
            if present and not has:
                await member.add_roles(role, reason="WARDOGS seeding: super_seeder")
            elif not present and has:
                await member.remove_roles(role, reason="WARDOGS seeding: super_seeder")
        except discord.NotFound:
            pass
        except Exception as e:
            log.warning("set_super_seeder_role failed %s: %s", discord_id, e)

    # ---------- Role reconcile loop ----------

    @tasks.loop(seconds=max(60, config.DISCORD_ROLE_RECONCILE_SECONDS))
    async def role_loop(self):
        if self._role_running:
            return
        self._role_running = True
        try:
            await self._role_reconcile()
        except Exception as e:
            log.exception("discord_role_reconcile_failed: %s", e)
        finally:
            self._role_running = False

    @role_loop.before_loop
    async def _before_role_loop(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(20)

    async def _role_reconcile(self) -> dict[str, int]:
        stats = {"seeder_granted": 0, "vip_granted": 0, "vip_revoked": 0, "checked": 0}
        guild = self.bot.get_guild(config.GUILD_ID)
        if not guild:
            return stats
        seeder_role = guild.get_role(config.SEEDER_ROLE_ID) if config.SEEDER_ROLE_ID else None
        vip_role = guild.get_role(config.VIP_ROLE_ID_SEED) if config.VIP_ROLE_ID_SEED else None
        seeder_ids: set[str] = set()
        for user in seed_list_users():
            seeder_ids.add(user["discord_id"])
            try:
                member = guild.get_member(int(user["discord_id"])) or await guild.fetch_member(int(user["discord_id"]))
            except discord.NotFound:
                continue
            except Exception:
                continue
            stats["checked"] += 1
            if seeder_role and seeder_role not in member.roles:
                try:
                    await member.add_roles(seeder_role, reason="WARDOGS seeding: seeder")
                    stats["seeder_granted"] += 1
                except Exception:
                    pass
            membership = seed_get_vip_membership(user["id"])
            should_vip = (
                bool(membership and membership["state"] != "inactive")
                or self.donations.active(user["discord_id"])
                or _panel_vip_active(user.get("steam_id") or "")
            )
            if vip_role:
                has = vip_role in member.roles
                if should_vip and not has:
                    try:
                        await member.add_roles(vip_role, reason="WARDOGS seeding: vip")
                        stats["vip_granted"] += 1
                    except Exception:
                        pass
                elif not should_vip and has:
                    try:
                        await member.remove_roles(vip_role, reason="WARDOGS seeding: vip")
                        stats["vip_revoked"] += 1
                    except Exception:
                        pass
        for did in self.donations.recipient_ids():
            if did in seeder_ids or not vip_role:
                continue
            try:
                member = guild.get_member(int(did)) or await guild.fetch_member(int(did))
            except Exception:
                continue
            stats["checked"] += 1
            if vip_role in member.roles and not self.donations.active(did) and not _panel_vip_active_by_discord(did):
                try:
                    await member.remove_roles(vip_role, reason="WARDOGS seeding: vip expired")
                    stats["vip_revoked"] += 1
                except Exception:
                    pass
        return stats

    # ---------- Report / leaderboard loop ----------

    @tasks.loop(seconds=max(30, config.LEADERBOARD_REFRESH_SECONDS))
    async def report_loop(self):
        if self._report_running:
            return
        self._report_running = True
        try:
            await self._report_refresh()
        except Exception as e:
            log.exception("leaderboard_refresh_failed: %s", e)
        finally:
            self._report_running = False

    @report_loop.before_loop
    async def _before_report_loop(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(25)

    async def _report_refresh(self):
        guild = self.bot.get_guild(config.GUILD_ID)
        if not guild or not config.SEED_RANK_CHANNEL_ID:
            return
        channel = guild.get_channel(config.SEED_RANK_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            try:
                channel = await guild.fetch_channel(config.SEED_RANK_CHANNEL_ID)
            except Exception:
                return
        if not isinstance(channel, discord.TextChannel):
            return

        key = seed_week_key(_now_ms())
        rows = seed_get_leaderboard(key)
        now = _now_ms()
        lb_embed = self._leaderboard_card(rows, key, now)
        await self._upsert_message(channel, "leaderboard_message_id", lb_embed)

        state = seed_get_state("weekly_super_seeders_v1", {"awardWeek": seed_previous_week_key(now), "winners": []})
        winners_embed = self._weekly_winners_card(state, now)
        await self._upsert_message(channel, "weekly_winners_message_id", winners_embed)

    async def _upsert_message(self, channel: discord.TextChannel, state_key: str, embed: discord.Embed):
        prior = seed_get_state(state_key, "")
        if prior:
            try:
                msg = await channel.fetch_message(int(prior))
                await msg.edit(embed=embed)
                return
            except discord.NotFound:
                pass
            except Exception:
                pass
        msg = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        seed_set_state(state_key, str(msg.id))
        seed_audit("leaderboard_message_created", "", {"state_key": state_key, "message_id": str(msg.id)})

    def _leaderboard_card(self, rows: list[dict], week: str, updated_at: int) -> discord.Embed:
        embed = discord.Embed(
            color=discord.Color.blue(),
            title="🏆 Рейтинг сидеров WARDOGS | RU",
            description="**Текущая неделя** — места определяются только по подтверждённому времени сидинга.\n\n" + self._leaderboard_description(rows[:20]),
            timestamp=datetime.fromtimestamp(updated_at / 1000, tz=timezone.utc),
        )
        reward = (
            f"По итогам недели топ-3 получат <@&{config.SUPER_SEEDER_ROLE_ID}> и Reserved slot до следующего обновления."
            if config.SUPER_SEEDER_ROLE_ID
            else "По итогам недели три лидера получают награду сообщества."
        )
        embed.add_field(name="🎁 Награда лидерам", value=reward)
        embed.add_field(name="🔄 Обновление", value="Карточка обновляется автоматически; новое сообщение при каждом обновлении не создаётся.")
        extra = " • показан топ-20" if len(rows) > 20 else ""
        embed.set_footer(text=f"Неделя с {week} (МСК) • участников: {len(rows)}{extra}")
        return embed

    def _leaderboard_description(self, rows: list[dict]) -> str:
        if not rows:
            return "Пока нет подтверждённого времени. Зайдите на сервер и в голосовой канал — первое место свободно!"
        icons = ["🥇", "🥈", "🥉"]
        parts = []
        for i, row in enumerate(rows):
            icon = icons[i] if i < 3 else f"**{i + 1}.**"
            name = str(row.get("display_name") or "Игрок").replace("\n", " ")[:70]
            parts.append(
                f"{icon} **{name}**\n└ За неделю: **{_fmt_duration(row.get('weekly_seconds', 0))}** • Всего: {_fmt_duration(row.get('total_seconds', 0))}"
            )
        return "\n\n".join(parts)

    def _weekly_winners_card(self, state: dict, updated_at: int) -> discord.Embed:
        award_week = state.get("awardWeek") or seed_previous_week_key(updated_at)
        winners = (state.get("winners") or [])[:3]
        embed = discord.Embed(
            color=discord.Color.gold(),
            title="👑 Супер сидеры прошлой недели",
            description=(
                f"Лучшие участники недели с **{award_week} (МСК)**. Награды действуют до следующего недельного обновления."
                if winners
                else f"За завершившуюся неделю с **{award_week} (МСК)** подтверждённых участников не было."
            ),
            timestamp=datetime.fromtimestamp(updated_at / 1000, tz=timezone.utc),
        )
        icons = ["🥇", "🥈", "🥉"]
        for i, w in enumerate(winners):
            name = str(w.get("displayName") or "Игрок").replace("\n", " ")[:70]
            status = "⚪ награда снята после отвязки Steam" if w.get("active") is False else "роль выдана"
            embed.add_field(
                name=f"{icons[i] if i < 3 else str(i + 1)} {name}",
                value=f"Подтверждённое время: **{_fmt_duration(w.get('weeklySeconds', 0))}**\n{status}",
                inline=False,
            )
        embed.add_field(
            name="🏅 Награда",
            value=f"Роль <@&{config.SUPER_SEEDER_ROLE_ID}> + бесплатный Reserved slot на обоих игровых серверах.",
        )
        embed.set_footer(text="Награды пересматриваются автоматически каждую неделю")
        return embed

    # ---------- Donation feed loop ----------

    @tasks.loop(seconds=max(30, config.DONATION_POLL_SECONDS_SEED))
    async def donation_loop(self):
        if not self.donations.enabled:
            return
        try:
            async with aiohttp.ClientSession() as session:
                granted = await self.donations.refresh(session)
            if granted:
                log.info("donation_feed granted=%d", granted)
        except Exception as e:
            log.warning("donation_feed_unavailable: %s", e)

    @donation_loop.before_loop
    async def _before_donation_loop(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(15)

    # ---------- Gather loop ----------

    @tasks.loop(seconds=max(15, config.SEED_TICK_SECONDS))
    async def gather_loop(self):
        if self._gather_running:
            return
        self._gather_running = True
        try:
            await self._gather_tick()
        except Exception as e:
            log.exception("gather_tick_failed: %s", e)
        finally:
            self._gather_running = False

    @gather_loop.before_loop
    async def _before_gather_loop(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(10)

    async def _gather_tick(self):
        sessions = seed_get_state("seeding_gathers", {}) or {}
        if not sessions:
            return
        now = _now_ms()
        changed = False
        for server_id, session in list(sessions.items()):
            if session.get("status") != "active":
                continue
            if now >= session.get("expiresAt", 0):
                session["status"] = "expired"
                seed_audit("gather_expired", "system", {"id": session.get("id"), "server": server_id})
                changed = True
                continue
            client = self.rcon if server_id == "main" else self.rcon2
            if not client:
                continue
            try:
                status = await client.status()
                session["population"] = status["player_count"]
                session["maxPlayers"] = status.get("max_players")
                session["serverName"] = (status.get("server_name") or session.get("serverName") or "")[:200]
                session["sampleOk"] = True
                session["sampleAt"] = now
                if status["player_count"] >= session.get("target", 20):
                    session["status"] = "reached"
                    session["reachedAt"] = now
                    seed_audit("gather_target_reached", "system", {"id": session.get("id"), "server": server_id, "population": status["player_count"], "target": session.get("target")})
                    changed = True
            except Exception as e:
                session["sampleOk"] = False
                log.warning("gather_server_unavailable %s: %s", server_id, e)
            changed = True
            await self._deliver_gather_card(session)
        if changed:
            seed_set_state("seeding_gathers", sessions)
            self._gather_sessions = sessions

    async def _deliver_gather_card(self, session: dict):
        guild = self.bot.get_guild(config.GUILD_ID)
        if not guild:
            return
        channel = guild.get_channel(int(session.get("channelId") or 0))
        if not isinstance(channel, discord.TextChannel):
            return
        embed = self._gather_embed(session)
        view = self._gather_connect_view(session)
        msg_id = session.get("cardMessageId")
        message = None
        if msg_id:
            try:
                message = await channel.fetch_message(int(msg_id))
            except discord.NotFound:
                message = None
        if message:
            try:
                await message.edit(embed=embed, view=view)
            except Exception as e:
                log.warning("gather edit failed: %s", e)
        else:
            try:
                message = await channel.send(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
                session["cardMessageId"] = str(message.id)
            except Exception as e:
                log.warning("gather send failed: %s", e)

    def _gather_embed(self, session: dict) -> discord.Embed:
        status = session.get("status")
        population = session.get("population")
        target = session.get("target", 20)
        label = str(session.get("label") or "сервер")
        server_name = session.get("serverName") or label
        closed = status in ("cancelled", "expired")
        reached = status == "reached"

        if reached:
            desc = f"**Цель достигнута: {population}/{target} игроков!**\nСпасибо каждому, кто помог собрать стаю."
            color = discord.Color.green()
            title = f"🎉 Цель достигнута · {label}"
        elif closed:
            desc = "Сбор остановлен администрацией." if status == "cancelled" else "Время сбора истекло. Можно объявить новый сбор."
            color = discord.Color.dark_grey()
            title = f"🏁 Сбор завершён · {label}"
        elif population is None or not session.get("sampleOk", True):
            desc = "Связь с сервером временно недоступна. **Число игроков уточняется.**"
            color = 0xD4A017
            title = f"📣 Собираем стаю · {label}"
        else:
            missing = max(0, target - population)
            filled = min(10, int(population / target * 10)) if target else 0
            desc = (f"**Сейчас {population}/{target} — нужно ещё {missing}**\n\n"
                    f"{'🟩' * filled}{'⬛' * (10 - filled)}\n\nЗаходите на сервер и помогите запустить игру!")
            color = 0xD4A017
            title = f"📣 Собираем стаю · {label}"

        embed = discord.Embed(color=color, title=title, description=desc)
        embed.add_field(name="🎮 Игровой сервер", value=server_name)
        embed.add_field(name="🎯 Цель", value=f"{target} игроков", inline=True)
        expires = session.get("expiresAt", 0)
        embed.add_field(name="⏳ Срок сбора", value=f"<t:{int(expires / 1000)}:R>" if expires else "—", inline=True)
        sample_at = session.get("sampleAt")
        if sample_at:
            name = "Последняя проверка" if (reached or closed or not session.get("sampleOk", True)) else "Данные обновлены"
            embed.add_field(name=name, value=f"<t:{int(sample_at / 1000)}:R>", inline=False)
        embed.set_footer(text=f"{'Итог сбора' if reached else 'Сбор'} №{session.get('id', '')}")
        return embed

    def _gather_connect_view(self, session: dict) -> discord.ui.View:
        server_id = session.get("serverId") or session.get("id") or ""
        view = discord.ui.View(timeout=None)
        btn = discord.ui.Button(label="Как подключиться", emoji="🎮", style=discord.ButtonStyle.primary,
                                custom_id=f"seed:gather:connect:{server_id}:{session.get('id', '')}")
        async def _cb(interaction: discord.Interaction):
            await self._send_gather_instructions(interaction, session)
        btn.callback = _cb
        view.add_item(btn)
        return view

    async def _send_gather_instructions(self, interaction: discord.Interaction, session: dict):
        instructions = session.get("instructions") or (
            f"1. Запустите **WARDOGS**.\n2. Откройте список игровых серверов и найдите **{session.get('serverName') or session.get('label') or 'WARDOGS'}**.\n3. Подключитесь к этому серверу и дождитесь загрузки."
        )
        embed = discord.Embed(color=0x3498DB, title=f"🎮 Подключение · {session.get('label', '')}", description=instructions)
        voice_ref = f"<#{config.SEED_VOICE_CHANNEL_ID}>" if config.SEED_VOICE_CHANNEL_ID else "голосовой канал сидинга"
        rules_ref = f"<#{config.SEED_RULES_CHANNEL_ID}>" if config.SEED_RULES_CHANNEL_ID else "канал правил"
        embed.add_field(name="🐾 Если участвуете в сидинге",
                        value=f"Зайдите в голосовой канал {voice_ref}. Для начисления времени и баллов используйте подтверждённую Steam-привязку. Правила: {rules_ref} и команда `/seed help`.")
        embed.set_footer(text="WARDOGS RU · Сидинг")
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------- Backup loop ----------

    @tasks.loop(hours=6)
    async def backup_loop(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(60)
        try:
            await self._send_seed_backup()
        except Exception as e:
            log.error("seed backup failed: %s", e)

    async def _send_seed_backup(self):
        if not config.SEED_BACKUP_CHANNEL_ID:
            return
        guild = self.bot.get_guild(config.GUILD_ID)
        if not guild:
            return
        channel = guild.get_channel(config.SEED_BACKUP_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            try:
                channel = await guild.fetch_channel(config.SEED_BACKUP_CHANNEL_ID)
            except Exception:
                return
        if not isinstance(channel, discord.TextChannel):
            return
        # Send a compact JSON snapshot of seed system state (not full DB).
        snapshot = {
            "exported_at": datetime.now(MSK).isoformat(),
            "users": len(seed_list_users()),
            "weekly_top": [
                {"name": r.get("display_name"), "seconds": r.get("weekly_seconds")}
                for r in seed_get_leaderboard(seed_week_key(_now_ms()))[:10]
            ],
        }
        payload = json.dumps(snapshot, ensure_ascii=False, indent=2)
        await channel.send(
            content=f"**Сидинг — снимок состояния** ({snapshot['exported_at']})",
            file=discord.File(fp=io.BytesIO(payload.encode()), filename="seed_snapshot.json"),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # ---------- Единая привязка /steam ----------

    steam_group = app_commands.Group(
        name="steam",
        description="Единая Steam-привязка: сидер, баллы и VIP в одной команде",
    )

    async def _link_start(self, interaction: discord.Interaction, steam_id: str, kind: str, verify_cmd: str):
        await interaction.response.defer(ephemeral=True)
        normalized = steam_id.strip()
        if not STEAM_ID_RE.match(normalized):
            await interaction.followup.send("Нужен SteamID64: 17 цифр, обычно начинается с 7656119.", ephemeral=True)
            return
        clients = [c for _, _, c in self._servers()]
        if not clients:
            await interaction.followup.send("RCON не настроен, привязка временно недоступна.", ephemeral=True)
            return
        results = await asyncio.gather(*[c.players() for c in clients], return_exceptions=True)
        server_index = -1
        player_name = "Игрок"
        for i, res in enumerate(results):
            if isinstance(res, Exception):
                continue
            for entry in res:
                if entry["steam_id"] == normalized:
                    server_index = i
                    player_name = entry["name"]
                    break
            if server_index >= 0:
                break
        if server_index < 0:
            err = next((r for r in results if isinstance(r, Exception)), None)
            if err:
                await interaction.followup.send(f"Ошибка RCON: {err}", ephemeral=True)
                return
            await interaction.followup.send("Сначала зайдите на один из игровых серверов, затем повторите команду.", ephemeral=True)
            return

        code = _human_code(8)
        code_hash = seed_hash_code(str(interaction.user.id), code)
        expires_at = _now_ms() + config.LINK_CHALLENGE_MINUTES * 60_000
        try:
            seed_prepare_link_challenge(
                str(interaction.user.id),
                normalized,
                interaction.user.display_name or player_name,
                code_hash,
                expires_at,
                kind,
            )
        except SeedDomainError as e:
            await interaction.followup.send(e.message, ephemeral=True)
            return
        try:
            whisper = f"WARDOGS: код привязки {code}. В Discord: {verify_cmd}. Никому не сообщайте код."
            await clients[server_index].whisper(normalized, whisper)
        except Exception as e:
            seed_cancel_link_challenge(str(interaction.user.id))
            await interaction.followup.send(f"Не удалось отправить код в игру: {e}", ephemeral=True)
            return
        await interaction.followup.send(
            f"Код отправлен в игру игроку **{player_name}**.\n"
            f"Подтвердите через `{verify_cmd} code:{code}` в течение {config.LINK_CHALLENGE_MINUTES} мин.\n"
            f"*Код показан только вам — никому его не сообщайте.*",
            ephemeral=True,
        )

    async def _link_verify(self, interaction: discord.Interaction, code: str, kind: str):
        await interaction.response.defer(ephemeral=True)
        normalized = code.strip().upper()
        if not CODE_RE.match(normalized):
            await interaction.followup.send("Код состоит из 8 символов без пробелов.", ephemeral=True)
            return
        try:
            user = seed_verify_link_challenge(
                str(interaction.user.id),
                normalized,
                config.LINK_CHALLENGE_MAX_ATTEMPTS,
                kind,
            )
        except SeedDomainError as e:
            await interaction.followup.send(e.message, ephemeral=True)
            return
        did = str(interaction.user.id)
        if kind == "seeding":
            guild = self.bot.get_guild(config.GUILD_ID)
            if guild and config.SEEDER_ROLE_ID:
                try:
                    member = guild.get_member(interaction.user.id) or await guild.fetch_member(interaction.user.id)
                    role = guild.get_role(config.SEEDER_ROLE_ID)
                    if role and role not in member.roles:
                        await member.add_roles(role, reason="WARDOGS seeding: verified")
                except Exception:
                    pass
        now = _now_ms()
        vip_member = seed_get_vip_membership(user["id"])
        should_vip = bool(
            (vip_member and vip_member["state"] == "active" and (vip_member.get("active_until") or 0) > now)
            or _panel_vip_active(user["steam_id"])
            or _panel_vip_active_by_discord(did)
            or self.donations.active(did, now)
        )
        if should_vip:
            await self._set_vip_role(did, True)
        if kind == "seeding":
            vip_line = "\nVIP-роль выдана и закреплена — со временем не слетает." if should_vip else ""
            await interaction.followup.send(
                f"✅ Привязка подтверждена!\n"
                f"**Steam:** {user['steam_id']}\n"
                f"**Баланс:** {user.get('balance', 0)} баллов\n"
                f"Роль сидера выдана.{vip_line}\n"
                f"Зарабатывайте баллы: будьте в голосовом канале сидинга **и** на игровом сервере.",
                ephemeral=True,
            )
        else:
            vip_line = "\nVIP-роль выдана — со временем не слетает." if should_vip else ""
            await interaction.followup.send(
                f"✅ Привязка подтверждена.\n**Steam:** {user['steam_id']}{vip_line}\n"
                f"Оплаченный VIP с панели будет применён автоматически.",
                ephemeral=True,
            )

    async def _send_seed_status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user = seed_get_user_by_discord(str(interaction.user.id))
        if not user:
            await interaction.followup.send(
                "Привязка не найдена. Используйте `/steam link steam_id:<ваш SteamID64>`, затем `/steam verify`.",
                ephemeral=True,
            )
            return
        member = seed_get_vip_membership(user["id"])
        vip_until = None
        vip_state = "неактивен"
        if member and member["state"] == "active" and (member.get("active_until") or 0) > _now_ms():
            vip_state = "активен"
            vip_until = f"<t:{int(member['active_until'] / 1000)}:R>"
        elif member and member["state"] in ("pending_add", "pending_remove"):
            vip_state = f"синхронизация ({member['state']})"
        paid_until = self.donations.until(str(interaction.user.id))
        embed = discord.Embed(color=discord.Color.blurple(), title="📊 Статус сидинга")
        embed.add_field(name="Steam", value=user["steam_id"])
        embed.add_field(name="Баланс", value=f"**{user['balance']}** баллов")
        embed.add_field(name="Время всего", value=_fmt_duration(user["total_seconds"]))
        week_key = seed_week_key(_now_ms())
        rows = {r["id"]: r for r in seed_get_leaderboard(week_key)}
        wk = rows.get(user["id"], {})
        embed.add_field(name="За неделю", value=_fmt_duration(wk.get("weekly_seconds", 0)))
        embed.add_field(name="VIP (баллы)", value=f"{vip_state} {vip_until or ''}".strip())
        if paid_until and paid_until > _now_ms():
            embed.add_field(name="VIP (оплата)", value=f"до <t:{int(paid_until / 1000)}:f>")
        embed.set_footer(text=f"ID: {user['id'][:8]}…")
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _unlink_common(self, interaction: discord.Interaction, confirm: bool):
        await interaction.response.defer(ephemeral=True)
        if not confirm:
            await interaction.followup.send("Отвязка не выполнена (confirm=false).", ephemeral=True)
            return
        try:
            user = seed_unlink_discord(str(interaction.user.id))
        except SeedDomainError as e:
            await interaction.followup.send(e.message, ephemeral=True)
            return
        await interaction.followup.send(
            f"🔓 Привязка отменена. Steam {user['steam_id']} отвязан от вашего Discord.",
            ephemeral=True,
        )

    @steam_group.command(name="link", description="Привязать SteamID: код в игре, роль сидера и VIP")
    @app_commands.describe(steam_id="Ваш 17-значный SteamID64")
    @app_commands.guild_only()
    async def steam_link(self, interaction: discord.Interaction, steam_id: str):
        await self._link_start(interaction, steam_id, "seeding", "/steam verify")

    @steam_group.command(name="verify", description="Подтвердить код, полученный в игре")
    @app_commands.describe(code="Одноразовый код из сообщения на игровом сервере")
    @app_commands.guild_only()
    async def steam_verify(self, interaction: discord.Interaction, code: str):
        await self._link_verify(interaction, code, "seeding")

    @steam_group.command(name="status", description="Показать баланс, время и статус VIP")
    @app_commands.guild_only()
    async def steam_status(self, interaction: discord.Interaction):
        await self._send_seed_status(interaction)

    @steam_group.command(name="unlink", description="Отменить привязку Discord к Steam")
    @app_commands.describe(confirm="Подтвердите отвязку аккаунта")
    @app_commands.guild_only()
    async def steam_unlink(self, interaction: discord.Interaction, confirm: bool):
        await self._unlink_common(interaction, confirm)

    # ---------- Slash: /seed ----------

    seed_group = app_commands.Group(name="seed", description="Сидинг WARDOGS, баланс и VIP")

    @seed_group.command(name="link", description="Привязать Steam (алиас /steam link)")
    @app_commands.describe(steam_id="Ваш 17-значный SteamID64")
    @app_commands.guild_only()
    async def seed_link(self, interaction: discord.Interaction, steam_id: str):
        await self._link_start(interaction, steam_id, "seeding", "/steam verify")

    @seed_group.command(name="verify", description="Подтвердить код из игры (алиас /steam verify)")
    @app_commands.describe(code="Одноразовый код из сообщения на игровом сервере")
    @app_commands.guild_only()
    async def seed_verify(self, interaction: discord.Interaction, code: str):
        await self._link_verify(interaction, code, "seeding")

    @seed_group.command(name="unlink", description="Отменить привязку Discord к Steam")
    @app_commands.describe(confirm="Подтвердите отвязку аккаунта")
    @app_commands.guild_only()
    async def seed_unlink(self, interaction: discord.Interaction, confirm: bool):
        await self._unlink_common(interaction, confirm)

    @seed_group.command(name="status", description="Показать баланс, время и статус VIP")
    @app_commands.guild_only()
    async def seed_status(self, interaction: discord.Interaction):
        await self._send_seed_status(interaction)

    @seed_group.command(name="top", description="Показать рейтинг текущей недели")
    @app_commands.guild_only()
    async def seed_top(self, interaction: discord.Interaction):
        await interaction.response.defer()
        key = seed_week_key(_now_ms())
        rows = seed_get_leaderboard(key)
        embed = self._leaderboard_card(rows, key, _now_ms())
        await interaction.followup.send(embed=embed)

    @seed_group.command(name="shop", description="Показать магазин VIP")
    @app_commands.guild_only()
    async def seed_shop(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        embed = discord.Embed(
            color=discord.Color.gold(),
            title="🛒 Магазин VIP за баллы сидинга",
            description="Баллы начисляются за время в голосовом канале сидинга **и** на игровом сервере.",
        )
        for sku, product in VIP_CATALOG.items():
            cost = getattr(config, product["cost_attr"])
            embed.add_field(
                name=product["label"],
                value=f"**{cost}** баллов • `/seed buy plan:{sku}`",
                inline=False,
            )
        embed.add_field(
            name="ℹ️ Как это работает",
            value="VIP даёт Reserved slot на игровых серверах. Оплаченный VIP с панели — та же привязка `/steam link` + `/steam verify`.",
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @seed_group.command(name="panel", description="Задеплоить панели сидинга (правила и магазин)")
    @app_commands.guild_only()
    async def seed_panel_deploy(self, interaction: discord.Interaction):
        if not (interaction.user.guild_permissions.administrator or interaction.user.guild_permissions.manage_guild):
            return await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        r1 = await self._publish_rules_panel()
        r2 = await self._publish_shop_panel()
        await interaction.followup.send(f"Деплой панелей сидинга:\n• {r1}\n• {r2}", ephemeral=True)

    @seed_group.command(name="buy", description="Купить VIP за баллы")
    @app_commands.describe(plan="Срок VIP")
    @app_commands.choices(
        plan=[
            app_commands.Choice(name="1 день", value="1d"),
            app_commands.Choice(name="7 дней", value="7d"),
            app_commands.Choice(name="30 дней", value="30d"),
        ]
    )
    @app_commands.guild_only()
    async def seed_buy(self, interaction: discord.Interaction, plan: app_commands.Choice[str]):
        await self._vip_purchase(interaction, plan.value)

    async def _vip_purchase(self, interaction: discord.Interaction, sku: str):
        """Общая покупка VIP — из /seed buy и кнопок панели магазина. Ответ эфемерный."""
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        product = VIP_CATALOG.get(sku)
        if not product:
            await interaction.followup.send("Неизвестный вариант VIP.", ephemeral=True)
            return
        cost = getattr(config, product["cost_attr"])
        user = seed_get_user_by_discord(str(interaction.user.id))
        if not user:
            await interaction.followup.send("Сначала привяжите Steam через `/steam link`.", ephemeral=True)
            return
        if ReservedProtectionService.is_protected(user["id"], user["steam_id"]):
            await interaction.followup.send(
                "За вами закреплён постоянный Reserved slot. Покупка VIP отменена, баллы не списаны.",
                ephemeral=True,
            )
            return
        if self.rcon:
            try:
                await self.rcon.assert_live_reserved_writable()
            except RconError:
                await interaction.followup.send(
                    "WDRCON должен поддерживать мгновенную выдачу Reserved slots. Покупка отменена, баллы не списаны.",
                    ephemeral=True,
                )
                return
            try:
                snapshot = await self.rcon.reserved_snapshot()
                live = snapshot.get("live") or []
                if user["steam_id"] in live:
                    member = seed_get_vip_membership(user["id"])
                    if not member or member["state"] == "inactive":
                        await interaction.followup.send(
                            "У вас уже есть reserved slot вне магазина. Покупка отменена, баллы не списаны; обратитесь к администратору.",
                            ephemeral=True,
                        )
                        return
            except Exception:
                pass
        idempotency_key = f"{interaction.user.id}:{sku}:{_now_ms()}"
        try:
            order = seed_purchase_vip(
                str(interaction.user.id),
                sku,
                cost,
                product["duration_seconds"],
                idempotency_key,
            )
        except SeedDomainError as e:
            await interaction.followup.send(e.message, ephemeral=True)
            return
        await self._set_vip_role(str(interaction.user.id), True)
        if self.rcon:
            try:
                await self._vip_reconcile_user(order["user_id"], _now_ms(), seed_get_state("paid_slot_claims", {}) or {})
            except Exception as e:
                log.warning("post-purchase reconcile failed: %s", e)
        await interaction.followup.send(
            f"🛒 **{product['label']}** заказан.\n"
            f"Списано: **{cost}** баллов. Осталось: **{order['balance']}**.\n"
            f"VIP применится автоматически, как только Reserved slot будет выдан.",
            ephemeral=True,
        )

    @seed_group.command(name="help", description="Как работает сидинг")
    @app_commands.guild_only()
    async def seed_help(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        embed = discord.Embed(
            color=discord.Color.blurple(),
            title="🐺 Сидинг WARDOGS — как это работает",
            description=(
                "1. Зайдите на игровой сервер WARDOGS **и** в голосовой канал сидинга.\n"
                "2. Бот начисляет баллы автоматически (каждые 6 мин в первый час, далее каждые 3).\n"
                "3. Привяжите Steam: `/steam link` → получите код в игре → `/steam verify`.\n"
                "4. Копите баллы и покупайте VIP: `/seed shop` → `/seed buy`.\n"
                "5. Топ-3 недели получают роль Super Seeder + бесплатный Reserved slot."
            ),
        )
        embed.add_field(
            name="Окно начисления",
            value=f"{config.SEED_START_TIME}–{config.SEED_END_TIME} МСК, при {config.SEED_STOP_PLAYER_COUNT}+ игроках начисление приостанавливается.",
        )
        embed.add_field(
            name="Оплаченный VIP",
            value="С панели panel.dendich.online — та же единая привязка `/steam link` + `/steam verify`.",
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---------- Slash: /vip-link ----------

    vip_link_group = app_commands.Group(
        name="vip-link",
        description="Steam-привязка только для оплаченного VIP (рекомендуем /steam link — единая привязка)",
    )

    @vip_link_group.command(name="link", description="Привязать Steam только для оплаченного VIP")
    @app_commands.describe(steam_id="Ваш 17-значный SteamID64")
    @app_commands.guild_only()
    async def vip_link_link(self, interaction: discord.Interaction, steam_id: str):
        await self._link_start(interaction, steam_id, "donation", "/vip-link verify")

    @vip_link_group.command(name="verify", description="Подтвердить код из игры для оплаченного VIP")
    @app_commands.describe(code="Одноразовый код из игрового сообщения")
    @app_commands.guild_only()
    async def vip_link_verify(self, interaction: discord.Interaction, code: str):
        await self._link_verify(interaction, code, "donation")

    @vip_link_group.command(name="status", description="Проверить VIP-привязку")
    @app_commands.guild_only()
    async def vip_link_status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        donor = seed_get_donor_by_discord(str(interaction.user.id))
        if not donor:
            user = seed_get_user_by_discord(str(interaction.user.id))
            if user:
                await interaction.followup.send(
                    f"Вы привязаны для сидинга: Steam `{user['steam_id']}`. Оплаченный VIP подходит автоматически.",
                    ephemeral=True,
                )
                return
            await interaction.followup.send("Отдельной VIP-привязки нет. Используйте `/steam link` (единая привязка).", ephemeral=True)
            return
        until = self.donations.until(str(interaction.user.id))
        status = f"до <t:{int(until / 1000)}:f>" if until and until > _now_ms() else "нет активного оплаченного VIP"
        await interaction.followup.send(
            f"**Steam:** {donor['steam_id']}\n**Оплаченный VIP:** {status}",
            ephemeral=True,
        )

    @vip_link_group.command(name="unlink", description="Отменить отдельную VIP-привязку")
    @app_commands.describe(confirm="Подтвердите отвязку")
    @app_commands.guild_only()
    async def vip_link_unlink(self, interaction: discord.Interaction, confirm: bool):
        await interaction.response.defer(ephemeral=True)
        if not confirm:
            await interaction.followup.send("Отвязка не выполнена (confirm=false).", ephemeral=True)
            return
        try:
            donor = seed_unlink_donor(str(interaction.user.id))
        except SeedDomainError as e:
            await interaction.followup.send(e.message, ephemeral=True)
            return
        await interaction.followup.send(f"🔓 VIP-привязка отменена. Steam `{donor['steam_id']}`.", ephemeral=True)

    # ---------- Slash: /seed-gather ----------

    gather_group = app_commands.Group(
        name="seed-gather",
        description="Сбор игроков для координаторов и администраторов WARDOGS",
    )

    @gather_group.command(name="start", description="Позвать игроков и опубликовать обновляемую карточку сбора")
    @app_commands.describe(
        server="Игровой сервер",
        target="Цель сбора: по умолчанию 20 игроков",
        role="Кого уведомить: по умолчанию роль «Сидер»",
        channel="Канал карточки: по умолчанию текущий",
        minutes="Срок сбора: по умолчанию 120 минут",
        instructions="Своя инструкция подключения; иначе используется игровое название из RCON",
    )
    @app_commands.choices(
        server=[
            app_commands.Choice(name="Сервер 1", value="main"),
            app_commands.Choice(name="Сервер 2", value="second"),
        ]
    )
    @app_commands.guild_only()
    async def gather_start(
        self,
        interaction: discord.Interaction,
        server: app_commands.Choice[str],
        target: app_commands.Range[int, 2, 100] = 20,
        role: discord.Role | None = None,
        channel: discord.TextChannel | None = None,
        minutes: app_commands.Range[int, 15, 360] = 120,
        instructions: str = "",
    ):
        await interaction.response.defer(ephemeral=True)
        server_id = server.value
        if server_id == "main" and not self.rcon:
            await interaction.followup.send("RCON сервера 1 не настроен.", ephemeral=True)
            return
        if server_id == "second" and not self.rcon2:
            await interaction.followup.send("RCON сервера 2 не настроен.", ephemeral=True)
            return
        target_channel = channel or interaction.channel
        if not isinstance(target_channel, discord.TextChannel):
            await interaction.followup.send("Для сбора нужен текстовый канал.", ephemeral=True)
            return
        notify_role = role or (interaction.guild.get_role(config.SEEDER_ROLE_ID) if config.SEEDER_ROLE_ID else None)
        if not notify_role:
            await interaction.followup.send("Выберите роль для уведомления или задайте SEEDER_ROLE_ID.", ephemeral=True)
            return
        sessions = seed_get_state("seeding_gathers", {}) or {}
        existing = sessions.get(server_id)
        now = _now_ms()
        if existing and existing.get("status") == "active" and now < existing.get("expiresAt", 0):
            await interaction.followup.send("Для этого сервера уже идёт активный сбор. Дождитесь завершения или отмены.", ephemeral=True)
            return
        client = self.rcon if server_id == "main" else self.rcon2
        label = "Сервер 1" if server_id == "main" else "Сервер 2"
        population = None
        max_players = None
        server_name = ""
        try:
            status = await client.status()
            population = status["player_count"]
            max_players = status.get("max_players")
            server_name = status.get("server_name") or ""
        except Exception as e:
            await interaction.followup.send(f"RCON недоступен: {e}", ephemeral=True)
            return
        if population is not None and population >= target:
            await interaction.followup.send(
                f"На выбранном сервере уже {population}/{target} игроков. Дополнительный сбор не требуется.",
                ephemeral=True,
            )
            return
        if max_players and target > max_players:
            await interaction.followup.send("Цель превышает количество мест на выбранном сервере.", ephemeral=True)
            return
        request_id = secrets.token_hex(8)
        session = {
            "id": request_id,
            "serverId": server_id,
            "label": label,
            "target": target,
            "channelId": str(target_channel.id),
            "actorId": str(interaction.user.id),
            "roleId": str(notify_role.id),
            "instructions": instructions.strip()[:1000],
            "startedAt": now,
            "expiresAt": now + minutes * 60_000,
            "status": "active",
            "population": population,
            "maxPlayers": max_players,
            "serverName": server_name,
            "sampleOk": True,
            "sampleAt": now,
            "cardMessageId": None,
            "completionMessageId": None,
        }
        sessions[server_id] = session
        seed_set_state("seeding_gathers", sessions)
        self._gather_sessions = sessions
        seed_audit("gather_started", str(interaction.user.id), {"id": request_id, "server": server_id, "target": target})
        try:
            message = await target_channel.send(
                content=f"<@&{notify_role.id}> 🐺 Сбор на {label.lower()}! Помогите собрать {target} игроков.",
                embed=self._gather_embed(session),
                view=self._gather_connect_view(session),
                allowed_mentions=discord.AllowedMentions(roles=[notify_role]),
            )
            session["cardMessageId"] = str(message.id)
            sessions[server_id] = session
            seed_set_state("seeding_gathers", sessions)
            self._gather_sessions = sessions
        except Exception as e:
            log.warning("gather card send failed: %s", e)
            await self._deliver_gather_card(session)
        await interaction.followup.send(
            f"✅ Сбор на **{label}** объявлен: цель **{target}** игроков, срок **{minutes}** мин.\nКарточка: {target_channel.mention}",
            ephemeral=True,
        )

    @gather_group.command(name="status", description="Проверить сбор и обновить карточку")
    @app_commands.choices(
        server=[
            app_commands.Choice(name="Сервер 1", value="main"),
            app_commands.Choice(name="Сервер 2", value="second"),
        ]
    )
    @app_commands.guild_only()
    async def gather_status(self, interaction: discord.Interaction, server: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)
        sessions = seed_get_state("seeding_gathers", {}) or {}
        session = sessions.get(server.value)
        if not session:
            await interaction.followup.send("Для этого сервера сбор ещё не объявлялся.", ephemeral=True)
            return
        client = self.rcon if server.value == "main" else self.rcon2
        if client and session.get("status") == "active":
            try:
                status = await client.status()
                session["population"] = status["player_count"]
                session["sampleOk"] = True
                session["sampleAt"] = _now_ms()
                if status["player_count"] >= session.get("target", 20):
                    session["status"] = "reached"
                    session["reachedAt"] = _now_ms()
            except Exception as e:
                session["sampleOk"] = False
                log.warning("gather status sample failed: %s", e)
            sessions[server.value] = session
            seed_set_state("seeding_gathers", sessions)
            await self._deliver_gather_card(session)
        await interaction.followup.send(embed=self._gather_embed(session), view=self._gather_connect_view(session), ephemeral=True)

    @gather_group.command(name="stop", description="Остановить сбор игроков и закрыть карточку")
    @app_commands.choices(
        server=[
            app_commands.Choice(name="Сервер 1", value="main"),
            app_commands.Choice(name="Сервер 2", value="second"),
        ]
    )
    @app_commands.guild_only()
    async def gather_stop(self, interaction: discord.Interaction, server: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)
        sessions = seed_get_state("seeding_gathers", {}) or {}
        session = sessions.get(server.value)
        if not session:
            await interaction.followup.send("Для этого сервера сбор ещё не объявлялся.", ephemeral=True)
            return
        if session.get("status") != "active":
            await interaction.followup.send(f"Сбор уже не активен (статус: {session.get('status')}).", ephemeral=True)
            return
        session["status"] = "cancelled"
        session["cancelledAt"] = _now_ms()
        sessions[server.value] = session
        seed_set_state("seeding_gathers", sessions)
        seed_audit("gather_cancelled", str(interaction.user.id), {"id": session.get("id"), "server": server.value})
        await self._deliver_gather_card(session)
        await interaction.followup.send("❌ Сбор остановлен, карточка закрыта.", ephemeral=True)

    # ---------- Slash: /seed-admin ----------

    admin_group = app_commands.Group(
        name="seed-admin",
        description="Администрирование отдельного бота сидинга",
    )

    async def _check_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.manage_guild:
            return True
        await interaction.response.send_message("Нужны права «Управление сервером».", ephemeral=True)
        return False

    @admin_group.command(name="health", description="Проверить Discord, RCON и целостность БД")
    @app_commands.guild_only()
    async def admin_health(self, interaction: discord.Interaction):
        if not await self._check_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        lines = [f"**Discord:** OK ({self.bot.user})", f"**Пользователей:** {len(seed_list_users())}"]
        for label, client in (("Сервер 1", self.rcon), ("Сервер 2", self.rcon2)):
            if not client:
                lines.append(f"**{label} RCON:** не настроен")
                continue
            try:
                status = await client.status()
                lines.append(f"**{label} RCON:** OK — {status['player_count']} игроков, {status.get('server_name') or '?'}")
            except Exception as e:
                h = client.health
                lines.append(f"**{label} RCON:** DOWN ({e}), подряд ошибок: {h.get('consecutive_failures', 0)}")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @admin_group.command(name="points", description="Изменить баланс с записью в журнал")
    @app_commands.describe(user="Участник", amount="Положительное начисление или отрицательное списание", reason="Причина для аудита")
    @app_commands.guild_only()
    async def admin_points(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        amount: app_commands.Range[int, -1_000_000, 1_000_000],
        reason: str,
    ):
        if not await self._check_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        if amount == 0:
            await interaction.followup.send("Сумма не может быть нулевой.", ephemeral=True)
            return
        idem = secrets.token_hex(8)
        try:
            updated = seed_adjust_points(str(user.id), amount, reason, str(interaction.user.id), idem)
        except SeedDomainError as e:
            await interaction.followup.send(e.message, ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ Баланс <@{user.id}> изменен на **{amount:+d}**.\nНовый баланс: **{updated['balance']}**.",
            ephemeral=True,
        )

    @admin_group.command(name="time-add", description="Восстановить пропущенное время сидинга и баллы за текущую неделю")
    @app_commands.describe(user="Подтверждённый сидер", minutes="Пропущенное время: от 1 до 1440 минут", reason="Причина корректировки")
    @app_commands.guild_only()
    async def admin_time_add(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        minutes: app_commands.Range[int, 1, 1440],
        reason: str,
    ):
        if not await self._check_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        idem = secrets.token_hex(8)
        try:
            result = seed_add_time(str(user.id), minutes, reason, str(interaction.user.id), idem)
        except SeedDomainError as e:
            await interaction.followup.send(e.message, ephemeral=True)
            return
        if not result["applied"]:
            await interaction.followup.send("Эта операция уже была выполнена (дубликат).", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ <@{user.id}>: +{minutes} мин, +{result['points']} баллов (неделя {result['week_key']}).",
            ephemeral=True,
        )

    @admin_group.command(name="points-all", description="Начислить баллы всем подтверждённым сидерам")
    @app_commands.describe(amount="Сколько баллов получит каждый сидер", reason="Причина массового начисления", operation_id="Уникальное имя начисления", confirm="true — начислить")
    @app_commands.guild_only()
    async def admin_points_all(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 1_000_000],
        reason: str,
        operation_id: str,
        confirm: bool,
    ):
        if not await self._check_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        users = seed_list_users()
        if not confirm:
            await interaction.followup.send(
                f"Получателей: **{len(users)}**. Каждый получит **{amount}** баллов.\n"
                f"Повторите с `confirm:true` для выполнения. ID: `{operation_id}`",
                ephemeral=True,
            )
            return
        try:
            result = seed_grant_points_all(amount, reason, str(interaction.user.id), operation_id)
        except SeedDomainError as e:
            await interaction.followup.send(e.message, ephemeral=True)
            return
        if not result["applied"]:
            await interaction.followup.send("Эта операция уже была выполнена (дубликат).", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ Начислено **{result['total_points']}** баллов ({result['count']} сидерам × {amount}).",
            ephemeral=True,
        )

    @admin_group.command(name="time-add-all", description="Добавить время и заработанные баллы всем сидерам")
    @app_commands.describe(minutes="Сколько минут получит каждый сидер", reason="Причина массовой корректировки", operation_id="Уникальное имя начисления", confirm="true — начислить")
    @app_commands.guild_only()
    async def admin_time_all(
        self,
        interaction: discord.Interaction,
        minutes: app_commands.Range[int, 1, 1440],
        reason: str,
        operation_id: str,
        confirm: bool,
    ):
        if not await self._check_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        users = seed_list_users()
        if not confirm:
            await interaction.followup.send(
                f"Получателей: **{len(users)}**. Каждый получит **{minutes}** мин.\n"
                f"Повторите с `confirm:true` для выполнения. ID: `{operation_id}`",
                ephemeral=True,
            )
            return
        try:
            result = seed_add_time_all(minutes, reason, str(interaction.user.id), operation_id)
        except SeedDomainError as e:
            await interaction.followup.send(e.message, ephemeral=True)
            return
        if not result["applied"]:
            await interaction.followup.send("Эта операция уже была выполнена (дубликат).", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ Добавлено {minutes} мин всем ({result['count']} сидерам), суммарно +{result['total_points']} баллов.",
            ephemeral=True,
        )

    @admin_group.command(name="vip-sync", description="Немедленно сверить VIP с RCON")
    @app_commands.guild_only()
    async def admin_vip_sync(self, interaction: discord.Interaction):
        if not await self._check_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        if self._vip_running:
            await interaction.followup.send("Синхронизация VIP уже выполняется.", ephemeral=True)
            return
        self._vip_running = True
        try:
            await self._vip_reconcile()
        finally:
            self._vip_running = False
        await interaction.followup.send("✅ VIP-синхронизация завершена.", ephemeral=True)

    @admin_group.command(name="role-sync", description="Немедленно сверить Discord-роли (сидер/VIP) с данными")
    @app_commands.guild_only()
    async def admin_role_sync(self, interaction: discord.Interaction):
        if not await self._check_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        if self._role_running:
            await interaction.followup.send("Сверка ролей уже выполняется.", ephemeral=True)
            return
        self._role_running = True
        try:
            stats = await self._role_reconcile()
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка сверки ролей: {e}", ephemeral=True)
            return
        finally:
            self._role_running = False
        seed_audit(
            "role_sync_forced",
            str(interaction.user.id),
            stats,
        )
        await interaction.followup.send(
            "✅ Сверка ролей завершена.\n"
            f"Проверено участников: **{stats['checked']}**\n"
            f"Выдано роль «Сидер»: **{stats['seeder_granted']}**\n"
            f"Выдано VIP: **{stats['vip_granted']}**\n"
            f"Снято VIP: **{stats['vip_revoked']}**",
            ephemeral=True,
        )

    @admin_group.command(name="report", description="Обновить рейтинг, топ-3 и их недельные награды")
    @app_commands.guild_only()
    async def admin_report(self, interaction: discord.Interaction):
        if not await self._check_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        if self._weekly_running or self._report_running:
            await interaction.followup.send("Отчёт/награды уже обновляются.", ephemeral=True)
            return
        self._weekly_running = True
        try:
            await self._weekly_reconcile()
        finally:
            self._weekly_running = False
        self._report_running = True
        try:
            await self._report_refresh()
        finally:
            self._report_running = False
        await interaction.followup.send("✅ Рейтинг и недельные награды обновлены.", ephemeral=True)

    @admin_group.command(name="seeders", description="Показать список привязанных сидеров")
    @app_commands.describe(page="Страница списка")
    @app_commands.guild_only()
    async def admin_seeders(self, interaction: discord.Interaction, page: app_commands.Range[int, 1, 100] = 1):
        if not await self._check_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        rows = seed_list_seeders(seed_week_key(_now_ms()))
        per_page = 15
        total_pages = max(1, (len(rows) + per_page - 1) // per_page)
        page = min(page, total_pages)
        chunk = rows[(page - 1) * per_page : page * per_page]
        if not chunk:
            await interaction.followup.send("Сидеров пока нет.", ephemeral=True)
            return
        lines = []
        for i, row in enumerate(chunk, start=(page - 1) * per_page + 1):
            vip = row.get("vip") or {}
            vip_mark = "🟢" if vip.get("state") == "active" else "⚪"
            lines.append(
                f"**{i}.** {row['display_name']} — {row['balance']} балл, {_fmt_duration(row['total_seconds'])} всего, "
                f"{_fmt_duration(row.get('weekly_seconds', 0))} на неделе {vip_mark}"
            )
        embed = discord.Embed(
            color=discord.Color.blurple(),
            title=f"📋 Сидеры (стр. {page}/{total_pages})",
            description="\n".join(lines),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @admin_group.command(name="reserved-protect", description="Выдать и защитить постоянный Reserved slot")
    @app_commands.describe(user="Участник с подтверждённой привязкой Steam")
    @app_commands.guild_only()
    async def admin_reserved_protect(self, interaction: discord.Interaction, user: discord.Member):
        if not await self._check_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        ident = seed_get_vip_identity_by_discord(str(user.id))
        if not ident or not ident.get("steam_id"):
            await interaction.followup.send("У участника нет подтверждённой привязки Steam.", ephemeral=True)
            return
        entry, created = ReservedProtectionService.protect(ident, str(interaction.user.id))
        if self.rcon:
            try:
                await self.rcon.set_reserved(entry["steamId"], True)
            except Exception as e:
                await interaction.followup.send(
                    f"Запись о защите сохранена, но RCON не выдал слот: {e}", ephemeral=True
                )
                return
        await interaction.followup.send(
            f"🔒 Постоянный Reserved slot защищён для <@{user.id}> (`{entry['steamId']}`).\n"
            f"Бот не будет снимать этот слот при истечении VIP/наград.",
            ephemeral=True,
        )

    @admin_group.command(name="reserved-unprotect", description="Снять защиту, не удаляя Reserved slot")
    @app_commands.describe(user="Участник с постоянным Reserved slot")
    @app_commands.guild_only()
    async def admin_reserved_unprotect(self, interaction: discord.Interaction, user: discord.Member):
        if not await self._check_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        entry, removed = ReservedProtectionService.unprotect_by_discord(str(user.id), str(interaction.user.id))
        if not removed:
            await interaction.followup.send("Защиты для этого участника нет.", ephemeral=True)
            return
        await interaction.followup.send(
            f"🔓 Защита снята с <@{user.id}>. Сам Reserved slot остаётся, пока не истечёт.",
            ephemeral=True,
        )

    @admin_group.command(name="reserved-protected", description="Показать защищённые постоянные Reserved slots")
    @app_commands.describe(page="Страница списка")
    @app_commands.guild_only()
    async def admin_reserved_protected(self, interaction: discord.Interaction, page: app_commands.Range[int, 1, 100] = 1):
        if not await self._check_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        entries = ReservedProtectionService.list_entries()
        if not entries:
            await interaction.followup.send("Нет защищённых слотов.", ephemeral=True)
            return
        per_page = 15
        total_pages = max(1, (len(entries) + per_page - 1) // per_page)
        page = min(page, total_pages)
        chunk = entries[(page - 1) * per_page : page * per_page]
        lines = [
            f"**{i}.** {e.get('displayName')} — `{e.get('steamId')}` (<@{e.get('discordId')}>)"
            for i, e in enumerate(chunk, start=(page - 1) * per_page + 1)
        ]
        embed = discord.Embed(
            color=discord.Color.red(),
            title=f"🔒 Защищённые Reserved slots (стр. {page}/{total_pages})",
            description="\n".join(lines),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @admin_group.command(name="vip-role-sync", description="Синхронизировать VIP-роль в Discord → VIP-слоты на сайте")
    @app_commands.describe(reserved="Также выдать Reserved slot на игровых серверах через RCON")
    @app_commands.guild_only()
    async def admin_vip_role_sync(self, interaction: discord.Interaction, reserved: bool = False):
        if not await self._check_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        vip_role = guild.get_role(config.VIP_ROLE_ID_SEED) if config.VIP_ROLE_ID_SEED else None
        if not vip_role:
            await interaction.followup.send("VIP-роль не настроена (VIP_ROLE_ID_SEED).", ephemeral=True)
            return
        panel_db = get_panel_conn()
        now = time.time()
        created: list[tuple[str, str]] = []
        skipped: list[str] = []
        no_binding: list[str] = []
        rcon_errors: list[str] = []
        for member in guild.members:
            if member.bot or vip_role not in member.roles:
                continue
            row = panel_db.execute(
                "SELECT user_id FROM bindings WHERE discord_id = ?", (str(member.id),)
            ).fetchone()
            steam_id = str(row["user_id"]) if row else ""
            from_seed = False
            if not steam_id or not STEAM_ID_RE.match(steam_id):
                seeder_user = seed_get_user_by_discord(str(member.id))
                steam_id = (seeder_user or {}).get("steam_id") or ""
                from_seed = bool(steam_id)
            if not steam_id or not STEAM_ID_RE.match(steam_id):
                donor = seed_get_donor_by_discord(str(member.id))
                steam_id = (donor or {}).get("steam_id") or ""
                from_seed = bool(steam_id)
            if not steam_id or not STEAM_ID_RE.match(steam_id):
                no_binding.append(member.display_name)
                continue
            if from_seed:
                clash = panel_db.execute(
                    "SELECT discord_id FROM bindings WHERE user_id = ? AND discord_id <> ?",
                    (steam_id, str(member.id)),
                ).fetchone()
                if not clash:
                    panel_db.execute(
                        "INSERT INTO bindings (user_id, discord_id, discord_name, guild_member, roles, updated_utc) "
                        "VALUES (?, ?, ?, 1, '[]', ?) "
                        "ON CONFLICT(user_id) DO UPDATE SET discord_id = excluded.discord_id, "
                        "discord_name = excluded.discord_name, guild_member = 1, updated_utc = excluded.updated_utc",
                        (steam_id, str(member.id), member.display_name[:100], now),
                    )
            active = panel_db.execute(
                "SELECT 1 FROM vip_slots WHERE steam_id = ? AND (expires_utc IS NULL OR expires_utc > ?) LIMIT 1",
                (steam_id, now),
            ).fetchone()
            if active:
                skipped.append(steam_id)
                continue
            display_name = " ".join(member.display_name.split())[:120]
            for sid in ("ru1", "ru2"):
                panel_db.execute(
                    "INSERT INTO vip_slots "
                    "(id, server_id, steam_id, display_name, expires_utc, note, created_by, "
                    "created_utc, updated_utc, sync_state, sync_error) "
                    "VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, 'pending', '') "
                    "ON CONFLICT(server_id, steam_id) DO UPDATE SET "
                    "expires_utc = NULL, updated_utc = ?, sync_state = 'pending', sync_error = ''",
                    (
                        secrets.token_hex(12), sid, steam_id, display_name,
                        "Синхронизация Discord-роль VIP",
                        f"discord-role-sync:{interaction.user.id}",
                        now, now, now,
                    ),
                )
            created.append((display_name, steam_id))
            if reserved:
                rcon_ok = 0
                for label, client in (("Сервер 1", self.rcon), ("Сервер 2", self.rcon2)):
                    if not client:
                        continue
                    try:
                        await client.set_reserved(steam_id, True)
                        rcon_ok += 1
                    except Exception as e:
                        rcon_errors.append(f"{steam_id} · {label}: {e}")
                if rcon_ok:
                    panel_db.execute(
                        "UPDATE vip_slots SET sync_state = 'synced', synced_utc = ?, updated_utc = ? "
                        "WHERE steam_id = ? AND sync_state = 'pending'",
                        (now, now, steam_id),
                    )
        seed_audit(
            "vip_role_sync",
            str(interaction.user.id),
            {"created": len(created), "skipped_active": len(skipped), "no_binding": len(no_binding)},
        )
        lines = [
            f"Создано слотов: **{len(created)}** (постоянные, оба сервера)",
            f"Уже активны на сайте: **{len(skipped)}**",
            f"Без привязки Steam: **{len(no_binding)}**",
        ]
        if created:
            names = "\n".join(f"• {n} — `{s}`" for n, s in created[:30])
            lines.append(f"\n**Синхронизировано:**\n{names}")
            if len(created) > 30:
                lines.append(f"… и ещё {len(created) - 30}")
        if no_binding:
            hint = ", ".join(no_binding[:15])
            more = f" и ещё {len(no_binding) - 15}" if len(no_binding) > 15 else ""
            lines.append(
                f"\n**Нужно привязать Steam** (роль останется незащищённой — попросите выполнить `/steam link`):\n{hint}{more}"
            )
        if rcon_errors:
            lines.append("\n**Ошибки RCON:**\n" + "\n".join(f"• {e}" for e in rcon_errors[:10]))
        embed = discord.Embed(
            color=discord.Color.green() if created else discord.Color.blurple(),
            title="🔄 Синхронизация VIP-роли → сайт",
            description="\n".join(lines),
        )
        if reserved:
            embed.set_footer(text="Reserved slots выданы через RCON; остальные слоты в статусе pending (панель дособерёт).")
        else:
            embed.set_footer(text="Слоты на сайте созданы; роль VIP бот больше не снимет. Reserved slot на серверах не менялся.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @admin_group.command(name="backup", description="Отправить снимок состояния сидинга в канал бэкапов")
    @app_commands.guild_only()
    async def admin_backup(self, interaction: discord.Interaction):
        if not await self._check_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await self._send_seed_backup()
        await interaction.followup.send("✅ Снимок отправлен.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(SeedingCog(bot))

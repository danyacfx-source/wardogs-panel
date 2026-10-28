"""Донат-бот: поддержка, VIP-шоп, спонсор-роли, топ-10, thanks-канал.

Полный порт bots/donation-bot/src/ на Python/discord.py.
Читает донат-фид панели (/donate/feed.php), валидирует платежи HMAC,
выдаёт спонсор-роли, публикует благодарности и рейтинг.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
import urllib.parse
from datetime import datetime, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from database import (
    get_donation_meta, set_donation_meta,
    save_donation_order, get_donation_order,
    save_donation_cart, get_donation_cart, delete_donation_cart,
    get_donor_total, set_donor_total, list_donors, get_all_donor_ids,
    save_donation_payment, get_donation_payment, mark_payment_field, list_pending_payments,
    save_donation_review, delete_donation_review, list_donation_reviews,
    save_donation_vip, list_active_vip,
    save_donation_adjustment, get_donation_adjustment, mark_adjustment_done, list_pending_adjustments,
    get_feed_offset, set_feed_offset, is_top_dirty, set_top_dirty,
)

log = logging.getLogger("donation")

TIERS = config.DONATION_TIERS or []

PRODUCTS = {
    "personal_1": {"name": "Личный VIP · 1 месяц", "kind": "personal", "months": 1, "seats": 1, "amount": 50000},
    "personal_3": {"name": "Личный VIP · 3 месяца", "kind": "personal", "months": 3, "seats": 1, "amount": 120000},
    "personal_12": {"name": "Личный VIP · 12 месяцев", "kind": "personal", "months": 12, "seats": 1, "amount": 480000},
    "clan_10": {"name": "VIP для клана · 10 мест · 1 месяц", "kind": "clan", "months": 1, "seats": 10, "amount": 450000},
    "clan_20": {"name": "VIP для клана · 20 мест · 1 месяц", "kind": "clan", "months": 1, "seats": 20, "amount": 850000},
    "clan_30": {"name": "VIP для клана · 30 мест · 1 месяц", "kind": "clan", "months": 1, "seats": 30, "amount": 1200000},
}

VIP_ROLE_ID = config.DONATION_VIP_ROLE_ID
CHANNEL_SUPPORT = config.DONATION_CHANNEL_SUPPORT
CHANNEL_VIP = config.DONATION_CHANNEL_VIP
CHANNEL_THANKS = config.DONATION_CHANNEL_THANKS
CHANNEL_TOP = config.DONATION_CHANNEL_TOP

ORDER_LIFETIME_MS = 2 * 60 * 60 * 1000
PAYMENT_GRACE_MS = 24 * 60 * 60 * 1000
NO_MENTIONS = discord.AllowedMentions(users=True, roles=False, everyone=False)


class ManualDonationError(Exception):
    pass


def format_rub(kopecks: int) -> str:
    return f"{kopecks / 100:,.2f} ₽".replace(",", " ").replace(".00 ₽", " ₽")


def parse_rub(raw: str) -> int:
    s = raw.strip().replace(",", ".")
    if not re.match(r"^[1-9]\d{0,5}(?:\.\d{1,2})?$", s):
        raise ManualDonationError("Укажите сумму от 50 до 100 000 ₽.")
    parts = s.split(".")
    r = int(parts[0])
    k = int(parts[1].ljust(2, "0")) if len(parts) > 1 else 0
    amount = r * 100 + k
    if amount < 5000 or amount > 10000000:
        raise ManualDonationError("Укажите сумму от 50 до 100 000 ₽.")
    return amount


def parse_removal_rub(raw: str) -> int:
    s = raw.strip().replace(",", ".")
    if not re.match(r"^(?:0|[1-9]\d{0,5})(?:\.\d{1,2})?$", s):
        raise ManualDonationError("Укажите сумму списания от 0,01 до 100 000 ₽.")
    parts = s.split(".")
    r = int(parts[0])
    k = int(parts[1].ljust(2, "0")) if len(parts) > 1 else 0
    amount = r * 100 + k
    if amount < 1 or amount > 10000000:
        raise ManualDonationError("Укажите сумму списания от 0,01 до 100 000 ₽.")
    return amount


def tier_for(total: int):
    for t in reversed(TIERS):
        if total >= t["min"]:
            return t
    return None


def add_calendar_months(start_ms: int, months: int) -> int:
    d = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    day = d.day
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    import calendar
    last_day = calendar.monthrange(year, month)[1]
    d = d.replace(year=year, month=month, day=min(day, last_day))
    return int(d.timestamp() * 1000)


def create_order(buyer_id, kind, amount, product_id=None, recipients=None, public_thanks=True, now=None):
    now = now or int(time.time() * 1000)
    recipients = recipients or []
    order_id = secrets.token_hex(16)
    return {
        "id": order_id, "buyerId": str(buyer_id), "kind": kind, "productId": product_id,
        "amountKopecks": amount, "recipients": [str(r) for r in recipients],
        "publicThanks": bool(public_thanks), "createdAt": now, "expiresAt": now + ORDER_LIFETIME_MS,
    }


def checkout_token(order, secret: str) -> str:
    payload = {
        "id": order["id"], "buyerId": order["buyerId"], "kind": order["kind"],
        "productId": order.get("productId"), "recipients": order.get("recipients", []),
        "amountKopecks": order["amountKopecks"], "createdAt": order["createdAt"], "expiresAt": order["expiresAt"],
    }
    base = __import__("base64").urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    sig = hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()
    return f"{base}.{sig}"


def checkout_url(order, site_base: str, secret: str) -> str:
    token = checkout_token(order, secret)
    return f"{site_base.rstrip('/')}/donate/index.php?o={urllib.parse.quote(token)}"


def payment_problem(event, order, checkout_secret):
    if not order or event.get("orderId") != order["id"]:
        return "unknown_order"
    op_id = event.get("operationId", "")
    if not re.match(r"^[A-Za-z0-9-]{1,80}$", op_id) or event.get("currency") != "643":
        return "invalid_event"
    if not isinstance(event.get("grossKopecks"), int) or not isinstance(event.get("timestamp"), int):
        return "invalid_event"
    if event["grossKopecks"] != order["amountKopecks"]:
        return "amount_mismatch"
    ts_ms = event["timestamp"] * 1000
    if ts_ms < order["createdAt"] - 60000 or ts_ms > order["expiresAt"] + PAYMENT_GRACE_MS:
        return "time_mismatch"
    if order["kind"] != "donation":
        if not event.get("orderToken"):
            return "missing_order_token"
        if event["orderToken"] != checkout_token(order, checkout_secret):
            return "order_token_mismatch"
    return None


class DonationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.site_base = getattr(config, "DONATION_SITE_BASE_URL", "")
        self.checkout_secret = getattr(config, "DONATION_CHECKOUT_SECRET", "")
        self.feed_token = getattr(config, "DONATION_FEED_TOKEN", "")
        self.poll_seconds = getattr(config, "DONATION_POLL_SECONDS", 45)
        self._ticking = False

    async def cog_load(self):
        if self.site_base and self.checkout_secret and self.feed_token:
            self.feed_loop.start()
        # Авто-upsert панелей на старте: поддержать/VIP работают сразу после рестарта
        # без ручного /donation-admin setup.
        asyncio.create_task(self._startup_upsert())

    async def _startup_upsert(self):
        await self.bot.wait_until_ready()
        guild = self.bot.get_guild(config.GUILD_ID)
        if guild is None:
            return
        # Не создаём каналы/спонсорский чат — только обновляем уже настроенные панели.
        for key, ch_id, embed_fn in (
            ("support", CHANNEL_SUPPORT, self._support_embed),
            ("vip", CHANNEL_VIP, self._vip_embed),
        ):
            try:
                ch = guild.get_channel(ch_id)
                if ch is None:
                    continue
                # Собираем view заново (custom_id те же — on_interaction разрулит).
                if key == "support":
                    btn = discord.ui.View()
                    btn.add_item(discord.ui.Button(label="Поддержать", emoji="💛", style=discord.ButtonStyle.primary, custom_id="support:open"))
                    comps = [btn]
                else:
                    vip_view = discord.ui.View()
                    personal_select = discord.ui.Select(custom_id="vip:personal", placeholder="Выбрать личный VIP",
                        options=[discord.SelectOption(label=PRODUCTS[k]["name"], value=k, description=format_rub(PRODUCTS[k]["amount"]))
                                 for k in ["personal_1", "personal_3", "personal_12"]])
                    clan_select = discord.ui.Select(custom_id="vip:clan", placeholder="Выбрать VIP для клана",
                        options=[discord.SelectOption(label=PRODUCTS[k]["name"], value=k, description=format_rub(PRODUCTS[k]["amount"]))
                                 for k in ["clan_10", "clan_20", "clan_30"]])
                    vip_view.add_item(personal_select)
                    vip_view2 = discord.ui.View()
                    vip_view2.add_item(clan_select)
                    comps = [vip_view, vip_view2]
                await self._upsert_panel(guild, key, ch_id, embed_fn(), comps)
                log.info("Панель %s обновлена на старте", key)
            except Exception as e:
                log.warning("Не удалось обновить панель %s на старте: %s", key, e)
        # Топ-10 тоже.
        try:
            await self._refresh_top(guild)
            log.info("Топ спонсоров обновлён на старте")
        except Exception as e:
            log.warning("Не удалось обновить топ на старте: %s", e)

    # ---- Slash commands ----

    donate_group = app_commands.Group(name="donate", description="Поддержка WARDOGS RU")

    @donate_group.command(name="status", description="Проверка статуса и VIP")
    async def donate_status(self, interaction: discord.Interaction):
        await self._cmd_status(interaction)

    donation_admin = app_commands.Group(
        name="donation-admin",
        description="Управление донатным ботом",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    async def _admin_guild(self, interaction: discord.Interaction):
        member_perms = interaction.member.permissions if interaction.member else None
        if not member_perms or not member_perms.manage_guild:
            await interaction.response.send_message("Недостаточно прав", ephemeral=True)
            return None
        await interaction.response.defer(ephemeral=True)
        return interaction.guild or self.bot.get_guild(config.GUILD_ID)

    @donation_admin.command(name="setup", description="Настроить VIP роль, создать или обновить донат-канал")
    async def admin_setup(self, interaction: discord.Interaction):
        guild = await self._admin_guild(interaction)
        if guild is None:
            return
        try:
            vip_role_id, sponsor_ch_id = await self._do_setup(guild)
            await interaction.followup.send(
                f"Карточки опубликованы. VIP роль: <@&{vip_role_id}>. Чат спонсоров: <#{sponsor_ch_id}>. "
                "Выдачей общей VIP роли за оплату и баллы управляет бот сидеров.")
        except Exception as e:
            await interaction.followup.send(f"Ошибка setup: {e}")

    @donation_admin.command(name="check", description="Проверить журнал платежей и баланс")
    async def admin_check(self, interaction: discord.Interaction):
        guild = await self._admin_guild(interaction)
        if guild is None:
            return
        try:
            msg = await self._tick_summary(guild)
            await interaction.followup.send(msg)
        except Exception as e:
            log.error("Donation check failed", exc_info=True)
            await interaction.followup.send(
                f"Не удалось прочитать журнал платежей: {e}. Проверьте SITE_BASE_URL и FEED_TOKEN.")

    @donation_admin.command(name="review", description="Проверить платежи, требующие ручной проверки")
    async def admin_review(self, interaction: discord.Interaction):
        guild = await self._admin_guild(interaction)
        if guild is None:
            return
        reasons = {
            "unknown_order": "нет заказа в данных бота",
            "order_already_paid": "ссылка заказа уже оплачивалась",
            "amount_mismatch": "сумма отличается от заказа",
            "time_mismatch": "время перевода вне срока заказа",
            "invalid_event": "ошибка формата уведомления",
            "missing_order_token": "нет сохранённого подписанного VIP заказа",
            "order_token_mismatch": "подписанный VIP заказ не совпадает",
        }
        reviews = list_donation_reviews()
        lines = [f"• {r['operation_id']}: {reasons.get(r['reason'], r['reason'])}" for r in reviews[:20]]
        await interaction.followup.send("\n".join(lines) if lines else "нет")

    @donation_admin.command(name="add", description="Ручное добавление доната: сумма, роль, ранги и баллы")
    @app_commands.describe(
        user="Пользователь, чей донат будет учтён",
        amount="Сумма в рублях от 50 до 100 000, например 500 или 500,50",
        reason="Причина ручного доната; пишется только в официальных целях",
        operation_id="ID операции ЮMoney; если не указан — формируется автоматически",
    )
    async def admin_add(self, interaction: discord.Interaction, user: discord.User,
                        amount: str, reason: str, operation_id: str = None):
        guild = await self._admin_guild(interaction)
        if guild is None:
            return
        await self._cmd_admin_add(interaction, guild)

    @donation_admin.command(name="remove", description="Ручное списание доната с возвратом роли и баллов")
    @app_commands.describe(
        user="Пользователь, у которого снижается донат",
        amount="Сумма списания в рублях от 0,01 до 100 000, например 500 или 0,50",
        reason="Причина списания; пишется только в официальных целях",
    )
    async def admin_remove(self, interaction: discord.Interaction, user: discord.User,
                           amount: str, reason: str):
        guild = await self._admin_guild(interaction)
        if guild is None:
            return
        await self._cmd_admin_remove(interaction, guild)

    def cog_unload(self):
        if self.feed_loop.is_running():
            self.feed_loop.cancel()

    # ---- Feed ----

    async def _read_feed(self, offset: int):
        url = f"{self.site_base.rstrip('/')}/donate/feed.php?offset={offset}"
        headers = {"X-WD-Feed-Token": self.feed_token}
        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Donation feed HTTP {resp.status}")
                body = await resp.json()
        events = body.get("events")
        next_offset = body.get("nextOffset")
        if not isinstance(events, list) or not isinstance(next_offset, int) or next_offset < offset or len(events) > 100:
            raise RuntimeError("Invalid donation feed")
        return {"events": events, "nextOffset": next_offset}

    def _apply_payment(self, event, now_ms, checkout_secret):
        op_id = event.get("operationId", "")
        existing = get_donation_payment(op_id)
        if existing:
            return {"status": "duplicate"}
        order = get_donation_order(event.get("orderId", ""))
        # check if order already paid
        conn_payments = list_pending_payments()
        # simpler: check any payment with same order_id
        from database import get_conn
        row = get_conn().execute(
            "SELECT operation_id FROM donations_payments WHERE order_id=?", (event.get("orderId", ""),)
        ).fetchone()
        if row:
            save_donation_review(op_id, json.dumps(event, ensure_ascii=False), "order_already_paid")
            return {"status": "review"}
        problem = payment_problem(event, order, checkout_secret)
        if problem:
            save_donation_review(op_id, json.dumps(event, ensure_ascii=False), problem)
            return {"status": "review"}
        delete_donation_review(op_id)
        buyer_id = order["buyerId"]
        total = get_donor_total(int(buyer_id))
        before = tier_for(total)
        total += event["grossKopecks"]
        set_donor_total(int(buyer_id), total, order.get("publicThanks", True))
        after = tier_for(total)
        payment = {
            "operationId": op_id, "orderId": order["id"], "buyerId": buyer_id,
            "amount": event["grossKopecks"], "kind": order["kind"], "productId": order.get("productId"),
            "timestamp": event["timestamp"] * 1000, "totalAfter": total,
            "previousRoleId": before["roleId"] if before else None,
            "newRoleId": after["roleId"] if after else None,
            "roleDone": False, "thanksDone": not order.get("publicThanks", True),
            "vipDone": order["kind"] == "donation",
        }
        save_donation_payment(payment)
        set_top_dirty(True)
        if order["kind"] != "donation":
            product = PRODUCTS.get(order.get("productId", ""))
            if product:
                paid_at = event["timestamp"] * 1000
                for rid in order.get("recipients", []):
                    prev_end = paid_at
                    for v in list_active_vip(int(rid), paid_at):
                        if v["ends_at"] > prev_end:
                            prev_end = v["ends_at"]
                    vip_id = f"{op_id}:{rid}"
                    save_donation_vip({
                        "id": vip_id, "userId": int(rid), "buyerId": int(buyer_id),
                        "productId": order.get("productId"), "startsAt": prev_end,
                        "endsAt": add_calendar_months(prev_end, product["months"]),
                    })
        return {"status": "credited", "payment": payment}

    async def _import_feed(self):
        added = seen = held = 0
        for _ in range(20):
            offset = get_feed_offset()
            batch = await self._read_feed(offset)
            if not batch["events"]:
                break
            for event in batch["events"]:
                result = self._apply_payment(event, int(time.time() * 1000), self.checkout_secret)
                if result["status"] == "credited":
                    added += 1
                if result["status"] == "review":
                    held += 1
                seen += 1
            set_feed_offset(batch["nextOffset"])
            if len(batch["events"]) < 100 or batch["nextOffset"] == offset:
                break
        return {"added": added, "seen": seen, "held": held}

    # ---- Delivery ----

    async def _sync_sponsor_member(self, guild, user_id, member=None):
        total = get_donor_total(user_id)
        tier = tier_for(total)
        desired = tier["roleId"] if tier else None
        if member is None:
            member = await guild.fetch_member(user_id)
        if desired and desired not in [r.id for r in member.roles]:
            await member.add_roles(discord.Object(id=desired), reason="Накопленная поддержка WARDOGS RU")
        for t in TIERS:
            if t["roleId"] != desired and t["roleId"] in [r.id for r in member.roles]:
                await member.remove_roles(discord.Object(id=t["roleId"]), reason="Обновление уровня спонсора")

    def _thanks_marker(self, op_id: str) -> str:
        return hashlib.sha256(op_id.encode()).hexdigest()[:12]

    def _thanks_embed(self, p):
        marker = self._thanks_marker(p["operation_id"])
        total_after = p.get("total_after") or 0
        tier = tier_for(total_after)
        gained = p.get("new_role_id") and p.get("new_role_id") != p.get("previous_role_id")
        color = 0xF2C26B if gained else 0x659DCB
        title = "🎉 Новый уровень поддержки!" if gained else "💛 Спасибо за поддержку!"
        embed = discord.Embed(color=color, title=title,
                              description=f"Спасибо <@{p['buyer_id']}> за вклад в развитие WARDOGS RU!")
        embed.set_author(name="WARDOGS RU · БЛАГОДАРНОСТЬ")
        embed.add_field(name="Последний перевод", value=f"**{format_rub(p.get('amount') or 0)}**", inline=True)
        embed.add_field(name="Всего поддержано", value=f"**{format_rub(total_after)}**", inline=True)
        embed.add_field(name="Спонсорская роль",
                        value=f"{tier['icon']} **{tier['name']}**" if tier else "Пока не достигнут первый порог")
        embed.set_footer(text=f"WARDOGS RU · платёж {marker}")
        if gained and tier:
            embed.add_field(name="🏅 Поздравляем!",
                            value=f"Вы достигли уровня **{tier['name']}**. Спасибо, что помогаете нашей стае расти!")
        return embed

    async def _send_thanks(self, guild, p):
        ch = guild.get_channel(CHANNEL_THANKS)
        if ch is None:
            ch = await self.bot.fetch_channel(CHANNEL_THANKS)
        marker = self._thanks_marker(p["operation_id"])
        async for msg in ch.history(limit=50):
            if msg.author.id == self.bot.user.id and msg.embeds and marker in (msg.embeds[0].footer.text or ""):
                mark_payment_field(p["operation_id"], "thanks_done")
                return
        embed = self._thanks_embed(p)
        await ch.send(embed=embed, allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=p["buyer_id"])]))
        mark_payment_field(p["operation_id"], "thanks_done")

    async def _deliver_payments(self, guild):
        errors = []
        for p in list_pending_payments():
            if not p.get("role_done"):
                try:
                    await self._sync_sponsor_member(guild, p["buyer_id"])
                    mark_payment_field(p["operation_id"], "role_done")
                except Exception as e:
                    errors.append(f"роль ({p['operation_id']}): {e}")
            if not p.get("thanks_done"):
                try:
                    await self._send_thanks(guild, p)
                except Exception as e:
                    errors.append(f"благодарность ({p['operation_id']}): {e}")
        return errors

    async def _deliver_adjustments(self, guild):
        errors = []
        for a in list_pending_adjustments():
            try:
                await self._sync_sponsor_member(guild, a["buyer_id"])
                mark_adjustment_done(a["id"])
            except Exception as e:
                errors.append(f"роль после списания ({a['id']}): {e}")
        return errors

    def _top_embed(self):
        rows = list_donors()
        marks = ["🥇", "🥈", "🥉"]
        lines = []
        for i, d in enumerate(rows[:10]):
            tier = tier_for(d["total"])
            tier_str = f"  ·  {tier['icon']} {tier['name']}" if tier else ""
            lines.append(f"{marks[i] if i < 3 else '◆'} **{i + 1}.** <@{d['user_id']}>  ·  **{format_rub(d['total'])}**{tier_str}")
        desc = "\n\n".join(lines) if lines else "Пока нет подтверждённых публичных донатов. Первый герой стаи может появиться здесь!"
        e = discord.Embed(color=0xE0B75B, title="🏆 Топ 10 спонсоров", description=desc)
        e.set_author(name="WARDOGS RU · ЗАЛ СЛАВЫ")
        if self.site_base:
            e.set_thumbnail(url=f"{self.site_base.rstrip('/')}/static/donate/assets/legend.png?v=20261018")
        e.set_footer(text="Рейтинг обновляется после каждого подтверждённого доната · суммы накопительные")
        return e

    async def _refresh_top(self, guild):
        ch = guild.get_channel(CHANNEL_TOP)
        if ch is None:
            ch = await self.bot.fetch_channel(CHANNEL_TOP)
        msg_id = get_donation_meta("panel_top")
        if msg_id:
            try:
                old = await ch.fetch_message(int(msg_id))
                if old.author.id == self.bot.user.id:
                    await old.edit(embed=self._top_embed())
                    return
            except Exception:
                pass
        sent = await ch.send(embed=self._top_embed(), allowed_mentions=NO_MENTIONS)
        set_donation_meta("panel_top", str(sent.id))

    async def _sync_sponsor_roles(self, guild):
        for uid in get_all_donor_ids():
            try:
                member = await guild.fetch_member(uid)
                await self._sync_sponsor_member(guild, uid, member)
            except discord.NotFound:
                pass
            except Exception as e:
                log.error("Sponsor reconciliation pending: %s %s", uid, e)

    # ---- Cards ----

    def _support_embed(self):
        tiers_text = "\n".join(f"{t['icon']} **{t['name']}** — от **{format_rub(t['min'])}**" for t in TIERS)
        embed = discord.Embed(color=0xD8AA5C, title="🐾 Поддержать нашу стаю",
                              description="Добровольная поддержка помогает оплачивать и развивать игровой сервер, развивать сообщество, проводить события и создавать новые возможности для игроков. Минимальный перевод — 50 ₽. Каждый подтверждённый перевод прибавляется к вашей общей сумме.")
        embed.set_author(name="WARDOGS RU · СООБЩЕСТВО")
        if self.site_base:
            embed.set_thumbnail(url=f"{self.site_base.rstrip('/')}/static/donate/assets/paw.png?v=20261018")
        embed.add_field(name="🎖️ Спонсорские роли", value=tiers_text)
        embed.add_field(name="💎 Преимущества", value="Закрытый чат спонсоров. Возможность попросить администратора выбрать команду, если он онлайн и переход не создаст явного дисбаланса. Спонсорская роль сама по себе не выдаёт reserved slot.")
        embed.add_field(name="🔗 SteamID и игровой доступ", value="Для обычной поддержки Steam-привязка не нужна. Если покупаете VIP, привяжите Steam через `/steam link steam_id` и `/steam verify code` — эта единая привязка подходит и для сидинга, и для VIP.")
        embed.add_field(name="🤝 Прозрачность", value="Поддержка добровольна. Нажмите кнопку, укажите сумму и оплатите по персональной ссылке ЮMoney. Одна подтверждённая операция учитывается один раз.")
        embed.set_footer(text="WARDOGS RU · Вместе держим рубеж")
        return embed

    def _vip_embed(self):
        embed = discord.Embed(color=0x9D7DEB, title="💎 VIP-доступ на игровой сервер",
                              description=f"VIP действует указанный срок с момента подтверждения оплаты. Участник получает готовую роль <@&{VIP_ROLE_ID}> и reserved slot при привязанном SteamID. Покупка также входит в общую сумму донатов **покупателя**.")
        embed.set_author(name="WARDOGS RU · VIP")
        if self.site_base:
            embed.set_thumbnail(url=f"{self.site_base.rstrip('/')}/static/donate/assets/vip.png?v=20261018")
        embed.add_field(name="👤 Личный VIP", value="1 месяц — **500 ₽**\n3 месяца — **1 200 ₽**\n12 месяцев — **4 800 ₽**", inline=True)
        embed.add_field(name="🛡️ VIP для клана", value="10 участников / 1 месяц — **4 500 ₽**\n20 участников / 1 месяц — **8 500 ₽**\n30 участников / 1 месяц — **12 000 ₽**", inline=True)
        embed.add_field(name="📌 Порядок", value="Выберите вариант ниже. Для кланового пакета отметьте всех получателей до оплаты. Каждый получатель привязывает SteamID через `/steam link steam_id` и `/steam verify code`; уже привязанным повторять это не нужно. Без привязки слот не появится. Повторная покупка продлевает срок.")
        embed.set_footer(text="VIP роль действует оплаченный срок · слот сохраняется при другом действующем основании")
        return embed

    # ---- Setup ----

    @staticmethod
    def _as_single_view(components):
        """discord.py 2.7 отдаёт в send/edit только один View — склеиваем панель."""
        views = [view for view in (components or []) if view is not None]
        if not views:
            return None
        if len(views) == 1:
            return views[0]
        merged = discord.ui.View(timeout=None)
        for view in views:
            for item in list(view.children):
                merged.add_item(item)
        return merged

    async def _upsert_panel(self, guild, key, channel_id, embed, components=None):
        ch = guild.get_channel(channel_id)
        if ch is None:
            ch = await self.bot.fetch_channel(channel_id)
        msg_id = get_donation_meta(f"panel_{key}")
        payload = {"embed": embed, "allowed_mentions": NO_MENTIONS}
        view = self._as_single_view(components)
        if view:
            payload["view"] = view
        if msg_id:
            try:
                old = await ch.fetch_message(int(msg_id))
                if old.author.id == self.bot.user.id:
                    await old.edit(**payload)
                    return
            except Exception:
                pass
        sent = await ch.send(**payload)
        set_donation_meta(f"panel_{key}", str(sent.id))

    async def _do_setup(self, guild):
        for t in TIERS:
            role = guild.get_role(t["roleId"])
            if role is None or not role.is_assignable():
                raise RuntimeError(f"Cannot manage sponsor role {t['name']}")
        vip_role = guild.get_role(VIP_ROLE_ID)
        if vip_role is None:
            raise RuntimeError(f"Готовая VIP роль {VIP_ROLE_ID} не найдена на сервере")
        sponsor_ch_id = get_donation_meta("sponsor_channel_id")
        sponsor_ch = None
        if sponsor_ch_id:
            sponsor_ch = guild.get_channel(int(sponsor_ch_id))
        if sponsor_ch is None:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            }
            for t in TIERS:
                overwrites[discord.Object(id=t["roleId"])] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True)
            sponsor_ch = await guild.create_text_channel("💎・спонсоры", overwrites=overwrites, reason="Закрытый чат спонсоров")
            set_donation_meta("sponsor_channel_id", str(sponsor_ch.id))
        btn = discord.ui.View()
        btn.add_item(discord.ui.Button(label="Поддержать", emoji="💛", style=discord.ButtonStyle.primary, custom_id="support:open"))
        await self._upsert_panel(guild, "support", CHANNEL_SUPPORT, self._support_embed(), [btn])
        vip_view = discord.ui.View()
        personal_select = discord.ui.Select(custom_id="vip:personal", placeholder="Выбрать личный VIP",
            options=[discord.SelectOption(label=PRODUCTS[k]["name"], value=k, description=format_rub(PRODUCTS[k]["amount"]))
                     for k in ["personal_1", "personal_3", "personal_12"]])
        clan_select = discord.ui.Select(custom_id="vip:clan", placeholder="Выбрать VIP для клана",
            options=[discord.SelectOption(label=PRODUCTS[k]["name"], value=k, description=format_rub(PRODUCTS[k]["amount"]))
                     for k in ["clan_10", "clan_20", "clan_30"]])
        vip_view.add_item(personal_select)
        vip_view2 = discord.ui.View()
        vip_view2.add_item(clan_select)
        await self._upsert_panel(guild, "vip", CHANNEL_VIP, self._vip_embed(), [vip_view, vip_view2])
        await self._refresh_top(guild)
        return vip_role.id, sponsor_ch.id

    # ---- Feed loop ----

    @tasks.loop(seconds=45)
    async def feed_loop(self):
        if self._ticking:
            return
        self._ticking = True
        try:
            guild = self.bot.get_guild(config.GUILD_ID)
            if guild is None:
                return
            imported = None
            feed_error = None
            try:
                imported = await self._import_feed()
            except Exception as e:
                feed_error = e
                log.error("Donation feed unavailable: %s", e)
            errors = await self._deliver_payments(guild)
            errors.extend(await self._deliver_adjustments(guild))
            if is_top_dirty():
                try:
                    await self._refresh_top(guild)
                    set_top_dirty(False)
                except Exception as e:
                    errors.append(f"топ спонсоров: {e}")
            try:
                await self._sync_sponsor_roles(guild)
            except Exception as e:
                log.error("Sponsor role sync pending: %s", e)
            if errors:
                log.warning("Donation delivery errors: %s", errors[:5])
        finally:
            self._ticking = False

    @feed_loop.before_loop
    async def before_feed_loop(self):
        await self.bot.wait_until_ready()

    # ---- Interactions ----

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.guild_id != config.GUILD_ID:
            return
        try:
            if interaction.type == discord.InteractionType.component:
                cid = interaction.data.get("custom_id", "")
                if cid == "support:open":
                    modal = discord.ui.Modal(custom_id="support:amount", title="Поддержать WARDOGS RU")
                    amount_input = discord.ui.TextInput(label="Сумма в рублях, от 50 ₽", placeholder="Например, 50",
                                                        style=discord.TextStyle.short, required=True, max_length=9)
                    modal.add_item(amount_input)
                    await interaction.response.send_modal(modal)
                    return
                if cid.startswith("vip:personal") or cid.startswith("vip:clan"):
                    select = interaction.data
                    values = select.get("values", [])
                    if values:
                        await self._create_vip_selection(interaction, values[0])
                        return
                if cid.startswith("vip:recipients:"):
                    cart_id = cid.split(":")[2]
                    values = interaction.data.get("values", [])
                    await self._select_recipients(interaction, cart_id, values)
                    return

            if interaction.type == discord.InteractionType.modal_submit:
                cid = interaction.data.get("custom_id", "")
                if cid == "support:amount":
                    raw = interaction.data["components"][0]["components"][0]["value"]
                    amount = parse_rub(raw)
                    order = create_order(interaction.user.id, "donation", amount)
                    save_donation_order(order)
                    await self._send_checkout(interaction, order)
                    return

        except ManualDonationError as e:
            await self._respond_error(interaction, str(e))
        except Exception as e:
            log.error("Donation interaction failed", exc_info=True)
            await self._respond_error(interaction, "Ошибка обработки. Если платёж уже сделан, не повторяйте его: обратитесь к администрации.")

    async def _respond_error(self, interaction, msg):
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass

    async def _send_checkout(self, interaction, order, update=False):
        url = checkout_url(order, self.site_base, self.checkout_secret)
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Перейти к переводу", emoji="💛", url=url, style=discord.ButtonStyle.link))
        name = "Поддержка сообщества" if order["kind"] == "donation" else PRODUCTS.get(order.get("productId", ""), {}).get("name", "VIP")
        content = (f"**{name} · {format_rub(order['amountKopecks'])}**\n"
                   f"Откройте персональную страницу оплаты до <t:{order['expiresAt'] // 1000}:F>. "
                   "После оплаты статус обновится автоматически.")
        if update:
            await interaction.response.edit_message(content=content, view=view)
        else:
            await interaction.response.send_message(content=content, view=view, ephemeral=True)

    async def _create_vip_selection(self, interaction, product_id):
        product = PRODUCTS.get(product_id)
        if not product:
            return
        if product["kind"] == "personal":
            order = create_order(interaction.user.id, "personal", product["amount"],
                                 product_id=product_id, recipients=[interaction.user.id])
            save_donation_order(order)
            await self._send_checkout(interaction, order)
            return
        import secrets as _s
        cart = {"id": _s.token_hex(8), "buyerId": str(interaction.user.id),
                "productId": product_id, "recipients": [], "expiresAt": int(time.time() * 1000) + 15 * 60 * 1000}
        save_donation_cart(cart)
        left = product["seats"]
        view = discord.ui.View()
        select = discord.ui.UserSelect(custom_id=f"vip:recipients:{cart['id']}",
                                       placeholder=f"Выберите ещё {left} участников",
                                       min_values=1, max_values=min(25, left))
        view.add_item(select)
        await interaction.response.send_message(
            content=f"**{product['name']} — {format_rub(product['amount'])}**\n"
                    f"Выберите {product['seats']} разных участников сервера. Для 30 мест выбор проходит в два шага.",
            view=view, ephemeral=True)

    async def _select_recipients(self, interaction, cart_id, values):
        cart = get_donation_cart(cart_id)
        if not cart or cart["buyerId"] != str(interaction.user.id) or int(time.time() * 1000) > cart["expiresAt"]:
            await interaction.response.send_message("Выбор истёк. Откройте список VIP заново.", ephemeral=True)
            return
        product = PRODUCTS[cart["productId"]]
        recipients = list(cart["recipients"]) + [str(v) for v in values]
        save_donation_cart({**cart, "recipients": recipients})
        if len(recipients) < product["seats"]:
            left = product["seats"] - len(recipients)
            view = discord.ui.View()
            select = discord.ui.UserSelect(custom_id=f"vip:recipients:{cart['id']}",
                                           placeholder=f"Выберите ещё {left} участников",
                                           min_values=1, max_values=min(25, left))
            view.add_item(select)
            await interaction.response.edit_message(
                content=f"Выбрано {len(recipients)} из {product['seats']}. Выберите оставшихся участников.", view=view)
            return
        order = create_order(interaction.user.id, "clan", product["amount"],
                             product_id=cart["productId"], recipients=recipients)
        save_donation_order(order)
        delete_donation_cart(cart_id)
        url = checkout_url(order, self.site_base, self.checkout_secret)
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Оплатить VIP для клана", url=url, style=discord.ButtonStyle.link))
        mentions = ", ".join(f"<@{r}>" for r in recipients)
        await interaction.response.edit_message(
            content=f"**{product['name']} · {format_rub(product['amount'])}**\n"
                    f"Получатели ({len(recipients)}): {mentions}\n"
                    f"Проверьте список перед оплатой. Ссылка действительна до <t:{order['expiresAt'] // 1000}:F>.",
            view=view)

    async def _cmd_status(self, interaction):
        uid = interaction.user.id
        total = get_donor_total(uid)
        tier = tier_for(total)
        now_ms = int(time.time() * 1000)
        vip_rows = list_active_vip(uid, now_ms)
        until = max((v["ends_at"] for v in vip_rows), default=None)
        vip_str = f"до <t:{until // 1000}:F>" if until else "не активен"
        content = (f"Накоплено: **{format_rub(total)}**. Спонсорская роль: **{tier['name'] if tier else 'пока нет'}**.\n"
                   f"VIP: {vip_str}.\n"
                   "Спонсорская роль не даёт игровой слот. Для оплаченного VIP используйте /steam link steam_id и /steam verify code — единая привязка.")
        await interaction.response.send_message(content, ephemeral=True)

    async def _tick_summary(self, guild):
        imported = None
        feed_error = None
        try:
            imported = await self._import_feed()
        except Exception as e:
            feed_error = e
        errors = await self._deliver_payments(guild)
        errors.extend(await self._deliver_adjustments(guild))
        if is_top_dirty():
            try:
                await self._refresh_top(guild)
                set_top_dirty(False)
            except Exception:
                pass
        from database import get_conn
        order_count = get_conn().execute("SELECT COUNT(*) c FROM donations_orders").fetchone()["c"]
        payment_count = get_conn().execute("SELECT COUNT(*) c FROM donations_payments").fetchone()["c"]
        roles_pending = get_conn().execute("SELECT COUNT(*) c FROM donations_payments WHERE role_done=0").fetchone()["c"]
        thanks_pending = get_conn().execute("SELECT COUNT(*) c FROM donations_payments WHERE thanks_done=0").fetchone()["c"]
        review_count = get_conn().execute("SELECT COUNT(*) c FROM donations_reviews").fetchone()["c"]
        adj_count = get_conn().execute("SELECT COUNT(*) c FROM donations_adjustments").fetchone()["c"]
        adj_pending = get_conn().execute("SELECT COUNT(*) c FROM donations_adjustments WHERE role_done=0").fetchone()["c"]
        if feed_error:
            site_status = f"Не удалось прочитать журнал сайта: {feed_error}. Сохранённые записи обработаны; платёж не повторяйте."
        else:
            site_status = (f"Сайт доступен. Прочитано новых уведомлений: {imported['seen']}; "
                           f"учтено платежей: {imported['added']}; отложено на проверку: {imported['held']}.")
        err_str = f"Ошибки: {' | '.join(errors[:3])}" if errors else "Ошибок выдачи при проверке нет."
        return (f"{site_status}\n"
                f"Всего заказов: {order_count}; подтверждённых платежей: {payment_count}; "
                f"ожидают выдачи роли: {roles_pending}; публикации благодарности: {thanks_pending}; "
                f"на ручной проверке: {review_count}.\n"
                f"Ручных списаний: {adj_count}; ожидают обновления роли: {adj_pending}.\n"
                f"Курсор журнала: {get_feed_offset()}. {err_str} Игровыми слотами управляет бот сидеров.")

    async def _cmd_admin_add(self, interaction, guild):
        opts = {o["name"]: o.get("value") for o in interaction.data.get("options", [{}])[0].get("options", [])}
        user_id = int(opts.get("user", 0))
        if not user_id:
            await interaction.followup.send("Укажите пользователя.")
            return
        member = guild.get_member(user_id)
        if member and member.bot:
            await interaction.followup.send("Выберите участника сообщества, а не бота.")
            return
        amount = parse_rub(str(opts.get("amount", "")))
        reason = str(opts.get("reason", "")).strip()
        op_id_raw = opts.get("operation_id")
        request_id = str(interaction.id)
        op_id = op_id_raw.strip() if op_id_raw else f"manual-{request_id}"
        # dedup
        existing = get_donation_payment(op_id)
        if existing:
            if existing["buyer_id"] != user_id or existing["amount"] != amount:
                await interaction.followup.send("Эта операция уже учтена с другой суммой или пользователем.")
                return
            tier = tier_for(get_donor_total(user_id))
            await interaction.followup.send(
                f"Эта операция уже учтена; повторного начисления нет.\n"
                f"Общая сумма: **{format_rub(get_donor_total(user_id))}**. "
                f"Спонсорская роль: **{tier['name'] if tier else 'пока нет'}**.\n"
                f"ID записи: `{op_id}`.")
            return
        order = create_order(user_id, "donation", amount, public_thanks=True)
        order["source"] = "manual"
        save_donation_order(order)
        now_ms = int(time.time() * 1000)
        total_before = get_donor_total(user_id)
        total_after = total_before + amount
        before_tier = tier_for(total_before)
        after_tier = tier_for(total_after)
        set_donor_total(user_id, total_after, True)
        payment = {
            "operationId": op_id, "orderId": order["id"], "buyerId": user_id,
            "amount": amount, "kind": "donation", "productId": None,
            "timestamp": now_ms, "totalAfter": total_after,
            "previousRoleId": before_tier["roleId"] if before_tier else None,
            "newRoleId": after_tier["roleId"] if after_tier else None,
            "roleDone": False, "thanksDone": False, "vipDone": True,
            "source": "manual", "adminId": interaction.user.id, "reason": reason,
            "recordedAt": now_ms, "requestId": request_id,
        }
        save_donation_payment(payment)
        set_top_dirty(True)
        errors = await self._deliver_payments(guild)
        if is_top_dirty():
            try:
                await self._refresh_top(guild)
                set_top_dirty(False)
            except Exception:
                pass
        pending = []
        p2 = get_donation_payment(op_id)
        if p2 and not p2.get("role_done"):
            pending.append("выдача роли")
        if p2 and not p2.get("thanks_done"):
            pending.append("благодарность")
        if is_top_dirty():
            pending.append("обновление рейтинга")
        tier = tier_for(get_donor_total(user_id))
        if pending:
            msg = (f"Записана поддержка <@{user_id}>: **{format_rub(amount)}**.\n"
                   f"Общая сумма: **{format_rub(get_donor_total(user_id))}**. Спонсорская роль: **{tier['name'] if tier else 'пока нет'}**.\n"
                   f"Запись сохранена. Ожидают повторной попытки: {', '.join(pending)}. Проверьте права бота и выполните /donation-admin check; начисление повторять не нужно.\n"
                   f"ID записи: `{op_id}`.")
        else:
            msg = (f"Записана поддержка <@{user_id}>: **{format_rub(amount)}**.\n"
                   f"Общая сумма: **{format_rub(get_donor_total(user_id))}**. Спонсорская роль: **{tier['name'] if tier else 'пока нет'}**.\n"
                   f"Благодарность опубликована в <#{CHANNEL_THANKS}>. Рейтинг обновлён в <#{CHANNEL_TOP}>.\n"
                   f"ID записи: `{op_id}`.")
        await interaction.followup.send(msg)

    async def _cmd_admin_remove(self, interaction, guild):
        opts = {o["name"]: o.get("value") for o in interaction.data.get("options", [{}])[0].get("options", [])}
        user_id = int(opts.get("user", 0))
        if not user_id:
            await interaction.followup.send("Укажите пользователя.")
            return
        member = guild.get_member(user_id)
        if member and member.bot:
            await interaction.followup.send("Выберите участника сообщества, а не бота.")
            return
        amount = parse_removal_rub(str(opts.get("amount", "")))
        reason = str(opts.get("reason", "")).strip()
        request_id = str(interaction.id)
        adj_id = f"manual-remove-{request_id}"
        existing = get_donation_adjustment(adj_id)
        if existing:
            await interaction.followup.send("Эта команда уже выполнена; повторного списания нет.")
            return
        total = get_donor_total(user_id)
        if total == 0:
            await interaction.followup.send("У этого пользователя нет учтённой суммы поддержки.")
            return
        if amount > total:
            await interaction.followup.send(f"Нельзя списать больше общей суммы: сейчас {format_rub(total)}.")
            return
        total_after = total - amount
        before_tier = tier_for(total)
        after_tier = tier_for(total_after)
        set_donor_total(user_id, total_after, True)
        save_donation_adjustment({
            "id": adj_id, "buyerId": user_id, "amount": amount, "adminId": interaction.user.id,
            "reason": reason, "recordedAt": int(time.time() * 1000),
            "totalBefore": total, "totalAfter": total_after,
            "previousRoleId": before_tier["roleId"] if before_tier else None,
            "newRoleId": after_tier["roleId"] if after_tier else None, "roleDone": False,
        })
        set_top_dirty(True)
        errors = await self._deliver_adjustments(guild)
        if is_top_dirty():
            try:
                await self._refresh_top(guild)
                set_top_dirty(False)
            except Exception:
                pass
        adj = get_donation_adjustment(adj_id)
        pending = []
        if adj and not adj.get("role_done"):
            pending.append("обновление роли")
        if is_top_dirty():
            pending.append("обновление рейтинга")
        tier = tier_for(get_donor_total(user_id))
        if pending:
            msg = (f"Из поддержки <@{user_id}> списано **{format_rub(amount)}**.\n"
                   f"До корректировки: **{format_rub(total)}**. Сейчас: **{format_rub(get_donor_total(user_id))}**.\n"
                   f"Спонсорская роль: **{tier['name'] if tier else 'нет'}**.\n"
                   f"Списание сохранено. Ожидают повторной попытки: {', '.join(pending)}. Проверьте права бота и выполните /donation-admin check; повторное списание не нужно.\n"
                   f"ID корректировки: `{adj_id}`.")
        else:
            msg = (f"Из поддержки <@{user_id}> списано **{format_rub(amount)}**.\n"
                   f"До корректировки: **{format_rub(total)}**. Сейчас: **{format_rub(get_donor_total(user_id))}**.\n"
                   f"Спонсорская роль: **{tier['name'] if tier else 'нет'}**.\n"
                   f"Спонсорская роль и рейтинг обновлены.\n"
                   f"ID корректировки: `{adj_id}`.")
        await interaction.followup.send(msg)


async def setup(bot):
    await bot.add_cog(DonationCog(bot))

"""SW-карточки: смена стороны с проверкой перевеса.

В канале SW_CHANNEL_ID лежат две карточки (на каждый сервер): выбор игрока
из списка онлайна + кнопки сторон. Лимиты для обычных: до +2, кулдаун 1 свап
за игру. VIP (seed или покупной): без лимита перевеса, без кулдауна.
Каждый свап логируется в sw_log для отчёта по перевесам.
"""

from __future__ import annotations

import asyncio
import logging
import time

import discord
from discord.ext import commands, tasks

import config
import database as db
from seeding_rcon import WardogsRconClient, RconError

log = logging.getLogger("swap")

FALLBACK_FACTIONS = ["Valkyra", "Lonestar", "Manticore"]
SW_COOLDOWN: dict[tuple[str, str], str] = {}
PICKS: dict[tuple[int, str], str] = {}
CARD_REFRESH_SECONDS = 60


def _server_index(sid: str) -> int | None:
    """Нормализует id сервера: ru1/main/1 -> 0, ru2/second/2 -> 1."""
    ids = [str(s.get("id")) for s in config.SERVERS]
    if sid in ids:
        idx = ids.index(sid)
        return idx if idx < 2 else None
    aliases = {"1": 0, "main": 0, "ru1": 0, "2": 1, "second": 1, "ru2": 1}
    return aliases.get(sid)


def _get_rcon_client(sid: str) -> WardogsRconClient | None:
    idx = _server_index(sid)
    if idx == 0 and config.SEED_RCON_URL and config.SEED_RCON_PASSWORD:
        return WardogsRconClient(config.SEED_RCON_URL, config.SEED_RCON_PASSWORD, label="sw")
    if idx == 1 and config.SEED_RCON_2_URL and config.SEED_RCON_2_PASSWORD:
        return WardogsRconClient(config.SEED_RCON_2_URL, config.SEED_RCON_2_PASSWORD, label="sw")
    return None


async def _has_move_faction(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    if config.SW_ADMIN_ROLE_IDS:
        member_role_ids = {r.id for r in member.roles}
        if member_role_ids & set(config.SW_ADMIN_ROLE_IDS):
            return True
    return False


def _is_vip(steam_id: str) -> bool:
    """VIP: seed-подписка active или активный VIP-слот в панели (покупной)."""
    # Seed VIP: seed_vip_memberships.user_id = seed_users.id (UUID), не SteamID.
    try:
        user = db.seed_get_user_by_steam(steam_id)
        if user:
            member = db.seed_get_vip_membership(user["id"])
            if member and member.get("state") == "active":
                until = member.get("active_until")
                # active_until хранится в миллисекундах
                if until is None or until > int(time.time() * 1000):
                    return True
    except Exception:
        pass
    # Покупной VIP из site.db панели
    try:
        panel_db = db.get_panel_conn()
        now = time.time()
        row = panel_db.execute(
            "SELECT 1 FROM vip_slots WHERE steam_id = ? AND (expires_utc IS NULL OR expires_utc > ?) LIMIT 1",
            (steam_id, now),
        ).fetchone()
        if row:
            return True
    except Exception:
        pass
    return False


async def _execute_swap(sid: str, side: str, target_steam_id: str, actor_discord_id: str = "") -> tuple[bool, str]:
    client = _get_rcon_client(sid)
    if not client:
        return False, "Сервер не настроен"
    try:
        players = await client.players()
        status = await client.status()
        status_map = status.get("map", "")
    except RconError as exc:
        return False, f"Сервер недоступен: {exc}"
    finally:
        await client.close()

    current = next((p for p in players if p["steam_id"] == target_steam_id), None)
    if current is None:
        return False, "Игрок не в сети"
    source = current.get("faction", "")
    if source.lower() == side.lower():
        return False, "Игрок уже в этой стороне"

    is_vip = _is_vip(target_steam_id)

    if not is_vip and status_map:
        prev = SW_COOLDOWN.get((sid, target_steam_id))
        if prev == status_map:
            return False, "Уже был свап на этой карте — 1 раз за игру (VIP — без кулдауна)"

    def count(name):
        return sum(1 for p in players if p.get("faction", "").lower() == name.lower())

    counts = {}
    for p in players:
        f = p.get("faction", "") or "?"
        counts[f] = counts.get(f, 0) + 1

    diff = abs(count(source) - count(side))
    if not is_vip and diff > 2:
        return False, f"Перевес {diff} больше допустимого (до +2; VIP — без лимита)"

    client2 = _get_rcon_client(sid)
    if not client2:
        return False, "Сервер не настроен"
    try:
        await client2.set_faction(target_steam_id, side)
    except RconError as exc:
        return False, f"Ошибка RCON: {exc}"
    finally:
        await client2.close()

    if not is_vip and status_map:
        SW_COOLDOWN[(sid, target_steam_id)] = status_map

    try:
        db.seed_log_swap(
            server_id=sid,
            steam_id=target_steam_id,
            source_faction=source,
            target_faction=side,
            diff=diff,
            player_count=len(players),
            faction_counts=counts,
            is_vip=is_vip,
            actor_discord_id=actor_discord_id,
            map_name=status_map,
        )
    except Exception:
        log.exception("Ошибка логирования свапа")

    vip_note = " [VIP — без лимита]" if is_vip else ""
    log.info("SW %s: %s %s→%s (перевес %d, игроков %d)%s", sid, target_steam_id, source, side, diff, len(players), vip_note)
    return True, f"{target_steam_id} переведён в «{side}» (перевес {diff}){vip_note}"


def _player_options(players: list | None) -> list[discord.SelectOption]:
    opts = []
    for p in sorted(players or [], key=lambda x: str(x.get("name") or "").lower())[:25]:
        steam = str(p.get("steam_id") or "")
        if not steam:
            continue
        label = str(p.get("name") or steam)[:100]
        faction = str(p.get("faction") or "?")
        opts.append(discord.SelectOption(label=label, value=steam, description=f"{steam} · {faction}"[:100]))
    if not opts:
        opts = [discord.SelectOption(label="Игроков нет — список обновится сам", value="__empty__")]
    return opts


def _server_label(sid: str) -> str:
    server = next((s for s in config.SERVERS if str(s.get("id")) == sid), None)
    return str((server or {}).get("name") or sid)


def _card_embed(sid: str, players: list | None, counts: dict | None, map_name: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"Смена стороны — {_server_label(sid)}",
        description=(
            "Выберите игрока в списке, затем нажмите кнопку нужной стороны.\n"
            "Обычные игроки: **перевес до +2**, один свап за игру. "
            "**VIP** — без лимита перевеса и без кулдауна."
        ),
        color=config.EMBED_COLOR,
    )
    if counts:
        line = "  ·  ".join(f"{f} — {counts.get(f, 0)}" for f in FALLBACK_FACTIONS)
        embed.add_field(name="Онлайн по сторонам", value=line, inline=False)
    else:
        embed.add_field(name="Онлайн по сторонам", value="данные с сервера недоступны, обновляется…", inline=False)
    embed.set_footer(text=f"Карта: {map_name or '—'} • Ответ виден только вам")
    return embed


class SWCardView(discord.ui.View):
    """Персистентная карточка одного сервера: список онлайна + кнопки сторон."""

    def __init__(self, sid: str, players: list | None = None):
        super().__init__(timeout=None)
        self.sid = sid
        select = discord.ui.Select(
            custom_id=f"sw:{sid}:player",
            placeholder="Кого перевести (онлайн)…",
            min_values=1,
            max_values=1,
            options=_player_options(players),
        )
        select.callback = self._on_player
        self.add_item(select)
        for faction in FALLBACK_FACTIONS:
            button = discord.ui.Button(
                custom_id=f"sw:{sid}:side:{faction}",
                label=f"→ {faction}",
                style=discord.ButtonStyle.secondary,
            )
            button.callback = self._side_callback(faction)
            self.add_item(button)

    async def _on_player(self, interaction: discord.Interaction):
        if not await _has_move_faction(interaction.user):
            return await interaction.response.send_message("Нет права move_faction", ephemeral=True)
        values = interaction.data.get("values") or []
        steam = values[0] if values else ""
        if not steam or steam.startswith("__"):
            return await interaction.response.send_message("Список игроков пуст", ephemeral=True)
        PICKS[(interaction.user.id, self.sid)] = steam
        await interaction.response.send_message(
            f"Выбран `{steam}` — нажмите кнопку стороны. Ответ виден только вам.",
            ephemeral=True,
        )

    def _side_callback(self, side: str):
        async def callback(interaction: discord.Interaction):
            if not await _has_move_faction(interaction.user):
                return await interaction.response.send_message("Нет права move_faction", ephemeral=True)
            steam = PICKS.get((interaction.user.id, self.sid))
            if not steam:
                return await interaction.response.send_message(
                    "Сначала выберите игрока в списке выше.", ephemeral=True
                )
            await interaction.response.defer(ephemeral=True)
            ok, result = await _execute_swap(self.sid, side, steam, str(interaction.user.id))
            if ok:
                PICKS.pop((interaction.user.id, self.sid), None)
            await interaction.followup.send(result, ephemeral=True)

        return callback


class SwapCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last_sig: dict[str, str] = {}
        self._ensured = False
        self._cards_loop.start()

    async def cog_unload(self):
        self._cards_loop.cancel()

    async def _channel(self):
        if not config.SW_CHANNEL_ID:
            return None
        guild = self.bot.get_guild(config.GUILD_ID)
        if guild is None:
            return None
        channel = guild.get_channel(config.SW_CHANNEL_ID)
        if channel is None:
            try:
                channel = await guild.fetch_channel(config.SW_CHANNEL_ID)
            except Exception:
                return None
        return channel if isinstance(channel, discord.TextChannel) else None

    async def _fetch_state(self, sid: str):
        """(players, counts, map) по живому RCON; (None, None, '') при недоступности."""
        client = _get_rcon_client(sid)
        if not client:
            return None, None, ""
        try:
            players = await client.players()
            status = await client.status()
        except RconError as exc:
            log.debug("SW state %s недоступен: %s", sid, exc)
            return None, None, ""
        finally:
            await client.close()
        counts = {f: 0 for f in FALLBACK_FACTIONS}
        for p in players:
            faction = p.get("faction") or ""
            if faction in counts:
                counts[faction] += 1
        return players, counts, str(status.get("map") or "")

    @staticmethod
    def _sig(players: list | None, counts: dict | None, map_name: str) -> str:
        ids = ",".join(sorted(str(p.get("steam_id") or "") for p in (players or [])))
        return f"{ids}|{counts}|{map_name}"

    async def _ensure_cards(self) -> bool:
        """Публикует/чинит карточки обоих серверов. Апсерт по сохранённому message_id."""
        channel = await self._channel()
        if channel is None:
            log.warning("SW-карточки: канал %s недоступен", config.SW_CHANNEL_ID)
            return False
        ok = True
        for s in config.SERVERS[:2]:
            sid = str(s.get("id"))
            players, counts, map_name = await self._fetch_state(sid)
            embed = _card_embed(sid, players, counts, map_name)
            view = SWCardView(sid, players)
            prior = db.seed_get_state(f"sw_card_{sid}", "")
            if prior:
                try:
                    msg = await channel.fetch_message(int(prior))
                    if msg.author.id == self.bot.user.id:
                        await msg.edit(embed=embed, view=view)
                        self._last_sig[sid] = self._sig(players, counts, map_name)
                        log.info("SW-карточка %s обновлена", sid)
                        continue
                except Exception:
                    pass
                db.seed_set_state(f"sw_card_{sid}", "")
            try:
                msg = await channel.send(embed=embed, view=view)
                db.seed_set_state(f"sw_card_{sid}", str(msg.id))
                self._last_sig[sid] = self._sig(players, counts, map_name)
                log.info("SW-карточка %s опубликована (message_id=%s)", sid, msg.id)
            except Exception as exc:
                log.error("SW-карточки: не удалось опубликовать %s: %s", sid, exc)
                ok = False
        return ok

    @tasks.loop(seconds=CARD_REFRESH_SECONDS)
    async def _cards_loop(self):
        if not self._ensured:
            self._ensured = await self._ensure_cards()
            if not self._ensured:
                return
        channel = await self._channel()
        if channel is None:
            return
        for s in config.SERVERS[:2]:
            sid = str(s.get("id"))
            players, counts, map_name = await self._fetch_state(sid)
            if players is None:
                continue
            sig = self._sig(players, counts, map_name)
            if sig == self._last_sig.get(sid):
                continue
            prior = db.seed_get_state(f"sw_card_{sid}", "")
            if not prior:
                self._ensured = False
                continue
            try:
                msg = await channel.fetch_message(int(prior))
                await msg.edit(embed=_card_embed(sid, players, counts, map_name), view=SWCardView(sid, players))
                self._last_sig[sid] = sig
                log.info("SW-карточка %s обновлена (онлайн %d)", sid, len(players))
            except discord.NotFound:
                db.seed_set_state(f"sw_card_{sid}", "")
                self._ensured = False
            except Exception as exc:
                log.warning("SW-карточки: не удалось обновить %s: %s", sid, exc)

    @_cards_loop.before_loop
    async def _cards_before_loop(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(5)


async def setup(bot: commands.Bot):
    for s in config.SERVERS[:2]:
        try:
            bot.add_view(SWCardView(str(s.get("id"))))
        except Exception:
            log.exception("Регистрация SW-карточки для сервера %s", s.get("id"))
    await bot.add_cog(SwapCog(bot))

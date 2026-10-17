"""Slash-команда /sw: смена стороны с проверкой перевеса.

Лимиты для обычных: до +2, кулдаун 1 свап за игру.
VIP (seed или покупной): без лимита перевеса, без кулдауна.
Каждый свап логируется в sw_log для отчёта по перевесам.
"""

from __future__ import annotations

import json
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

import config
import database as db
from seeding_rcon import WardogsRconClient, RconError

log = logging.getLogger("swap")

FALLBACK_FACTIONS = ["Valkyra", "Lonestar", "Manticore"]
SW_COOLDOWN: dict[tuple[str, str], str] = {}


def _get_rcon_client(sid: str) -> WardogsRconClient | None:
    if sid == "1" and config.SEED_RCON_URL and config.SEED_RCON_PASSWORD:
        return WardogsRconClient(config.SEED_RCON_URL, config.SEED_RCON_PASSWORD, label="sw")
    if sid == "2" and config.SEED_RCON_2_URL and config.SEED_RCON_2_PASSWORD:
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
    # Seed VIP
    try:
        member = db.seed_get_vip_membership(steam_id)
        if member and member.get("state") == "active":
            until = member.get("active_until")
            if until is None or until > time.time():
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


async def _execute_swap(sid: str, side: str, target_steam_id: str, actor_discord_id: str = ""):
    client = _get_rcon_client(sid)
    if not client:
        return "Сервер не настроен"
    try:
        players = await client.players()
        status = await client.status()
        status_map = status.get("map", "")
    except RconError as exc:
        return f"Сервер недоступен: {exc}"
    finally:
        await client.close()

    current = next((p for p in players if p["steam_id"] == target_steam_id), None)
    if current is None:
        return "Игрок не в сети"
    source = current.get("faction", "")
    if source.lower() == side.lower():
        return "Игрок уже в этой стороне"

    is_vip = _is_vip(target_steam_id)

    if not is_vip and status_map:
        prev = SW_COOLDOWN.get((sid, target_steam_id))
        if prev == status_map:
            return "Уже был свап на этой карте — 1 раз за игру (VIP — без кулдауна)"

    def count(name):
        return sum(1 for p in players if p.get("faction", "").lower() == name.lower())

    counts = {}
    for p in players:
        f = p.get("faction", "") or "?"
        counts[f] = counts.get(f, 0) + 1

    diff = abs(count(source) - count(side))
    if not is_vip and diff > 2:
        return f"Перевес {diff} больше допустимого (до +2; VIP — без лимита)"

    client2 = _get_rcon_client(sid)
    if not client2:
        return "Сервер не настроен"
    try:
        await client2.set_faction(target_steam_id, side)
    except RconError as exc:
        return f"Ошибка RCON: {exc}"
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
    return f"{target_steam_id} переведён в «{side}» (перевес {diff}){vip_note}"


class SwapCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="sw", description="Смена стороны с проверкой перевеса (до +2, VIP без лимита)")
    @app_commands.describe(server="Сервер", side="Новая сторона", player="Игрок (SteamID64)")
    @app_commands.guild_only()
    async def sw(self, interaction: discord.Interaction, server: str, side: str, player: str):
        if not await _has_move_faction(interaction.user):
            return await interaction.response.send_message(
                "Нет права move_faction", ephemeral=True
            )
        if not side or len(side) > 80:
            return await interaction.response.send_message("Укажите сторону", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        result = await _execute_swap(server, side, player, str(interaction.user.id))
        await interaction.followup.send(result, ephemeral=True)

    @sw.autocomplete("server")
    async def sw_server_autocomplete(self, interaction: discord.Interaction, current: str):
        choices = []
        for s in config.SERVERS:
            name = str(s.get("name") or s.get("id", ""))[:100]
            value = str(s["id"])
            if current.lower() in name.lower() or current in value:
                choices.append(app_commands.Choice(name=name, value=value))
        return choices[:25]

    @sw.autocomplete("side")
    async def sw_side_autocomplete(self, interaction: discord.Interaction, current: str):
        server_opt = next((o for o in interaction.data.get("options", []) if o["name"] == "server"), None)
        sid = server_opt["value"] if server_opt else ""
        client = _get_rcon_client(sid)
        if not client:
            return [
                app_commands.Choice(name=f, value=f)
                for f in FALLBACK_FACTIONS
                if current.lower() in f.lower()
            ][:25]
        try:
            scores = await client.faction_scores()
            names = [f["name"] for f in scores if f.get("name")] or FALLBACK_FACTIONS
        except Exception:
            names = FALLBACK_FACTIONS
        finally:
            await client.close()
        return [
            app_commands.Choice(name=n, value=n)
            for n in names
            if current.lower() in n.lower()
        ][:25]

    @sw.autocomplete("player")
    async def sw_player_autocomplete(self, interaction: discord.Interaction, current: str):
        server_opt = next((o for o in interaction.data.get("options", []) if o["name"] == "server"), None)
        sid = server_opt["value"] if server_opt else ""
        client = _get_rcon_client(sid)
        if not client:
            return []
        try:
            players = await client.players()
        except Exception:
            return []
        finally:
            await client.close()
        choices = []
        for p in players:
            label = f"{p['name']} · {p['steam_id']}"[:100]
            if current.lower() in label.lower():
                choices.append(app_commands.Choice(name=label, value=p["steam_id"]))
        return choices[:25]


async def setup(bot: commands.Bot):
    await bot.add_cog(SwapCog(bot))

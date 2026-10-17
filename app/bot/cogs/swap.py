"""Slash-команда /sw: смена стороны с проверкой перевеса (до +2, с VIP до +4)."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

import config
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
    """Право move_faction: administrator в Discord или роль из SW_ADMIN_ROLE_IDS."""
    if member.guild_permissions.administrator:
        return True
    if config.SW_ADMIN_ROLE_IDS:
        member_role_ids = {r.id for r in member.roles}
        if member_role_ids & set(config.SW_ADMIN_ROLE_IDS):
            return True
    return False


def _swap_allowed(diff: int, has_vip: bool) -> bool:
    return diff <= (4 if has_vip else 2)


async def _execute_swap(sid: str, side: str, target_steam_id: str):
    """Ядро SW: проверка перевеса → PATCH faction."""
    client = _get_rcon_client(sid)
    if not client:
        return "Сервер не настроен"
    try:
        players = await client.players()
        status_map = (await client.status()).get("map", "")
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

    if status_map:
        prev = SW_COOLDOWN.get((sid, target_steam_id))
        if prev == status_map:
            return "Уже был свап на этой карте — 1 раз за игру"

    def count(name):
        return sum(1 for p in players if p.get("faction", "").lower() == name.lower())

    diff = abs(count(source) - count(side))
    if not _swap_allowed(diff, False):
        return f"Перевес {diff} больше допустимого (до +2)"

    client2 = _get_rcon_client(sid)
    if not client2:
        return "Сервер не настроен"
    try:
        await client2.set_faction(target_steam_id, side)
    except RconError as exc:
        return f"Ошибка RCON: {exc}"
    finally:
        await client2.close()

    if status_map:
        SW_COOLDOWN[(sid, target_steam_id)] = status_map
    return f"{target_steam_id} переведён в «{side}» (перевес был {diff})"


class SwapCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="sw", description="Смена стороны с проверкой перевеса (до +2, с VIP до +4)")
    @app_commands.describe(server="Сервер", side="Новая сторона", player="Игрок (SteamID64)")
    @app_commands.guild_only()
    async def sw(self, interaction: discord.Interaction, server: str, side: str, player: str):
        if not await _has_move_faction(interaction.user):
            return await interaction.response.send_message(
                "Нет права move_faction (проверьте роли в панели)", ephemeral=True
            )
        if not side or len(side) > 80:
            return await interaction.response.send_message("Укажите сторону", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        result = await _execute_swap(server, side, player)
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

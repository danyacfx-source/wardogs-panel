import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
import discord
from discord import app_commands
from discord.ext import commands

import config
from database import (
    get_trap_channels,
    remove_trap_channel,
    set_trap_channel,
)

log = logging.getLogger("trap")

TRAP_NOTICE = (
    "**НЕ ОТПРАВЛЯЙТЕ СООБЩЕНИЯ В ЭТОТ КАНАЛ**\n"
    "Этот канал используется для выявления спам-ботов. "
    "Любые отправленные сюда сообщения приведут к **кику**."
)


class TrapChannelCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _is_trap(self, channel_id: int) -> bool:
        return channel_id in config.TRAP_CHANNEL_IDS or channel_id in get_trap_channels()

    def _trap_ids(self) -> list[int]:
        return list(dict.fromkeys([*config.TRAP_CHANNEL_IDS, *get_trap_channels()]))

    def _hardcoded(self, channel_id: int) -> bool:
        return channel_id in config.TRAP_CHANNEL_IDS

    @commands.Cog.listener()
    async def on_ready(self):
        # Раскладываем предупреждение по каналам-ловушкам при каждом старте:
        # если последнее сообщение в канале — не наше объявление, постим его.
        for cid in self._trap_ids():
            ch = self.bot.get_channel(cid)
            if ch is None:
                try:
                    ch = await self.bot.fetch_channel(cid)
                except Exception:
                    continue
            try:
                async for msg in ch.history(limit=1):
                    if msg.author.id == self.bot.user.id and msg.content == TRAP_NOTICE:
                        break
                else:
                    await ch.send(TRAP_NOTICE)
                    log.info("Предупреждение отправлено в канал-ловушку %s", cid)
            except discord.Forbidden:
                log.warning("Нет прав писать в канал-ловушку %s", cid)
            except Exception as e:
                log.warning("Не смог отправить предупреждение в ловушку %s: %s", cid, e)

    @app_commands.command(name="trap", description="Сделать канал ловушкой: сообщение в нём = кик")
    @app_commands.describe(channel="Канал (по умолчанию текущий)")
    @app_commands.guild_only()
    async def trap_enable(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
        target = channel or interaction.channel
        set_trap_channel(target.id, interaction.guild_id)
        await target.send(TRAP_NOTICE)
        await interaction.response.send_message(
            f"⚠️ `#{target.name}` теперь канал-ловушка: любое сообщение там будет удалено, автор — кикнут.",
            ephemeral=True,
        )

    @app_commands.command(name="untrap", description="Убрать канал из ловушек")
    @app_commands.describe(channel="Канал (по умолчанию текущий)")
    @app_commands.guild_only()
    async def trap_disable(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
        target = channel or interaction.channel
        if self._hardcoded(target.id):
            return await interaction.response.send_message(
                "❌ Этот канал зашит в конфиг бота, командой убрать нельзя.", ephemeral=True
            )
        if remove_trap_channel(target.id):
            await interaction.response.send_message(
                f"✅ `#{target.name}` больше не канал-ловушка.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"ℹ️ `#{target.name}` и так не был каналом-ловушкой.", ephemeral=True
            )

    @app_commands.command(name="traplist", description="Список каналов-ловушек")
    @app_commands.guild_only()
    async def trap_list(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
        ids = list(dict.fromkeys([*config.TRAP_CHANNEL_IDS, *get_trap_channels()]))
        if not ids:
            return await interaction.response.send_message(
                "📭 Каналов-ловушек нет.", ephemeral=True
            )
        lines = []
        for cid in ids:
            ch = interaction.guild.get_channel(cid)
            hard = " (хардкод)" if self._hardcoded(cid) else ""
            lines.append(f"- {ch.mention if ch else f'`{cid}`'}{hard}")
        await interaction.response.send_message(
            "⚠️ **Каналы-ловушки** (сообщение = кик):\n" + "\n".join(lines),
            ephemeral=True,
        )

    async def _log(self, message: discord.Message, member: discord.Member, kicked: bool):
        ch_id = config.TRAP_LOG_CHANNEL
        if not ch_id:
            return
        ch = self.bot.get_channel(ch_id)
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(ch_id)
            except Exception:
                return
        embed = discord.Embed(
            title="🚨 Сообщение в канале-ловушке",
            description=f"**{member}** ({member.id}) написал в {message.channel.mention}",
            color=discord.Color.dark_red() if kicked else discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Действие",
            value="🦶 Кикнут" if kicked else "⚠️ Кик не удался (нет прав у бота)",
            inline=False,
        )
        embed.add_field(
            name="Содержимое",
            value=(message.content or "*вложение*")[:1000],
            inline=False,
        )
        try:
            await ch.send(embed=embed)
        except Exception as e:
            log.warning("Не смог отправить лог ловушки: %s", e)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        if not self._is_trap(message.channel.id):
            return
        member = message.author
        if not isinstance(member, discord.Member):
            return

        try:
            await message.delete()
        except discord.NotFound:
            pass
        except Exception:
            pass

        try:
            await member.kick(reason="Написал(а) в канале-ловушке")
            await self._log(message, member, kicked=True)
        except discord.Forbidden:
            log.warning("Нет прав кикнуть %s (канал-ловушка %s)", member, message.channel)
            await self._log(message, member, kicked=False)
        except Exception as e:
            log.error("Ошибка кика %s из ловушки: %s", member, e)
            await self._log(message, member, kicked=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(TrapChannelCog(bot))
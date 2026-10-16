import asyncio
import json
import os
import discord
from discord import app_commands
from discord.ext import commands
import aiohttp

import config
from twitch_api import fetch_status

intents = discord.Intents.default()
intents.guilds = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

async def load_cogs():
    for cog in ["cogs.translate"]:
        try:
            await bot.load_extension(cog)
            print(f"[cog] загружен {cog}")
        except Exception as e:
            print(f"[cog] ошибка {cog}: {e}")

live_state: dict[str, bool] = {}
last_title: dict[str, str] = {}
live_messages: dict[str, tuple[int, int]] = {}

# --- persisted Twitch channels (дополняют .env) ---
CHANNELS_FILE = os.path.join(os.path.dirname(__file__), "twitch_channels.json")

def _load_persisted_channels() -> list[str]:
    try:
        if os.path.isfile(CHANNELS_FILE):
            with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return [str(s).strip().lower().lstrip("@") for s in data if str(s).strip()]
    except Exception:
        pass
    return []

def _save_persisted_channels(channels: list[str]):
    try:
        with open(CHANNELS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(set(channels)), f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[twitch] save channels failed: {e}")

# грузим при старте и мерджим с .env
_persisted = _load_persisted_channels()
if _persisted:
    # мерджим без дублей, порядок: .env + файл
    merged = []
    seen = set()
    for c in config.TWITCH_CHANNELS + _persisted:
        c = c.strip().lower().lstrip("@")
        if c and c not in seen:
            seen.add(c)
            merged.append(c)
    config.TWITCH_CHANNELS[:] = merged

def _thumb(url: str | None, w=640, h=360) -> str | None:
    if not url:
        return None
    return url.replace("{width}", str(w)).replace("{height}", str(h))

def build_live_embed(channel: str, stream: dict) -> discord.Embed:
    url = f"https://twitch.tv/{channel}"
    title = (stream.get("title") or "").strip() or "Стрим начался!"
    game = (stream.get("game_name") or "—").strip()
    viewers = stream.get("viewer_count", 0)
    thumb = _thumb(stream.get("thumbnail_url"))
    avatar = stream.get("profile_image_url")
    name = stream.get("user_name") or channel
    e = discord.Embed(title=title[:256], url=url, color=0x6441A5, description=f"**{name}** в эфире")
    if avatar:
        e.set_author(name=name, icon_url=avatar, url=url)
    else:
        e.set_author(name=name, url=url)
    e.add_field(name="Игра", value=game[:100], inline=True)
    e.add_field(name="Зрителей", value=f"{viewers:,}".replace(",", " "), inline=True)
    e.add_field(name="Канал", value=f"[{channel}]({url})", inline=True)
    if thumb:
        e.set_image(url=thumb)
    if avatar:
        e.set_thumbnail(url=avatar)
    e.set_footer(text="Twitch • live")
    e.timestamp = discord.utils.utcnow()
    return e

def build_ended_embed(channel: str, last_stream: dict | None) -> discord.Embed:
    url = f"https://twitch.tv/{channel}"
    name = (last_stream or {}).get("user_name") or channel
    avatar = (last_stream or {}).get("profile_image_url")
    game = (last_stream or {}).get("game_name") or "—"
    e = discord.Embed(title="Стрим завершён", description=f"**{name}** закончил трансляцию", color=0x95A5A6, url=url)
    if avatar:
        e.set_author(name=name, icon_url=avatar, url=url)
    e.add_field(name="Игра", value=game[:100], inline=True)
    e.add_field(name="Канал", value=f"[{channel}]({url})", inline=True)
    if avatar:
        e.set_thumbnail(url=avatar)
    e.set_footer(text="Twitch • offline")
    e.timestamp = discord.utils.utcnow()
    return e

async def check_once():
    ch = bot.get_channel(config.ANNOUNCE_CHANNEL_ID)
    if ch is None:
        try:
            ch = await bot.fetch_channel(config.ANNOUNCE_CHANNEL_ID)
        except Exception as e:
            print(f"[twitch] канал анонсов не найден: {e}")
            return
    if not isinstance(ch, discord.TextChannel):
        return
    async with aiohttp.ClientSession() as session:
        for tw in list(config.TWITCH_CHANNELS):
            try:
                stream = await fetch_status(session, tw)
            except Exception as e:
                print(f"[twitch] {tw}: {e}")
                continue
            was_live = live_state.get(tw, False)
            is_live = bool(stream and stream.get("id"))
            if stream and stream.get("is_gql") and not stream.get("id"):
                is_live = False
            elif stream:
                is_live = True
            else:
                is_live = False

            if is_live and not was_live:
                live_state[tw] = True
                last_title[tw] = (stream or {}).get("title", "")
                ping = f"<@&{config.PING_ROLE_ID}> " if config.PING_ROLE_ID else ""
                embed = build_live_embed(tw, stream)
                try:
                    sent = await ch.send(content=f"{ping}**{tw}** в эфире! https://twitch.tv/{tw}", embed=embed)
                    live_messages[tw] = (ch.id, sent.id)
                    print(f"[twitch] {tw} LIVE -> {sent.id}")
                except Exception as e:
                    print(f"[twitch] send failed: {e}")
            elif is_live and was_live:
                if stream.get("title") != last_title.get(tw):
                    last_title[tw] = stream.get("title", "")
                    if tw in live_messages:
                        cid, mid = live_messages[tw]
                        try:
                            msg = await ch.fetch_message(mid)
                            await msg.edit(embed=build_live_embed(tw, stream))
                        except Exception:
                            pass
            elif not is_live and was_live:
                live_state[tw] = False
                if tw in live_messages:
                    cid, mid = live_messages.pop(tw)
                    try:
                        msg = await ch.fetch_message(mid)
                        last_stream = {"user_name": tw, "game_name": last_title.get(tw, "—"), "profile_image_url": stream.get("profile_image_url") if stream else None}
                        await msg.edit(content=f"**{tw}** — стрим завершён", embed=build_ended_embed(tw, last_stream))
                        print(f"[twitch] {tw} ENDED -> edited {mid}")
                    except Exception as e:
                        print(f"[twitch] edit ended failed: {e}")
                        try:
                            await ch.send(embed=build_ended_embed(tw, None))
                        except Exception:
                            pass
                else:
                    print(f"[twitch] {tw} OFFLINE")
                last_title.pop(tw, None)

# --- /twitch команды ---
def _can_manage(member: discord.Member) -> bool:
    return bool(member.guild_permissions.administrator or member.guild_permissions.manage_guild)

twitch_group = app_commands.Group(name="twitch", description="Twitch анонсы")

@twitch_group.command(name="status", description="Проверить live статус канала")
@app_commands.describe(channel="Ник Twitch (пусто = все из списка)")
async def twitch_status(interaction: discord.Interaction, channel: str | None = None):
    await interaction.response.defer(ephemeral=True)
    targets = [channel.strip().lower().lstrip("@")] if channel else list(config.TWITCH_CHANNELS)
    if not targets:
        await interaction.followup.send("Список каналов пуст. Добавь через `/twitch add`.", ephemeral=True)
        return
    lines = []
    async with aiohttp.ClientSession() as session:
        for tw in targets[:10]:
            try:
                s = await fetch_status(session, tw)
                is_live = bool(s and s.get("id"))
                if is_live:
                    lines.append(f"🔴 **{tw}** — live | {s.get('game_name','—')} | {s.get('viewer_count',0)} зрителей | {s.get('title','')[:80]}")
                else:
                    lines.append(f"⚪ **{tw}** — offline")
            except Exception as e:
                lines.append(f"❓ **{tw}** — ошибка: {e}")
    await interaction.followup.send("\n".join(lines)[:1900] or "Нет данных", ephemeral=True)

@twitch_group.command(name="add", description="Добавить Twitch канал в парсер")
@app_commands.describe(channel="Ник Twitch (без @)")
async def twitch_add(interaction: discord.Interaction, channel: str):
    if not _can_manage(interaction.user):
        await interaction.response.send_message("❌ Нужны права администратора.", ephemeral=True)
        return
    ch = channel.strip().lower().lstrip("@")
    if not ch or not ch.replace("_", "").replace("-", "").isalnum():
        await interaction.response.send_message("❌ Укажи корректный ник.", ephemeral=True)
        return
    if ch in config.TWITCH_CHANNELS:
        await interaction.response.send_message(f"ℹ️ `{ch}` уже в списке.", ephemeral=True)
        return
    config.TWITCH_CHANNELS.append(ch)
    _save_persisted_channels(config.TWITCH_CHANNELS)
    await interaction.response.send_message(f"✅ Добавил `{ch}`. Текущий список: {', '.join(config.TWITCH_CHANNELS)}", ephemeral=True)

@twitch_group.command(name="remove", description="Удалить Twitch канал из парсера")
@app_commands.describe(channel="Ник Twitch")
async def twitch_remove(interaction: discord.Interaction, channel: str):
    if not _can_manage(interaction.user):
        await interaction.response.send_message("❌ Нужны права администратора.", ephemeral=True)
        return
    ch = channel.strip().lower().lstrip("@")
    if ch not in config.TWITCH_CHANNELS:
        await interaction.response.send_message(f"ℹ️ `{ch}` нет в списке.", ephemeral=True)
        return
    config.TWITCH_CHANNELS.remove(ch)
    live_state.pop(ch, None)
    _save_persisted_channels(config.TWITCH_CHANNELS)
    await interaction.response.send_message(f"✅ Удалил `{ch}`. Осталось: {', '.join(config.TWITCH_CHANNELS) or '—'}", ephemeral=True)

@twitch_group.command(name="list", description="Список отслеживаемых Twitch каналов")
async def twitch_list(interaction: discord.Interaction):
    txt = ", ".join(f"`{c}`" for c in config.TWITCH_CHANNELS) or "— пусто —"
    await interaction.response.send_message(f"Отслеживаются: {txt}\nКанал анонсов: <#{config.ANNOUNCE_CHANNEL_ID}>", ephemeral=True)

@twitch_group.command(name="test", description="Тестовый анонс (отправляет embed в канал анонсов)")
async def twitch_test(interaction: discord.Interaction):
    if not _can_manage(interaction.user):
        await interaction.response.send_message("❌ Нужны права администратора.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    ch = bot.get_channel(config.ANNOUNCE_CHANNEL_ID)
    if ch is None:
        try:
            ch = await bot.fetch_channel(config.ANNOUNCE_CHANNEL_ID)
        except Exception as e:
            await interaction.followup.send(f"❌ Канал не найден: {e}", ephemeral=True)
            return
    fake = {"user_name": "test_channel", "user_login": "test", "title": "Тестовый стрим — проверка эмбеда", "game_name": "Just Chatting", "viewer_count": 123, "thumbnail_url": None, "profile_image_url": None}
    embed = build_live_embed("test_channel", fake)
    try:
        await ch.send(content="🧪 Тестовый анонс", embed=embed)
        await interaction.followup.send("✅ Отправил тестовый анонс.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка: {e}", ephemeral=True)

@bot.event
async def on_ready():
    print(f"[ready] {bot.user} — парсер Twitch запущен: {', '.join(config.TWITCH_CHANNELS) or '—'}")
    try:
        await load_cogs()
    except Exception:
        pass
    try:
        bot.tree.add_command(twitch_group)
        await bot.tree.sync()
        print("[ready] /twitch + Перевести синхронизированы")
    except Exception as e:
        print(f"[ready] sync failed: {e}")
    bot.loop.create_task(poll_loop())

async def poll_loop():
    await bot.wait_until_ready()
    await check_once()
    while True:
        await asyncio.sleep(config.TWITCH_POLL_SECONDS)
        try:
            await check_once()
        except Exception as e:
            print(f"[twitch] poll error: {e}")

if __name__ == "__main__":
    if not config.TOKEN:
        print("DISCORD_TOKEN не задан в .env")
        raise SystemExit(1)
    if not config.TWITCH_CHANNELS:
        print("TWITCH_CHANNELS пуст — добавь через .env или /twitch add")
    bot.run(config.TOKEN)

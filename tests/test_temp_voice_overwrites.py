"""Временные голосовые комнаты: наследуют права категории, владелец получает свои права, при передаче права переходят."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from app.bot import config
from app.bot.cogs import temp_voice


def _member(uid, name="Тест"):
    member = MagicMock()
    member.id = uid
    member.bot = False
    member.display_name = name
    return member


def test_temp_channel_gets_category_overwrites_and_owner_rights(monkeypatch):
    monkeypatch.setattr(config, "VC_CATEGORY", 555)
    category = MagicMock()
    category.overwrites = {"everyone": "deny-connect"}
    vc = MagicMock()
    vc.id = 4242
    vc.delete = AsyncMock()
    guild = MagicMock()
    guild.get_channel.return_value = category
    guild.create_voice_channel = AsyncMock(return_value=vc)
    owner = _member(10)
    owner.guild = guild
    trigger = MagicMock()
    trigger.members = [owner]
    owner.move_to = AsyncMock()

    asyncio.run(temp_voice.TempVoiceCog._create_temp_channel(None, owner, trigger))

    kwargs = guild.create_voice_channel.call_args.kwargs
    assert kwargs["category"] is category
    assert kwargs["overwrites"]["everyone"] == "deny-connect"
    owner_ow = kwargs["overwrites"][owner]
    assert owner_ow.manage_channels is True
    assert owner_ow.connect is True
    assert owner_ow.move_members is True


def test_transfer_moves_owner_rights_to_new_member(monkeypatch):
    monkeypatch.setattr(config, "VC_CATEGORY", 555)
    old_owner = _member(10, "Старый")
    new_owner = _member(20, "Новый")
    vc = MagicMock(spec=discord.VoiceChannel)
    vc.id = 77
    vc.members = [new_owner]
    vc.set_permissions = AsyncMock()
    guild = MagicMock()
    guild.get_channel.return_value = vc
    guild.get_member.side_effect = lambda uid: {10: old_owner, 20: new_owner}.get(uid)
    bot = SimpleNamespace(guilds=[guild])
    cog = SimpleNamespace(bot=bot)

    temp_voice.temp_channel_owners[77] = 10
    try:
        result = asyncio.run(temp_voice.TempVoiceCog.panel_transfer_channel(cog, 77, 20))
    finally:
        temp_voice.temp_channel_owners.pop(77, None)

    assert result["ok"] is True
    calls = vc.set_permissions.await_args_list
    # Старому владельцу права сняты (overwrite=None), новому выданы.
    revoke = [c for c in calls if c.args[0] is old_owner]
    grant = [c for c in calls if c.args[0] is new_owner]
    assert revoke and revoke[0].kwargs["overwrite"] is None
    assert grant and grant[0].kwargs["overwrite"].manage_channels is True

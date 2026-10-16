import test from 'node:test';
import assert from 'node:assert/strict';
import { PermissionFlagsBits } from 'discord.js';
import { validateConfiguredChannels } from '../src/discord/channel-preflight.js';

const ids = {
  voiceChannelId: '1552253117586739210',
  rankChannelId: '1552254053109727262',
  shopChannelId: '1552255043757936721',
  rulesChannelId: '1552254112060411944',
  backupChannelId: '1552401803184701502',
  auditChannelId: '1552402077068820611',
  seederRoleId: '1552728825568174090',
  superSeederRoleId: '1552841261713719440',
  vipRoleId: '1552725871830503484'
};

function fixture({ publicBackup = false, highManagedRole = false } = {}) {
  const botMember = {
    id: 'bot-member',
    permissions: { has: () => true },
    roles: { highest: { position: 100 } }
  };
  const everyone = { id: 'everyone' };
  const text = (id, isPrivate = false, everyoneCanView = false) => ({
    id,
    isTextBased: () => true,
    isSendable: () => true,
    isVoiceBased: () => false,
    permissionsFor(target) {
      if (target === everyone) {
        return { has: (permission) => permission === PermissionFlagsBits.ViewChannel && (!isPrivate || everyoneCanView) };
      }
      return { has: () => true };
    }
  });
  const channels = new Map([
    [ids.voiceChannelId, {
      id: ids.voiceChannelId,
      isVoiceBased: () => true,
      isTextBased: () => false,
      permissionsFor: () => ({ has: () => true })
    }],
    [ids.rankChannelId, text(ids.rankChannelId)],
    [ids.shopChannelId, text(ids.shopChannelId)],
    [ids.rulesChannelId, text(ids.rulesChannelId)],
    [ids.backupChannelId, text(ids.backupChannelId, true, publicBackup)],
    [ids.auditChannelId, text(ids.auditChannelId, true)]
  ]);
  const roles = new Map([
    [ids.seederRoleId, { id: ids.seederRoleId, managed: false, position: 10 }],
    [ids.superSeederRoleId, { id: ids.superSeederRoleId, managed: false, position: 11 }],
    [ids.vipRoleId, { id: ids.vipRoleId, managed: false, position: highManagedRole ? 100 : 12 }]
  ]);
  const guild = {
    members: { me: botMember },
    roles: { everyone, cache: roles, fetch: async (id) => roles.get(id) },
    channels: { cache: channels, fetch: async (id) => channels.get(id) }
  };
  return {
    discord: { guilds: { cache: new Map([['111111111111111111', guild]]) } },
    config: { discord: { guildId: '111111111111111111', ...ids } }
  };
}

test('channel preflight accepts configured public and private channels', async () => {
  await validateConfiguredChannels(fixture());
});

test('channel preflight refuses a backup channel visible to everyone', async () => {
  await assert.rejects(
    validateConfiguredChannels(fixture({ publicBackup: true })),
    /backup channel must be hidden/
  );
});

test('channel preflight refuses a managed reward role above the bot role', async () => {
  await assert.rejects(
    validateConfiguredChannels(fixture({ highManagedRole: true })),
    /Bot role must be above the configured VIP role/
  );
});

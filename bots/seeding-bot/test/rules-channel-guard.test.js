import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { installRulesChannelGuard } from '../src/discord/rules-channel-guard.js';

test('rules channel guard deletes ordinary messages but keeps the bot panels', async () => {
  const discord = new EventEmitter();
  discord.user = { id: '999999999999999999' };
  const config = {
    discord: {
      guildId: '403186008579112973',
      rulesChannelId: '1552254112060411944'
    }
  };
  installRulesChannelGuard({
    discord,
    config,
    logger: { info() {}, warn() {} }
  });

  let deleted = 0;
  const ordinary = {
    guildId: config.discord.guildId,
    channelId: config.discord.rulesChannelId,
    author: { id: '111111111111111111' },
    attachments: { size: 0 },
    delete: async () => { deleted += 1; }
  };
  discord.emit('messageCreate', ordinary);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(deleted, 1);

  discord.emit('messageCreate', { ...ordinary, author: { id: discord.user.id } });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(deleted, 1);
});

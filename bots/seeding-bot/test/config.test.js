import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { randomBytes } from 'node:crypto';
import { readConfig } from '../src/config.js';

test('sensitive values can be loaded from separate secret files', async (t) => {
  const directory = await mkdtemp(join(tmpdir(), 'wardogs-config-test-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const values = {
    token: 'discord-token-for-test-only',
    rcon: 'rcon-password-for-test-only',
    data: randomBytes(32).toString('base64'),
    backup: randomBytes(32).toString('base64')
  };
  for (const [name, value] of Object.entries(values)) await writeFile(join(directory, name), `${value}\n`);
  const config = readConfig({
    DISCORD_TOKEN_FILE: join(directory, 'token'),
    DISCORD_CLIENT_ID: '111111111111111111',
    DISCORD_GUILD_ID: '222222222222222222',
    DISCORD_ADMIN_ROLE_IDS: '333333333333333333',
    WARDOGS_RCON_URL: 'https://rcon.example.test:7799',
    WARDOGS_RCON_2_URL: 'https://rcon-2.example.test:7789',
    WARDOGS_RCON_PASSWORD_FILE: join(directory, 'rcon'),
    WARDOGS_RCON_2_PASSWORD_FILE: join(directory, 'rcon'),
    DATA_ENCRYPTION_KEY_FILE: join(directory, 'data'),
    BACKUP_ENCRYPTION_KEY_FILE: join(directory, 'backup'),
    DATA_DIR: directory
  });
  assert.equal(config.discord.token, values.token);
  assert.equal(config.rcon.password, values.rcon);
  assert.equal(config.rconSecondary.password, values.rcon);
  assert.equal(config.rcon.allowInsecureHttp, false);
  assert.equal(config.security.masterKey.length, 32);
  assert.equal(config.discord.rulesChannelId, '1552254112060411944');
  assert.equal(config.discord.seederRoleId, '1552728825568174090');
  assert.equal(config.discord.superSeederRoleId, '1552841261713719440');
  assert.equal(config.discord.vipRoleId, '1552725871830503484');
  assert.equal(config.discord.auditChannelId, '1552402077068820611');
  assert.equal(config.discord.backupChannelId, '1552401803184701502');
  assert.equal(config.seed.leaderboardRefreshSeconds, 60);
});

test('BotHost DISCORD_BOT_TOKEN is accepted without a duplicate token variable', () => {
  const values = {
    DISCORD_BOT_TOKEN: 'bothost-discord-token-for-test-only',
    DISCORD_CLIENT_ID: '111111111111111111',
    DISCORD_GUILD_ID: '222222222222222222',
    DISCORD_ADMIN_ROLE_IDS: '333333333333333333',
    WARDOGS_RCON_URL: 'https://rcon.example.test:7799',
    WARDOGS_RCON_2_URL: 'https://rcon-2.example.test:7789',
    WARDOGS_RCON_PASSWORD: 'rcon-password-for-test-only',
    WARDOGS_RCON_2_PASSWORD: 'rcon-second-password-for-test-only',
    DATA_ENCRYPTION_KEY: randomBytes(32).toString('base64'),
    BACKUP_ENCRYPTION_KEY: randomBytes(32).toString('base64')
  };
  const config = readConfig(values);
  assert.equal(config.discord.token, values.DISCORD_BOT_TOKEN);
  assert.throws(
    () => readConfig({ ...values, DISCORD_TOKEN: 'different-token' }),
    /different values/
  );
});

test('plain HTTP is refused unless explicitly acknowledged', () => {
  const environment = {
    DISCORD_TOKEN: 'discord-token-for-test-only',
    DISCORD_CLIENT_ID: '111111111111111111',
    DISCORD_GUILD_ID: '222222222222222222',
    DISCORD_ADMIN_ROLE_IDS: '333333333333333333',
    WARDOGS_RCON_URL: 'http://203.0.113.10:7799',
    WARDOGS_RCON_PASSWORD: 'rcon-password-for-test-only',
    WARDOGS_RCON_2_PASSWORD: 'rcon-second-password-for-test-only',
    DATA_ENCRYPTION_KEY: randomBytes(32).toString('base64'),
    BACKUP_ENCRYPTION_KEY: randomBytes(32).toString('base64')
  };
  assert.throws(() => readConfig(environment), /Plain HTTP RCON is disabled/);
  assert.equal(readConfig({ ...environment, RCON_ALLOW_INSECURE_HTTP: 'true' }).rcon.allowInsecureHttp, true);
});

test('WARDOGS deployment IDs stay pinned while the RCON endpoint is explicit', () => {
  const environment = {
    DISCORD_BOT_TOKEN: 'bothost-discord-token-for-test-only',
    DISCORD_CLIENT_ID: '111111111111111111',
    WARDOGS_RCON_URL: 'http://51.83.166.12:7799',
    WARDOGS_RCON_PASSWORD: 'rcon-password-for-test-only',
    WARDOGS_RCON_2_PASSWORD: 'rcon-second-password-for-test-only',
    DATA_ENCRYPTION_KEY: randomBytes(32).toString('base64'),
    BACKUP_ENCRYPTION_KEY: randomBytes(32).toString('base64'),
    RCON_ALLOW_INSECURE_HTTP: 'true'
  };
  const config = readConfig(environment);
  assert.equal(config.discord.guildId, '403186008579112973');
  assert.deepEqual(config.discord.adminRoleIds, [
    '805429609457254450',
    '815605974412558376'
  ]);
  assert.deepEqual(config.discord.gatherRoleIds, ['859738052666327040']);
  assert.equal(config.discord.adminRoleIds.includes('859738052666327040'), false);
  assert.equal(config.rcon.url.href, 'http://51.83.166.12:7799/');
  assert.equal(config.rconSecondary.url.href, 'http://216.144.249.92:7789/');
  assert.equal(config.rcon.retryAttempts, 1);
  assert.equal(config.rcon.retryBaseMs, 300);
  assert.equal(config.rcon.outageLogIntervalSeconds, 900);
  assert.throws(() => readConfig({ ...environment, WARDOGS_RCON_2_PASSWORD: '' }),
    /Missing required secret: WARDOGS_RCON_2_PASSWORD/);
  assert.throws(() => readConfig({ ...environment,
    WARDOGS_RCON_2_URL: environment.WARDOGS_RCON_URL }), /must point to different game servers/);
});

test('the direct RCON endpoint is required and the public HTML console is rejected', () => {
  const environment = {
    DISCORD_BOT_TOKEN: 'bothost-discord-token-for-test-only',
    DISCORD_CLIENT_ID: '111111111111111111',
    WARDOGS_RCON_PASSWORD: 'rcon-password-for-test-only',
    WARDOGS_RCON_2_PASSWORD: 'rcon-second-password-for-test-only',
    DATA_ENCRYPTION_KEY: randomBytes(32).toString('base64'),
    BACKUP_ENCRYPTION_KEY: randomBytes(32).toString('base64')
  };
  assert.throws(() => readConfig(environment), /Missing required setting: WARDOGS_RCON_URL/);
  assert.throws(
    () => readConfig({
      ...environment,
      WARDOGS_RCON_URL: 'http://rcon.wardogs.com',
      RCON_ALLOW_INSECURE_HTTP: 'true'
    }),
    /public HTML control panel/
  );
});

test('managed Discord role IDs must be distinct', () => {
  const environment = {
    DISCORD_BOT_TOKEN: 'bothost-discord-token-for-test-only',
    DISCORD_CLIENT_ID: '111111111111111111',
    WARDOGS_RCON_URL: 'https://rcon.example.test:7799',
    WARDOGS_RCON_2_URL: 'https://rcon-2.example.test:7789',
    WARDOGS_RCON_PASSWORD: 'rcon-password-for-test-only',
    WARDOGS_RCON_2_PASSWORD: 'rcon-second-password-for-test-only',
    DATA_ENCRYPTION_KEY: randomBytes(32).toString('base64'),
    BACKUP_ENCRYPTION_KEY: randomBytes(32).toString('base64'),
    SEEDER_ROLE_ID: '1552728825568174090',
    SUPER_SEEDER_ROLE_ID: '1552728825568174090'
  };
  assert.throws(() => readConfig(environment), /must have unique IDs/);
});

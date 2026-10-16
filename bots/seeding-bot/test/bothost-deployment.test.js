import test from 'node:test';
import assert from 'node:assert/strict';
import { access, mkdtemp, readFile, readdir, rm, stat } from 'node:fs/promises';
import { constants } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { join, resolve } from 'node:path';

const root = resolve(fileURLToPath(new URL('..', import.meta.url)));

test('BotHost deployment metadata and persistent paths are pinned', async () => {
  const metadata = JSON.parse(await readFile(resolve(root, 'bothost.json'), 'utf8'));
  assert.deepEqual(metadata, { main: 'index.js', language: 'node' });

  const packageConfig = JSON.parse(await readFile(resolve(root, 'package.json'), 'utf8'));
  assert.equal(packageConfig.version, '1.8.1');
  assert.equal(packageConfig.dependencies['better-sqlite3'], '12.6.2');
  assert.match(packageConfig.engines.node, />=20\.18\.0/);

  const dockerfile = await readFile(resolve(root, 'Dockerfile'), 'utf8');
  assert.match(dockerfile, /^FROM node:20\.20\.2-bookworm-slim/m);
  assert.match(dockerfile, /COPY package\.json package-lock\.json \.npmrc \.\//);
  assert.match(dockerfile, /WORKDIR \/opt\/wardogs-seeding-bot/);
  assert.match(dockerfile, /npm ci --omit=dev/);
  assert.doesNotMatch(dockerfile, /npm ci[^\n]*--ignore-scripts/);
  assert.match(dockerfile, /ENTRYPOINT \["\/usr\/local\/bin\/wardogs-entrypoint"\]/);
  assert.doesNotMatch(dockerfile, /COPY\s+\.\s+\./);

  const entrypoint = await readFile(resolve(root, 'docker-entrypoint.sh'), 'utf8');
  assert.match(entrypoint, /DATA_DIR:-\/app\/data/);
  assert.match(entrypoint, /exec gosu node:node/);
  assert.equal((await stat(resolve(root, 'docker-entrypoint.sh'))).mode & 0o111, 0o111);
});

test('BotHost environment template contains the private audit and backup channels', async () => {
  const environment = await readFile(resolve(root, '.env.bothost.example'), 'utf8');
  assert.match(environment, /^DISCORD_GUILD_ID=403186008579112973$/m);
  assert.match(environment, /^DISCORD_ADMIN_ROLE_IDS=805429609457254450,815605974412558376$/m);
  assert.match(environment, /^SEEDER_ROLE_ID=1552728825568174090$/m);
  assert.match(environment, /^SUPER_SEEDER_ROLE_ID=1552841261713719440$/m);
  assert.match(environment, /^VIP_ROLE_ID=1552725871830503484$/m);
  assert.match(environment, /^WARDOGS_RCON_URL=http:\/\/51\.83\.166\.12:7799$/m);
  assert.match(environment, /^WARDOGS_RCON_2_URL=http:\/\/216\.144\.249\.92:7789$/m);
  assert.match(environment, /^WARDOGS_RCON_2_PASSWORD=CHANGE_ME_SECOND_SERVER$/m);
  assert.match(environment, /^RCON_ALLOW_INSECURE_HTTP=true$/m);
  assert.match(environment, /^RCON_RETRY_ATTEMPTS=1$/m);
  assert.match(environment, /^RCON_OUTAGE_LOG_INTERVAL_SECONDS=900$/m);
  assert.match(environment, /^AUDIT_CHANNEL_ID=1552402077068820611$/m);
  assert.match(environment, /^BACKUP_CHANNEL_ID=1552401803184701502$/m);
  assert.match(environment, /^WEEKLY_AWARD_RECONCILE_SECONDS=300$/m);
  assert.match(environment, /^DATA_DIR=\/app\/data$/m);
  await assert.rejects(access(resolve(root, 'docker-compose.yml'), constants.F_OK));
});

test('secret generator creates only the three locally generated secrets', async (t) => {
  const directory = await mkdtemp(join(tmpdir(), 'wardogs-secret-test-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const result = spawnSync(
    process.execPath,
    [resolve(root, 'scripts/generate-secrets.mjs'), '--dir', directory],
    { encoding: 'utf8' }
  );
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual((await readdir(directory)).sort(), [
    'backup_encryption_key',
    'data_encryption_key',
    'rcon_password'
  ]);
  const dataKey = (await readFile(join(directory, 'data_encryption_key'), 'utf8')).trim();
  const backupKey = (await readFile(join(directory, 'backup_encryption_key'), 'utf8')).trim();
  assert.equal(Buffer.from(dataKey, 'base64').length, 32);
  assert.equal(Buffer.from(backupKey, 'base64').length, 32);
  assert.notEqual(dataKey, backupKey);
  assert.match(result.stdout, /DATA_ENCRYPTION_KEY/);
  assert.match(result.stdout, /Discord token is created only in Discord Developer Portal/);
  assert.doesNotMatch(result.stdout, new RegExp(dataKey.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
});

import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { randomBytes } from 'node:crypto';
import { CryptoBox } from '../src/crypto-box.js';
import { SeedingDatabase } from '../src/database.js';
import { RconError } from '../src/rcon-client.js';
import { SeedingService } from '../src/services/seeding-service.js';

test('seeding logs one RCON outage and automatically reports recovery', async () => {
  let unavailable = true;
  const warningEvents = [];
  const infoEvents = [];
  const voiceChannel = { isVoiceBased: () => true };
  const guild = {
    channels: {
      cache: new Map([['1552253117586739210', voiceChannel]]),
      fetch: async () => voiceChannel
    },
    voiceStates: { cache: new Map() }
  };
  const database = {
    getState: () => ({ blocked: false, belowSince: null }),
    setState() {},
    getUserBySteam: () => null,
    applySeedingTick: () => []
  };
  const rconFailure = () => new RconError('RCON is unreachable', {
    code: 'unreachable',
    route: 'GET /v1/status',
    networkCode: 'ECONNREFUSED'
  });
  const service = new SeedingService({
    database,
    rcon: {
      players: async () => {
        if (unavailable) throw rconFailure();
        return [];
      },
      status: async () => {
        if (unavailable) throw rconFailure();
        return { playerCount: 0, maxPlayers: 100 };
      }
    },
    discord: { guilds: { cache: new Map([['403186008579112973', guild]]) } },
    config: {
      discord: {
        guildId: '403186008579112973',
        voiceChannelId: '1552253117586739210'
      },
      rcon: { outageLogIntervalSeconds: 900 },
      seed: {
        start: { minutes: 0 },
        end: { minutes: 1440 },
        stopPlayerCount: 20,
        resumeBelowSeconds: 300,
        sessionGraceSeconds: 180,
        tickSeconds: 30
      }
    },
    logger: {
      warn: (event) => warningEvents.push(event),
      info: (event) => infoEvents.push(event),
      error() {}
    },
    lease: { held: true }
  });

  await service.tick();
  await service.tick();
  assert.deepEqual(warningEvents, ['rcon_connection_lost']);
  assert.equal(service.lastStatus.state, 'rcon_unavailable');

  unavailable = false;
  await service.tick();
  assert.deepEqual(infoEvents, ['rcon_connection_restored']);
  assert.equal(service.lastStatus.state, 'active');
});

test('two independent servers share one session, survive an outage and never double-count a player', async (t) => {
  const directory = await mkdtemp(join(tmpdir(), 'wardogs-dual-seed-'));
  const crypto = new CryptoBox(randomBytes(32));
  const database = new SeedingDatabase({ path: join(directory, 'seeding.sqlite'), crypto,
    guildId: '403186008579112973', logger: { info() {}, warn() {}, error() {} } });
  t.after(async () => { database.close(); await rm(directory, { recursive: true, force: true }); });
  const discordId = '111111111111111111';
  const steamId = '76561198000000001';
  const code = 'ABCDEFGH';
  database.prepareLinkChallenge({ discordId, steamId, displayName: 'Seeder',
    codeDigest: crypto.challengeDigest(crypto.blindIndex(discordId, 'discord-id'), code),
    expiresAt: Date.now() + 60_000 });
  database.verifyLinkChallenge({ discordId, code, maxAttempts: 5 });

  let now = Date.UTC(2026, 8, 28, 12);
  t.mock.method(Date, 'now', () => now);
  let mainDown = false;
  let secondDown = false;
  let mainPopulation = 1;
  const outage = () => new RconError('unreachable', { code: 'unreachable' });
  const player = [{ steamId, name: 'Seeder' }];
  const rcon = (isMain) => ({
    players: async () => {
      if (isMain ? mainDown : secondDown) throw outage();
      return player;
    },
    status: async () => {
      if (isMain ? mainDown : secondDown) throw outage();
      return { playerCount: isMain ? mainPopulation : 1, maxPlayers: 100 };
    }
  });
  const voiceChannelId = '1552253117586739210';
  const guild = { channels: { cache: new Map([[voiceChannelId, { isVoiceBased: () => true }]]) },
    voiceStates: { cache: new Map([[discordId, { id: discordId, channelId: voiceChannelId,
      member: { displayName: 'Seeder', user: { bot: false } } }]]) } };
  const service = new SeedingService({ database, rcon: rcon(true), rconSecondary: rcon(false),
    discord: { guilds: { cache: new Map([['403186008579112973', guild]]) } },
    config: { discord: { guildId: '403186008579112973', voiceChannelId },
      rcon: { outageLogIntervalSeconds: 900 },
      seed: { start: { minutes: 0 }, end: { minutes: 1439 }, stopPlayerCount: 20,
        resumeBelowSeconds: 300, sessionGraceSeconds: 180, tickSeconds: 30 } },
    logger: { info() {}, warn() {}, error() {} }, lease: { held: true } });

  await service.tick();
  for (let i = 1; i <= 12; i += 1) {
    now += 30_000;
    if (i === 5) mainDown = true;
    if (i === 9) { mainDown = false; mainPopulation = 20; }
    await service.tick();
    assert.equal(service.lastStatus.eligibleCount, 1);
  }
  assert.equal(service.lastStatus.servers[0].state, 'population_limit');
  assert.equal(service.lastStatus.servers[1].state, 'active');
  assert.equal(database.getUserByDiscord(discordId).totalSeconds, 360);
  assert.equal(database.getUserByDiscord(discordId).balance, 1);
  assert.equal(database.getLeaderboard('2026-09-28')[0].weeklySeconds, 360);
  assert.equal(database.verifyIntegrity().users, 1);

  secondDown = true;
  now += 30_000;
  await service.tick();
  assert.equal(service.lastStatus.eligibleCount, 0);
  assert.equal(database.getUserByDiscord(discordId).totalSeconds, 360);
});

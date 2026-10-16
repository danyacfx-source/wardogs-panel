import test from 'node:test';
import assert from 'node:assert/strict';
import { commandDefinitions } from '../src/discord/commands.js';
import { rulesPayload } from '../src/discord/rules.js';
import { shopPayload } from '../src/discord/shop.js';
import { leaderboardCard, ReportService } from '../src/services/report-service.js';
import { liveSlotLines } from '../src/discord/interactions.js';

const config = {
  discord: {
    guildId: '111111111111111111',
    voiceChannelId: '1552253117586739210',
    rankChannelId: '1552254053109727262',
    shopChannelId: '1552255043757936721',
    rulesChannelId: '1552254112060411944',
    seederRoleId: '1552728825568174090',
    superSeederRoleId: '1552841261713719440',
    vipRoleId: '1552725871830503484'
  },
  seed: {
    start: { text: '08:00' },
    end: { text: '22:00' },
    stopPlayerCount: 20,
    resumeBelowSeconds: 300,
    sessionGraceSeconds: 180,
    leaderboardRefreshSeconds: 60,
    weeklyAwardReconcileSeconds: 300
  },
  vip: { oneDayCost: 120, sevenDayCost: 360, thirtyDayCost: 760 }
};

test('commands include unlink and administrative seeder list', () => {
  const [seed, donor, admin] = commandDefinitions(config);
  assert.ok(seed.options.some((option) => option.name === 'unlink'));
  assert.equal(donor.name, 'vip-link');
  assert.ok(donor.options.some((option) => option.name === 'verify'));
  assert.ok(admin.options.some((option) => option.name === 'seeders'));
  for (const name of ['points-all', 'time-add-all']) {
    const sub = admin.options.find((option) => option.name === name);
    assert.ok(sub);
    assert.ok(sub.options.some((option) => option.name === 'confirm' && option.required));
    assert.ok(sub.options.some((option) => option.name === 'operation_id' && option.required));
  }
  assert.ok(admin.options.some((option) => option.name === 'reserved-protect'));
  assert.ok(admin.options.some((option) => option.name === 'reserved-unprotect'));
  assert.ok(admin.options.some((option) => option.name === 'reserved-protected'));
  const report = admin.options.find((option) => option.name === 'report');
  assert.equal(report.options?.length || 0, 0);
});

test('shop, rules and leaderboard payloads contain the requested cards and buttons', () => {
  const shop = shopPayload(config);
  assert.equal(shop.embeds.length, 1);
  assert.equal(shop.components[0].components.length, 3);
  assert.match(shop.embeds[0].toJSON().title, /Магазин/);

  const rules = rulesPayload(config);
  assert.equal(rules.embeds.length, 4);
  assert.deepEqual(rules.embeds.map((embed) => embed.toJSON().title), [
    '📜 Правила сидинга WARDOGS | RU',
    '🚀 Как начать сидинг — пошагово',
    '👑 Магазин наград и VIP',
    '🏆 Еженедельный рейтинг и награды'
  ]);

  const card = leaderboardCard([{
    displayName: 'Tester', weeklySeconds: 3600, totalSeconds: 7200
  }], '2026-09-21', 1_700_000_000_000).toJSON();
  assert.match(card.description, /Tester/);
  assert.match(card.footer.text, /участников: 1/);
});

test('slot status reads both current live lists on each request', async () => {
  const steamId = '76561198000000001';
  let first = [];
  let second = [];
  const main = { liveReservedIds: async () => first };
  const extra = { liveReservedIds: async () => second };
  assert.match(await liveSlotLines(steamId, main, extra), /Сервер 1: ⏳ сейчас не активен/);
  first = [steamId];
  second = [steamId];
  const status = await liveSlotLines(steamId, main, extra);
  assert.match(status, /Сервер 1: ✅ активен сейчас/);
  assert.match(status, /Сервер 2: ✅ активен сейчас/);
  extra.liveReservedIds = async () => { throw new Error('RCON offline'); };
  assert.match(await liveSlotLines(steamId, main, extra), /Сервер 2: не удалось проверить RCON/);
});

test('leaderboard refresh edits the saved message instead of sending a duplicate', async () => {
  let edits = 0;
  let sends = 0;
  const message = {
    id: '999999999999999999',
    async edit(payload) {
      edits += 1;
      assert.equal(payload.embeds.length, 1);
    }
  };
  const channel = {
    isTextBased: () => true,
    isSendable: () => true,
    messages: { fetch: async () => message },
    send: async () => { sends += 1; return message; }
  };
  const service = new ReportService({
    database: {
      getLeaderboard: () => [],
      getState: () => message.id,
      setState: () => { throw new Error('should not create state'); },
      audit: () => { throw new Error('should not create audit'); }
    },
    discord: {
      guilds: {
        cache: new Map([[config.discord.guildId, {
          channels: { cache: new Map([[config.discord.rankChannelId, channel]]) }
        }]])
      }
    },
    config,
    logger: { warn() {}, error() {} },
    lease: { held: true }
  });
  const result = await service.refresh();
  assert.equal(result.created, false);
  assert.equal(edits, 2);
  assert.equal(sends, 0);
});

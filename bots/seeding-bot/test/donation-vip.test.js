import test from 'node:test';
import assert from 'node:assert/strict';
import { createHmac } from 'node:crypto';
import { DonationService, applyVipEvent, validatedVipEvent } from '../src/services/donation-service.js';
import { VipService } from '../src/services/vip-service.js';
import { LinkService } from '../src/services/link-service.js';

const discordId = '111111111111111111';
const steamId = '76561198000000001';
const secret = 'c'.repeat(64);
const paidAt = Date.UTC(2026, 8, 25, 18);

test('VIP-only verification code points to its own Discord command', async () => {
  let challengePurpose;
  let whisper;
  const service = new LinkService({
    database: {
      prepareLinkChallenge: value => { challengePurpose = value.purpose; },
      verifyLinkChallenge: value => ({ purpose: value.purpose }),
      cancelLinkChallenge: () => { throw new Error('whisper should work'); }
    },
    rcon: { players: async () => [{ steamId, name: 'Tester' }],
      whisper: async (_id, message) => { whisper = message; } },
    crypto: { blindIndex: () => 'blind', challengeDigest: () => 'digest' },
    config: { security: { challengeMinutes: 5, challengeMaxAttempts: 5 } },
    logger: { warn() {} }
  });
  await service.begin({ discordId, steamId, purpose: 'donation' });
  assert.equal(challengePurpose, 'donation');
  assert.match(whisper, /\/vip-link verify/);
  assert.doesNotMatch(whisper, /\/seed verify/);
  assert.equal(service.verify({ discordId, code: 'ABCDEFGH', purpose: 'donation' }).purpose, 'donation');
});

test('seeder and paid VIP identities can verify on server 2 without granting Seeder', async () => {
  const whispers = [];
  const primary = { players: async () => [], whisper: async () => whispers.push('main') };
  const second = { players: async () => [{ steamId, name: 'Tester' }],
    whisper: async () => whispers.push('second') };
  const service = new LinkService({
    database: { prepareLinkChallenge() {}, cancelLinkChallenge() {} },
    rcon: primary, rconSecondary: second,
    crypto: { blindIndex: () => 'blind', challengeDigest: () => 'digest' },
    config: { security: { challengeMinutes: 5 } }, logger: { warn() {} }
  });
  await service.begin({ discordId, steamId, purpose: 'seeding' });
  assert.deepEqual(whispers, ['second']);
  await service.begin({ discordId, steamId, purpose: 'donation' });
  assert.deepEqual(whispers, ['second', 'second']);
});
function event(productId = 'personal_1', operationId = 'operation-1') {
  const prices = { personal_1: 50000, clan_10: 450000 };
  const recipients = productId === 'personal_1' ? [discordId] :
    Array.from({ length: 10 }, (_, i) => String(111111111111111111n + BigInt(i)));
  const order = { id: 'a'.repeat(32), buyerId: discordId,
    kind: productId === 'personal_1' ? 'personal' : 'clan', productId, recipients,
    amountKopecks: prices[productId], createdAt: paidAt - 30_000,
    expiresAt: paidAt - 30_000 + 7_200_000 };
  const encoded = Buffer.from(JSON.stringify(order)).toString('base64url');
  const orderToken = `${encoded}.${createHmac('sha256', secret).update(encoded).digest('hex')}`;
  return { operationId, orderId: order.id, grossKopecks: prices[productId],
    currency: '643', timestamp: paidAt / 1000, orderToken };
}

test('only an exact signed VIP payment gives a slot entitlement; donations and repeats do not', () => {
  const state = { orders: {}, vip: {} };
  const good = event();
  assert.equal(applyVipEvent(state, good, secret), true);
  assert.equal(applyVipEvent(state, { ...good, operationId: 'different-operation' }, secret), false);
  assert.equal(Object.keys(state.vip).length, 1);
  assert.equal(state.vip[discordId].endsAt, Date.UTC(2026, 9, 25, 18));
  assert.equal(validatedVipEvent({ ...good, grossKopecks: 49999 }, secret), null);
  assert.equal(validatedVipEvent({ ...good, orderToken: good.orderToken.slice(0, -1) + '0' }, secret), null);
  assert.equal(applyVipEvent({ orders: {}, vip: {} }, { ...good, orderToken: undefined }, secret), false);
  const clan = event('clan_10');
  assert.equal(applyVipEvent({ orders: {}, vip: {} }, clan, secret), true);
});

class FakeDatabase {
  constructor() {
    this.user = { id: 'u1', discordId, steamId, linked: true };
    this.states = {};
    this.member = null;
  }
  getState(key, fallback) { return structuredClone(this.states[key] ?? fallback); }
  setState(key, value) { this.states[key] = structuredClone(value); }
  getUserByDiscord(id) { return id === discordId ? this.user : null; }
  getUserById(id) { return id === 'u1' ? this.user : null; }
  getVipIdentityByDiscord(id) { return this.getUserByDiscord(id); }
  getVipIdentityById(id) { return this.getUserById(id); }
  listVipMemberships() { return this.member ? [this.member] : []; }
  getVipMembership() { return this.member ? structuredClone(this.member) : null; }
  setVipOwnership(_id, managedByBot) { this.member.managedByBot = managedByBot; }
  activateVip() { this.member.state = 'active'; this.member.activeUntil = Date.now() + 86_400_000; }
  setVipState(_id, state) { this.member.state = state; if (state === 'inactive') this.member.managedByBot = null; }
  setVipError(_id, error) { this.member.lastError = error; }
}
function setupVip({ paid = true, manual = false, seeder = null } = {}) {
  const database = new FakeDatabase();
  database.member = seeder;
  const live = new Set(manual || seeder?.state === 'active' ? [steamId] : []);
  const writes = [];
  const rcon = {
    async reservedSnapshot() { return { live: [...live], document: [...live] }; },
    async setReserved(id, desired) {
      writes.push({ id, desired });
      if (desired) live.add(id); else live.delete(id);
      return { active: desired, pendingRestart: false };
    }
  };
  const donations = { enabled: true, paid, healthy: true, active: () => donations.paid,
    activeRecipientIds: () => donations.paid ? [discordId] : [],
    canRemove: () => donations.healthy };
  const vip = new VipService({ database, rcon, donations,
    config: { vip: { oneDayCost: 120, sevenDayCost: 360, thirtyDayCost: 760, reconcileSeconds: 120 } },
    lease: { held: true }, logger: { warn() {}, error() {} } });
  return { database, donations, rcon, vip, live, writes };
}

test('paid VIP grants and removes its own slot after expiry', async () => {
  const f = setupVip();
  await f.vip.reconcile();
  assert.equal(f.live.has(steamId), true);
  assert.equal(f.database.getState('paid_slot_claims', {})[steamId].owner, 'bot');
  f.donations.paid = false;
  await f.vip.reconcile();
  assert.equal(f.live.has(steamId), false);
  assert.deepEqual(f.writes.map(write => write.desired), [true, false]);
});

test('expired paid VIP keeps ownership while an old config entry can return after restart', async () => {
  const f = setupVip();
  await f.vip.reconcile();
  const oldSnapshot = f.rcon.reservedSnapshot;
  f.rcon.reservedSnapshot = async () => ({ ...(await oldSnapshot()), document: [steamId] });
  f.donations.paid = false;
  await f.vip.reconcile();
  assert.equal(f.live.has(steamId), false);
  assert.equal(f.database.getState('paid_slot_claims', {})[steamId].owner, 'bot');
  f.rcon.reservedSnapshot = oldSnapshot;
  await f.vip.reconcile();
  assert.equal(f.database.getState('paid_slot_claims', {})[steamId], undefined);
});

test('VIP-only identity receives a paid slot without becoming a seeding account', async () => {
  const f = setupVip();
  f.database.user.purpose = 'donation';
  f.database.getUserByDiscord = () => null;
  f.database.getUserById = () => null;
  f.database.getVipIdentityByDiscord = id => id === discordId ? f.database.user : null;
  f.database.getVipIdentityById = id => id === 'u1' ? f.database.user : null;
  await f.vip.reconcile();
  assert.equal(f.live.has(steamId), true);
  assert.equal(f.database.getUserByDiscord(discordId), null);
  f.donations.paid = false;
  await f.vip.reconcile();
  assert.equal(f.live.has(steamId), false);
});

test('overlap of paid and seeding VIP preserves the slot until both end', async () => {
  const f = setupVip({ seeder: { userId: 'u1', state: 'active', activeUntil: Date.now() - 1,
    managedByBot: true } });
  await f.vip.reconcile();
  assert.equal(f.database.member.state, 'inactive');
  assert.equal(f.live.has(steamId), true);
  assert.equal(f.database.getState('paid_slot_claims', {})[steamId].owner, 'bot');
  f.donations.paid = false;
  await f.vip.reconcile();
  assert.equal(f.live.has(steamId), false);
  assert.deepEqual(f.writes.map(write => write.desired), [false]);
});

test('paid VIP expiry keeps active seeding VIP; later removes bot owned slot', async () => {
  const f = setupVip();
  await f.vip.reconcile();
  f.database.member = { userId: 'u1', state: 'pending_add', activeUntil: null,
    queuedSeconds: 86_400, managedByBot: null };
  await f.vip.reconcile();
  assert.equal(f.database.member.managedByBot, true);
  assert.equal(f.database.member.state, 'active');
  f.donations.paid = false;
  await f.vip.reconcile();
  assert.equal(f.live.has(steamId), true);
  f.database.member.activeUntil = Date.now() - 1;
  await f.vip.reconcile();
  assert.equal(f.live.has(steamId), false);
});

test('preexisting manual reserved slot survives paid VIP expiry', async () => {
  const f = setupVip({ manual: true });
  await f.vip.reconcile();
  assert.equal(f.database.getState('paid_slot_claims', {})[steamId].owner, 'external');
  f.donations.paid = false;
  await f.vip.reconcile();
  assert.equal(f.live.has(steamId), true);
  assert.equal(f.writes.length, 0);
});

test('feed outage postpones removal until the renewed entitlement can be checked', async () => {
  const f = setupVip();
  await f.vip.reconcile();
  f.donations.paid = false;
  f.donations.healthy = false;
  await f.vip.reconcile();
  assert.equal(f.live.has(steamId), true);
  f.donations.healthy = true;
  await f.vip.reconcile();
  assert.equal(f.live.has(steamId), false);
});

test('seeder reads the signed site feed and persists VIP entitlements', async () => {
  const database = new FakeDatabase();
  const data = event();
  let calls = 0;
  const service = new DonationService({ database, logger: { warn() {} },
    config: { donations: { url: new URL('https://wardogs-ru.ru'),
      feedToken: 'f'.repeat(64), checkoutSecret: secret, pollSeconds: 45 } },
    fetchFn: async (url, options) => {
      assert.equal(options.headers['X-WD-Feed-Token'], 'f'.repeat(64));
      assert.equal(url.searchParams.get('offset'), calls ? '100' : '0');
      calls++;
      return { ok: true, text: async () => JSON.stringify({ events: calls === 1 ? [data] : [], nextOffset: 100 }) };
    } });
  assert.equal(await service.refresh(), 1);
  assert.equal(service.active(discordId, paidAt), true);
  assert.deepEqual(service.recipientIds(), [discordId]);
  assert.deepEqual(service.activeRecipientIds(Date.UTC(2026, 10, 1)), []);
  assert.deepEqual(service.recipientIds(), [discordId]);
  assert.equal(service.canRemove(), true);
  assert.equal(database.getState('donation_feed', {}).offset, 100);
  assert.equal(await service.refresh(), 0);
});

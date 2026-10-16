import test from 'node:test';
import assert from 'node:assert/strict';
import { SecondaryReservedService } from '../src/services/secondary-reserved-service.js';
import { VipService } from '../src/services/vip-service.js';
import { RconError } from '../src/rcon-client.js';

const user = { id: 'user-1', discordId: '111111111111111111',
  steamId: '76561198000000001', linked: true };

function fixture({ manual = false } = {}) {
  const state = new Map();
  const reserved = new Set(manual ? [user.steamId] : []);
  const writes = [];
  const database = {
    member: null,
    getState: (key, fallback) => structuredClone(state.get(key) ?? fallback),
    setState: (key, value) => state.set(key, structuredClone(value)),
    getVipMembership: () => database.member,
    listVipMemberships: () => database.member ? [database.member] : [],
    getVipIdentityById: id => id === user.id ? user : null,
    getVipIdentityByDiscord: id => id === user.discordId ? user : null,
    getUserByDiscord: id => id === user.discordId ? user : null
  };
  const donations = {
    enabled: true, paid: false, healthy: true,
    active: () => donations.paid,
    activeRecipientIds: () => donations.paid ? [user.discordId] : [],
    canRemove: () => donations.healthy
  };
  const weekly = { winner: false,
    isActiveWinner: () => weekly.winner,
    snapshot: () => ({ winners: weekly.winner ? [{ userId: user.id, discordId: user.discordId,
      steamId: user.steamId, active: true }] : [] }) };
  const protection = { protected: false,
    isProtected: () => protection.protected,
    list: () => protection.protected ? [{ userId: user.id, discordId: user.discordId,
      steamId: user.steamId }] : [] };
  const rcon = {
    down: false,
    capabilities: async () => ({ routes: ['GET /v1/reserved-slots', 'POST /v1/reserved-slots',
      'DELETE /v1/reserved-slots/{steamId}'] }),
    reservedSnapshot: async () => {
      if (rcon.down) throw new Error('second RCON outage');
      return { live: [...reserved], document: [...reserved] };
    },
    setReserved: async (id, desired) => {
      if (rcon.down) throw new Error('second RCON outage');
      writes.push(desired);
      if (desired) reserved.add(id); else reserved.delete(id);
      return { active: desired };
    }
  };
  const config = { vip: { reconcileSeconds: 120 }, rcon: { outageLogIntervalSeconds: 900 } };
  const service = new SecondaryReservedService({ database, rcon, config,
    logger: { warn() {}, error() {} }, lease: { held: true },
    weeklyAwardService: weekly, reservedProtectionService: protection, donations });
  return { service, database, donations, weekly, protection, rcon, reserved, writes };
}

test('paid VIP on server 2 is added and removed; feed outage prevents early removal', async () => {
  const f = fixture();
  f.donations.paid = true;
  assert.equal((await f.service.reconcile()).failed, 0);
  assert.equal(f.reserved.has(user.steamId), true);
  assert.equal(f.database.getState('secondary_reserved_slots_v1', {})[user.steamId].owner, 'bot');
  f.donations.paid = false;
  f.donations.healthy = false;
  await f.service.reconcile();
  assert.equal(f.reserved.has(user.steamId), true);
  f.donations.healthy = true;
  await f.service.reconcile();
  assert.equal(f.reserved.has(user.steamId), false);
  assert.deepEqual(f.writes, [true, false]);
});

test('manual server 2 slot is preserved after paid VIP expires', async () => {
  const f = fixture({ manual: true });
  f.donations.paid = true;
  await f.service.reconcile();
  assert.equal(f.database.getState('secondary_reserved_slots_v1', {})[user.steamId].owner, 'external');
  f.donations.paid = false;
  await f.service.reconcile();
  assert.equal(f.reserved.has(user.steamId), true);
  assert.deepEqual(f.writes, []);
});

test('weekly award hands server 2 slot to paid VIP and later removes it', async () => {
  const f = fixture();
  f.weekly.winner = true;
  await f.service.reconcile();
  f.donations.paid = true;
  f.weekly.winner = false;
  await f.service.reconcile();
  assert.equal(f.reserved.has(user.steamId), true);
  f.donations.paid = false;
  await f.service.reconcile();
  assert.equal(f.reserved.has(user.steamId), false);
  assert.deepEqual(f.writes, [true, false]);
});

test('permanent protection on server 2 keeps a previously bot-owned slot after unprotect', async () => {
  const f = fixture();
  f.database.member = { userId: user.id, state: 'active', activeUntil: Date.now() + 60_000 };
  await f.service.reconcile();
  f.protection.protected = true;
  await f.service.reconcile();
  assert.equal(f.database.getState('secondary_reserved_slots_v1', {})[user.steamId].owner, 'external');
  f.database.member = null;
  f.protection.protected = false;
  await f.service.reconcile();
  assert.equal(f.reserved.has(user.steamId), true);
  assert.deepEqual(f.writes, [true]);
});

test('protected slot is retried on the first server after its RCON recovers', async () => {
  const f = fixture();
  f.protection.protected = true;
  let available = false;
  let presentOnPrimary = false;
  f.service.primaryRcon = {
    reservedSnapshot: async () => {
      if (!available) throw new Error('first RCON outage');
      return { live: presentOnPrimary ? [user.steamId] : [], document: [] };
    },
    setReserved: async () => { presentOnPrimary = true; return { active: true }; }
  };
  assert.equal((await f.service.reconcile()).failed, 1);
  assert.equal(f.reserved.has(user.steamId), true);
  available = true;
  assert.equal((await f.service.reconcile()).failed, 0);
  assert.equal(presentOnPrimary, true);
});

test('new VIP for points starts only after both servers confirm a live slot', async () => {
  const f = fixture();
  let primaryActive = false;
  const primary = {
    reservedSnapshot: async () => ({ live: primaryActive ? [user.steamId] : [],
      document: primaryActive ? [user.steamId] : [] }),
    setReserved: async () => { primaryActive = true; return { active: true }; }
  };
  f.database.member = { userId: user.id, state: 'pending_add', queuedSeconds: 86400,
    managedByBot: null };
  f.database.setVipOwnership = (_id, owned) => { f.database.member.managedByBot = owned; };
  f.database.setVipError = (_id, reason) => { f.database.member.lastError = reason; };
  f.database.activateVip = () => { f.database.member.state = 'active'; };
  f.rcon.down = true;
  const vip = new VipService({ database: f.database, rcon: primary,
    secondaryReservedService: f.service,
    config: { vip: { oneDayCost: 120, sevenDayCost: 360, thirtyDayCost: 760 },
      rcon: { outageLogIntervalSeconds: 900 } },
    logger: { warn() {}, error() {} }, lease: { held: true } });
  assert.equal((await vip.reconcileUser(user.id)).ok, false);
  assert.equal(f.database.member.state, 'pending_add');
  f.rcon.down = false;
  await vip.reconcileUser(user.id);
  assert.equal(f.database.member.state, 'active');
  assert.equal(f.reserved.has(user.steamId), true);
  assert.equal(primaryActive, true);
});

test('VIP points are not charged when the second RCON has no writable reserved-slot API', async () => {
  const f = fixture();
  let charged = false;
  f.database.purchaseVip = () => { charged = true; throw new Error('should not charge'); };
  f.rcon.capabilities = async () => ({ routes: ['GET /v1/reserved-slots'] });
  const vip = new VipService({ database: f.database,
    rcon: { reservedSnapshot: async () => ({ live: [], document: [] }),
      assertLiveReservedWritable: async () => {} },
    secondaryReservedService: f.service,
    config: { vip: { oneDayCost: 120, sevenDayCost: 360, thirtyDayCost: 760 } },
    logger: { warn() {}, error() {} }, lease: { held: true } });
  await assert.rejects(() => vip.purchase({ discordId: user.discordId,
    sku: '1d', idempotencyKey: 'no-second-api' }), /баллы не списаны/);
  assert.equal(charged, false);
});

test('VIP points are not charged when the first RCON lacks live reserved-slot writes', async () => {
  const f = fixture();
  let charged = false;
  f.database.purchaseVip = () => { charged = true; throw new Error('should not charge'); };
  const vip = new VipService({ database: f.database,
    rcon: { assertLiveReservedWritable: async () => {
      throw new RconError('requires live routes', { code: 'live_reserved_unavailable' });
    } },
    secondaryReservedService: f.service,
    config: { vip: { oneDayCost: 120, sevenDayCost: 360, thirtyDayCost: 760 } },
    logger: { warn() {}, error() {} }, lease: { held: true } });
  await assert.rejects(() => vip.purchase({ discordId: user.discordId,
    sku: '1d', idempotencyKey: 'no-first-live-api' }), /баллы не списаны/);
  assert.equal(charged, false);
});

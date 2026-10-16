import test from 'node:test';
import assert from 'node:assert/strict';
import { RoleService } from '../src/services/role-service.js';
import { WeeklyAwardService } from '../src/services/weekly-award-service.js';
import { VipService } from '../src/services/vip-service.js';
import { ReservedProtectionService } from '../src/services/reserved-protection-service.js';

const roleIds = {
  seederRoleId: '1552728825568174090',
  superSeederRoleId: '1552841261713719440',
  vipRoleId: '1552725871830503484'
};

function stateDatabase(extra = {}) {
  const state = new Map();
  return {
    getState: (key, fallback) => state.has(key) ? structuredClone(state.get(key)) : structuredClone(fallback),
    setState: (key, value) => state.set(key, structuredClone(value)),
    audit() {},
    listUsers: () => [],
    getVipMembership: () => null,
    ...extra,
    state
  };
}

test('role service grants and removes the configured Seeder role idempotently', async () => {
  const assigned = new Set();
  const member = {
    roles: {
      cache: { has: (id) => assigned.has(id) },
      add: async (id) => assigned.add(id),
      remove: async (id) => assigned.delete(id)
    }
  };
  const database = stateDatabase();
  const service = new RoleService({
    database,
    discord: {
      guilds: {
        cache: new Map([['403186008579112973', {
          members: { cache: new Map(), fetch: async () => member }
        }]])
      }
    },
    config: {
      discord: { guildId: '403186008579112973', roleReconcileSeconds: 300, ...roleIds }
    },
    logger: { warn() {}, error() {} },
    lease: { held: true }
  });

  assert.equal((await service.setSeeder('111111111111111111', true)).changed, true);
  assert.equal(assigned.has(roleIds.seederRoleId), true);
  assert.equal((await service.setSeeder('111111111111111111', true)).changed, false);
  assert.equal((await service.setSeeder('111111111111111111', false)).changed, true);
  assert.equal(assigned.has(roleIds.seederRoleId), false);
  assert.equal(service.pendingCount(), 0);
});

test('failed Discord role changes are encrypted-state queued and retried', async () => {
  let available = false;
  const assigned = new Set();
  const member = {
    roles: {
      cache: { has: (id) => assigned.has(id) },
      add: async (id) => assigned.add(id),
      remove: async (id) => assigned.delete(id)
    }
  };
  const database = stateDatabase();
  const service = new RoleService({
    database,
    discord: {
      guilds: {
        cache: new Map([['403186008579112973', {
          members: {
            cache: new Map(),
            fetch: async () => {
              if (!available) throw new Error('temporary Discord outage');
              return member;
            }
          }
        }]])
      }
    },
    config: {
      discord: { guildId: '403186008579112973', roleReconcileSeconds: 300, ...roleIds }
    },
    logger: { warn() {}, error() {} },
    lease: { held: true }
  });

  assert.equal((await service.setSeeder('111111111111111111', true)).queued, true);
  assert.equal(service.pendingCount(), 1);
  available = true;
  await service.reconcile();
  assert.equal(assigned.has(roleIds.seederRoleId), true);
  assert.equal(service.pendingCount(), 0);
});

test('one shared VIP role follows paid VIP and VIP bought for seeding points', async () => {
  const seederId = '111111111111111111';
  const buyerId = '222222222222222222';
  const assigned = new Map([[seederId, new Set()], [buyerId, new Set()]]);
  const memberFor = id => ({ roles: {
    cache: { has: roleId => assigned.get(id).has(roleId) },
    add: async roleId => assigned.get(id).add(roleId),
    remove: async roleId => assigned.get(id).delete(roleId)
  } });
  const seeder = { id: 'seed-1', discordId: seederId };
  let seedingVip = false;
  const paid = new Set([seederId, buyerId]);
  let feedReady = true;
  const service = new RoleService({
    database: stateDatabase({
      listUsers: () => [seeder],
      getUserByDiscord: id => id === seederId ? seeder : null,
      getVipMembership: () => ({ state: seedingVip ? 'active' : 'inactive' })
    }),
    discord: { guilds: { cache: new Map([['403186008579112973', {
      members: { cache: new Map(), fetch: async id => memberFor(id) }
    }]]) } },
    config: { discord: { guildId: '403186008579112973', roleReconcileSeconds: 300, ...roleIds } },
    logger: { warn() {}, error() {} }, lease: { held: true },
    donations: { enabled: true, active: id => paid.has(id),
      recipientIds: () => [seederId, buyerId], canRemove: () => feedReady }
  });

  await service.reconcile();
  assert.equal(assigned.get(seederId).has(roleIds.vipRoleId), true);
  assert.equal(assigned.get(buyerId).has(roleIds.vipRoleId), true);
  paid.delete(seederId);
  seedingVip = true;
  await service.setVip(seederId, false);
  await service.reconcile();
  assert.equal(assigned.get(seederId).has(roleIds.vipRoleId), true);
  paid.delete(buyerId);
  feedReady = false;
  await service.reconcile();
  assert.equal(assigned.get(buyerId).has(roleIds.vipRoleId), true);
  feedReady = true;
  await service.reconcile();
  assert.equal(assigned.get(buyerId).has(roleIds.vipRoleId), false);
  seedingVip = false;
  await service.reconcile();
  assert.equal(assigned.get(seederId).has(roleIds.vipRoleId), false);
});

test('queued VIP removal is recalculated when a paid VIP arrives', async () => {
  const discordId = '222222222222222222';
  const assigned = new Set([roleIds.vipRoleId]);
  let available = false;
  let paid = false;
  const service = new RoleService({
    database: stateDatabase({ getUserByDiscord: () => null }),
    discord: { guilds: { cache: new Map([['403186008579112973', {
      members: { cache: new Map(), fetch: async () => {
        if (!available) throw new Error('Discord unavailable');
        return { roles: { cache: { has: id => assigned.has(id) },
          add: async id => assigned.add(id), remove: async id => assigned.delete(id) } };
      } }
    }]]) } },
    config: { discord: { guildId: '403186008579112973', roleReconcileSeconds: 300, ...roleIds } },
    logger: { warn() {}, error() {} }, lease: { held: true },
    donations: { enabled: true, active: () => paid, recipientIds: () => [discordId], canRemove: () => true }
  });
  assert.equal((await service.setVip(discordId, false)).queued, true);
  available = true;
  paid = true;
  await service.reconcile();
  assert.equal(assigned.has(roleIds.vipRoleId), true);
  assert.equal(service.pendingCount(), 0);
});

test('weekly awards rotate top three roles and award-owned reserved slots', async () => {
  const users = [1, 2, 3, 4, 5].map((number) => ({
    id: `user-${number}`,
    discordId: `11111111111111111${number}`,
    steamId: `7656119800000000${number}`,
    displayName: `Seeder ${number}`,
    weeklySeconds: (6 - number) * 3600,
    weeklyPoints: number,
    totalSeconds: number * 7200
  }));
  let leaderboard = users.slice(0, 3);
  const linked = new Map(users.map((user) => [user.discordId, user]));
  const database = stateDatabase({
    getLeaderboard: () => leaderboard,
    getUserByDiscord: (id) => linked.get(id) || null,
    getVipMembership: () => ({ state: 'inactive' })
  });
  const roleChanges = [];
  const reserved = new Set();
  const service = new WeeklyAwardService({
    database,
    rcon: {
      reservedSnapshot: async () => ({ live: [...reserved], document: [...reserved] }),
      setReserved: async (steamId, desired) => {
        if (desired) reserved.add(steamId);
        else reserved.delete(steamId);
        return { active: desired, pendingRestart: false };
      }
    },
    roleService: {
      setSuperSeeder: async (discordId, present) => {
        roleChanges.push({ discordId, present });
        return { ok: true };
      }
    },
    config: {
      seed: { weeklyAwardReconcileSeconds: 300 },
      rcon: { outageLogIntervalSeconds: 900 }
    },
    logger: { warn() {}, error() {} },
    lease: { held: true }
  });
  let secondReconciles = 0;
  service.secondaryReservedService = {
    reconcile: async () => { secondReconciles++; },
    status: () => 'active'
  };

  await service.reconcile(Date.parse('2026-09-21T00:00:00Z'));
  let award = service.snapshot();
  assert.equal(award.awardWeek, '2026-09-14');
  assert.deepEqual(award.winners.map((winner) => winner.userId), ['user-1', 'user-2', 'user-3']);
  assert.ok(award.winners.every((winner) => winner.slotOwnership === 'award' && winner.slotStatus === 'active'));
  assert.ok(award.winners.every((winner) => winner.secondarySlotStatus === 'active'));
  assert.deepEqual([...reserved].sort(), users.slice(0, 3).map((user) => user.steamId).sort());

  leaderboard = [users[1], users[3], users[4]];
  await service.reconcile(Date.parse('2026-09-28T00:00:00Z'));
  award = service.snapshot();
  assert.equal(award.awardWeek, '2026-09-21');
  assert.deepEqual(award.winners.map((winner) => winner.userId), ['user-2', 'user-4', 'user-5']);
  assert.deepEqual([...reserved].sort(), leaderboard.map((user) => user.steamId).sort());
  assert.ok(roleChanges.some((change) => change.discordId === users[0].discordId && change.present === false));
  assert.ok(roleChanges.some((change) => change.discordId === users[3].discordId && change.present === true));
  assert.equal(award.cleanup.length, 0);
  assert.equal(secondReconciles, 2);
});

test('weekly award does not remove a reserved slot still required by VIP', async () => {
  const user = {
    id: 'user-1',
    discordId: '111111111111111111',
    steamId: '76561198000000001',
    displayName: 'Seeder',
    weeklySeconds: 7200,
    weeklyPoints: 30,
    totalSeconds: 7200
  };
  let leaderboard = [user];
  let vipActive = false;
  const reserved = new Set();
  const database = stateDatabase({
    getLeaderboard: () => leaderboard,
    getUserByDiscord: () => user,
    getVipMembership: () => ({ state: vipActive ? 'active' : 'inactive' })
  });
  const service = new WeeklyAwardService({
    database,
    rcon: {
      reservedSnapshot: async () => ({ live: [...reserved], document: [...reserved] }),
      setReserved: async (steamId, desired) => {
        if (desired) reserved.add(steamId);
        else reserved.delete(steamId);
        return { active: desired };
      }
    },
    roleService: { setSuperSeeder: async () => ({ ok: true }) },
    config: {
      seed: { weeklyAwardReconcileSeconds: 300 },
      rcon: { outageLogIntervalSeconds: 900 }
    },
    logger: { warn() {}, error() {} },
    lease: { held: true }
  });

  await service.reconcile(Date.parse('2026-09-21T00:00:00Z'));
  vipActive = true;
  leaderboard = [];
  await service.reconcile(Date.parse('2026-09-28T00:00:00Z'));
  assert.equal(reserved.has(user.steamId), true);
  assert.equal(service.snapshot().cleanup[0].status, 'waiting_vip');

  vipActive = false;
  await service.reconcile(Date.parse('2026-09-28T00:01:00Z'));
  assert.equal(reserved.has(user.steamId), false);
  assert.equal(service.snapshot().cleanup.length, 0);
});

test('weekly slot transfers to paid VIP and survives a payment-feed outage before expiry', async () => {
  const user = {
    id: 'user-paid', discordId: '111111111111111111', steamId: '76561198000000001',
    displayName: 'Paid Seeder', weeklySeconds: 7200, weeklyPoints: 30, linked: true
  };
  let leaderboard = [user];
  const reserved = new Set();
  const writes = [];
  const database = stateDatabase({
    getLeaderboard: () => leaderboard,
    getUserByDiscord: () => user,
    getVipIdentityByDiscord: () => user,
    getVipIdentityById: () => user,
    listVipMemberships: () => []
  });
  const donations = {
    enabled: true, paid: false, healthy: true,
    active() { return this.paid; },
    activeRecipientIds() { return this.paid ? [user.discordId] : []; },
    canRemove() { return this.healthy; }
  };
  const rcon = {
    reservedSnapshot: async () => ({ live: [...reserved], document: [...reserved] }),
    setReserved: async (steamId, desired) => {
      writes.push(desired);
      if (desired) reserved.add(steamId); else reserved.delete(steamId);
      return { active: desired };
    }
  };
  const config = {
    seed: { weeklyAwardReconcileSeconds: 300 },
    vip: { oneDayCost: 120, sevenDayCost: 360, thirtyDayCost: 760, reconcileSeconds: 120 },
    rcon: { outageLogIntervalSeconds: 900 }
  };
  const weekly = new WeeklyAwardService({ database, rcon, config, donations,
    roleService: { setSuperSeeder: async () => ({ ok: true }) },
    logger: { warn() {}, error() {} }, lease: { held: true } });
  const vip = new VipService({ database, rcon, config, donations, weeklyAwardService: weekly,
    logger: { warn() {}, error() {} }, lease: { held: true } });

  await weekly.reconcile(Date.parse('2026-09-21T00:00:00Z'));
  donations.paid = true;
  leaderboard = [];
  await weekly.reconcile(Date.parse('2026-09-28T00:00:00Z'));
  assert.equal(reserved.has(user.steamId), true);
  assert.equal(database.getState('paid_slot_claims', {})[user.steamId].owner, 'bot');
  assert.deepEqual(writes, [true]);

  donations.paid = false;
  donations.healthy = false;
  await vip.reconcile();
  assert.equal(reserved.has(user.steamId), true);
  donations.healthy = true;
  await vip.reconcile();
  assert.equal(reserved.has(user.steamId), false);
  assert.deepEqual(writes, [true, false]);
});

test('protected permanent slot survives weekly rotation and is never queued for removal', async () => {
  const user = {
    id: 'user-protected',
    discordId: '111111111111111111',
    steamId: '76561198000000001',
    displayName: 'Permanent Seeder',
    weeklySeconds: 10_800,
    weeklyPoints: 40,
    totalSeconds: 10_800
  };
  let leaderboard = [user];
  const reserved = new Set();
  const writes = [];
  const database = stateDatabase({
    getLeaderboard: () => leaderboard,
    getUserByDiscord: () => user,
    getVipMembership: () => ({ state: 'inactive' })
  });
  const protection = new ReservedProtectionService({ database });
  const service = new WeeklyAwardService({
    database,
    rcon: {
      reservedSnapshot: async () => ({ live: [...reserved], document: [...reserved] }),
      setReserved: async (steamId, desired) => {
        writes.push(desired);
        if (desired) reserved.add(steamId);
        else reserved.delete(steamId);
        return { active: desired };
      }
    },
    roleService: { setSuperSeeder: async () => ({ ok: true }) },
    config: {
      seed: { weeklyAwardReconcileSeconds: 300 },
      rcon: { outageLogIntervalSeconds: 900 }
    },
    logger: { warn() {}, error() {} },
    lease: { held: true },
    reservedProtectionService: protection
  });

  await service.reconcile(Date.parse('2026-09-21T00:00:00Z'));
  assert.equal(reserved.has(user.steamId), true);
  assert.equal(service.snapshot().winners[0].slotOwnership, 'award');

  // Administration makes the already temporary, bot-owned slot permanent.
  protection.protect({ user, actorId: '222222222222222222' });
  service.markProtected(user.id, user.steamId);
  assert.equal(service.snapshot().winners[0].slotOwnership, 'external');
  assert.equal(service.snapshot().winners[0].slotProtected, true);

  leaderboard = [];
  await service.reconcile(Date.parse('2026-09-28T00:00:00Z'));
  assert.equal(reserved.has(user.steamId), true);
  assert.deepEqual(writes, [true]);
  assert.equal(service.snapshot().cleanup.length, 0);
});

test('protected permanent slot is not removed when bot-managed VIP expires', async () => {
  const user = {
    id: 'user-protected',
    discordId: '111111111111111111',
    steamId: '76561198000000001'
  };
  let membership = {
    userId: user.id,
    state: 'active',
    activeUntil: Date.now() - 1,
    managedByBot: true
  };
  let reservedWrites = 0;
  const roleStates = [];
  const database = stateDatabase({
    getUserById: () => user,
    getVipIdentityById: () => ({ ...user, linked: true }),
    getVipMembership: () => membership,
    setVipState: (_userId, state) => { membership.state = state; },
    setVipError() {}
  });
  const protection = new ReservedProtectionService({ database });
  protection.protect({ user: { ...user, displayName: 'Permanent Seeder' }, actorId: '222222222222222222' });
  const service = new VipService({
    database,
    rcon: { setReserved: async () => { reservedWrites += 1; return { active: false }; } },
    config: {
      vip: { oneDayCost: 120, sevenDayCost: 360, thirtyDayCost: 760, reconcileSeconds: 120 },
      rcon: { outageLogIntervalSeconds: 900 }
    },
    logger: { warn() {}, error() {} },
    lease: { held: true },
    roleService: {
      setVip: async (_discordId, present) => { roleStates.push(present); return { ok: true }; }
    },
    reservedProtectionService: protection
  });

  await service.reconcileUser(user.id);
  assert.equal(membership.state, 'inactive');
  assert.equal(reservedWrites, 0);
  assert.equal(roleStates.at(-1), false);
});

test('VIP purchase is rejected before charging when permanent slot is protected', async () => {
  const user = {
    id: 'user-protected',
    discordId: '111111111111111111',
    steamId: '76561198000000001',
    displayName: 'Permanent Seeder'
  };
  let charged = false;
  let queriedRcon = false;
  const database = stateDatabase({
    getUserByDiscord: () => user,
    getVipMembership: () => ({ state: 'active' }),
    purchaseVip: () => { charged = true; }
  });
  const protection = new ReservedProtectionService({ database });
  protection.protect({ user, actorId: '222222222222222222' });
  const service = new VipService({
    database,
    rcon: { reservedSnapshot: async () => { queriedRcon = true; return { live: [], document: [] }; } },
    config: {
      vip: { oneDayCost: 120, sevenDayCost: 360, thirtyDayCost: 760, reconcileSeconds: 120 },
      rcon: { outageLogIntervalSeconds: 900 }
    },
    logger: { warn() {}, error() {} },
    lease: { held: true },
    reservedProtectionService: protection
  });

  await assert.rejects(
    service.purchase({ discordId: user.discordId, sku: '1d', idempotencyKey: 'protected-order' }),
    (error) => error.code === 'protected_reserved'
  );
  assert.equal(charged, false);
  assert.equal(queriedRcon, false);
});

test('VIP can adopt an award-owned slot, grants its role and hands the slot back on expiry', async () => {
  const user = {
    id: 'user-1',
    discordId: '111111111111111111',
    steamId: '76561198000000001',
    balance: 500
  };
  let membership = null;
  const roleStates = [];
  let adopted = 0;
  let reservedWrites = 0;
  const database = {
    getUserByDiscord: () => user,
    getUserById: () => user,
    getVipIdentityById: () => ({ ...user, linked: true }),
    getState: () => ({}),
    setState() {},
    getVipMembership: () => membership,
    purchaseVip: ({ cost, durationSeconds }) => {
      membership = {
        userId: user.id,
        state: 'pending_add',
        queuedSeconds: durationSeconds,
        activeUntil: null,
        managedByBot: null
      };
      return { userId: user.id, balance: user.balance - cost };
    },
    setVipOwnership: (_userId, managedByBot) => { membership.managedByBot = managedByBot; },
    activateVip: () => {
      membership.state = 'active';
      membership.activeUntil = Date.now() + membership.queuedSeconds * 1000;
      membership.queuedSeconds = 0;
    },
    setVipState: (_userId, state) => { membership.state = state; },
    setVipError() {},
    listVipMemberships: () => [membership]
  };
  const weeklyAwardService = {
    active: false,
    hasManagedSlot: () => true,
    isActiveWinner() { return this.active; },
    adoptManagedSlot: () => { adopted += 1; }
  };
  const service = new VipService({
    database,
    rcon: {
      reservedSnapshot: async () => ({ live: [user.steamId], document: [user.steamId] }),
      assertLiveReservedWritable: async () => {},
      setReserved: async () => { reservedWrites += 1; return { active: true }; }
    },
    config: {
      vip: { oneDayCost: 120, sevenDayCost: 360, thirtyDayCost: 760, reconcileSeconds: 120 },
      rcon: { outageLogIntervalSeconds: 900 }
    },
    logger: { warn() {}, error() {} },
    lease: { held: true },
    roleService: {
      setVip: async (_discordId, present) => {
        roleStates.push(present);
        return { ok: true };
      }
    },
    weeklyAwardService
  });

  const purchase = await service.purchase({ discordId: user.discordId, sku: '1d', idempotencyKey: 'order-1' });
  assert.equal(purchase.membership.state, 'active');
  assert.equal(purchase.membership.managedByBot, true);
  assert.equal(roleStates.includes(true), true);
  assert.equal(reservedWrites, 0);

  weeklyAwardService.active = true;
  membership.activeUntil = Date.now() - 1;
  await service.reconcileUser(user.id);
  assert.equal(membership.state, 'inactive');
  assert.equal(adopted, 1);
  assert.equal(roleStates.at(-1), false);
  assert.equal(reservedWrites, 0);
});

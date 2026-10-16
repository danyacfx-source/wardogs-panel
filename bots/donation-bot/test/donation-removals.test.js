'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { Store } = require('../src/store');
const { PaymentWorkflow } = require('../src/payment-workflow');
const { TIERS } = require('../src/catalog');
const { applyDonationRemoval, applyManualDonation, applyPayment, parseRemovalRub,
  topDonors, tierFor, ManualDonationError } = require('../src/core');

const buyerId = '1553090039615852675';
const adminId = '1553092734791983174';
const requestId = '1553093158441590975';
const now = Date.UTC(2026, 8, 30, 5, 20);
const input = (overrides = {}) => ({ buyerId, adminId, requestId, amount: 50000,
  reason: 'Исправление ошибочно начисленной суммы', ...overrides });
const state = (total = 250000) => ({ orders: {}, donors: { [buyerId]: { total, public: true } },
  payments: {}, reviews: {}, vip: {}, topDirty: false });

test('removal parses exact kopecks and permits correcting amounts below the donation minimum', () => {
  assert.equal(parseRemovalRub('0,01'), 1);
  assert.equal(parseRemovalRub('0.50'), 50);
  assert.equal(parseRemovalRub('50.01'), 5001);
  assert.equal(parseRemovalRub('100000.00'), 10000000);
  for (const value of ['0', '0,00', '-50', '0.001', '100000.01', '1e3', 'NaN', '01.50']) {
    assert.throws(() => parseRemovalRub(value), ManualDonationError);
  }
});

test('removal lowers the cumulative role and rank while retaining staff audit details', () => {
  const s = state();
  s.donors[adminId] = { total: 225000, public: true };
  const result = applyDonationRemoval(s, input(), now);
  assert.equal(result.status, 'removed');
  assert.equal(s.donors[buyerId].total, 200000);
  assert.deepEqual(topDonors(s).map(([id]) => id), [adminId, buyerId]);
  assert.equal(s.topDirty, true);
  assert.equal(result.adjustment.previousRoleId, TIERS[1].roleId);
  assert.equal(result.adjustment.newRoleId, TIERS[0].roleId);
  assert.equal(result.adjustment.totalBefore, 250000);
  assert.equal(result.adjustment.totalAfter, 200000);
  assert.equal(result.adjustment.delta, -50000);
  assert.equal(result.adjustment.adminId, adminId);
  assert.equal(result.adjustment.reason, input().reason);
  assert.equal(result.adjustment.recordedAt, now);
  assert.equal(result.adjustment.roleDone, false);
});

test('zero total is removed from the leaderboard and has no sponsor role', () => {
  const s = state(50000);
  const result = applyDonationRemoval(s, input(), now);
  assert.equal(s.donors[buyerId].total, 0);
  assert.equal(result.adjustment.newRoleId, null);
  assert.deepEqual(topDonors(s), []);
  assert.deepEqual(s.payments, {});
});

test('single-kopeck correction can remove the first role without changing privacy', () => {
  const s = state(50000);
  s.donors[buyerId].public = false;
  const result = applyDonationRemoval(s, input({ amount: 1 }), now);
  assert.equal(s.donors[buyerId].total, 49999);
  assert.equal(s.donors[buyerId].public, false);
  assert.equal(result.adjustment.newRoleId, null);
  assert.deepEqual(topDonors(s), []);
});

test('same command is idempotent even after a full removal and conflicting retries are rejected', () => {
  const s = state(50000);
  const first = applyDonationRemoval(s, input(), now);
  const retry = applyDonationRemoval(s, input(), now + 1000);
  assert.equal(retry.status, 'duplicate');
  assert.equal(retry.adjustment, first.adjustment);
  assert.equal(s.donors[buyerId].total, 0);
  assert.equal(Object.keys(s.adjustments).length, 1);
  for (const changed of [{ amount: 49999 }, { buyerId: adminId }, { adminId: buyerId }]) {
    assert.throws(() => applyDonationRemoval(s, input(changed), now), ManualDonationError);
  }
});

test('excessive, empty and malformed removals never change totals or the audit journal', () => {
  const invalid = [
    { amount: 50001 }, { amount: 0 }, { amount: -1 }, { amount: 0.5 },
    { amount: 10000001 }, { reason: '   ' }, { reason: 'x'.repeat(301) },
    { buyerId: 'invalid' }, { buyerId: adminId }, { adminId: 'invalid' }, { requestId: 'invalid' },
  ];
  for (const values of invalid) {
    const s = state(50000);
    const original = structuredClone(s);
    assert.throws(() => applyDonationRemoval(s, input(values), now), ManualDonationError);
    assert.deepEqual(s, original);
  }
  const empty = state(0);
  assert.throws(() => applyDonationRemoval(empty, input(), now), /нет учтённой суммы/);
  assert.deepEqual(empty, state(0));
});

test('payment deduplication and active VIP survive removing a recorded amount', () => {
  const s = state(0);
  const original = applyManualDonation(s, input({ operationId: 'real-yoomoney-id' }), now).payment;
  s.vip['existing-vip'] = { userId: buyerId, startsAt: now, endsAt: now + 86400000 };
  const payments = structuredClone(s.payments);
  const vip = structuredClone(s.vip);
  applyDonationRemoval(s, input(), now);
  assert.deepEqual(s.payments, payments);
  assert.deepEqual(s.vip, vip);
  assert.equal(applyPayment(s, { operationId: original.operationId, orderId: original.orderId,
    grossKopecks: original.amount, currency: '643', timestamp: Math.floor(now / 1000) }, now).status, 'duplicate');
  assert.equal(s.donors[buyerId].total, 0);
});

async function fixture(t, total = 250000) {
  const directory = await fs.mkdtemp(path.join(__dirname, 'removal-test-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const store = new Store(directory);
  await store.open();
  await store.mutate(s => { s.donors[buyerId] = { total, public: true }; });
  const calls = { imports: 0, thanks: [], roles: [], top: [] };
  const workflow = new PaymentWorkflow({
    store, logger: { error() {} },
    async importFeed() { calls.imports++; throw new Error('Site offline'); },
    async deliverPayments() {
      for (const p of Object.values(store.data.payments)) if (!p.thanksDone) {
        calls.thanks.push(p.operationId);
        await store.mutate(s => { s.payments[p.operationId].thanksDone = true; });
      }
      return [];
    },
    async deliverAdjustments() {
      for (const a of Object.values(store.data.adjustments)) if (!a.roleDone) {
        calls.roles.push(tierFor(store.data.donors[a.buyerId].total)?.roleId || null);
        await store.mutate(s => { s.adjustments[a.id].roleDone = true; });
      }
      return [];
    },
    async refreshTop() { calls.top.push(structuredClone(topDonors(store.data))); },
    async syncSponsorRoles() {},
  });
  return { store, calls, workflow };
}

test('removal updates roles and top immediately without a new thanks or contacting the site', async t => {
  const { store, calls, workflow } = await fixture(t);
  await workflow.removeManual({}, input(), now);
  assert.equal(calls.imports, 0);
  assert.deepEqual(calls.roles, [TIERS[0].roleId]);
  assert.equal(calls.top[0][0][1].total, 200000);
  assert.deepEqual(calls.thanks, []);
  assert.equal(store.data.topDirty, false);
});

test('saved correction retries role and leaderboard changes after restart with the site offline', async t => {
  const { store, calls, workflow } = await fixture(t, 50000);
  const deliver = workflow.deliverAdjustments;
  const refresh = workflow.refreshTop;
  workflow.deliverAdjustments = async () => ['Missing role permissions'];
  workflow.refreshTop = async () => { throw new Error('Missing channel permissions'); };
  const result = await workflow.removeManual({}, input(), now);
  assert.equal(result.errors.length, 2);
  assert.equal(result.adjustment.roleDone, false);
  assert.equal(store.data.topDirty, true);
  const reopened = new Store(store.directory);
  await reopened.open();
  assert.equal(reopened.data.donors[buyerId].total, 0);
  assert.equal(reopened.data.adjustments[result.adjustment.id].roleDone, false);
  store.data = reopened.data;
  workflow.deliverAdjustments = deliver;
  workflow.refreshTop = refresh;
  await workflow.check({});
  assert.equal(store.data.adjustments[result.adjustment.id].roleDone, true);
  assert.deepEqual(calls.roles, [null]);
  assert.deepEqual(calls.top, [[]]);
  assert.equal(store.data.topDirty, false);
  assert.equal((await workflow.removeManual({}, input(), now)).status, 'duplicate');
  assert.equal(calls.roles.length, 1);
  assert.equal(store.data.donors[buyerId].total, 0);
});

test('concurrent removals cannot make a negative total and a rejected job does not block later credits', async t => {
  const { store, workflow } = await fixture(t, 50000);
  const results = await Promise.allSettled([
    workflow.removeManual({}, input(), now),
    workflow.removeManual({}, input({ requestId: adminId }), now),
  ]);
  assert.equal(results[0].status, 'fulfilled');
  assert.equal(results[1].status, 'rejected');
  assert.equal(store.data.donors[buyerId].total, 0);
  assert.equal(Object.keys(store.data.adjustments).length, 1);
  await workflow.addManual({}, input({ requestId: buyerId, amount: 5000 }), now);
  assert.equal(store.data.donors[buyerId].total, 5000);
});

test('opening a previous-version database adds the correction journal and keeps all prior data', async t => {
  const directory = await fs.mkdtemp(path.join(__dirname, 'removal-test-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const old = state();
  old.panels = { top: 'existing-message-id' };
  old.feedOffset = 456;
  old.vip = { vip: { userId: buyerId, endsAt: now + 1000 } };
  await fs.writeFile(path.join(directory, 'donations.json'), JSON.stringify(old));
  const store = new Store(directory);
  await store.open();
  assert.deepEqual(store.data, { ...old, adjustments: {} });
  assert.deepEqual(JSON.parse(await fs.readFile(store.file, 'utf8')), { ...old, adjustments: {} });
});

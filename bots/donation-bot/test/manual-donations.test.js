'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { Store } = require('../src/store');
const { PaymentWorkflow } = require('../src/payment-workflow');
const { TIERS } = require('../src/catalog');
const { applyManualDonation, applyPayment, createOrder, parseRub, topDonors, ManualDonationError } = require('../src/core');

const buyerId = '1553090039615852675';
const adminId = '1553092734791983174';
const requestId = '1553093158441590975';
const now = Date.UTC(2026, 8, 30, 4, 45);
const input = (overrides = {}) => ({ buyerId, adminId, requestId, amount: 5000,
  reason: 'Перевод получен, уведомление не обработалось', ...overrides });
const state = () => ({ orders: {}, donors: {}, payments: {}, reviews: {}, vip: {}, topDirty: false });
const silentLogger = { error() {} };

test('manual support joins the cumulative total, rank and normal role/thanks outbox', () => {
  const s = state();
  s.donors[buyerId] = { total: 45000, public: false };
  s.donors[adminId] = { total: 49000, public: true };
  const result = applyManualDonation(s, input({ amount: parseRub('50,01') }), now);
  assert.equal(result.status, 'credited');
  assert.equal(s.donors[buyerId].total, 50001);
  assert.equal(s.donors[buyerId].public, true);
  assert.deepEqual(topDonors(s).map(([id]) => id), [buyerId, adminId]);
  assert.equal(s.topDirty, true);
  assert.equal(result.payment.previousRoleId, null);
  assert.equal(result.payment.newRoleId, TIERS[0].roleId);
  assert.equal(result.payment.roleDone, false);
  assert.equal(result.payment.thanksDone, false);
  assert.equal(result.payment.source, 'manual');
  assert.equal(result.payment.adminId, adminId);
  assert.equal(result.payment.reason, input().reason);
  assert.equal(result.payment.recordedAt, now);
  assert.equal(result.payment.vipDone, true);
  assert.deepEqual(s.vip, {});
});

test('manual sum is added to previous support and can cross several tiers', () => {
  const s = state();
  s.donors[buyerId] = { total: TIERS[0].min, public: true };
  const result = applyManualDonation(s, input({ amount: 2000000 }), now);
  assert.equal(result.payment.totalAfter, 2050000);
  assert.equal(result.payment.previousRoleId, TIERS[0].roleId);
  assert.equal(result.payment.newRoleId, TIERS[4].roleId);
});

test('late automatic notification and repeated manual operation do not double-credit', () => {
  const s = state();
  const original = createOrder({ buyerId, kind: 'donation', amount: 5000 }, now - 60000);
  s.orders[original.id] = original;
  const manual = input({ operationId: '76543210987654321' });
  applyManualDonation(s, manual, now);
  const orderCount = Object.keys(s.orders).length;
  assert.equal(applyManualDonation(s, { ...manual, requestId: adminId }, now).status, 'duplicate');
  assert.equal(applyPayment(s, { operationId: manual.operationId, orderId: original.id,
    grossKopecks: 5000, currency: '643', timestamp: Math.floor((now - 10000) / 1000) }, now).status, 'duplicate');
  assert.equal(s.donors[buyerId].total, 5000);
  assert.equal(Object.keys(s.payments).length, 1);
  assert.equal(Object.keys(s.orders).length, orderCount);
  assert.throws(() => applyManualDonation(s, { ...manual, buyerId: adminId }, now), ManualDonationError);
  assert.throws(() => applyManualDonation(s, { ...manual, amount: 5001 }, now), ManualDonationError);
});

test('retrying the same Discord interaction without a YooMoney ID is idempotent', () => {
  const s = state();
  assert.equal(applyManualDonation(s, input(), now).status, 'credited');
  assert.equal(applyManualDonation(s, input(), now).status, 'duplicate');
  assert.equal(applyManualDonation(s, input({ operationId: 'different-id' }), now).status, 'duplicate');
  assert.equal(s.donors[buyerId].total, 5000);
  assert.equal(Object.keys(s.orders).length, 1);
});

test('invalid manual data makes no changes and decimal rubles keep their precision', () => {
  assert.equal(parseRub('100000'), 10000000);
  assert.equal(parseRub('50.01'), 5001);
  for (const value of ['49.99', '100000.01', '1e3', '-50', '50.001']) assert.throws(() => parseRub(value));
  const invalid = [
    { amount: 4999 }, { amount: 10000001 }, { amount: 5000.1 },
    { reason: '   ' }, { reason: 'a'.repeat(301) }, { buyerId: 'wrong' },
    { adminId: 'wrong' }, { requestId: 'wrong' }, { operationId: '' },
    { operationId: 'bad/id' }, { operationId: 'manual-reserved' },
  ];
  for (const values of invalid) {
    const s = state();
    assert.throws(() => applyManualDonation(s, input(values), now), ManualDonationError);
    assert.deepEqual(s, state());
  }
});

test('reviewed operation validates known amount and buyer before resolving the review', () => {
  const s = state();
  const order = createOrder({ buyerId, kind: 'donation', amount: 5000 }, now);
  s.orders[order.id] = order;
  s.reviews['review-id'] = { reason: 'time_mismatch', event: { orderId: order.id, grossKopecks: 5000 } };
  const original = structuredClone(s);
  assert.throws(() => applyManualDonation(s, input({ operationId: 'review-id', amount: 5001 }), now), ManualDonationError);
  assert.deepEqual(s, original);
  assert.throws(() => applyManualDonation(s, input({ operationId: 'review-id', buyerId: adminId }), now), ManualDonationError);
  assert.deepEqual(s, original);
  assert.equal(applyManualDonation(s, input({ operationId: 'review-id' }), now).status, 'credited');
  assert.equal(s.reviews['review-id'], undefined);
});

async function fixture(t, overrides = {}) {
  const directory = await fs.mkdtemp(path.join(__dirname, 'manual-test-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const store = new Store(directory);
  await store.open();
  const calls = { imports: 0, role: [], thanks: [], top: [] };
  const options = {
    store, logger: silentLogger,
    async importFeed() { calls.imports++; throw new Error('Site offline'); },
    async deliverPayments() {
      const errors = [];
      for (const p of Object.values(store.data.payments)) {
        if (!p.roleDone) {
          calls.role.push(p.operationId);
          await store.mutate(s => { s.payments[p.operationId].roleDone = true; });
        }
        if (!p.thanksDone) {
          calls.thanks.push(p.operationId);
          await store.mutate(s => { s.payments[p.operationId].thanksDone = true; });
        }
      }
      return errors;
    },
    async refreshTop() { calls.top.push(topDonors(store.data)); },
    async syncSponsorRoles() {},
    ...overrides,
  };
  return { store, calls, workflow: new PaymentWorkflow(options), options };
}

test('manual entry publishes immediately without fetching the site and survives reopening', async t => {
  const { store, calls, workflow } = await fixture(t);
  const result = await workflow.addManual({}, input({ amount: 50000 }), now);
  assert.equal(calls.imports, 0);
  assert.equal(calls.role.length, 1);
  assert.equal(calls.thanks.length, 1);
  assert.equal(calls.top[0][0][0], buyerId);
  assert.equal(calls.top[0][0][1].total, 50000);
  assert.equal(store.data.topDirty, false);
  assert.deepEqual(result.errors, []);
  const reopened = new Store(store.directory);
  await reopened.open();
  assert.equal(reopened.data.payments[result.payment.operationId].adminId, adminId);
  assert.equal(reopened.data.donors[buyerId].total, 50000);
  await assert.rejects(() => workflow.addManual({}, input(), now), ManualDonationError);
  await workflow.addManual({}, input({ amount: 50000 }), now);
  assert.equal(calls.thanks.length, 1);
  assert.equal(store.data.donors[buyerId].total, 50000);
});

test('failed Discord updates remain pending and retry even when the site is unavailable', async t => {
  const { store, calls, workflow, options } = await fixture(t);
  let failing = true;
  workflow.deliverPayments = async guild => failing ? ['Нет доступа к каналу благодарностей'] : options.deliverPayments(guild);
  workflow.refreshTop = async guild => {
    if (failing) throw new Error('Missing permissions');
    return options.refreshTop(guild);
  };
  const first = await workflow.addManual({}, input(), now);
  assert.equal(first.status, 'credited');
  assert.equal(first.payment.thanksDone, false);
  assert.equal(store.data.donors[buyerId].total, 5000);
  assert.equal(store.data.topDirty, true);
  assert.equal(first.errors.length, 2);
  failing = false;
  const retried = await workflow.check({});
  assert.match(retried.feedError.message, /offline/);
  assert.equal(calls.imports, 1);
  assert.equal(calls.thanks.length, 1);
  assert.equal(first.payment.thanksDone, true);
  assert.equal(store.data.topDirty, false);
  await workflow.check({});
  assert.equal(calls.thanks.length, 1);
  assert.equal(store.data.donors[buyerId].total, 5000);
});

test('automatic check and manual delivery cannot publish the same pending thanks concurrently', async t => {
  const { store, calls, workflow } = await fixture(t);
  await store.mutate(s => applyManualDonation(s, input(), now));
  await Promise.all([workflow.check({}), workflow.addManual({}, input(), now)]);
  assert.equal(calls.thanks.length, 1);
  assert.equal(calls.top.length, 1);
  assert.equal(store.data.donors[buyerId].total, 5000);
});

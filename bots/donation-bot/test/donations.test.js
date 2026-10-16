'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { createHmac } = require('node:crypto');
const { TIERS, PRODUCTS } = require('../src/catalog');
const { parseRub, tierFor, createOrder, checkoutToken, applyPayment, addCalendarMonths, topDonors } = require('../src/core');
const { SiteFeed } = require('../src/site-feed');

const buyer = '1553090039615852675';
const secret = 'x'.repeat(64);
const now = Date.UTC(2026, 8, 25, 18);
const state = () => ({ orders: {}, donors: {}, payments: {}, reviews: {}, vip: {}, topDirty: false });
function incoming(order, id, amount = order.amountKopecks) {
  return { operationId: id, orderId: order.id, grossKopecks: amount, currency: '643',
    timestamp: Math.floor(now / 1000), orderToken: checkoutToken(order, secret) };
}

test('rubles use exact kopecks and tiers are cumulative', () => {
  assert.equal(parseRub('50'), 5000);
  assert.equal(parseRub('50,01'), 5001);
  assert.equal(parseRub('500'), 50000);
  assert.equal(parseRub('1 000'.replace(' ', '')), 100000);
  assert.equal(parseRub('1200,50'), 120050);
  assert.throws(() => parseRub('49.99'));
  assert.throws(() => parseRub('100.999'));
  assert.doesNotThrow(() => createOrder({ buyerId: buyer, kind: 'donation', amount: 5000 }, now));
  assert.throws(() => createOrder({ buyerId: buyer, kind: 'donation', amount: 4999 }, now));
  for (const tier of TIERS) assert.equal(tierFor(tier.min)?.roleId, tier.roleId);
  assert.equal(tierFor(49999), null);
});

test('signed checkout token is bound to amount, buyer, order and expiry', () => {
  const order = createOrder({ buyerId: buyer, kind: 'donation', amount: 50000 }, now);
  const [encoded, signature] = checkoutToken(order, 'x'.repeat(64)).split('.');
  assert.equal(signature, createHmac('sha256', 'x'.repeat(64)).update(encoded).digest('hex'));
  const payload = JSON.parse(Buffer.from(encoded, 'base64url').toString());
  assert.deepEqual(payload, { id: order.id, buyerId: buyer, kind: 'donation', productId: null,
    recipients: [], amountKopecks: 50000, createdAt: order.createdAt, expiresAt: order.expiresAt });
});

test('payments credit once; mismatch is held for review; threshold upgrade recorded', () => {
  const s = state();
  const first = createOrder({ buyerId: buyer, kind: 'donation', amount: 50000 }, now);
  s.orders[first.id] = first;
  assert.equal(applyPayment(s, incoming(first, 'op1'), now, secret).status, 'credited');
  assert.equal(applyPayment(s, incoming(first, 'op1'), now, secret).status, 'duplicate');
  assert.equal(applyPayment(s, incoming(first, 'op-again'), now, secret).status, 'review');
  assert.equal(s.donors[buyer].total, 50000);
  assert.equal(s.payments.op1.newRoleId, TIERS[0].roleId);
  const second = createOrder({ buyerId: buyer, kind: 'donation', amount: 200000 }, now);
  s.orders[second.id] = second;
  assert.equal(applyPayment(s, incoming(second, 'op2', 199999), now, secret).status, 'review');
  assert.equal(s.donors[buyer].total, 50000);
  assert.equal(applyPayment(s, incoming(second, 'op3'), now, secret).status, 'credited');
  assert.equal(s.payments.op3.previousRoleId, TIERS[0].roleId);
  assert.equal(s.payments.op3.newRoleId, TIERS[1].roleId);
  assert.deepEqual(topDonors(s).map(([id]) => id), [buyer]);
});

test('a 50 ruble donation is counted without awarding a sponsor role early', () => {
  const s = state();
  const order = createOrder({ buyerId: buyer, kind: 'donation', amount: 5000 }, now);
  s.orders[order.id] = order;
  assert.equal(applyPayment(s, incoming(order, 'small-donation'), now, secret).status, 'credited');
  assert.equal(s.donors[buyer].total, 5000);
  assert.equal(s.payments['small-donation'].newRoleId, null);
  assert.equal(Object.keys(s.vip).length, 0);
});

test('VIP clan package grants exactly 30 seats and counts toward buyer total', () => {
  const s = state();
  const recipients = Array.from({ length: 30 }, (_, i) => String(1553090039615852700n + BigInt(i)));
  const product = PRODUCTS.clan_30;
  const order = createOrder({ buyerId: buyer, kind: 'clan', productId: 'clan_30', amount: product.amount, recipients }, now);
  s.orders[order.id] = order;
  assert.equal(applyPayment(s, incoming(order, 'clanOp'), now, secret).status, 'credited');
  assert.equal(Object.keys(s.vip).length, 30);
  assert.equal(s.donors[buyer].total, 1200000);
  assert.equal(s.payments.clanOp.newRoleId, TIERS[3].roleId);
  assert.throws(() => createOrder({ buyerId: buyer, kind: 'clan', productId: 'clan_30', amount: product.amount, recipients: [...recipients.slice(0,29), recipients[0]] }, now));
});

test('calendar month renewals stack and clamp at month end', () => {
  assert.equal(new Date(addCalendarMonths(Date.UTC(2026, 0, 31), 1)).toISOString().slice(0, 10), '2026-02-28');
  const s = state();
  const p = PRODUCTS.personal_1;
  const a = createOrder({ buyerId: buyer, kind: 'personal', productId: 'personal_1', amount: p.amount, recipients: [buyer] }, now);
  const b = createOrder({ buyerId: buyer, kind: 'personal', productId: 'personal_1', amount: p.amount, recipients: [buyer] }, now);
  s.orders[a.id] = a; s.orders[b.id] = b;
  assert.equal(applyPayment(s, { ...incoming(a, 'vip-missing'), orderToken: undefined }, now, secret).status, 'review');
  applyPayment(s, incoming(a, 'vip1'), now, secret);
  applyPayment(s, incoming(b, 'vip2'), now, secret);
  assert.equal(s.vip['vip2:' + buyer].startsAt, s.vip['vip1:' + buyer].endsAt);
});

test('a verified VIP notification can be retried with its signed order after review', () => {
  const s = state();
  const p = PRODUCTS.personal_1;
  const order = createOrder({ buyerId: buyer, kind: 'personal', productId: 'personal_1',
    amount: p.amount, recipients: [buyer] }, now);
  s.orders[order.id] = order;
  const notification = incoming(order, 'original-vip-operation');
  assert.equal(applyPayment(s, { ...notification, orderToken: undefined }, now, secret).status, 'review');
  assert.equal(s.reviews[notification.operationId].reason, 'missing_order_token');
  assert.equal(s.donors[buyer], undefined);
  assert.equal(applyPayment(s, notification, now, secret).status, 'credited');
  assert.equal(s.reviews[notification.operationId], undefined);
  assert.equal(s.donors[buyer].total, p.amount);
  assert.equal(applyPayment(s, notification, now, secret).status, 'duplicate');
});

test('payment review distinguishes missing bot state from an amount mismatch', () => {
  const s = state();
  const order = createOrder({ buyerId: buyer, kind: 'donation', amount: 50000 }, now);
  assert.equal(applyPayment(s, incoming(order, 'without-order'), now, secret).status, 'review');
  assert.equal(s.reviews['without-order'].reason, 'unknown_order');
  s.orders[order.id] = order;
  assert.equal(applyPayment(s, incoming(order, 'wrong-sum', 49900), now, secret).status, 'review');
  assert.equal(s.reviews['wrong-sum'].reason, 'amount_mismatch');
});

test('site feed uses token and rejects malformed cursors', async () => {
  let auth;
  const feed = new SiteFeed('https://wardogs-ru.ru', 'secret', async (_url, options) => {
    auth = options.headers['X-WD-Feed-Token'];
    return { ok: true, json: async () => ({ events: [], nextOffset: 100 }) };
  });
  assert.equal((await feed.read(0)).nextOffset, 100);
  assert.equal(auth, 'secret');
  await assert.rejects(() => feed.read(101), /Invalid donation feed/);
});

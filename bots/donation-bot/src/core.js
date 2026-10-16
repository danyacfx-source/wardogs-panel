'use strict';

const { randomBytes, createHmac } = require('node:crypto');
const { TIERS, PRODUCTS } = require('./catalog');
const ORDER_LIFETIME_MS = 2 * 60 * 60 * 1000;
const PAYMENT_GRACE_MS = 24 * 60 * 60 * 1000;

const formatRub = (kopecks) => new Intl.NumberFormat('ru-RU', { style: 'currency', currency: 'RUB' }).format(kopecks / 100);
function parseRub(input) {
  const s = String(input).trim().replace(',', '.');
  if (!/^[1-9]\d{0,5}(?:\.\d{1,2})?$/.test(s)) throw new Error('Укажите сумму от 50 до 100 000 ₽.');
  const [r, k = ''] = s.split('.');
  const amount = Number(r) * 100 + Number(k.padEnd(2, '0'));
  if (amount < 5000 || amount > 10000000) throw new Error('Укажите сумму от 50 до 100 000 ₽.');
  return amount;
}
function parseRemovalRub(input) {
  const s = String(input).trim().replace(',', '.');
  if (!/^(?:0|[1-9]\d{0,5})(?:\.\d{1,2})?$/.test(s)) throw new ManualDonationError('Укажите сумму списания от 0,01 до 100 000 ₽.');
  const [r, k = ''] = s.split('.');
  const amount = Number(r) * 100 + Number(k.padEnd(2, '0'));
  if (amount < 1 || amount > 10000000) throw new ManualDonationError('Укажите сумму списания от 0,01 до 100 000 ₽.');
  return amount;
}
function tierFor(total) { return [...TIERS].reverse().find(t => total >= t.min) || null; }
function addCalendarMonths(startMs, months) {
  const d = new Date(startMs);
  const day = d.getUTCDate();
  d.setUTCDate(1);
  d.setUTCMonth(d.getUTCMonth() + months);
  const lastDay = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 0)).getUTCDate();
  d.setUTCDate(Math.min(day, lastDay));
  return d.getTime();
}
function createOrder({ buyerId, kind, amount, productId = null, recipients = [], publicThanks = true }, now = Date.now()) {
  if (!/^\d{17,20}$/.test(buyerId)) throw new Error('Invalid buyer ID');
  if (!['donation', 'personal', 'clan'].includes(kind)) throw new Error('Invalid order type');
  if (kind === 'donation') {
    if (!Number.isInteger(amount) || amount < 5000 || amount > 10000000 || recipients.length) throw new Error('Invalid donation');
  } else {
    const p = PRODUCTS[productId];
    if (!p || p.kind !== kind || amount !== p.amount || recipients.length !== p.seats ||
        new Set(recipients).size !== recipients.length || recipients.some(id => !/^\d{17,20}$/.test(id)) ||
        (kind === 'personal' && recipients[0] !== buyerId)) throw new Error('Invalid VIP order');
  }
  return { id: randomBytes(16).toString('hex'), buyerId, kind, productId, amountKopecks: amount,
    recipients: [...recipients], publicThanks: Boolean(publicThanks), createdAt: now,
    expiresAt: now + ORDER_LIFETIME_MS };
}
function checkoutToken(order, secret) {
  const payload = { id: order.id, buyerId: order.buyerId, kind: order.kind,
    productId: order.productId, recipients: order.recipients,
    amountKopecks: order.amountKopecks, createdAt: order.createdAt, expiresAt: order.expiresAt };
  const base = Buffer.from(JSON.stringify(payload)).toString('base64url');
  return `${base}.${createHmac('sha256', secret).update(base).digest('hex')}`;
}
function paymentProblem(event, order, checkoutSecret) {
  if (!order || event?.orderId !== order.id) return 'unknown_order';
  if (!/^[A-Za-z0-9-]{1,80}$/.test(event.operationId || '') || event.currency !== '643' ||
      !Number.isSafeInteger(event.grossKopecks) || !Number.isInteger(event.timestamp)) return 'invalid_event';
  if (event.grossKopecks !== order.amountKopecks) return 'amount_mismatch';
  if (event.timestamp * 1000 < order.createdAt - 60000 ||
      event.timestamp * 1000 > order.expiresAt + PAYMENT_GRACE_MS) return 'time_mismatch';
  if (order.kind !== 'donation') {
    if (!event.orderToken) return 'missing_order_token';
    if (typeof checkoutSecret !== 'string' || event.orderToken !== checkoutToken(order, checkoutSecret)) return 'order_token_mismatch';
  }
  return null;
}
function paymentIsValid(event, order, checkoutSecret) { return paymentProblem(event, order, checkoutSecret) === null; }
function applyPayment(state, event, now = Date.now(), checkoutSecret = '') {
  if (state.payments[event.operationId]) return { status: 'duplicate' };
  const order = state.orders[event.orderId];
  if (Object.values(state.payments).some(payment => payment.orderId === event.orderId)) {
    state.reviews[event.operationId] = { event, reason: 'order_already_paid' };
    return { status: 'review' };
  }
  const problem = paymentProblem(event, order, checkoutSecret);
  if (problem) {
    state.reviews[event.operationId] = { event, reason: problem };
    return { status: 'review' };
  }
  delete state.reviews[event.operationId];
  const donor = state.donors[order.buyerId] || { total: 0, public: true };
  const before = tierFor(donor.total);
  donor.total += event.grossKopecks;
  donor.public = order.publicThanks;
  state.donors[order.buyerId] = donor;
  const after = tierFor(donor.total);
  const payment = { operationId: event.operationId, orderId: order.id, buyerId: order.buyerId,
    amount: event.grossKopecks, kind: order.kind, productId: order.productId,
    timestamp: event.timestamp * 1000, totalAfter: donor.total, previousRoleId: before?.roleId || null,
    newRoleId: after?.roleId || null, roleDone: false, thanksDone: !order.publicThanks,
    vipDone: order.kind === 'donation' };
  state.payments[event.operationId] = payment;
  state.topDirty = true;
  if (order.kind !== 'donation') {
    const product = PRODUCTS[order.productId];
    for (const recipientId of order.recipients) {
      const paidAt = event.timestamp * 1000;
      const previousEnd = Math.max(paidAt, ...Object.values(state.vip).filter(v =>
        v.userId === recipientId && v.endsAt > paidAt).map(v => v.endsAt));
      const id = `${event.operationId}:${recipientId}`;
      state.vip[id] = { id, userId: recipientId, buyerId: order.buyerId,
        productId: order.productId, startsAt: previousEnd, endsAt: addCalendarMonths(previousEnd, product.months) };
    }
  }
  return { status: 'credited', payment };
}
function topDonors(state) {
  return Object.entries(state.donors).filter(([,d]) => d.public !== false && d.total > 0)
    .sort((a,b) => b[1].total - a[1].total || a[0].localeCompare(b[0])).slice(0,10);
}

class ManualDonationError extends Error {}
function applyManualDonation(state, { buyerId, amount, adminId, reason, operationId = null, requestId }, now = Date.now()) {
  if (!/^\d{17,20}$/.test(buyerId) || !/^\d{17,20}$/.test(adminId) || !/^\d{17,20}$/.test(requestId)) {
    throw new ManualDonationError('Не удалось определить пользователя или автора ручной записи.');
  }
  if (!Number.isInteger(amount) || amount < 5000 || amount > 10000000) {
    throw new ManualDonationError('Укажите сумму от 50 до 100 000 ₽.');
  }
  const note = typeof reason === 'string' ? reason.trim() : '';
  if (!note || note.length > 300) throw new ManualDonationError('Укажите причину ручной записи: от 1 до 300 символов.');
  const id = operationId === null ? `manual-${requestId}` : String(operationId).trim();
  if (!/^[A-Za-z0-9-]{1,80}$/.test(id) || (operationId !== null && id.startsWith('manual-'))) {
    throw new ManualDonationError('ID операции ЮMoney: от 1 до 80 латинских букв, цифр или дефисов, без префикса manual-.');
  }
  const existing = state.payments[id] || Object.values(state.payments).find(p => p.manualRequestId === requestId);
  if (existing) {
    if (existing.buyerId !== buyerId || existing.amount !== amount) {
      throw new ManualDonationError('Эта операция уже учтена с другой суммой или для другого пользователя. Проверьте ID операции.');
    }
    return { status: 'duplicate', payment: existing };
  }
  const review = state.reviews[id];
  if (review && review.event.grossKopecks !== amount) {
    throw new ManualDonationError('Сумма не совпадает с уведомлением по этой операции. Проверьте сумму и ID операции.');
  }
  const reviewedOrder = review && state.orders[review.event.orderId];
  if (reviewedOrder && reviewedOrder.buyerId !== buyerId) {
    throw new ManualDonationError('Эта операция относится к другому пользователю. Проверьте получателя и ID операции.');
  }
  const order = createOrder({ buyerId, kind: 'donation', amount, publicThanks: true }, now);
  order.source = 'manual';
  state.orders[order.id] = order;
  const result = applyPayment(state, { operationId: id, orderId: order.id, currency: '643',
    grossKopecks: amount, timestamp: Math.floor(now / 1000) }, now);
  Object.assign(result.payment, { source: 'manual', adminId, reason: note,
    recordedAt: now, manualRequestId: requestId });
  return result;
}

function applyDonationRemoval(state, { buyerId, amount, adminId, reason, requestId }, now = Date.now()) {
  if (!/^\d{17,20}$/.test(buyerId) || !/^\d{17,20}$/.test(adminId) || !/^\d{17,20}$/.test(requestId)) {
    throw new ManualDonationError('Не удалось определить пользователя или автора списания.');
  }
  if (!Number.isSafeInteger(amount) || amount < 1 || amount > 10000000) {
    throw new ManualDonationError('Укажите сумму списания от 0,01 до 100 000 ₽.');
  }
  const note = typeof reason === 'string' ? reason.trim() : '';
  if (!note || note.length > 300) throw new ManualDonationError('Укажите причину списания: от 1 до 300 символов.');
  const id = `manual-remove-${requestId}`;
  const existing = state.adjustments?.[id];
  if (existing) {
    if (existing.buyerId !== buyerId || existing.amount !== amount || existing.adminId !== adminId) {
      throw new ManualDonationError('Эта команда уже выполнена с другими параметрами. Проверьте запись списания.');
    }
    return { status: 'duplicate', adjustment: existing };
  }
  const donor = state.donors[buyerId];
  if (!donor || donor.total === 0) throw new ManualDonationError('У этого пользователя нет учтённой суммы поддержки.');
  if (!Number.isSafeInteger(donor.total) || donor.total < 0) {
    throw new ManualDonationError('Сумма поддержки в данных некорректна. Списание не выполнено.');
  }
  if (amount > donor.total) {
    throw new ManualDonationError(`Нельзя списать больше общей суммы поддержки: сейчас ${formatRub(donor.total)}.`);
  }
  const totalBefore = donor.total;
  const totalAfter = totalBefore - amount;
  const adjustment = { id, buyerId, amount, delta: -amount, kind: 'donation-removal',
    source: 'manual', adminId, reason: note, requestId, recordedAt: now,
    totalBefore, totalAfter, previousRoleId: tierFor(totalBefore)?.roleId || null,
    newRoleId: tierFor(totalAfter)?.roleId || null, roleDone: false };
  donor.total = totalAfter;
  state.adjustments ||= {};
  state.adjustments[id] = adjustment;
  state.topDirty = true;
  // Keep original payments for audit and deduplication of delayed notifications.
  return { status: 'removed', adjustment };
}

module.exports = { formatRub, parseRub, tierFor, addCalendarMonths, createOrder,
  checkoutToken, paymentProblem, paymentIsValid, applyPayment, topDonors,
  applyManualDonation, ManualDonationError, parseRemovalRub, applyDonationRemoval };

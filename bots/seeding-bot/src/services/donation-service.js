import { createHmac, timingSafeEqual } from 'node:crypto';

const SNOWFLAKE = /^\d{17,20}$/;
const PRODUCTS = Object.freeze({
  personal_1: { kind: 'personal', amount: 50000, seats: 1, months: 1 },
  personal_3: { kind: 'personal', amount: 120000, seats: 1, months: 3 },
  personal_12: { kind: 'personal', amount: 480000, seats: 1, months: 12 },
  clan_10: { kind: 'clan', amount: 450000, seats: 10, months: 1 },
  clan_20: { kind: 'clan', amount: 850000, seats: 20, months: 1 },
  clan_30: { kind: 'clan', amount: 1200000, seats: 30, months: 1 }
});

export function calendarMonths(start, months) {
  const date = new Date(start);
  const day = date.getUTCDate();
  date.setUTCDate(1);
  date.setUTCMonth(date.getUTCMonth() + months);
  const last = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + 1, 0)).getUTCDate();
  date.setUTCDate(Math.min(day, last));
  return date.getTime();
}

export function validatedVipEvent(event, secret) {
  if (!event || !/^[A-Za-z0-9-]{1,80}$/.test(event.operationId || '') ||
      !/^[a-f0-9]{32}$/.test(event.orderId || '') || event.currency !== '643' ||
      !Number.isSafeInteger(event.grossKopecks) || !Number.isSafeInteger(event.timestamp) ||
      typeof event.orderToken !== 'string' || event.orderToken.length > 2048) return null;
  const match = /^([A-Za-z0-9_-]+)\.([a-f0-9]{64})$/.exec(event.orderToken);
  if (!match) return null;
  const expected = createHmac('sha256', secret).update(match[1]).digest();
  const actual = Buffer.from(match[2], 'hex');
  if (!timingSafeEqual(actual, expected)) return null;
  let order;
  try { order = JSON.parse(Buffer.from(match[1], 'base64url').toString('utf8')); }
  catch { return null; }
  const product = PRODUCTS[order?.productId];
  if (!product || order.id !== event.orderId || order.kind !== product.kind ||
      order.amountKopecks !== product.amount || event.grossKopecks !== product.amount ||
      !SNOWFLAKE.test(order.buyerId || '') ||
      !Number.isSafeInteger(order.createdAt) || !Number.isSafeInteger(order.expiresAt) ||
      order.expiresAt !== order.createdAt + 7_200_000 ||
      !Array.isArray(order.recipients) || order.recipients.length !== product.seats ||
      order.recipients.some(id => !SNOWFLAKE.test(id)) ||
      new Set(order.recipients).size !== order.recipients.length ||
      (product.kind === 'personal' && order.recipients[0] !== order.buyerId) ||
      event.timestamp * 1000 < order.createdAt - 60_000 ||
      event.timestamp * 1000 > order.expiresAt + 86_400_000) return null;
  return { orderId: order.id, operationId: event.operationId, recipients: order.recipients,
    months: product.months, paidAt: event.timestamp * 1000 };
}

export function applyVipEvent(state, event, secret) {
  const grant = validatedVipEvent(event, secret);
  if (!grant || state.orders[grant.orderId]) return false;
  state.orders[grant.orderId] = grant.operationId;
  for (const discordId of grant.recipients) {
    const startsAt = Math.max(grant.paidAt, state.vip[discordId]?.endsAt || 0);
    state.vip[discordId] = { endsAt: calendarMonths(startsAt, grant.months), orderId: grant.orderId };
  }
  return true;
}

export class DonationService {
  constructor({ database, config, logger, fetchFn = fetch }) {
    this.database = database;
    this.config = config.donations;
    this.logger = logger;
    this.fetchFn = fetchFn;
    this.state = database.getState('donation_feed', { offset: 0, orders: {}, vip: {} });
    this.lastSuccess = 0;
    this.timer = null;
    this.running = false;
  }

  get enabled() { return Boolean(this.config?.url); }
  until(discordId) { return this.enabled ? this.state.vip[discordId]?.endsAt || null : null; }
  active(discordId, now = Date.now()) { return this.enabled && (this.state.vip[discordId]?.endsAt || 0) > now; }
  recipientIds() { return this.enabled ? Object.keys(this.state.vip) : []; }
  activeRecipientIds(now = Date.now()) {
    return this.enabled ? Object.keys(this.state.vip).filter(id => this.active(id, now)) : [];
  }
  canRemove(now = Date.now()) {
    return !this.enabled || now - this.lastSuccess < Math.max(180_000, this.config.pollSeconds * 3_000);
  }
  start(onChange) {
    if (!this.enabled) return;
    this.timer = setInterval(() => this.refresh().then(() => onChange()).catch(error =>
      this.logger.warn('donation_feed_unavailable', { error })), this.config.pollSeconds * 1000);
    this.timer.unref();
  }
  stop() { if (this.timer) clearInterval(this.timer); }

  async refresh() {
    if (!this.enabled || this.running) return 0;
    this.running = true;
    let granted = 0;
    try {
      for (let page = 0; page < 50; page++) {
        const url = new URL('/donate/feed.php', this.config.url);
        url.searchParams.set('offset', String(this.state.offset));
        const response = await this.fetchFn(url, { headers: { 'X-WD-Feed-Token': this.config.feedToken },
          redirect: 'error', signal: AbortSignal.timeout(12_000) });
        if (!response.ok) throw new Error(`Donation feed HTTP ${response.status}`);
        const raw = await response.text();
        if (raw.length > 1_000_000) throw new Error('Donation feed response is too large');
        const batch = JSON.parse(raw);
        if (!Array.isArray(batch.events) || batch.events.length > 100 ||
            !Number.isSafeInteger(batch.nextOffset) || batch.nextOffset < this.state.offset ||
            (batch.events.length > 0 && batch.nextOffset === this.state.offset)) {
          throw new Error('Invalid donation feed cursor');
        }
        for (const event of batch.events) if (applyVipEvent(this.state, event, this.config.checkoutSecret)) granted++;
        this.state.offset = batch.nextOffset;
        this.database.setState('donation_feed', this.state);
        if (batch.events.length < 100) {
          this.lastSuccess = Date.now();
          return granted;
        }
      }
      // Never authorize removal while the backlog is still being caught up.
      return granted;
    } finally { this.running = false; }
  }
}

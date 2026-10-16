'use strict';

const { applyManualDonation, applyDonationRemoval } = require('./core');

// Feed imports and manual credits share one delivery queue. This avoids two
// workers publishing a pending thank-you or overwriting the topDirty flag.
class PaymentWorkflow {
  constructor({ store, importFeed, deliverPayments, deliverAdjustments = async () => [], refreshTop, syncSponsorRoles, logger = console }) {
    this.store = store;
    this.importFeed = importFeed;
    this.deliverPayments = deliverPayments;
    this.deliverAdjustments = deliverAdjustments;
    this.refreshTop = refreshTop;
    this.syncSponsorRoles = syncSponsorRoles;
    this.logger = logger;
    this.queue = Promise.resolve();
  }
  run(job) {
    const result = this.queue.then(job);
    this.queue = result.catch(() => {});
    return result;
  }
  async publishPending(guild) {
    const errors = await this.deliverPayments(guild);
    errors.push(...await this.deliverAdjustments(guild));
    if (this.store.data.topDirty) {
      try {
        await this.refreshTop(guild);
        await this.store.mutate(s => { s.topDirty = false; });
      } catch (error) {
        errors.push(`топ спонсоров: ${error.message}`);
        this.logger.error('Sponsor leaderboard pending:', error);
      }
    }
    return errors;
  }
  addManual(guild, input, now = Date.now()) {
    return this.run(async () => {
      const result = await this.store.mutate(s => applyManualDonation(s, input, now));
      // The durable credit is saved before attempting any Discord updates.
      const errors = await this.publishPending(guild);
      return { ...result, errors };
    });
  }
  removeManual(guild, input, now = Date.now()) {
    return this.run(async () => {
      const result = await this.store.mutate(s => applyDonationRemoval(s, input, now));
      const errors = await this.publishPending(guild);
      return { ...result, errors };
    });
  }
  check(guild) {
    return this.run(async () => {
      let imported = null, feedError = null;
      try { imported = await this.importFeed(); }
      catch (error) {
        feedError = error;
        this.logger.error('Donation feed unavailable; delivering saved payments:', error);
      }
      const errors = await this.publishPending(guild);
      try { await this.syncSponsorRoles(guild); }
      catch (error) { this.logger.error('Sponsor role sync pending:', error); }
      return { imported, feedError, errors };
    });
  }
}

module.exports = { PaymentWorkflow };

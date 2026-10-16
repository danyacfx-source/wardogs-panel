import { randomUUID } from 'node:crypto';

export class LeaseService {
  constructor({ database, logger, onLost }) {
    this.database = database;
    this.logger = logger;
    this.onLost = onLost;
    this.ownerId = randomUUID();
    this.ttlMs = 60_000;
    this.timer = null;
    this.held = false;
  }

  start() {
    this.held = this.database.acquireLease(this.ownerId, Date.now(), this.ttlMs);
    if (!this.held) throw new Error('Another bot instance currently owns the database lease');
    this.timer = setInterval(() => {
      try {
        this.held = this.database.renewLease(this.ownerId, Date.now(), this.ttlMs);
        if (!this.held) {
          this.logger.error('instance_lease_lost');
          clearInterval(this.timer);
          this.onLost?.();
        }
      } catch (error) {
        this.logger.error('instance_lease_renew_failed', { error });
        this.held = false;
        clearInterval(this.timer);
        this.onLost?.();
      }
    }, 20_000);
    this.timer.unref();
  }

  stop() {
    if (this.timer) clearInterval(this.timer);
    if (this.held) this.database.releaseLease(this.ownerId);
    this.held = false;
  }
}

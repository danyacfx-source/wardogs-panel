import { RconError } from '../rcon-client.js';
import { DomainError } from '../database.js';

const STATE_KEY = 'secondary_reserved_slots_v1';

function present(snapshot, steamId) {
  return snapshot.live?.includes(steamId) === true || snapshot.document?.includes(steamId) === true;
}

function active(snapshot, steamId) {
  return snapshot.live?.includes(steamId) === true;
}

// The first server retains its established ownership rules. This encrypted registry
// tracks the second server independently, so a manual slot on either server survives.
export class SecondaryReservedService {
  constructor({ database, rcon, primaryRcon = null, config, logger, lease, weeklyAwardService,
    reservedProtectionService, donations }) {
    this.database = database;
    this.rcon = rcon;
    this.primaryRcon = primaryRcon;
    this.config = config;
    this.logger = logger;
    this.lease = lease;
    this.weeklyAwardService = weeklyAwardService;
    this.reservedProtectionService = reservedProtectionService;
    this.donations = donations;
    this.timer = null;
    this.running = false;
    this.locks = new Map();
    this.lastWarningAt = 0;
    this.lastStatus = { state: 'starting', updatedAt: Date.now() };
  }

  start() {
    this.reconcile().catch((error) => this.logger.error('secondary_slots_initial_reconcile_failed', { error }));
    this.timer = setInterval(() => {
      this.reconcile().catch((error) => this.logger.error('secondary_slots_reconcile_failed', { error }));
    }, this.config.vip.reconcileSeconds * 1000);
    this.timer.unref();
  }

  stop() { if (this.timer) clearInterval(this.timer); }

  #claims() { return this.database.getState(STATE_KEY, {}); }
  #save(steamId, claim) {
    const claims = this.#claims();
    if (claim) claims[steamId] = claim;
    else delete claims[steamId];
    this.database.setState(STATE_KEY, claims);
  }

  #protected(user) {
    return this.reservedProtectionService?.isProtected(user.id, user.steamId) === true;
  }

  #entitled(user, now = Date.now()) {
    const member = this.database.getVipMembership(user.id);
    return this.#protected(user) || this.weeklyAwardService?.isActiveWinner(user.id) === true ||
      this.donations?.active(user.discordId, now) === true ||
      member?.state === 'pending_add' || member?.state === 'active' && member.activeUntil > now;
  }

  async #withLock(steamId, task) {
    const previous = this.locks.get(steamId) || Promise.resolve();
    const running = previous.catch(() => {}).then(task);
    this.locks.set(steamId, running);
    try { return await running; }
    finally { if (this.locks.get(steamId) === running) this.locks.delete(steamId); }
  }

  #warn(error) {
    const now = Date.now();
    if (now - this.lastWarningAt < this.config.rcon.outageLogIntervalSeconds * 1000) return;
    this.lastWarningAt = now;
    this.logger.warn(error instanceof RconError ? 'secondary_slot_rcon_unavailable' : 'secondary_slot_sync_failed', { error });
  }

  async reservedSnapshot() { return this.#verifiedSnapshot(await this.rcon.reservedSnapshot()); }

  #verifiedSnapshot(snapshot) {
    if (snapshot.live === null && snapshot.document === null) {
      throw new RconError('Second RCON cannot verify reserved slots', {
        code: 'reserved_unverifiable', route: 'GET /v1/reserved-slots'
      });
    }
    return snapshot;
  }

  async assertWritable() {
    const capabilities = await this.rcon.capabilities();
    const routes = capabilities.routes.map((route) => String(route)
      .replace(/\{[^}]+\}|:[^/\s]+/g, '*').trim());
    const live = routes.includes('POST /v1/reserved-slots') &&
      routes.includes('DELETE /v1/reserved-slots/*');
    if (!routes.includes('GET /v1/reserved-slots') || !live) {
      throw new DomainError('secondary_vip_unavailable',
        'Второй WDRCON не поддерживает мгновенную выдачу и снятие Reserved slots. Покупка отменена, баллы не списаны.');
    }
  }

  async ensureUser(user, { permanent = false } = {}) {
    if (!this.lease.held) return { active: false, status: 'waiting_lease' };
    return this.#withLock(user.steamId, async () => {
      const snapshot = this.#verifiedSnapshot(await this.rcon.reservedSnapshot());
      const exists = present(snapshot, user.steamId);
      const protectedSlot = permanent || this.#protected(user);
      const paidSeen = this.donations?.active(user.discordId) === true;
      let claim = this.#claims()[user.steamId];
      if (!claim) {
        claim = { userId: user.id, discordId: user.discordId, owner: protectedSlot || exists ? 'external' : 'bot',
          paidSeen, status: 'checking' };
      } else {
        claim = { ...claim, userId: user.id, discordId: user.discordId,
          owner: protectedSlot ? 'external' : !exists ? 'bot' : claim.owner,
          paidSeen: claim.paidSeen || paidSeen };
      }
      // Persist ownership before a write to recover safely after a crash.
      this.#save(user.steamId, claim);
      if (active(snapshot, user.steamId)) {
        this.#save(user.steamId, { ...claim, status: 'active' });
        return { active: true, status: 'active', owner: claim.owner };
      }
      if (!active(snapshot, user.steamId)) {
        const result = await this.rcon.setReserved(user.steamId, true);
        const status = result.active === true ? 'active' : 'waiting_rcon';
        this.#save(user.steamId, { ...claim, status });
        return { active: result.active === true, status, owner: claim.owner };
      }
      this.#save(user.steamId, { ...claim, status: 'active' });
      return { active: true, status: 'active', owner: claim.owner };
    });
  }

  #desired(now) {
    const desired = new Map();
    for (const entry of this.reservedProtectionService?.list() || []) {
      desired.set(entry.steamId, { user: { id: entry.userId, discordId: entry.discordId,
        steamId: entry.steamId }, permanent: true });
    }
    for (const winner of this.weeklyAwardService?.snapshot().winners || []) {
      if (winner.active === false) continue;
      if (this.database.getUserByDiscord(winner.discordId)?.id !== winner.userId) continue;
      if (!desired.has(winner.steamId)) desired.set(winner.steamId, { user: {
        id: winner.userId, discordId: winner.discordId, steamId: winner.steamId
      }, permanent: false });
    }
    for (const member of this.database.listVipMemberships()) {
      if (member.state !== 'pending_add' && !(member.state === 'active' && member.activeUntil > now)) continue;
      const user = this.database.getVipIdentityById(member.userId);
      if (user?.linked && !desired.has(user.steamId)) desired.set(user.steamId, { user, permanent: false });
    }
    for (const discordId of this.donations?.activeRecipientIds(now) || []) {
      const user = this.database.getVipIdentityByDiscord(discordId);
      if (user?.linked && !desired.has(user.steamId)) desired.set(user.steamId, { user, permanent: false });
    }
    return desired;
  }

  async #reconcileProtectedPrimary() {
    if (!this.primaryRcon) return { checked: 0, failed: 0 };
    let failed = 0;
    const entries = this.reservedProtectionService?.list() || [];
    for (const entry of entries) {
      try {
        const snapshot = await this.primaryRcon.reservedSnapshot();
        if (snapshot.live === null && snapshot.document === null) {
          throw new RconError('First RCON cannot verify protected slot', {
            code: 'reserved_unverifiable', route: 'GET /v1/reserved-slots'
          });
        }
        if (!active(snapshot, entry.steamId)) {
          await this.primaryRcon.setReserved(entry.steamId, true);
        }
      } catch (error) { failed++; this.#warn(error); }
    }
    return { checked: entries.length, failed };
  }

  async #release(steamId, claim, now) {
    return this.#withLock(steamId, async () => {
      // Re-read entitlement after awaits and before the destructive operation.
      const user = { id: claim.userId, discordId: claim.discordId, steamId };
      if (this.#entitled(user, now)) return;
      if (claim.owner === 'external') { this.#save(steamId, null); return; }
      if (this.donations && !this.donations.canRemove(now) || !this.donations?.enabled && claim.paidSeen) return;
      const snapshot = this.#verifiedSnapshot(await this.rcon.reservedSnapshot());
      if (this.#entitled(user, Date.now()) || this.donations && !this.donations.canRemove()) return;
      if (!present(snapshot, steamId)) { this.#save(steamId, null); return; }
      const result = await this.rcon.setReserved(steamId, false);
      if (result.active === false) {
        const after = this.#verifiedSnapshot(await this.rcon.reservedSnapshot());
        if (after.document?.includes(steamId)) {
          this.#save(steamId, { ...claim, status: 'legacy_config_pending' });
        } else this.#save(steamId, null);
      }
      else this.#save(steamId, { ...claim, status: 'pending_remove' });
    });
  }

  async reconcile(now = Date.now()) {
    if (this.running || !this.lease.held) return { skipped: true };
    this.running = true;
    let checked = 0;
    let failed = 0;
    try {
      const primary = await this.#reconcileProtectedPrimary();
      checked += primary.checked;
      failed += primary.failed;
      const desired = this.#desired(now);
      for (const { user, permanent } of desired.values()) {
        checked++;
        try { await this.ensureUser(user, { permanent }); }
        catch (error) { failed++; this.#warn(error); }
      }
      for (const [steamId, claim] of Object.entries(this.#claims())) {
        if (desired.has(steamId)) continue;
        checked++;
        try { await this.#release(steamId, claim, now); }
        catch (error) { failed++; this.#warn(error); }
      }
      this.lastStatus = { state: failed ? 'degraded' : 'active', checked, failed, updatedAt: now };
      return { skipped: false, checked, failed };
    } finally { this.running = false; }
  }

  status(steamId) { return this.#claims()[steamId]?.status || 'not_tracked'; }
}

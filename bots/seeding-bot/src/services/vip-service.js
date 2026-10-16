import { DomainError } from '../database.js';
import { RconError } from '../rcon-client.js';

export function vipCatalog(config) {
  return Object.freeze({
    '1d': { sku: '1d', label: 'VIP на 1 день', cost: config.vip.oneDayCost, durationSeconds: 86_400 },
    '7d': { sku: '7d', label: 'VIP на 7 дней', cost: config.vip.sevenDayCost, durationSeconds: 604_800 },
    '30d': { sku: '30d', label: 'VIP на 30 дней', cost: config.vip.thirtyDayCost, durationSeconds: 2_592_000 }
  });
}

export class VipService {
  constructor({
    database, rcon, config, logger, lease, roleService = null,
    weeklyAwardService = null, reservedProtectionService = null, donations = null,
    secondaryReservedService = null
  }) {
    this.database = database;
    this.rcon = rcon;
    this.config = config;
    this.logger = logger;
    this.lease = lease;
    this.roleService = roleService;
    this.weeklyAwardService = weeklyAwardService;
    this.reservedProtectionService = reservedProtectionService;
    this.donations = donations;
    this.secondaryReservedService = secondaryReservedService;
    this.catalog = vipCatalog(config);
    this.timer = null;
    this.running = false;
    this.userLocks = new Map();
    this.lastRconWarningAt = 0;
  }

  start() {
    this.reconcile().catch((error) => this.logger.error('vip_initial_reconcile_failed', { error }));
    this.timer = setInterval(() => {
      this.reconcile().catch((error) => this.logger.error('vip_reconcile_unhandled', { error }));
    }, this.config.vip.reconcileSeconds * 1000);
    this.timer.unref();
  }

  stop() {
    if (this.timer) clearInterval(this.timer);
  }

  #claims() { return this.database.getState('paid_slot_claims', {}); }
  #claim(steamId) { return this.#claims()[steamId] || null; }
  #saveClaim(user, owner) {
    const claims = this.#claims();
    if (claims[user.steamId]?.discordId === user.discordId && claims[user.steamId]?.owner === owner) return;
    claims[user.steamId] = { discordId: user.discordId, owner };
    this.database.setState('paid_slot_claims', claims);
  }
  #forgetClaim(steamId) {
    const claims = this.#claims();
    if (!claims[steamId]) return;
    delete claims[steamId];
    this.database.setState('paid_slot_claims', claims);
  }

  async purchase({ discordId, sku, idempotencyKey }) {
    const product = this.catalog[sku];
    if (!product) throw new DomainError('bad_product', 'Неизвестный вариант VIP.');
    const user = this.database.getUserByDiscord(discordId);
    if (!user) throw new DomainError('not_linked', 'Сначала привяжите Steam через /seed link.');
    const existing = this.database.getVipMembership(user.id);
    if (this.#isProtected(user)) {
      throw new DomainError(
        'protected_reserved',
        'За вами закреплён постоянный Reserved slot. Покупка VIP отменена, баллы не списаны.'
      );
    }
    try {
      await Promise.all([
        this.rcon.assertLiveReservedWritable(),
        this.secondaryReservedService?.assertWritable()
      ]);
    } catch (error) {
      if (error instanceof RconError && error.code === 'live_reserved_unavailable') {
        throw new DomainError('live_reserved_unavailable',
          'WDRCON должен поддерживать мгновенную выдачу и снятие слотов на обоих серверах. Покупка отменена, баллы не списаны.');
      }
      throw error;
    }
    if (!existing || existing.state === 'inactive') {
      const snapshots = await Promise.all([
        this.rcon.reservedSnapshot(),
        this.secondaryReservedService?.reservedSnapshot()
      ]);
      const alreadyReserved = snapshots.some((snapshot) => snapshot &&
        (snapshot.live?.includes(user.steamId) || snapshot.document?.includes(user.steamId)));
      const weeklyRewardSlot = this.weeklyAwardService?.hasManagedSlot(user.id) === true;
      const paidOwnedSlot = this.donations?.active(discordId) &&
        this.#claim(user.steamId)?.owner === 'bot';
      if (alreadyReserved && !weeklyRewardSlot && !paidOwnedSlot) {
        throw new DomainError(
          'already_reserved',
          'У вас уже есть reserved slot вне магазина на одном из серверов. Покупка отменена, баллы не списаны; обратитесь к администратору.'
        );
      }
    }
    const order = this.database.purchaseVip({
      discordId,
      sku,
      cost: product.cost,
      durationSeconds: product.durationSeconds,
      idempotencyKey
    });
    const roleSync = await this.roleService?.setVip(user.discordId, true);
    await this.reconcileUser(order.userId);
    return { order, product, membership: this.database.getVipMembership(order.userId), roleSync };
  }

  async reconcile() {
    if (this.running || !this.lease.held) return { skipped: true, checked: 0, failed: 0 };
    this.running = true;
    let checked = 0;
    let failed = 0;
    try {
      const ids = new Set(this.database.listVipMemberships()
        .filter((member) => member.state !== 'inactive').map((member) => member.userId));
      for (const discordId of this.donations?.activeRecipientIds() || []) {
        const user = this.database.getVipIdentityByDiscord(discordId);
        if (user) ids.add(user.id);
      }
      for (const claim of Object.values(this.#claims())) {
        const user = this.database.getVipIdentityByDiscord(claim.discordId);
        if (user) ids.add(user.id);
      }
      for (const userId of ids) {
        checked += 1;
        const result = await this.reconcileUser(userId);
        if (result?.ok === false) failed += 1;
      }
      const secondary = await this.secondaryReservedService?.reconcile();
      if (secondary && !secondary.skipped) {
        checked += secondary.checked;
        failed += secondary.failed;
      }
      return { skipped: false, checked, failed };
    } finally {
      this.running = false;
    }
  }

  async reconcileUser(userId) {
    if (this.userLocks.has(userId)) return this.userLocks.get(userId);
    const task = this.#reconcileUser(userId).finally(() => this.userLocks.delete(userId));
    this.userLocks.set(userId, task);
    return task;
  }

  async #reconcileUser(userId) {
    if (!this.lease.held) return { ok: false, errorCode: 'lease_not_held' };
    let member = this.database.getVipMembership(userId);
    const user = this.database.getVipIdentityById(userId);
    if (!user?.linked) return { ok: true };
    const now = Date.now();
    const paid = this.donations?.active(user.discordId, now) || false;
    if ((!member || member.state === 'inactive') && !paid && !this.#claim(user.steamId)) {
      return { ok: true };
    }
    if (member) await this.roleService?.setVip(user.discordId, member.state !== 'inactive');
    try {
      if (paid) await this.#syncPaidSlot(user, member);
      else await this.#cleanupPaidClaim(user, member, now);

      if (member?.state === 'pending_add') {
        const secondary = await this.secondaryReservedService?.ensureUser(user);
        const secondaryActive = !this.secondaryReservedService || secondary.active === true;
        const snapshot = await this.rcon.reservedSnapshot();
        const livePresent = snapshot.live?.includes(user.steamId) === true;
        const documentPresent = snapshot.document?.includes(user.steamId) === true;
        if (member.managedByBot === null) {
          const preexisting = livePresent || documentPresent;
          const ownedWeeklyReward = this.weeklyAwardService?.hasManagedSlot(userId) === true;
          const ownedPaidSlot = this.#claim(user.steamId)?.owner === 'bot';
          this.database.setVipOwnership(
            userId,
            !this.#isProtected(user) && (!preexisting || ownedWeeklyReward || ownedPaidSlot),
            now
          );
          member = this.database.getVipMembership(userId);
        }
        if (member.managedByBot === false) {
          if (livePresent && secondaryActive) this.database.activateVip(userId, now);
          else this.database.setVipError(userId, 'Ожидание Reserved slot на обоих серверах.', now);
          return { ok: true };
        }
        if (livePresent) {
          if (secondaryActive) this.database.activateVip(userId, now);
          else this.database.setVipError(userId, 'Ожидание Reserved slot на втором сервере.', now);
          return { ok: true };
        }
        const result = await this.rcon.setReserved(user.steamId, true);
        if (paid) this.#saveClaim(user, 'bot');
        if (result.active === true && secondaryActive) this.database.activateVip(userId, now);
        else this.database.setVipError(userId, 'Ожидание Reserved slot на обоих серверах.', now);
        return { ok: true };
      }

      if (member?.state === 'active' && member.activeUntil <= now) {
        if (this.#isProtected(user)) {
          this.#forgetClaim(user.steamId);
          this.database.setVipState(userId, 'inactive', { now });
          await this.roleService?.setVip(user.discordId, false);
          return { ok: true };
        }
        if (this.weeklyAwardService?.isActiveWinner(userId)) {
          if (member.managedByBot !== false) this.weeklyAwardService.adoptManagedSlot(userId);
          this.#forgetClaim(user.steamId);
          this.database.setVipState(userId, 'inactive', { now });
          await this.roleService?.setVip(user.discordId, false);
          return { ok: true };
        }
        if (paid) {
          if (member.managedByBot !== false) this.#saveClaim(user, 'bot');
          this.database.setVipState(userId, 'inactive', { now });
          await this.roleService?.setVip(user.discordId, false);
          return { ok: true };
        }
        if (member.managedByBot === false) {
          this.database.setVipState(userId, 'inactive', { now });
          await this.roleService?.setVip(user.discordId, false);
          return { ok: true };
        }
        this.database.setVipState(userId, 'pending_remove', { now });
        member = this.database.getVipMembership(userId);
      }

      if (member?.state === 'pending_remove') {
        if (this.#isProtected(user)) {
          this.#forgetClaim(user.steamId);
          this.database.setVipState(userId, 'inactive', { now });
          await this.roleService?.setVip(user.discordId, false);
          return { ok: true };
        }
        if (this.weeklyAwardService?.isActiveWinner(userId)) {
          if (member.managedByBot !== false) this.weeklyAwardService.adoptManagedSlot(userId);
          this.#forgetClaim(user.steamId);
          this.database.setVipState(userId, 'inactive', { now });
          await this.roleService?.setVip(user.discordId, false);
          return { ok: true };
        }
        if (paid) {
          if (member.managedByBot !== false) this.#saveClaim(user, 'bot');
          this.database.setVipState(userId, 'inactive', { now });
          await this.roleService?.setVip(user.discordId, false);
          return { ok: true };
        }
        if (member.managedByBot === false && this.#claim(user.steamId)?.owner !== 'bot') {
          this.database.setVipState(userId, 'inactive', { now });
          await this.roleService?.setVip(user.discordId, false);
          return { ok: true };
        }
        if (this.donations?.enabled && !this.donations.canRemove(now) ||
            !this.donations?.enabled && this.#claim(user.steamId)) {
          this.database.setVipError(userId, 'Ожидается связь с журналом оплаченных VIP.', now);
          return { ok: true };
        }
        // Last fail-safe check before a destructive RCON operation in VIP cleanup.
        if (this.#isProtected(user)) {
          this.database.setVipState(userId, 'inactive', { now });
          await this.roleService?.setVip(user.discordId, false);
          return { ok: true };
        }
        if (this.donations?.active(user.discordId, now) ||
            this.weeklyAwardService?.isActiveWinner(userId)) return { ok: true };
        const result = await this.rcon.setReserved(user.steamId, false);
        if (result.active === false) {
          const after = await this.rcon.reservedSnapshot();
          if (after.document?.includes(user.steamId)) this.#saveClaim(user, 'bot');
          else this.#forgetClaim(user.steamId);
          this.database.setVipState(userId, 'inactive', { now });
          await this.roleService?.setVip(user.discordId, false);
        } else this.database.setVipError(userId, 'Ожидание снятия Reserved slot в игровом сервере.', now);
      }
      return { ok: true };
    } catch (error) {
      const code = typeof error?.code === 'string' ? error.code : 'rcon_unavailable';
      if (member && member.state !== 'inactive') this.database.setVipError(userId, `RCON: ${code}`, now);
      if (error instanceof RconError) {
        const intervalMs = this.config.rcon.outageLogIntervalSeconds * 1000;
        if (now - this.lastRconWarningAt >= intervalMs) {
          this.lastRconWarningAt = now;
          this.logger.warn('vip_rcon_unavailable', { error });
        }
      } else {
        this.logger.warn('vip_user_reconcile_failed', { error, userId });
      }
      return { ok: false, errorCode: code };
    }
  }

  async #syncPaidSlot(user, member) {
    const snapshot = await this.rcon.reservedSnapshot();
    const present = snapshot.live?.includes(user.steamId) === true;
    const documented = snapshot.document?.includes(user.steamId) === true;
    const protectedSlot = this.#isProtected(user);
    const claim = this.#claim(user.steamId);
    if (protectedSlot) this.#saveClaim(user, 'external');
    else if (!claim) {
      const botOwned = !(present || documented) || member?.managedByBot === true ||
        this.weeklyAwardService?.hasManagedSlot(user.id) === true;
      this.#saveClaim(user, botOwned ? 'bot' : 'external');
    }
    if (!present) {
      // Claim ownership before writing to RCON, so a restart can recover it.
      if (!protectedSlot) this.#saveClaim(user, 'bot');
      await this.rcon.setReserved(user.steamId, true);
    }
  }

  async #cleanupPaidClaim(user, member, now) {
    const claim = this.#claim(user.steamId);
    if (!claim) return;
    if (claim.owner === 'external' || this.#isProtected(user)) {
      this.#forgetClaim(user.steamId);
      return;
    }
    if (this.weeklyAwardService?.isActiveWinner(user.id)) {
      this.weeklyAwardService.adoptManagedSlot(user.id);
      this.#forgetClaim(user.steamId);
      return;
    }
    if (member?.state === 'pending_add' ||
        member?.state === 'active' && member.activeUntil > now) {
      if (member.managedByBot !== true) this.database.setVipOwnership(user.id, true, now);
      this.#forgetClaim(user.steamId);
      return;
    }
    // The seeding VIP's own expiration pass is responsible for the shared slot.
    if (member?.state === 'active' || member?.state === 'pending_remove') return;
    if (!this.donations?.enabled || !this.donations.canRemove(now) ||
        this.donations.active(user.discordId, now) || this.#isProtected(user)) return;
    const result = await this.rcon.setReserved(user.steamId, false);
    if (result.active === false) {
      const after = await this.rcon.reservedSnapshot();
      if (!after.document?.includes(user.steamId)) this.#forgetClaim(user.steamId);
    }
  }

  #isProtected(user) {
    return this.reservedProtectionService?.isProtected(user.id, user.steamId) === true;
  }
}

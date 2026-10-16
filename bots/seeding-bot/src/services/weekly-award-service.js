import { previousWeekKey } from '../time.js';
import { RconError } from '../rcon-client.js';

const STATE_KEY = 'weekly_super_seeders_v1';

function emptyState() {
  return { version: 1, awardWeek: '', awardedAt: null, winners: [], cleanup: [] };
}

function slotPresent(snapshot, steamId) {
  return snapshot.live?.includes(steamId) === true || snapshot.document?.includes(steamId) === true;
}

function slotStatus(snapshot, steamId) {
  if (snapshot.live?.includes(steamId) === true) return 'active';
  if (snapshot.document?.includes(steamId) === true) return 'pending_restart';
  return 'missing';
}

export class WeeklyAwardService {
  constructor({ database, rcon, roleService, config, logger, lease,
    reservedProtectionService = null, donations = null, secondaryReservedService = null }) {
    this.database = database;
    this.rcon = rcon;
    this.roleService = roleService;
    this.config = config;
    this.logger = logger;
    this.lease = lease;
    this.reservedProtectionService = reservedProtectionService;
    this.donations = donations;
    this.secondaryReservedService = secondaryReservedService;
    this.timer = null;
    this.running = false;
    this.lastRconWarningAt = 0;
  }

  start({ reconcileImmediately = true } = {}) {
    if (reconcileImmediately) {
      this.reconcile().catch((error) => this.logger.error('weekly_award_initial_reconcile_failed', { error }));
    }
    this.timer = setInterval(() => {
      this.reconcile().catch((error) => this.logger.error('weekly_award_reconcile_failed', { error }));
    }, this.config.seed.weeklyAwardReconcileSeconds * 1000);
    this.timer.unref();
  }

  stop() {
    if (this.timer) clearInterval(this.timer);
  }

  snapshot() {
    const state = this.database.getState(STATE_KEY, emptyState());
    return {
      ...emptyState(),
      ...state,
      winners: Array.isArray(state?.winners) ? state.winners : [],
      cleanup: Array.isArray(state?.cleanup) ? state.cleanup : []
    };
  }

  hasManagedSlot(userId) {
    const state = this.snapshot();
    return state.winners.some((winner) => (
      winner.userId === userId && winner.slotOwnership === 'award' && !this.#isProtected(winner)
    )) || state.cleanup.some((entry) => (
      entry.userId === userId && entry.slotOwnership === 'award' && !this.#isProtected(entry)
    ));
  }

  isActiveWinner(userId) {
    return this.snapshot().winners.some((winner) => winner.userId === userId && winner.active !== false);
  }

  adoptManagedSlot(userId) {
    const state = this.snapshot();
    const winner = state.winners.find((entry) => entry.userId === userId && entry.active !== false);
    if (!winner || this.#isProtected(winner)) return false;
    winner.slotOwnership = 'award';
    winner.slotStatus = winner.slotStatus === 'external' ? 'active' : winner.slotStatus;
    this.database.setState(STATE_KEY, state);
    return true;
  }

  markProtected(userId, steamId) {
    const state = this.snapshot();
    let changed = false;
    const winner = state.winners.find((entry) => entry.userId === userId || entry.steamId === steamId);
    if (winner && winner.slotOwnership !== 'external') {
      winner.slotOwnership = 'external';
      changed = true;
    }
    if (winner && winner.slotProtected !== true) {
      winner.slotProtected = true;
      changed = true;
    }
    const cleanupLength = state.cleanup.length;
    state.cleanup = state.cleanup.filter((entry) => entry.userId !== userId && entry.steamId !== steamId);
    if (state.cleanup.length !== cleanupLength) changed = true;
    if (changed) this.database.setState(STATE_KEY, state);
    return changed;
  }

  markUnprotected(userId, steamId) {
    const state = this.snapshot();
    const winner = state.winners.find((entry) => entry.userId === userId || entry.steamId === steamId);
    if (!winner || winner.slotProtected !== true) return false;
    winner.slotProtected = false;
    this.database.setState(STATE_KEY, state);
    return true;
  }

  async handleUnlink(user) {
    const state = this.snapshot();
    const winner = state.winners.find((entry) => entry.userId === user.id);
    if (!winner) return { wasWinner: false };
    winner.active = false;
    winner.roleStatus = 'revoked';
    winner.slotStatus = 'revoked';
    if (winner.slotOwnership === 'award' && !this.#isProtected(winner)) {
      this.#addCleanup(state, winner, 'identity_unlinked');
    } else if (this.#isProtected(winner)) {
      winner.slotOwnership = 'external';
    }
    this.database.setState(STATE_KEY, state);
    await this.roleService.setSuperSeeder(user.discordId, false);
    await this.#reconcileCleanup();
    return { wasWinner: true };
  }

  async reconcile(now = Date.now()) {
    if (this.running || !this.lease.held) return { skipped: true };
    this.running = true;
    try {
      const targetWeek = previousWeekKey(now);
      let state = this.snapshot();
      if (state.awardWeek !== targetWeek) {
        state = await this.#rotate(state, targetWeek, now);
      }
      await this.#reconcileWinners();
      await this.#reconcileCleanup();
      await this.secondaryReservedService?.reconcile(now);
      if (this.secondaryReservedService) {
        for (const winner of this.snapshot().winners) {
          this.#patchWinner(winner.userId, {
            secondarySlotStatus: this.secondaryReservedService.status(winner.steamId)
          });
        }
      }
      const current = this.snapshot();
      return { skipped: false, awardWeek: current.awardWeek, winners: current.winners.length };
    } finally {
      this.running = false;
    }
  }

  async #rotate(previous, targetWeek, now) {
    const rows = this.database.getLeaderboard(targetWeek).slice(0, 3);
    const priorByUser = new Map(previous.winners.map((winner) => [winner.userId, winner]));
    const selected = new Set(rows.map((row) => row.id));
    const cleanup = previous.cleanup.filter((entry) => !selected.has(entry.userId));

    for (const oldWinner of previous.winners) {
      if (selected.has(oldWinner.userId)) continue;
      await this.roleService.setSuperSeeder(oldWinner.discordId, false);
      if (oldWinner.slotOwnership === 'award' && !this.#isProtected(oldWinner)) {
        this.#addCleanup({ cleanup }, oldWinner, 'ranking_rotated');
      }
    }

    const winners = rows.map((row, index) => {
      const prior = priorByUser.get(row.id);
      return {
        place: index + 1,
        userId: row.id,
        discordId: row.discordId,
        steamId: row.steamId,
        displayName: row.displayName,
        weeklySeconds: row.weeklySeconds,
        weeklyPoints: row.weeklyPoints,
        active: true,
        roleStatus: prior?.roleStatus || 'checking',
        slotOwnership: prior?.slotOwnership || 'unknown',
        slotStatus: prior?.slotStatus || 'checking',
        slotProtected: this.#isProtected(row)
      };
    });
    const state = { version: 1, awardWeek: targetWeek, awardedAt: now, winners, cleanup };
    this.database.setState(STATE_KEY, state);
    this.database.audit('weekly_super_seeders_rotated', '', { weekKey: targetWeek, winnerCount: winners.length });
    return state;
  }

  async #reconcileWinners() {
    let state = this.snapshot();
    for (const winner of state.winners) {
      const linked = this.database.getUserByDiscord(winner.discordId)?.id === winner.userId;
      const role = await this.roleService.setSuperSeeder(winner.discordId, linked);
      this.#patchWinner(winner.userId, {
        active: linked,
        roleStatus: linked ? (role.ok ? 'active' : 'waiting_discord') : 'revoked'
      });
    }

    state = this.snapshot();
    const active = state.winners.filter((winner) => winner.active !== false);
    if (!active.length) return;

    let snapshot;
    try {
      snapshot = await this.rcon.reservedSnapshot();
    } catch (error) {
      for (const winner of active) this.#patchWinner(winner.userId, { slotStatus: 'waiting_rcon' });
      this.#logRconWarning(error);
      return;
    }

    for (const current of active) {
      let winner = this.snapshot().winners.find((entry) => entry.userId === current.userId);
      if (!winner) continue;
      const present = slotPresent(snapshot, winner.steamId);
      const protectedSlot = this.#isProtected(winner);
      if (winner.slotProtected !== protectedSlot) {
        this.#patchWinner(winner.userId, { slotProtected: protectedSlot });
      }
      if (protectedSlot) {
        this.#patchWinner(winner.userId, {
          slotOwnership: 'external',
          slotStatus: present ? slotStatus(snapshot, winner.steamId) : 'applying'
        });
        if (snapshot.live?.includes(winner.steamId)) continue;
        try {
          const result = await this.rcon.setReserved(winner.steamId, true);
          this.#patchWinner(winner.userId, { slotStatus: result.active === true ? 'active' : 'waiting_rcon' });
          this.#rememberAddedSlot(snapshot, winner.steamId, result.active === true);
        } catch (error) {
          this.#patchWinner(winner.userId, { slotStatus: 'waiting_rcon' });
          this.#logRconWarning(error);
        }
        continue;
      }
      if (winner.slotOwnership === 'unknown' && present) {
        this.#patchWinner(winner.userId, { slotOwnership: 'external', slotStatus: slotStatus(snapshot, winner.steamId) });
        if (snapshot.live?.includes(winner.steamId)) continue;
      }
      if (winner.slotOwnership === 'external' && present) {
        this.#patchWinner(winner.userId, { slotStatus: slotStatus(snapshot, winner.steamId) });
        if (snapshot.live?.includes(winner.steamId)) continue;
      }
      if (winner.slotOwnership === 'award' && present) {
        this.#patchWinner(winner.userId, { slotStatus: slotStatus(snapshot, winner.steamId) });
        if (snapshot.live?.includes(winner.steamId)) continue;
      }

      // Ownership is recorded before the external write, so a crash cannot leave an untracked slot.
      if (!present) this.#patchWinner(winner.userId, { slotOwnership: 'award', slotStatus: 'applying' });
      try {
        const result = await this.rcon.setReserved(winner.steamId, true);
        const status = result.active === true ? 'active' : 'waiting_rcon';
        this.#patchWinner(winner.userId, { slotStatus: status });
        this.#rememberAddedSlot(snapshot, winner.steamId, result.active === true);
      } catch (error) {
        this.#patchWinner(winner.userId, { slotStatus: 'waiting_rcon' });
        this.#logRconWarning(error);
      }
    }
  }

  async #reconcileCleanup() {
    const state = this.snapshot();
    if (!state.cleanup.length) return;
    let snapshot;
    try {
      snapshot = await this.rcon.reservedSnapshot();
    } catch (error) {
      this.#logRconWarning(error);
      return;
    }

    for (const entry of this.snapshot().cleanup) {
      if (this.#isProtected(entry)) {
        this.#removeCleanup(entry.userId, entry.steamId);
        continue;
      }
      if (this.isActiveWinner(entry.userId)) {
        this.#removeCleanup(entry.userId, entry.steamId);
        continue;
      }
      if (this.donations?.active(entry.discordId)) {
        // The award created this slot; hand ownership to paid VIP before dropping
        // the weekly cleanup record so paid expiry can remove it safely.
        const claims = this.database.getState('paid_slot_claims', {});
        claims[entry.steamId] = { discordId: entry.discordId, owner: 'bot' };
        this.database.setState('paid_slot_claims', claims);
        this.#removeCleanup(entry.userId, entry.steamId);
        continue;
      }
      if (this.donations && (!this.donations.canRemove() ||
          !this.donations.enabled &&
            this.database.getState('paid_slot_claims', {})[entry.steamId])) {
        this.#patchCleanup(entry.userId, entry.steamId, { status: 'waiting_payment_feed' });
        continue;
      }
      const membership = this.database.getVipMembership(entry.userId);
      if (membership && membership.state !== 'inactive') {
        this.#patchCleanup(entry.userId, entry.steamId, { status: 'waiting_vip' });
        continue;
      }
      if (!slotPresent(snapshot, entry.steamId)) {
        this.#removeCleanup(entry.userId, entry.steamId);
        continue;
      }
      try {
        // Re-check immediately before the destructive RCON call in case an admin
        // protected this slot while the cleanup pass was awaiting the snapshot.
        if (this.#isProtected(entry)) {
          this.#removeCleanup(entry.userId, entry.steamId);
          continue;
        }
        if (this.donations?.active(entry.discordId) ||
            this.donations && (!this.donations.canRemove() ||
              !this.donations.enabled &&
                this.database.getState('paid_slot_claims', {})[entry.steamId])) {
          this.#patchCleanup(entry.userId, entry.steamId, { status: 'waiting_paid_vip' });
          continue;
        }
        const result = await this.rcon.setReserved(entry.steamId, false);
        if (result.active === false) {
          const after = await this.rcon.reservedSnapshot();
          if (after.document?.includes(entry.steamId)) {
            this.#patchCleanup(entry.userId, entry.steamId, { status: 'legacy_config_pending' });
          } else this.#removeCleanup(entry.userId, entry.steamId);
        } else {
          this.#patchCleanup(entry.userId, entry.steamId, { status: 'waiting_rcon' });
        }
      } catch (error) {
        this.#patchCleanup(entry.userId, entry.steamId, { status: 'waiting_rcon' });
        this.#logRconWarning(error);
      }
    }
  }

  #addCleanup(state, winner, reason) {
    state.cleanup ||= [];
    if (this.#isProtected(winner)) return;
    if (state.cleanup.some((entry) => entry.userId === winner.userId && entry.steamId === winner.steamId)) return;
    state.cleanup.push({
      userId: winner.userId,
      discordId: winner.discordId,
      steamId: winner.steamId,
      slotOwnership: 'award',
      status: 'pending_remove',
      reason,
      queuedAt: Date.now()
    });
  }

  #patchWinner(userId, values) {
    const state = this.snapshot();
    const winner = state.winners.find((entry) => entry.userId === userId);
    if (!winner) return;
    Object.assign(winner, values);
    this.database.setState(STATE_KEY, state);
  }

  #patchCleanup(userId, steamId, values) {
    const state = this.snapshot();
    const entry = state.cleanup.find((item) => item.userId === userId && item.steamId === steamId);
    if (!entry) return;
    Object.assign(entry, values);
    this.database.setState(STATE_KEY, state);
  }

  #removeCleanup(userId, steamId) {
    const state = this.snapshot();
    state.cleanup = state.cleanup.filter((entry) => entry.userId !== userId || entry.steamId !== steamId);
    this.database.setState(STATE_KEY, state);
  }

  #isProtected(entry) {
    return this.reservedProtectionService?.isProtected(entry.userId, entry.steamId) === true;
  }

  #rememberAddedSlot(snapshot, steamId, active) {
    if (snapshot.live) {
      const live = new Set(snapshot.live);
      if (active) live.add(steamId);
      snapshot.live = [...live];
    }
    if (snapshot.document) {
      const document = new Set(snapshot.document);
      document.add(steamId);
      snapshot.document = [...document];
    }
  }

  #logRconWarning(error) {
    const now = Date.now();
    const interval = this.config.rcon.outageLogIntervalSeconds * 1000;
    if (now - this.lastRconWarningAt < interval) return;
    this.lastRconWarningAt = now;
    if (error instanceof RconError) this.logger.warn('weekly_award_rcon_unavailable', { error });
    else this.logger.warn('weekly_award_slot_sync_failed', { error });
  }
}

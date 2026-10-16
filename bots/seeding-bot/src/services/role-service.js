const QUEUE_STATE_KEY = 'discord_role_sync_queue_v1';

function isUnknownMember(error) {
  return Number(error?.code) === 10007 || Number(error?.rawError?.code) === 10007;
}

export class RoleService {
  constructor({ database, discord, config, logger, lease, donations = null }) {
    this.database = database;
    this.discord = discord;
    this.config = config;
    this.logger = logger;
    this.lease = lease;
    this.donations = donations;
    this.timer = null;
    this.running = false;
    this.queueMutation = Promise.resolve();
  }

  start({ reconcileImmediately = true } = {}) {
    if (reconcileImmediately) {
      this.reconcile().catch((error) => this.logger.error('discord_role_initial_reconcile_failed', { error }));
    }
    this.timer = setInterval(() => {
      this.reconcile().catch((error) => this.logger.error('discord_role_reconcile_failed', { error }));
    }, this.config.discord.roleReconcileSeconds * 1000);
    this.timer.unref();
  }

  stop() {
    if (this.timer) clearInterval(this.timer);
  }

  setSeeder(discordId, present) {
    return this.#setRole(discordId, this.config.discord.seederRoleId, present, 'seeder');
  }

  setSuperSeeder(discordId, present) {
    return this.#setRole(discordId, this.config.discord.superSeederRoleId, present, 'super_seeder');
  }

  setVip(discordId, present) {
    const paid = this.donations?.active(discordId) === true;
    // Keep the existing role while the paid feed is unavailable: an extension
    // could have occurred since the last successful poll.
    if (!present && !paid && this.donations?.enabled &&
        this.donations.recipientIds().includes(discordId) && !this.donations.canRemove()) {
      return Promise.resolve({ ok: true, changed: false, queued: false, deferred: true });
    }
    return this.#setRole(discordId, this.config.discord.vipRoleId, Boolean(present || paid), 'vip');
  }

  pendingCount() {
    return this.#queue().length;
  }

  async reconcile() {
    if (this.running || !this.lease.held) return { skipped: true, checked: 0, pending: this.pendingCount() };
    this.running = true;
    let checked = 0;
    try {
      for (const action of this.#queue()) {
        checked += 1;
        if (action.roleId === this.config.discord.vipRoleId) {
          const user = this.database.getUserByDiscord(action.discordId);
          const member = user && this.database.getVipMembership(user.id);
          await this.setVip(action.discordId, Boolean(member && member.state !== 'inactive'));
        } else {
          await this.#setRole(action.discordId, action.roleId, action.present, action.kind, { queueOnFailure: true });
        }
      }

      const seeders = this.database.listUsers();
      const seederIds = new Set(seeders.map(user => user.discordId));
      for (const user of seeders) {
        checked += 1;
        await this.setSeeder(user.discordId, true);
        const membership = this.database.getVipMembership(user.id);
        await this.setVip(user.discordId, Boolean(membership && membership.state !== 'inactive'));
      }
      for (const discordId of this.donations?.recipientIds() || []) {
        if (seederIds.has(discordId)) continue;
        checked += 1;
        await this.setVip(discordId, false);
      }
      return { skipped: false, checked, pending: this.pendingCount() };
    } finally {
      this.running = false;
    }
  }

  async #setRole(discordId, roleId, present, kind, { queueOnFailure = true } = {}) {
    try {
      const guild = this.discord.guilds.cache.get(this.config.discord.guildId);
      if (!guild) throw new Error('Configured Discord server is unavailable');
      const member = guild.members.cache?.get(discordId) || await guild.members.fetch(discordId);
      const hasRole = member.roles.cache.has(roleId);
      let changed = false;
      if (present && !hasRole) {
        await member.roles.add(roleId, `WARDOGS seeding: ${kind}`);
        changed = true;
      } else if (!present && hasRole) {
        await member.roles.remove(roleId, `WARDOGS seeding: ${kind}`);
        changed = true;
      }
      await this.#removeQueued(discordId, roleId);
      if (changed) this.database.audit('discord_role_changed', '', { kind, present });
      return { ok: true, changed, queued: false };
    } catch (error) {
      if (!present && isUnknownMember(error)) {
        await this.#removeQueued(discordId, roleId);
        return { ok: true, changed: false, queued: false };
      }
      if (queueOnFailure) await this.#queueAction({ discordId, roleId, present, kind });
      this.logger.warn('discord_role_sync_deferred', { error, kind, present });
      return { ok: false, changed: false, queued: queueOnFailure };
    }
  }

  #queue() {
    const state = this.database.getState(QUEUE_STATE_KEY, { actions: [] });
    return Array.isArray(state?.actions) ? state.actions.filter((action) =>
      typeof action?.discordId === 'string'
      && typeof action?.roleId === 'string'
      && typeof action?.present === 'boolean'
    ) : [];
  }

  #mutateQueue(mutator) {
    const task = this.queueMutation.then(() => {
      const actions = this.#queue();
      const next = mutator(actions) || actions;
      this.database.setState(QUEUE_STATE_KEY, { actions: next.slice(-500) });
    });
    this.queueMutation = task.catch(() => {});
    return task;
  }

  #queueAction(action) {
    return this.#mutateQueue((actions) => {
      const filtered = actions.filter((queued) =>
        queued.discordId !== action.discordId || queued.roleId !== action.roleId
      );
      filtered.push({ ...action, queuedAt: Date.now() });
      return filtered;
    });
  }

  #removeQueued(discordId, roleId) {
    return this.#mutateQueue((actions) => actions.filter((action) =>
      action.discordId !== discordId || action.roleId !== roleId
    ));
  }
}

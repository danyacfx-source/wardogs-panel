const STATE_KEY = 'protected_reserved_slots_v1';

function emptyState() {
  return { version: 1, entries: [] };
}

function validateEntry(entry) {
  if (!entry || typeof entry !== 'object') throw new Error('Invalid protected reserved-slot record');
  if (typeof entry.userId !== 'string' || !entry.userId || entry.userId.length > 100) {
    throw new Error('Invalid protected reserved-slot user id');
  }
  if (!/^\d{17,20}$/.test(entry.discordId) || !/^7656119\d{10}$/.test(entry.steamId)) {
    throw new Error('Invalid protected reserved-slot identity');
  }
  if (!Number.isSafeInteger(entry.protectedAt) || entry.protectedAt <= 0) {
    throw new Error('Invalid protected reserved-slot timestamp');
  }
  if (typeof entry.displayName !== 'string' || entry.displayName.length > 100) {
    throw new Error('Invalid protected reserved-slot display name');
  }
  return entry;
}

export class ReservedProtectionService {
  constructor({ database }) {
    this.database = database;
  }

  snapshot() {
    const state = this.database.getState(STATE_KEY, emptyState());
    if (!state || state.version !== 1 || !Array.isArray(state.entries)) {
      throw new Error('Invalid protected reserved-slot state');
    }
    const entries = state.entries.map(validateEntry);
    const users = new Set();
    const discord = new Set();
    const steam = new Set();
    for (const entry of entries) {
      if (users.has(entry.userId) || discord.has(entry.discordId) || steam.has(entry.steamId)) {
        throw new Error('Duplicate protected reserved-slot identity');
      }
      users.add(entry.userId);
      discord.add(entry.discordId);
      steam.add(entry.steamId);
    }
    return { version: 1, entries };
  }

  list() {
    return this.snapshot().entries
      .slice()
      .sort((left, right) => left.protectedAt - right.protectedAt);
  }

  count() {
    return this.snapshot().entries.length;
  }

  getByDiscord(discordId) {
    return this.snapshot().entries.find((entry) => entry.discordId === discordId) || null;
  }

  isProtected(userId, steamId = '') {
    return this.snapshot().entries.some((entry) => entry.userId === userId || (steamId && entry.steamId === steamId));
  }

  protect({ user, actorId, now = Date.now() }) {
    validateEntry({
      userId: user?.id,
      discordId: user?.discordId,
      steamId: user?.steamId,
      displayName: String(user?.displayName || 'Игрок').slice(0, 100),
      protectedAt: now
    });
    const state = this.snapshot();
    const matches = state.entries.filter((entry) => (
      entry.userId === user.id || entry.discordId === user.discordId || entry.steamId === user.steamId
    ));
    const previous = matches.sort((left, right) => left.protectedAt - right.protectedAt)[0] || null;
    const entry = {
      userId: user.id,
      discordId: user.discordId,
      steamId: user.steamId,
      displayName: String(user.displayName || 'Игрок').replace(/[\r\n\0]/g, ' ').slice(0, 100),
      protectedAt: previous?.protectedAt || now
    };
    state.entries = state.entries.filter((item) => (
      item.userId !== user.id && item.discordId !== user.discordId && item.steamId !== user.steamId
    ));
    state.entries.push(entry);
    this.database.setState(STATE_KEY, state);
    this.database.audit('reserved_slot_protected', actorId, { userId: user.id, created: !previous }, now);
    return { entry, created: !previous };
  }

  unprotectByDiscord({ discordId, actorId, now = Date.now() }) {
    const state = this.snapshot();
    const entry = state.entries.find((item) => item.discordId === discordId) || null;
    if (!entry) return { entry: null, removed: false };
    state.entries = state.entries.filter((item) => item.discordId !== discordId);
    this.database.setState(STATE_KEY, state);
    this.database.audit('reserved_slot_unprotected', actorId, { userId: entry.userId }, now);
    return { entry, removed: true };
  }
}

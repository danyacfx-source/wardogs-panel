import { mkdirSync, chmodSync } from 'node:fs';
import { dirname } from 'node:path';
import { randomUUID } from 'node:crypto';
import { incrementalPoints } from './scoring.js';
import { weekKey as currentWeekKey } from './time.js';
import { openDatabase } from './sqlite.js';

const SCHEMA_VERSION = '3';

export class DomainError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'DomainError';
    this.code = code;
  }
}

function safeInteger(value, name, { min = 0 } = {}) {
  if (!Number.isSafeInteger(value) || value < min) throw new Error(`Invalid ${name} in encrypted database`);
  return value;
}

function validateUser(value) {
  if (!value || typeof value !== 'object') throw new Error('Invalid encrypted user record');
  if (!/^\d{17,20}$/.test(value.discordId) || !/^7656119\d{10}$/.test(value.steamId)) {
    throw new Error('Invalid identity in encrypted database');
  }
  safeInteger(value.balance, 'balance');
  safeInteger(value.totalSeconds, 'totalSeconds');
  safeInteger(value.lifetimePoints, 'lifetimePoints');
  return value;
}

export class SeedingDatabase {
  constructor({ path, crypto, guildId, logger }) {
    this.path = path;
    this.crypto = crypto;
    this.logger = logger;
    mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
    try { chmodSync(dirname(path), 0o700); } catch {}
    this.db = openDatabase(path);
    this.db.exec(`
      PRAGMA journal_mode=WAL;
      PRAGMA synchronous=FULL;
      PRAGMA foreign_keys=ON;
      PRAGMA trusted_schema=OFF;
      PRAGMA secure_delete=ON;
      PRAGMA busy_timeout=5000;

      CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
      ) STRICT;

      CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        discord_idx TEXT NOT NULL UNIQUE,
        steam_idx TEXT NOT NULL UNIQUE,
        payload_enc TEXT NOT NULL,
        linked INTEGER NOT NULL DEFAULT 1 CHECK (linked IN (0, 1)),
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
      ) STRICT;

      CREATE TABLE IF NOT EXISTS link_challenges (
        discord_idx TEXT PRIMARY KEY,
        payload_enc TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL
      ) STRICT;

      CREATE TABLE IF NOT EXISTS donor_links (
        id TEXT PRIMARY KEY,
        discord_idx TEXT NOT NULL UNIQUE,
        steam_idx TEXT NOT NULL UNIQUE,
        payload_enc TEXT NOT NULL,
        created_at INTEGER NOT NULL
      ) STRICT;

      CREATE TABLE IF NOT EXISTS weekly_stats (
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        week_key TEXT NOT NULL,
        payload_enc TEXT NOT NULL,
        PRIMARY KEY (user_id, week_key)
      ) STRICT;

      CREATE TABLE IF NOT EXISTS active_sessions (
        user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        payload_enc TEXT NOT NULL
      ) STRICT;

      CREATE TABLE IF NOT EXISTS point_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
        ref_idx TEXT NOT NULL UNIQUE,
        created_at INTEGER NOT NULL,
        payload_enc TEXT NOT NULL,
        prev_hash TEXT NOT NULL,
        entry_hash TEXT NOT NULL
      ) STRICT;

      CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at INTEGER NOT NULL,
        kind TEXT NOT NULL,
        actor_idx TEXT NOT NULL,
        payload_enc TEXT NOT NULL,
        prev_hash TEXT NOT NULL,
        entry_hash TEXT NOT NULL
      ) STRICT;

      CREATE TABLE IF NOT EXISTS vip_memberships (
        user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE RESTRICT,
        payload_enc TEXT NOT NULL,
        updated_at INTEGER NOT NULL
      ) STRICT;

      CREATE TABLE IF NOT EXISTS vip_orders (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
        idempotency_idx TEXT NOT NULL UNIQUE,
        payload_enc TEXT NOT NULL,
        created_at INTEGER NOT NULL
      ) STRICT;

      CREATE TABLE IF NOT EXISTS weekly_reports (
        week_key TEXT PRIMARY KEY,
        payload_enc TEXT NOT NULL,
        published_at INTEGER NOT NULL
      ) STRICT;

      CREATE TABLE IF NOT EXISTS system_state (
        key TEXT PRIMARY KEY,
        payload_enc TEXT NOT NULL
      ) STRICT;

      CREATE TABLE IF NOT EXISTS instance_lease (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        owner_id TEXT NOT NULL,
        expires_at INTEGER NOT NULL
      ) STRICT;
      INSERT OR IGNORE INTO instance_lease (id, owner_id, expires_at) VALUES (1, '', 0);
    `);

    this.#migrateSchema();
    this.#bindMeta('key_fingerprint', crypto.fingerprint);
    this.#bindMeta('guild_index', crypto.blindIndex(guildId, 'guild'));
    try {
      chmodSync(path, 0o600);
    } catch {}
  }

  close() {
    this.db.close();
  }

  serialize() {
    this.db.exec('PRAGMA wal_checkpoint(FULL)');
    return this.db.serialize();
  }

  #bindMeta(key, expected) {
    const row = this.db.prepare('SELECT value FROM meta WHERE key = ?').get(key);
    if (!row) {
      this.db.prepare('INSERT INTO meta (key, value) VALUES (?, ?)').run(key, expected);
      return;
    }
    if (row.value !== expected) throw new Error(`Database ${key} does not match this bot configuration`);
  }

  #migrateSchema() {
    const row = this.db.prepare("SELECT value FROM meta WHERE key = 'schema_version'").get();
    const columns = new Set(this.db.prepare('PRAGMA table_info(users)').all().map((column) => column.name));
    if (!row) {
      if (!columns.has('linked')) throw new Error('Fresh database schema is incomplete');
      this.db.prepare("INSERT INTO meta (key, value) VALUES ('schema_version', ?)").run(SCHEMA_VERSION);
      return;
    }
    if (row.value === '1' || row.value === '2') {
      this.#transaction(() => {
        if (!columns.has('linked')) {
          this.db.exec('ALTER TABLE users ADD COLUMN linked INTEGER NOT NULL DEFAULT 1 CHECK (linked IN (0, 1))');
        }
        this.db.prepare("UPDATE meta SET value = ? WHERE key = 'schema_version'").run(SCHEMA_VERSION);
      });
      return;
    }
    if (row.value !== SCHEMA_VERSION || !columns.has('linked')) {
      throw new Error('Database schema_version does not match this bot version');
    }
  }

  #transaction(callback) {
    this.db.exec('BEGIN IMMEDIATE');
    try {
      const result = callback();
      this.db.exec('COMMIT');
      return result;
    } catch (error) {
      try { this.db.exec('ROLLBACK'); } catch {}
      throw error;
    }
  }

  #openUser(row) {
    if (!row) return null;
    return validateUser({
      id: row.id,
      ...this.crypto.openJson(row.payload_enc, `user:${row.id}`),
      linked: row.linked === 1
    });
  }

  #openDonor(row) {
    if (!row) return null;
    const value = this.crypto.openJson(row.payload_enc, `donor:${row.id}`);
    if (!/^\d{17,20}$/.test(value.discordId) || !/^7656119\d{10}$/.test(value.steamId)) {
      throw new Error('Invalid encrypted donation identity');
    }
    safeInteger(value.linkedAt, 'donation linkedAt');
    return { id: row.id, discordId: value.discordId, steamId: value.steamId,
      displayName: value.displayName, linkedAt: value.linkedAt, linked: true, purpose: 'donation' };
  }

  #saveUser(user, now = Date.now()) {
    validateUser(user);
    this.db.prepare('UPDATE users SET payload_enc = ?, updated_at = ? WHERE id = ?').run(
      this.crypto.sealJson({
        discordId: user.discordId,
        steamId: user.steamId,
        displayName: String(user.displayName || 'Игрок').slice(0, 80),
        balance: user.balance,
        totalSeconds: user.totalSeconds,
        lifetimePoints: user.lifetimePoints,
        linkedAt: user.linkedAt
      }, `user:${user.id}`),
      now,
      user.id
    );
  }

  #appendLedger(user, delta, reason, ref, now) {
    const refIdx = this.crypto.blindIndex(ref, 'ledger-ref');
    const duplicate = this.db.prepare('SELECT id FROM point_ledger WHERE ref_idx = ?').get(refIdx);
    if (duplicate) return false;
    const previous = this.db.prepare('SELECT entry_hash FROM point_ledger ORDER BY id DESC LIMIT 1').get()?.entry_hash || '';
    const payload = this.crypto.sealJson(
      { delta, balanceAfter: user.balance, reason: String(reason).slice(0, 160) },
      `ledger:${refIdx}`
    );
    const result = this.db.prepare(`
      INSERT INTO point_ledger (user_id, ref_idx, created_at, payload_enc, prev_hash, entry_hash)
      VALUES (?, ?, ?, ?, ?, '')
    `).run(user.id, refIdx, now, payload, previous);
    const id = Number(result.lastInsertRowid);
    const hash = this.crypto.chainDigest({
      domain: 'point-ledger', previous, id, timestamp: now, kind: 'points', actor: user.id, payload
    });
    this.db.prepare('UPDATE point_ledger SET entry_hash = ? WHERE id = ?').run(hash, id);
    return true;
  }

  #appendAudit(kind, actor, details, now = Date.now()) {
    const actorIdx = actor ? this.crypto.blindIndex(actor, 'audit-actor') : '';
    const previous = this.db.prepare('SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1').get()?.entry_hash || '';
    const payload = this.crypto.sealJson(details ?? {}, `audit:${kind}:${now}:${previous}`);
    const result = this.db.prepare(`
      INSERT INTO audit_log (created_at, kind, actor_idx, payload_enc, prev_hash, entry_hash)
      VALUES (?, ?, ?, ?, ?, '')
    `).run(now, String(kind).slice(0, 80), actorIdx, payload, previous);
    const id = Number(result.lastInsertRowid);
    const hash = this.crypto.chainDigest({
      domain: 'audit-log', previous, id, timestamp: now, kind, actor: actorIdx, payload
    });
    this.db.prepare('UPDATE audit_log SET entry_hash = ? WHERE id = ?').run(hash, id);
    return hash;
  }

  audit(kind, actor, details, now = Date.now()) {
    return this.#transaction(() => this.#appendAudit(kind, actor, details, now));
  }

  verifyIntegrity() {
    const integrity = this.db.prepare('PRAGMA integrity_check').get();
    if (integrity.integrity_check !== 'ok') throw new Error('SQLite integrity check failed');

    let previous = '';
    const sums = new Map();
    for (const row of this.db.prepare('SELECT * FROM point_ledger ORDER BY id').all()) {
      if (row.prev_hash !== previous) throw new Error(`Point ledger chain is broken at row ${row.id}`);
      const expected = this.crypto.chainDigest({
        domain: 'point-ledger', previous, id: row.id, timestamp: row.created_at,
        kind: 'points', actor: row.user_id, payload: row.payload_enc
      });
      if (!this.crypto.safeEqual(expected, row.entry_hash)) {
        throw new Error(`Point ledger authentication failed at row ${row.id}`);
      }
      const payload = this.crypto.openJson(row.payload_enc, `ledger:${row.ref_idx}`);
      if (!Number.isSafeInteger(payload.delta)) throw new Error(`Invalid ledger delta at row ${row.id}`);
      safeInteger(payload.balanceAfter, 'ledger balance');
      const running = (sums.get(row.user_id) || 0) + payload.delta;
      if (running !== payload.balanceAfter) throw new Error(`Ledger running balance is invalid at row ${row.id}`);
      sums.set(row.user_id, running);
      previous = row.entry_hash;
    }

    previous = '';
    for (const row of this.db.prepare('SELECT * FROM audit_log ORDER BY id').all()) {
      if (row.prev_hash !== previous) throw new Error(`Audit chain is broken at row ${row.id}`);
      const expected = this.crypto.chainDigest({
        domain: 'audit-log', previous, id: row.id, timestamp: row.created_at,
        kind: row.kind, actor: row.actor_idx, payload: row.payload_enc
      });
      if (!this.crypto.safeEqual(expected, row.entry_hash)) {
        throw new Error(`Audit authentication failed at row ${row.id}`);
      }
      this.crypto.openJson(row.payload_enc, `audit:${row.kind}:${row.created_at}:${previous}`);
      previous = row.entry_hash;
    }

    for (const row of this.db.prepare('SELECT * FROM users').all()) {
      const user = this.#openUser(row);
      if (!sums.has(user.id) || sums.get(user.id) !== user.balance) {
        throw new Error(`Point balance does not match the authenticated ledger for user ${user.id}`);
      }
    }

    for (const row of this.db.prepare('SELECT * FROM link_challenges').all()) {
      this.crypto.openJson(row.payload_enc, `challenge:${row.discord_idx}`);
    }
    for (const row of this.db.prepare('SELECT * FROM donor_links').all()) {
      const donor = this.#openDonor(row);
      if (this.crypto.blindIndex(donor.discordId, 'discord-id') !== row.discord_idx ||
          this.crypto.blindIndex(donor.steamId, 'steam-id') !== row.steam_idx ||
          this.db.prepare('SELECT id FROM users WHERE linked = 1 AND (discord_idx = ? OR steam_idx = ?)')
            .get(row.discord_idx, row.steam_idx)) {
        throw new Error('Donation identity conflicts with a seeding identity');
      }
    }
    for (const row of this.db.prepare('SELECT * FROM weekly_stats').all()) {
      this.crypto.openJson(row.payload_enc, `week:${row.user_id}:${row.week_key}`);
    }
    for (const row of this.db.prepare('SELECT * FROM active_sessions').all()) {
      this.crypto.openJson(row.payload_enc, `session:${row.user_id}`);
    }
    for (const row of this.db.prepare('SELECT * FROM vip_memberships').all()) {
      this.crypto.openJson(row.payload_enc, `vip:${row.user_id}`);
    }
    for (const row of this.db.prepare('SELECT * FROM vip_orders').all()) {
      this.crypto.openJson(row.payload_enc, `order:${row.id}`);
    }
    for (const row of this.db.prepare('SELECT * FROM weekly_reports').all()) {
      this.crypto.openJson(row.payload_enc, `report:${row.week_key}`);
    }
    for (const row of this.db.prepare('SELECT * FROM system_state').all()) {
      this.crypto.openJson(row.payload_enc, `state:${row.key}`);
    }
    const userCounts = this.db.prepare(`
      SELECT
        SUM(CASE WHEN linked = 1 THEN 1 ELSE 0 END) AS linked_count,
        SUM(CASE WHEN linked = 0 THEN 1 ELSE 0 END) AS archived_count
      FROM users
    `).get();
    return {
      users: Number(userCounts.linked_count || 0),
      archivedUsers: Number(userCounts.archived_count || 0),
      ledgerHead: this.db.prepare('SELECT entry_hash FROM point_ledger ORDER BY id DESC LIMIT 1').get()?.entry_hash || '',
      auditHead: this.db.prepare('SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1').get()?.entry_hash || ''
    };
  }

  acquireLease(ownerId, now, ttlMs) {
    const result = this.db.prepare(`
      UPDATE instance_lease SET owner_id = ?, expires_at = ?
      WHERE id = 1 AND (owner_id = ? OR expires_at < ?)
    `).run(ownerId, now + ttlMs, ownerId, now);
    return result.changes === 1;
  }

  renewLease(ownerId, now, ttlMs) {
    const result = this.db.prepare(`
      UPDATE instance_lease SET expires_at = ? WHERE id = 1 AND owner_id = ? AND expires_at >= ?
    `).run(now + ttlMs, ownerId, now);
    return result.changes === 1;
  }

  releaseLease(ownerId) {
    this.db.prepare("UPDATE instance_lease SET owner_id = '', expires_at = 0 WHERE id = 1 AND owner_id = ?").run(ownerId);
  }

  prepareLinkChallenge({ discordId, steamId, displayName, codeDigest, expiresAt,
    purpose = 'seeding', now = Date.now() }) {
    if (!['seeding', 'donation'].includes(purpose)) throw new Error('Invalid link purpose');
    const discordIdx = this.crypto.blindIndex(discordId, 'discord-id');
    const steamIdx = this.crypto.blindIndex(steamId, 'steam-id');
    if (this.db.prepare('SELECT id FROM users WHERE linked = 1 AND discord_idx = ?').get(discordIdx)) {
      throw new DomainError('already_linked', purpose === 'donation'
        ? 'Steam уже привязан для сидинга и подходит для оплаченного VIP. Новая привязка не нужна.'
        : 'Ваш Discord уже привязан к Steam.');
    }
    if (this.db.prepare('SELECT id FROM users WHERE linked = 1 AND steam_idx = ?').get(steamIdx)) {
      throw new DomainError('steam_taken', 'Этот Steam уже привязан к другому Discord.');
    }
    const donorDiscord = this.getDonorByDiscord(discordId);
    const donorSteam = this.getDonorBySteam(steamId);
    if (purpose === 'donation' && donorDiscord) {
      throw new DomainError('already_linked', 'Steam уже привязан для VIP.');
    }
    if (purpose === 'donation' && donorSteam ||
        purpose === 'seeding' && (donorDiscord && donorDiscord.steamId !== steamId ||
          donorSteam && donorSteam.discordId !== discordId)) {
      throw new DomainError('steam_taken', 'Discord или Steam уже привязан к другой паре аккаунтов.');
    }
    const payload = this.crypto.sealJson({
      discordId,
      steamId,
      displayName: String(displayName || 'Игрок').slice(0, 80),
      codeDigest,
      attempts: 0,
      purpose
    }, `challenge:${discordIdx}`);
    this.#transaction(() => {
      this.db.prepare(`
        INSERT INTO link_challenges (discord_idx, payload_enc, created_at, expires_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(discord_idx) DO UPDATE SET
          payload_enc = excluded.payload_enc,
          created_at = excluded.created_at,
          expires_at = excluded.expires_at
      `).run(discordIdx, payload, now, expiresAt);
      this.#appendAudit('link_challenge_created', discordId, { steamIdx, purpose }, now);
    });
  }

  cancelLinkChallenge(discordId, now = Date.now()) {
    const discordIdx = this.crypto.blindIndex(discordId, 'discord-id');
    this.#transaction(() => {
      this.db.prepare('DELETE FROM link_challenges WHERE discord_idx = ?').run(discordIdx);
      this.#appendAudit('link_challenge_cancelled', discordId, {}, now);
    });
  }

  verifyLinkChallenge({ discordId, code, maxAttempts, purpose = 'seeding', now = Date.now() }) {
    if (!['seeding', 'donation'].includes(purpose)) throw new Error('Invalid link purpose');
    const discordIdx = this.crypto.blindIndex(discordId, 'discord-id');
    const outcome = this.#transaction(() => {
      const row = this.db.prepare('SELECT * FROM link_challenges WHERE discord_idx = ?').get(discordIdx);
      const command = purpose === 'donation' ? '/vip-link link' : '/seed link';
      if (!row) return { error: new DomainError('challenge_missing', `Активный код не найден. Сначала выполните ${command}.`) };
      if (row.expires_at < now) {
        this.db.prepare('DELETE FROM link_challenges WHERE discord_idx = ?').run(discordIdx);
        this.#appendAudit('link_challenge_expired', discordId, {}, now);
        return { error: new DomainError('challenge_expired', `Код истёк. Запросите новый через ${command}.`) };
      }
      const value = this.crypto.openJson(row.payload_enc, `challenge:${discordIdx}`);
      if ((value.purpose || 'seeding') !== purpose) {
        return { error: new DomainError('wrong_link_purpose',
          `Этот код создан для другой привязки. Подтвердите его нужной командой или начните заново через ${command}.`) };
      }
      const supplied = this.crypto.challengeDigest(discordIdx, code);
      if (!this.crypto.safeEqual(value.codeDigest, supplied)) {
        value.attempts += 1;
        if (value.attempts >= maxAttempts) {
          this.db.prepare('DELETE FROM link_challenges WHERE discord_idx = ?').run(discordIdx);
          this.#appendAudit('link_challenge_locked', discordId, {}, now);
          return { error: new DomainError('challenge_locked', 'Слишком много попыток. Запросите новый код.') };
        }
        this.db.prepare('UPDATE link_challenges SET payload_enc = ? WHERE discord_idx = ?').run(
          this.crypto.sealJson(value, `challenge:${discordIdx}`),
          discordIdx
        );
        return { error: new DomainError('bad_code', `Неверный код. Осталось попыток: ${maxAttempts - value.attempts}.`) };
      }

      const steamIdx = this.crypto.blindIndex(value.steamId, 'steam-id');
      if (this.db.prepare(
        'SELECT id FROM users WHERE linked = 1 AND (discord_idx = ? OR steam_idx = ?)'
      ).get(discordIdx, steamIdx)) {
        return { error: new DomainError('identity_taken', 'Discord или Steam уже привязан. Обратитесь к администратору.') };
      }

      const donorDiscord = this.getDonorByDiscord(discordId);
      const donorSteam = this.getDonorBySteam(value.steamId);
      if (purpose === 'donation' && (donorDiscord || donorSteam) ||
          purpose === 'seeding' && (donorDiscord && donorDiscord.steamId !== value.steamId ||
            donorSteam && donorSteam.discordId !== discordId)) {
        return { error: new DomainError('identity_taken', 'Discord или Steam уже привязан к другой паре аккаунтов.') };
      }

      const archivedUsers = this.db.prepare('SELECT * FROM users WHERE linked = 0').all()
        .map((archivedRow) => this.#openUser(archivedRow));
      const archived = archivedUsers.filter((candidate) =>
        candidate.discordId === discordId && candidate.steamId === value.steamId);
      const partialMatch = archivedUsers.some((candidate) =>
        candidate.discordId === discordId || candidate.steamId === value.steamId);
      if (archived.length > 1 || (archived.length === 0 && partialMatch)) {
        return {
          error: new DomainError(
            'identity_history_conflict',
            'Новая пара Discord↔Steam не совпадает с архивной. Автоматический перенос баллов заблокирован; обратитесь к администратору.'
          )
        };
      }

      if (purpose === 'donation') {
        const id = randomUUID();
        const donor = { id, discordId, steamId: value.steamId, displayName: value.displayName,
          linkedAt: now, linked: true, purpose: 'donation' };
        this.db.prepare(`
          INSERT INTO donor_links (id, discord_idx, steam_idx, payload_enc, created_at)
          VALUES (?, ?, ?, ?, ?)
        `).run(id, discordIdx, steamIdx, this.crypto.sealJson({
          discordId, steamId: value.steamId, displayName: value.displayName, linkedAt: now
        }, `donor:${id}`), now);
        this.db.prepare('DELETE FROM link_challenges WHERE discord_idx = ?').run(discordIdx);
        this.#appendAudit('donation_identity_linked', discordId, { id, steamIdx }, now);
        return { user: donor };
      }

      if (donorDiscord) {
        this.db.prepare('DELETE FROM donor_links WHERE id = ?').run(donorDiscord.id);
        this.#appendAudit('donation_identity_promoted', discordId, { donorId: donorDiscord.id, steamIdx }, now);
      }

      let user;
      if (archived.length === 1) {
        user = archived[0];
        user.discordId = discordId;
        user.steamId = value.steamId;
        user.displayName = value.displayName;
        user.linkedAt = now;
        user.linked = true;
        this.db.prepare(`
          UPDATE users
          SET discord_idx = ?, steam_idx = ?, linked = 1, updated_at = ?
          WHERE id = ? AND linked = 0
        `).run(discordIdx, steamIdx, now, user.id);
        this.#saveUser(user, now);
        this.#appendLedger(user, 0, 'account_relinked', `relink:${user.id}:${now}`, now);
        this.#appendAudit('identity_relinked', discordId, { userId: user.id, steamIdx }, now);
      } else {
        const id = randomUUID();
        user = {
          id,
          discordId,
          steamId: value.steamId,
          displayName: value.displayName,
          balance: 0,
          totalSeconds: 0,
          lifetimePoints: 0,
          linkedAt: now,
          linked: true
        };
        this.db.prepare(`
          INSERT INTO users (id, discord_idx, steam_idx, payload_enc, linked, created_at, updated_at)
          VALUES (?, ?, ?, ?, 1, ?, ?)
        `).run(
          id,
          discordIdx,
          steamIdx,
          this.crypto.sealJson({
            discordId: user.discordId,
            steamId: user.steamId,
            displayName: user.displayName,
            balance: user.balance,
            totalSeconds: user.totalSeconds,
            lifetimePoints: user.lifetimePoints,
            linkedAt: user.linkedAt
          }, `user:${id}`),
          now,
          now
        );
        this.#appendLedger(user, 0, 'account_linked', `link:${id}`, now);
        this.#appendAudit('identity_linked', discordId, { userId: id, steamIdx }, now);
      }
      this.db.prepare('DELETE FROM link_challenges WHERE discord_idx = ?').run(discordIdx);
      return { user };
    });
    if (outcome.error) throw outcome.error;
    return outcome.user;
  }

  getUserByDiscord(discordId) {
    const index = this.crypto.blindIndex(discordId, 'discord-id');
    return this.#openUser(this.db.prepare('SELECT * FROM users WHERE linked = 1 AND discord_idx = ?').get(index));
  }

  getUserBySteam(steamId) {
    const index = this.crypto.blindIndex(steamId, 'steam-id');
    return this.#openUser(this.db.prepare('SELECT * FROM users WHERE linked = 1 AND steam_idx = ?').get(index));
  }

  getUserById(id) {
    return this.#openUser(this.db.prepare('SELECT * FROM users WHERE id = ?').get(id));
  }

  getDonorByDiscord(discordId) {
    const index = this.crypto.blindIndex(discordId, 'discord-id');
    return this.#openDonor(this.db.prepare('SELECT * FROM donor_links WHERE discord_idx = ?').get(index));
  }

  getDonorBySteam(steamId) {
    const index = this.crypto.blindIndex(steamId, 'steam-id');
    return this.#openDonor(this.db.prepare('SELECT * FROM donor_links WHERE steam_idx = ?').get(index));
  }

  countDonorLinks() {
    return this.db.prepare('SELECT COUNT(*) AS count FROM donor_links').get().count;
  }

  getVipIdentityByDiscord(discordId) {
    return this.getUserByDiscord(discordId) || this.getDonorByDiscord(discordId);
  }

  getVipIdentityById(id) {
    return this.getUserById(id) || this.#openDonor(this.db.prepare('SELECT * FROM donor_links WHERE id = ?').get(id));
  }

  unlinkDonorDiscord({ discordId, now = Date.now() }) {
    return this.#transaction(() => {
      const donor = this.getDonorByDiscord(discordId);
      if (!donor) throw new DomainError('not_linked',
        'Отдельной VIP-привязки нет. Если вы сидер, используйте /seed unlink.');
      const paid = this.getState('donation_feed', { vip: {} }).vip?.[discordId];
      const claim = this.getState('paid_slot_claims', {})[donor.steamId];
      if ((paid?.endsAt || 0) > now || claim) throw new DomainError('paid_vip_active',
        'Нельзя отменить привязку, пока оплаченный VIP действует или игровой слот ожидает снятия.');
      this.db.prepare('DELETE FROM donor_links WHERE id = ?').run(donor.id);
      this.db.prepare('DELETE FROM link_challenges WHERE discord_idx = ?').run(
        this.crypto.blindIndex(discordId, 'discord-id'));
      this.#appendAudit('donation_identity_unlinked', discordId, { donorId: donor.id,
        steamIdx: this.crypto.blindIndex(donor.steamId, 'steam-id') }, now);
      return { ...donor, linked: false };
    });
  }

  listUsers() {
    return this.db.prepare('SELECT * FROM users WHERE linked = 1 ORDER BY created_at').all()
      .map((row) => this.#openUser(row));
  }

  unlinkDiscord({ discordId, actorId = discordId, reason = 'self_unlink', now = Date.now() }) {
    return this.#transaction(() => {
      const user = this.getUserByDiscord(discordId);
      if (!user) throw new DomainError('not_linked', 'Steam не привязан к вашему Discord.');
      const member = this.#openMembership(
        this.db.prepare('SELECT * FROM vip_memberships WHERE user_id = ?').get(user.id)
      );
      if (member && member.state !== 'inactive') {
        throw new DomainError(
          'vip_active',
          'Нельзя отменить привязку, пока VIP активен или ожидает применения/снятия. Дождитесь завершения либо обратитесь к администратору.'
        );
      }
      const paid = this.getState('donation_feed', { vip: {} }).vip?.[discordId];
      const claim = this.getState('paid_slot_claims', {})[user.steamId];
      if ((paid?.endsAt || 0) > now || claim) {
        throw new DomainError('paid_vip_active',
          'Нельзя отменить привязку, пока оплаченный VIP действует или reserved slot ожидает снятия.');
      }

      const discordIdx = this.crypto.blindIndex(discordId, 'discord-id');
      const tombstone = randomUUID();
      const archivedDiscordIdx = this.crypto.blindIndex(`${user.id}:${tombstone}`, 'unlinked-discord');
      const archivedSteamIdx = this.crypto.blindIndex(`${tombstone}:${user.id}`, 'unlinked-steam');
      this.db.prepare(`
        UPDATE users
        SET discord_idx = ?, steam_idx = ?, linked = 0, updated_at = ?
        WHERE id = ? AND linked = 1
      `).run(archivedDiscordIdx, archivedSteamIdx, now, user.id);
      this.db.prepare('DELETE FROM active_sessions WHERE user_id = ?').run(user.id);
      this.db.prepare('DELETE FROM link_challenges WHERE discord_idx = ?').run(discordIdx);
      this.#appendAudit('identity_unlinked', actorId, {
        userId: user.id,
        reason: String(reason).replace(/[\r\n\0]/g, ' ').slice(0, 100)
      }, now);
      return { ...user, linked: false };
    });
  }

  resetSessions(now = Date.now()) {
    this.#transaction(() => {
      const count = this.db.prepare('SELECT COUNT(*) AS count FROM active_sessions').get().count;
      this.db.prepare('DELETE FROM active_sessions').run();
      if (count) this.#appendAudit('sessions_reset_on_startup', '', { count }, now);
    });
  }

  applySeedingTick({ eligibleMembers, now, weekKey, graceSeconds, maxDeltaSeconds }) {
    const eligible = new Map(eligibleMembers.map((member) => [member.discordId, member]));
    return this.#transaction(() => {
      const awards = [];
      const users = this.db.prepare('SELECT * FROM users WHERE linked = 1').all().map((row) => this.#openUser(row));
      const sessionGet = this.db.prepare('SELECT payload_enc FROM active_sessions WHERE user_id = ?');
      const sessionSet = this.db.prepare(`
        INSERT INTO active_sessions (user_id, payload_enc) VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET payload_enc = excluded.payload_enc
      `);
      const sessionDelete = this.db.prepare('DELETE FROM active_sessions WHERE user_id = ?');

      for (const user of users) {
        const member = eligible.get(user.discordId);
        const row = sessionGet.get(user.id);
        let session = row ? this.crypto.openJson(row.payload_enc, `session:${user.id}`) : null;

        if (!member) {
          if (!session) continue;
          if (session.ineligibleSince === null) session.ineligibleSince = now;
          if (now - session.ineligibleSince > graceSeconds * 1000) {
            sessionDelete.run(user.id);
          } else {
            sessionSet.run(user.id, this.crypto.sealJson(session, `session:${user.id}`));
          }
          continue;
        }

        if (member.displayName && member.displayName !== user.displayName) {
          user.displayName = String(member.displayName).slice(0, 80);
        }
        if (!session) {
          session = { continuousSeconds: 0, lastEligibleAt: now, ineligibleSince: null };
          sessionSet.run(user.id, this.crypto.sealJson(session, `session:${user.id}`));
          this.#saveUser(user, now);
          continue;
        }
        safeInteger(session.continuousSeconds, 'continuous session seconds');
        if (session.ineligibleSince !== null) {
          if (now - session.ineligibleSince > graceSeconds * 1000) session.continuousSeconds = 0;
          session.ineligibleSince = null;
          session.lastEligibleAt = now;
          sessionSet.run(user.id, this.crypto.sealJson(session, `session:${user.id}`));
          this.#saveUser(user, now);
          continue;
        }

        const rawDelta = Math.floor((now - session.lastEligibleAt) / 1000);
        const delta = Math.min(Math.max(0, rawDelta), maxDeltaSeconds);
        if (!delta) continue;
        const before = session.continuousSeconds;
        const after = before + delta;
        const points = incrementalPoints(before, after);
        session.continuousSeconds = after;
        session.lastEligibleAt = now;
        user.totalSeconds += delta;
        user.lifetimePoints += points;
        user.balance += points;
        this.#saveUser(user, now);
        sessionSet.run(user.id, this.crypto.sealJson(session, `session:${user.id}`));

        const weeklyRow = this.db.prepare(
          'SELECT payload_enc FROM weekly_stats WHERE user_id = ? AND week_key = ?'
        ).get(user.id, weekKey);
        const weekly = weeklyRow
          ? this.crypto.openJson(weeklyRow.payload_enc, `week:${user.id}:${weekKey}`)
          : { seconds: 0, points: 0 };
        weekly.seconds += delta;
        weekly.points += points;
        this.db.prepare(`
          INSERT INTO weekly_stats (user_id, week_key, payload_enc) VALUES (?, ?, ?)
          ON CONFLICT(user_id, week_key) DO UPDATE SET payload_enc = excluded.payload_enc
        `).run(user.id, weekKey, this.crypto.sealJson(weekly, `week:${user.id}:${weekKey}`));

        if (points > 0) {
          this.#appendLedger(user, points, 'seeding', `seed:${user.id}:${session.lastEligibleAt}:${after}`, now);
          awards.push({ discordId: user.discordId, points, balance: user.balance });
        }
      }
      return awards;
    });
  }

  getLeaderboard(weekKey) {
    const result = [];
    for (const row of this.db.prepare(`
      SELECT w.user_id, w.payload_enc, u.payload_enc AS user_payload
      FROM weekly_stats w JOIN users u ON u.id = w.user_id
      WHERE w.week_key = ? AND u.linked = 1
    `).all(weekKey)) {
      const user = validateUser({ id: row.user_id, ...this.crypto.openJson(row.user_payload, `user:${row.user_id}`) });
      const weekly = this.crypto.openJson(row.payload_enc, `week:${row.user_id}:${weekKey}`);
      safeInteger(weekly.seconds, 'weekly seconds');
      safeInteger(weekly.points, 'weekly points');
      if (weekly.seconds > 0) result.push({ ...user, weeklySeconds: weekly.seconds, weeklyPoints: weekly.points });
    }
    return result.sort((a, b) => b.weeklySeconds - a.weeklySeconds || a.displayName.localeCompare(b.displayName, 'ru'));
  }

  listSeeders(weekKey) {
    const weekly = new Map(this.getLeaderboard(weekKey).map((row) => [row.id, row]));
    return this.listUsers().map((user) => ({
      ...user,
      weeklySeconds: weekly.get(user.id)?.weeklySeconds || 0,
      weeklyPoints: weekly.get(user.id)?.weeklyPoints || 0,
      vip: this.getVipMembership(user.id)
    })).sort((a, b) =>
      b.weeklySeconds - a.weeklySeconds
      || b.totalSeconds - a.totalSeconds
      || a.displayName.localeCompare(b.displayName, 'ru'));
  }

  adjustPoints({ discordId, delta, reason, actorId, idempotencyKey, now = Date.now() }) {
    if (!Number.isSafeInteger(delta) || delta === 0 || Math.abs(delta) > 1_000_000) {
      throw new DomainError('bad_amount', 'Сумма должна быть целым числом от -1000000 до 1000000, кроме нуля.');
    }
    return this.#transaction(() => {
      const user = this.getUserByDiscord(discordId);
      if (!user) throw new DomainError('not_linked', 'У пользователя нет привязанного аккаунта.');
      if (user.balance + delta < 0) throw new DomainError('negative_balance', 'Баланс не может стать отрицательным.');
      const ref = `admin:${idempotencyKey}`;
      const existing = this.db.prepare('SELECT id FROM point_ledger WHERE ref_idx = ?').get(
        this.crypto.blindIndex(ref, 'ledger-ref')
      );
      if (existing) return user;
      user.balance += delta;
      this.#saveUser(user, now);
      this.#appendLedger(user, delta, `admin:${String(reason).slice(0, 100)}`, ref, now);
      this.#appendAudit('points_adjusted', actorId, { userId: user.id, delta, reason: String(reason).slice(0, 100) }, now);
      return user;
    });
  }

  grantPointsToAll({ amount, reason, actorId, idempotencyKey, now = Date.now() }) {
    if (!Number.isSafeInteger(amount) || amount < 1 || amount > 1_000_000) {
      throw new DomainError('bad_amount', 'Начисление всем сидерам: от 1 до 1000000 баллов.');
    }
    const note = String(reason || '').replace(/[\r\n\0]/g, ' ').trim().slice(0, 100);
    if (note.length < 3 || !idempotencyKey || !actorId) {
      throw new DomainError('bad_reason', 'Укажите причину не короче трёх символов.');
    }
    return this.#transaction(() => {
      const marker = `admin-bulk:${this.crypto.blindIndex(idempotencyKey, 'bulk-admin')}`;
      const previous = this.getState(marker);
      if (previous) {
        if (previous.kind !== 'points') throw new DomainError('duplicate_command', 'ID команды уже использован.');
        return { ...previous, applied: false };
      }
      const users = this.listUsers();
      for (const user of users) {
        user.balance += amount;
        this.#saveUser(user, now);
        this.#appendLedger(user, amount, `admin_all:${note}`,
          `admin-bulk-points:${idempotencyKey}:${user.id}`, now);
      }
      const result = { kind: 'points', count: users.length, amount,
        totalPoints: users.length * amount };
      safeInteger(result.totalPoints, 'bulk points');
      this.#appendAudit('points_granted_all', actorId, {
        ...result, reason: note
      }, now);
      this.setState(marker, result);
      return { ...result, applied: true };
    });
  }

  #addTimeForUser(user, minutes, note, ref, key, now) {
    const weeklyRow = this.db.prepare(
      'SELECT payload_enc FROM weekly_stats WHERE user_id = ? AND week_key = ?'
    ).get(user.id, key);
    const weekly = weeklyRow
      ? this.crypto.openJson(weeklyRow.payload_enc, `week:${user.id}:${key}`)
      : { seconds: 0, points: 0 };
    const before = safeInteger(weekly.manualSeconds ?? 0, 'manual weekly seconds');
    const seconds = minutes * 60;
    const points = incrementalPoints(before, before + seconds);
    weekly.manualSeconds = before + seconds;
    weekly.seconds += seconds;
    weekly.points += points;
    user.totalSeconds += seconds;
    user.lifetimePoints += points;
    user.balance += points;
    this.#saveUser(user, now);
    this.db.prepare(`
      INSERT INTO weekly_stats (user_id, week_key, payload_enc) VALUES (?, ?, ?)
      ON CONFLICT(user_id, week_key) DO UPDATE SET payload_enc = excluded.payload_enc
    `).run(user.id, key, this.crypto.sealJson(weekly, `week:${user.id}:${key}`));
    // Zero-point entries still reserve the unique command ID for safe retries.
    this.#appendLedger(user, points, `manual_time:${note}`, ref, now);
    return points;
  }

  addSeedingTime({ discordId, minutes, reason, actorId, idempotencyKey, now = Date.now() }) {
    if (!Number.isSafeInteger(minutes) || minutes < 1 || minutes > 1440) {
      throw new DomainError('bad_minutes', 'Добавьте от 1 до 1440 минут за одну операцию.');
    }
    const note = String(reason || '').replace(/[\r\n\0]/g, ' ').trim().slice(0, 100);
    if (note.length < 3 || !idempotencyKey || !actorId) {
      throw new DomainError('bad_reason', 'Укажите причину не короче трёх символов.');
    }
    return this.#transaction(() => {
      const user = this.getUserByDiscord(discordId);
      if (!user) throw new DomainError('not_linked', 'У пользователя нет подтверждённой привязки сидера.');
      const ref = `admin-time:${idempotencyKey}`;
      const duplicate = this.db.prepare('SELECT id FROM point_ledger WHERE ref_idx = ?').get(
        this.crypto.blindIndex(ref, 'ledger-ref')
      );
      const key = currentWeekKey(now);
      if (duplicate) return { applied: false, user, minutes: 0, points: 0, weekKey: key };
      const points = this.#addTimeForUser(user, minutes, note, ref, key, now);
      this.#appendAudit('seeding_time_added', actorId, {
        userId: user.id, minutes, points, weekKey: key, reason: note
      }, now);
      return { applied: true, user, minutes, points, weekKey: key };
    });
  }

  addSeedingTimeToAll({ minutes, reason, actorId, idempotencyKey, now = Date.now() }) {
    if (!Number.isSafeInteger(minutes) || minutes < 1 || minutes > 1440) {
      throw new DomainError('bad_minutes', 'Добавьте от 1 до 1440 минут за одну операцию.');
    }
    const note = String(reason || '').replace(/[\r\n\0]/g, ' ').trim().slice(0, 100);
    if (note.length < 3 || !idempotencyKey || !actorId) {
      throw new DomainError('bad_reason', 'Укажите причину не короче трёх символов.');
    }
    return this.#transaction(() => {
      const marker = `admin-bulk:${this.crypto.blindIndex(idempotencyKey, 'bulk-admin')}`;
      const previous = this.getState(marker);
      if (previous) {
        if (previous.kind !== 'time') throw new DomainError('duplicate_command', 'ID команды уже использован.');
        return { ...previous, applied: false };
      }
      const users = this.listUsers();
      const key = currentWeekKey(now);
      let totalPoints = 0;
      for (const user of users) {
        totalPoints += this.#addTimeForUser(user, minutes, note,
          `admin-bulk-time:${idempotencyKey}:${user.id}`, key, now);
      }
      const result = { kind: 'time', count: users.length, minutes, totalPoints, weekKey: key };
      safeInteger(totalPoints, 'bulk points');
      this.#appendAudit('seeding_time_added_all', actorId, { ...result, reason: note }, now);
      this.setState(marker, result);
      return { ...result, applied: true };
    });
  }

  #openMembership(row) {
    return row ? { userId: row.user_id, ...this.crypto.openJson(row.payload_enc, `vip:${row.user_id}`) } : null;
  }

  #saveMembership(member, now = Date.now()) {
    const payload = { ...member };
    delete payload.userId;
    this.db.prepare(`
      INSERT INTO vip_memberships (user_id, payload_enc, updated_at) VALUES (?, ?, ?)
      ON CONFLICT(user_id) DO UPDATE SET payload_enc = excluded.payload_enc, updated_at = excluded.updated_at
    `).run(member.userId, this.crypto.sealJson(payload, `vip:${member.userId}`), now);
  }

  purchaseVip({ discordId, sku, cost, durationSeconds, idempotencyKey, now = Date.now() }) {
    return this.#transaction(() => {
      const idemIdx = this.crypto.blindIndex(idempotencyKey, 'vip-order');
      const prior = this.db.prepare('SELECT * FROM vip_orders WHERE idempotency_idx = ?').get(idemIdx);
      if (prior) {
        const order = this.crypto.openJson(prior.payload_enc, `order:${prior.id}`);
        const priorUser = this.getUserById(order.userId);
        return { ...order, balance: priorUser?.balance ?? 0 };
      }
      const user = this.getUserByDiscord(discordId);
      if (!user) throw new DomainError('not_linked', 'Сначала привяжите Steam через /seed link.');
      if (user.balance < cost) throw new DomainError('insufficient_points', `Недостаточно баллов: нужно ${cost}, доступно ${user.balance}.`);

      user.balance -= cost;
      this.#saveUser(user, now);
      const orderId = randomUUID();
      this.#appendLedger(user, -cost, `vip_purchase:${sku}`, `vip:${orderId}`, now);

      let member = this.#openMembership(
        this.db.prepare('SELECT * FROM vip_memberships WHERE user_id = ?').get(user.id)
      );
      if (!member) {
        member = {
          userId: user.id,
          state: 'pending_add',
          queuedSeconds: durationSeconds,
          activeUntil: null,
          managedByBot: null,
          pendingSince: now,
          lastError: ''
        };
      } else if (member.state === 'active' && member.activeUntil > now) {
        member.activeUntil += durationSeconds;
      } else if (member.state === 'pending_remove') {
        member.state = 'pending_add';
        member.queuedSeconds += durationSeconds;
        member.activeUntil = null;
        member.pendingSince = now;
      } else {
        member.state = 'pending_add';
        member.queuedSeconds += durationSeconds;
        member.pendingSince ||= now;
      }
      member.lastError = '';
      this.#saveMembership(member, now);

      const order = {
        id: orderId,
        userId: user.id,
        sku,
        cost,
        durationSeconds,
        purchasedAt: now,
        state: member.state === 'active' ? 'active' : 'queued'
      };
      this.db.prepare(`
        INSERT INTO vip_orders (id, user_id, idempotency_idx, payload_enc, created_at)
        VALUES (?, ?, ?, ?, ?)
      `).run(orderId, user.id, idemIdx, this.crypto.sealJson(order, `order:${orderId}`), now);
      this.#appendAudit('vip_purchased', discordId, { orderId, userId: user.id, sku, cost }, now);
      return { ...order, balance: user.balance, membershipState: member.state };
    });
  }

  listVipMemberships() {
    return this.db.prepare('SELECT * FROM vip_memberships').all().map((row) => this.#openMembership(row));
  }

  getVipMembership(userId) {
    return this.#openMembership(this.db.prepare('SELECT * FROM vip_memberships WHERE user_id = ?').get(userId));
  }

  setVipOwnership(userId, managedByBot, now = Date.now()) {
    this.#transaction(() => {
      const member = this.#openMembership(this.db.prepare('SELECT * FROM vip_memberships WHERE user_id = ?').get(userId));
      if (!member) return;
      member.managedByBot = managedByBot;
      member.lastError = '';
      this.#saveMembership(member, now);
      this.#appendAudit('vip_ownership_classified', '', { userId, managedByBot }, now);
    });
  }

  activateVip(userId, now = Date.now()) {
    return this.#transaction(() => {
      const member = this.#openMembership(this.db.prepare('SELECT * FROM vip_memberships WHERE user_id = ?').get(userId));
      if (!member || member.state !== 'pending_add' || member.queuedSeconds <= 0) return member;
      member.state = 'active';
      member.activeUntil = now + member.queuedSeconds * 1000;
      member.queuedSeconds = 0;
      member.pendingSince = null;
      member.lastError = '';
      this.#saveMembership(member, now);
      for (const row of this.db.prepare('SELECT * FROM vip_orders WHERE user_id = ?').all(userId)) {
        const order = this.crypto.openJson(row.payload_enc, `order:${row.id}`);
        if (order.state === 'queued') {
          order.state = 'active';
          this.db.prepare('UPDATE vip_orders SET payload_enc = ? WHERE id = ?').run(
            this.crypto.sealJson(order, `order:${row.id}`), row.id
          );
        }
      }
      this.#appendAudit('vip_activated', '', { userId, activeUntil: member.activeUntil }, now);
      return member;
    });
  }

  setVipState(userId, state, { error = '', now = Date.now() } = {}) {
    const allowed = new Set(['pending_add', 'active', 'pending_remove', 'inactive']);
    if (!allowed.has(state)) throw new Error('Invalid VIP state');
    return this.#transaction(() => {
      const member = this.#openMembership(this.db.prepare('SELECT * FROM vip_memberships WHERE user_id = ?').get(userId));
      if (!member) return null;
      member.state = state;
      member.pendingSince = state.startsWith('pending_') ? (member.pendingSince || now) : null;
      member.lastError = String(error).slice(0, 160);
      if (state === 'inactive') {
        member.activeUntil = null;
        member.managedByBot = null;
      }
      this.#saveMembership(member, now);
      if (state === 'inactive') {
        for (const row of this.db.prepare('SELECT * FROM vip_orders WHERE user_id = ?').all(userId)) {
          const order = this.crypto.openJson(row.payload_enc, `order:${row.id}`);
          if (order.state === 'active') {
            order.state = 'expired';
            this.db.prepare('UPDATE vip_orders SET payload_enc = ? WHERE id = ?').run(
              this.crypto.sealJson(order, `order:${row.id}`), row.id
            );
          }
        }
        this.#appendAudit('vip_expired', '', { userId }, now);
      }
      return member;
    });
  }

  setVipError(userId, error, now = Date.now()) {
    return this.#transaction(() => {
      const member = this.#openMembership(this.db.prepare('SELECT * FROM vip_memberships WHERE user_id = ?').get(userId));
      if (!member) return null;
      member.lastError = String(error).slice(0, 160);
      this.#saveMembership(member, now);
      return member;
    });
  }

  reportPublished(weekKey) {
    const row = this.db.prepare('SELECT * FROM weekly_reports WHERE week_key = ?').get(weekKey);
    return row ? this.crypto.openJson(row.payload_enc, `report:${weekKey}`) : null;
  }

  markReportPublished(weekKey, messageIds, now = Date.now()) {
    this.#transaction(() => {
      const payload = { weekKey, messageIds, publishedAt: now };
      this.db.prepare(`
        INSERT INTO weekly_reports (week_key, payload_enc, published_at) VALUES (?, ?, ?)
        ON CONFLICT(week_key) DO UPDATE SET payload_enc = excluded.payload_enc, published_at = excluded.published_at
      `).run(weekKey, this.crypto.sealJson(payload, `report:${weekKey}`), now);
      this.#appendAudit('weekly_report_published', '', { weekKey, messageCount: messageIds.length }, now);
    });
  }

  getState(key, fallback = null) {
    const row = this.db.prepare('SELECT payload_enc FROM system_state WHERE key = ?').get(key);
    return row ? this.crypto.openJson(row.payload_enc, `state:${key}`) : fallback;
  }

  setState(key, value) {
    this.db.prepare(`
      INSERT INTO system_state (key, payload_enc) VALUES (?, ?)
      ON CONFLICT(key) DO UPDATE SET payload_enc = excluded.payload_enc
    `).run(key, this.crypto.sealJson(value, `state:${key}`));
  }

  chainHeads() {
    return {
      ledger: this.db.prepare('SELECT entry_hash FROM point_ledger ORDER BY id DESC LIMIT 1').get()?.entry_hash || '',
      audit: this.db.prepare('SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1').get()?.entry_hash || ''
    };
  }
}

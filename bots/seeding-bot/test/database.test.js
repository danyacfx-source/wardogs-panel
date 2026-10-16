import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { randomBytes } from 'node:crypto';
import { openDatabase } from '../src/sqlite.js';
import { CryptoBox } from '../src/crypto-box.js';
import { DomainError, SeedingDatabase } from '../src/database.js';
import { ReservedProtectionService } from '../src/services/reserved-protection-service.js';

async function fixture(t) {
  const directory = await mkdtemp(join(tmpdir(), 'wardogs-seed-test-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const crypto = new CryptoBox(randomBytes(32));
  const database = new SeedingDatabase({
    path: join(directory, 'test.sqlite'),
    crypto,
    guildId: '1552253117586739210',
    logger: { info() {}, warn() {}, error() {} }
  });
  t.after(() => database.close());
  return { database, crypto };
}

function challenge(database, crypto, { discordId = '111111111111111111', steamId = '76561198000000001',
  purpose = 'seeding' } = {}) {
  const code = 'ABCDEFGH';
  const index = crypto.blindIndex(discordId, 'discord-id');
  database.prepareLinkChallenge({
    discordId,
    steamId,
    displayName: 'Tester',
    codeDigest: crypto.challengeDigest(index, code),
    expiresAt: Date.now() + 60_000,
    purpose
  });
  return { code, discordId, steamId };
}

test('failed link attempts are persisted and lock the challenge', async (t) => {
  const { database, crypto } = await fixture(t);
  const value = challenge(database, crypto);
  assert.throws(
    () => database.verifyLinkChallenge({ discordId: value.discordId, code: 'ZZZZZZZZ', maxAttempts: 2 }),
    (error) => error instanceof DomainError && error.code === 'bad_code'
  );
  assert.throws(
    () => database.verifyLinkChallenge({ discordId: value.discordId, code: 'YYYYYYYY', maxAttempts: 2 }),
    (error) => error instanceof DomainError && error.code === 'challenge_locked'
  );
  assert.throws(
    () => database.verifyLinkChallenge({ discordId: value.discordId, code: value.code, maxAttempts: 2 }),
    (error) => error instanceof DomainError && error.code === 'challenge_missing'
  );
});

test('scoring, VIP purchase and integrity verification agree', async (t) => {
  const { database, crypto } = await fixture(t);
  const value = challenge(database, crypto);
  const user = database.verifyLinkChallenge({ discordId: value.discordId, code: value.code, maxAttempts: 5 });
  const start = Date.now();
  database.applySeedingTick({
    eligibleMembers: [{ discordId: value.discordId, displayName: 'Tester' }],
    now: start,
    weekKey: '2026-09-21',
    graceSeconds: 180,
    maxDeltaSeconds: 360
  });
  for (let step = 1; step <= 10; step += 1) {
    database.applySeedingTick({
      eligibleMembers: [{ discordId: value.discordId, displayName: 'Tester' }],
      now: start + step * 360_000,
      weekKey: '2026-09-21',
      graceSeconds: 180,
      maxDeltaSeconds: 360
    });
  }
  assert.equal(database.getUserById(user.id).balance, 10);
  database.adjustPoints({
    discordId: value.discordId,
    delta: 200,
    reason: 'test grant',
    actorId: '222222222222222222',
    idempotencyKey: 'grant-1'
  });
  const order = database.purchaseVip({
    discordId: value.discordId,
    sku: '1d',
    cost: 120,
    durationSeconds: 86_400,
    idempotencyKey: 'purchase-1'
  });
  assert.equal(order.balance, 90);
  assert.equal(database.getVipMembership(user.id).state, 'pending_add');
  assert.equal(database.verifyIntegrity().users, 1);
});

test('admin time repair updates shared lifetime and weekly ranking once and keeps an authenticated audit', async (t) => {
  const { database, crypto } = await fixture(t);
  const value = challenge(database, crypto);
  const user = database.verifyLinkChallenge({ discordId: value.discordId, code: value.code, maxAttempts: 5 });
  const now = Date.UTC(2026, 8, 28, 12);
  const options = { discordId: value.discordId, actorId: '222222222222222222',
    reason: 'сбой опроса второго RCON', now };

  const first = database.addSeedingTime({ ...options, minutes: 5, idempotencyKey: 'repair-1' });
  assert.equal(first.points, 0);
  assert.equal(database.addSeedingTime({ ...options, minutes: 5, idempotencyKey: 'repair-1' }).applied, false);
  const second = database.addSeedingTime({ ...options, minutes: 1, idempotencyKey: 'repair-2' });
  assert.equal(second.points, 1);
  const third = database.addSeedingTime({ ...options, minutes: 114, idempotencyKey: 'repair-3' });
  assert.equal(third.points, 29);
  assert.equal(database.getUserById(user.id).totalSeconds, 7200);
  assert.equal(database.getUserById(user.id).balance, 30);
  assert.equal(database.getUserById(user.id).lifetimePoints, 30);
  assert.equal(database.getLeaderboard('2026-09-28')[0].weeklySeconds, 7200);
  assert.equal(database.verifyIntegrity().users, 1);
  assert.equal(database.serialize().includes(Buffer.from(options.reason)), false);
  assert.throws(() => database.addSeedingTime({ ...options, minutes: 0,
    idempotencyKey: 'repair-4' }), /от 1 до 1440/);
});

test('bulk points reach only active seeders, remain idempotent and roll back together', async (t) => {
  const { database, crypto } = await fixture(t);
  const first = challenge(database, crypto);
  const second = challenge(database, crypto, { discordId: '111111111111111112',
    steamId: '76561198000000002' });
  const donor = challenge(database, crypto, { discordId: '111111111111111113',
    steamId: '76561198000000003', purpose: 'donation' });
  const archived = challenge(database, crypto, { discordId: '111111111111111114',
    steamId: '76561198000000004' });
  for (const value of [first, second, donor, archived]) {
    database.verifyLinkChallenge({ discordId: value.discordId, code: value.code,
      purpose: value === donor ? 'donation' : 'seeding', maxAttempts: 5 });
  }
  database.unlinkDiscord({ discordId: archived.discordId });
  const options = { amount: 20, reason: 'компенсация за второй сервер',
    actorId: '222222222222222222', idempotencyKey: 'bulk-compensation' };
  const firstResult = database.grantPointsToAll(options);
  assert.deepEqual(firstResult, { kind: 'points', count: 2, amount: 20,
    totalPoints: 40, applied: true });
  assert.equal(database.getUserByDiscord(first.discordId).balance, 20);
  assert.equal(database.getUserByDiscord(second.discordId).balance, 20);
  assert.equal(database.getDonorByDiscord(donor.discordId) !== null, true);
  assert.equal(database.getUserByDiscord(donor.discordId), null);
  assert.equal(database.listUsers().length, 2);
  assert.equal(database.grantPointsToAll(options).applied, false);
  assert.equal(database.getUserByDiscord(first.discordId).balance, 20);
  assert.equal(database.getLeaderboard('2026-09-28').length, 0);

  const secondUserId = database.getUserByDiscord(second.discordId).id;
  database.db.exec(`CREATE TRIGGER fail_bulk BEFORE UPDATE ON users
    WHEN NEW.id = '${secondUserId}' BEGIN SELECT RAISE(ABORT, 'simulated bulk failure'); END;`);
  assert.throws(() => database.grantPointsToAll({ ...options, amount: 10,
    idempotencyKey: 'bulk-rollback' }), /simulated bulk failure/);
  database.db.exec('DROP TRIGGER fail_bulk');
  assert.equal(database.getUserByDiscord(first.discordId).balance, 20);
  assert.equal(database.getState(`admin-bulk:${crypto.blindIndex('bulk-rollback', 'bulk-admin')}`), null);
  assert.equal(database.verifyIntegrity().users, 2);
});

test('bulk time uses the same weekly scoring and skips VIP-only identities', async (t) => {
  const { database, crypto } = await fixture(t);
  const first = challenge(database, crypto);
  const second = challenge(database, crypto, { discordId: '111111111111111112',
    steamId: '76561198000000002' });
  const donor = challenge(database, crypto, { discordId: '111111111111111113',
    steamId: '76561198000000003', purpose: 'donation' });
  for (const value of [first, second, donor]) {
    database.verifyLinkChallenge({ discordId: value.discordId, code: value.code,
      purpose: value === donor ? 'donation' : 'seeding', maxAttempts: 5 });
  }
  const now = Date.UTC(2026, 8, 29, 12);
  database.addSeedingTime({ discordId: first.discordId, minutes: 5, now,
    reason: 'первоначальная корректировка', actorId: '222222222222222222',
    idempotencyKey: 'single-repair' });
  const options = { minutes: 1, reason: 'общий сбой RCON', now,
    actorId: '222222222222222222', idempotencyKey: 'all-repair' };
  const result = database.addSeedingTimeToAll(options);
  assert.deepEqual(result, { kind: 'time', count: 2, minutes: 1,
    totalPoints: 1, weekKey: '2026-09-28', applied: true });
  assert.equal(database.getUserByDiscord(first.discordId).totalSeconds, 360);
  assert.equal(database.getUserByDiscord(first.discordId).balance, 1);
  assert.equal(database.getUserByDiscord(second.discordId).totalSeconds, 60);
  assert.equal(database.getUserByDiscord(second.discordId).balance, 0);
  assert.equal(database.getUserByDiscord(donor.discordId), null);
  assert.equal(database.getLeaderboard('2026-09-28').length, 2);
  assert.equal(database.addSeedingTimeToAll(options).applied, false);
  assert.equal(database.getUserByDiscord(second.discordId).totalSeconds, 60);
  assert.equal(database.verifyIntegrity().users, 2);
});

test('database snapshot serializes to a portable SQLite file', async (t) => {
  const { database } = await fixture(t);
  const snapshot = database.serialize();
  assert.ok(Buffer.isBuffer(snapshot));
  assert.equal(snapshot.subarray(0, 16).toString('binary'), 'SQLite format 3\0');

  const directory = await mkdtemp(join(tmpdir(), 'wardogs-snapshot-test-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const path = join(directory, 'snapshot.sqlite');
  await writeFile(path, snapshot, { mode: 0o600 });
  const restored = openDatabase(path, { readOnly: true });
  assert.equal(restored.prepare('PRAGMA integrity_check').get().integrity_check, 'ok');
  restored.close();
});

test('tampering with encrypted account data is detected', async (t) => {
  const { database, crypto } = await fixture(t);
  const value = challenge(database, crypto);
  const user = database.verifyLinkChallenge({ discordId: value.discordId, code: value.code, maxAttempts: 5 });
  const row = database.db.prepare('SELECT payload_enc FROM users WHERE id = ?').get(user.id);
  const parts = row.payload_enc.split('.');
  const ciphertext = Buffer.from(parts[2], 'base64url');
  ciphertext[0] ^= 1;
  parts[2] = ciphertext.toString('base64url');
  database.db.prepare('UPDATE users SET payload_enc = ? WHERE id = ?').run(parts.join('.'), user.id);
  assert.throws(() => database.verifyIntegrity());
});

test('permanent reserved-slot protection is encrypted, idempotent and audited', async (t) => {
  const { database, crypto } = await fixture(t);
  const value = challenge(database, crypto);
  const user = database.verifyLinkChallenge({ discordId: value.discordId, code: value.code, maxAttempts: 5 });
  const service = new ReservedProtectionService({ database });

  const first = service.protect({ user, actorId: '222222222222222222', now: 1_700_000_000_000 });
  const second = service.protect({ user, actorId: '222222222222222222', now: 1_700_000_100_000 });
  assert.equal(first.created, true);
  assert.equal(second.created, false);
  assert.equal(service.count(), 1);
  assert.equal(service.isProtected(user.id, user.steamId), true);
  assert.equal(service.getByDiscord(user.discordId).protectedAt, 1_700_000_000_000);

  const stored = database.db.prepare(
    "SELECT payload_enc FROM system_state WHERE key = 'protected_reserved_slots_v1'"
  ).get().payload_enc;
  assert.equal(stored.includes(user.discordId), false);
  assert.equal(stored.includes(user.steamId), false);
  assert.equal(database.verifyIntegrity().users, 1);

  const removed = service.unprotectByDiscord({
    discordId: user.discordId,
    actorId: '222222222222222222',
    now: 1_700_000_200_000
  });
  assert.equal(removed.removed, true);
  assert.equal(service.count(), 0);
});

test('1.5.1 database upgrade preserves seeding points and protected slots', async (t) => {
  const { database, crypto } = await fixture(t);
  const value = challenge(database, crypto);
  const user = database.verifyLinkChallenge({ discordId: value.discordId, code: value.code, maxAttempts: 5 });
  database.adjustPoints({ discordId: value.discordId, delta: 250,
    reason: 'migration test', actorId: '222222222222222222', idempotencyKey: 'migrate-grant' });
  new ReservedProtectionService({ database }).protect({ user, actorId: '222222222222222222' });

  const directory = await mkdtemp(join(tmpdir(), 'wardogs-151-upgrade-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const path = join(directory, 'seeding.sqlite');
  await writeFile(path, database.serialize());
  const old = openDatabase(path);
  old.prepare("UPDATE meta SET value = '1' WHERE key = 'schema_version'").run();
  old.close();

  const migrated = new SeedingDatabase({ path, crypto, guildId: '1552253117586739210',
    logger: { info() {}, warn() {}, error() {} } });
  t.after(() => migrated.close());
  assert.equal(migrated.verifyIntegrity().users, 1);
  assert.equal(migrated.getUserByDiscord(value.discordId).balance, 250);
  assert.equal(new ReservedProtectionService({ database: migrated }).isProtected(user.id, user.steamId), true);
  assert.equal(migrated.countDonorLinks(), 0);
});

test('unlink archives identity and verified relink restores balance and history', async (t) => {
  const { database, crypto } = await fixture(t);
  const value = challenge(database, crypto);
  const original = database.verifyLinkChallenge({ discordId: value.discordId, code: value.code, maxAttempts: 5 });
  database.adjustPoints({
    discordId: value.discordId,
    delta: 200,
    reason: 'test grant',
    actorId: '222222222222222222',
    idempotencyKey: 'unlink-grant'
  });

  const archived = database.unlinkDiscord({ discordId: value.discordId });
  assert.equal(archived.linked, false);
  assert.equal(database.getUserByDiscord(value.discordId), null);
  assert.equal(database.getUserBySteam(value.steamId), null);
  assert.deepEqual(database.verifyIntegrity(), {
    users: 0,
    archivedUsers: 1,
    ledgerHead: database.chainHeads().ledger,
    auditHead: database.chainHeads().audit
  });

  const relink = challenge(database, crypto, value);
  const restored = database.verifyLinkChallenge({
    discordId: relink.discordId,
    code: relink.code,
    maxAttempts: 5
  });
  assert.equal(restored.id, original.id);
  assert.equal(restored.balance, 200);
  assert.equal(database.verifyIntegrity().users, 1);
});

test('unlink is blocked while VIP is pending or active', async (t) => {
  const { database, crypto } = await fixture(t);
  const value = challenge(database, crypto);
  database.verifyLinkChallenge({ discordId: value.discordId, code: value.code, maxAttempts: 5 });
  database.adjustPoints({
    discordId: value.discordId,
    delta: 200,
    reason: 'test grant',
    actorId: '222222222222222222',
    idempotencyKey: 'vip-unlink-grant'
  });
  database.purchaseVip({
    discordId: value.discordId,
    sku: '1d',
    cost: 120,
    durationSeconds: 86_400,
    idempotencyKey: 'vip-unlink-order'
  });
  assert.throws(
    () => database.unlinkDiscord({ discordId: value.discordId }),
    (error) => error instanceof DomainError && error.code === 'vip_active'
  );
  assert.ok(database.getUserByDiscord(value.discordId));
});

test('VIP-only link is verified but never creates a seeding account or points', async (t) => {
  const { database, crypto } = await fixture(t);
  const value = challenge(database, crypto, { purpose: 'donation' });
  assert.throws(() => database.verifyLinkChallenge({ discordId: value.discordId, code: value.code,
    maxAttempts: 5, purpose: 'seeding' }),
  (error) => error instanceof DomainError && error.code === 'wrong_link_purpose');
  const donor = database.verifyLinkChallenge({ discordId: value.discordId, code: value.code,
    maxAttempts: 5, purpose: 'donation' });
  assert.equal(donor.purpose, 'donation');
  assert.equal(database.getVipIdentityByDiscord(value.discordId).steamId, value.steamId);
  assert.equal(database.countDonorLinks(), 1);
  assert.equal(database.getUserByDiscord(value.discordId), null);
  assert.equal(database.getUserBySteam(value.steamId), null);
  assert.equal(database.listSeeders('2026-09-21').length, 0);
  database.applySeedingTick({ eligibleMembers: [{ discordId: value.discordId,
    displayName: 'Tester' }], now: Date.now(), weekKey: '2026-09-21', graceSeconds: 180,
  maxDeltaSeconds: 360 });
  assert.equal(database.getLeaderboard('2026-09-21').length, 0);
  assert.equal(database.verifyIntegrity().users, 0);
  assert.throws(() => challenge(database, crypto, { discordId: '222222222222222222',
    steamId: value.steamId }), (error) => error instanceof DomainError && error.code === 'steam_taken');
});

test('VIP-only link can be promoted to seeding only with a second verified code', async (t) => {
  const { database, crypto } = await fixture(t);
  const donorChallenge = challenge(database, crypto, { purpose: 'donation' });
  database.verifyLinkChallenge({ discordId: donorChallenge.discordId, code: donorChallenge.code,
    maxAttempts: 5, purpose: 'donation' });
  const seedChallenge = challenge(database, crypto, donorChallenge);
  const seeder = database.verifyLinkChallenge({ discordId: seedChallenge.discordId,
    code: seedChallenge.code, maxAttempts: 5 });
  assert.equal(seeder.linked, true);
  assert.equal(database.getDonorByDiscord(seedChallenge.discordId), null);
  assert.equal(database.getVipIdentityByDiscord(seedChallenge.discordId).id, seeder.id);
  assert.equal(database.listSeeders('2026-09-21').length, 1);
  assert.equal(database.verifyIntegrity().users, 1);
});

test('VIP-only link cannot be transferred while paid VIP or its slot is active', async (t) => {
  const { database, crypto } = await fixture(t);
  const value = challenge(database, crypto, { purpose: 'donation' });
  database.verifyLinkChallenge({ discordId: value.discordId, code: value.code,
    maxAttempts: 5, purpose: 'donation' });
  database.setState('donation_feed', { vip: { [value.discordId]: { endsAt: Date.now() + 60_000 } } });
  assert.throws(() => database.unlinkDonorDiscord({ discordId: value.discordId }),
    (error) => error instanceof DomainError && error.code === 'paid_vip_active');
  database.setState('donation_feed', { vip: {} });
  database.setState('paid_slot_claims', { [value.steamId]: { discordId: value.discordId, owner: 'bot' } });
  assert.throws(() => database.unlinkDonorDiscord({ discordId: value.discordId }),
    (error) => error instanceof DomainError && error.code === 'paid_vip_active');
  database.setState('paid_slot_claims', {});
  database.unlinkDonorDiscord({ discordId: value.discordId });
  assert.equal(database.getVipIdentityByDiscord(value.discordId), null);
  assert.equal(database.countDonorLinks(), 0);
});

test('donation identity is encrypted and detected if modified', async (t) => {
  const { database, crypto } = await fixture(t);
  const value = challenge(database, crypto, { purpose: 'donation' });
  const donor = database.verifyLinkChallenge({ discordId: value.discordId, code: value.code,
    maxAttempts: 5, purpose: 'donation' });
  const row = database.db.prepare('SELECT payload_enc FROM donor_links WHERE id = ?').get(donor.id);
  assert.doesNotMatch(row.payload_enc, /76561198000000001/);
  database.db.prepare('UPDATE donor_links SET payload_enc = ? WHERE id = ?').run(row.payload_enc + 'x', donor.id);
  assert.throws(() => database.verifyIntegrity());
});

test('archived points cannot be moved to a different Steam account automatically', async (t) => {
  const { database, crypto } = await fixture(t);
  const value = challenge(database, crypto);
  database.verifyLinkChallenge({ discordId: value.discordId, code: value.code, maxAttempts: 5 });
  database.unlinkDiscord({ discordId: value.discordId });
  const replacement = challenge(database, crypto, {
    discordId: value.discordId,
    steamId: '76561198000000002'
  });
  assert.throws(
    () => database.verifyLinkChallenge({
      discordId: replacement.discordId,
      code: replacement.code,
      maxAttempts: 5
    }),
    (error) => error instanceof DomainError && error.code === 'identity_history_conflict'
  );
  assert.equal(database.verifyIntegrity().archivedUsers, 1);
});

test('version 1 database is migrated without manual intervention', async (t) => {
  const directory = await mkdtemp(join(tmpdir(), 'wardogs-migration-test-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const path = join(directory, 'legacy.sqlite');
  const legacy = openDatabase(path);
  legacy.exec(`
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT;
    INSERT INTO meta (key, value) VALUES ('schema_version', '1');
    CREATE TABLE users (
      id TEXT PRIMARY KEY,
      discord_idx TEXT NOT NULL UNIQUE,
      steam_idx TEXT NOT NULL UNIQUE,
      payload_enc TEXT NOT NULL,
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL
    ) STRICT;
  `);
  legacy.close();

  const database = new SeedingDatabase({
    path,
    crypto: new CryptoBox(randomBytes(32)),
    guildId: '1552253117586739210',
    logger: { info() {}, warn() {}, error() {} }
  });
  t.after(() => database.close());
  const columns = database.db.prepare('PRAGMA table_info(users)').all().map((row) => row.name);
  assert.ok(columns.includes('linked'));
  assert.equal(database.db.prepare("SELECT value FROM meta WHERE key = 'schema_version'").get().value, '3');
  assert.ok(database.db.prepare("SELECT name FROM sqlite_master WHERE name = 'donor_links'").get());
});

test('version 2 database opens with separate VIP links without losing seeding data', async (t) => {
  const directory = await mkdtemp(join(tmpdir(), 'wardogs-vip-migration-test-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const path = join(directory, 'existing.sqlite');
  const crypto = new CryptoBox(randomBytes(32));
  const original = new SeedingDatabase({ path, crypto, guildId: '1552253117586739210',
    logger: { info() {}, warn() {}, error() {} } });
  const value = challenge(original, crypto);
  original.verifyLinkChallenge({ discordId: value.discordId, code: value.code, maxAttempts: 5 });
  original.db.prepare("UPDATE meta SET value = '2' WHERE key = 'schema_version'").run();
  original.close();
  const upgraded = new SeedingDatabase({ path, crypto, guildId: '1552253117586739210',
    logger: { info() {}, warn() {}, error() {} } });
  t.after(() => upgraded.close());
  assert.equal(upgraded.db.prepare("SELECT value FROM meta WHERE key = 'schema_version'").get().value, '3');
  assert.equal(upgraded.getUserByDiscord(value.discordId).steamId, value.steamId);
  assert.equal(upgraded.countDonorLinks(), 0);
  assert.equal(upgraded.verifyIntegrity().users, 1);
});

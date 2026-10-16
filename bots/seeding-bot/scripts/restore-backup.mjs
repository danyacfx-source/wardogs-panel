import { readFile, writeFile, rename, unlink, mkdir } from 'node:fs/promises';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import process from 'node:process';
import { decryptBackup } from '../src/backup-crypto.js';
import { openDatabase } from '../src/sqlite.js';

function secret(name) {
  const inline = process.env[name]?.trim();
  const file = process.env[`${name}_FILE`]?.trim();
  if (inline && file) throw new Error(`Set only ${name} or ${name}_FILE`);
  return inline || (file ? readFileSync(file, 'utf8').trim() : '');
}

const [backupArgument, targetArgument] = process.argv.slice(2);
if (!backupArgument || !targetArgument) {
  throw new Error('Usage: npm run restore -- /path/backup.wdsbak /path/new-seeding.sqlite');
}
const backupPath = resolve(backupArgument);
const targetPath = resolve(targetArgument);
if (!targetPath.endsWith('.sqlite')) throw new Error('Restore target must end with .sqlite');
if (existsSync(targetPath)) throw new Error('Restore target already exists; it will not be overwritten');

const rawKey = secret('BACKUP_ENCRYPTION_KEY');
const backupKey = Buffer.from(rawKey, 'base64');
if (backupKey.length !== 32 || backupKey.toString('base64').replace(/=+$/, '') !== rawKey.replace(/=+$/, '')) {
  throw new Error('BACKUP_ENCRYPTION_KEY must be exactly 32 random bytes encoded as base64');
}
const plain = decryptBackup(await readFile(backupPath), backupKey);
if (!plain.subarray(0, 16).equals(Buffer.from('SQLite format 3\0', 'binary'))) {
  throw new Error('Decrypted content is not a SQLite database');
}

await mkdir(dirname(targetPath), { recursive: true, mode: 0o700 });
const temporaryPath = `${targetPath}.restore-${process.pid}.tmp`;
try {
  await writeFile(temporaryPath, plain, { mode: 0o600, flag: 'wx' });
  const database = openDatabase(temporaryPath, { readOnly: true });
  const result = database.prepare('PRAGMA integrity_check').get();
  database.close();
  if (result.integrity_check !== 'ok') throw new Error('Restored SQLite integrity check failed');
  await rename(temporaryPath, targetPath);
  console.log(`Backup restored and verified to ${targetPath}`);
  console.log('Keep the bot stopped while replacing its active database.');
} catch (error) {
  await unlink(temporaryPath).catch(() => {});
  throw error;
}

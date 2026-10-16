import { createCipheriv, createDecipheriv, hkdfSync, randomBytes } from 'node:crypto';

const MAGIC = Buffer.from('WDSBAK1\n', 'ascii');
const AAD = Buffer.from('wardogs-seeding-backup:v1', 'utf8');
const SALT = Buffer.from('wardogs-seeding-backup-key:v1', 'utf8');

function derive(key) {
  return Buffer.from(hkdfSync('sha256', key, SALT, Buffer.from('aes-256-gcm', 'utf8'), 32));
}

export function encryptBackup(sqliteBytes, backupKey) {
  const iv = randomBytes(12);
  const cipher = createCipheriv('aes-256-gcm', derive(backupKey), iv, { authTagLength: 16 });
  cipher.setAAD(AAD);
  const ciphertext = Buffer.concat([cipher.update(sqliteBytes), cipher.final()]);
  return Buffer.concat([MAGIC, iv, cipher.getAuthTag(), ciphertext]);
}

export function decryptBackup(backupBytes, backupKey) {
  const input = Buffer.from(backupBytes);
  if (input.length < MAGIC.length + 12 + 16 || !input.subarray(0, MAGIC.length).equals(MAGIC)) {
    throw new Error('This is not a WARDOGS seeding backup');
  }
  const ivStart = MAGIC.length;
  const tagStart = ivStart + 12;
  const dataStart = tagStart + 16;
  const decipher = createDecipheriv('aes-256-gcm', derive(backupKey), input.subarray(ivStart, tagStart), {
    authTagLength: 16
  });
  decipher.setAAD(AAD);
  decipher.setAuthTag(input.subarray(tagStart, dataStart));
  return Buffer.concat([decipher.update(input.subarray(dataStart)), decipher.final()]);
}

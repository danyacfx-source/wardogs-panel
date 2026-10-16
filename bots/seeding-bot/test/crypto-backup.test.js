import test from 'node:test';
import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';
import { CryptoBox } from '../src/crypto-box.js';
import { decryptBackup, encryptBackup } from '../src/backup-crypto.js';

test('field encryption authenticates value and context', () => {
  const box = new CryptoBox(randomBytes(32));
  const sealed = box.seal('secret value', 'user:1');
  assert.equal(box.open(sealed, 'user:1'), 'secret value');
  assert.throws(() => box.open(sealed, 'user:2'));
  const parts = sealed.split('.');
  const ciphertext = Buffer.from(parts[2], 'base64url');
  ciphertext[0] ^= 1;
  parts[2] = ciphertext.toString('base64url');
  const changed = parts.join('.');
  assert.throws(() => box.open(changed, 'user:1'));
});

test('backup encryption round-trips and rejects a wrong key', () => {
  const key = randomBytes(32);
  const input = Buffer.from('SQLite format 3\0test bytes');
  const encrypted = encryptBackup(input, key);
  assert.deepEqual(decryptBackup(encrypted, key), input);
  assert.throws(() => decryptBackup(encrypted, randomBytes(32)));
});

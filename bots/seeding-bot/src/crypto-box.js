import {
  createCipheriv,
  createDecipheriv,
  createHash,
  createHmac,
  hkdfSync,
  randomBytes,
  randomInt,
  timingSafeEqual
} from 'node:crypto';

const FORMAT = 'v1';
const SALT = Buffer.from('wardogs-seeding-bot:v1', 'utf8');

function derive(master, label) {
  return Buffer.from(hkdfSync('sha256', master, SALT, Buffer.from(label, 'utf8'), 32));
}

function b64(value) {
  return Buffer.from(value).toString('base64url');
}

function unb64(value) {
  return Buffer.from(value, 'base64url');
}

export class CryptoBox {
  constructor(masterKey) {
    this.encryptionKey = derive(masterKey, 'field-encryption');
    this.indexKey = derive(masterKey, 'blind-index');
    this.auditKey = derive(masterKey, 'tamper-evident-chain');
    this.challengeKey = derive(masterKey, 'link-challenges');
    this.fingerprint = createHash('sha256').update(masterKey).digest('hex').slice(0, 16);
  }

  seal(value, context) {
    const plaintext = Buffer.from(String(value), 'utf8');
    const iv = randomBytes(12);
    const cipher = createCipheriv('aes-256-gcm', this.encryptionKey, iv, { authTagLength: 16 });
    cipher.setAAD(Buffer.from(`${FORMAT}\0${context}`, 'utf8'));
    const ciphertext = Buffer.concat([cipher.update(plaintext), cipher.final()]);
    const tag = cipher.getAuthTag();
    return `${FORMAT}.${b64(iv)}.${b64(ciphertext)}.${b64(tag)}`;
  }

  open(envelope, context) {
    const parts = String(envelope).split('.');
    if (parts.length !== 4 || parts[0] !== FORMAT) throw new Error('Unsupported encrypted value');
    const [, ivRaw, ciphertextRaw, tagRaw] = parts;
    const iv = unb64(ivRaw);
    const ciphertext = unb64(ciphertextRaw);
    const tag = unb64(tagRaw);
    if (iv.length !== 12 || tag.length !== 16) throw new Error('Invalid encrypted value');
    const decipher = createDecipheriv('aes-256-gcm', this.encryptionKey, iv, { authTagLength: 16 });
    decipher.setAAD(Buffer.from(`${FORMAT}\0${context}`, 'utf8'));
    decipher.setAuthTag(tag);
    return Buffer.concat([decipher.update(ciphertext), decipher.final()]).toString('utf8');
  }

  sealJson(value, context) {
    return this.seal(JSON.stringify(value), context);
  }

  openJson(envelope, context) {
    return JSON.parse(this.open(envelope, context));
  }

  blindIndex(value, context) {
    return createHmac('sha256', this.indexKey)
      .update(context)
      .update('\0')
      .update(String(value).trim())
      .digest('base64url');
  }

  challengeDigest(discordIndex, code) {
    return createHmac('sha256', this.challengeKey)
      .update(discordIndex)
      .update('\0')
      .update(String(code).trim().toUpperCase())
      .digest('base64url');
  }

  chainDigest({ domain, previous, id, timestamp, kind, actor, payload }) {
    return createHmac('sha256', this.auditKey)
      .update(domain)
      .update('\0')
      .update(previous || '')
      .update('\0')
      .update(String(id))
      .update('\0')
      .update(String(timestamp))
      .update('\0')
      .update(kind || '')
      .update('\0')
      .update(actor || '')
      .update('\0')
      .update(payload || '')
      .digest('base64url');
  }

  safeEqual(left, right) {
    const a = Buffer.from(String(left));
    const b = Buffer.from(String(right));
    return a.length === b.length && timingSafeEqual(a, b);
  }
}

export function generateHumanCode(length = 8) {
  const alphabet = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ';
  let out = '';
  for (let index = 0; index < length; index += 1) out += alphabet[randomInt(alphabet.length)];
  return out;
}

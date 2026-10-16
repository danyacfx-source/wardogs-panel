import { randomUUID } from 'node:crypto';

const LEVELS = { debug: 10, info: 20, warn: 30, error: 40 };
const SENSITIVE_KEY = /token|password|secret|authorization|cookie|code|steam|discord|rcon.*body/i;

function scrub(value, secrets, depth = 0) {
  if (depth > 5) return '[depth-limit]';
  if (value === null || value === undefined || typeof value === 'number' || typeof value === 'boolean') {
    return value;
  }
  if (typeof value === 'string') {
    let out = value.replace(/Bearer\s+[A-Za-z0-9._~+/=-]+/gi, 'Bearer [redacted]');
    out = out.replace(/7656119\d{10}/g, '[steam-id]');
    for (const secret of secrets) {
      if (secret && secret.length >= 8) out = out.split(secret).join('[redacted]');
    }
    return out.slice(0, 2000);
  }
  if (value instanceof Error) {
    const error = {
      name: value.name,
      message: scrub(value.message, secrets, depth + 1)
    };
    if (typeof value.code === 'string') error.code = scrub(value.code, secrets, depth + 1);
    if (Number.isInteger(value.status) && value.status > 0) error.status = value.status;
    if (typeof value.networkCode === 'string' && value.networkCode) {
      error.networkCode = scrub(value.networkCode, secrets, depth + 1);
    }
    if (typeof value.route === 'string' && value.route) {
      error.route = scrub(value.route, secrets, depth + 1);
    }
    return error;
  }
  if (Array.isArray(value)) return value.slice(0, 50).map((item) => scrub(item, secrets, depth + 1));
  if (typeof value === 'object') {
    const out = {};
    for (const [key, item] of Object.entries(value)) {
      out[key] = SENSITIVE_KEY.test(key) ? '[redacted]' : scrub(item, secrets, depth + 1);
    }
    return out;
  }
  return String(value);
}

export function createLogger({ level = 'info', secrets = [] } = {}) {
  const threshold = LEVELS[level] ?? LEVELS.info;
  const write = (entryLevel, event, fields = {}) => {
    if (LEVELS[entryLevel] < threshold) return;
    const row = {
      timestamp: new Date().toISOString(),
      level: entryLevel,
      event,
      ...scrub(fields, secrets)
    };
    const line = JSON.stringify(row);
    if (entryLevel === 'error') console.error(line);
    else if (entryLevel === 'warn') console.warn(line);
    else console.log(line);
  };
  return {
    debug: (event, fields) => write('debug', event, fields),
    info: (event, fields) => write('info', event, fields),
    warn: (event, fields) => write('warn', event, fields),
    error: (event, fields) => write('error', event, fields),
    correlationId: () => randomUUID().slice(0, 12)
  };
}

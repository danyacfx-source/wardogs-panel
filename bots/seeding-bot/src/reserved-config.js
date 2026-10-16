const SESSION_SECTION = '/Script/WDGame.WDGameSession';
const RESERVED_KEY = 'DefaultReservedPlayerIds';
const STEAM_ID = /^7656119\d{10}$/;

function isHeader(line) {
  const value = line.trim();
  return value.startsWith('[') && value.endsWith(']');
}

function headerName(line) {
  const value = line.trim();
  return value.slice(1, -1).trim();
}

function keyName(line) {
  const eq = line.indexOf('=');
  if (eq < 1) return '';
  return line
    .slice(0, eq)
    .trim()
    .replace(/^[+.!-]/, '')
    .toLowerCase();
}

function unquote(value) {
  const trimmed = value.trim();
  return trimmed.startsWith('"') && trimmed.endsWith('"') ? trimmed.slice(1, -1) : trimmed;
}

export function readReservedIds(text) {
  const lines = String(text).replace(/^\uFEFF/, '').split(/\r\n|\n|\r/);
  let inSection = false;
  let ids = [];
  for (const raw of lines) {
    const line = raw.trim();
    if (isHeader(line)) {
      inSection = headerName(line).toLowerCase() === SESSION_SECTION.toLowerCase();
      continue;
    }
    if (!inSection || !line || line.startsWith(';') || line.startsWith('#')) continue;
    const eq = line.indexOf('=');
    if (eq < 1 || keyName(line) !== RESERVED_KEY.toLowerCase()) continue;
    const command = line.trimStart()[0];
    const id = unquote(line.slice(eq + 1));
    if (command === '!') ids = [];
    else if (command === '-') ids = ids.filter((value) => value !== id);
    else if (STEAM_ID.test(id) && !ids.includes(id)) ids.push(id);
  }
  return ids;
}

export function writeReservedIds(text, ids) {
  const normalized = [...new Set(ids.map(String))];
  if (normalized.some((id) => !STEAM_ID.test(id))) throw new Error('Invalid SteamID64 in reserved list');

  const source = String(text);
  const hasBom = source.startsWith('\uFEFF');
  const eol = source.includes('\r\n') ? '\r\n' : '\n';
  const lines = source.replace(/^\uFEFF/, '').split(/\r\n|\n|\r/);
  let sectionStart = -1;
  let sectionEnd = lines.length;
  for (let index = 0; index < lines.length; index += 1) {
    if (!isHeader(lines[index])) continue;
    if (sectionStart >= 0) {
      sectionEnd = index;
      break;
    }
    if (headerName(lines[index]).toLowerCase() === SESSION_SECTION.toLowerCase()) sectionStart = index;
  }

  const block = [
    `!${RESERVED_KEY}=ClearArray`,
    ...normalized.map((id) => `.${RESERVED_KEY}=${id}`)
  ];

  if (sectionStart < 0) {
    while (lines.length && !lines.at(-1).trim()) lines.pop();
    if (lines.length) lines.push('');
    lines.push(`[${SESSION_SECTION}]`, ...block, '');
    return `${hasBom ? '\uFEFF' : ''}${lines.join(eol)}`;
  }

  let insertion = -1;
  const kept = [];
  for (let index = 0; index < lines.length; index += 1) {
    const inside = index > sectionStart && index < sectionEnd;
    const ownLine = inside && keyName(lines[index]) === RESERVED_KEY.toLowerCase();
    if (ownLine) {
      if (insertion < 0) insertion = kept.length;
      continue;
    }
    kept.push(lines[index]);
  }
  if (insertion < 0) {
    insertion = sectionEnd;
    while (insertion > sectionStart + 1 && !kept[insertion - 1].trim()) insertion -= 1;
  }
  kept.splice(insertion, 0, ...block);
  return `${hasBom ? '\uFEFF' : ''}${kept.join(eol)}`;
}


import { readdir, readFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('..', import.meta.url)));
const directories = ['src', 'scripts', 'test'];
const files = [];

async function walk(path) {
  for (const entry of await readdir(path, { withFileTypes: true })) {
    const child = join(path, entry.name);
    if (entry.isDirectory()) await walk(child);
    else if (entry.name.endsWith('.js') || entry.name.endsWith('.mjs')) files.push(child);
  }
}
for (const directory of directories) await walk(join(root, directory));

const forbidden = [
  { name: 'Discord bot token shape', pattern: /(?:MTA|MTI|MTM|MTQ|MTU|MTY|MTc|MTg|MTk|MjA)[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{20,}/ },
  { name: 'private key', pattern: /-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/ },
  { name: 'hard-coded Authorization bearer', pattern: /Authorization\s*:\s*['"]Bearer\s+[A-Za-z0-9._~+/=-]{16,}/i }
];
let failed = false;
for (const file of files) {
  const text = await readFile(file, 'utf8');
  for (const check of forbidden) {
    if (check.pattern.test(text)) {
      console.error(`${check.name} found in ${file}`);
      failed = true;
    }
  }
  const syntax = spawnSync(process.execPath, ['--check', file], { encoding: 'utf8' });
  if (syntax.status !== 0) {
    console.error(syntax.stderr || `Syntax check failed: ${file}`);
    failed = true;
  }
}
if (failed) process.exitCode = 1;
else console.log(`Source check passed (${files.length} JavaScript files).`);

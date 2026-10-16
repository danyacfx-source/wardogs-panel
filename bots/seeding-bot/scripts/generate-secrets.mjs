import { chmod, mkdir, writeFile } from 'node:fs/promises';
import { randomBytes } from 'node:crypto';
import { resolve, join } from 'node:path';

const argument = process.argv.indexOf('--dir');
if (argument >= 0 && !process.argv[argument + 1]) throw new Error('--dir requires a directory path');
const directory = resolve(argument >= 0 ? process.argv[argument + 1] : 'secrets');
if (directory === resolve('/')) throw new Error('Refusing to use the filesystem root as a secrets directory');
await mkdir(directory, { recursive: true, mode: 0o700 });
await chmod(directory, 0o700);

async function create(name, value) {
  const path = join(directory, name);
  try {
    await writeFile(path, `${value}\n`, { encoding: 'utf8', mode: 0o600, flag: 'wx' });
    return `created ${path}`;
  } catch (error) {
    if (error.code === 'EEXIST') {
      await chmod(path, 0o600);
      return `kept existing ${path}`;
    }
    throw error;
  }
}

const results = await Promise.all([
  create('data_encryption_key', randomBytes(32).toString('base64')),
  create('backup_encryption_key', randomBytes(32).toString('base64')),
  create('rcon_password', randomBytes(36).toString('base64url'))
]);
console.log(results.join('\n'));
console.log(`
Secret values were not printed.

Copy the content of each file to the matching BotHost variable:
  data_encryption_key   -> DATA_ENCRYPTION_KEY
  backup_encryption_key -> BACKUP_ENCRYPTION_KEY
  rcon_password         -> WARDOGS_RCON_PASSWORD and Qonzer WDRCON Password=

The Discord token is created only in Discord Developer Portal; this script cannot generate it.
Do not upload this directory with the bot. Keep the two encryption keys in an offline password vault.`);

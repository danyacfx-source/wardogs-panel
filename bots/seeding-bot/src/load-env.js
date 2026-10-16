import { existsSync } from 'node:fs';
import { loadEnvFile } from 'node:process';

// BotHost supplies real secrets through process.env. A local .env is supported for
// development only and is excluded from Git, Docker builds, npm packages and release ZIPs.
export function loadLocalEnv() {
  if (existsSync('.env')) loadEnvFile('.env');
}


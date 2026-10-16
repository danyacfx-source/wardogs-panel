'use strict';

const path = require('node:path');
const { CHANNELS } = require('./catalog');
const needed = (env, key) => { const v = env[key]?.trim(); if (!v) throw new Error(`${key} is required`); return v; };
function loadConfig(env = process.env) {
  const config = {
    botToken: needed(env, 'BOT_TOKEN'), clientId: needed(env, 'CLIENT_ID'),
    guildId: env.GUILD_ID?.trim() || '403186008579112973',
    siteBaseUrl: needed(env, 'SITE_BASE_URL'),
    checkoutSecret: needed(env, 'CHECKOUT_SECRET'), feedToken: needed(env, 'FEED_TOKEN'),
    dataDir: env.DATA_DIR?.trim() || path.join(process.cwd(), 'data'),
    pollSeconds: Number(env.POLL_SECONDS || 45), channels: CHANNELS,
  };
  if (!/^\d{17,20}$/.test(config.clientId) || !/^\d{17,20}$/.test(config.guildId)) throw new Error('Invalid Discord ID');
  if (config.checkoutSecret.length < 32 || config.feedToken.length < 32) throw new Error('Secrets must be at least 32 characters');
  if (!Number.isInteger(config.pollSeconds) || config.pollSeconds < 30 || config.pollSeconds > 600) throw new Error('POLL_SECONDS must be 30–600');
  if (new URL(config.siteBaseUrl).protocol !== 'https:') throw new Error('SITE_BASE_URL must be HTTPS');
  return config;
}
module.exports = { loadConfig };

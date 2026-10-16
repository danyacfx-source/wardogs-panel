import { isAbsolute, resolve } from 'node:path';
import { readFileSync } from 'node:fs';

const SNOWFLAKE = /^\d{17,20}$/;
const CLOCK = /^(?:[01]\d|2[0-3]):[0-5]\d$/;

function required(env, name) {
  const inline = env[name]?.trim();
  const file = env[`${name}_FILE`]?.trim();
  if (inline && file) throw new Error(`Set only one of ${name} or ${name}_FILE`);
  let value = inline;
  if (file) {
    if (/\0/.test(file)) throw new Error(`${name}_FILE contains a forbidden character`);
    try {
      value = readFileSync(file, 'utf8').trim();
    } catch {
      throw new Error(`Cannot read secret file configured by ${name}_FILE`);
    }
  }
  if (!value) throw new Error(`Missing required secret: ${name} or ${name}_FILE`);
  if (/[\r\n\0]/.test(value)) throw new Error(`${name} contains forbidden control characters`);
  return value;
}

function discordToken(env) {
  const canonical = env.DISCORD_TOKEN?.trim();
  const platform = env.DISCORD_BOT_TOKEN?.trim();
  const file = env.DISCORD_TOKEN_FILE?.trim();
  if (canonical && platform && canonical !== platform) {
    throw new Error('DISCORD_TOKEN and DISCORD_BOT_TOKEN contain different values');
  }
  if (file && platform) {
    throw new Error('Set only DISCORD_TOKEN_FILE or the BotHost DISCORD_BOT_TOKEN');
  }
  if (canonical || file) return required(env, 'DISCORD_TOKEN');
  return required(env, 'DISCORD_BOT_TOKEN');
}

function optional(env, name, fallback = '') {
  const value = env[name]?.trim();
  if (!value) return fallback;
  if (/[\r\n\0]/.test(value)) throw new Error(`${name} contains forbidden control characters`);
  return value;
}

function integer(env, name, fallback, { min, max }) {
  const raw = optional(env, name, String(fallback));
  if (!/^-?\d+$/.test(raw)) throw new Error(`${name} must be an integer`);
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value < min || value > max) {
    throw new Error(`${name} must be between ${min} and ${max}`);
  }
  return value;
}

function boolean(env, name, fallback = false) {
  const raw = optional(env, name, fallback ? 'true' : 'false').toLowerCase();
  if (!['true', 'false'].includes(raw)) throw new Error(`${name} must be true or false`);
  return raw === 'true';
}

function snowflake(env, name, fallback = undefined) {
  const value = fallback === undefined ? required(env, name) : optional(env, name, fallback);
  if (value && !SNOWFLAKE.test(value)) throw new Error(`${name} must be a Discord snowflake`);
  return value;
}

function snowflakeList(env, name, fallback = undefined) {
  const raw = fallback === undefined ? required(env, name) : optional(env, name, fallback);
  const values = raw
    .split(',')
    .map((value) => value.trim())
    .filter(Boolean);
  if (!values.length || values.some((value) => !SNOWFLAKE.test(value))) {
    throw new Error(`${name} must contain comma-separated Discord role IDs`);
  }
  return [...new Set(values)];
}

function base64Key(env, name) {
  const raw = required(env, name);
  let key;
  try {
    key = Buffer.from(raw, 'base64');
  } catch {
    throw new Error(`${name} must be base64 encoded`);
  }
  if (key.length !== 32 || key.toString('base64').replace(/=+$/, '') !== raw.replace(/=+$/, '')) {
    throw new Error(`${name} must be exactly 32 random bytes encoded as base64`);
  }
  return key;
}

function clock(env, name, fallback) {
  const value = optional(env, name, fallback);
  if (!CLOCK.test(value)) throw new Error(`${name} must use HH:MM (24-hour) format`);
  const [hour, minute] = value.split(':').map(Number);
  return { text: value, minutes: hour * 60 + minute };
}

function rconUrl(env, allowInsecure, name = 'WARDOGS_RCON_URL', fallback = '') {
  const configured = optional(env, name, fallback);
  if (!configured) throw new Error(`Missing required setting: ${name}`);
  const raw = configured.replace(/\/+$/, '');
  let url;
  try {
    url = new URL(raw);
  } catch {
    throw new Error(`${name} is not a valid URL`);
  }
  if (!['http:', 'https:'].includes(url.protocol)) {
    throw new Error(`${name} must use http:// or https://`);
  }
  if (url.protocol === 'http:' && !allowInsecure) {
    throw new Error(
      'Plain HTTP RCON is disabled. Use an HTTPS endpoint or explicitly set RCON_ALLOW_INSECURE_HTTP=true after applying an IP firewall rule.'
    );
  }
  if (url.username || url.password || url.search || url.hash || !['', '/'].includes(url.pathname)) {
    throw new Error(`${name} must contain only scheme, host and port`);
  }
  if (
    url.hostname.toLowerCase() === 'rcon.wardogs.com'
    && (!url.port || url.port === '80' || url.port === '443')
  ) {
    throw new Error(
      `${name} points to the public HTML control panel. Use the game server address and the WDRCON port from WDRCONSettings.`
    );
  }
  return url;
}

export function readConfig(env = process.env) {
  const allowInsecureHttp = boolean(env, 'RCON_ALLOW_INSECURE_HTTP', false);
  const masterKey = base64Key(env, 'DATA_ENCRYPTION_KEY');
  const backupKey = base64Key(env, 'BACKUP_ENCRYPTION_KEY');
  if (masterKey.equals(backupKey)) {
    throw new Error('DATA_ENCRYPTION_KEY and BACKUP_ENCRYPTION_KEY must be different');
  }

  const start = clock(env, 'SEED_START_TIME', '08:00');
  const end = clock(env, 'SEED_END_TIME', '22:00');
  if (start.minutes >= end.minutes) throw new Error('SEED_START_TIME must be earlier than SEED_END_TIME');

  const dataDirRaw = optional(env, 'DATA_DIR', '/app/data');
  const dataDir = isAbsolute(dataDirRaw) ? dataDirRaw : resolve(dataDirRaw);
  const backupDirRaw = optional(env, 'BACKUP_DIR', `${dataDir}/backups`);
  const backupDir = isAbsolute(backupDirRaw) ? backupDirRaw : resolve(backupDirRaw);

  const tlsPin = optional(env, 'RCON_TLS_CERT_SHA256').replace(/:/g, '').toUpperCase();
  if (tlsPin && !/^[A-F0-9]{64}$/.test(tlsPin)) {
    throw new Error('RCON_TLS_CERT_SHA256 must be a SHA-256 certificate fingerprint');
  }
  const secondaryTlsPin = optional(env, 'RCON_2_TLS_CERT_SHA256').replace(/:/g, '').toUpperCase();
  if (secondaryTlsPin && !/^[A-F0-9]{64}$/.test(secondaryTlsPin)) {
    throw new Error('RCON_2_TLS_CERT_SHA256 must be a SHA-256 certificate fingerprint');
  }

  const donationUrlRaw = optional(env, 'DONATION_SITE_BASE_URL');
  const donationFeedToken = optional(env, 'DONATION_FEED_TOKEN');
  const donationCheckoutSecret = optional(env, 'DONATION_CHECKOUT_SECRET');
  if (Boolean(donationUrlRaw) !== Boolean(donationFeedToken) ||
      Boolean(donationUrlRaw) !== Boolean(donationCheckoutSecret)) {
    throw new Error('Configure all three DONATION_* variables together');
  }
  let donationUrl = null;
  if (donationUrlRaw) {
    donationUrl = new URL(donationUrlRaw);
    if (donationUrl.protocol !== 'https:' || donationUrl.username || donationUrl.password ||
        donationUrl.search || donationUrl.hash || !['', '/'].includes(donationUrl.pathname) ||
        donationFeedToken.length < 32 || donationCheckoutSecret.length < 32) {
      throw new Error('Donation site requires an HTTPS origin and two long secrets');
    }
  }

  const config = {
    discord: {
      token: discordToken(env),
      clientId: snowflake(env, 'DISCORD_CLIENT_ID'),
      guildId: snowflake(env, 'DISCORD_GUILD_ID', '403186008579112973'),
      adminRoleIds: snowflakeList(
        env,
        'DISCORD_ADMIN_ROLE_IDS',
        '805429609457254450,815605974412558376'
      ),
      gatherRoleIds: ['859738052666327040'],
      seederRoleId: snowflake(env, 'SEEDER_ROLE_ID', '1552728825568174090'),
      superSeederRoleId: snowflake(env, 'SUPER_SEEDER_ROLE_ID', '1552841261713719440'),
      vipRoleId: snowflake(env, 'VIP_ROLE_ID', '1552725871830503484'),
      roleReconcileSeconds: integer(env, 'DISCORD_ROLE_RECONCILE_SECONDS', 300, {
        min: 60,
        max: 3600
      }),
      voiceChannelId: snowflake(env, 'SEED_VOICE_CHANNEL_ID', '1552253117586739210'),
      rankChannelId: snowflake(env, 'SEED_RANK_CHANNEL_ID', '1552254053109727262'),
      shopChannelId: snowflake(env, 'SEED_SHOP_CHANNEL_ID', '1552255043757936721'),
      rulesChannelId: snowflake(env, 'SEED_RULES_CHANNEL_ID', '1552254112060411944'),
      auditChannelId: snowflake(env, 'AUDIT_CHANNEL_ID', '1552402077068820611'),
      backupChannelId: snowflake(env, 'BACKUP_CHANNEL_ID', '1552401803184701502')
    },
    rcon: {
      url: rconUrl(env, allowInsecureHttp),
      password: required(env, 'WARDOGS_RCON_PASSWORD'),
      allowInsecureHttp,
      tlsPin,
      timeoutMs: integer(env, 'RCON_TIMEOUT_MS', 6000, { min: 1000, max: 30000 }),
      retryAttempts: integer(env, 'RCON_RETRY_ATTEMPTS', 1, { min: 0, max: 3 }),
      retryBaseMs: integer(env, 'RCON_RETRY_BASE_MS', 300, { min: 100, max: 2000 }),
      outageLogIntervalSeconds: integer(env, 'RCON_OUTAGE_LOG_INTERVAL_SECONDS', 900, {
        min: 60,
        max: 3600
      }),
      maxResponseBytes: integer(env, 'RCON_MAX_RESPONSE_BYTES', 1_048_576, {
        min: 65536,
        max: 4_194_304
      })
    },
    rconSecondary: {
      url: rconUrl(env, allowInsecureHttp, 'WARDOGS_RCON_2_URL', 'http://216.144.249.92:7789'),
      password: required(env, 'WARDOGS_RCON_2_PASSWORD'),
      allowInsecureHttp,
      tlsPin: secondaryTlsPin,
      timeoutMs: integer(env, 'RCON_TIMEOUT_MS', 6000, { min: 1000, max: 30000 }),
      retryAttempts: integer(env, 'RCON_RETRY_ATTEMPTS', 1, { min: 0, max: 3 }),
      retryBaseMs: integer(env, 'RCON_RETRY_BASE_MS', 300, { min: 100, max: 2000 }),
      maxResponseBytes: integer(env, 'RCON_MAX_RESPONSE_BYTES', 1_048_576, {
        min: 65536,
        max: 4_194_304
      })
    },
    security: {
      masterKey,
      backupKey,
      challengeMinutes: integer(env, 'LINK_CHALLENGE_MINUTES', 5, { min: 2, max: 30 }),
      challengeMaxAttempts: integer(env, 'LINK_CHALLENGE_MAX_ATTEMPTS', 5, { min: 1, max: 10 })
    },
    seed: {
      timezone: 'Europe/Moscow',
      start,
      end,
      stopPlayerCount: integer(env, 'SEED_STOP_PLAYER_COUNT', 20, { min: 2, max: 100 }),
      resumeBelowSeconds: integer(env, 'SEED_RESUME_BELOW_SECONDS', 300, { min: 0, max: 3600 }),
      tickSeconds: integer(env, 'SEED_TICK_SECONDS', 30, { min: 15, max: 120 }),
      sessionGraceSeconds: integer(env, 'SEED_SESSION_GRACE_SECONDS', 180, { min: 0, max: 900 }),
      firstHourIntervalSeconds: 360,
      laterIntervalSeconds: 180,
      leaderboardRefreshSeconds: integer(env, 'LEADERBOARD_REFRESH_SECONDS', 60, { min: 30, max: 3600 }),
      weeklyAwardReconcileSeconds: integer(env, 'WEEKLY_AWARD_RECONCILE_SECONDS', 300, {
        min: 60,
        max: 3600
      })
    },
    vip: {
      oneDayCost: integer(env, 'VIP_1_DAY_COST', 120, { min: 1, max: 1_000_000 }),
      sevenDayCost: integer(env, 'VIP_7_DAY_COST', 360, { min: 1, max: 1_000_000 }),
      thirtyDayCost: integer(env, 'VIP_30_DAY_COST', 760, { min: 1, max: 1_000_000 }),
      reconcileSeconds: integer(env, 'VIP_RECONCILE_SECONDS', 120, { min: 30, max: 1800 })
    },
    donations: {
      url: donationUrl,
      feedToken: donationFeedToken,
      checkoutSecret: donationCheckoutSecret,
      pollSeconds: integer(env, 'DONATION_POLL_SECONDS', 45, { min: 30, max: 600 })
    },
    storage: {
      dataDir,
      backupDir,
      databasePath: `${dataDir}/seeding.sqlite`,
      backupRetention: integer(env, 'BACKUP_RETENTION', 30, { min: 3, max: 180 }),
      backupHourMsk: integer(env, 'BACKUP_HOUR_MSK', 3, { min: 0, max: 23 }),
      backupMinuteMsk: integer(env, 'BACKUP_MINUTE_MSK', 15, { min: 0, max: 59 })
    },
    logLevel: optional(env, 'LOG_LEVEL', 'info').toLowerCase()
  };

  if (!['debug', 'info', 'warn', 'error'].includes(config.logLevel)) {
    throw new Error('LOG_LEVEL must be debug, info, warn or error');
  }
  const channelIds = [
    config.discord.voiceChannelId,
    config.discord.rankChannelId,
    config.discord.shopChannelId,
    config.discord.rulesChannelId,
    config.discord.auditChannelId,
    config.discord.backupChannelId
  ];
  if (new Set(channelIds).size !== channelIds.length) {
    throw new Error('Every configured Discord channel must have a unique ID');
  }
  const managedRoleIds = [
    config.discord.seederRoleId,
    config.discord.superSeederRoleId,
    config.discord.vipRoleId
  ];
  if (new Set(managedRoleIds).size !== managedRoleIds.length) {
    throw new Error('Seeder, Super Seeder and VIP Discord roles must have unique IDs');
  }
  if (managedRoleIds.some((id) => channelIds.includes(id))) {
    throw new Error('A managed Discord role ID cannot also be used as a channel ID');
  }
  if ([config.rcon.password, config.rconSecondary.password].includes(config.discord.token)) {
    throw new Error('Discord and RCON credentials must be different');
  }
  if (config.rcon.url.href === config.rconSecondary.url.href) {
    throw new Error('The two WDRCON URLs must point to different game servers');
  }
  return Object.freeze(config);
}

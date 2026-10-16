import "dotenv/config";

function required(name) {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`Missing required environment variable: ${name}`);
  return value;
}

const guildId = required("GUILD_ID");
if (!/^\d{17,20}$/.test(guildId)) throw new Error("GUILD_ID must be a Discord snowflake");

const discordToken = process.env.DISCORD_TOKEN?.trim() || process.env.DISCORD_BOT_TOKEN?.trim() || "";
if (!discordToken) throw new Error("Missing DISCORD_TOKEN or DISCORD_BOT_TOKEN");

const syncUrl = process.env.SITE_SYNC_URL?.trim() || "";
if (syncUrl && !/^https:\/\/[a-z0-9.-]+\/[^\s]*$/i.test(syncUrl)) {
  throw new Error("SITE_SYNC_URL must be an HTTPS URL");
}

const syncSecret = process.env.SITE_SYNC_SECRET?.trim() || "";
if (syncUrl && syncSecret.length < 32) throw new Error("SITE_SYNC_SECRET must contain at least 32 characters");

const backupSecret = process.env.BACKUP_SECRET?.trim() || "";
if (backupSecret && (backupSecret.length < 32 || /^PASTE_|CHANGE_ME/i.test(backupSecret))) {
  throw new Error("BACKUP_SECRET must be a real secret containing at least 32 characters");
}

const backupChannelId = process.env.BACKUP_CHANNEL_ID?.trim() || "";
if (backupChannelId && !/^\d{17,20}$/.test(backupChannelId)) throw new Error("BACKUP_CHANNEL_ID must be a Discord snowflake");

const backupIntervalHours = Number.parseInt(process.env.BACKUP_INTERVAL_HOURS || "24", 10);
if (!Number.isInteger(backupIntervalHours) || backupIntervalHours < 1 || backupIntervalHours > 168) {
  throw new Error("BACKUP_INTERVAL_HOURS must be an integer from 1 to 168");
}

const backupRetention = Number.parseInt(process.env.BACKUP_RETENTION || "30", 10);
if (!Number.isInteger(backupRetention) || backupRetention < 3 || backupRetention > 90) {
  throw new Error("BACKUP_RETENTION must be an integer from 3 to 90");
}

export const config = Object.freeze({
  token: discordToken,
  guildId,
  adminRoleId: process.env.ADMIN_ROLE_ID?.trim() || "",
  syncUrl,
  syncSecret,
  siteClansUrl: process.env.SITE_CLANS_URL?.trim() || "https://wardogs-ru.ru/clans/",
  backupSecret,
  backupChannelId,
  backupIntervalHours,
  backupRetention,
  logLevel: process.env.LOG_LEVEL?.trim() || "info",
});

import {
  createCipheriv,
  createDecipheriv,
  createHash,
  randomBytes,
  scryptSync,
} from "node:crypto";
import { gzip, gunzip } from "node:zlib";
import { promisify } from "node:util";
import { mkdir, readFile, readdir, rename, unlink, writeFile } from "node:fs/promises";
import path from "node:path";
import { AttachmentBuilder, PermissionsBitField } from "discord.js";
import { config } from "./config.js";

const gzipAsync = promisify(gzip);
const gunzipAsync = promisify(gunzip);
const FORMAT = "wardogs-clans-backup";
const FORMAT_VERSION = 1;
const MAX_BACKUP_BYTES = 8 * 1024 * 1024;
const MAX_PAYLOAD_BYTES = 16 * 1024 * 1024;
const BACKUP_DIR = path.resolve(process.env.BACKUP_DIR?.trim() || path.join(process.cwd(), "data", "backups"));
const F = PermissionsBitField.Flags;

function asBase64(value) {
  return Buffer.from(value).toString("base64");
}

function fromBase64(value, label) {
  if (typeof value !== "string" || !/^[A-Za-z0-9+/]*={0,2}$/.test(value)) {
    throw new Error(`Invalid ${label} in backup envelope`);
  }
  return Buffer.from(value, "base64");
}

function safeTimestamp(date = new Date()) {
  return date.toISOString().replace(/[:.]/g, "-");
}

function clone(value) {
  return structuredClone(value);
}

function isSafeBackupChannel(channel, guild) {
  if (!channel?.isTextBased() || channel.isThread()) return false;
  const publicPermissions = channel.permissionsFor(guild.roles.everyone);
  const botPermissions = channel.permissionsFor(guild.members.me);
  return !publicPermissions?.has(F.ViewChannel)
    && botPermissions?.has([F.ViewChannel, F.SendMessages, F.ReadMessageHistory, F.AttachFiles]);
}

function channelSnapshot(channel) {
  if (!channel) return null;
  return {
    id: channel.id,
    name: channel.name,
    type: channel.type,
    parentId: channel.parentId || null,
    topic: typeof channel.topic === "string" ? channel.topic : null,
    userLimit: Number.isInteger(channel.userLimit) ? channel.userLimit : null,
    bitrate: Number.isInteger(channel.bitrate) ? channel.bitrate : null,
    permissionOverwrites: channel.permissionOverwrites?.cache?.map((overwrite) => ({
      id: overwrite.id,
      type: overwrite.type,
      allow: overwrite.allow.bitfield.toString(),
      deny: overwrite.deny.bitfield.toString(),
    })) ?? [],
  };
}

async function captureDiscord(guild, state) {
  await Promise.all([guild.members.fetch(), guild.roles.fetch(), guild.channels.fetch()]);
  const clans = {};

  for (const clan of Object.values(state.clans)) {
    const roles = {};
    for (const [key, roleId] of Object.entries(clan.roleIds || {})) {
      const role = guild.roles.cache.get(roleId);
      roles[key] = role ? {
        id: role.id,
        name: role.name,
        color: role.color,
        hoist: role.hoist,
        mentionable: role.mentionable,
        memberIds: role.members.filter((member) => !member.user.bot).map((member) => member.id),
      } : { id: roleId, missing: true, memberIds: [] };
    }

    const channels = {};
    for (const [key, channelId] of Object.entries(clan.channelIds || {})) {
      channels[key] = channelSnapshot(guild.channels.cache.get(channelId));
    }

    clans[clan.id] = { roles, channels };
  }

  const setup = {};
  for (const [key, value] of Object.entries(state.setup || {})) {
    if (key.endsWith("ChannelId") || key.endsWith("CategoryId") || key.endsWith("ForumId")) {
      setup[key] = channelSnapshot(guild.channels.cache.get(value));
    }
  }

  return { clans, setup };
}

export async function encodeBackup(payload) {
  const plain = Buffer.from(JSON.stringify(payload), "utf8");
  const compressed = await gzipAsync(plain, { level: 9 });
  const salt = randomBytes(16);
  const iv = randomBytes(12);
  const key = scryptSync(config.backupSecret, salt, 32);
  const cipher = createCipheriv("aes-256-gcm", key, iv);
  const ciphertext = Buffer.concat([cipher.update(compressed), cipher.final()]);
  const envelope = {
    format: FORMAT,
    version: FORMAT_VERSION,
    cipher: "aes-256-gcm",
    kdf: "scrypt",
    createdAt: payload.createdAt,
    guildId: payload.guild.id,
    checksum: createHash("sha256").update(plain).digest("hex"),
    salt: asBase64(salt),
    iv: asBase64(iv),
    tag: asBase64(cipher.getAuthTag()),
    data: asBase64(ciphertext),
  };
  return Buffer.from(`${JSON.stringify(envelope)}\n`, "utf8");
}

export async function decodeBackup(buffer) {
  if (!config.backupSecret) throw new Error("BACKUP_SECRET is not configured");
  if (!Buffer.isBuffer(buffer) || buffer.length === 0 || buffer.length > MAX_BACKUP_BYTES) {
    throw new Error("Backup file is empty or too large");
  }
  const envelope = JSON.parse(buffer.toString("utf8"));
  if (envelope?.format !== FORMAT || envelope?.version !== FORMAT_VERSION) throw new Error("Unsupported backup format");
  if (envelope.guildId !== config.guildId) throw new Error("Backup belongs to another Discord server");
  if (envelope.cipher !== "aes-256-gcm" || envelope.kdf !== "scrypt") throw new Error("Unsupported backup encryption");
  const salt = fromBase64(envelope.salt, "salt");
  const iv = fromBase64(envelope.iv, "iv");
  const tag = fromBase64(envelope.tag, "authentication tag");
  const ciphertext = fromBase64(envelope.data, "ciphertext");
  if (salt.length !== 16 || iv.length !== 12 || tag.length !== 16 || ciphertext.length === 0) throw new Error("Corrupted backup envelope");

  const key = scryptSync(config.backupSecret, salt, 32);
  const decipher = createDecipheriv("aes-256-gcm", key, iv);
  decipher.setAuthTag(tag);
  const compressed = Buffer.concat([decipher.update(ciphertext), decipher.final()]);
  const plain = await gunzipAsync(compressed, { maxOutputLength: MAX_PAYLOAD_BYTES });
  const checksum = createHash("sha256").update(plain).digest("hex");
  if (checksum !== envelope.checksum) throw new Error("Backup checksum mismatch");

  const payload = JSON.parse(plain.toString("utf8"));
  if (payload?.format !== FORMAT || payload?.version !== FORMAT_VERSION) throw new Error("Invalid backup payload");
  if (payload.guild?.id !== config.guildId || !payload.state || !payload.discord) throw new Error("Incomplete backup payload");
  return payload;
}

export class BackupManager {
  #store;
  #log;
  #timer = null;
  #running = false;

  constructor(store, logger = console) {
    this.#store = store;
    this.#log = logger;
  }

  get enabled() {
    return Boolean(config.backupSecret);
  }

  async resolveChannel(guild) {
    const candidates = [config.backupChannelId, this.#store.state.setup.backupChannelId].filter(Boolean);
    for (const id of candidates) {
      const channel = await guild.channels.fetch(id).catch(() => null);
      if (isSafeBackupChannel(channel, guild)) return channel;
    }
    return guild.channels.cache.find((channel) =>
      channel?.name === "🔐・резервные-копии" && isSafeBackupChannel(channel, guild),
    ) ?? null;
  }

  async create(guild, reason = "manual") {
    if (!this.enabled) throw new Error("BACKUP_SECRET is not configured");
    if (this.#running) throw new Error("Backup operation is already running");
    this.#running = true;
    try {
      const createdAt = new Date().toISOString();
      const state = this.#store.snapshot();
      const payload = {
        format: FORMAT,
        version: FORMAT_VERSION,
        createdAt,
        reason,
        guild: { id: guild.id, name: guild.name },
        settings: {
          adminRoleId: state.setup.adminRoleId || config.adminRoleId || null,
          backupChannelId: state.setup.backupChannelId || config.backupChannelId || null,
          backupIntervalHours: config.backupIntervalHours,
          backupRetention: config.backupRetention,
          siteSyncUrl: config.syncUrl || null,
          siteClansUrl: config.siteClansUrl,
        },
        state,
        discord: await captureDiscord(guild, state),
      };
      const contents = await encodeBackup(payload);
      if (contents.length > MAX_BACKUP_BYTES) throw new Error("Encrypted backup exceeds the 8 MB safety limit");

      await mkdir(BACKUP_DIR, { recursive: true, mode: 0o700 });
      const filename = `wardogs-clans-${safeTimestamp(new Date(createdAt))}.wdbak`;
      const target = path.join(BACKUP_DIR, filename);
      const temporary = `${target}.${process.pid}.tmp`;
      await writeFile(temporary, contents, { mode: 0o600 });
      await rename(temporary, target);

      const channel = await this.resolveChannel(guild);
      if (!channel) throw new Error("Private backup channel is missing; run /clan-setup or set BACKUP_CHANNEL_ID");
      const message = await channel.send({
        content: [
          "🔐 **Зашифрованная резервная копия клановой системы**",
          `Причина: \`${reason}\` · кланов: **${Object.keys(state.clans).length}** · заявок на клан: **${Object.keys(state.applications).length}** · заявок на вступление: **${Object.keys(state.joinApplications || {}).length}**`,
          `Создано: <t:${Math.floor(Date.parse(createdAt) / 1000)}:F> · формат: \`${FORMAT}/${FORMAT_VERSION}\``,
        ].join("\n"),
        files: [new AttachmentBuilder(contents, { name: filename, description: "Encrypted WARDOGS clan backup" })],
        allowedMentions: { parse: [] },
      });

      await this.#store.update((current) => {
        current.backup = { lastAt: createdAt, lastMessageId: message.id, lastFilename: filename, lastError: null };
      });
      await this.#pruneLocal();
      await this.#pruneDiscord(channel);
      this.#log.info(`Backup created: ${filename}`);
      return { filename, createdAt, messageId: message.id, clanCount: Object.keys(state.clans).length };
    } catch (error) {
      await this.#store.update((current) => {
        current.backup = { ...(current.backup || {}), lastError: String(error?.message || error), lastAttemptAt: new Date().toISOString() };
      }).catch(() => null);
      throw error;
    } finally {
      this.#running = false;
    }
  }

  async loadLatest(guild) {
    if (!this.enabled) throw new Error("BACKUP_SECRET is not configured");
    const channel = await this.resolveChannel(guild);
    if (!channel) throw new Error("Private backup channel was not found");
    const messages = await channel.messages.fetch({ limit: 100 });
    const candidate = messages
      .filter((message) => message.author.id === guild.members.me.id)
      .sort((a, b) => b.createdTimestamp - a.createdTimestamp)
      .find((message) => message.attachments.some((attachment) => attachment.name?.endsWith(".wdbak")));
    const attachment = candidate?.attachments.find((item) => item.name?.endsWith(".wdbak"));
    if (!attachment) throw new Error("No backup attachment found in the private channel");
    if (attachment.size > MAX_BACKUP_BYTES) throw new Error("Backup attachment is too large");
    const response = await fetch(attachment.url, { redirect: "error", signal: AbortSignal.timeout(20_000) });
    if (!response.ok) throw new Error(`Backup download failed (${response.status})`);
    const buffer = Buffer.from(await response.arrayBuffer());
    return { payload: await decodeBackup(buffer), messageId: candidate.id, filename: attachment.name };
  }

  async inspectLocal(filename) {
    const safeName = path.basename(filename);
    if (safeName !== filename || !safeName.endsWith(".wdbak")) throw new Error("Invalid backup filename");
    return decodeBackup(await readFile(path.join(BACKUP_DIR, safeName)));
  }

  start(guild) {
    this.stop();
    if (!this.enabled) {
      this.#log.error("Automatic backups disabled: BACKUP_SECRET is not configured");
      return;
    }
    const interval = config.backupIntervalHours * 60 * 60 * 1000;
    const run = async () => {
      const state = this.#store.state;
      if (!Object.keys(state.setup || {}).length) {
        this.#log.error("Automatic backup skipped: clan setup state is empty; restore a backup or run /clan-setup");
        return;
      }
      const lastAt = Date.parse(state.backup?.lastAt || "");
      if (Number.isFinite(lastAt) && Date.now() - lastAt < interval * 0.9) return;
      await this.create(guild, "automatic").catch((error) => this.#log.error("Automatic backup failed", error));
    };
    this.#timer = setInterval(run, interval);
    this.#timer.unref?.();
    const initial = setTimeout(run, 60_000);
    initial.unref?.();
  }

  stop() {
    if (this.#timer) clearInterval(this.#timer);
    this.#timer = null;
  }

  status() {
    return {
      enabled: this.enabled,
      intervalHours: config.backupIntervalHours,
      retention: config.backupRetention,
      ...clone(this.#store.state.backup || {}),
    };
  }

  async #pruneLocal() {
    const files = (await readdir(BACKUP_DIR)).filter((name) => /^wardogs-clans-.*\.wdbak$/.test(name)).sort().reverse();
    await Promise.all(files.slice(config.backupRetention).map((name) => unlink(path.join(BACKUP_DIR, name)).catch(() => null)));
  }

  async #pruneDiscord(channel) {
    const messages = await channel.messages.fetch({ limit: 100 });
    const backups = [...messages
      .filter((message) => message.author.id === channel.guild.members.me.id && message.attachments.some((item) => item.name?.endsWith(".wdbak")))
      .values()].sort((a, b) => b.createdTimestamp - a.createdTimestamp);
    for (const message of backups.slice(config.backupRetention)) await message.delete().catch(() => null);
  }
}

import { chmod, mkdir, readdir, rename, unlink, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { AttachmentBuilder } from 'discord.js';
import { encryptBackup } from '../backup-crypto.js';
import { moscowDateKey, moscowParts } from '../time.js';

const MAX_DISCORD_BACKUP_BYTES = 7_500_000;

export class BackupService {
  constructor({ database, discord, config, logger, lease }) {
    this.database = database;
    this.discord = discord;
    this.config = config;
    this.logger = logger;
    this.lease = lease;
    this.timer = null;
    this.running = false;
  }

  start() {
    this.check().catch((error) => this.logger.error('backup_initial_check_failed', { error }));
    this.timer = setInterval(() => {
      this.check().catch((error) => this.logger.error('backup_check_failed', { error }));
    }, 60_000);
    this.timer.unref();
  }

  stop() {
    if (this.timer) clearInterval(this.timer);
  }

  async check() {
    if (!this.lease.held || this.running) return;
    const now = Date.now();
    const parts = moscowParts(now);
    const due = parts.hour > this.config.storage.backupHourMsk
      || (parts.hour === this.config.storage.backupHourMsk && parts.minute >= this.config.storage.backupMinuteMsk);
    const date = moscowDateKey(now);
    if (!due || this.database.getState('last_backup_date', '') === date) return;
    await this.create(date);
  }

  async create(date = moscowDateKey(Date.now())) {
    if (this.running) return null;
    this.running = true;
    try {
      await mkdir(this.config.storage.backupDir, { recursive: true, mode: 0o700 });
      await chmod(this.config.storage.backupDir, 0o700);
      const previousHeads = this.database.chainHeads();
      this.database.audit('backup_snapshot', '', { date, previousHeads });
      const heads = this.database.chainHeads();
      const encrypted = encryptBackup(this.database.serialize(), this.config.security.backupKey);
      const createdAt = Date.now();
      const parts = moscowParts(createdAt);
      const clock = `${String(parts.hour).padStart(2, '0')}${String(parts.minute).padStart(2, '0')}${String(parts.second).padStart(2, '0')}${String(createdAt % 1000).padStart(3, '0')}`;
      const filename = `wardogs-seeding-${date}-${clock}.wdsbak`;
      const finalPath = join(this.config.storage.backupDir, filename);
      const temporaryPath = `${finalPath}.tmp`;
      await writeFile(temporaryPath, encrypted, { mode: 0o600 });
      await rename(temporaryPath, finalPath);
      this.database.setState('last_backup_date', date);
      await this.#rotate();
      await this.#anchorAndUpload(filename, encrypted, heads);
      this.logger.info('backup_created', { filename, bytes: encrypted.length });
      return finalPath;
    } finally {
      this.running = false;
    }
  }

  async #rotate() {
    const names = (await readdir(this.config.storage.backupDir))
      .filter((name) => /^wardogs-seeding-\d{4}-\d{2}-\d{2}-\d{9}\.wdsbak$/.test(name))
      .sort()
      .reverse();
    for (const name of names.slice(this.config.storage.backupRetention)) {
      await unlink(join(this.config.storage.backupDir, name));
    }
  }

  async #anchorAndUpload(filename, encrypted, heads) {
    const guild = this.discord.guilds.cache.get(this.config.discord.guildId);
    const anchorText = `Резервная копия ${filename}\nAudit: \`${heads.audit || 'empty'}\`\nLedger: \`${heads.ledger || 'empty'}\``;
    if (this.config.discord.auditChannelId) {
      const channel = guild?.channels.cache.get(this.config.discord.auditChannelId)
        || await guild?.channels.fetch(this.config.discord.auditChannelId);
      if (channel?.isTextBased() && channel.isSendable()) {
        await channel.send({ content: anchorText, allowedMentions: { parse: [] } });
      }
    }
    if (this.config.discord.backupChannelId) {
      const channel = guild?.channels.cache.get(this.config.discord.backupChannelId)
        || await guild?.channels.fetch(this.config.discord.backupChannelId);
      if (channel?.isTextBased() && channel.isSendable()) {
        if (encrypted.length <= MAX_DISCORD_BACKUP_BYTES) {
          await channel.send({
            content: 'Зашифрованная резервная копия. Ключ в Discord не хранится.',
            files: [new AttachmentBuilder(encrypted, { name: filename })],
            allowedMentions: { parse: [] }
          });
        } else {
          await channel.send({
            content: `${anchorText}\nФайл не приложен: его размер превышает безопасный лимит отправки.`,
            allowedMentions: { parse: [] }
          });
        }
      }
    }
  }
}

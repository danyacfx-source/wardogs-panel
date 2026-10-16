import { generateHumanCode } from '../crypto-box.js';
import { DomainError } from '../database.js';

const STEAM_ID = /^7656119\d{10}$/;

export class LinkService {
  constructor({ database, rcon, rconSecondary = null, crypto, config, logger }) {
    this.database = database;
    this.rcon = rcon;
    this.rconSecondary = rconSecondary;
    this.crypto = crypto;
    this.config = config;
    this.logger = logger;
  }

  async begin({ discordId, steamId, displayName, purpose = 'seeding' }) {
    const normalized = String(steamId).trim();
    if (!STEAM_ID.test(normalized)) {
      throw new DomainError('bad_steam_id', 'Нужен SteamID64: 17 цифр, обычно начинается с 7656119.');
    }
    const clients = this.rconSecondary ? [this.rcon, this.rconSecondary] : [this.rcon];
    const results = await Promise.allSettled(clients.map((client) => client.players()));
    const serverIndex = results.findIndex((result) => result.status === 'fulfilled'
      && result.value.some((entry) => entry.steamId === normalized));
    if (serverIndex < 0) {
      const failed = results.find((result) => result.status === 'rejected');
      if (failed) throw failed.reason;
      throw new DomainError('player_offline', 'Сначала зайдите на один из игровых серверов, затем повторите команду.');
    }
    const player = results[serverIndex].value.find((entry) => entry.steamId === normalized);

    const code = generateHumanCode(8);
    const discordIdx = this.crypto.blindIndex(discordId, 'discord-id');
    const codeDigest = this.crypto.challengeDigest(discordIdx, code);
    const expiresAt = Date.now() + this.config.security.challengeMinutes * 60_000;
    this.database.prepareLinkChallenge({
      discordId,
      steamId: normalized,
      displayName: displayName || player.name,
      codeDigest,
      expiresAt,
      purpose
    });
    try {
      await clients[serverIndex].whisper(
        normalized,
        `WARDOGS: код привязки ${code}. В Discord: ${purpose === 'donation' ? '/vip-link verify' : '/seed verify'}. Никому не сообщайте код.`
      );
    } catch (error) {
      this.database.cancelLinkChallenge(discordId);
      throw error;
    }
    return { expiresAt };
  }

  verify({ discordId, code, purpose = 'seeding' }) {
    const normalized = String(code).trim().toUpperCase();
    if (!/^[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{8}$/.test(normalized)) {
      throw new DomainError('bad_code_format', 'Код состоит из 8 символов без пробелов.');
    }
    return this.database.verifyLinkChallenge({
      discordId,
      code: normalized,
      maxAttempts: this.config.security.challengeMaxAttempts,
      purpose
    });
  }
}

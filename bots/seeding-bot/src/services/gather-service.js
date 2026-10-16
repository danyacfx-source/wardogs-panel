import { ChannelType, PermissionFlagsBits } from 'discord.js';
import { DomainError } from '../database.js';
import { gatherCard, gatherReachedCard, gatherMarker, gatherInstructions } from '../discord/gather.js';

const STATE_KEY = 'seeding_gathers';
const COOLDOWN_MS = 10 * 60_000;
const SNOWFLAKE = /^\d{17,20}$/;
const noMentions = { parse: [], repliedUser: false };
const missingMessage = (error) => error?.code === 10008 || error?.status === 404;

export class GatherService {
  constructor({ database, discord, config, logger, lease, rcon, rconSecondary }) {
    Object.assign(this, { database, discord, config, logger, lease });
    this.servers = new Map([['main', { label: 'Сервер 1', rcon }], ['second', { label: 'Сервер 2', rcon: rconSecondary }]]);
    this.queue = Promise.resolve();
    this.timer = null;
    this.ticking = false;
  }

  #run(job) {
    const result = this.queue.then(() => {
      if (!this.lease.held) throw new DomainError('gather_locked', 'Бот ещё запускается или остановлен защитной блокировкой.');
      return job();
    });
    this.queue = result.catch(() => {});
    return result;
  }
  #sessions() { return this.database.getState(STATE_KEY, {}); }
  #save(session) {
    const sessions = this.#sessions();
    sessions[session.serverId] = session;
    this.database.setState(STATE_KEY, sessions);
  }
  #server(serverId) {
    const server = this.servers.get(serverId);
    if (!server?.rcon) throw new DomainError('gather_bad_server', 'Выберите сервер 1 или сервер 2.');
    return server;
  }
  async #channel(channelId) {
    const guild = this.discord.guilds.cache.get(this.config.discord.guildId);
    const channel = guild?.channels.cache.get(channelId) || await guild?.channels.fetch(channelId);
    if (!channel || channel.guildId !== this.config.discord.guildId
      || ![ChannelType.GuildText, ChannelType.GuildAnnouncement].includes(channel.type)
      || !channel.isSendable()) {
      throw new DomainError('gather_bad_channel', 'Для сбора нужен текстовый канал этого Discord-сервера.');
    }
    if ([this.config.discord.rulesChannelId, this.config.discord.auditChannelId,
      this.config.discord.backupChannelId].includes(channelId)) {
      throw new DomainError('gather_protected_channel', 'Выберите обычный текстовый канал, а не канал правил, аудита или резервных копий.');
    }
    const member = guild.members.me || await guild.members.fetchMe();
    const permissions = channel.permissionsFor(member);
    const required = [PermissionFlagsBits.ViewChannel, PermissionFlagsBits.SendMessages,
      PermissionFlagsBits.EmbedLinks, PermissionFlagsBits.ReadMessageHistory];
    if (!permissions || required.some((permission) => !permissions.has(permission))) {
      throw new DomainError('gather_channel_permissions', 'Боту нужны права просмотра канала, отправки сообщений, встраивания ссылок и чтения истории.');
    }
    return { channel, guild, permissions };
  }

  async #sample(session, now) {
    try {
      const status = await this.#server(session.serverId).rcon.status();
      if (!Number.isSafeInteger(status.playerCount) || status.playerCount < 0) throw new Error('Invalid player count');
      session.population = status.playerCount;
      session.maxPlayers = status.maxPlayers;
      session.serverName = String(status.serverName || session.serverName || '').replace(/[\r\n\0]/g, ' ').slice(0, 200);
      session.sampleOk = true;
      session.sampleAt = now;
    } catch (error) {
      session.sampleOk = false;
      if (!session.outageLogged) this.logger.warn('gather_server_unavailable', { server: session.serverId, error });
    }
    session.outageLogged = !session.sampleOk;
  }
  async #find(channel, session, kind) {
    const recent = await channel.messages.fetch({ limit: 50 });
    const marker = gatherMarker(session, kind);
    return recent.find((message) => message.author.id === this.discord.user.id
      && message.embeds[0]?.footer?.text?.endsWith(marker));
  }
  async #deliver(session, { notifyRole = false } = {}) {
    try {
      const { channel, guild, permissions } = await this.#channel(session.channelId);
      let message = null;
      if (session.cardMessageId) {
        try { message = await channel.messages.fetch(session.cardMessageId); }
        catch (error) { if (!missingMessage(error)) throw error; }
      }
      message ||= await this.#find(channel, session, 'card');
      if (message) {
        if (message.author.id !== this.discord.user.id) throw new Error('Gather message is not owned by this bot');
        if (message.content?.includes(`<@&${session.roleId}>`)) session.notificationConfirmed = true;
        await message.edit(gatherCard(session));
      } else {
        const ping = session.status === 'active' && !session.notificationConfirmed
          && (notifyRole || !session.cardMessageId);
        if (ping) {
          const role = guild.roles.cache.get(session.roleId) || await guild.roles.fetch(session.roleId);
          if (!role || (!role.mentionable && !permissions.has(PermissionFlagsBits.MentionEveryone))) {
            throw new DomainError('gather_role_permissions', 'Бот временно не может уведомить выбранную роль.');
          }
          session.notificationAttempted = true;
          this.#save(session);
        }
        message = await channel.send({ ...gatherCard(session),
          content: ping ? `<@&${session.roleId}> 🐺 Сбор на ${session.label.toLowerCase()}! Помогите собрать ${session.target} игроков.` : '',
          allowedMentions: ping ? { parse: [], roles: [session.roleId], users: [], repliedUser: false } : noMentions,
          nonce: `g-${session.id}`, enforceNonce: true });
        if (ping) session.notificationConfirmed = true;
      }
      session.cardMessageId = message.id;
      this.#save(session);
      if (session.status === 'reached' && !session.completionMessageId) {
        const previous = await this.#find(channel, session, 'done');
        const done = previous || await channel.send({ ...gatherReachedCard(session), nonce: `done-${session.id}`, enforceNonce: true });
        session.completionMessageId = done.id;
      }
      session.deliveryPending = false;
      this.#save(session);
    } catch (error) {
      session.deliveryPending = true;
      this.#save(session);
      this.logger.warn('gather_discord_update_pending', { id: session.id, server: session.serverId, error });
    }
  }
  async #refresh(session, now) {
    if (session.status === 'active') {
      if (now >= session.expiresAt) {
        session.status = 'expired';
        this.database.audit('gather_expired', 'system', { id: session.id, server: session.serverId }, now);
      } else {
        await this.#sample(session, now);
        if (session.sampleOk && session.population >= session.target) {
          session.status = 'reached';
          session.reachedAt = now;
          this.database.audit('gather_target_reached', 'system', {
            id: session.id, server: session.serverId, population: session.population, target: session.target
          }, now);
        }
      }
      this.#save(session);
      await this.#deliver(session);
    } else if (session.deliveryPending || (session.status === 'reached' && !session.completionMessageId)) {
      await this.#deliver(session);
    }
    return session;
  }

  startGather({ serverId, channelId, actorId, requestId, target = 20,
    roleId = this.config.discord.seederRoleId, minutes = 120, instructions = '' }, now = Date.now()) {
    return this.#run(async () => {
      const server = this.#server(serverId);
      if (![channelId, actorId, requestId, roleId].every((id) => SNOWFLAKE.test(id))
        || !Number.isInteger(target) || target < 2 || target > 100
        || !Number.isInteger(minutes) || minutes < 15 || minutes > 360
        || typeof instructions !== 'string' || instructions.trim().length > 1000) {
        throw new DomainError('gather_bad_input', 'Проверьте параметры сбора: цель 2–100, срок 15–360 минут, инструкция до 1000 символов.');
      }
      const sessions = this.#sessions();
      const replay = Object.values(sessions).find((session) => session.id === requestId);
      if (replay) {
        if (replay.serverId !== serverId || replay.channelId !== channelId || replay.target !== target
          || replay.roleId !== roleId || replay.actorId !== actorId) {
          throw new DomainError('gather_conflicting_retry', 'Эта команда уже выполнена с другими параметрами.');
        }
        await this.#refresh(replay, now);
        return { created: false, session: replay };
      }
      const existing = sessions[serverId];
      if (existing?.status === 'active' && now < existing.expiresAt) {
        return { created: false, session: await this.#refresh(existing, now) };
      }
      if (existing && now - existing.startedAt < COOLDOWN_MS) {
        throw new DomainError('gather_cooldown', 'Для этого сервера новый сбор можно объявлять не чаще одного раза в 10 минут.');
      }
      if (existing?.status === 'active') await this.#refresh(existing, now);
      const { channel, guild, permissions } = await this.#channel(channelId);
      const role = guild.roles.cache.get(roleId) || await guild.roles.fetch(roleId);
      if (!role || roleId === guild.id) throw new DomainError('gather_bad_role', 'Выберите существующую роль участников; @everyone для сбора не используется.');
      if (!role.mentionable && !permissions.has(PermissionFlagsBits.MentionEveryone)) {
        throw new DomainError('gather_role_permissions', 'Разрешите упоминание выбранной роли или дайте боту право «Упоминание @everyone, @here и всех ролей» в канале сбора.');
      }
      const session = { id: requestId, serverId, label: server.label, target, channelId: channel.id,
        actorId, roleId, instructions: instructions.trim(), startedAt: now, expiresAt: now + minutes * 60_000,
        status: 'active', population: null, serverName: '', sampleOk: false, sampleAt: null,
        cardMessageId: null, completionMessageId: null, notificationAttempted: false, notificationConfirmed: false, deliveryPending: true };
      await this.#sample(session, now);
      if (session.sampleOk && session.population >= target) {
        throw new DomainError('gather_already_full', `На выбранном сервере уже ${session.population}/${target} игроков. Дополнительный сбор не требуется.`);
      }
      if (session.sampleOk && session.maxPlayers > 0 && target > session.maxPlayers) {
        throw new DomainError('gather_target_too_large', 'Цель превышает количество мест на выбранном сервере.');
      }
      this.#save(session);
      this.database.audit('gather_started', actorId, { id: requestId, server: serverId,
        channelId, roleId, target, expiresAt: session.expiresAt }, now);
      await this.#deliver(session, { notifyRole: true });
      return { created: true, session };
    });
  }

  status(serverId, now = Date.now()) {
    return this.#run(async () => {
      this.#server(serverId);
      const session = this.#sessions()[serverId];
      return session ? this.#refresh(session, now) : null;
    });
  }
  cancel(serverId, actorId, now = Date.now()) {
    return this.#run(async () => {
      this.#server(serverId);
      if (!SNOWFLAKE.test(actorId)) throw new DomainError('gather_bad_actor', 'Не удалось определить автора команды.');
      const session = this.#sessions()[serverId];
      if (!session) throw new DomainError('gather_missing', 'Для этого сервера сбор ещё не объявлялся.');
      if (session.status !== 'active') return session;
      session.status = 'cancelled';
      session.cancelledAt = now;
      this.#save(session);
      this.database.audit('gather_cancelled', actorId, { id: session.id, server: serverId }, now);
      await this.#deliver(session);
      return session;
    });
  }
  instructions(serverId, sessionId) {
    const session = this.#sessions()[serverId];
    if (!session || session.id !== sessionId) {
      throw new DomainError('gather_old_button', 'Этот сбор заменён новым. Откройте инструкцию в актуальной карточке сбора.');
    }
    return gatherInstructions(session, this.config);
  }
  async tick(now = Date.now()) {
    if (this.ticking || !this.lease.held) return;
    this.ticking = true;
    try {
      await this.#run(async () => {
        for (const session of Object.values(this.#sessions())) await this.#refresh(session, now);
      });
    } finally { this.ticking = false; }
  }
  start() {
    if (this.timer) return;
    this.tick().catch((error) => this.logger.error('gather_initial_tick_failed', { error }));
    this.timer = setInterval(() => this.tick().catch((error) =>
      this.logger.error('gather_tick_failed', { error })), this.config.seed.tickSeconds * 1000);
    this.timer.unref();
  }
  stop() { if (this.timer) clearInterval(this.timer); this.timer = null; }
}

import {
  escapeMarkdown,
  MessageFlags,
  PermissionFlagsBits
} from 'discord.js';
import { DomainError } from '../database.js';
import { RconError } from '../rcon-client.js';
import { leaderboardEmbeds } from '../services/report-service.js';
import { shopPayload } from './shop.js';
import { BRAND, brandedEmbed, brandedFooter } from './theme.js';
import { discordTimestamp, formatDuration, weekKey } from '../time.js';
import { gatherCard, gatherMessageUrl } from './gather.js';

class RateLimiter {
  constructor(limit = 8, windowMs = 60_000) {
    this.limit = limit;
    this.windowMs = windowMs;
    this.entries = new Map();
  }

  take(key) {
    const now = Date.now();
    const valid = (this.entries.get(key) || []).filter((time) => now - time < this.windowMs);
    if (valid.length >= this.limit) return false;
    valid.push(now);
    this.entries.set(key, valid);
    if (this.entries.size > 5000) {
      for (const [id, times] of this.entries) if (!times.some((time) => now - time < this.windowMs)) this.entries.delete(id);
    }
    return true;
  }
}

function hasAnyRole(interaction, roleIds) {
  const roles = interaction.member?.roles;
  if (roles?.cache) return roleIds.some((id) => roles.cache.has(id));
  if (Array.isArray(roles)) return roleIds.some((id) => roles.includes(id));
  return false;
}

function hasAdminAccess(interaction, roleIds) {
  return Boolean(interaction.memberPermissions?.has(PermissionFlagsBits.ManageGuild))
    && hasAnyRole(interaction, roleIds);
}

function hasGatherAccess(interaction, config) {
  return hasAdminAccess(interaction, config.discord.adminRoleIds)
    || hasAnyRole(interaction, config.discord.gatherRoleIds || []);
}

function vipLine(member) {
  if (!member || member.state === 'inactive') return 'не активен';
  if (member.state === 'pending_add') return 'ожидает подтверждения слота на обоих серверах; срок ещё не начался';
  if (member.state === 'pending_remove') return 'истёк; удаление reserved slot ожидает применения';
  if (member.state === 'active') return `активен до ${discordTimestamp(member.activeUntil, 'f')}`;
  return 'неизвестно';
}

function vipShort(member) {
  if (!member || member.state === 'inactive') return 'нет';
  if (member.state === 'active') return 'активен';
  if (member.state === 'pending_add') return 'ожидает активации';
  if (member.state === 'pending_remove') return 'ожидает снятия';
  return 'неизвестно';
}

export async function liveSlotLines(steamId, rcon, rconSecondary) {
  const results = await Promise.allSettled([rcon.liveReservedIds(), rconSecondary.liveReservedIds()]);
  return results.map((result, index) => {
    const label = `Сервер ${index + 1}`;
    if (result.status === 'rejected') return `${label}: не удалось проверить RCON`;
    if (result.value === null) return `${label}: WDRCON не отдаёт активный список`;
    return `${label}: ${result.value.includes(steamId) ? '✅ активен сейчас' : '⏳ сейчас не активен'}`;
  }).join('\n');
}

function safeName(name) {
  return escapeMarkdown(String(name || 'Игрок').replace(/[\r\n\0]/g, ' ').slice(0, 70));
}

function bulkOperationId(interaction) {
  const value = interaction.options.getString('operation_id', true).trim().toLowerCase();
  if (!/^[a-z0-9][a-z0-9_-]{2,63}$/.test(value)) {
    throw new DomainError('bad_operation_id',
      'operation_id: от 3 до 64 символов, только латинские буквы, цифры, _ и -.');
  }
  return value;
}

function seedStateLine(status, config) {
  if (status.servers?.length) {
    const lines = status.servers.map((server) => {
      if (server.state === 'active') return `${server.label}: ${server.population} игроков, начисление активно`;
      if (server.state === 'population_limit') {
        return `${server.label}: ${server.population} игроков, пауза (порог ${config.seed.stopPlayerCount})`;
      }
      return `${server.label}: RCON недоступен, начисление приостановлено`;
    });
    return `${lines.join('\n')}\nПодтверждённых сидеров: ${status.eligibleCount}`;
  }
  if (status.state === 'active') return `начисление активно; на сервере ${status.population}, подтверждённых сидеров ${status.eligibleCount}`;
  if (status.state === 'population_limit') return `пауза: на сервере ${status.population}/${config.seed.stopPlayerCount}+ игроков`;
  if (status.state === 'closed') return `закрыто по расписанию ${config.seed.start.text}–${config.seed.end.text} МСК`;
  if (status.state === 'rcon_unavailable') return 'временно приостановлено: нет связи с игровым сервером; неподтверждённое время не начисляется';
  if (status.state === 'error') return 'временно приостановлено: сервер или Discord недоступен';
  return 'бот запускается';
}

function interactionCommand(interaction) {
  if (!interaction.isChatInputCommand()) return interaction.customId;
  let subcommand = '';
  try {
    subcommand = interaction.options.getSubcommand(false) || '';
  } catch {}
  return subcommand ? `${interaction.commandName}:${subcommand}` : interaction.commandName;
}

function rconFailureReason(error) {
  if (!(error instanceof RconError)) return 'неизвестная ошибка';
  if (error.code === 'wrong_endpoint') return 'указан адрес веб-панели вместо WDRCON API';
  if ([401, 403].includes(error.status)) return 'RCON отклонил пароль или доступ';
  const labels = {
    ECONNREFUSED: 'порт отклонил соединение',
    ECONNRESET: 'соединение сброшено',
    ETIMEDOUT: 'истёк тайм-аут соединения',
    EHOSTUNREACH: 'узел недоступен',
    ENETUNREACH: 'сеть недоступна',
    ENOTFOUND: 'имя сервера не найдено',
    EAI_AGAIN: 'временная ошибка DNS',
    ERR_TLS_CERT_ALTNAME_INVALID: 'ошибка TLS-сертификата',
    DEPTH_ZERO_SELF_SIGNED_CERT: 'недоверенный TLS-сертификат'
  };
  if (labels[error.networkCode]) return labels[error.networkCode];
  if (error.status) return `HTTP ${error.status}`;
  return 'сетевое соединение недоступно';
}

export function installInteractionHandler(deps) {
  const {
    discord, database, config, logger, linkService, seedingService,
    vipService, reportService, backupService, rcon, lease
  } = deps;
  const limiter = new RateLimiter();

  discord.on('interactionCreate', async (interaction) => {
    if (interaction.guildId !== config.discord.guildId) return;
    const gatherButton = interaction.isButton()
      && /^seed:gather:connect:(?:main|second):\d{17,20}$/.test(interaction.customId);
    const supportedButton = gatherButton || (interaction.isButton() && /^seed:buy:(?:1d|7d|30d)$/.test(interaction.customId));
    const supportedCommand = interaction.isChatInputCommand()
      && ['seed', 'seed-admin', 'seed-gather', 'vip-link'].includes(interaction.commandName);
    if (!supportedButton && !supportedCommand) return;
    const correlationId = logger.correlationId();
    try {
      if (!lease.held) {
        await interaction.reply({
          content: 'Бот ещё запускается или остановлен защитной блокировкой. Повторите позже.',
          flags: MessageFlags.Ephemeral
        });
        return;
      }
      if (!limiter.take(interaction.user.id)) {
        await interaction.reply({
          content: 'Слишком много команд. Подождите минуту и повторите.',
          flags: MessageFlags.Ephemeral
        });
        return;
      }
      if (supportedButton) {
        await interaction.deferReply({ flags: MessageFlags.Ephemeral });
        if (gatherButton) {
          const [, , , serverId, sessionId] = interaction.customId.split(':');
          await interaction.editReply(deps.gatherService.instructions(serverId, sessionId));
          return;
        }
        await buyVip(interaction, interaction.customId.split(':').at(-1), vipService);
        return;
      }
      if (interaction.commandName === 'vip-link') {
        await handleVipLink(interaction, deps);
        return;
      }
      if (interaction.commandName === 'seed-gather') {
        const action = interaction.options.getSubcommand();
        if (!['start', 'status', 'stop'].includes(action) || !hasGatherAccess(interaction, config)) {
          await interaction.reply({ content: 'Недостаточно прав.', flags: MessageFlags.Ephemeral });
          database.audit('gather_access_denied', interaction.user.id, { command: interactionCommand(interaction) });
          return;
        }
        await handleGather(interaction, deps, action);
        return;
      }
      if (interaction.commandName === 'seed-admin') {
        if (!hasAdminAccess(interaction, config.discord.adminRoleIds)) {
          await interaction.reply({ content: 'Недостаточно прав.', flags: MessageFlags.Ephemeral });
          database.audit('admin_access_denied', interaction.user.id, { command: interaction.commandName });
          return;
        }
        await handleAdmin(interaction, deps);
        return;
      }
      await handleSeed(interaction, deps);
    } catch (error) {
      const command = interactionCommand(interaction);
      const safe = error instanceof DomainError
        ? error.message
        : error instanceof RconError
          ? interaction.isChatInputCommand() && interaction.commandName === 'seed-admin'
            ? `RCON недоступен: ${rconFailureReason(error)}. Проверьте игровой сервер, WDRCON-порт и firewall.`
            : 'Связь с игровым сервером временно отсутствует. Операция отменена, баллы не списаны. Повторите позже.'
          : `Операция не выполнена. Код ошибки: ${correlationId}`;
      if (error instanceof DomainError) {
        logger.info('interaction_rejected', { domainCode: error.code, correlationId, command });
      } else if (error instanceof RconError) {
        logger.warn('interaction_rcon_failed', { error, correlationId, command });
      } else {
        logger.error('interaction_failed', { error, correlationId, command });
      }
      const payload = { content: safe, allowedMentions: { parse: [] } };
      if (interaction.deferred || interaction.replied) await interaction.editReply(payload).catch(() => {});
      else await interaction.reply({ ...payload, flags: MessageFlags.Ephemeral }).catch(() => {});
    }
  });
}

async function handleVipLink(interaction, deps) {
  const { database, linkService, donationService, rcon, rconSecondary } = deps;
  const sub = interaction.options.getSubcommand();
  if (sub === 'link') {
    await interaction.deferReply({ flags: MessageFlags.Ephemeral });
    const result = await linkService.begin({
      discordId: interaction.user.id,
      steamId: interaction.options.getString('steam_id', true),
      displayName: interaction.member?.displayName || interaction.user.globalName || interaction.user.username,
      purpose: 'donation'
    });
    await interaction.editReply({ content: `Код отправлен вам в игре. Подтвердите его командой \`/vip-link verify\` до ${discordTimestamp(result.expiresAt, 'T')}. Эта привязка предназначена только для оплаченного VIP.`,
      allowedMentions: { parse: [] } });
    return;
  }
  if (sub === 'verify') {
    const user = linkService.verify({ discordId: interaction.user.id,
      code: interaction.options.getString('code', true), purpose: 'donation' });
    await interaction.reply({ content: `Steam …${user.steamId.slice(-4)} подтверждён для оплаченного VIP. Эта привязка не включает сидинг и не начисляет баллы.`,
      flags: MessageFlags.Ephemeral, allowedMentions: { parse: [] } });
    return;
  }
  if (sub === 'status') {
    await interaction.deferReply({ flags: MessageFlags.Ephemeral });
    const identity = database.getVipIdentityByDiscord(interaction.user.id);
    const until = donationService?.until(interaction.user.id);
    const slots = identity ? await liveSlotLines(identity.steamId, rcon, rconSecondary) : '';
    await interaction.editReply({ content: identity
      ? `Steam …${identity.steamId.slice(-4)} привязан ${identity.purpose === 'donation' ? 'только для оплаченного VIP' : 'для сидинга и оплаченного VIP'}.\nОплаченный VIP: ${until && until > Date.now() ? `до ${discordTimestamp(until, 'f')}` : 'не активен'}.\nReserved slot (проверено сейчас):\n${slots}`
      : 'Steam для VIP не привязан. Зайдите на игровой сервер и выполните `/vip-link link steam_id`.',
    allowedMentions: { parse: [] } });
    return;
  }
  if (sub === 'unlink') {
    if (interaction.options.getBoolean('confirm', true) !== true) {
      await interaction.reply({ content: 'Отвязка отменена.', flags: MessageFlags.Ephemeral });
      return;
    }
    const user = database.unlinkDonorDiscord({ discordId: interaction.user.id });
    await interaction.reply({ content: `Отдельная VIP-привязка к Steam …${user.steamId.slice(-4)} отменена.`,
      flags: MessageFlags.Ephemeral, allowedMentions: { parse: [] } });
  }
}

async function handleSeed(interaction, deps) {
  const {
    database, config, linkService, seedingService, vipService,
    roleService, weeklyAwardService, reservedProtectionService, donationService,
    secondaryReservedService
  } = deps;
  const sub = interaction.options.getSubcommand();
  if (sub === 'link') {
    await interaction.deferReply({ flags: MessageFlags.Ephemeral });
    const result = await linkService.begin({
      discordId: interaction.user.id,
      steamId: interaction.options.getString('steam_id', true),
      displayName: interaction.member?.displayName || interaction.user.globalName || interaction.user.username
    });
    await interaction.editReply({
      content: `Код отправлен вам личным сообщением внутри WARDOGS. Введите его командой \`/seed verify\` до ${discordTimestamp(result.expiresAt, 'T')}. Никому не показывайте код.`,
      allowedMentions: { parse: [] }
    });
    return;
  }
  if (sub === 'verify') {
    const user = linkService.verify({
      discordId: interaction.user.id,
      code: interaction.options.getString('code', true)
    });
    const role = await roleService.setSeeder(user.discordId, true);
    await interaction.reply({
      content: [
        `Привязка подтверждена. SteamID оканчивается на **${user.steamId.slice(-4)}**.`,
        role.ok
          ? `Роль <@&${config.discord.seederRoleId}> выдана — можно начинать сидинг.`
          : `Выдача роли <@&${config.discord.seederRoleId}> поставлена в защищённую очередь и повторится автоматически.`
      ].join('\n'),
      flags: MessageFlags.Ephemeral,
      allowedMentions: { parse: [] }
    });
    return;
  }
  if (sub === 'unlink') {
    if (interaction.options.getBoolean('confirm', true) !== true) {
      await interaction.reply({ content: 'Отвязка отменена.', flags: MessageFlags.Ephemeral });
      return;
    }
    const user = database.unlinkDiscord({ discordId: interaction.user.id });
    const [seederRole] = await Promise.all([
      roleService.setSeeder(user.discordId, false),
      roleService.setVip(user.discordId, false),
      roleService.setSuperSeeder(user.discordId, false)
    ]);
    await weeklyAwardService.handleUnlink(user);
    await interaction.reply({
      content: [
        `Привязка к Steam …${user.steamId.slice(-4)} отменена.`,
        seederRole.ok
          ? `Роль <@&${config.discord.seederRoleId}> снята.`
          : 'Снятие Discord-роли поставлено в очередь и будет повторено автоматически.',
        'Баллы и история сохранены в защищённом архиве. Они автоматически восстановятся только для той же пары Discord↔Steam; изменение одного из аккаунтов требует проверки администратора.'
      ].join('\n'),
      flags: MessageFlags.Ephemeral,
      allowedMentions: { parse: [] }
    });
    return;
  }
  if (sub === 'status') {
    await interaction.deferReply({ flags: MessageFlags.Ephemeral });
    const user = database.getUserByDiscord(interaction.user.id);
    if (!user) throw new DomainError('not_linked', database.getDonorByDiscord(interaction.user.id)
      ? 'У вас только VIP-привязка. Для участия в сидинге выполните /seed link с тем же SteamID и подтвердите код.'
      : 'Steam не привязан. Зайдите на сервер и выполните /seed link.');
    const member = database.getVipMembership(user.id);
    const paidUntil = donationService?.until(interaction.user.id);
    const weekly = database.getLeaderboard(weekKey()).find((row) => row.id === user.id);
    const superSeeder = weeklyAwardService.isActiveWinner(user.id);
    const permanentReserved = reservedProtectionService.isProtected(user.id, user.steamId);
    const slots = await liveSlotLines(user.steamId, deps.rcon, deps.rconSecondary);
    const embed = brandedEmbed({ color: BRAND.blue, title: '📊 Ваш сидинг WARDOGS | RU' })
      .addFields(
        { name: '💰 Баланс', value: `**${user.balance}** баллов`, inline: true },
        { name: '📅 За неделю', value: `**${formatDuration(weekly?.weeklySeconds || 0)}**`, inline: true },
        { name: '⏱️ Всего', value: `**${formatDuration(user.totalSeconds)}**`, inline: true },
        { name: '👑 VIP', value: vipLine(member) },
        { name: '💎 Оплаченный VIP', value: paidUntil && paidUntil > Date.now()
          ? `до ${discordTimestamp(paidUntil, 'f')}` : 'не активен' },
        {
          name: '🏆 Награда рейтинга',
          value: superSeeder
            ? `активна: роль <@&${config.discord.superSeederRoleId}>; состояние слотов показано ниже`
            : 'не активна'
        },
        {
          name: '🔒 Постоянный Reserved slot',
          value: permanentReserved ? 'защищён администрацией от автоматического удаления' : 'не закреплён'
        },
        { name: '🎫 Reserved slot (проверено сейчас)', value: slots },
        { name: '🎮 Сидинг сейчас', value: seedStateLine(seedingService.lastStatus, config) }
      )
      .setFooter(brandedFooter(`Steam …${user.steamId.slice(-4)}`));
    await interaction.editReply({ embeds: [embed] });
    return;
  }
  if (sub === 'top') {
    const key = weekKey();
    const rows = database.getLeaderboard(key).slice(0, 20);
    await interaction.reply({ embeds: leaderboardEmbeds(rows, key, { title: 'Топ сидеров этой недели' }), flags: MessageFlags.Ephemeral });
    return;
  }
  if (sub === 'shop') {
    await interaction.reply({ ...shopPayload(config), flags: MessageFlags.Ephemeral });
    return;
  }
  if (sub === 'buy') {
    await interaction.deferReply({ flags: MessageFlags.Ephemeral });
    await buyVip(interaction, interaction.options.getString('plan', true), vipService);
    return;
  }
  if (sub === 'help') {
    await interaction.reply({
      content: [
        `Сидинг учитывается с **${config.seed.start.text} до ${config.seed.end.text} МСК**, когда вы одновременно на любом из двух серверов и в заданном голосовом канале. Время и баллы общие; одновременно на двух серверах время не удваивается.`,
        'Первые 60 минут: 1 балл за каждые 6 минут. Затем: 1 балл за каждые 3 минуты.',
        `При ${config.seed.stopPlayerCount}+ игроках начисление останавливается. После снижения онлайна действует пауза ${Math.round(config.seed.resumeBelowSeconds / 60)} мин.`,
        'Привязка: зайдите в игру → `/seed link` → получите код в игре → `/seed verify`.',
        `После подтверждения выдаётся роль <@&${config.discord.seederRoleId}>. Топ-3 завершившейся недели получает <@&${config.discord.superSeederRoleId}> и Reserved slot на обоих серверах до следующего обновления.`,
        'Если нужен только оплаченный VIP без участия в сидинге, используйте `/vip-link link` и `/vip-link verify`.',
        'Отвязка: `/seed unlink confirm:true`. При активном или ожидающем VIP отвязка блокируется.'
      ].join('\n'),
      flags: MessageFlags.Ephemeral,
      allowedMentions: { parse: [] }
    });
  }
}

async function buyVip(interaction, sku, vipService) {
  const result = await vipService.purchase({
    discordId: interaction.user.id,
    sku,
    idempotencyKey: interaction.id
  });
  const status = result.membership?.state === 'active'
    ? `VIP активен до ${discordTimestamp(result.membership.activeUntil, 'f')}.`
    : 'Покупка поставлена в очередь. Срок начнётся после активации Reserved slot на обоих серверах.';
  const role = result.roleSync?.ok === false
    ? 'VIP-роль поставлена в очередь автоматической выдачи.'
    : 'VIP-роль выдана.';
  await interaction.editReply({
    content: `${result.product.label} приобретён за ${result.product.cost} баллов. Остаток: ${result.order.balance}.\n${role}\n${status}`,
    allowedMentions: { parse: [] }
  });
}

async function handleGather(interaction, deps, action) {
  const { config } = deps;
  await interaction.deferReply({ flags: MessageFlags.Ephemeral });
  const serverId = interaction.options.getString('server', true);
  if (action === 'start') {
    const result = await deps.gatherService.startGather({
      serverId, actorId: interaction.user.id, requestId: interaction.id,
      channelId: interaction.options.getChannel('channel')?.id || interaction.channelId,
      target: interaction.options.getInteger('target') ?? 20,
      roleId: interaction.options.getRole('role')?.id || config.discord.seederRoleId,
      minutes: interaction.options.getInteger('minutes') ?? 120,
      instructions: interaction.options.getString('instructions') || ''
    });
    const url = gatherMessageUrl(result.session, config.discord.guildId);
    await interaction.editReply({ content: `${result.created ? 'Сбор объявлен' : 'Сбор для этого сервера уже существует'}. `
      + `Цель: ${result.session.target} игроков. Канал: <#${result.session.channelId}>.`
      + (url ? `\n[Открыть карточку сбора](${url})` : '')
      + (result.session.deliveryPending ? '\nДанные сохранены, публикация ожидает повторной попытки. Проверьте права бота; новый сбор объявлять не нужно.' : ''),
    allowedMentions: { parse: [] } });
    return;
  }
  const session = action === 'stop'
    ? await deps.gatherService.cancel(serverId, interaction.user.id)
    : await deps.gatherService.status(serverId);
  if (!session) {
    await interaction.editReply({ content: 'Для этого сервера сбор ещё не объявлялся.', allowedMentions: { parse: [] } });
    return;
  }
  const payload = gatherCard(session);
  payload.components = [];
  payload.content = action === 'stop'
    ? (session.status === 'cancelled' ? 'Сбор остановлен.' : 'Сбор уже завершён.')
    : 'Состояние сбора проверено.';
  if (session.deliveryPending) payload.content += ' Обновление публичной карточки ожидает повторной попытки; проверьте права бота.';
  await interaction.editReply(payload);
}

async function handleAdmin(interaction, deps) {
  const {
    database, config, rcon, rconSecondary, vipService, secondaryReservedService,
    reportService, backupService,
    seedingService, roleService, weeklyAwardService, reservedProtectionService, logger, donationService
  } = deps;
  const sub = interaction.options.getSubcommand();
  const gatherAction = new Map([
    ['gather', 'start'], ['gather-status', 'status'], ['gather-stop', 'stop']
  ]).get(sub);
  if (gatherAction) {
    await handleGather(interaction, deps, gatherAction);
    return;
  }
  if (sub === 'health') {
    await interaction.deferReply({ flags: MessageFlags.Ephemeral });
    const [capabilitiesResult, statusResult, secondaryCapabilitiesResult, secondaryResult] = await Promise.allSettled([
      rcon.capabilities(),
      rcon.status(),
      rconSecondary.capabilities(),
      rconSecondary.status()
    ]);
    const integrity = database.verifyIntegrity();
    const rconOk = capabilitiesResult.status === 'fulfilled' && statusResult.status === 'fulfilled';
    const capabilities = capabilitiesResult.status === 'fulfilled' ? capabilitiesResult.value : null;
    const status = statusResult.status === 'fulfilled' ? statusResult.value : null;
    const normalizedRoutes = capabilities?.routes.map((route) => String(route)
      .replace(/\{[^}]+\}|:[^/\s]+/g, '*')) || [];
    const directVip = normalizedRoutes.includes('POST /v1/reserved-slots')
      && normalizedRoutes.includes('DELETE /v1/reserved-slots/*');
    const secondaryRoutes = secondaryCapabilitiesResult.status === 'fulfilled'
      ? secondaryCapabilitiesResult.value.routes.map((route) => String(route)
        .replace(/\{[^}]+\}|:[^/\s]+/g, '*')) : [];
    const secondaryDirectVip = secondaryRoutes.includes('POST /v1/reserved-slots')
      && secondaryRoutes.includes('DELETE /v1/reserved-slots/*');
    const secondaryRconOk = secondaryResult.status === 'fulfilled' &&
      secondaryCapabilitiesResult.status === 'fulfilled' &&
      secondaryRoutes.includes('GET /v1/status') &&
      secondaryRoutes.includes('GET /v1/players') &&
      secondaryRoutes.includes('POST /v1/players/*/message') &&
      secondaryRoutes.includes('GET /v1/reserved-slots');
    const failedResult = [capabilitiesResult, statusResult].find((result) => result.status === 'rejected');
    const rconError = failedResult?.reason;
    const health = rcon.healthSnapshot();
    const lastSuccess = health.lastSuccessAt
      ? `последний успешный ответ ${discordTimestamp(health.lastSuccessAt, 'R')}`
      : 'успешных ответов после запуска ещё не было';
    await interaction.editReply({
      content: [
        '✅ Discord: подключён',
        rconOk
          ? `✅ RCON сервер 1: ${status.playerCount}${status.maxPlayers ? `/${status.maxPlayers}` : ''} игроков`
          : `❌ RCON сервер 1: ${rconFailureReason(rconError)}; ${lastSuccess}`,
        secondaryRconOk
          ? `✅ RCON сервер 2: ${secondaryResult.value.playerCount}${secondaryResult.value.maxPlayers ? `/${secondaryResult.value.maxPlayers}` : ''} игроков`
          : `❌ RCON сервер 2: ${secondaryResult.status === 'rejected'
            ? rconFailureReason(secondaryResult.reason)
            : secondaryCapabilitiesResult.status === 'rejected'
              ? rconFailureReason(secondaryCapabilitiesResult.reason)
              : 'нет полного WDRCON API или доступа к Reserved slots'}; проверьте WDRCON`,
        `✅ База: ${integrity.users} активных привязок, ${integrity.archivedUsers} архивных, HMAC-цепочки целы`,
        `VIP сервер 1: ${rconOk ? (directVip ? 'мгновенная выдача и снятие доступны' : 'мгновенные команды отсутствуют; требуется обновление WDRCON') : 'ожидает восстановления RCON'}`,
        `VIP сервер 2: ${secondaryRconOk ? (secondaryDirectVip ? 'мгновенная выдача и снятие доступны' : 'мгновенные команды отсутствуют; требуется обновление WDRCON') : 'ожидает доступного WDRCON с поддержкой Reserved slots'}`,
        `Синхронизация слотов сервера 2: ${secondaryReservedService.lastStatus.state}`,
        `VIP-привязок без сидинга: ${database.countDonorLinks()}`,
        `Оплаченный VIP: ${donationService?.enabled ?
          (donationService.canRemove() ? 'журнал платежей доступен' : 'журнал недоступен; снятие слотов приостановлено') : 'не подключён'}`,
        `Транспорт: ${config.rcon.url.protocol === 'https:' ? 'HTTPS' : 'HTTP — обязательно ограничить firewall по IP'}`,
        `Сидинг: ${seedStateLine(seedingService.lastStatus, config)}`,
        `Роли Discord: ${roleService.pendingCount() ? `в очереди ${roleService.pendingCount()}` : 'синхронизированы'}`,
        `Награды недели: ${weeklyAwardService.snapshot().awardWeek || 'ожидают первого расчёта'}`,
        `Защищённые Reserved slots: ${reservedProtectionService.count()}`
      ].join('\n'),
      allowedMentions: { parse: [] }
    });
    return;
  }
  if (sub === 'points') {
    const target = interaction.options.getUser('user', true);
    const amount = interaction.options.getInteger('amount', true);
    const reason = interaction.options.getString('reason', true);
    const user = database.adjustPoints({
      discordId: target.id,
      delta: amount,
      reason,
      actorId: interaction.user.id,
      idempotencyKey: interaction.id
    });
    await interaction.reply({
      content: `Баланс изменён на ${amount > 0 ? '+' : ''}${amount}. Новый баланс: ${user.balance}.`,
      flags: MessageFlags.Ephemeral
    });
    return;
  }
  if (sub === 'time-add') {
    const target = interaction.options.getUser('user', true);
    const result = database.addSeedingTime({
      discordId: target.id,
      minutes: interaction.options.getInteger('minutes', true),
      reason: interaction.options.getString('reason', true),
      actorId: interaction.user.id,
      idempotencyKey: interaction.id
    });
    await interaction.reply({
      content: result.applied
        ? `Восстановлено ${result.minutes} мин сидинга за неделю ${result.weekKey}: +${result.points} баллов. `
          + `Всего ${formatDuration(result.user.totalSeconds)}, баланс ${result.user.balance} баллов. Причина сохранена в журнале.`
        : 'Эта корректировка уже применена; повторного начисления не было.',
      flags: MessageFlags.Ephemeral,
      allowedMentions: { parse: [] }
    });
    return;
  }
  if (sub === 'points-all') {
    const amount = interaction.options.getInteger('amount', true);
    const operationId = bulkOperationId(interaction);
    if (!interaction.options.getBoolean('confirm', true)) {
      await interaction.reply({ content: `Проверка: подтверждённых сидеров ${database.listUsers().length}; `
        + `каждый получит +${amount} баллов. Для начисления повторите команду с confirm:true `
        + `и тем же operation_id (${operationId}).`,
      flags: MessageFlags.Ephemeral });
      return;
    }
    await interaction.deferReply({ flags: MessageFlags.Ephemeral });
    const result = database.grantPointsToAll({
      amount,
      reason: interaction.options.getString('reason', true),
      actorId: interaction.user.id,
      idempotencyKey: `points-all:${operationId}`
    });
    await interaction.editReply(result.applied
      ? `Готово: ${result.count} сидерам начислено по ${result.amount} баллов `
        + `(всего ${result.totalPoints}). Отдельные VIP-привязки не затронуты. Операция записана в журнал.`
      : 'Эта массовая команда уже применена; повторного начисления не было.');
    return;
  }
  if (sub === 'time-add-all') {
    const minutes = interaction.options.getInteger('minutes', true);
    const operationId = bulkOperationId(interaction);
    if (!interaction.options.getBoolean('confirm', true)) {
      await interaction.reply({ content: `Проверка: подтверждённых сидеров ${database.listUsers().length}; `
        + `каждому будет добавлено ${minutes} мин. Баллы рассчитываются по правилам ручного восстановления. `
        + `Для начисления повторите команду с confirm:true и тем же operation_id (${operationId}).`,
      flags: MessageFlags.Ephemeral });
      return;
    }
    await interaction.deferReply({ flags: MessageFlags.Ephemeral });
    const result = database.addSeedingTimeToAll({
      minutes,
      reason: interaction.options.getString('reason', true),
      actorId: interaction.user.id,
      idempotencyKey: `time-add-all:${operationId}`
    });
    let reportUpdated = false;
    if (result.applied && result.count) {
      try { reportUpdated = !(await reportService.refresh()).skipped; }
      catch (error) { logger.warn('bulk_time_report_refresh_failed', { error }); }
    }
    await interaction.editReply(result.applied
      ? `Готово: ${result.count} сидерам добавлено по ${result.minutes} мин за неделю `
        + `${result.weekKey} (МСК), всего +${result.totalPoints} баллов. `
        + (reportUpdated ? 'Рейтинг обновлён.' : 'Карточка рейтинга обновится автоматически.')
      : 'Эта массовая команда уже применена; повторного начисления не было.');
    return;
  }
  if (sub === 'vip-sync') {
    await interaction.deferReply({ flags: MessageFlags.Ephemeral });
    if (donationService?.enabled) await donationService.refresh();
    const result = await vipService.reconcile();
    await roleService.reconcile();
    if (result.skipped) {
      await interaction.editReply('Сверка VIP уже выполняется. Повторите через несколько секунд.');
    } else if (result.failed > 0) {
      await interaction.editReply(
        `RCON недоступен для ${result.failed} из ${result.checked} проверок на двух серверах. VIP оставлены в безопасной очереди; баллы и сроки не потеряны.`
      );
    } else {
      await interaction.editReply(`VIP сверены с обоими серверами. Проверено записей: ${result.checked}.`);
    }
    return;
  }
  if (sub === 'report') {
    await interaction.deferReply({ flags: MessageFlags.Ephemeral });
    await weeklyAwardService.reconcile();
    await roleService.reconcile();
    const result = await reportService.refresh();
    await interaction.editReply(result.skipped
      ? 'Обновление рейтинга уже выполняется.'
      : `Карточки рейтинга и недельных победителей обновлены. Участников за неделю: ${result.rowCount}.`);
    return;
  }
  if (sub === 'seeders') {
    const rows = database.listSeeders(weekKey());
    const pageSize = 15;
    const pages = Math.max(1, Math.ceil(rows.length / pageSize));
    const page = interaction.options.getInteger('page') || 1;
    if (page > pages) throw new DomainError('bad_page', `Доступно страниц: ${pages}.`);
    const offset = (page - 1) * pageSize;
    const chunk = rows.slice(offset, offset + pageSize);
    const description = chunk.length
      ? chunk.map((row, index) => [
        `**${offset + index + 1}. ${safeName(row.displayName)}**`,
        `Steam …${row.steamId.slice(-4)} • неделя ${formatDuration(row.weeklySeconds)} • всего ${formatDuration(row.totalSeconds)} • ${row.balance} б. • VIP: ${vipShort(row.vip)} • Супер: ${weeklyAwardService.isActiveWinner(row.id) ? 'да' : 'нет'} • Постоянный слот: ${reservedProtectionService.isProtected(row.id, row.steamId) ? '🔒' : 'нет'}`
      ].join('\n')).join('\n\n')
      : 'Привязанных сидеров пока нет.';
    const embed = brandedEmbed({
      color: BRAND.dark,
      title: '🛡️ Список сидеров WARDOGS | RU',
      description
    })
      .setFooter(brandedFooter(`Активных привязок: ${rows.length} • страница ${page}/${pages}`))
      .setTimestamp();
    await interaction.reply({
      embeds: [embed],
      flags: MessageFlags.Ephemeral,
      allowedMentions: { parse: [] }
    });
    return;
  }
  if (sub === 'reserved-protect') {
    await interaction.deferReply({ flags: MessageFlags.Ephemeral });
    const target = interaction.options.getUser('user', true);
    const user = database.getUserByDiscord(target.id);
    if (!user) {
      throw new DomainError(
        'protected_user_not_linked',
        'Участник должен сначала подтвердить Steam через /seed link и /seed verify.'
      );
    }
    const protection = reservedProtectionService.protect({
      user,
      actorId: interaction.user.id
    });
    weeklyAwardService.markProtected(user.id, user.steamId);
    const paidClaims = database.getState('paid_slot_claims', {});
    if (paidClaims[user.steamId]) {
      paidClaims[user.steamId].owner = 'external';
      database.setState('paid_slot_claims', paidClaims);
    }

    const slotStatuses = [];
    try {
      // Always enqueue an idempotent add. The RCON client serializes mutations,
      // so this add wins over any cleanup that had already started.
      const result = await rcon.setReserved(user.steamId, true);
      slotStatuses.push(result.active === true
        ? '✅ Сервер 1: Reserved slot активен.'
        : '⚠️ Сервер 1: слот пока не активен.');
    } catch (error) {
      logger.warn('protected_reserved_slot_sync_deferred', { error, userId: user.id });
      slotStatuses.push(error?.code === 'live_reserved_unavailable'
        ? '⚠️ Сервер 1: WDRCON не поддерживает мгновенную выдачу.'
        : '⚠️ Сервер 1: ожидает восстановления RCON.');
    }
    try {
      const result = await secondaryReservedService.ensureUser(user, { permanent: true });
      slotStatuses.push(result.active
        ? '✅ Сервер 2: Reserved slot активен.'
        : '⚠️ Сервер 2: слот пока не активен.');
    } catch (error) {
      logger.warn('protected_secondary_slot_sync_deferred', { error, userId: user.id });
      slotStatuses.push(error?.code === 'live_reserved_unavailable'
        ? '⚠️ Сервер 2: WDRCON не поддерживает мгновенную выдачу.'
        : '⚠️ Сервер 2: ожидает восстановления RCON.');
    }

    const embed = brandedEmbed({
      color: BRAND.green,
      title: '🔒 Постоянный Reserved slot защищён',
      description: [
        `Участник: **${safeName(user.displayName)}**`,
        `SteamID64: **…${user.steamId.slice(-4)}**`,
        '',
        ...slotStatuses
      ].join('\n')
    })
      .addFields({
        name: 'Защита от автоматики',
        value: 'VIP и еженедельный рейтинг не смогут удалить слоты на обоих серверах. Повторный запуск команды безопасен.'
      })
      .setFooter(brandedFooter(protection.created ? 'Новая защищённая запись создана' : 'Защищённая запись подтверждена'))
      .setTimestamp();
    await interaction.editReply({ embeds: [embed], allowedMentions: { parse: [] } });
    return;
  }
  if (sub === 'reserved-unprotect') {
    const target = interaction.options.getUser('user', true);
    const result = reservedProtectionService.unprotectByDiscord({
      discordId: target.id,
      actorId: interaction.user.id
    });
    if (!result.removed) {
      throw new DomainError('reserved_not_protected', 'У этого участника нет защищённого постоянного Reserved slot.');
    }
    weeklyAwardService.markUnprotected(result.entry.userId, result.entry.steamId);
    const embed = brandedEmbed({
      color: BRAND.gold,
      title: '🔓 Защита Reserved slot снята',
      description: [
        `Участник: **${safeName(result.entry.displayName)}**`,
        `SteamID64: **…${result.entry.steamId.slice(-4)}**`,
        '',
        '**Reserved slots этой командой не удаляются.** Если их нужно убрать, сделайте это отдельно через RCON каждого сервера.'
      ].join('\n')
    })
      .setFooter(brandedFooter('Операция записана в защищённый аудит'))
      .setTimestamp();
    await interaction.reply({ embeds: [embed], flags: MessageFlags.Ephemeral, allowedMentions: { parse: [] } });
    return;
  }
  if (sub === 'reserved-protected') {
    const rows = reservedProtectionService.list();
    const pageSize = 15;
    const pages = Math.max(1, Math.ceil(rows.length / pageSize));
    const page = interaction.options.getInteger('page') || 1;
    if (page > pages) throw new DomainError('bad_page', `Доступно страниц: ${pages}.`);
    const offset = (page - 1) * pageSize;
    const chunk = rows.slice(offset, offset + pageSize);
    const description = chunk.length
      ? chunk.map((entry, index) => [
        `**${offset + index + 1}. ${safeName(entry.displayName)}**`,
        `<@${entry.discordId}> • Steam …${entry.steamId.slice(-4)} • защищён ${discordTimestamp(entry.protectedAt, 'd')}`
      ].join('\n')).join('\n\n')
      : 'Защищённых постоянных Reserved slots пока нет.';
    const embed = brandedEmbed({
      color: BRAND.dark,
      title: '🔐 Защищённые Reserved slots',
      description
    })
      .addFields({
        name: 'Гарантия',
        value: 'Эти записи не удаляются при окончании VIP или смене недельного топ-3.'
      })
      .setFooter(brandedFooter(`Всего: ${rows.length} • страница ${page}/${pages}`))
      .setTimestamp();
    await interaction.reply({ embeds: [embed], flags: MessageFlags.Ephemeral, allowedMentions: { parse: [] } });
    return;
  }
  if (sub === 'backup') {
    await interaction.deferReply({ flags: MessageFlags.Ephemeral });
    await backupService.create();
    await interaction.editReply('Зашифрованная резервная копия создана.');
  }
}

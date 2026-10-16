import { escapeMarkdown } from 'discord.js';
import { BRAND, brandedEmbed, brandedFooter } from '../discord/theme.js';
import { formatDuration, previousWeekKey, weekKey } from '../time.js';

function safeName(name) {
  return escapeMarkdown(String(name || 'Игрок').replace(/[\r\n\0]/g, ' ').slice(0, 70));
}

function placeIcon(place) {
  return ['🥇', '🥈', '🥉'][place - 1] || `**${place}.**`;
}

function leaderboardDescription(rows, offset = 0) {
  if (!rows.length) {
    return 'Пока нет подтверждённого времени. Зайдите на сервер и в голосовой канал — первое место свободно!';
  }
  return rows.map((row, index) => {
    const place = offset + index + 1;
    return `${placeIcon(place)} **${safeName(row.displayName)}**\n└ За неделю: **${formatDuration(row.weeklySeconds)}** • Всего: ${formatDuration(row.totalSeconds)}`;
  }).join('\n\n');
}

export function leaderboardEmbeds(rows, reportWeek, { title = '🏆 Текущий рейтинг сидеров' } = {}) {
  const pages = [];
  const source = rows.length ? rows : [{ empty: true }];
  for (let offset = 0; offset < source.length; offset += 20) {
    const chunk = rows.length ? rows.slice(offset, offset + 20) : [];
    pages.push(brandedEmbed({
      color: BRAND.blue,
      title,
      description: leaderboardDescription(chunk, offset)
    })
      .setFooter(brandedFooter(`Неделя с ${reportWeek} (МСК)${rows.length > 20 ? ` • страница ${pages.length + 1}` : ''}`))
      .setTimestamp());
  }
  return pages;
}

export function leaderboardCard(rows, reportWeek, updatedAt = Date.now(), config = null) {
  const top = rows.slice(0, 20);
  const reward = config?.discord?.superSeederRoleId
    ? `По итогам недели топ-3 получат <@&${config.discord.superSeederRoleId}> и Reserved slot до следующего обновления.`
    : 'По итогам недели три лидера получают награду сообщества.';
  return brandedEmbed({
    color: BRAND.blue,
    title: '🏆 Рейтинг сидеров WARDOGS | RU',
    description: [
      '**Текущая неделя** — места определяются только по подтверждённому времени сидинга.',
      '',
      leaderboardDescription(top)
    ].join('\n')
  })
    .addFields(
      { name: '🎁 Награда лидерам', value: reward },
      { name: '🔄 Обновление', value: 'Карточка обновляется автоматически; новое сообщение при каждом обновлении не создаётся.' }
    )
    .setFooter(brandedFooter(`Неделя с ${reportWeek} (МСК) • участников: ${rows.length}${rows.length > 20 ? ' • показан топ-20' : ''}`))
    .setTimestamp(updatedAt);
}

function rewardStatus(winner) {
  if (winner.active === false) return '⚪ награда снята после отвязки Steam';
  const role = winner.roleStatus === 'waiting_discord'
    ? 'роль ожидает восстановления Discord'
    : 'роль выдана';
  const protectedLabel = winner.slotProtected ? 'постоянный защищённый слот' : 'Reserved slot';
  if (winner.secondarySlotStatus) {
    const describe = (status) => status === 'active' ? 'активен' :
      status === 'pending_restart' ? 'ждёт рестарта' : 'ожидает синхронизации';
    return `${winner.slotProtected ? '🔒' : '🟢'} ${role}; ${protectedLabel}: `
      + `сервер 1 — ${describe(winner.slotStatus)}, сервер 2 — ${describe(winner.secondarySlotStatus)}`;
  }
  if (winner.slotStatus === 'active') return `${winner.slotProtected ? '🔒' : '🟢'} ${role}; ${protectedLabel} активен`;
  if (winner.slotStatus === 'pending_restart') return `🟡 ${role}; ${protectedLabel} ожидает рестарта сервера`;
  if (winner.slotStatus === 'waiting_rcon') return `🟠 ${role}; ${protectedLabel} ожидает восстановления RCON`;
  return `🔄 ${role}; ${protectedLabel} синхронизируется`;
}

export function weeklyWinnersCard(state, config, updatedAt = Date.now()) {
  const awardWeek = state?.awardWeek || previousWeekKey(updatedAt);
  const winners = Array.isArray(state?.winners) ? state.winners.slice(0, 3) : [];
  const embed = brandedEmbed({
    color: BRAND.gold,
    title: '👑 Супер сидеры прошлой недели',
    description: winners.length
      ? `Лучшие участники недели с **${awardWeek} (МСК)**. Награды действуют до следующего недельного обновления.`
      : `За завершившуюся неделю с **${awardWeek} (МСК)** подтверждённых участников не было.`
  });

  if (winners.length) {
    embed.addFields(...winners.map((winner, index) => ({
      name: `${placeIcon(index + 1)} ${safeName(winner.displayName)}`,
      value: [
        `Подтверждённое время: **${formatDuration(winner.weeklySeconds)}**`,
        rewardStatus(winner)
      ].join('\n'),
      inline: false
    })));
  }
  embed.addFields({
    name: '🏅 Награда',
    value: `Роль <@&${config.discord.superSeederRoleId}> + бесплатный Reserved slot на обоих игровых серверах.`
  });
  return embed
    .setFooter(brandedFooter('Награды пересматриваются автоматически каждую неделю'))
    .setTimestamp(updatedAt);
}

function isMissingMessage(error) {
  return Number(error?.code) === 10008 || Number(error?.status) === 404;
}

export class ReportService {
  constructor({ database, discord, config, logger, lease, weeklyAwardService = null }) {
    this.database = database;
    this.discord = discord;
    this.config = config;
    this.logger = logger;
    this.lease = lease;
    this.weeklyAwardService = weeklyAwardService;
    this.timer = null;
    this.running = false;
  }

  start() {
    this.refresh().catch((error) => this.logger.error('leaderboard_initial_refresh_failed', { error }));
    this.timer = setInterval(() => {
      this.refresh().catch((error) => this.logger.error('leaderboard_refresh_failed', { error }));
    }, this.config.seed.leaderboardRefreshSeconds * 1000);
    this.timer.unref();
  }

  stop() {
    if (this.timer) clearInterval(this.timer);
  }

  currentEmbeds() {
    const key = weekKey(Date.now());
    return leaderboardEmbeds(this.database.getLeaderboard(key), key);
  }

  async refresh() {
    if (!this.lease.held || this.running) return { skipped: true, rowCount: 0 };
    this.running = true;
    try {
      const guild = this.discord.guilds.cache.get(this.config.discord.guildId);
      const channel = guild?.channels.cache.get(this.config.discord.rankChannelId)
        || await guild?.channels.fetch(this.config.discord.rankChannelId);
      if (!channel?.isTextBased() || !channel.isSendable()) throw new Error('Rank channel is not sendable');

      const key = weekKey();
      const rows = this.database.getLeaderboard(key);
      const now = Date.now();
      const leaderboard = await this.#upsert(channel, {
        stateKey: 'leaderboard_message_id',
        event: 'leaderboard_message_created',
        warning: 'leaderboard_message_recreate',
        payload: {
          embeds: [leaderboardCard(rows, key, now, this.config)],
          components: [],
          allowedMentions: { parse: [] }
        }
      });
      const awardState = this.weeklyAwardService?.snapshot() || {
        awardWeek: previousWeekKey(now),
        winners: this.database.getLeaderboard(previousWeekKey(now)).slice(0, 3).map((row, index) => ({
          ...row,
          place: index + 1,
          active: true,
          slotStatus: 'checking'
        }))
      };
      const winners = await this.#upsert(channel, {
        stateKey: 'weekly_winners_message_id',
        event: 'weekly_winners_message_created',
        warning: 'weekly_winners_message_recreate',
        payload: {
          embeds: [weeklyWinnersCard(awardState, this.config, now)],
          components: [],
          allowedMentions: { parse: [] }
        }
      });
      return {
        skipped: false,
        created: leaderboard.created || winners.created,
        messageId: leaderboard.messageId,
        winnerMessageId: winners.messageId,
        rowCount: rows.length
      };
    } finally {
      this.running = false;
    }
  }

  async #upsert(channel, { stateKey, event, warning, payload }) {
    const prior = this.database.getState(stateKey, '');
    if (prior) {
      try {
        const message = await channel.messages.fetch(prior);
        await message.edit(payload);
        return { created: false, messageId: message.id };
      } catch (error) {
        if (!isMissingMessage(error)) throw error;
        this.logger.warn(warning, { error });
      }
    }
    const message = await channel.send(payload);
    this.database.setState(stateKey, message.id);
    this.database.audit(event, '', { weekKey: weekKey(), messageId: message.id });
    return { created: true, messageId: message.id };
  }

  async publish() {
    return this.refresh();
  }
}

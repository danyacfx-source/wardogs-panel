import { BRAND, brandedEmbed, brandedFooter } from './theme.js';

export function rulesPayload(config) {
  const rules = brandedEmbed({
    color: BRAND.red,
    title: '📜 Правила сидинга WARDOGS | RU',
    description: 'Помогайте серверу набрать онлайн, получайте баллы и обменивайте их на игровые привилегии.'
  })
    .addFields(
      {
        name: '⏰ Когда начисляются баллы',
        value: `Ежедневно с **${config.seed.start.text} до ${config.seed.end.text} МСК**. Вне этого времени сидинг не учитывается.`
      },
      {
        name: '🎮 Что нужно для участия',
        value: `Одновременно находиться на одном из двух серверов WARDOGS и в голосовом канале <#${config.discord.voiceChannelId}> с привязанным SteamID64. Время и баллы общие; одновременное присутствие не удваивает начисление.`
      },
      {
        name: '⭐ Начисление',
        value: [
          'Первый непрерывный час — **1 балл каждые 6 минут** (до 10 баллов).',
          'Со второго часа — **1 балл каждые 3 минуты** (20 баллов в час).'
        ].join('\n')
      },
      {
        name: '⏸️ Когда счётчик приостанавливается',
        value: [
          `На каждом сервере отдельно при онлайне **${config.seed.stopPlayerCount}+ игроков**. Возобновление — после ${Math.round(config.seed.resumeBelowSeconds / 60)} минут ниже порога.`,
          `Техническая пауза до ${Math.round(config.seed.sessionGraceSeconds / 60)} минут сохраняет стаж непрерывной сессии, но время паузы не оплачивается.`
        ].join('\n')
      }
    )
    .setDescription([
      'Помогайте серверу набрать онлайн, получайте баллы и обменивайте их на игровые привилегии.',
      '',
      'Попытки обхода правил, подмена аккаунта или передача учётной записи могут привести к проверке и отмене начислений.'
    ].join('\n'))
    .setFooter(brandedFooter('Баллы начисляются только после проверки Discord и игрового сервера'));

  const start = brandedEmbed({
    color: BRAND.green,
    title: '🚀 Как начать сидинг — пошагово',
    description: [
      '**Все команды можно выполнять прямо в этом канале.** Начните вводить `/seed`, затем выберите нужный пункт из меню Discord.',
      '',
      `**1. Зайдите на любой из двух игровых серверов WARDOGS.**  
Оставайтесь в игре до завершения привязки — бот должен увидеть ваш SteamID среди игроков.`,
      '',
      `**2. Подготовьте SteamID64.**  
Это 17 цифр, обычно начинающихся с \`7656\`. Укажите именно SteamID64, а не ссылку на профиль и не игровой ник.`,
      '',
      `**3. Выполните \`/seed link\`.**  
В поле \`steam_id\` вставьте SteamID64. Бот отправит одноразовый восьмизначный код личным сообщением внутри игры.`,
      '',
      `**4. Выполните \`/seed verify\`.**  
В поле \`code\` введите код из игры. После успешной проверки вы получите роль <@&${config.discord.seederRoleId}>.`,
      '',
      `**5. Перейдите в голосовой канал <#${config.discord.voiceChannelId}>.**  
Пока вы одновременно в игре на одном из двух серверов и в этом голосовом канале, бот считает общее время по правилам выше.`,
      '',
      `**6. Проверьте результат.**  
Команда \`/seed status\` показывает баланс, время, состояние сидинга и VIP. \`/seed top\` показывает текущий рейтинг.`,
      '',
      '**Нужен только оплаченный VIP?** Используйте `/vip-link link steam_id` и `/vip-link verify code`. Эта привязка не выдаёт роль сидера и не начисляет баллы.',
      '',
      '**Отвязка:** сидер — `/seed unlink confirm:true`, участник только с VIP — `/vip-link unlink confirm:true`. При действующем VIP отвязка недоступна; история сидера сохраняется.'
    ].join('\n')
  })
    .setFooter(brandedFooter('Обычные сообщения в этом канале автоматически удаляются; slash-команды работают'));

  const rewards = brandedEmbed({
    color: BRAND.gold,
    title: '👑 Магазин наград и VIP',
    description: `Откройте <#${config.discord.shopChannelId}> и нажмите кнопку нужной награды. Покупка и ответ бота видны только вам.`
  })
    .addFields(
      { name: '🎫 VIP на 1 день', value: `**${config.vip.oneDayCost} баллов**`, inline: true },
      { name: '🎟️ VIP на 7 дней', value: `**${config.vip.sevenDayCost} баллов**`, inline: true },
      { name: '💎 VIP на 30 дней', value: `**${config.vip.thirtyDayCost} баллов**`, inline: true },
      {
        name: 'Что входит',
        value: `Роль <@&${config.discord.vipRoleId}> и Reserved slot на обоих серверах. Срок начинается после подтверждённой активации слота на обоих.`
      },
      {
        name: '🔒 Уже есть постоянный слот?',
        value: 'Магазин не спишет баллы за дублирующий VIP. Защищённый администрацией Reserved slot сохраняется независимо от VIP и рейтинга.'
      }
    )
    .setFooter(brandedFooter('Баланс и статус: /seed status'));

  const ranking = brandedEmbed({
    color: BRAND.blue,
    title: '🏆 Еженедельный рейтинг и награды',
    description: `Рейтинг в <#${config.discord.rankChannelId}> строится по подтверждённому времени сидинга за текущую неделю и обновляется автоматически.`
  })
    .addFields(
      {
        name: '🥇🥈🥉 Награда топ-3',
        value: `Три лучших сидера завершившейся недели получают роль <@&${config.discord.superSeederRoleId}> и бесплатный Reserved slot на обоих серверах.`
      },
      {
        name: '📅 Срок награды',
        value: 'Неделя считается с понедельника 00:00 до следующего понедельника 00:00 МСК. Роль и слот действуют до следующего обновления, затем бот передаёт их новой тройке лидеров.'
      },
      {
        name: '📊 Две карточки рейтинга',
        value: 'Первая показывает живой рейтинг текущей недели, вторая — трёх победителей прошлой недели и состояние их наград.'
      },
      {
        name: '🔐 Постоянные Reserved slots',
        value: 'Слоты на обоих серверах, отдельно защищённые администрацией, не снимаются при окончании VIP или при смене победителей рейтинга.'
      }
    )
    .setFooter(brandedFooter('Чем больше подтверждённого времени — тем выше место'));

  return { embeds: [rules, start, rewards, ranking], allowedMentions: { parse: [] } };
}

function isMissingMessage(error) {
  return Number(error?.code) === 10008 || Number(error?.status) === 404;
}

export async function publishRulesPanel({ database, discord, config, logger }) {
  const guild = discord.guilds.cache.get(config.discord.guildId);
  const channel = guild?.channels.cache.get(config.discord.rulesChannelId)
    || await guild?.channels.fetch(config.discord.rulesChannelId);
  if (!channel?.isTextBased() || !channel.isSendable()) throw new Error('Rules channel is not sendable');
  const payload = rulesPayload(config);
  const prior = database.getState('rules_message_id', '');
  if (prior) {
    try {
      const message = await channel.messages.fetch(prior);
      await message.edit(payload);
      return message;
    } catch (error) {
      if (!isMissingMessage(error)) throw error;
      logger.warn('rules_message_recreate', { error });
    }
  }
  const message = await channel.send(payload);
  database.setState('rules_message_id', message.id);
  return message;
}

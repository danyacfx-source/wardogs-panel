import { ActionRowBuilder, ButtonBuilder, ButtonStyle, escapeMarkdown } from 'discord.js';
import { BRAND, brandedEmbed, brandedFooter } from './theme.js';
import { discordTimestamp } from '../time.js';

const name = (text) => escapeMarkdown(String(text || '').replace(/[\r\n\0]/g, ' ').slice(0, 200));
export const gatherMarker = (session, kind = 'card') =>
  `${kind === 'done' ? 'Итог сбора' : 'Сбор'} №${session.id}`;

export function gatherCard(session) {
  const reached = session.status === 'reached';
  const closed = ['cancelled', 'expired'].includes(session.status);
  let description;
  if (reached) description = `**Цель достигнута: ${session.population}/${session.target} игроков!**\nСпасибо каждому, кто помог собрать стаю.`;
  else if (closed) description = session.status === 'cancelled'
    ? 'Сбор остановлен администрацией.' : 'Время сбора истекло. Можно объявить новый сбор.';
  else if (!session.sampleOk) description = 'Связь с сервером временно недоступна. **Число игроков уточняется.**';
  else {
    const missing = Math.max(0, session.target - session.population);
    const filled = Math.min(10, Math.floor(session.population / session.target * 10));
    description = `**Сейчас ${session.population}/${session.target} — нужно ещё ${missing}**\n\n` +
      `${'🟩'.repeat(filled)}${'⬛'.repeat(10 - filled)}\n\nЗаходите на сервер и помогите запустить игру!`;
  }
  const embed = brandedEmbed({ color: reached ? BRAND.green : closed ? BRAND.dark : BRAND.gold,
    title: `${reached ? '🎉 Цель достигнута' : closed ? '🏁 Сбор завершён' : '📣 Собираем стаю'} · ${session.label}`,
    description })
    .addFields(
      { name: '🎮 Игровой сервер', value: name(session.serverName) || session.label },
      { name: '🎯 Цель', value: `${session.target} игроков`, inline: true },
      { name: '⏳ Срок сбора', value: discordTimestamp(session.expiresAt, 'R'), inline: true }
    ).setFooter(brandedFooter(gatherMarker(session)));
  if (session.sampleAt) embed.addFields({ name: reached || closed || !session.sampleOk ? 'Последняя проверка' : 'Данные обновлены',
    value: `${discordTimestamp(session.sampleAt, 'R')}${!session.sampleOk && session.population !== null ? ` · последний известный онлайн: ${session.population}` : ''}` });
  return { embeds: [embed], components: [new ActionRowBuilder().addComponents(
    new ButtonBuilder().setCustomId(`seed:gather:connect:${session.serverId}:${session.id}`)
      .setLabel('Как подключиться').setEmoji('🎮').setStyle(ButtonStyle.Primary)
  )], allowedMentions: { parse: [], repliedUser: false } };
}

export function gatherReachedCard(session) {
  return { embeds: [brandedEmbed({ color: BRAND.green,
    title: `🎉 Порог достигнут · ${session.label}`,
    description: `На сервере **${name(session.serverName) || session.label}** собрались **${session.population}/${session.target} игроков**.\n\nСпасибо за помощь, стая! 🐺`
  }).setFooter(brandedFooter(gatherMarker(session, 'done')))], allowedMentions: { parse: [], repliedUser: false } };
}

export function gatherInstructions(session, config) {
  const instructions = session.instructions || (session.serverName
    ? `1. Запустите **WARDOGS**.\n2. Откройте список игровых серверов и найдите **${name(session.serverName)}**.\n3. Подключитесь к этому серверу и дождитесь загрузки.`
    : 'Название игрового сервера пока не получено. Попробуйте открыть инструкцию после восстановления связи или уточните название у администрации.');
  return { embeds: [brandedEmbed({ color: BRAND.blue,
    title: `🎮 Подключение · ${session.label}`, description: instructions
  }).addFields({ name: '🐾 Если участвуете в сидинге',
    value: `Зайдите в голосовой канал <#${config.discord.voiceChannelId}>. Для начисления времени и баллов используйте подтверждённую Steam-привязку. Правила: <#${config.discord.rulesChannelId}> и команда \`/seed help\`.`
  }).setFooter(brandedFooter())], allowedMentions: { parse: [], repliedUser: false } };
}

export function gatherMessageUrl(session, guildId) {
  return session.cardMessageId
    ? `https://discord.com/channels/${guildId}/${session.channelId}/${session.cardMessageId}` : null;
}

import {
  ActionRowBuilder,
  ButtonBuilder,
  ButtonStyle,
} from 'discord.js';
import { vipCatalog } from '../services/vip-service.js';
import { BRAND, brandedEmbed, brandedFooter } from './theme.js';

export function shopPayload(config) {
  const catalog = vipCatalog(config);
  const embed = brandedEmbed({
    color: BRAND.gold,
    title: '👑 Магазин наград WARDOGS | RU',
    description: 'Обменивайте заработанные за сидинг баллы на VIP: Discord-роль и Reserved slot на обоих серверах.'
  })
    .addFields(
      {
        name: `🎫 VIP на 1 день • ${catalog['1d'].cost} баллов`,
        value: `Роль <@&${config.discord.vipRoleId}> и Reserved slot на обоих серверах на 24 часа после подтверждённой активации.`
      },
      {
        name: `🎟️ VIP на 7 дней • ${catalog['7d'].cost} баллов`,
        value: `Роль <@&${config.discord.vipRoleId}> и Reserved slot на обоих серверах на 7 суток после подтверждённой активации.`
      },
      {
        name: `💎 VIP на 30 дней • ${catalog['30d'].cost} баллов`,
        value: `Роль <@&${config.discord.vipRoleId}> и Reserved slot на обоих серверах на 30 суток после подтверждённой активации.`
      },
      {
        name: 'ℹ️ Как купить',
        value: [
          '1. Убедитесь, что Steam привязан и на балансе достаточно очков.',
          '2. Нажмите кнопку нужного тарифа ниже.',
          '3. Бот выдаст VIP-роль и синхронизирует Reserved slot на обоих серверах.',
          '',
          'Срок VIP начинается только после появления SteamID в live reserved slots обоих серверов. Покупка и ответ бота видны только вам.'
        ].join('\n')
      }
    )
    .setFooter(brandedFooter('Баланс и статус: /seed status'));
  const row = new ActionRowBuilder().addComponents(
    new ButtonBuilder().setCustomId('seed:buy:1d').setLabel('VIP 1 день').setEmoji('🎫').setStyle(ButtonStyle.Primary),
    new ButtonBuilder().setCustomId('seed:buy:7d').setLabel('VIP 7 дней').setEmoji('🎟️').setStyle(ButtonStyle.Primary),
    new ButtonBuilder().setCustomId('seed:buy:30d').setLabel('VIP 30 дней').setEmoji('💎').setStyle(ButtonStyle.Success)
  );
  return { embeds: [embed], components: [row], allowedMentions: { parse: [] } };
}

function isMissingMessage(error) {
  return Number(error?.code) === 10008 || Number(error?.status) === 404;
}

export async function publishShopPanel({ database, discord, config, logger }) {
  const guild = discord.guilds.cache.get(config.discord.guildId);
  const channel = guild?.channels.cache.get(config.discord.shopChannelId)
    || await guild?.channels.fetch(config.discord.shopChannelId);
  if (!channel?.isTextBased() || !channel.isSendable()) throw new Error('Shop channel is not sendable');
  const payload = shopPayload(config);
  const prior = database.getState('shop_message_id', '');
  if (prior) {
    try {
      const message = await channel.messages.fetch(prior);
      await message.edit(payload);
      return message;
    } catch (error) {
      if (!isMissingMessage(error)) throw error;
      logger.warn('shop_message_recreate', { error });
    }
  }
  const message = await channel.send(payload);
  database.setState('shop_message_id', message.id);
  return message;
}

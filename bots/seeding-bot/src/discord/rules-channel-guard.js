import { Events } from 'discord.js';

export function installRulesChannelGuard({ discord, config, logger }) {
  discord.on(Events.MessageCreate, async (message) => {
    if (message.guildId !== config.discord.guildId) return;
    if (message.channelId !== config.discord.rulesChannelId) return;
    if (message.author?.id === discord.user?.id) return;
    try {
      await message.delete();
    } catch (error) {
      logger.warn('rules_channel_message_remove_failed', { error });
    }
  });
}

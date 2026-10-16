import { EmbedBuilder } from 'discord.js';

export const BRAND = Object.freeze({
  red: 0xd71920,
  gold: 0xf3b33d,
  blue: 0x2f9bff,
  green: 0x35b66f,
  dark: 0x252a34,
  footer: 'WARDOGS | RU • Система сидинга'
});

export function brandedEmbed({ color = BRAND.red, title, description } = {}) {
  const embed = new EmbedBuilder().setColor(color);
  if (title) embed.setTitle(title);
  if (description) embed.setDescription(description);
  return embed;
}

export function brandedFooter(extra = '') {
  return { text: extra ? `${BRAND.footer} • ${extra}` : BRAND.footer };
}

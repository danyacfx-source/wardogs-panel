'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
process.env.BOT_TOKEN = 'test';
process.env.CLIENT_ID = '1553090039615852675';
process.env.SITE_BASE_URL = 'https://wardogs-ru.ru';
process.env.CHECKOUT_SECRET = 'x'.repeat(64);
process.env.FEED_TOKEN = 'y'.repeat(64);
const { PermissionFlagsBits, PermissionsBitField, MessageFlags } = require('discord.js');
const { supportCard, vipCard, topCard, thanksCard, commands, handleInteraction } = require('../src/index');
const { VIP_ROLE_ID, TIERS } = require('../src/catalog');
const { applyManualDonation } = require('../src/core');

test('Discord cards serialize with the sponsor button, two VIP lists and leaderboard', () => {
  const support = supportCard();
  const vip = vipCard();
  const top = topCard();
  assert.equal(support.components[0].toJSON().components[0].label, 'Поддержать');
  assert.equal(vip.components.length, 2);
  assert.equal(vip.components[0].toJSON().components[0].options.length, 3);
  assert.equal(vip.components[1].toJSON().components[0].options.length, 3);
  assert.match(support.embeds[0].toJSON().description, /развивать игровой сервер/);
  assert.match(support.embeds[0].toJSON().description, /Минимальный перевод — 50 ₽/);
  assert.match(support.embeds[0].toJSON().fields.map(field => field.value).join(' '), /не выдаёт reserved slot/);
  assert.match(vip.embeds[0].toJSON().fields.map(field => field.value).join(' '), /vip-link link/);
  assert.match(vip.embeds[0].toJSON().description, /<@&1552725871830503484>/);
  assert.doesNotMatch(vip.embeds[0].toJSON().fields.map(field => field.value).join(' '), /seed link/);
  assert.match(top.embeds[0].toJSON().title, /Топ 10/);
});

test('VIP uses the existing shared role and never creates one', () => {
  assert.equal(VIP_ROLE_ID, '1552725871830503484');
  const source = readFileSync(require.resolve('../src/index'), 'utf8');
  assert.doesNotMatch(source, /guild\.roles\.create\(/);
  assert.doesNotMatch(source, /member\.roles\.(?:add|remove)\(\s*VIP_ROLE_ID/);
});

test('manual admin command requires Manage Guild and collects exact rubles and recovery ID', () => {
  const command = commands.map(c => c.toJSON()).find(c => c.name === 'donation-admin');
  assert.equal(command.default_member_permissions, PermissionFlagsBits.ManageGuild.toString());
  const add = command.options.find(o => o.name === 'add');
  assert.deepEqual(add.options.map(o => [o.name, o.required === true]), [
    ['user', true], ['amount', true], ['reason', true], ['operation_id', false],
  ]);
  assert.equal(add.options.find(o => o.name === 'amount').type, 3);
  const remove = command.options.find(o => o.name === 'remove');
  assert.deepEqual(remove.options.map(o => [o.name, o.required === true]), [
    ['user', true], ['amount', true], ['reason', true],
  ]);
  assert.equal(remove.options.find(o => o.name === 'amount').type, 3);
});

test('manual command rejects a member without Manage Guild before accepting the credit', async () => {
  let reply;
  const originalError = console.error;
  console.error = () => {};
  try {
    await handleInteraction({
      guildId: process.env.GUILD_ID || '403186008579112973',
      isButton: () => false, isModalSubmit: () => false,
      isStringSelectMenu: () => false, isUserSelectMenu: () => false,
      isChatInputCommand: () => true, commandName: 'donation-admin',
      memberPermissions: new PermissionsBitField(0n),
      async deferReply() { assert.fail('An unauthorized credit must not start'); },
      async reply(payload) { reply = payload; },
    });
  } finally { console.error = originalError; }
  assert.equal(reply.content, 'Недостаточно прав');
  assert.equal(reply.flags, MessageFlags.Ephemeral);
});

test('manual support uses the same public thanks card, mentions and upgrade celebration', () => {
  const buyerId = '1553090039615852675';
  const state = { orders: {}, donors: { [buyerId]: { total: 45000 } }, payments: {}, reviews: {}, vip: {} };
  const { payment } = applyManualDonation(state, { buyerId, amount: 5000,
    adminId: '1553092734791983174', requestId: '1553093158441590975',
    reason: 'Private staff audit reason' });
  const card = thanksCard(payment);
  const embed = card.embeds[0].toJSON();
  assert.match(embed.title, /Новый уровень поддержки/);
  assert.match(embed.description, new RegExp(`<@${buyerId}>`));
  assert.match(embed.fields.find(f => f.name === 'Последний перевод').value, /50/);
  assert.match(embed.fields.find(f => f.name === 'Всего поддержано').value, /500/);
  assert.match(embed.fields.find(f => f.name === '🏅 Поздравляем!').value, /Друг стаи/);
  assert.deepEqual(card.allowedMentions.users, [buyerId]);
  assert.equal(card.enforceNonce, true);
  assert.doesNotMatch(JSON.stringify(card), /Private staff audit reason|1553092734791983174/);
  assert.match(topCard(state).embeds[0].toJSON().description, /500/);
  const normalCard = thanksCard({ ...payment, source: undefined, adminId: undefined, reason: undefined });
  assert.deepEqual(card.embeds[0].toJSON(), normalCard.embeds[0].toJSON());
  assert.equal(payment.newRoleId, TIERS[0].roleId);
});

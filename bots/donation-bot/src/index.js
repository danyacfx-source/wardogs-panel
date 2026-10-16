'use strict';

const { createHash, randomBytes } = require('node:crypto');
const {
  Client, GatewayIntentBits, Events, REST, Routes, SlashCommandBuilder,
  PermissionFlagsBits, MessageFlags, EmbedBuilder, ActionRowBuilder, ButtonBuilder,
  ButtonStyle, StringSelectMenuBuilder, UserSelectMenuBuilder, ModalBuilder,
  TextInputBuilder, TextInputStyle, ChannelType,
} = require('discord.js');
const { loadConfig } = require('./config');
const { TIERS, PRODUCTS, VIP_ROLE_ID } = require('./catalog');
const { Store } = require('./store');
const { SiteFeed } = require('./site-feed');
const { PaymentWorkflow } = require('./payment-workflow');
const { formatRub, parseRub, parseRemovalRub, tierFor, createOrder, checkoutToken, applyPayment, topDonors, ManualDonationError } = require('./core');

const config = loadConfig();
const store = new Store(config.dataDir);
const feed = new SiteFeed(config.siteBaseUrl, config.feedToken);
const client = new Client({ intents: [GatewayIntentBits.Guilds] });
let ticking = false;
const noMentions = { parse: [] };

const commands = [
  new SlashCommandBuilder().setName('donate').setDescription('Поддержка WARDOGS RU')
    .addSubcommand(s => s.setName('status').setDescription('Мои донаты и VIP')),
  new SlashCommandBuilder().setName('donation-admin').setDescription('Управление донатным ботом')
    .setDefaultMemberPermissions(PermissionFlagsBits.ManageGuild)
    .addSubcommand(s => s.setName('setup').setDescription('Проверить VIP роль, создать чат и опубликовать карточки'))
    .addSubcommand(s => s.setName('check').setDescription('Проверить новые платежи и привилегии'))
    .addSubcommand(s => s.setName('review').setDescription('Показать платежи, требующие ручной проверки'))
    .addSubcommand(s => s.setName('add').setDescription('Вручную учесть поддержку: сумма, роль, рейтинг и благодарность')
      .addUserOption(o => o.setName('user').setDescription('Пользователь, чья поддержка будет учтена').setRequired(true))
      .addStringOption(o => o.setName('amount').setDescription('Сумма в рублях от 50 до 100 000, например 500 или 500,50').setRequired(true).setMaxLength(9))
      .addStringOption(o => o.setName('reason').setDescription('Причина ручной записи; видна только в журнале данных').setRequired(true).setMinLength(1).setMaxLength(300))
      .addStringOption(o => o.setName('operation_id').setDescription('ID операции ЮMoney: защита от повторного учёта позднего уведомления').setMinLength(1).setMaxLength(80)))
    .addSubcommand(s => s.setName('remove').setDescription('Вычесть сумму из поддержки и обновить роль и рейтинг')
      .addUserOption(o => o.setName('user').setDescription('Пользователь, у которого снимается сумма поддержки').setRequired(true))
      .addStringOption(o => o.setName('amount').setDescription('Сумма списания в рублях от 0,01 до 100 000, например 500 или 0,50').setRequired(true).setMaxLength(9))
      .addStringOption(o => o.setName('reason').setDescription('Причина списания; видна только в журнале данных').setRequired(true).setMinLength(1).setMaxLength(300))),
];

function supportCard() {
  const embed = new EmbedBuilder().setColor(0xd8aa5c)
    .setAuthor({ name: 'WARDOGS RU · СООБЩЕСТВО' })
    .setTitle('🐾 Поддержать нашу стаю')
    .setThumbnail(new URL('/donate/assets/paw.png', config.siteBaseUrl).toString())
    .setDescription('Добровольная поддержка помогает оплачивать и развивать игровой сервер, развивать сообщество, проводить события и создавать новые возможности для игроков. Минимальный перевод — 50 ₽. Каждый подтверждённый перевод прибавляется к вашей общей сумме.')
    .addFields(
      { name: '🎖️ Спонсорские роли', value: TIERS.map(t => `${t.icon} **${t.name}** — от **${formatRub(t.min)}**`).join('\n') },
      { name: '💎 Преимущества', value: 'Закрытый чат спонсоров. Возможность попросить администратора выбрать команду, если он онлайн и переход не создаст явного дисбаланса. Спонсорская роль сама по себе не выдаёт reserved slot.' },
      { name: '🔗 SteamID и игровой доступ', value: 'Для обычной поддержки Steam-привязка не нужна. Если покупаете VIP, привяжите Steam через `/vip-link link steam_id` и `/vip-link verify code`. Привязка сидера также подходит для VIP.' },
      { name: '🤝 Прозрачность', value: 'Поддержка добровольна. Нажмите кнопку, укажите сумму и оплатите по персональной ссылке ЮMoney. Одна подтверждённая операция учитывается один раз.' },
    ).setFooter({ text: 'WARDOGS RU · Вместе держим рубеж' });
  const buttons = new ActionRowBuilder().addComponents(new ButtonBuilder().setCustomId('support:open').setLabel('Поддержать').setEmoji('💛').setStyle(ButtonStyle.Primary));
  return { embeds: [embed], components: [buttons], allowedMentions: noMentions };
}
function vipCard() {
  const embed = new EmbedBuilder().setColor(0x9d7deb).setAuthor({ name: 'WARDOGS RU · VIP' })
    .setTitle('💎 VIP-доступ на игровой сервер')
    .setThumbnail(new URL('/donate/assets/vip.png', config.siteBaseUrl).toString())
    .setDescription(`VIP действует указанный срок с момента подтверждения оплаты. Участник получает готовую роль <@&${VIP_ROLE_ID}> и reserved slot при привязанном SteamID. Покупка также входит в общую сумму донатов **покупателя**.`)
    .addFields(
      { name: '👤 Личный VIP', value: '1 месяц — **500 ₽**\n3 месяца — **1 200 ₽**\n12 месяцев — **4 800 ₽**', inline: true },
      { name: '🛡️ VIP для клана', value: '10 участников / 1 месяц — **4 500 ₽**\n20 участников / 1 месяц — **8 500 ₽**\n30 участников / 1 месяц — **12 000 ₽**', inline: true },
      { name: '📌 Порядок', value: 'Выберите вариант ниже. Для кланового пакета отметьте всех получателей до оплаты. Каждый получатель привязывает SteamID через `/vip-link link steam_id` и `/vip-link verify code`; уже привязанным сидерам повторять это не нужно. Без привязки слот не появится. Повторная покупка продлевает срок.' },
    ).setFooter({ text: 'VIP роль действует оплаченный срок · слот сохраняется при другом действующем основании' });
  const personal = new StringSelectMenuBuilder().setCustomId('vip:personal').setPlaceholder('Выбрать личный VIP').addOptions(
    ['personal_1','personal_3','personal_12'].map(id => ({ label: PRODUCTS[id].name, value: id, description: formatRub(PRODUCTS[id].amount) })));
  const clan = new StringSelectMenuBuilder().setCustomId('vip:clan').setPlaceholder('Выбрать VIP для клана').addOptions(
    ['clan_10','clan_20','clan_30'].map(id => ({ label: PRODUCTS[id].name, value: id, description: formatRub(PRODUCTS[id].amount) })));
  return { embeds: [embed], components: [new ActionRowBuilder().addComponents(personal), new ActionRowBuilder().addComponents(clan)], allowedMentions: noMentions };
}
function topCard(state = store.data) {
  const rows = topDonors(state || { donors: {} });
  const marks = ['🥇','🥈','🥉'];
  const description = rows.length ? rows.map(([id, donor], index) => {
    const tier = tierFor(donor.total);
    return `${marks[index] || '◆'} **${index + 1}.** <@${id}>  ·  **${formatRub(donor.total)}**${tier ? `  ·  ${tier.icon} ${tier.name}` : ''}`;
  }).join('\n\n') : 'Пока нет подтверждённых публичных донатов. Первый герой стаи может появиться здесь!';
  return { embeds: [new EmbedBuilder().setColor(0xe0b75b).setAuthor({ name: 'WARDOGS RU · ЗАЛ СЛАВЫ' })
    .setTitle('🏆 Топ 10 спонсоров').setDescription(description)
    .setThumbnail(new URL('/donate/assets/legend.png', config.siteBaseUrl).toString())
    .setFooter({ text: 'Рейтинг обновляется после каждого подтверждённого доната · суммы накопительные' })], allowedMentions: noMentions };
}
async function channel(guild, id) {
  const found = await guild.channels.fetch(id);
  if (!found?.isTextBased() || !found.send) throw new Error(`Channel ${id} is unavailable`);
  return found;
}
async function upsertPanel(guild, key, id, payload) {
  const target = await channel(guild, id);
  const messageId = store.data.panels[key];
  if (messageId) {
    const old = await target.messages.fetch(messageId).catch(() => null);
    if (old?.author.id === client.user.id) { await old.edit(payload); return; }
  }
  const sent = await target.send(payload);
  await store.mutate(s => { s.panels[key] = sent.id; });
}
async function refreshTop(guild) { await upsertPanel(guild, 'top', config.channels.top, topCard()); }
async function setup(guild) {
  await guild.members.fetchMe();
  for (const tier of TIERS) {
    const role = await guild.roles.fetch(tier.roleId);
    if (!role || !role.editable) throw new Error(`Cannot manage sponsor role ${tier.name}`);
  }
  const vipRole = await guild.roles.fetch(VIP_ROLE_ID);
  if (!vipRole) throw new Error(`Готовая VIP роль ${VIP_ROLE_ID} не найдена на сервере`);
  if (store.data.vipRoleId !== VIP_ROLE_ID) await store.mutate(s => {
    if (s.vipRoleId && s.vipRoleId !== VIP_ROLE_ID) s.legacyVipRoleId = s.vipRoleId;
    s.vipRoleId = VIP_ROLE_ID;
  });
  let sponsorChat = store.data.sponsorChannelId ? await guild.channels.fetch(store.data.sponsorChannelId) : null;
  if (!sponsorChat) {
    sponsorChat = await guild.channels.create({ name: '💎・спонсоры', type: ChannelType.GuildText, permissionOverwrites: [
      { id: guild.roles.everyone.id, deny: [PermissionFlagsBits.ViewChannel] },
      { id: client.user.id, allow: [PermissionFlagsBits.ViewChannel, PermissionFlagsBits.SendMessages, PermissionFlagsBits.ReadMessageHistory] },
      ...TIERS.map(t => ({ id: t.roleId, allow: [PermissionFlagsBits.ViewChannel, PermissionFlagsBits.SendMessages, PermissionFlagsBits.ReadMessageHistory] })),
    ], reason: 'Закрытый чат спонсоров' });
    await store.mutate(s => { s.sponsorChannelId = sponsorChat.id; });
  }
  await upsertPanel(guild, 'support', config.channels.support, supportCard());
  await upsertPanel(guild, 'vip', config.channels.vip, vipCard());
  await refreshTop(guild);
  return { vipRoleId: vipRole.id, sponsorChannelId: sponsorChat.id,
    legacyVipRoleId: store.data.legacyVipRoleId || null };
}
function checkoutUrl(order) {
  const url = new URL('/donate/index.php', config.siteBaseUrl);
  url.searchParams.set('o', checkoutToken(order, config.checkoutSecret));
  return url.toString();
}
async function saveOrderAndReply(interaction, order, mode = 'reply') {
  await store.mutate(s => { s.orders[order.id] = order; });
  const button = new ActionRowBuilder().addComponents(new ButtonBuilder().setURL(checkoutUrl(order)).setLabel('Перейти к переводу').setEmoji('💛').setStyle(ButtonStyle.Link));
  const payload = { content: `**${order.kind === 'donation' ? 'Поддержка сообщества' : PRODUCTS[order.productId].name} · ${formatRub(order.amountKopecks)}**\nОткройте персональную страницу оплаты до <t:${Math.floor(order.expiresAt / 1000)}:F>. После оплаты статус обновится автоматически.`, components: [button], allowedMentions: noMentions };
  if (mode === 'update') await interaction.update(payload);
  else await interaction.reply({ ...payload, flags: MessageFlags.Ephemeral });
}
function recipientMenu(cart) {
  const left = PRODUCTS[cart.productId].seats - cart.recipients.length;
  return new ActionRowBuilder().addComponents(new UserSelectMenuBuilder().setCustomId(`vip:recipients:${cart.id}`)
    .setPlaceholder(`Выберите ещё ${left} участников`).setMinValues(Math.min(25, left)).setMaxValues(Math.min(25, left)));
}
async function createVipSelection(interaction, productId) {
  const product = PRODUCTS[productId];
  if (!product) throw new Error('Unknown product');
  if (product.kind === 'personal') {
    const order = createOrder({ buyerId: interaction.user.id, kind: 'personal', productId,
      amount: product.amount, recipients: [interaction.user.id] });
    await saveOrderAndReply(interaction, order);
    return;
  }
  const cart = { id: randomBytes(8).toString('hex'), buyerId: interaction.user.id, productId, recipients: [], expiresAt: Date.now() + 15 * 60 * 1000 };
  await store.mutate(s => { s.carts[cart.id] = cart; });
  await interaction.reply({ content: `**${product.name} — ${formatRub(product.amount)}**\nВыберите ${product.seats} разных участников сервера. Для 30 мест выбор проходит в два шага.`, components: [recipientMenu(cart)], flags: MessageFlags.Ephemeral, allowedMentions: noMentions });
}
async function selectRecipients(interaction, id) {
  const cart = store.data.carts[id];
  if (!cart || cart.buyerId !== interaction.user.id || Date.now() > cart.expiresAt) {
    await interaction.reply({ content: 'Выбор истёк. Откройте список VIP заново.', flags: MessageFlags.Ephemeral }); return;
  }
  const product = PRODUCTS[cart.productId];
  const expected = Math.min(25, product.seats - cart.recipients.length);
  if (interaction.values.length !== expected || interaction.values.some(id => cart.recipients.includes(id))) {
    await interaction.reply({ content: 'Выберите указанное количество новых, разных участников.', flags: MessageFlags.Ephemeral }); return;
  }
  const recipients = [...cart.recipients, ...interaction.values];
  await store.mutate(s => { s.carts[id].recipients = recipients; });
  if (recipients.length < product.seats) {
    await interaction.update({ content: `Выбрано ${recipients.length} из ${product.seats}. Выберите оставшихся участников.`, components: [recipientMenu(store.data.carts[id])], allowedMentions: noMentions });
    return;
  }
  const order = createOrder({ buyerId: interaction.user.id, kind: 'clan', productId: cart.productId,
    amount: product.amount, recipients });
  await store.mutate(s => { delete s.carts[id]; s.orders[order.id] = order; });
  const button = new ActionRowBuilder().addComponents(new ButtonBuilder().setURL(checkoutUrl(order)).setStyle(ButtonStyle.Link).setLabel('Оплатить VIP для клана'));
  await interaction.update({ content: `**${product.name} · ${formatRub(product.amount)}**\nПолучатели (${recipients.length}): ${recipients.map(id => `<@${id}>`).join(', ')}\nПроверьте список перед оплатой. Ссылка действительна до <t:${Math.floor(order.expiresAt / 1000)}:F>.`, components: [button], allowedMentions: noMentions });
}
async function status(interaction) {
  const donor = store.data.donors[interaction.user.id];
  const tier = tierFor(donor?.total || 0);
  const active = Object.values(store.data.vip).filter(v => v.userId === interaction.user.id && v.endsAt > Date.now());
  const until = active.length ? Math.max(...active.map(v => v.endsAt)) : null;
  await interaction.reply({ content: `Накоплено: **${formatRub(donor?.total || 0)}**. Спонсорская роль: **${tier?.name || 'пока нет'}**.\nVIP: ${until ? `до <t:${Math.floor(until / 1000)}:F>` : 'не активен'}.\nСпонсорская роль не даёт игровой слот. Для оплаченного VIP используйте /vip-link link steam_id и /vip-link verify code; действующая привязка сидера тоже подходит.`, flags: MessageFlags.Ephemeral, allowedMentions: noMentions });
}

async function importFeed() {
  let added = 0, seen = 0, held = 0;
  for (let pages = 0; pages < 20; pages++) {
    const offset = store.data.feedOffset;
    const batch = await feed.read(offset);
    if (!batch.events.length) break;
    for (const event of batch.events) {
      const result = await store.mutate(s => applyPayment(s, event, Date.now(), config.checkoutSecret));
      if (result.status === 'credited') added++;
      if (result.status === 'review') held++;
      seen++;
    }
    await store.mutate(s => { s.feedOffset = batch.nextOffset; });
    if (batch.events.length < 100 || batch.nextOffset === offset) break;
  }
  return { added, seen, held };
}
async function syncTierRole(guild, payment) {
  await syncSponsorMember(guild, payment.buyerId);
  await store.mutate(s => { s.payments[payment.operationId].roleDone = true; });
}
async function syncSponsorMember(guild, userId, knownMember = null) {
  const desired = tierFor(store.data.donors[userId]?.total || 0)?.roleId || null;
  const member = knownMember || await guild.members.fetch({ user: userId, force: true });
  if (desired && !member.roles.cache.has(desired)) await member.roles.add(desired, 'Накопленная поддержка WARDOGS RU');
  for (const tier of TIERS) if (tier.roleId !== desired && member.roles.cache.has(tier.roleId)) await member.roles.remove(tier.roleId, 'Обновление уровня спонсора');
}
async function sendThanks(guild, payment) {
  const target = await channel(guild, config.channels.thanks);
  const marker = createHash('sha256').update(payment.operationId).digest('hex').slice(0, 12);
  const recent = await target.messages.fetch({ limit: 50 });
  if (recent.some(m => m.author.id === client.user.id && m.embeds[0]?.footer?.text?.includes(marker))) {
    await store.mutate(s => { s.payments[payment.operationId].thanksDone = true; }); return;
  }
  await target.send(thanksCard(payment));
  await store.mutate(s => { s.payments[payment.operationId].thanksDone = true; });
}
function thanksCard(payment) {
  const marker = createHash('sha256').update(payment.operationId).digest('hex').slice(0, 12);
  const tier = tierFor(payment.totalAfter);
  const gained = payment.newRoleId && payment.newRoleId !== payment.previousRoleId;
  const embed = new EmbedBuilder().setColor(gained ? 0xf2c26b : 0x659dcb)
    .setAuthor({ name: 'WARDOGS RU · БЛАГОДАРНОСТЬ' })
    .setTitle(gained ? '🎉 Новый уровень поддержки!' : '💛 Спасибо за поддержку!')
    .setDescription(`Спасибо <@${payment.buyerId}> за вклад в развитие WARDOGS RU!`)
    .addFields(
      { name: 'Последний перевод', value: `**${formatRub(payment.amount)}**`, inline: true },
      { name: 'Всего поддержано', value: `**${formatRub(payment.totalAfter)}**`, inline: true },
      { name: 'Спонсорская роль', value: tier ? `${tier.icon} **${tier.name}**` : 'Пока не достигнут первый порог' },
    ).setFooter({ text: `WARDOGS RU · платёж ${marker}` });
  if (gained) embed.addFields({ name: '🏅 Поздравляем!', value: `Вы достигли уровня **${tier.name}**. Спасибо, что помогаете нашей стае расти!` });
  return { embeds: [embed], nonce: `wd-${marker}`, enforceNonce: true, allowedMentions: { users: [payment.buyerId], roles: [] } };
}
async function deliverPayments(guild) {
  const errors = [];
  for (const payment of Object.values(store.data.payments)) {
    if (!payment.roleDone) try { await syncTierRole(guild, payment); } catch (error) {
      errors.push(`роль (${payment.operationId}): ${error.message}`);
      console.error('Sponsor role pending:', payment.operationId, error);
    }
    if (!payment.thanksDone) try { await sendThanks(guild, payment); } catch (error) {
      errors.push(`благодарность (${payment.operationId}): ${error.message}`);
      console.error('Thanks pending:', payment.operationId, error);
    }
  }
  return errors;
}
async function deliverAdjustments(guild) {
  const errors = [];
  for (const adjustment of Object.values(store.data.adjustments || {})) {
    if (adjustment.roleDone) continue;
    try {
      await syncSponsorMember(guild, adjustment.buyerId);
      await store.mutate(s => { s.adjustments[adjustment.id].roleDone = true; });
    } catch (error) {
      errors.push(`роль после списания (${adjustment.id}): ${error.message}`);
      console.error('Sponsor correction pending:', adjustment.id, error);
    }
  }
  return errors;
}
async function syncSponsorRoles(guild) {
  for (const [id] of Object.entries(store.data.donors)) {
    try {
      const member = await guild.members.fetch({ user: id, force: true });
      await syncSponsorMember(guild, id, member);
    } catch (error) { if (error.status !== 404) console.error('Sponsor reconciliation pending:', id, error); }
  }
}
const paymentWorkflow = new PaymentWorkflow({ store, importFeed, deliverPayments, deliverAdjustments, refreshTop, syncSponsorRoles });
async function tick() {
  if (ticking) return 'Проверка уже выполняется.';
  ticking = true;
  try {
    const guild = await client.guilds.fetch(config.guildId);
    const { imported, feedError, errors } = await paymentWorkflow.check(guild);
    const payments = Object.values(store.data.payments);
    const rolesPending = payments.filter(p => !p.roleDone).length;
    const thanksPending = payments.filter(p => !p.thanksDone).length;
    const adjustments = Object.values(store.data.adjustments || {});
    const correctionRolesPending = adjustments.filter(a => !a.roleDone).length;
    const siteStatus = feedError ? `Не удалось прочитать журнал сайта: ${feedError.message}. Проверьте SITE_BASE_URL, FEED_TOKEN и /donate/feed.php. Сохранённые записи обработаны; платёж не повторяйте.` :
      `Сайт доступен. Прочитано новых уведомлений: ${imported.seen}; учтено платежей: ${imported.added}; отложено на проверку: ${imported.held}.`;
    return `${siteStatus}\n` +
      `Всего заказов в базе: ${Object.keys(store.data.orders).length}; подтверждённых платежей: ${payments.length}; ожидают выдачи роли: ${rolesPending}; публикации благодарности: ${thanksPending}; на ручной проверке: ${Object.keys(store.data.reviews).length}.\n` +
      `Ручных списаний: ${adjustments.length}; ожидают обновления роли после списания: ${correctionRolesPending}.\n` +
      `Курсор журнала: ${store.data.feedOffset}. ${errors.length ? `Ошибки: ${errors.slice(0,3).join(' | ')}` : 'Ошибок выдачи при проверке нет.'} ` +
      'Игровыми слотами управляет бот сидеров.';
  } finally { ticking = false; }
}
async function handleInteraction(interaction) {
  if (interaction.guildId !== config.guildId) return;
  try {
    if (interaction.isButton() && interaction.customId === 'support:open') {
      const modal = new ModalBuilder().setCustomId('support:amount').setTitle('Поддержать WARDOGS RU');
      modal.addComponents(new ActionRowBuilder().addComponents(new TextInputBuilder().setCustomId('amount').setLabel('Сумма в рублях, от 50 ₽').setPlaceholder('Например, 50').setStyle(TextInputStyle.Short).setRequired(true)));
      await interaction.showModal(modal); return;
    }
    if (interaction.isModalSubmit() && interaction.customId === 'support:amount') {
      const amount = parseRub(interaction.fields.getTextInputValue('amount'));
      const order = createOrder({ buyerId: interaction.user.id, kind: 'donation', amount });
      await saveOrderAndReply(interaction, order); return;
    }
    if (interaction.isStringSelectMenu() && interaction.customId.startsWith('vip:')) {
      await createVipSelection(interaction, interaction.values[0]); return;
    }
    if (interaction.isUserSelectMenu() && interaction.customId.startsWith('vip:recipients:')) {
      await selectRecipients(interaction, interaction.customId.split(':')[2]); return;
    }
    if (!interaction.isChatInputCommand()) return;
    if (interaction.commandName === 'donate') { await status(interaction); return; }
    if (interaction.commandName === 'donation-admin') {
      if (!interaction.memberPermissions?.has(PermissionFlagsBits.ManageGuild)) throw new Error('Недостаточно прав');
      await interaction.deferReply({ flags: MessageFlags.Ephemeral });
      const sub = interaction.options.getSubcommand();
      if (sub === 'setup') {
        const created = await setup(await client.guilds.fetch(config.guildId));
        await interaction.editReply(`Карточки опубликованы. VIP роль: <@&${created.vipRoleId}>. Чат спонсоров: <#${created.sponsorChannelId}>. ` +
          'Выдачей общей VIP роли за оплату и баллы управляет бот сидеров.' +
          (created.legacyVipRoleId ? ` Ранее созданная роль <@&${created.legacyVipRoleId}> сохранена: после проверки переноса её можно удалить вручную.` : ''));
      } else if (sub === 'check') {
        try { await interaction.editReply(await tick()); }
        catch (error) {
          console.error('Donation check failed:', error);
          await interaction.editReply(`Не удалось прочитать журнал платежей на сайте: ${error.message}. Проверьте SITE_BASE_URL и FEED_TOKEN, файл /donate/feed.php и журнал BotHost. Уже поступивший платёж не повторяйте.`);
        }
      } else if (sub === 'add') {
        const user = interaction.options.getUser('user', true);
        if (user.bot) throw new ManualDonationError('Выберите участника сообщества, а не бота.');
        const amount = parseRub(interaction.options.getString('amount', true));
        const guild = await client.guilds.fetch(config.guildId);
        const result = await paymentWorkflow.addManual(guild, {
          buyerId: user.id, amount, adminId: interaction.user.id,
          reason: interaction.options.getString('reason', true),
          operationId: interaction.options.getString('operation_id'), requestId: interaction.id,
        });
        const payment = result.payment;
        const tier = tierFor(store.data.donors[user.id].total);
        const pending = [!payment.roleDone && 'выдача роли', !payment.thanksDone && 'благодарность', store.data.topDirty && 'обновление рейтинга'].filter(Boolean);
        await interaction.editReply({ content:
          (result.status === 'duplicate' ? 'Эта операция уже учтена; повторного начисления нет.' : `Записана поддержка <@${user.id}>: **${formatRub(amount)}**.`) +
          `\nОбщая сумма: **${formatRub(store.data.donors[user.id].total)}**. Спонсорская роль: **${tier?.name || 'пока нет'}**.\n` +
          (pending.length ? `Запись сохранена. Ожидают повторной попытки: ${pending.join(', ')}. Проверьте права бота и выполните /donation-admin check; начисление повторять не нужно.` :
            (result.status === 'duplicate' ? 'Ранее учтённая запись обработана. Начисление и благодарность не дублируются.' :
              `Благодарность опубликована в <#${config.channels.thanks}>. Рейтинг обновлён в <#${config.channels.top}>.`)) +
          `\nID записи: \`${payment.operationId}\`.`, allowedMentions: noMentions });
      } else if (sub === 'remove') {
        const user = interaction.options.getUser('user', true);
        if (user.bot) throw new ManualDonationError('Выберите участника сообщества, а не бота.');
        const amount = parseRemovalRub(interaction.options.getString('amount', true));
        const guild = await client.guilds.fetch(config.guildId);
        const result = await paymentWorkflow.removeManual(guild, {
          buyerId: user.id, amount, adminId: interaction.user.id,
          reason: interaction.options.getString('reason', true), requestId: interaction.id,
        });
        const adjustment = result.adjustment;
        const total = store.data.donors[user.id].total;
        const tier = tierFor(total);
        const pending = [!adjustment.roleDone && 'обновление роли', store.data.topDirty && 'обновление рейтинга'].filter(Boolean);
        await interaction.editReply({ content:
          (result.status === 'duplicate' ? 'Эта команда уже выполнена; повторного списания нет.' :
            `Из поддержки <@${user.id}> списано **${formatRub(amount)}**.`) +
          `\nДо корректировки: **${formatRub(adjustment.totalBefore)}**. Сейчас: **${formatRub(total)}**.\n` +
          `Спонсорская роль: **${tier?.name || 'нет'}**.\n` +
          (pending.length ? `Списание сохранено. Ожидают повторной попытки: ${pending.join(', ')}. Проверьте права бота и выполните /donation-admin check; повторное списание не нужно.` : 'Спонсорская роль и рейтинг обновлены.') +
          `\nID корректировки: \`${adjustment.id}\`.`, allowedMentions: noMentions });
      } else {
        const reasons = {
          unknown_order: 'нет заказа в данных бота — проверьте постоянный DATA_DIR',
          order_already_paid: 'ссылка заказа уже оплачивалась',
          amount_mismatch: 'сумма отличается от заказа',
          time_mismatch: 'время перевода вне срока заказа',
          invalid_event: 'ошибка формата уведомления',
          missing_order_token: 'нет сохранённого подписанного VIP заказа на сайте',
          order_token_mismatch: 'подписанный VIP заказ не совпадает с данными бота',
          amount_or_time_mismatch: 'сумма, время или VIP заказ не совпадают',
        };
        await interaction.editReply(`Ручная проверка: ${Object.values(store.data.reviews).slice(0,20).map(r => `\n• ${r.event.operationId}: ${reasons[r.reason] || r.reason}`).join('') || 'нет'}`);
      }
    }
  } catch (error) {
    console.error('Interaction failed:', error);
    const msg = error instanceof ManualDonationError || error.message === 'Недостаточно прав' || error.message.startsWith('Укажите сумму') ? error.message : 'Ошибка обработки. Если платёж уже сделан, не повторяйте его: обратитесь к администрации.';
    if (interaction.deferred) await interaction.editReply(msg).catch(() => {});
    else if (interaction.replied) await interaction.followUp({ content: msg, flags: MessageFlags.Ephemeral }).catch(() => {});
    else await interaction.reply({ content: msg, flags: MessageFlags.Ephemeral }).catch(() => {});
  }
}
client.on(Events.InteractionCreate, handleInteraction);
client.once(Events.ClientReady, async () => {
  try {
    await new REST({ version: '10' }).setToken(config.botToken).put(Routes.applicationGuildCommands(config.clientId, config.guildId), { body: commands.map(c => c.toJSON()) });
    console.log(`Ready as ${client.user.tag}; guild commands registered`);
  } catch (error) { console.error('Startup failed:', error); client.destroy(); process.exitCode = 1; }
  if (!client.isReady()) return;
  try { console.log(await tick()); }
  catch (error) { console.error('Donation initial check failed; the bot will retry:', error); }
  setInterval(() => tick().catch(error => console.error('Donation tick failed:', error)), config.pollSeconds * 1000).unref();
});
if (require.main === module) {
  store.open().then(() => client.login(config.botToken)).catch(error => { console.error('Fatal:', error); process.exitCode = 1; client.destroy(); });
}
module.exports = { supportCard, vipCard, topCard, thanksCard, commands, handleInteraction };

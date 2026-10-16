import {
  ActionRowBuilder,
  ButtonBuilder,
  ButtonStyle,
  ChannelType,
  Client,
  EmbedBuilder,
  GatewayIntentBits,
  ModalBuilder,
  PermissionsBitField,
  SlashCommandBuilder,
  StringSelectMenuBuilder,
  TextInputBuilder,
  TextInputStyle,
  UserSelectMenuBuilder,
} from "discord.js";
import { createHash } from "node:crypto";
import { config } from "./config.js";
import { BackupManager } from "./backup.js";
import { Store } from "./store.js";
import { syncSite } from "./site-sync.js";

const F = PermissionsBitField.Flags;
const store = new Store();
const client = new Client({
  intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildMembers, GatewayIntentBits.GuildVoiceStates],
});

const COLORS = { red: 0xc5162d, dark: 0x151012, green: 0x169b62, grey: 0x626262 };
const ROLE_KEYS = ["leader", "deputy", "officer", "fighter", "recruit"];
const ROLE_LABELS = { leader: "Лидер", deputy: "Заместитель", officer: "Офицер", fighter: "Боец", recruit: "Рекрут" };
const IMAGE_URL = /^https:\/\/(?:cdn\.discordapp\.com|media\.discordapp\.net)\/attachments\/\d{17,20}\/\d{17,20}\/[A-Za-z0-9_.-]{1,200}(?:\?[^\s]{1,300})?$/;
const INVITE_URL = /^https:\/\/(?:discord\.gg|discord\.com\/invite)\/[A-Za-z0-9-]{2,64}$/;
const CLAN_LOGO_MAX_BYTES = 25 * 1024 * 1024;
const ROLE_ICON_MAX_BYTES = 256 * 1024;
const CLAN_LOGO_MIMES = new Set(["image/png", "image/jpeg", "image/webp"]);
const rateLimits = new Map();
let syncTimer = null;

const log = {
  info: (...args) => console.log(new Date().toISOString(), "INFO", ...args),
  error: (...args) => console.error(new Date().toISOString(), "ERROR", ...args),
};
const backupManager = new BackupManager(store, log);

function clean(value, max) {
  return String(value ?? "").replace(/[\u0000-\u001f\u007f]/g, " ").replace(/\s+/g, " ").trim().slice(0, max);
}

function slug(value, max = 80) {
  return clean(value, max).toLowerCase().replace(/[^\p{L}\p{N}-]+/gu, "-").replace(/^-+|-+$/g, "").slice(0, max) || "clan";
}

function detectImageMime(buffer) {
  if (buffer.length >= 8 && buffer.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) return "image/png";
  if (buffer.length >= 3 && buffer[0] === 0xff && buffer[1] === 0xd8 && buffer[2] === 0xff) return "image/jpeg";
  if (buffer.length >= 12 && buffer.subarray(0, 4).toString("ascii") === "RIFF" && buffer.subarray(8, 12).toString("ascii") === "WEBP") return "image/webp";
  return null;
}

async function inspectDiscordImage(sourceUrl, maxBytes, imageLabel) {
  const url = clean(sourceUrl, 500);
  if (!IMAGE_URL.test(url)) throw new Error(`${imageLabel} должен быть прямой ссылкой на изображение, загруженное в Discord.`);
  const response = await fetch(url, { redirect: "error", signal: AbortSignal.timeout(15_000) });
  if (!response.ok) throw new Error(`Discord не отдал изображение (${response.status}). Скопируйте ссылку заново.`);
  const declaredSize = Number.parseInt(response.headers.get("content-length") || "0", 10);
  if (Number.isFinite(declaredSize) && declaredSize > maxBytes) throw new Error(`Размер файла «${imageLabel}» превышает ${Math.floor(maxBytes / 1024)} КБ.`);
  const bytes = Buffer.from(await response.arrayBuffer());
  if (!bytes.length || bytes.length > maxBytes) throw new Error(`Размер файла «${imageLabel}» должен быть от 1 байта до ${Math.floor(maxBytes / 1024)} КБ.`);
  const mimeType = detectImageMime(bytes);
  if (!mimeType || !CLAN_LOGO_MIMES.has(mimeType)) throw new Error("Разрешены только PNG, JPG/JPEG и WebP.");
  return { url, hash: createHash("sha256").update(bytes).digest("hex"), mime: mimeType, size: bytes.length, bytes };
}

async function inspectClanLogo(sourceUrl) {
  const image = await inspectDiscordImage(sourceUrl, CLAN_LOGO_MAX_BYTES, "Логотип клана");
  return { logoUrl: image.url, logoHash: image.hash, logoMime: image.mime, logoSize: image.size };
}

async function inspectRoleIcon(sourceUrl) {
  const image = await inspectDiscordImage(sourceUrl, ROLE_ICON_MAX_BYTES, "Иконка ролей");
  return { roleIconUrl: image.url, roleIconHash: image.hash, roleIconMime: image.mime, roleIconSize: image.size, bytes: image.bytes };
}

function storedRoleIconBytes(value) {
  if (!value?.roleIconData || typeof value.roleIconData !== "string" || !/^[A-Za-z0-9+/]+={0,2}$/.test(value.roleIconData)) throw new Error("Сохранённые данные иконки ролей повреждены.");
  const bytes = Buffer.from(value.roleIconData, "base64");
  if (!bytes.length || bytes.length > ROLE_ICON_MAX_BYTES) throw new Error("Сохранённая иконка ролей превышает 256 КБ.");
  if (detectImageMime(bytes) !== value.roleIconMime || createHash("sha256").update(bytes).digest("hex") !== value.roleIconHash) throw new Error("Контрольная сумма иконки ролей не совпала.");
  return bytes;
}

function canRun(userId, key, milliseconds = 4000) {
  const id = `${userId}:${key}`;
  const now = Date.now();
  if ((rateLimits.get(id) ?? 0) > now) return false;
  rateLimits.set(id, now + milliseconds);
  return true;
}

function isAdmin(member) {
  const roleId = store.state.setup.adminRoleId || config.adminRoleId;
  return member.permissions.has(F.ManageGuild) || (roleId && member.roles.cache.has(roleId));
}

function clanRank(member, clan) {
  if (isAdmin(member)) return 99;
  if (member.roles.cache.has(clan.roleIds.leader)) return 5;
  if (member.roles.cache.has(clan.roleIds.deputy)) return 4;
  if (member.roles.cache.has(clan.roleIds.officer)) return 3;
  if (member.roles.cache.has(clan.roleIds.fighter)) return 2;
  if (member.roles.cache.has(clan.roleIds.recruit)) return 1;
  return 0;
}

function applicationEmbed(app, applicant) {
  const embed = new EmbedBuilder()
    .setColor(COLORS.red)
    .setTitle(`Заявка на регистрацию: ${app.name}`)
    .setDescription(app.description)
    .addFields(
      { name: "Тег", value: `[${app.tag}]`, inline: true },
      { name: "Лидер", value: `${app.leaderName}\n<@${app.applicantId}>`, inline: true },
      { name: "Общий состав", value: String(app.declaredMembers), inline: true },
      { name: "Discord клана", value: app.discordUrl },
      { name: "Логотип", value: `${Math.ceil(app.logoSize / 1024)} КБ · ${app.logoMime.replace("image/", "").toUpperCase()}`, inline: true },
      { name: "Иконка ролей", value: `${Math.ceil(app.roleIconSize / 1024)} КБ · ${app.roleIconMime.replace("image/", "").toUpperCase()}`, inline: true },
      { name: "Цели и задачи", value: app.goals },
    )
    .setFooter({ text: `Заявитель: ${applicant.tag} · ID ${app.applicantId}` })
    .setTimestamp(new Date(app.createdAt));
  if (app.logoUrl && IMAGE_URL.test(app.logoUrl)) embed.setThumbnail(app.logoUrl);
  return embed;
}

function clanEmbed(clan) {
  const status = clan.recruitmentStatus === "open" ? "🟢 Набор открыт" : "⚫ Набор закрыт";
  const embed = new EmbedBuilder()
    .setColor(clan.recruitmentStatus === "open" ? COLORS.red : COLORS.grey)
    .setAuthor({ name: `ПОДРАЗДЕЛЕНИЕ [${clan.tag}]` })
    .setTitle(clan.name)
    .setDescription(clan.description)
    .addFields(
      { name: "Командир", value: clan.leaderName, inline: true },
      { name: "Бойцов на сервере", value: String(clan.serverMembers ?? 0), inline: true },
      { name: "Общий состав", value: String(clan.declaredMembers), inline: true },
      { name: "Статус", value: status, inline: true },
      { name: "Discord клана", value: `[Открыть приглашение](${clan.discordUrl})`, inline: true },
      { name: "Цели и задачи", value: clan.goals },
    )
    .setFooter({ text: "Проверено администрацией WARDOGS RU" })
    .setTimestamp(new Date(clan.updatedAt));
  if (clan.logoUrl && IMAGE_URL.test(clan.logoUrl)) embed.setThumbnail(clan.logoUrl);
  return embed;
}

function recruitmentEmbed(clan) {
  const r = clan.recruitment || {};
  const embed = new EmbedBuilder()
    .setColor(clan.recruitmentStatus === "open" ? COLORS.green : COLORS.grey)
    .setTitle(`📣 [${clan.tag}] ${clan.name} — набор ${clan.recruitmentStatus === "open" ? "открыт" : "закрыт"}`)
    .setDescription(clan.description)
    .addFields(
      { name: "Кого ищем", value: r.lookingFor || "Информация уточняется" },
      { name: "Требования", value: r.requirements || "Информация уточняется" },
      { name: "Прайм-тайм", value: r.schedule || "По договорённости", inline: true },
      { name: "Контакт", value: r.contact || clan.leaderName, inline: true },
      { name: "Состав", value: `${clan.serverMembers ?? 0} на сервере · ${clan.declaredMembers} всего`, inline: true },
      { name: "Discord клана", value: `[Присоединиться](${clan.discordUrl})` },
    )
    .setFooter({ text: "Официальная карточка набора WARDOGS RU · одна публикация на клан" })
    .setTimestamp(new Date(clan.updatedAt));
  if (r.imageUrl && IMAGE_URL.test(r.imageUrl)) embed.setImage(r.imageUrl);
  if (clan.logoUrl && IMAGE_URL.test(clan.logoUrl)) embed.setThumbnail(clan.logoUrl);
  return embed;
}

function registrationPanel() {
  return {
    embeds: [new EmbedBuilder().setColor(COLORS.red).setTitle("РЕГИСТРАЦИЯ КЛАНА").setDescription("Создайте заявку на добавление клана в WARDOGS RU. После заполнения откроется закрытый тикет, а инфраструктура клана будет создана только после ручного подтверждения администратора.").addFields({ name: "Понадобится", value: "Название и тег · ник лидера · число участников · Discord-ссылка · описание · цели · логотип клана · отдельная иконка ролей PNG/JPG/WebP до 256 КБ" }).setFooter({ text: "Один пользователь может вести одну активную заявку" })],
    components: [new ActionRowBuilder().addComponents(new ButtonBuilder().setCustomId("clan:apply").setLabel("Подать заявку").setEmoji("🛡️").setStyle(ButtonStyle.Danger))],
    allowedMentions: { parse: [] },
  };
}

function clanControlRow(clanId) {
  return new ActionRowBuilder().addComponents(
    new ButtonBuilder().setCustomId(`recruit:create:${clanId}`).setLabel("Создать набор").setEmoji("📣").setStyle(ButtonStyle.Danger),
    new ButtonBuilder().setCustomId(`recruit:edit:${clanId}`).setLabel("Изменить").setStyle(ButtonStyle.Secondary),
    new ButtonBuilder().setCustomId(`recruit:toggle:${clanId}`).setLabel("Открыть / закрыть").setStyle(ButtonStyle.Secondary),
    new ButtonBuilder().setCustomId(`recruit:delete:${clanId}`).setLabel("Удалить карточку").setStyle(ButtonStyle.Secondary),
  );
}

function clanManagementRow(clanId) {
  return new ActionRowBuilder().addComponents(
    new ButtonBuilder().setCustomId(`manage:members:${clanId}`).setLabel("Участники").setEmoji("👥").setStyle(ButtonStyle.Primary),
    new ButtonBuilder().setCustomId(`manage:count:${clanId}`).setLabel("Состав").setStyle(ButtonStyle.Secondary),
    new ButtonBuilder().setCustomId(`manage:sync:${clanId}`).setLabel("Обновить").setStyle(ButtonStyle.Secondary),
    new ButtonBuilder().setCustomId(`manage:logo:${clanId}`).setLabel("Логотип").setStyle(ButtonStyle.Secondary),
    new ButtonBuilder().setCustomId(`manage:dissolve:${clanId}`).setLabel("Распустить").setStyle(ButtonStyle.Danger),
  );
}

function clanControlRows(clanId) {
  return [clanControlRow(clanId), clanManagementRow(clanId)];
}

function recruitmentApplicationRow(clan) {
  return new ActionRowBuilder().addComponents(
    new ButtonBuilder()
      .setCustomId(`join:apply:${clan.id}`)
      .setLabel(clan.recruitmentStatus === "open" ? "Подать заявку в клан" : "Набор закрыт")
      .setEmoji("📨")
      .setStyle(ButtonStyle.Success)
      .setDisabled(clan.recruitmentStatus !== "open"),
  );
}

async function audit(guild, title, description, color = COLORS.dark) {
  const channel = guild.channels.cache.get(store.state.setup.logChannelId);
  if (!channel?.isTextBased()) return;
  await channel.send({ embeds: [new EmbedBuilder().setColor(color).setTitle(title).setDescription(description).setTimestamp()], allowedMentions: { parse: [] } }).catch(log.error);
}

async function memberCount(guild, clan) {
  await guild.members.fetch();
  const roleIds = new Set(Object.values(clan.roleIds));
  return guild.members.cache.filter((member) => !member.user.bot && member.roles.cache.some((role) => roleIds.has(role.id))).size;
}

async function updateClanArtifacts(guild, clan, { sync = true } = {}) {
  clan.serverMembers = await memberCount(guild, clan);
  clan.updatedAt = new Date().toISOString();
  const list = guild.channels.cache.get(store.state.setup.listChannelId);
  if (list?.isTextBased()) {
    const old = clan.listMessageId ? await list.messages.fetch(clan.listMessageId).catch(() => null) : null;
    if (old) await old.edit({ embeds: [clanEmbed(clan)], allowedMentions: { parse: [] } });
    else clan.listMessageId = (await list.send({ embeds: [clanEmbed(clan)], allowedMentions: { parse: [] } })).id;
  }
  if (clan.recruitmentThreadId) {
    const thread = await guild.channels.fetch(clan.recruitmentThreadId).catch(() => null);
    if (thread?.isThread()) {
      const starter = await thread.fetchStarterMessage().catch(() => null);
      if (starter) await starter.edit({ embeds: [recruitmentEmbed(clan)], components: [recruitmentApplicationRow(clan)], allowedMentions: { parse: [] } }).catch(log.error);
    }
  }
  await store.update((state) => { state.clans[clan.id] = clan; });
  if (sync) await syncSite(Object.values(store.state.clans), log).catch(() => null);
}

function scheduleClanRefresh(guild, clanId) {
  clearTimeout(syncTimer);
  syncTimer = setTimeout(async () => {
    const clan = store.state.clans[clanId];
    if (clan) await updateClanArtifacts(guild, clan).catch(log.error);
  }, 2500);
}

async function ensureChannel(guild, storedId, options) {
  const existing = storedId ? await guild.channels.fetch(storedId).catch(() => null) : null;
  return existing ?? guild.channels.create(options);
}

async function positionClanRoles(guild, roleIds) {
  const anchorId = store.state.setup.roleAnchorId;
  const anchor = anchorId ? await guild.roles.fetch(anchorId).catch(() => null) : null;
  if (!anchor) throw new Error("Опорная роль для клановых ролей не найдена. Повторите /clan-setup.");
  if (anchor.position >= guild.members.me.roles.highest.position) throw new Error("Опорная роль должна находиться ниже роли бота.");
  for (const key of [...ROLE_KEYS].reverse()) {
    const role = roleIds[key] ? await guild.roles.fetch(roleIds[key]).catch(() => null) : null;
    if (!role) continue;
    const currentAnchor = guild.roles.cache.get(anchor.id) || anchor;
    await role.setPosition(Math.max(1, currentAnchor.position - 1), { reason: "WARDOGS clan hierarchy" });
  }
}

function clanCategoryOverwrites(guild, roleIds) {
  const adminRoleId = store.state.setup.adminRoleId;
  return [
    { id: guild.roles.everyone.id, deny: [F.ViewChannel] },
    { id: guild.members.me.id, allow: [F.ViewChannel, F.SendMessages, F.ReadMessageHistory, F.Connect, F.Speak, F.ManageChannels, F.MoveMembers] },
    ...(adminRoleId ? [{ id: adminRoleId, allow: [F.ViewChannel, F.SendMessages, F.ReadMessageHistory, F.Connect, F.Speak] }] : []),
    ...Object.values(roleIds).map((id) => ({ id, allow: [F.ViewChannel, F.SendMessages, F.ReadMessageHistory, F.Connect, F.Speak] })),
  ];
}

function clanHqOverwrites(guild, roleIds) {
  const adminRoleId = store.state.setup.adminRoleId;
  return [
    { id: guild.roles.everyone.id, deny: [F.ViewChannel] },
    { id: guild.members.me.id, allow: [F.ViewChannel, F.SendMessages, F.ReadMessageHistory, F.ManageChannels] },
    ...(adminRoleId ? [{ id: adminRoleId, allow: [F.ViewChannel, F.SendMessages, F.ReadMessageHistory] }] : []),
    ...["leader", "deputy", "officer"].map((key) => ({ id: roleIds[key], allow: [F.ViewChannel, F.SendMessages, F.ReadMessageHistory] })),
    ...["fighter", "recruit"].map((key) => ({ id: roleIds[key], deny: [F.ViewChannel] })),
  ];
}

function joinTicketOverwrites(guild, clan, applicantId) {
  const adminRoleId = store.state.setup.adminRoleId;
  return [
    { id: guild.roles.everyone.id, deny: [F.ViewChannel] },
    { id: applicantId, allow: [F.ViewChannel, F.SendMessages, F.ReadMessageHistory] },
    { id: guild.members.me.id, allow: [F.ViewChannel, F.SendMessages, F.ReadMessageHistory, F.ManageChannels, F.EmbedLinks] },
    ...(adminRoleId ? [{ id: adminRoleId, allow: [F.ViewChannel, F.SendMessages, F.ReadMessageHistory] }] : []),
    ...["leader", "deputy", "officer"].map((key) => ({ id: clan.roleIds[key], allow: [F.ViewChannel, F.SendMessages, F.ReadMessageHistory] })),
    ...["fighter", "recruit"].map((key) => ({ id: clan.roleIds[key], deny: [F.ViewChannel] })),
  ];
}

function headquartersMessage(clan, restored = false) {
  return {
    embeds: [new EmbedBuilder().setColor(COLORS.red).setTitle(`ШТАБ КЛАНА [${clan.tag}]`).setDescription(restored
      ? "Штаб восстановлен. Закрытая панель руководства снова управляет участниками, кланом и набором."
      : "Закрытая панель руководства: управление участниками, составом, логотипом, синхронизацией и официальной карточкой набора.").addFields({ name: "Голосовые комнаты", value: "Зайдите в «➕・Создать комнату». Для настройки используйте `/voice name`, `/voice limit`, `/voice lock` и `/voice unlock`." })],
    components: clanControlRows(clan.id),
    allowedMentions: { parse: [] },
  };
}

async function upgradeClanInfrastructure(guild, clan) {
  const category = await guild.channels.fetch(clan.channelIds.category).catch(() => null);
  if (!category || category.type !== ChannelType.GuildCategory) throw new Error(`Категория клана [${clan.tag}] не найдена.`);
  await category.permissionOverwrites.set(clanCategoryOverwrites(guild, clan.roleIds), `WARDOGS clan permissions: ${clan.name}`);
  let chat = clan.channelIds.chat ? await guild.channels.fetch(clan.channelIds.chat).catch(() => null) : null;
  if (!chat || chat.type !== ChannelType.GuildText) chat = await guild.channels.create({ name: "💬・общий-чат", type: ChannelType.GuildText, parent: category.id, topic: "Общий чат всех участников клана.", reason: `WARDOGS clan upgrade: ${clan.name}` });
  clan.channelIds.chat = chat.id;
  const hq = await guild.channels.fetch(clan.channelIds.hq).catch(() => null);
  if (!hq || hq.type !== ChannelType.GuildText) throw new Error(`Штаб клана [${clan.tag}] не найден.`);
  await hq.permissionOverwrites.set(clanHqOverwrites(guild, clan.roleIds), `WARDOGS headquarters permissions: ${clan.name}`);
  let panel = clan.controlMessageId ? await hq.messages.fetch(clan.controlMessageId).catch(() => null) : null;
  if (!panel) {
    const messages = await hq.messages.fetch({ limit: 50 }).catch(() => null);
    panel = messages?.find((message) => message.author.id === client.user.id && message.components.some((row) => row.components.some((component) => component.customId?.startsWith("recruit:create:")))) || null;
  }
  if (panel) await panel.edit(headquartersMessage(clan));
  else panel = await hq.send(headquartersMessage(clan));
  clan.controlMessageId = panel.id;
}

async function runSetup(interaction) {
  if (!interaction.member.permissions.has(F.ManageGuild) || !interaction.member.permissions.has(F.ManageChannels)) {
    return interaction.reply({ content: "Нужны права «Управлять сервером» и «Управлять каналами».", ephemeral: true });
  }
  await interaction.deferReply({ ephemeral: true });
  const guild = interaction.guild;
  const me = guild.members.me;
  const required = [F.ManageChannels, F.ManageRoles, F.MoveMembers, F.SendMessages, F.EmbedLinks, F.AttachFiles, F.ManageThreads];
  if (!me.permissions.has(required)) return interaction.editReply("Боту не хватает прав: Manage Channels, Manage Roles, Move Members, Send Messages, Embed Links, Attach Files и Manage Threads.");
  const adminRole = interaction.options.getRole("admin-role", true);
  const roleAnchor = interaction.options.getRole("role-anchor", true);
  if (roleAnchor.id === guild.roles.everyone.id || roleAnchor.managed) return interaction.editReply("Выберите обычную опорную роль, а не @everyone или роль интеграции.");
  if (roleAnchor.position >= me.roles.highest.position) return interaction.editReply("Опорная роль должна находиться ниже роли бота. Сначала поднимите роль бота выше неё.");
  const everyone = guild.roles.everyone.id;
  const botId = me.id;
  const previous = store.state.setup;

  const systemCategory = await ensureChannel(guild, previous.systemCategoryId, { name: "══ КЛАНОВАЯ СИСТЕМА ══", type: ChannelType.GuildCategory });
  const ticketCategory = await ensureChannel(guild, previous.ticketCategoryId, {
    name: "══ ЗАЯВКИ КЛАНОВ ══", type: ChannelType.GuildCategory,
    permissionOverwrites: [
      { id: everyone, deny: [F.ViewChannel] },
      { id: adminRole.id, allow: [F.ViewChannel, F.SendMessages, F.ReadMessageHistory] },
      { id: botId, allow: [F.ViewChannel, F.SendMessages, F.ReadMessageHistory, F.ManageChannels] },
    ],
  });
  const publicReadOnly = [
    { id: everyone, allow: [F.ViewChannel, F.ReadMessageHistory], deny: [F.SendMessages] },
    { id: botId, allow: [F.ViewChannel, F.SendMessages, F.ReadMessageHistory, F.EmbedLinks, F.ManageMessages] },
  ];
  const registration = await ensureChannel(guild, previous.registrationChannelId, { name: "🛡️・регистрация-кланов", type: ChannelType.GuildText, parent: systemCategory.id, permissionOverwrites: publicReadOnly });
  const list = await ensureChannel(guild, previous.listChannelId, { name: "📜・список-кланов", type: ChannelType.GuildText, parent: systemCategory.id, permissionOverwrites: publicReadOnly });
  const forum = await ensureChannel(guild, previous.recruitmentForumId, {
    name: "📢・агитация-кланов", type: ChannelType.GuildForum, parent: systemCategory.id,
    topic: "Единый форум набора. Одну официальную публикацию на клан создаёт бот.",
    availableTags: [
      { name: "Набор открыт", emoji: { name: "🟢" } }, { name: "Набор закрыт", emoji: { name: "⚫" } },
      { name: "Соревновательный", emoji: { name: "⚔️" } }, { name: "Свободная игра", emoji: { name: "🎮" } },
    ],
    permissionOverwrites: [
      { id: everyone, allow: [F.ViewChannel, F.ReadMessageHistory, F.SendMessagesInThreads], deny: [F.SendMessages, F.CreatePublicThreads] },
      { id: botId, allow: [F.ViewChannel, F.SendMessages, F.SendMessagesInThreads, F.CreatePublicThreads, F.ManageThreads, F.EmbedLinks] },
    ],
  });
  const logs = await ensureChannel(guild, previous.logChannelId, {
    name: "🔒・журнал-кланов", type: ChannelType.GuildText, parent: systemCategory.id,
    permissionOverwrites: [
      { id: everyone, deny: [F.ViewChannel] }, { id: adminRole.id, allow: [F.ViewChannel, F.ReadMessageHistory] },
      { id: botId, allow: [F.ViewChannel, F.SendMessages, F.ReadMessageHistory, F.EmbedLinks] },
    ],
  });
  const backupChannel = await ensureChannel(guild, previous.backupChannelId || config.backupChannelId, {
    name: "🔐・резервные-копии", type: ChannelType.GuildText, parent: systemCategory.id,
    topic: "Зашифрованные автоматические копии данных клановой системы. Доступ только администрации.",
    permissionOverwrites: [
      { id: everyone, deny: [F.ViewChannel] },
      { id: adminRole.id, allow: [F.ViewChannel, F.ReadMessageHistory] },
      { id: botId, allow: [F.ViewChannel, F.SendMessages, F.ReadMessageHistory, F.EmbedLinks, F.AttachFiles, F.ManageMessages] },
    ],
  });

  let panelMessage = previous.panelMessageId ? await registration.messages.fetch(previous.panelMessageId).catch(() => null) : null;
  if (panelMessage) await panelMessage.edit(registrationPanel());
  else panelMessage = await registration.send(registrationPanel());

  await store.update((state) => {
    state.setup = {
      adminRoleId: adminRole.id, systemCategoryId: systemCategory.id, ticketCategoryId: ticketCategory.id,
      registrationChannelId: registration.id, listChannelId: list.id, recruitmentForumId: forum.id,
      logChannelId: logs.id, backupChannelId: backupChannel.id, panelMessageId: panelMessage.id, roleAnchorId: roleAnchor.id,
    };
  });
  for (const clan of Object.values(store.state.clans)) {
    await positionClanRoles(guild, clan.roleIds).catch(log.error);
    await upgradeClanInfrastructure(guild, clan).catch(log.error);
  }
  await store.save();
  backupManager.start(guild);
  if (backupManager.enabled) await backupManager.create(guild, "setup").catch(log.error);
  await syncSite(Object.values(store.state.clans), log).catch(() => null);
  return interaction.editReply(`Готово. Система настроена. Администраторская роль: ${adminRole}. Клановые роли будут размещаться сразу под ролью ${roleAnchor}.`);
}

function firstApplicationModal() {
  return new ModalBuilder().setCustomId("clan:apply:first").setTitle("Заявка на клан · 1 из 2").addComponents(
    new ActionRowBuilder().addComponents(new TextInputBuilder().setCustomId("name").setLabel("1. Название клана").setStyle(TextInputStyle.Short).setMinLength(2).setMaxLength(80).setRequired(true)),
    new ActionRowBuilder().addComponents(new TextInputBuilder().setCustomId("tag").setLabel("2. Тег клана").setPlaceholder("Например: WDR").setStyle(TextInputStyle.Short).setMinLength(2).setMaxLength(16).setRequired(true)),
    new ActionRowBuilder().addComponents(new TextInputBuilder().setCustomId("leader").setLabel("3. Ник лидера").setStyle(TextInputStyle.Short).setMinLength(2).setMaxLength(80).setRequired(true)),
    new ActionRowBuilder().addComponents(new TextInputBuilder().setCustomId("members").setLabel("4. Участников всего").setPlaceholder("Число, включая людей вне WARDOGS RU").setStyle(TextInputStyle.Short).setMaxLength(6).setRequired(true)),
  );
}

function secondApplicationModal(userId) {
  return new ModalBuilder().setCustomId(`clan:apply:second:${userId}`).setTitle("Заявка на клан · 2 из 2").addComponents(
    new ActionRowBuilder().addComponents(new TextInputBuilder().setCustomId("discord").setLabel("5. Ссылка на Discord клана").setStyle(TextInputStyle.Short).setMaxLength(120).setRequired(true)),
    new ActionRowBuilder().addComponents(new TextInputBuilder().setCustomId("description").setLabel("6. Кратко о клане").setStyle(TextInputStyle.Paragraph).setMinLength(20).setMaxLength(600).setRequired(true)),
    new ActionRowBuilder().addComponents(new TextInputBuilder().setCustomId("goals").setLabel("7. Цели и задачи клана").setStyle(TextInputStyle.Paragraph).setMinLength(10).setMaxLength(600).setRequired(true)),
    new ActionRowBuilder().addComponents(new TextInputBuilder().setCustomId("logo").setLabel("8. Ссылка на логотип клана Discord").setPlaceholder("PNG, JPG/JPEG или WebP").setStyle(TextInputStyle.Short).setMaxLength(500).setRequired(true)),
    new ActionRowBuilder().addComponents(new TextInputBuilder().setCustomId("role-icon").setLabel("9. Иконка ролей Discord (до 256 КБ)").setPlaceholder("Отдельный PNG, JPG/JPEG или WebP").setStyle(TextInputStyle.Short).setMaxLength(500).setRequired(true)),
  );
}

async function submitFirst(interaction) {
  const memberCount = Number.parseInt(clean(interaction.fields.getTextInputValue("members"), 6), 10);
  const tag = clean(interaction.fields.getTextInputValue("tag"), 16).replace(/[^\p{L}\p{N}_-]/gu, "");
  if (!Number.isInteger(memberCount) || memberCount < 1 || memberCount > 100000) return interaction.reply({ content: "Количество участников должно быть целым числом от 1 до 100000.", ephemeral: true });
  if (tag.length < 2) return interaction.reply({ content: "Тег должен содержать 2–16 букв, цифр, дефисов или подчёркиваний.", ephemeral: true });
  const duplicate = Object.values(store.state.clans).find((clan) => clan.tag.toLowerCase() === tag.toLowerCase() || clan.name.toLowerCase() === clean(interaction.fields.getTextInputValue("name"), 80).toLowerCase());
  if (duplicate) return interaction.reply({ content: "Клан с таким названием или тегом уже зарегистрирован.", ephemeral: true });
  await store.update((state) => {
    state.drafts[interaction.user.id] = {
      name: clean(interaction.fields.getTextInputValue("name"), 80), tag,
      leaderName: clean(interaction.fields.getTextInputValue("leader"), 80), declaredMembers: memberCount,
      createdAt: new Date().toISOString(),
    };
  });
  return interaction.reply({ content: "Первая часть сохранена. Нажмите кнопку и заполните оставшиеся три пункта.", components: [new ActionRowBuilder().addComponents(new ButtonBuilder().setCustomId(`clan:apply:continue:${interaction.user.id}`).setLabel("Продолжить заявку").setStyle(ButtonStyle.Danger))], ephemeral: true });
}

async function submitSecond(interaction, userId) {
  if (interaction.user.id !== userId) return interaction.reply({ content: "Эта форма принадлежит другому пользователю.", ephemeral: true });
  const draft = store.state.drafts[userId];
  if (!draft || Date.now() - Date.parse(draft.createdAt) > 60 * 60 * 1000) return interaction.reply({ content: "Черновик истёк. Начните заявку заново.", ephemeral: true });
  const discordUrl = clean(interaction.fields.getTextInputValue("discord"), 120);
  if (!INVITE_URL.test(discordUrl)) return interaction.reply({ content: "Укажите полную ссылку вида https://discord.gg/код.", ephemeral: true });
  const guild = interaction.guild;
  const adminRoleId = store.state.setup.adminRoleId;
  const categoryId = store.state.setup.ticketCategoryId;
  if (!adminRoleId || !categoryId) return interaction.reply({ content: "Клановая система ещё не настроена администратором.", ephemeral: true });
  await interaction.deferReply({ ephemeral: true });
  let logo;
  let roleIcon;
  try {
    logo = await inspectClanLogo(interaction.fields.getTextInputValue("logo"));
    const checkedRoleIcon = await inspectRoleIcon(interaction.fields.getTextInputValue("role-icon"));
    roleIcon = {
      roleIconUrl: checkedRoleIcon.roleIconUrl,
      roleIconHash: checkedRoleIcon.roleIconHash,
      roleIconMime: checkedRoleIcon.roleIconMime,
      roleIconSize: checkedRoleIcon.roleIconSize,
      roleIconData: checkedRoleIcon.bytes.toString("base64"),
    };
  } catch (error) {
    return interaction.editReply(`Изображение не принято: ${clean(error.message, 300)}`);
  }
  const ticket = await guild.channels.create({
    name: `заявка-${slug(draft.tag, 20)}-${interaction.user.id.slice(-4)}`,
    type: ChannelType.GuildText, parent: categoryId,
    permissionOverwrites: [
      { id: guild.roles.everyone.id, deny: [F.ViewChannel] },
      { id: interaction.user.id, allow: [F.ViewChannel, F.SendMessages, F.ReadMessageHistory] },
      { id: adminRoleId, allow: [F.ViewChannel, F.SendMessages, F.ReadMessageHistory] },
      { id: guild.members.me.id, allow: [F.ViewChannel, F.SendMessages, F.ReadMessageHistory, F.ManageChannels, F.EmbedLinks] },
    ],
    reason: `Clan application from ${interaction.user.tag}`,
  });
  const app = {
    ...draft, applicantId: interaction.user.id, discordUrl,
    ...logo, ...roleIcon,
    description: clean(interaction.fields.getTextInputValue("description"), 600),
    goals: clean(interaction.fields.getTextInputValue("goals"), 600),
    ticketId: ticket.id, createdAt: new Date().toISOString(), status: "pending",
  };
  await store.update((state) => { state.applications[ticket.id] = app; delete state.drafts[userId]; });
  const row = new ActionRowBuilder().addComponents(
    new ButtonBuilder().setCustomId(`clan:approve:${ticket.id}`).setLabel("Одобрить").setEmoji("✅").setStyle(ButtonStyle.Success),
    new ButtonBuilder().setCustomId(`clan:reject:${ticket.id}`).setLabel("Отклонить").setEmoji("✖️").setStyle(ButtonStyle.Danger),
  );
  await ticket.send({ content: `<@&${adminRoleId}>`, embeds: [applicationEmbed(app, interaction.user)], components: [row], allowedMentions: { roles: [adminRoleId] } });
  await interaction.editReply(`Заявка создана: ${ticket}. Ожидайте решения администрации.`);
  await audit(guild, "Новая заявка на клан", `${app.name} [${app.tag}] · заявитель <@${app.applicantId}>`, COLORS.red);
}

async function createClan(interaction, ticketId) {
  if (!isAdmin(interaction.member)) return interaction.reply({ content: "Подтверждать заявки может только администрация.", ephemeral: true });
  const app = store.state.applications[ticketId];
  if (!app || app.status !== "pending") return interaction.reply({ content: "Заявка уже обработана или не найдена.", ephemeral: true });
  await interaction.deferReply({ ephemeral: true });
  const guild = interaction.guild;
  const member = await guild.members.fetch(app.applicantId).catch(() => null);
  if (!member) return interaction.editReply("Заявитель вышел с сервера. Одобрение отменено.");
  if (Object.values(store.state.clans).some((clan) => clan.tag.toLowerCase() === app.tag.toLowerCase())) return interaction.editReply("Клан с таким тегом уже существует.");
  if (!guild.features.includes("ROLE_ICONS")) return interaction.editReply("Discord не разрешает иконки ролей на этом сервере: отсутствует функция ROLE_ICONS. Клан не создан, роли не создавались.");
  let roleIconBytes;
  try {
    roleIconBytes = storedRoleIconBytes(app);
  } catch (error) {
    return interaction.editReply(`Не удалось подготовить иконку ролей: ${clean(error.message, 300)}`);
  }
  const roles = {};
  try {
    for (const key of [...ROLE_KEYS].reverse()) {
      const role = await guild.roles.create({ name: `${app.name.slice(0, 78 - ROLE_LABELS[key].length)} ${ROLE_LABELS[key]}`, color: key === "leader" ? COLORS.red : COLORS.dark, icon: roleIconBytes, hoist: key === "leader", mentionable: false, reason: `Approved clan ${app.name}` });
      roles[key] = role.id;
    }
    await positionClanRoles(guild, roles);
  } catch (error) {
    for (const roleId of Object.values(roles)) await guild.roles.delete(roleId, "Rollback after failed clan role creation").catch(() => null);
    return interaction.editReply(`Клан не создан: Discord отклонил создание ролей или их иконку. Созданные на этом шаге роли удалены. ${clean(error.message, 220)}`);
  }
  const category = await guild.channels.create({ name: `[${app.tag}] ${app.name}`.slice(0, 100), type: ChannelType.GuildCategory, permissionOverwrites: clanCategoryOverwrites(guild, roles), reason: `Approved clan ${app.name}` });
  const chat = await guild.channels.create({ name: "💬・общий-чат", type: ChannelType.GuildText, parent: category.id, topic: "Общий чат всех участников клана." });
  const hq = await guild.channels.create({ name: "⚔️・штаб-клана", type: ChannelType.GuildText, parent: category.id, topic: "Закрытое управление кланом, участниками и набором.", permissionOverwrites: clanHqOverwrites(guild, roles) });
  const voice = await guild.channels.create({ name: "🔊・Общий штаб", type: ChannelType.GuildVoice, parent: category.id });
  const creator = await guild.channels.create({ name: "➕・Создать комнату", type: ChannelType.GuildVoice, parent: category.id, userLimit: 1 });
  await member.roles.add(roles.leader, `Clan ${app.name} approved`);
  const now = new Date().toISOString();
  const clan = {
    id: category.id, name: app.name, tag: app.tag, leaderId: member.id, leaderName: app.leaderName,
    declaredMembers: app.declaredMembers, serverMembers: 1, discordUrl: app.discordUrl,
    logoUrl: app.logoUrl, logoHash: app.logoHash, logoMime: app.logoMime, logoSize: app.logoSize,
    roleIconUrl: app.roleIconUrl, roleIconHash: app.roleIconHash, roleIconMime: app.roleIconMime, roleIconSize: app.roleIconSize, roleIconData: app.roleIconData,
    description: app.description, goals: app.goals, roleIds: roles,
    channelIds: { category: category.id, chat: chat.id, hq: hq.id, voice: voice.id, creator: creator.id },
    recruitmentStatus: "closed", recruitmentThreadId: null, recruitmentUrl: null, recruitment: null,
    listMessageId: null, createdAt: now, updatedAt: now,
  };
  await store.update((state) => { state.clans[clan.id] = clan; state.applications[ticketId].status = "approved"; });
  clan.controlMessageId = (await hq.send(headquartersMessage(clan))).id;
  await updateClanArtifacts(guild, clan);
  await audit(guild, "Клан зарегистрирован", `${clan.name} [${clan.tag}] · лидер <@${clan.leaderId}> · одобрил <@${interaction.user.id}>`, COLORS.green);
  await member.send(`Ваш клан «${clan.name}» одобрен в WARDOGS RU. Закрытый штаб создан на сервере.`).catch(() => null);
  await interaction.editReply(`Клан «${clan.name}» создан. Лидеру выдана роль, карточка добавлена в список.`);
  setTimeout(() => guild.channels.delete(ticketId, "Approved clan application archived in audit log").catch(() => null), 10_000);
}

function recruitmentModal(clan, mode) {
  const r = clan.recruitment || {};
  const input = (id, label, style, max, value = "", required = true) => {
    const field = new TextInputBuilder().setCustomId(id).setLabel(label).setStyle(style).setMaxLength(max).setRequired(required);
    if (value) field.setValue(value.slice(0, max));
    return new ActionRowBuilder().addComponents(field);
  };
  return new ModalBuilder().setCustomId(`recruit:submit:${mode}:${clan.id}`).setTitle(mode === "create" ? "Карточка набора" : "Изменить набор").addComponents(
    input("looking", "Кого ищем", TextInputStyle.Paragraph, 500, r.lookingFor),
    input("requirements", "Требования", TextInputStyle.Paragraph, 500, r.requirements),
    input("schedule", "Прайм-тайм", TextInputStyle.Short, 100, r.schedule),
    input("contact", "Контакт", TextInputStyle.Short, 100, r.contact || clan.leaderName),
    input("image", "Ссылка на изображение Discord (необяз.)", TextInputStyle.Short, 300, r.imageUrl, false),
  );
}

async function submitRecruitment(interaction, mode, clanId) {
  const clan = store.state.clans[clanId];
  if (!clan || clanRank(interaction.member, clan) < 3) return interaction.reply({ content: "Карточкой набора управляют лидер, заместитель, офицер или администратор.", ephemeral: true });
  if (mode === "create" && clan.recruitmentThreadId) return interaction.reply({ content: "У клана уже есть публикация. Используйте «Изменить».", ephemeral: true });
  const imageUrl = clean(interaction.fields.getTextInputValue("image"), 300);
  if (imageUrl && !IMAGE_URL.test(imageUrl)) return interaction.reply({ content: "Изображение должно быть загружено в Discord. Скопируйте ссылку на вложение с cdn.discordapp.com или media.discordapp.net.", ephemeral: true });
  await interaction.deferReply({ ephemeral: true });
  clan.recruitment = {
    lookingFor: clean(interaction.fields.getTextInputValue("looking"), 500),
    requirements: clean(interaction.fields.getTextInputValue("requirements"), 500),
    schedule: clean(interaction.fields.getTextInputValue("schedule"), 100),
    contact: clean(interaction.fields.getTextInputValue("contact"), 100), imageUrl,
  };
  clan.recruitmentStatus = "open";
  clan.updatedAt = new Date().toISOString();
  const forum = await interaction.guild.channels.fetch(store.state.setup.recruitmentForumId).catch(() => null);
  if (!forum || forum.type !== ChannelType.GuildForum) return interaction.editReply("Форум набора не найден. Администратору нужно повторить `/clan-setup`.");
  if (!clan.recruitmentThreadId) {
    const openTag = forum.availableTags.find((tag) => tag.name === "Набор открыт")?.id;
    const thread = await forum.threads.create({ name: `[${clan.tag}] ${clan.name} — набор открыт`.slice(0, 100), autoArchiveDuration: 10080, appliedTags: openTag ? [openTag] : [], message: { embeds: [recruitmentEmbed(clan)], components: [recruitmentApplicationRow(clan)], allowedMentions: { parse: [] } }, reason: `Recruitment post for ${clan.name}` });
    clan.recruitmentThreadId = thread.id;
    clan.recruitmentUrl = `https://discord.com/channels/${config.guildId}/${forum.id}/${thread.id}`;
  } else {
    const thread = await interaction.guild.channels.fetch(clan.recruitmentThreadId).catch(() => null);
    if (!thread?.isThread()) { clan.recruitmentThreadId = null; clan.recruitmentUrl = null; return interaction.editReply("Старая публикация была удалена. Нажмите «Создать набор» ещё раз."); }
    await thread.setArchived(false).catch(() => null);
    await thread.setLocked(false).catch(() => null);
    await thread.setName(`[${clan.tag}] ${clan.name} — набор открыт`.slice(0, 100));
  }
  await updateClanArtifacts(interaction.guild, clan);
  await audit(interaction.guild, mode === "create" ? "Создана карточка набора" : "Изменена карточка набора", `${clan.name} [${clan.tag}] · автор <@${interaction.user.id}>`, COLORS.red);
  return interaction.editReply(`Карточка набора сохранена: ${clan.recruitmentUrl}`);
}

async function toggleRecruitment(interaction, clan) {
  if (!clan.recruitmentThreadId) return interaction.reply({ content: "Сначала создайте карточку набора.", ephemeral: true });
  await interaction.deferReply({ ephemeral: true });
  const thread = await interaction.guild.channels.fetch(clan.recruitmentThreadId).catch(() => null);
  if (!thread?.isThread()) return interaction.editReply("Публикация не найдена.");
  clan.recruitmentStatus = clan.recruitmentStatus === "open" ? "closed" : "open";
  const forum = thread.parent;
  const tagName = clan.recruitmentStatus === "open" ? "Набор открыт" : "Набор закрыт";
  const statusTag = forum?.availableTags?.find((tag) => tag.name === tagName)?.id;
  const otherTags = thread.appliedTags.filter((id) => !forum?.availableTags?.some((tag) => ["Набор открыт", "Набор закрыт"].includes(tag.name) && tag.id === id));
  if (clan.recruitmentStatus === "open") {
    await thread.setArchived(false).catch(() => null);
    await thread.setLocked(false).catch(() => null);
  }
  await thread.setAppliedTags(statusTag ? [statusTag, ...otherTags].slice(0, 5) : otherTags);
  await thread.setName(`[${clan.tag}] ${clan.name} — набор ${clan.recruitmentStatus === "open" ? "открыт" : "закрыт"}`.slice(0, 100));
  await updateClanArtifacts(interaction.guild, clan);
  await thread.setLocked(clan.recruitmentStatus === "closed");
  await thread.setArchived(clan.recruitmentStatus === "closed");
  return interaction.editReply(`Набор ${clan.recruitmentStatus === "open" ? "открыт" : "закрыт"}.`);
}

async function deleteRecruitment(interaction, clan) {
  if (!clan.recruitmentThreadId) return interaction.reply({ content: "У клана нет карточки набора.", ephemeral: true });
  await interaction.deferReply({ ephemeral: true });
  const threadId = clan.recruitmentThreadId;
  clan.recruitmentThreadId = null; clan.recruitmentUrl = null; clan.recruitment = null; clan.recruitmentStatus = "closed";
  await interaction.guild.channels.delete(threadId, `Recruitment post deleted by ${interaction.user.tag}`).catch(() => null);
  await updateClanArtifacts(interaction.guild, clan);
  return interaction.editReply("Карточка набора удалена. Теперь можно создать новую.");
}

function joinApplicationModal(clan) {
  return new ModalBuilder().setCustomId(`join:submit:${clan.id}`).setTitle(`Вступление в [${clan.tag}]`).addComponents(
    new ActionRowBuilder().addComponents(new TextInputBuilder().setCustomId("nickname").setLabel("Игровой ник").setStyle(TextInputStyle.Short).setMinLength(2).setMaxLength(80).setRequired(true)),
    new ActionRowBuilder().addComponents(new TextInputBuilder().setCustomId("age").setLabel("Возраст (необязательно)").setStyle(TextInputStyle.Short).setMaxLength(3).setRequired(false)),
    new ActionRowBuilder().addComponents(new TextInputBuilder().setCustomId("experience").setLabel("Опыт и предпочитаемая роль").setStyle(TextInputStyle.Paragraph).setMinLength(3).setMaxLength(500).setRequired(true)),
    new ActionRowBuilder().addComponents(new TextInputBuilder().setCustomId("motivation").setLabel("Почему хотите вступить, когда играете").setStyle(TextInputStyle.Paragraph).setMinLength(10).setMaxLength(700).setRequired(true)),
  );
}

function joinApplicationEmbed(application, clan, user) {
  return new EmbedBuilder()
    .setColor(COLORS.red)
    .setTitle(`Заявка на вступление в [${clan.tag}]`)
    .setDescription(`Кандидат: ${user} · ID ${application.applicantId}`)
    .addFields(
      { name: "Игровой ник", value: application.nickname, inline: true },
      { name: "Возраст", value: application.age || "Не указан", inline: true },
      { name: "Опыт и роль", value: application.experience },
      { name: "Мотивация и время игры", value: application.motivation },
    )
    .setFooter({ text: "Решение принимают лидер, заместитель или офицер клана" })
    .setTimestamp(new Date(application.createdAt));
}

async function submitJoinApplication(interaction, clanId) {
  const clan = store.state.clans[clanId];
  if (!clan || clan.recruitmentStatus !== "open" || !clan.recruitmentThreadId) return interaction.reply({ content: "Набор в этот клан сейчас закрыт.", ephemeral: true });
  if (store.findClanByMember(interaction.member)) return interaction.reply({ content: "Вы уже состоите в зарегистрированном клане.", ephemeral: true });
  const existing = Object.values(store.state.joinApplications).find((item) => item.clanId === clan.id && item.applicantId === interaction.user.id && item.status === "pending");
  if (existing) return interaction.reply({ content: `У вас уже есть активная заявка: <#${existing.ticketId}>.`, ephemeral: true });
  await interaction.deferReply({ ephemeral: true });
  const ticket = await interaction.guild.channels.create({
    name: `вступление-${slug(clan.tag, 20)}-${interaction.user.id.slice(-4)}`,
    type: ChannelType.GuildText,
    parent: clan.channelIds.category,
    topic: `Заявка ${interaction.user.tag} на вступление в [${clan.tag}]`,
    permissionOverwrites: joinTicketOverwrites(interaction.guild, clan, interaction.user.id),
    reason: `Join application to ${clan.name} from ${interaction.user.tag}`,
  });
  const application = {
    ticketId: ticket.id, clanId: clan.id, applicantId: interaction.user.id,
    nickname: clean(interaction.fields.getTextInputValue("nickname"), 80),
    age: clean(interaction.fields.getTextInputValue("age"), 3),
    experience: clean(interaction.fields.getTextInputValue("experience"), 500),
    motivation: clean(interaction.fields.getTextInputValue("motivation"), 700),
    status: "pending", createdAt: new Date().toISOString(),
  };
  await store.update((state) => { state.joinApplications[ticket.id] = application; });
  const leadershipRoles = [clan.roleIds.leader, clan.roleIds.deputy, clan.roleIds.officer];
  await ticket.send({
    content: leadershipRoles.map((id) => `<@&${id}>`).join(" "),
    embeds: [joinApplicationEmbed(application, clan, interaction.user)],
    components: [new ActionRowBuilder().addComponents(
      new ButtonBuilder().setCustomId(`join:approve:${ticket.id}`).setLabel("Принять рекрутом").setEmoji("✅").setStyle(ButtonStyle.Success),
      new ButtonBuilder().setCustomId(`join:reject:${ticket.id}`).setLabel("Отклонить").setEmoji("✖️").setStyle(ButtonStyle.Danger),
    )],
    allowedMentions: { roles: leadershipRoles },
  });
  await audit(interaction.guild, "Заявка на вступление", `[${clan.tag}] · кандидат <@${interaction.user.id}> · тикет <#${ticket.id}>`, COLORS.red);
  return interaction.editReply(`Заявка отправлена руководству клана: ${ticket}.`);
}

async function approveJoinApplication(interaction, ticketId) {
  const application = store.state.joinApplications[ticketId];
  const clan = application && store.state.clans[application.clanId];
  if (!application || application.status !== "pending" || !clan) return interaction.reply({ content: "Заявка уже обработана или клан не найден.", ephemeral: true });
  if (clanRank(interaction.member, clan) < 3) return interaction.reply({ content: "Решение может принять только руководство этого клана.", ephemeral: true });
  await interaction.deferReply({ ephemeral: true });
  const applicant = await interaction.guild.members.fetch(application.applicantId).catch(() => null);
  if (!applicant) return interaction.editReply("Кандидат больше не состоит на сервере.");
  const otherClan = store.findClanByMember(applicant);
  if (otherClan && otherClan.id !== clan.id) return interaction.editReply("Кандидат уже вступил в другой клан.");
  await applicant.roles.add(clan.roleIds.recruit, `Join application approved by ${interaction.user.tag}`);
  await store.update((state) => { state.joinApplications[ticketId].status = "approved"; state.joinApplications[ticketId].decidedAt = new Date().toISOString(); state.joinApplications[ticketId].decidedBy = interaction.user.id; });
  await updateClanArtifacts(interaction.guild, clan);
  await applicant.send(`Ваша заявка в клан «${clan.name}» одобрена. Вам выдана роль рекрута.`).catch(() => null);
  await audit(interaction.guild, "Кандидат принят в клан", `[${clan.tag}] · <@${applicant.id}> · решение <@${interaction.user.id}>`, COLORS.green);
  await interaction.editReply(`${applicant} принят в клан как рекрут.`);
  setTimeout(() => interaction.guild.channels.delete(ticketId, "Approved clan join application archived in audit log").catch(() => null), 15_000);
}

function memberManagementRow(clanId, userId) {
  return new ActionRowBuilder().addComponents(
    new ButtonBuilder().setCustomId(`member:add:${clanId}:${userId}`).setLabel("Принять рекрутом").setStyle(ButtonStyle.Success),
    new ButtonBuilder().setCustomId(`member:roles:${clanId}:${userId}`).setLabel("Назначить роль").setStyle(ButtonStyle.Primary),
    new ButtonBuilder().setCustomId(`member:remove:${clanId}:${userId}`).setLabel("Исключить").setStyle(ButtonStyle.Danger),
  );
}

function roleSelectRow(clanId, userId) {
  return new ActionRowBuilder().addComponents(new StringSelectMenuBuilder()
    .setCustomId(`member:role:${clanId}:${userId}`)
    .setPlaceholder("Выберите клановую роль")
    .addOptions(ROLE_KEYS.map((key) => ({ label: ROLE_LABELS[key], value: key }))));
}

function clanLogoModal(clan) {
  return new ModalBuilder().setCustomId(`clan:logo:${clan.id}`).setTitle(`Логотип [${clan.tag}]`).addComponents(
    new ActionRowBuilder().addComponents(new TextInputBuilder().setCustomId("logo").setLabel("Ссылка на логотип клана Discord").setStyle(TextInputStyle.Short).setMaxLength(500).setValue(clan.logoUrl || "").setRequired(true)),
  );
}

async function handleManagementButton(interaction, action, clanId) {
  const clan = store.state.clans[clanId];
  if (!clan || interaction.channelId !== clan.channelIds.hq || clanRank(interaction.member, clan) < 3) return interaction.reply({ content: "Эта панель доступна только руководству в штабе своего клана.", ephemeral: true });
  if (action === "members") return interaction.reply({ content: "Выберите участника сервера:", components: [new ActionRowBuilder().addComponents(new UserSelectMenuBuilder().setCustomId(`member:select:${clan.id}`).setPlaceholder("Участник").setMinValues(1).setMaxValues(1))], ephemeral: true });
  if (action === "count") return interaction.reply({ content: `На сервере: ${await memberCount(interaction.guild, clan)}. Общий заявленный состав: ${clan.declaredMembers}.`, ephemeral: true });
  if (action === "sync") { await interaction.deferReply({ ephemeral: true }); await updateClanArtifacts(interaction.guild, clan); return interaction.editReply("Карточки Discord и сайта обновлены."); }
  if (action === "logo") return interaction.showModal(clanLogoModal(clan));
  if (action === "dissolve") {
    if (interaction.user.id !== clan.leaderId && !isAdmin(interaction.member)) return interaction.reply({ content: "Распустить клан может только лидер или администрация.", ephemeral: true });
    return interaction.showModal(new ModalBuilder().setCustomId(`clan:dissolve:${clan.id}`).setTitle("Подтверждение роспуска").addComponents(new ActionRowBuilder().addComponents(new TextInputBuilder().setCustomId("tag").setLabel(`Введите тег ${clan.tag}`).setStyle(TextInputStyle.Short).setMaxLength(16).setRequired(true))));
  }
}

async function applyMemberAction(interaction, action, clanId, userId) {
  const clan = store.state.clans[clanId];
  if (!clan || interaction.channelId !== clan.channelIds.hq || clanRank(interaction.member, clan) < 3) return interaction.reply({ content: "Доступ запрещён.", ephemeral: true });
  const member = await interaction.guild.members.fetch(userId).catch(() => null);
  if (!member || member.user.bot) return interaction.reply({ content: "Участник не найден.", ephemeral: true });
  if (action === "roles") {
    if (clanRank(interaction.member, clan) < 4) return interaction.reply({ content: "Назначать роли может заместитель, лидер или администрация.", ephemeral: true });
    return interaction.update({ content: `Выберите роль для ${member}:`, components: [roleSelectRow(clan.id, member.id)], allowedMentions: { users: [] } });
  }
  if (action === "add") {
    const otherClan = store.findClanByMember(member);
    if (otherClan && otherClan.id !== clan.id) return interaction.reply({ content: "Участник уже состоит в другом клане.", ephemeral: true });
    await member.roles.add(clan.roleIds.recruit, `Added in clan HQ by ${interaction.user.tag}`);
    scheduleClanRefresh(interaction.guild, clan.id);
    return interaction.update({ content: `${member} принят рекрутом.`, components: [], allowedMentions: { users: [] } });
  }
  if (action === "remove") {
    if (member.id === clan.leaderId && !isAdmin(interaction.member)) return interaction.reply({ content: "Лидера нельзя исключить без участия администрации.", ephemeral: true });
    await member.roles.remove(Object.values(clan.roleIds), `Removed in clan HQ by ${interaction.user.tag}`);
    scheduleClanRefresh(interaction.guild, clan.id);
    return interaction.update({ content: `${member} исключён из клана.`, components: [], allowedMentions: { users: [] } });
  }
}

async function dissolveClan(interaction, clanId) {
  const clan = store.state.clans[clanId];
  if (!clan || (interaction.user.id !== clan.leaderId && !isAdmin(interaction.member))) return interaction.reply({ content: "Распустить клан может только лидер или администрация.", ephemeral: true });
  const expected = clean(interaction.fields.getTextInputValue("tag"), 16);
  if (expected.toLowerCase() !== clan.tag.toLowerCase()) return interaction.reply({ content: "Тег не совпал. Клан не был распущен.", ephemeral: true });
  await interaction.deferReply({ ephemeral: true });
  const joinTicketIds = Object.values(store.state.joinApplications).filter((item) => item.clanId === clan.id).map((item) => item.ticketId);
  await store.update((state) => {
    delete state.clans[clan.id];
    for (const ticketId of joinTicketIds) delete state.joinApplications[ticketId];
  });
  await syncSite(Object.values(store.state.clans), log).catch(() => null);
  const list = interaction.guild.channels.cache.get(store.state.setup.listChannelId);
  if (clan.listMessageId && list?.isTextBased()) await list.messages.delete(clan.listMessageId).catch(() => null);
  if (clan.recruitmentThreadId) await interaction.guild.channels.delete(clan.recruitmentThreadId, "Clan dissolved").catch(() => null);
  await audit(interaction.guild, "Клан распущен", `${clan.name} [${clan.tag}] · инициатор <@${interaction.user.id}>`, COLORS.grey);
  for (const id of joinTicketIds) await interaction.guild.channels.delete(id, "Clan dissolved").catch(() => null);
  for (const id of [clan.channelIds.chat, clan.channelIds.hq, clan.channelIds.voice, clan.channelIds.creator].filter(Boolean)) await interaction.guild.channels.delete(id, "Clan dissolved").catch(() => null);
  for (const [id, voice] of Object.entries(store.state.temporaryVoices)) if (voice.clanId === clan.id) await interaction.guild.channels.delete(id, "Clan dissolved").catch(() => null);
  await interaction.guild.channels.delete(clan.channelIds.category, "Clan dissolved").catch(() => null);
  for (const id of Object.values(clan.roleIds)) await interaction.guild.roles.delete(id, "Clan dissolved").catch(() => null);
  await interaction.editReply(`Клан «${clan.name}» распущен. Его роли, каналы и публичные карточки удалены.`);
}

async function handleClanCommand(interaction) {
  const sub = interaction.options.getSubcommand();
  if (sub === "sync") {
    if (!isAdmin(interaction.member)) return interaction.reply({ content: "Команда доступна администрации.", ephemeral: true });
    await interaction.deferReply({ ephemeral: true });
    for (const clan of Object.values(store.state.clans)) await updateClanArtifacts(interaction.guild, clan, { sync: false });
    await syncSite(Object.values(store.state.clans), log);
    return interaction.editReply("Счётчики, карточки Discord и данные сайта синхронизированы.");
  }
  let clan = store.findClanByMember(interaction.member);
  if (sub === "dissolve" && isAdmin(interaction.member)) {
    const requestedTag = interaction.options.getString("tag");
    if (requestedTag) clan = Object.values(store.state.clans).find((item) => item.tag.toLowerCase() === requestedTag.toLowerCase()) ?? null;
  }
  if (!clan) return interaction.reply({ content: "Вы не состоите в зарегистрированном клане.", ephemeral: true });
  const rank = clanRank(interaction.member, clan);
  if (sub === "count") return interaction.reply({ content: `На сервере WARDOGS RU: ${await memberCount(interaction.guild, clan)}. Общий заявленный состав: ${clan.declaredMembers}.`, ephemeral: true });
  if (sub === "dissolve") {
    if (interaction.user.id !== clan.leaderId && !isAdmin(interaction.member)) return interaction.reply({ content: "Распустить клан может только лидер или администрация.", ephemeral: true });
    return interaction.showModal(new ModalBuilder().setCustomId(`clan:dissolve:${clan.id}`).setTitle("Подтверждение роспуска").addComponents(new ActionRowBuilder().addComponents(new TextInputBuilder().setCustomId("tag").setLabel(`Введите тег ${clan.tag}`).setStyle(TextInputStyle.Short).setMaxLength(16).setRequired(true))));
  }
  if (rank < 3) return interaction.reply({ content: "Команда доступна офицерам, заместителю, лидеру и администрации.", ephemeral: true });
  const target = interaction.options.getMember("member");
  if (!target || target.user.bot) return interaction.reply({ content: "Выберите участника сервера, который не является ботом.", ephemeral: true });
  const targetClan = store.findClanByMember(target);
  if (sub === "invite") {
    if (targetClan && targetClan.id !== clan.id) return interaction.reply({ content: "Участник уже состоит в другом клане.", ephemeral: true });
    await target.roles.add(clan.roleIds.recruit, `Invited by ${interaction.user.tag}`);
    scheduleClanRefresh(interaction.guild, clan.id);
    return interaction.reply({ content: `${target} получил роль рекрута [${clan.tag}].`, ephemeral: true, allowedMentions: { users: [] } });
  }
  if (sub === "remove") {
    if (target.id === clan.leaderId && !isAdmin(interaction.member)) return interaction.reply({ content: "Лидера нельзя исключить. Сначала передайте руководство через администрацию.", ephemeral: true });
    await target.roles.remove(Object.values(clan.roleIds), `Removed by ${interaction.user.tag}`);
    scheduleClanRefresh(interaction.guild, clan.id);
    return interaction.reply({ content: `${target} исключён из клана.`, ephemeral: true, allowedMentions: { users: [] } });
  }
  if (sub === "role") {
    if (rank < 4) return interaction.reply({ content: "Назначать внутренние роли может заместитель, лидер или администрация.", ephemeral: true });
    const roleKey = interaction.options.getString("role", true);
    if (roleKey === "leader" && !isAdmin(interaction.member)) return interaction.reply({ content: "Передачу роли лидера выполняет только администрация.", ephemeral: true });
    if (targetClan && targetClan.id !== clan.id) return interaction.reply({ content: "Участник состоит в другом клане.", ephemeral: true });
    await target.roles.remove(Object.values(clan.roleIds));
    await target.roles.add(clan.roleIds[roleKey], `Clan role assigned by ${interaction.user.tag}`);
    if (roleKey === "leader") { clan.leaderId = target.id; clan.leaderName = target.displayName; }
    await updateClanArtifacts(interaction.guild, clan);
    return interaction.reply({ content: `${target} назначен: ${ROLE_LABELS[roleKey]}.`, ephemeral: true, allowedMentions: { users: [] } });
  }
}

async function handleVoiceCommand(interaction) {
  const channel = interaction.member.voice.channel;
  const data = channel && store.state.temporaryVoices[channel.id];
  if (!channel || !data) return interaction.reply({ content: "Сначала зайдите в свою временную голосовую комнату.", ephemeral: true });
  if (data.ownerId !== interaction.user.id && !isAdmin(interaction.member)) return interaction.reply({ content: "Настраивать комнату может её создатель или администратор.", ephemeral: true });
  const sub = interaction.options.getSubcommand();
  if (sub === "name") await channel.setName(clean(interaction.options.getString("value", true), 80));
  if (sub === "limit") await channel.setUserLimit(interaction.options.getInteger("value", true));
  if (sub === "lock") {
    const clan = store.state.clans[data.clanId];
    for (const id of Object.values(clan.roleIds)) await channel.permissionOverwrites.edit(id, { Connect: false });
    await channel.permissionOverwrites.edit(interaction.user.id, { ViewChannel: true, Connect: true, Speak: true, ManageChannels: true });
  }
  if (sub === "unlock") await channel.lockPermissions();
  return interaction.reply({ content: "Настройки голосовой комнаты обновлены.", ephemeral: true });
}

async function handleRecruitmentCommand(interaction) {
  const clan = store.findClanByMember(interaction.member);
  if (!clan || clanRank(interaction.member, clan) < 3) return interaction.reply({ content: "Команда доступна руководству зарегистрированного клана.", ephemeral: true });
  const sub = interaction.options.getSubcommand();
  if (["create", "edit"].includes(sub)) {
    if (sub === "create" && clan.recruitmentThreadId) return interaction.reply({ content: "У клана уже есть публикация. Используйте `/recruitment edit`.", ephemeral: true });
    if (sub === "edit" && !clan.recruitmentThreadId) return interaction.reply({ content: "Сначала создайте публикацию.", ephemeral: true });
    return interaction.showModal(recruitmentModal(clan, sub));
  }
  if (sub === "toggle") return toggleRecruitment(interaction, clan);
  if (sub === "delete") return deleteRecruitment(interaction, clan);
}

async function repairRestoredState(guild, payload) {
  await Promise.all([guild.members.fetch(), guild.roles.fetch(), guild.channels.fetch()]);
  const restoredClans = {};
  const clanIdMap = {};
  const report = { roles: 0, channels: 0, memberships: 0, recruitmentPosts: 0, joinTickets: 0, warnings: [] };

  for (const sourceClan of Object.values(store.state.clans)) {
    const clan = structuredClone(sourceClan);
    const snapshot = payload.discord?.clans?.[sourceClan.id] || { roles: {}, channels: {} };
    clan.roleIds ||= {};
    clan.channelIds ||= {};
    let restoredRoleIcon = null;
    if (clan.roleIconData) {
      if (!guild.features.includes("ROLE_ICONS")) report.warnings.push(`иконки ролей [${clan.tag}] недоступны: сервер не имеет функции ROLE_ICONS`);
      else {
        try { restoredRoleIcon = storedRoleIconBytes(clan); }
        catch (error) { report.warnings.push(`иконка ролей [${clan.tag}]: ${clean(error.message, 160)}`); }
      }
    }

    for (const key of [...ROLE_KEYS].reverse()) {
      let role = clan.roleIds[key] ? guild.roles.cache.get(clan.roleIds[key]) : null;
      const backedRole = snapshot.roles?.[key];
      if (!role) {
        role = await guild.roles.create({
          name: clean(backedRole?.name || `${clan.name} ${ROLE_LABELS[key]}`, 100),
          color: Number.isInteger(backedRole?.color) ? backedRole.color : key === "leader" ? COLORS.red : COLORS.dark,
          ...(restoredRoleIcon ? { icon: restoredRoleIcon } : {}),
          hoist: key === "leader",
          mentionable: false,
          reason: `WARDOGS backup restore: ${clan.name}`,
        });
        report.roles += 1;
      } else if (restoredRoleIcon) {
        await role.setIcon(restoredRoleIcon, `WARDOGS backup restore: ${clan.name}`).catch((error) => report.warnings.push(`роль ${role.name}: ${clean(error.message, 120)}`));
      }
      clan.roleIds[key] = role.id;
    }
    await positionClanRoles(guild, clan.roleIds).catch((error) => report.warnings.push(clean(error.message, 200)));

    const overwrites = clanCategoryOverwrites(guild, clan.roleIds);
    let category = clan.channelIds.category ? guild.channels.cache.get(clan.channelIds.category) : null;
    if (!category || category.type !== ChannelType.GuildCategory) {
      category = await guild.channels.create({
        name: `[${clan.tag}] ${clan.name}`.slice(0, 100),
        type: ChannelType.GuildCategory,
        permissionOverwrites: overwrites,
        reason: `WARDOGS backup restore: ${clan.name}`,
      });
      report.channels += 1;
    } else {
      for (const overwrite of overwrites) await category.permissionOverwrites.edit(overwrite.id, overwrite.allow ? { ViewChannel: true, SendMessages: true, ReadMessageHistory: true, Connect: true, Speak: true } : { ViewChannel: false }).catch(log.error);
    }
    clan.channelIds.category = category.id;
    clan.id = category.id;
    clanIdMap[sourceClan.id] = clan.id;

    const channelDefinitions = {
      chat: { name: "💬・общий-чат", type: ChannelType.GuildText, parent: category.id, topic: "Общий чат всех участников клана." },
      hq: { name: "⚔️・штаб-клана", type: ChannelType.GuildText, parent: category.id, topic: "Закрытое управление кланом, участниками и набором.", permissionOverwrites: clanHqOverwrites(guild, clan.roleIds) },
      voice: { name: "🔊・Общий штаб", type: ChannelType.GuildVoice, parent: category.id },
      creator: { name: "➕・Создать комнату", type: ChannelType.GuildVoice, parent: category.id, userLimit: 1 },
    };
    for (const [key, definition] of Object.entries(channelDefinitions)) {
      const oldId = clan.channelIds[key];
      const old = oldId ? guild.channels.cache.get(oldId) : null;
      const expectedType = definition.type;
      const channel = old?.type === expectedType ? old : await guild.channels.create({ ...definition, reason: `WARDOGS backup restore: ${clan.name}` });
      if (!old || old.type !== expectedType) report.channels += 1;
      clan.channelIds[key] = channel.id;
      if (key === "hq") {
        for (const overwrite of clanHqOverwrites(guild, clan.roleIds)) {
          const permissions = overwrite.allow
            ? { ViewChannel: true, SendMessages: true, ReadMessageHistory: true }
            : { ViewChannel: false };
          await channel.permissionOverwrites.edit(overwrite.id, permissions).catch(log.error);
        }
      }
      if (key === "hq" && (!old || old.type !== expectedType)) {
        clan.controlMessageId = (await channel.send(headquartersMessage(clan, true))).id;
      }
    }

    for (const key of ROLE_KEYS) {
      const memberIds = snapshot.roles?.[key]?.memberIds || [];
      for (const memberId of memberIds) {
        const member = guild.members.cache.get(memberId);
        if (member && !member.roles.cache.has(clan.roleIds[key])) {
          await member.roles.add(clan.roleIds[key], `WARDOGS backup restore: ${clan.name}`).catch(log.error);
          report.memberships += 1;
        }
      }
    }
    const leader = guild.members.cache.get(clan.leaderId);
    if (leader && !leader.roles.cache.has(clan.roleIds.leader)) {
      await leader.roles.add(clan.roleIds.leader, `WARDOGS backup restore: ${clan.name}`).catch(log.error);
      report.memberships += 1;
    }

    restoredClans[clan.id] = clan;
  }

  await store.update((state) => {
    state.clans = restoredClans;
    state.temporaryVoices = {};
  });
  for (const clan of Object.values(store.state.clans)) await upgradeClanInfrastructure(guild, clan).catch((error) => report.warnings.push(clean(error.message, 200)));
  await store.save();

  const restoredJoinApplications = {};
  for (const source of Object.values(store.state.joinApplications || {})) {
    const application = structuredClone(source);
    application.clanId = clanIdMap[application.clanId] || application.clanId;
    const clan = store.state.clans[application.clanId];
    if (application.status === "pending" && clan) {
      let ticket = application.ticketId ? await guild.channels.fetch(application.ticketId).catch(() => null) : null;
      if (!ticket || ticket.type !== ChannelType.GuildText) {
        ticket = await guild.channels.create({
          name: `вступление-${slug(clan.tag, 20)}-${application.applicantId.slice(-4)}`,
          type: ChannelType.GuildText, parent: clan.channelIds.category,
          topic: `Восстановленная заявка на вступление в [${clan.tag}]`,
          permissionOverwrites: joinTicketOverwrites(guild, clan, application.applicantId),
          reason: `WARDOGS backup restore: join application to ${clan.name}`,
        });
        application.ticketId = ticket.id;
        const leadershipRoles = [clan.roleIds.leader, clan.roleIds.deputy, clan.roleIds.officer];
        await ticket.send({
          content: leadershipRoles.map((id) => `<@&${id}>`).join(" "),
          embeds: [joinApplicationEmbed(application, clan, `<@${application.applicantId}>`)],
          components: [new ActionRowBuilder().addComponents(
            new ButtonBuilder().setCustomId(`join:approve:${ticket.id}`).setLabel("Принять рекрутом").setEmoji("✅").setStyle(ButtonStyle.Success),
            new ButtonBuilder().setCustomId(`join:reject:${ticket.id}`).setLabel("Отклонить").setEmoji("✖️").setStyle(ButtonStyle.Danger),
          )],
          allowedMentions: { roles: leadershipRoles },
        });
        report.channels += 1;
        report.joinTickets += 1;
      } else {
        await ticket.permissionOverwrites.set(joinTicketOverwrites(guild, clan, application.applicantId), `WARDOGS backup restore: ${clan.name}`).catch(log.error);
      }
    }
    restoredJoinApplications[application.ticketId] = application;
  }
  await store.update((state) => { state.joinApplications = restoredJoinApplications; });

  const forum = await guild.channels.fetch(store.state.setup.recruitmentForumId).catch(() => null);
  const list = await guild.channels.fetch(store.state.setup.listChannelId).catch(() => null);
  if (!forum || forum.type !== ChannelType.GuildForum) report.warnings.push("форум набора не найден — повторите /clan-setup");
  if (!list?.isTextBased()) report.warnings.push("канал списка кланов не найден — повторите /clan-setup");

  for (const clan of Object.values(store.state.clans)) {
    const existingThread = clan.recruitmentThreadId ? await guild.channels.fetch(clan.recruitmentThreadId).catch(() => null) : null;
    if (!existingThread?.isThread() && clan.recruitment && forum?.type === ChannelType.GuildForum) {
      const tagName = clan.recruitmentStatus === "open" ? "Набор открыт" : "Набор закрыт";
      const statusTag = forum.availableTags.find((tag) => tag.name === tagName)?.id;
      const thread = await forum.threads.create({
        name: `[${clan.tag}] ${clan.name} — набор ${clan.recruitmentStatus === "open" ? "открыт" : "закрыт"}`.slice(0, 100),
        autoArchiveDuration: 10080,
        appliedTags: statusTag ? [statusTag] : [],
        message: { embeds: [recruitmentEmbed(clan)], components: [recruitmentApplicationRow(clan)], allowedMentions: { parse: [] } },
        reason: `WARDOGS backup restore: ${clan.name}`,
      });
      clan.recruitmentThreadId = thread.id;
      clan.recruitmentUrl = `https://discord.com/channels/${config.guildId}/${forum.id}/${thread.id}`;
      if (clan.recruitmentStatus === "closed") {
        await thread.setLocked(true).catch(() => null);
        await thread.setArchived(true).catch(() => null);
      }
      report.recruitmentPosts += 1;
    }
    await updateClanArtifacts(guild, clan, { sync: false });
  }
  await syncSite(Object.values(store.state.clans), log);
  return report;
}

async function handleBackupCommand(interaction) {
  if (!isAdmin(interaction.member)) return interaction.reply({ content: "Команда доступна только администрации.", ephemeral: true });
  const sub = interaction.options.getSubcommand();
  if (sub === "status") {
    const status = backupManager.status();
    return interaction.reply({
      content: status.enabled
        ? `Автобэкап включён: каждые ${status.intervalHours} ч., хранится ${status.retention} копий. Последняя успешная копия: ${status.lastAt ? `<t:${Math.floor(Date.parse(status.lastAt) / 1000)}:F>` : "ещё не создана"}.${status.lastError ? `\nПоследняя ошибка: \`${clean(status.lastError, 500)}\`` : ""}`
        : "Резервное копирование выключено: добавьте BACKUP_SECRET длиной не менее 32 символов в .env и перезапустите бота.",
      ephemeral: true,
      allowedMentions: { parse: [] },
    });
  }
  if (!backupManager.enabled) return interaction.reply({ content: "Сначала настройте BACKUP_SECRET в .env и перезапустите бота.", ephemeral: true });
  if (sub === "create") {
    await interaction.deferReply({ ephemeral: true });
    const result = await backupManager.create(interaction.guild, `manual:${interaction.user.id}`);
    return interaction.editReply(`Готово: \`${result.filename}\`. Кланов в копии: ${result.clanCount}. Файл отправлен в закрытый канал резервных копий.`);
  }
  if (sub === "restore-latest") {
    const confirmation = clean(interaction.options.getString("confirmation", true), 32);
    if (confirmation !== "ВОССТАНОВИТЬ") return interaction.reply({ content: "Восстановление отменено. Введите слово ВОССТАНОВИТЬ точно, заглавными буквами.", ephemeral: true });
    await interaction.deferReply({ ephemeral: true });
    const source = await backupManager.loadLatest(interaction.guild);
    await backupManager.create(interaction.guild, `pre-restore:${interaction.user.id}`).catch(log.error);
    await store.replace(source.payload.state);
    const report = await repairRestoredState(interaction.guild, source.payload);
    backupManager.start(interaction.guild);
    await backupManager.create(interaction.guild, `post-restore:${interaction.user.id}`).catch(log.error);
    await audit(interaction.guild, "Клановая система восстановлена", `Источник: \`${clean(source.filename, 180)}\` · инициатор <@${interaction.user.id}>`, COLORS.green);
    const warning = report.warnings.length ? `\nВнимание: ${report.warnings.join("; ")}.` : "";
    return interaction.editReply(`Восстановление завершено. Кланов: ${Object.keys(store.state.clans).length}; создано ролей: ${report.roles}; каналов: ${report.channels}; возвращено ролей участникам: ${report.memberships}; публикаций набора: ${report.recruitmentPosts}; тикетов вступления: ${report.joinTickets}.${warning}`);
  }
}

const commands = [
  new SlashCommandBuilder().setName("clan-setup").setDescription("Настроить клановую систему").setDefaultMemberPermissions(F.ManageGuild)
    .addRoleOption((o) => o.setName("admin-role").setDescription("Роль администрации, проверяющей заявки").setRequired(true))
    .addRoleOption((o) => o.setName("role-anchor").setDescription("Клановые роли будут создаваться сразу под этой ролью").setRequired(true)),
  new SlashCommandBuilder().setName("clan").setDescription("Управление кланом")
    .addSubcommand((s) => s.setName("invite").setDescription("Принять участника рекрутом").addUserOption((o) => o.setName("member").setDescription("Участник").setRequired(true)))
    .addSubcommand((s) => s.setName("role").setDescription("Назначить внутреннюю роль").addUserOption((o) => o.setName("member").setDescription("Участник").setRequired(true)).addStringOption((o) => o.setName("role").setDescription("Роль").setRequired(true).addChoices(...ROLE_KEYS.map((key) => ({ name: ROLE_LABELS[key], value: key })))))
    .addSubcommand((s) => s.setName("remove").setDescription("Исключить участника из клана").addUserOption((o) => o.setName("member").setDescription("Участник").setRequired(true)))
    .addSubcommand((s) => s.setName("count").setDescription("Показать численность клана"))
    .addSubcommand((s) => s.setName("dissolve").setDescription("Распустить клан с подтверждением").addStringOption((o) => o.setName("tag").setDescription("Тег клана; для администрации")))
    .addSubcommand((s) => s.setName("sync").setDescription("Принудительно синхронизировать все кланы")),
  new SlashCommandBuilder().setName("recruitment").setDescription("Управление единственной карточкой набора")
    .addSubcommand((s) => s.setName("create").setDescription("Создать публикацию набора"))
    .addSubcommand((s) => s.setName("edit").setDescription("Изменить публикацию набора"))
    .addSubcommand((s) => s.setName("toggle").setDescription("Открыть или закрыть набор"))
    .addSubcommand((s) => s.setName("delete").setDescription("Удалить публикацию набора")),
  new SlashCommandBuilder().setName("voice").setDescription("Настройка временной голосовой комнаты")
    .addSubcommand((s) => s.setName("name").setDescription("Изменить название").addStringOption((o) => o.setName("value").setDescription("Новое название").setMinLength(2).setMaxLength(80).setRequired(true)))
    .addSubcommand((s) => s.setName("limit").setDescription("Лимит участников, 0 — без лимита").addIntegerOption((o) => o.setName("value").setDescription("От 0 до 99").setMinValue(0).setMaxValue(99).setRequired(true)))
    .addSubcommand((s) => s.setName("lock").setDescription("Закрыть комнату для новых участников"))
    .addSubcommand((s) => s.setName("unlock").setDescription("Открыть комнату участникам клана")),
  new SlashCommandBuilder().setName("clan-backup").setDescription("Резервные копии клановой системы").setDefaultMemberPermissions(F.ManageGuild)
    .addSubcommand((s) => s.setName("status").setDescription("Показать состояние автоматических копий"))
    .addSubcommand((s) => s.setName("create").setDescription("Создать зашифрованную копию сейчас"))
    .addSubcommand((s) => s.setName("restore-latest").setDescription("Восстановить последнюю копию из закрытого канала").addStringOption((o) => o.setName("confirmation").setDescription("Введите ВОССТАНОВИТЬ").setRequired(true))),
].map((command) => command.toJSON());

client.once("ready", async () => {
  try {
    await store.load();
    const guild = await client.guilds.fetch(config.guildId);
    await guild.commands.set(commands);
    log.info(`Ready as ${client.user.tag}; guild commands registered`);
    backupManager.start(guild);
    if (Object.keys(store.state.clans).length) {
      const fullGuild = await client.guilds.fetch(config.guildId);
      for (const clan of Object.values(store.state.clans)) await updateClanArtifacts(fullGuild, clan, { sync: false }).catch(log.error);
      await syncSite(Object.values(store.state.clans), log).catch(() => null);
    }
  } catch (error) { log.error("Startup failed", error); process.exitCode = 1; await client.destroy(); }
});

client.on("interactionCreate", async (interaction) => {
  if (!interaction.inGuild() || interaction.guildId !== config.guildId) return;
  if (!canRun(interaction.user.id, interaction.customId || interaction.commandName || "interaction", 1200)) {
    if (interaction.isRepliable()) await interaction.reply({ content: "Подождите пару секунд и повторите действие.", ephemeral: true }).catch(() => null);
    return;
  }
  try {
    if (interaction.isChatInputCommand()) {
      if (interaction.commandName === "clan-setup") return await runSetup(interaction);
      if (interaction.commandName === "clan") return await handleClanCommand(interaction);
      if (interaction.commandName === "recruitment") return await handleRecruitmentCommand(interaction);
      if (interaction.commandName === "voice") return await handleVoiceCommand(interaction);
      if (interaction.commandName === "clan-backup") return await handleBackupCommand(interaction);
    }
    if (interaction.isButton()) {
      const [area, action, id] = interaction.customId.split(":");
      if (interaction.customId === "clan:apply") {
        if (!store.state.setup.ticketCategoryId) return interaction.reply({ content: "Регистрация ещё не настроена.", ephemeral: true });
        if (store.findClanByLeader(interaction.user.id) || Object.values(store.state.applications).some((a) => a.applicantId === interaction.user.id && a.status === "pending")) return interaction.reply({ content: "У вас уже есть зарегистрированный клан или активная заявка.", ephemeral: true });
        return await interaction.showModal(firstApplicationModal());
      }
      if (area === "clan" && action === "approve") {
        if (!isAdmin(interaction.member)) return interaction.reply({ content: "Кнопка доступна администрации.", ephemeral: true });
        return interaction.reply({ content: "Подтвердите создание ролей, закрытой категории и публичной карточки.", components: [new ActionRowBuilder().addComponents(new ButtonBuilder().setCustomId(`clan:approve-confirm:${id}`).setLabel("Да, создать клан").setStyle(ButtonStyle.Success))], ephemeral: true });
      }
      if (area === "clan" && action === "approve-confirm") return await createClan(interaction, id);
      if (area === "clan" && action === "reject") {
        if (!isAdmin(interaction.member)) return interaction.reply({ content: "Кнопка доступна администрации.", ephemeral: true });
        return interaction.showModal(new ModalBuilder().setCustomId(`clan:reject-submit:${id}`).setTitle("Отклонение заявки").addComponents(new ActionRowBuilder().addComponents(new TextInputBuilder().setCustomId("reason").setLabel("Причина").setStyle(TextInputStyle.Paragraph).setMinLength(3).setMaxLength(500).setRequired(true))));
      }
      if (area === "recruit") {
        const clan = store.state.clans[id];
        if (!clan || clanRank(interaction.member, clan) < 3) return interaction.reply({ content: "У вас нет прав на управление этой карточкой.", ephemeral: true });
        if (["create", "edit"].includes(action)) return interaction.showModal(recruitmentModal(clan, action));
        if (action === "toggle") return await toggleRecruitment(interaction, clan);
        if (action === "delete") return await deleteRecruitment(interaction, clan);
      }
      if (area === "join" && action === "apply") {
        const clan = store.state.clans[id];
        if (!clan || clan.recruitmentStatus !== "open") return interaction.reply({ content: "Набор в этот клан сейчас закрыт.", ephemeral: true });
        if (store.findClanByMember(interaction.member)) return interaction.reply({ content: "Вы уже состоите в зарегистрированном клане.", ephemeral: true });
        const existing = Object.values(store.state.joinApplications).find((item) => item.clanId === clan.id && item.applicantId === interaction.user.id && item.status === "pending");
        if (existing) return interaction.reply({ content: `У вас уже есть активная заявка: <#${existing.ticketId}>.`, ephemeral: true });
        return interaction.showModal(joinApplicationModal(clan));
      }
      if (area === "join" && action === "approve") return await approveJoinApplication(interaction, id);
      if (area === "join" && action === "reject") {
        const application = store.state.joinApplications[id];
        const clan = application && store.state.clans[application.clanId];
        if (!application || application.status !== "pending" || !clan) return interaction.reply({ content: "Заявка уже обработана.", ephemeral: true });
        if (clanRank(interaction.member, clan) < 3) return interaction.reply({ content: "Решение может принять только руководство этого клана.", ephemeral: true });
        return interaction.showModal(new ModalBuilder().setCustomId(`join:reject-submit:${id}`).setTitle("Отклонение заявки").addComponents(new ActionRowBuilder().addComponents(new TextInputBuilder().setCustomId("reason").setLabel("Причина").setStyle(TextInputStyle.Paragraph).setMinLength(3).setMaxLength(500).setRequired(true))));
      }
      if (area === "manage") return await handleManagementButton(interaction, action, id);
      if (area === "member") {
        const [, memberAction, clanId, userId] = interaction.customId.split(":");
        return await applyMemberAction(interaction, memberAction, clanId, userId);
      }
      if (interaction.customId.startsWith("clan:apply:continue:")) {
        const userId = interaction.customId.split(":")[3];
        if (interaction.user.id !== userId) return interaction.reply({ content: "Это не ваша заявка.", ephemeral: true });
        return interaction.showModal(secondApplicationModal(userId));
      }
    }
    if (interaction.isUserSelectMenu() && interaction.customId.startsWith("member:select:")) {
      const clanId = interaction.customId.split(":")[2];
      const clan = store.state.clans[clanId];
      if (!clan || interaction.channelId !== clan.channelIds.hq || clanRank(interaction.member, clan) < 3) return interaction.reply({ content: "Доступ запрещён.", ephemeral: true });
      const userId = interaction.values[0];
      const member = await interaction.guild.members.fetch(userId).catch(() => null);
      if (!member || member.user.bot) return interaction.update({ content: "Выбранный участник недоступен.", components: [] });
      return interaction.update({ content: `Управление участником ${member}:`, components: [memberManagementRow(clan.id, member.id)], allowedMentions: { users: [] } });
    }
    if (interaction.isStringSelectMenu() && interaction.customId.startsWith("member:role:")) {
      const [, , clanId, userId] = interaction.customId.split(":");
      const clan = store.state.clans[clanId];
      if (!clan || interaction.channelId !== clan.channelIds.hq || clanRank(interaction.member, clan) < 4) return interaction.reply({ content: "Назначать роли может заместитель, лидер или администрация.", ephemeral: true });
      const roleKey = interaction.values[0];
      const member = await interaction.guild.members.fetch(userId).catch(() => null);
      if (!member || !ROLE_KEYS.includes(roleKey)) return interaction.update({ content: "Участник или роль не найдены.", components: [] });
      const otherClan = store.findClanByMember(member);
      if (otherClan && otherClan.id !== clan.id) return interaction.update({ content: "Участник состоит в другом клане.", components: [] });
      if (roleKey === "leader" && !isAdmin(interaction.member)) return interaction.update({ content: "Передать роль лидера может только администрация.", components: [] });
      await member.roles.remove(Object.values(clan.roleIds));
      await member.roles.add(clan.roleIds[roleKey], `Clan role assigned in HQ by ${interaction.user.tag}`);
      if (roleKey === "leader") { clan.leaderId = member.id; clan.leaderName = member.displayName; }
      await updateClanArtifacts(interaction.guild, clan);
      return interaction.update({ content: `${member} назначен: ${ROLE_LABELS[roleKey]}.`, components: [], allowedMentions: { users: [] } });
    }
    if (interaction.isModalSubmit()) {
      if (interaction.customId === "clan:apply:first") return await submitFirst(interaction);
      if (interaction.customId.startsWith("clan:apply:second:")) return await submitSecond(interaction, interaction.customId.split(":")[3]);
      if (interaction.customId.startsWith("recruit:submit:")) { const [, , mode, clanId] = interaction.customId.split(":"); return await submitRecruitment(interaction, mode, clanId); }
      if (interaction.customId.startsWith("clan:dissolve:")) return await dissolveClan(interaction, interaction.customId.split(":")[2]);
      if (interaction.customId.startsWith("join:submit:")) return await submitJoinApplication(interaction, interaction.customId.split(":")[2]);
      if (interaction.customId.startsWith("join:reject-submit:")) {
        const ticketId = interaction.customId.split(":")[3];
        const application = store.state.joinApplications[ticketId];
        const clan = application && store.state.clans[application.clanId];
        if (!application || application.status !== "pending" || !clan) return interaction.reply({ content: "Заявка уже обработана.", ephemeral: true });
        if (clanRank(interaction.member, clan) < 3) return interaction.reply({ content: "Доступ запрещён.", ephemeral: true });
        const reason = clean(interaction.fields.getTextInputValue("reason"), 500);
        await store.update((state) => { state.joinApplications[ticketId].status = "rejected"; state.joinApplications[ticketId].reason = reason; state.joinApplications[ticketId].decidedAt = new Date().toISOString(); state.joinApplications[ticketId].decidedBy = interaction.user.id; });
        const applicant = await interaction.guild.members.fetch(application.applicantId).catch(() => null);
        await applicant?.send(`Ваша заявка в клан «${clan.name}» отклонена. Причина: ${reason}`).catch(() => null);
        await audit(interaction.guild, "Заявка на вступление отклонена", `[${clan.tag}] · <@${application.applicantId}> · ${reason} · решение <@${interaction.user.id}>`, COLORS.grey);
        await interaction.reply({ content: "Заявка отклонена, кандидат уведомлён.", ephemeral: true });
        setTimeout(() => interaction.guild.channels.delete(ticketId, "Rejected clan join application archived in audit log").catch(() => null), 15_000);
        return;
      }
      if (interaction.customId.startsWith("clan:logo:")) {
        const clanId = interaction.customId.split(":")[2];
        const clan = store.state.clans[clanId];
        if (!clan || interaction.channelId !== clan.channelIds.hq || clanRank(interaction.member, clan) < 3) return interaction.reply({ content: "Доступ запрещён.", ephemeral: true });
        await interaction.deferReply({ ephemeral: true });
        let logo;
        try { logo = await inspectClanLogo(interaction.fields.getTextInputValue("logo")); }
        catch (error) { return interaction.editReply(`Логотип не принят: ${clean(error.message, 300)}`); }
        Object.assign(clan, logo);
        await updateClanArtifacts(interaction.guild, clan);
        await audit(interaction.guild, "Логотип клана обновлён", `[${clan.tag}] · автор <@${interaction.user.id}>`, COLORS.red);
        return interaction.editReply("Логотип обновлён в Discord и передан на сайт.");
      }
      if (interaction.customId.startsWith("clan:reject-submit:")) {
        if (!isAdmin(interaction.member)) return interaction.reply({ content: "Доступ запрещён.", ephemeral: true });
        const ticketId = interaction.customId.split(":")[2];
        const app = store.state.applications[ticketId];
        if (!app || app.status !== "pending") return interaction.reply({ content: "Заявка уже обработана.", ephemeral: true });
        const reason = clean(interaction.fields.getTextInputValue("reason"), 500);
        await store.update((state) => { state.applications[ticketId].status = "rejected"; state.applications[ticketId].reason = reason; });
        const applicant = await interaction.guild.members.fetch(app.applicantId).catch(() => null);
        await applicant?.send(`Заявка клана «${app.name}» отклонена. Причина: ${reason}`).catch(() => null);
        await audit(interaction.guild, "Заявка отклонена", `${app.name} [${app.tag}] · ${reason} · администратор <@${interaction.user.id}>`, COLORS.grey);
        await interaction.reply({ content: "Заявка отклонена, заявитель уведомлён.", ephemeral: true });
        setTimeout(() => interaction.guild.channels.delete(ticketId, "Rejected clan application archived in audit log").catch(() => null), 10_000);
      }
    }
  } catch (error) {
    log.error("Interaction failed", interaction.customId || interaction.commandName, error);
    const message = { content: "Операция не выполнена. Проверьте права и положение роли бота, затем повторите.", ephemeral: true };
    if (interaction.deferred || interaction.replied) await interaction.editReply(message).catch(() => null);
    else await interaction.reply(message).catch(() => null);
  }
});

client.on("voiceStateUpdate", async (oldState, newState) => {
  if (newState.guild.id !== config.guildId) return;
  try {
    if (newState.channelId) {
      const clan = Object.values(store.state.clans).find((item) => item.channelIds.creator === newState.channelId);
      if (clan && newState.member && clanRank(newState.member, clan) > 0) {
        const voice = await newState.guild.channels.create({ name: `🔊 ${clean(newState.member.displayName, 70)}`, type: ChannelType.GuildVoice, parent: clan.channelIds.category, reason: `Temporary clan voice for ${newState.member.user.tag}` });
        await voice.permissionOverwrites.edit(newState.member.id, { ViewChannel: true, Connect: true, Speak: true, ManageChannels: true });
        await store.update((state) => { state.temporaryVoices[voice.id] = { clanId: clan.id, ownerId: newState.member.id, createdAt: new Date().toISOString() }; });
        await newState.setChannel(voice, "Dynamic clan voice created");
      }
    }
    if (oldState.channelId && store.state.temporaryVoices[oldState.channelId]) {
      const channel = oldState.guild.channels.cache.get(oldState.channelId);
      if (!channel || channel.members.size === 0) {
        await oldState.guild.channels.delete(oldState.channelId, "Empty temporary clan voice").catch(() => null);
        await store.update((state) => { delete state.temporaryVoices[oldState.channelId]; });
      }
    }
  } catch (error) { log.error("Voice automation failed", error); }
});

client.on("guildMemberUpdate", (before, after) => {
  if (after.guild.id !== config.guildId || before.roles.cache.equals(after.roles.cache)) return;
  const ids = new Set([...before.roles.cache.keys(), ...after.roles.cache.keys()]);
  for (const clan of Object.values(store.state.clans)) if (Object.values(clan.roleIds).some((id) => ids.has(id))) scheduleClanRefresh(after.guild, clan.id);
});

client.on("guildMemberRemove", (member) => {
  if (member.guild.id !== config.guildId) return;
  const clan = store.findClanByMember(member);
  if (clan) scheduleClanRefresh(member.guild, clan.id);
});

process.on("unhandledRejection", (error) => log.error("Unhandled rejection", error));
process.on("SIGTERM", async () => { backupManager.stop(); await store.save(); client.destroy(); process.exit(0); });
process.on("SIGINT", async () => { backupManager.stop(); await store.save(); client.destroy(); process.exit(0); });

await client.login(config.token);

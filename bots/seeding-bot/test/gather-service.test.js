import test from 'node:test';
import assert from 'node:assert/strict';
import { Collection, ChannelType, PermissionsBitField, PermissionFlagsBits, MessageFlags } from 'discord.js';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { randomBytes } from 'node:crypto';
import { CryptoBox } from '../src/crypto-box.js';
import { SeedingDatabase, DomainError } from '../src/database.js';
import { GatherService } from '../src/services/gather-service.js';
import { gatherCard, gatherInstructions, gatherMessageUrl } from '../src/discord/gather.js';
import { commandDefinitions } from '../src/discord/commands.js';
import { installInteractionHandler } from '../src/discord/interactions.js';

const guildId = '403186008579112973';
const channelId = '1552254053109727262';
const botId = '333333333333333333';
const actorId = '444444444444444444';
const requestId = '555555555555555555';
const roleId = '1552728825568174090';
const now = Date.UTC(2026, 9, 1, 12);
const config = {
  discord: { guildId, seederRoleId: roleId, voiceChannelId: '1552253117586739210',
    rulesChannelId: '1552254112060411944', auditChannelId: '1552402077068820611',
    backupChannelId: '1552401803184701502', adminRoleIds: ['805429609457254450'] },
  seed: { tickSeconds: 30 }, vip: { oneDayCost: 120, sevenDayCost: 360, thirtyDayCost: 760 }
};
const input = (changes = {}) => ({ serverId: 'main', channelId, actorId, requestId, ...changes });

function fixture(database = null) {
  const stored = new Map();
  const audit = [];
  const db = database || {
    getState: (key, fallback) => stored.has(key) ? structuredClone(stored.get(key)) : structuredClone(fallback),
    setState: (key, value) => stored.set(key, structuredClone(value)),
    audit: (kind, actor, details) => audit.push({ kind, actor, details })
  };
  const messages = new Collection();
  const sends = [];
  const edits = [];
  const populations = { main: 12, second: 7 };
  const outages = { main: false, second: false };
  const polls = { main: 0, second: 0 };
  const permissions = new PermissionsBitField([
    PermissionFlagsBits.ViewChannel, PermissionFlagsBits.SendMessages,
    PermissionFlagsBits.EmbedLinks, PermissionFlagsBits.ReadMessageHistory,
    PermissionFlagsBits.MentionEveryone
  ]);
  let messageSerial = 0n;
  const channel = {
    id: channelId, guildId, type: ChannelType.GuildText, isSendable: () => true,
    permissionsFor: () => permissions,
    messages: { fetch: async (query) => {
      if (typeof query === 'string') {
        if (!messages.has(query)) throw Object.assign(new Error('Unknown Message'), { code: 10008 });
        return messages.get(query);
      }
      return new Collection([...messages].slice(-query.limit));
    } },
    async send(payload) {
      sends.push(payload);
      const message = { id: String(666666666666666660n + messageSerial++), author: { id: botId },
        content: payload.content || '', embeds: payload.embeds.map((embed) => embed.toJSON()),
        async edit(next) {
          edits.push(next);
          this.embeds = next.embeds.map((embed) => embed.toJSON());
          if (next.content !== undefined) this.content = next.content;
          return this;
        }
      };
      messages.set(message.id, message);
      return message;
    }
  };
  const role = { id: roleId, mentionable: false };
  const guild = { id: guildId, members: { me: {} },
    channels: { cache: new Map([[channelId, channel]]), fetch: async () => channel },
    roles: { cache: new Map([[roleId, role], [guildId, { id: guildId, mentionable: true }]]), fetch: async () => null } };
  const discord = { user: { id: botId }, guilds: { cache: new Map([[guildId, guild]]) } };
  const lease = { held: true };
  const logger = { warn() {}, error() {}, info() {}, correlationId: () => 'test-error' };
  const clients = Object.fromEntries(['main', 'second'].map((serverId) => [serverId, {
    async status() {
      polls[serverId]++;
      if (outages[serverId]) throw new Error('RCON offline');
      return { playerCount: populations[serverId], maxPlayers: 100, serverName: `WARDOGS | RU ${serverId}` };
    }
  }]));
  const dependencies = { database: db, discord, config, logger, lease, rcon: clients.main, rconSecondary: clients.second };
  const service = new GatherService(dependencies);
  return { service, dependencies, db, stored, audit, messages, sends, edits, populations, outages,
    polls, permissions, role, guild, channel, lease, discord, logger };
}

test('gather announces only the chosen role and displays real 12/20, missing eight and a connection button', async () => {
  const f = fixture();
  const result = await f.service.startGather(input(), now);
  assert.equal(result.created, true);
  assert.equal(result.session.population, 12);
  assert.match(f.sends[0].embeds[0].toJSON().description, /Сейчас 12\/20 — нужно ещё 8/);
  assert.match(f.sends[0].content, new RegExp(`<@&${roleId}>`));
  assert.deepEqual(f.sends[0].allowedMentions, { parse: [], roles: [roleId], users: [], repliedUser: false });
  assert.equal(f.sends[0].components[0].toJSON().components[0].custom_id, `seed:gather:connect:main:${requestId}`);
  assert.deepEqual(f.polls, { main: 1, second: 0 });
  assert.equal(f.audit[0].kind, 'gather_started');
  assert.match(gatherMessageUrl(result.session, guildId), /discord\.com\/channels/);
});

test('card edits follow population and reaching the threshold sends one completion without another role ping', async () => {
  const f = fixture();
  await f.service.startGather(input(), now);
  f.populations.main = 15;
  await f.service.tick(now + 30_000);
  assert.match(f.edits.at(-1).embeds[0].toJSON().description, /15\/20 — нужно ещё 5/);
  assert.deepEqual(f.edits.at(-1).allowedMentions.parse, []);
  f.populations.main = 21;
  await f.service.tick(now + 60_000);
  assert.equal(f.sends.length, 2);
  assert.match(f.sends[1].embeds[0].toJSON().description, /21\/20/);
  assert.deepEqual(f.sends[1].allowedMentions.parse, []);
  f.populations.main = 8;
  await f.service.tick(now + 90_000);
  await f.service.tick(now + 120_000);
  const restarted = new GatherService(f.dependencies);
  await restarted.tick(now + 150_000);
  assert.equal(f.sends.length, 2);
  assert.equal(f.audit.filter((entry) => entry.kind === 'gather_target_reached').length, 1);
});

test('two servers keep independent populations and goals rather than adding players from both', async () => {
  const f = fixture();
  await f.service.startGather(input(), now);
  const second = await f.service.startGather(input({ serverId: 'second', requestId: actorId }), now);
  assert.equal(second.session.population, 7);
  assert.match(f.sends[1].embeds[0].toJSON().description, /7\/20 — нужно ещё 13/);
  f.populations.second = 20;
  await f.service.tick(now + 30_000);
  const sessions = f.db.getState('seeding_gathers');
  assert.equal(sessions.main.status, 'active');
  assert.equal(sessions.second.status, 'reached');
  assert.equal(sessions.main.population, 12);
});

test('replayed, concurrent and extra start commands cannot generate repeated role notifications', async () => {
  const f = fixture();
  const results = await Promise.all([f.service.startGather(input(), now), f.service.startGather(input(), now)]);
  assert.deepEqual(results.map((result) => result.created), [true, false]);
  const existing = await f.service.startGather(input({ requestId: actorId }), now + 1000);
  assert.equal(existing.created, false);
  assert.equal(f.sends.length, 1);
  await assert.rejects(f.service.startGather(input({ target: 30 }), now), DomainError);
});

test('RCON outage displays unknown population, never zero, and cannot falsely reach the goal', async () => {
  const f = fixture();
  const started = await f.service.startGather(input(), now);
  f.outages.main = true;
  await f.service.tick(now + 30_000);
  const session = f.db.getState('seeding_gathers').main;
  assert.equal(session.population, 12);
  assert.equal(session.status, 'active');
  const card = gatherCard(session).embeds[0].toJSON();
  assert.match(card.description, /Число игроков уточняется/);
  assert.doesNotMatch(card.description, /Сейчас 0|Сейчас 12/);
  assert.equal(f.sends.length, 1);
  f.outages.main = false;
  f.populations.main = 20;
  await f.service.tick(now + 60_000);
  assert.equal(f.sends.length, 2);
  assert.equal(started.session.target, 20);
});

test('deleted gather card is recreated without pinging the role', async () => {
  const f = fixture();
  const started = await f.service.startGather(input(), now);
  f.messages.delete(started.session.cardMessageId);
  await f.service.tick(now + 30_000);
  assert.equal(f.sends.length, 2);
  assert.equal(f.sends[1].content, '');
  assert.deepEqual(f.sends[1].allowedMentions.parse, []);
});

test('initial role notification retries when the first card could not be sent', async () => {
  const f = fixture();
  const send = f.channel.send;
  let fail = true;
  f.channel.send = async (payload) => {
    if (fail) throw new Error('Discord offline');
    return send(payload);
  };
  const pending = await f.service.startGather(input(), now);
  assert.equal(pending.session.deliveryPending, true);
  assert.equal(f.sends.length, 0);
  fail = false;
  await new GatherService(f.dependencies).tick(now + 30_000);
  assert.equal(f.sends.length, 1);
  assert.deepEqual(f.sends[0].allowedMentions.roles, [roleId]);
  assert.equal(f.db.getState('seeding_gathers').main.notificationConfirmed, true);
});

test('lost response after posting the first card is recovered from history without a duplicate role ping', async () => {
  const f = fixture();
  const send = f.channel.send;
  f.channel.send = async (payload) => {
    await send(payload);
    throw new Error('Lost response');
  };
  await f.service.startGather(input(), now);
  assert.equal(f.db.getState('seeding_gathers').main.cardMessageId, null);
  f.channel.send = send;
  await new GatherService(f.dependencies).tick(now + 30_000);
  assert.equal(f.sends.length, 1);
  assert.equal(f.db.getState('seeding_gathers').main.notificationConfirmed, true);
  assert.equal(f.db.getState('seeding_gathers').main.deliveryPending, false);
});

test('completion delivery retries after a Discord failure and finds a message whose ID was lost on restart', async () => {
  const f = fixture();
  await f.service.startGather(input(), now);
  const send = f.channel.send;
  let fail = true;
  f.channel.send = async (payload) => {
    if (fail) throw new Error('Discord offline');
    return send(payload);
  };
  f.populations.main = 20;
  await f.service.tick(now + 30_000);
  assert.equal(f.db.getState('seeding_gathers').main.status, 'reached');
  assert.equal(f.db.getState('seeding_gathers').main.deliveryPending, true);
  fail = false;
  await new GatherService(f.dependencies).tick(now + 60_000);
  assert.equal(f.sends.length, 2);
  const sessions = f.db.getState('seeding_gathers');
  sessions.main.completionMessageId = null;
  sessions.main.deliveryPending = true;
  f.db.setState('seeding_gathers', sessions);
  await new GatherService(f.dependencies).tick(now + 90_000);
  assert.equal(f.sends.length, 2);
  assert.equal(f.db.getState('seeding_gathers').main.deliveryPending, false);
});

test('card update failure on one server does not stop gathering on the other server', async () => {
  const f = fixture();
  const main = await f.service.startGather(input(), now);
  await f.service.startGather(input({ serverId: 'second', requestId: actorId }), now);
  f.messages.get(main.session.cardMessageId).edit = async () => { throw new Error('Discord error'); };
  f.populations.second = 20;
  await f.service.tick(now + 30_000);
  assert.equal(f.db.getState('seeding_gathers').main.deliveryPending, true);
  assert.equal(f.db.getState('seeding_gathers').second.status, 'reached');
  assert.equal(f.sends.length, 3);
});

test('cancellation, expiration and cooldown prevent lingering gatherings or repeated pings', async () => {
  const f = fixture();
  await f.service.startGather(input({ minutes: 15 }), now);
  await f.service.cancel('main', actorId, now + 1000);
  const polls = f.polls.main;
  f.populations.main = 20;
  await f.service.tick(now + 30_000);
  assert.equal(f.polls.main, polls);
  assert.equal(f.sends.length, 1);
  assert.equal(f.db.getState('seeding_gathers').main.status, 'cancelled');
  await assert.rejects(f.service.startGather(input({ requestId: actorId }), now + 60_000), /10 минут/);
  f.populations.main = 12;
  const next = await f.service.startGather(input({ requestId: actorId, minutes: 15 }), now + 600_000);
  await f.service.tick(next.session.expiresAt);
  assert.equal(f.db.getState('seeding_gathers').main.status, 'expired');
  assert.equal(f.sends.length, 2);
  assert.match(f.edits.at(-1).embeds[0].toJSON().description, /Время сбора истекло/);
});

test('invalid goals, protected channels, missing permissions and @everyone cannot post a gathering', async () => {
  for (const changes of [{ target: 1 }, { target: 101 }, { minutes: 1 }, { minutes: 361 },
    { serverId: 'other' }, { instructions: 'x'.repeat(1001) }, { roleId: guildId },
    { channelId: config.discord.rulesChannelId }]) {
    const f = fixture();
    await assert.rejects(f.service.startGather(input(changes), now), DomainError);
    assert.equal(f.sends.length, 0);
    assert.deepEqual(f.db.getState('seeding_gathers', {}), {});
  }
  const f = fixture();
  f.permissions.remove(PermissionFlagsBits.MentionEveryone);
  await assert.rejects(f.service.startGather(input(), now), /упоминание выбранной роли/);
  f.role.mentionable = true;
  assert.equal((await f.service.startGather(input(), now)).created, true);
  const unavailable = fixture();
  unavailable.permissions.remove(PermissionFlagsBits.ReadMessageHistory);
  await assert.rejects(unavailable.service.startGather(input(), now), /чтения истории/);
});

test('already-reached population rejects a needless role ping, and no active gathering means no RCON polls', async () => {
  const f = fixture();
  await f.service.tick(now);
  assert.deepEqual(f.polls, { main: 0, second: 0 });
  f.populations.main = 25;
  await assert.rejects(f.service.startGather(input(), now), /уже 25\/20/);
  assert.equal(f.sends.length, 0);
  f.lease.held = false;
  await f.service.tick(now);
  await assert.rejects(f.service.startGather(input(), now), /защитной блокировкой/);
  const small = fixture();
  small.dependencies.rcon.status = async () => ({ playerCount: 12, maxPlayers: 30, serverName: 'Small server' });
  await assert.rejects(small.service.startGather(input({ target: 40 }), now), /количество мест/);
  assert.equal(small.sends.length, 0);
});

test('connection instructions use the actual game name or custom instructions and do not expose RCON addresses', async () => {
  const f = fixture();
  const result = await f.service.startGather(input(), now);
  const payload = f.service.instructions('main', requestId);
  assert.match(payload.embeds[0].toJSON().description, /WARDOGS \| RU main/);
  assert.doesNotMatch(JSON.stringify(payload), /7789|7799|Authorization|password/);
  const custom = gatherInstructions({ ...result.session, instructions: 'Подключитесь через ID сервера: 12345' }, config);
  assert.equal(custom.embeds[0].toJSON().description, 'Подключитесь через ID сервера: 12345');
  assert.throws(() => f.service.instructions('second', requestId), DomainError);
});

test('the encrypted database preserves gatherings across restart without adding or altering seeding users', async t => {
  const directory = await mkdtemp(join(tmpdir(), 'wardogs-gather-'));
  const crypto = new CryptoBox(randomBytes(32));
  const options = { path: join(directory, 'seeding.sqlite'), crypto, guildId, logger: { info() {}, warn() {}, error() {} } };
  const database = new SeedingDatabase(options);
  t.after(async () => { database.close(); await rm(directory, { recursive: true, force: true }); });
  const f = fixture(database);
  await f.service.startGather(input(), now);
  assert.equal(database.listUsers().length, 0);
  assert.equal(database.verifyIntegrity().users, 0);
  const copy = new SeedingDatabase(options);
  try {
    const session = copy.getState('seeding_gathers').main;
    assert.equal(session.population, 12);
    assert.equal(session.cardMessageId, f.sends.length ? [...f.messages.keys()][0] : null);
    await new GatherService({ ...f.dependencies, database: copy }).tick(now + 30_000);
    assert.equal(f.sends.length, 1);
    assert.equal(copy.verifyIntegrity().users, 0);
  } finally { copy.close(); }
});

test('gather commands serialize and unprivileged members can use only the connection button', async () => {
  const admin = commandDefinitions(config).find((command) => command.name === 'seed-admin');
  for (const sub of ['gather', 'gather-status', 'gather-stop']) {
    const command = admin.options.find((option) => option.name === sub);
    assert.equal(command.options[0].required, true);
    assert.deepEqual(command.options[0].choices.map((choice) => choice.value), ['main', 'second']);
  }
  const f = fixture();
  await f.service.startGather(input(), now);
  let handler;
  f.discord.on = (_event, callback) => { handler = callback; };
  installInteractionHandler({ ...f.dependencies, gatherService: f.service });
  let payload;
  const button = {
    guildId, customId: `seed:gather:connect:main:${requestId}`, user: { id: actorId },
    isButton: () => true, isChatInputCommand: () => false,
    async deferReply(options) { assert.equal(options.flags, MessageFlags.Ephemeral); this.deferred = true; },
    async editReply(value) { payload = value; }
  };
  await handler(button);
  assert.match(payload.embeds[0].toJSON().description, /WARDOGS/);
  let rejected;
  await handler({ guildId, user: { id: botId }, commandName: 'seed-admin',
    isButton: () => false, isChatInputCommand: () => true,
    memberPermissions: new PermissionsBitField(0n),
    async reply(value) { rejected = value; }
  });
  assert.equal(rejected.content, 'Недостаточно прав.');
  assert.equal(f.sends.length, 1);
});

test('authorized gather command forwards the selected server, channel, target, role and instructions', async () => {
  const f = fixture();
  const session = (await f.service.startGather(input(), now)).session;
  let handler;
  f.discord.on = (_event, callback) => { handler = callback; };
  let forwarded;
  installInteractionHandler({ ...f.dependencies, gatherService: {
    async startGather(value) { forwarded = value; return { created: true, session }; }
  } });
  let reply;
  const strings = { server: 'second', instructions: 'Найдите игровой сервер по ID.' };
  const integers = { target: 25, minutes: 60 };
  await handler({ guildId, channelId, id: requestId, user: { id: actorId }, commandName: 'seed-admin',
    memberPermissions: new PermissionsBitField(PermissionFlagsBits.ManageGuild),
    member: { roles: { cache: new Map([['805429609457254450', {}]]) } },
    isButton: () => false, isChatInputCommand: () => true,
    options: { getSubcommand: () => 'gather', getString: (key) => strings[key],
      getInteger: (key) => integers[key], getChannel: () => ({ id: channelId }), getRole: () => ({ id: roleId }) },
    async deferReply(payload) { assert.equal(payload.flags, MessageFlags.Ephemeral); this.deferred = true; },
    async editReply(payload) { reply = payload; }
  });
  assert.deepEqual(forwarded, { serverId: 'second', actorId, requestId, channelId, target: 25,
    roleId, minutes: 60, instructions: strings.instructions });
  assert.match(reply.content, /Сбор объявлен/);
  assert.deepEqual(reply.allowedMentions.parse, []);
});

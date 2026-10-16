import test from 'node:test';
import assert from 'node:assert/strict';
import { MessageFlags, PermissionFlagsBits, PermissionsBitField } from 'discord.js';
import { commandDefinitions } from '../src/discord/commands.js';
import { installInteractionHandler } from '../src/discord/interactions.js';

const guildId = '403186008579112973';
const gatherRoleId = '859738052666327040';
const adminRoleIds = ['805429609457254450', '815605974412558376'];
const actorId = '444444444444444444';
const channelId = '1552254053109727262';
const requestId = '555555555555555555';
const config = {
  discord: { guildId, adminRoleIds, gatherRoleIds: [gatherRoleId], seederRoleId: '1552728825568174090' },
  vip: { oneDayCost: 120, sevenDayCost: 360, thirtyDayCost: 760 }
};

function fixture() {
  let handler;
  const calls = [];
  const audits = [];
  const session = {
    id: requestId, serverId: 'second', label: 'Сервер 2', target: 20,
    channelId, cardMessageId: '666666666666666666', status: 'active',
    population: 12, sampleOk: true, expiresAt: Date.UTC(2026, 9, 1, 14)
  };
  const dependencies = {
    discord: { on(_event, callback) { handler = callback; } },
    config, lease: { held: true },
    database: { audit: (...args) => audits.push(args) },
    logger: { correlationId: () => 'access-test', info() {}, warn() {}, error() {} },
    gatherService: {
      async startGather(input) { calls.push(['start', input]); return { created: true, session }; },
      async status(serverId) { calls.push(['status', serverId]); return session; },
      async cancel(serverId, actor) { calls.push(['stop', serverId, actor]); return { ...session, status: 'cancelled' }; }
    }
  };
  installInteractionHandler(dependencies);
  return { handler, calls, audits, dependencies };
}

function interaction(changes = {}) {
  const replies = [];
  return {
    guildId, channelId, id: requestId, user: { id: actorId }, commandName: 'seed-gather',
    memberPermissions: new PermissionsBitField(0n), member: { roles: [gatherRoleId] },
    isButton: () => false, isChatInputCommand: () => true,
    options: {
      getSubcommand: () => 'start', getString: (key) => key === 'server' ? 'second' : null,
      getInteger: () => null, getChannel: () => null, getRole: () => null
    },
    replies,
    async reply(payload) { replies.push(payload); this.replied = true; },
    async deferReply(payload) { assert.equal(payload.flags, MessageFlags.Ephemeral); this.deferred = true; },
    async editReply(payload) { replies.push(payload); },
    ...changes
  };
}

test('coordinators can start, check and stop either server without Manage Server or a Steam link', async () => {
  for (const serverId of ['main', 'second']) {
    for (const action of ['start', 'status', 'stop']) {
      const f = fixture();
      const request = interaction();
      request.options.getSubcommand = () => action;
      request.options.getString = (key) => key === 'server' ? serverId : null;
      await f.handler(request);
      assert.equal(f.calls.length, 1);
      assert.equal(f.calls[0][0], action);
      assert.equal(request.deferred, true);
      assert.equal(f.audits.length, 0);
      assert.equal(request.replies.length, 1);
      if (action === 'start') {
        assert.deepEqual(f.calls[0][1], { serverId, actorId, requestId, channelId,
          target: 20, roleId: config.discord.seederRoleId, minutes: 120, instructions: '' });
        assert.match(request.replies[0].content, /Сбор объявлен/);
      } else {
        assert.equal(f.calls[0][1], serverId);
        if (action === 'stop') assert.equal(f.calls[0][2], actorId);
        assert.equal(request.replies[0].components.length, 0);
      }
    }
  }
});

test('coordinator access accepts both Discord member role representations', async () => {
  for (const roles of [[gatherRoleId], { cache: new Map([[gatherRoleId, {}]]) }]) {
    const f = fixture();
    await f.handler(interaction({ member: { roles } }));
    assert.equal(f.calls.length, 1);
  }
});

test('the coordinator role cannot execute any seed-admin subcommand, even with Manage Server or Administrator', async () => {
  const commands = commandDefinitions(config).find((command) => command.name === 'seed-admin').options;
  for (const permission of [0n, PermissionFlagsBits.ManageGuild, PermissionFlagsBits.Administrator]) {
    for (const command of commands) {
      const f = fixture();
      const request = interaction({ commandName: 'seed-admin', memberPermissions: new PermissionsBitField(permission) });
      request.options.getSubcommand = () => command.name;
      await f.handler(request);
      assert.equal(request.deferred, undefined, command.name);
      assert.deepEqual(request.replies, [{ content: 'Недостаточно прав.', flags: MessageFlags.Ephemeral }], command.name);
      assert.equal(f.calls.length, 0, command.name);
      assert.equal(f.audits[0][0], 'admin_access_denied', command.name);
    }
  }
});

test('ordinary members, Manage Server alone and admin roles without Manage Server cannot operate a gathering', async () => {
  const identities = [
    { member: undefined },
    { member: { roles: [] } },
    { member: { roles: ['1552728825568174090'] } },
    { member: { roles: [] }, memberPermissions: new PermissionsBitField(PermissionFlagsBits.ManageGuild) },
    { member: { roles: [] }, memberPermissions: new PermissionsBitField(PermissionFlagsBits.Administrator) },
    ...adminRoleIds.map((id) => ({ member: { roles: [id] } }))
  ];
  for (const identity of identities) {
    for (const action of ['start', 'status', 'stop']) {
      const f = fixture();
      const request = interaction(identity);
      request.options.getSubcommand = () => action;
      await f.handler(request);
      assert.deepEqual(request.replies, [{ content: 'Недостаточно прав.', flags: MessageFlags.Ephemeral }]);
      assert.equal(f.calls.length, 0);
      assert.equal(f.audits[0][0], 'gather_access_denied');
      assert.equal(f.audits[0][2].command, `seed-gather:${action}`);
    }
  }
});

test('both existing admin roles retain new and legacy gathering commands', async () => {
  for (const adminRoleId of adminRoleIds) {
    for (const [commandName, names] of [['seed-gather', ['start', 'status', 'stop']],
      ['seed-admin', ['gather', 'gather-status', 'gather-stop']]]) {
      for (const [index, name] of names.entries()) {
        const f = fixture();
        const request = interaction({ commandName, member: { roles: [adminRoleId] },
          memberPermissions: new PermissionsBitField(PermissionFlagsBits.ManageGuild) });
        request.options.getSubcommand = () => name;
        await f.handler(request);
        assert.equal(f.calls[0]?.[0], ['start', 'status', 'stop'][index]);
        assert.equal(f.audits.length, 0);
      }
    }
  }
});

test('forged non-gather actions cannot reach administrative operations through seed-gather', async () => {
  for (const action of ['points', 'points-all', 'backup', 'health', 'constructor', 'toString']) {
    for (const roles of [[gatherRoleId], [adminRoleIds[0]]]) {
      const f = fixture();
      const request = interaction({ member: { roles }, memberPermissions: new PermissionsBitField(PermissionFlagsBits.ManageGuild) });
      request.options.getSubcommand = () => action;
      await f.handler(request);
      assert.equal(request.replies[0].content, 'Недостаточно прав.');
      assert.equal(f.calls.length, 0);
      assert.equal(request.deferred, undefined);
    }
  }
});

test('removing the coordinator role revokes access on the next interaction', async () => {
  const f = fixture();
  await f.handler(interaction());
  const next = interaction({ member: { roles: [] } });
  next.options.getSubcommand = () => 'stop';
  await f.handler(next);
  assert.equal(f.calls.length, 1);
  assert.equal(next.replies[0].content, 'Недостаточно прав.');
});

test('gathering commands from other guilds are ignored before checking roles or changing state', async () => {
  const f = fixture();
  const request = interaction({ guildId: '777777777777777777' });
  await f.handler(request);
  assert.equal(f.calls.length, 0);
  assert.equal(f.audits.length, 0);
  assert.equal(request.replies.length, 0);
});

test('public gather command registration keeps admin permissions and all legacy command definitions intact', () => {
  const definitions = commandDefinitions(config);
  const admin = definitions.find((command) => command.name === 'seed-admin');
  const gather = definitions.find((command) => command.name === 'seed-gather');
  assert.equal(admin.default_member_permissions, String(PermissionFlagsBits.ManageGuild));
  assert.equal(gather.default_member_permissions, null);
  assert.deepEqual(gather.options.map((option) => option.name), ['start', 'status', 'stop']);
  for (const [newName, oldName] of [['start', 'gather'], ['status', 'gather-status'], ['stop', 'gather-stop']]) {
    assert.deepEqual(gather.options.find((option) => option.name === newName).options,
      admin.options.find((option) => option.name === oldName).options);
  }
  for (const name of ['points', 'time-add', 'points-all', 'time-add-all', 'vip-sync', 'report', 'seeders',
    'reserved-protect', 'reserved-unprotect', 'reserved-protected', 'backup', 'health']) {
    assert.equal(admin.options.some((option) => option.name === name), true, name);
    assert.equal(gather.options.some((option) => option.name === name), false, name);
  }
});

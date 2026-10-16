import { PermissionFlagsBits } from 'discord.js';

const TEXT_PERMISSIONS = [
  PermissionFlagsBits.ViewChannel,
  PermissionFlagsBits.SendMessages
];

function missingPermissions(channel, member, required) {
  const permissions = channel.permissionsFor(member);
  if (!permissions) return required;
  return required.filter((permission) => !permissions.has(permission));
}

export async function validateConfiguredChannels({ discord, config }) {
  const guild = discord.guilds.cache.get(config.discord.guildId);
  if (!guild) throw new Error('The configured Discord server is not visible to this bot');
  const botMember = guild.members.me || await guild.members.fetchMe();

  const specifications = [
    {
      name: 'seeding voice',
      id: config.discord.voiceChannelId,
      type: 'voice',
      permissions: [PermissionFlagsBits.ViewChannel]
    },
    {
      name: 'ranking',
      id: config.discord.rankChannelId,
      type: 'text',
      permissions: [...TEXT_PERMISSIONS, PermissionFlagsBits.EmbedLinks, PermissionFlagsBits.ReadMessageHistory]
    },
    {
      name: 'shop',
      id: config.discord.shopChannelId,
      type: 'text',
      permissions: [...TEXT_PERMISSIONS, PermissionFlagsBits.EmbedLinks, PermissionFlagsBits.ReadMessageHistory]
    },
    {
      name: 'rules',
      id: config.discord.rulesChannelId,
      type: 'text',
      permissions: [
        ...TEXT_PERMISSIONS,
        PermissionFlagsBits.EmbedLinks,
        PermissionFlagsBits.ReadMessageHistory,
        PermissionFlagsBits.ManageMessages
      ]
    },
    {
      name: 'audit',
      id: config.discord.auditChannelId,
      type: 'private-text',
      permissions: TEXT_PERMISSIONS
    },
    {
      name: 'backup',
      id: config.discord.backupChannelId,
      type: 'private-text',
      permissions: [...TEXT_PERMISSIONS, PermissionFlagsBits.AttachFiles]
    }
  ];

  for (const specification of specifications) {
    const channel = guild.channels.cache.get(specification.id)
      || await guild.channels.fetch(specification.id);
    if (!channel) throw new Error(`Configured ${specification.name} channel does not exist`);
    if (specification.type === 'voice' && !channel.isVoiceBased()) {
      throw new Error(`Configured ${specification.name} channel is not voice-based`);
    }
    if (specification.type !== 'voice' && (!channel.isTextBased() || !channel.isSendable())) {
      throw new Error(`Configured ${specification.name} channel is not sendable text`);
    }
    const missing = missingPermissions(channel, botMember, specification.permissions);
    if (missing.length) throw new Error(`Bot lacks required permissions in ${specification.name} channel`);

    if (specification.type === 'private-text') {
      const everyone = channel.permissionsFor(guild.roles.everyone);
      if (!everyone || everyone.has(PermissionFlagsBits.ViewChannel)) {
        throw new Error(`Configured ${specification.name} channel must be hidden from @everyone`);
      }
    }
  }

  if (!botMember.permissions?.has(PermissionFlagsBits.ManageRoles)) {
    throw new Error('Bot lacks the Manage Roles permission required for seeding rewards');
  }
  const managedRoles = [
    ['Seeder', config.discord.seederRoleId],
    ['Super Seeder', config.discord.superSeederRoleId],
    ['VIP', config.discord.vipRoleId]
  ];
  for (const [name, id] of managedRoles) {
    const role = guild.roles.cache?.get(id) || await guild.roles.fetch(id);
    if (!role) throw new Error(`Configured ${name} role does not exist`);
    if (role.managed) throw new Error(`Configured ${name} role is managed by an integration`);
    if (role.position >= botMember.roles.highest.position) {
      throw new Error(`Bot role must be above the configured ${name} role`);
    }
  }
}

import {
  ChannelType,
  PermissionFlagsBits,
  REST,
  Routes,
  SlashCommandBuilder
} from 'discord.js';

function addGatherSubcommands(command, names) {
  return command
    .addSubcommand((sub) => sub
      .setName(names.start)
      .setDescription('Позвать игроков и опубликовать обновляемую карточку сбора')
      .addStringOption((option) => option.setName('server').setDescription('Игровой сервер').setRequired(true)
        .addChoices({ name: 'Сервер 1', value: 'main' }, { name: 'Сервер 2', value: 'second' }))
      .addIntegerOption((option) => option.setName('target').setDescription('Цель сбора: по умолчанию 20 игроков').setMinValue(2).setMaxValue(100))
      .addRoleOption((option) => option.setName('role').setDescription('Кого уведомить: по умолчанию роль «Сидер»'))
      .addChannelOption((option) => option.setName('channel').setDescription('Канал карточки: по умолчанию текущий')
        .addChannelTypes(ChannelType.GuildText, ChannelType.GuildAnnouncement))
      .addIntegerOption((option) => option.setName('minutes').setDescription('Срок сбора: по умолчанию 120 минут').setMinValue(15).setMaxValue(360))
      .addStringOption((option) => option.setName('instructions').setDescription('Своя инструкция подключения; иначе используется игровое название из RCON')
        .setMinLength(3).setMaxLength(1000)))
    .addSubcommand((sub) => sub
      .setName(names.status).setDescription('Проверить сбор и обновить карточку')
      .addStringOption((option) => option.setName('server').setDescription('Игровой сервер').setRequired(true)
        .addChoices({ name: 'Сервер 1', value: 'main' }, { name: 'Сервер 2', value: 'second' })))
    .addSubcommand((sub) => sub
      .setName(names.stop).setDescription('Остановить сбор игроков и закрыть карточку')
      .addStringOption((option) => option.setName('server').setDescription('Игровой сервер').setRequired(true)
        .addChoices({ name: 'Сервер 1', value: 'main' }, { name: 'Сервер 2', value: 'second' })));
}

export function commandDefinitions(config) {
  const seed = new SlashCommandBuilder()
    .setName('seed')
    .setDescription('Сидинг WARDOGS, баланс и VIP')
    .addSubcommand((sub) => sub
      .setName('link')
      .setDescription('Привязать Steam для сидинга через код в игре')
      .addStringOption((option) => option
        .setName('steam_id')
        .setDescription('Ваш 17-значный SteamID64')
        .setMinLength(17)
        .setMaxLength(17)
        .setRequired(true)))
    .addSubcommand((sub) => sub
      .setName('verify')
      .setDescription('Подтвердить код, полученный в игре')
      .addStringOption((option) => option
        .setName('code')
        .setDescription('Одноразовый код из сообщения на игровом сервере')
        .setMinLength(8)
        .setMaxLength(8)
        .setRequired(true)))
    .addSubcommand((sub) => sub
      .setName('unlink')
      .setDescription('Отменить привязку Discord к Steam')
      .addBooleanOption((option) => option
        .setName('confirm')
        .setDescription('Подтвердите отвязку аккаунта')
        .setRequired(true)))
    .addSubcommand((sub) => sub.setName('status').setDescription('Показать баланс, время и статус VIP'))
    .addSubcommand((sub) => sub.setName('top').setDescription('Показать рейтинг текущей недели'))
    .addSubcommand((sub) => sub.setName('shop').setDescription('Показать магазин VIP'))
    .addSubcommand((sub) => sub
      .setName('buy')
      .setDescription('Купить VIP за баллы')
      .addStringOption((option) => option
        .setName('plan')
        .setDescription('Срок VIP')
        .setRequired(true)
        .addChoices(
          { name: `1 день — ${config.vip.oneDayCost} баллов`, value: '1d' },
          { name: `7 дней — ${config.vip.sevenDayCost} баллов`, value: '7d' },
          { name: `30 дней — ${config.vip.thirtyDayCost} баллов`, value: '30d' }
        )))
    .addSubcommand((sub) => sub.setName('help').setDescription('Как работает сидинг'));

  const donor = new SlashCommandBuilder()
    .setName('vip-link')
    .setDescription('Steam-привязка для оплаченного VIP без участия в сидинге')
    .addSubcommand((sub) => sub
      .setName('link')
      .setDescription('Привязать Steam только для оплаченного VIP')
      .addStringOption((option) => option
        .setName('steam_id')
        .setDescription('Ваш 17-значный SteamID64')
        .setMinLength(17)
        .setMaxLength(17)
        .setRequired(true)))
    .addSubcommand((sub) => sub
      .setName('verify')
      .setDescription('Подтвердить код из игры для оплаченного VIP')
      .addStringOption((option) => option
        .setName('code')
        .setDescription('Одноразовый код из игрового сообщения')
        .setMinLength(8)
        .setMaxLength(8)
        .setRequired(true)))
    .addSubcommand((sub) => sub.setName('status').setDescription('Проверить VIP-привязку'))
    .addSubcommand((sub) => sub
      .setName('unlink')
      .setDescription('Отменить отдельную VIP-привязку')
      .addBooleanOption((option) => option
        .setName('confirm')
        .setDescription('Подтвердите отвязку')
        .setRequired(true)));

  const gather = addGatherSubcommands(new SlashCommandBuilder()
    .setName('seed-gather')
    .setDescription('Сбор игроков для координаторов и администраторов WARDOGS')
    .setDefaultMemberPermissions(null), { start: 'start', status: 'status', stop: 'stop' });

  const admin = addGatherSubcommands(new SlashCommandBuilder()
    .setName('seed-admin')
    .setDescription('Администрирование отдельного бота сидинга')
    .setDefaultMemberPermissions(PermissionFlagsBits.ManageGuild)
    .addSubcommand((sub) => sub.setName('health').setDescription('Проверить Discord, RCON и целостность БД')),
    { start: 'gather', status: 'gather-status', stop: 'gather-stop' })
    .addSubcommand((sub) => sub
      .setName('points')
      .setDescription('Изменить баланс с записью в защищённый журнал')
      .addUserOption((option) => option.setName('user').setDescription('Участник').setRequired(true))
      .addIntegerOption((option) => option
        .setName('amount')
        .setDescription('Положительное начисление или отрицательное списание')
        .setMinValue(-1_000_000)
        .setMaxValue(1_000_000)
        .setRequired(true))
      .addStringOption((option) => option
        .setName('reason')
        .setDescription('Причина для аудита')
        .setMinLength(3)
        .setMaxLength(100)
        .setRequired(true)))
    .addSubcommand((sub) => sub
      .setName('time-add')
      .setDescription('Восстановить пропущенное время сидинга и баллы за текущую неделю')
      .addUserOption((option) => option.setName('user').setDescription('Подтверждённый сидер').setRequired(true))
      .addIntegerOption((option) => option
        .setName('minutes')
        .setDescription('Пропущенное время: от 1 до 1440 минут')
        .setMinValue(1)
        .setMaxValue(1440)
        .setRequired(true))
      .addStringOption((option) => option
        .setName('reason')
        .setDescription('Причина корректировки для защищённого журнала')
        .setMinLength(3)
        .setMaxLength(100)
        .setRequired(true)))
    .addSubcommand((sub) => sub
      .setName('points-all')
      .setDescription('Начислить баллы всем подтверждённым сидерам')
      .addIntegerOption((option) => option
        .setName('amount')
        .setDescription('Сколько баллов получит каждый сидер')
        .setMinValue(1)
        .setMaxValue(1_000_000)
        .setRequired(true))
      .addStringOption((option) => option
        .setName('reason')
        .setDescription('Причина массового начисления для журнала')
        .setMinLength(3)
        .setMaxLength(100)
        .setRequired(true))
      .addStringOption((option) => option
        .setName('operation_id')
        .setDescription('Уникальное имя начисления; повтор с тем же именем не начислит дважды')
        .setMinLength(3)
        .setMaxLength(64)
        .setRequired(true))
      .addBooleanOption((option) => option
        .setName('confirm')
        .setDescription('true — начислить, false — показать число получателей')
        .setRequired(true)))
    .addSubcommand((sub) => sub
      .setName('time-add-all')
      .setDescription('Добавить время и заработанные баллы всем сидерам')
      .addIntegerOption((option) => option
        .setName('minutes')
        .setDescription('Сколько минут получит каждый сидер: от 1 до 1440')
        .setMinValue(1)
        .setMaxValue(1440)
        .setRequired(true))
      .addStringOption((option) => option
        .setName('reason')
        .setDescription('Причина массовой корректировки для журнала')
        .setMinLength(3)
        .setMaxLength(100)
        .setRequired(true))
      .addStringOption((option) => option
        .setName('operation_id')
        .setDescription('Уникальное имя начисления; повтор с тем же именем не начислит дважды')
        .setMinLength(3)
        .setMaxLength(64)
        .setRequired(true))
      .addBooleanOption((option) => option
        .setName('confirm')
        .setDescription('true — начислить, false — показать число получателей')
        .setRequired(true)))
    .addSubcommand((sub) => sub.setName('vip-sync').setDescription('Немедленно сверить VIP с RCON'))
    .addSubcommand((sub) => sub
      .setName('report')
      .setDescription('Обновить рейтинг, топ-3 и их недельные награды'))
    .addSubcommand((sub) => sub
      .setName('seeders')
      .setDescription('Показать список привязанных сидеров')
      .addIntegerOption((option) => option
        .setName('page')
        .setDescription('Страница списка')
        .setMinValue(1)))
    .addSubcommand((sub) => sub
      .setName('reserved-protect')
      .setDescription('Выдать и защитить постоянный Reserved slot')
      .addUserOption((option) => option
        .setName('user')
        .setDescription('Участник с подтверждённой привязкой Steam')
        .setRequired(true)))
    .addSubcommand((sub) => sub
      .setName('reserved-unprotect')
      .setDescription('Снять защиту, не удаляя Reserved slot')
      .addUserOption((option) => option
        .setName('user')
        .setDescription('Участник с постоянным Reserved slot')
        .setRequired(true)))
    .addSubcommand((sub) => sub
      .setName('reserved-protected')
      .setDescription('Показать защищённые постоянные Reserved slots')
      .addIntegerOption((option) => option
        .setName('page')
        .setDescription('Страница списка')
        .setMinValue(1)))
    .addSubcommand((sub) => sub.setName('backup').setDescription('Создать зашифрованную резервную копию сейчас'));

  return [seed.toJSON(), donor.toJSON(), admin.toJSON(), gather.toJSON()];
}

export async function registerGuildCommands(config) {
  const rest = new REST({ version: '10' }).setToken(config.discord.token);
  await rest.put(
    Routes.applicationGuildCommands(config.discord.clientId, config.discord.guildId),
    { body: commandDefinitions(config) }
  );
}

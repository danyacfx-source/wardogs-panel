import process from 'node:process';
import { Client, Events, GatewayIntentBits } from 'discord.js';
import { readConfig } from './config.js';
import { CryptoBox } from './crypto-box.js';
import { createLogger } from './logger.js';
import { SeedingDatabase } from './database.js';
import { RconError, WardogsRconClient } from './rcon-client.js';
import { registerGuildCommands } from './discord/commands.js';
import { installInteractionHandler } from './discord/interactions.js';
import { publishShopPanel } from './discord/shop.js';
import { publishRulesPanel } from './discord/rules.js';
import { validateConfiguredChannels } from './discord/channel-preflight.js';
import { installRulesChannelGuard } from './discord/rules-channel-guard.js';
import { LeaseService } from './services/lease-service.js';
import { LinkService } from './services/link-service.js';
import { SeedingService } from './services/seeding-service.js';
import { VipService } from './services/vip-service.js';
import { DonationService } from './services/donation-service.js';
import { ReportService } from './services/report-service.js';
import { BackupService } from './services/backup-service.js';
import { RoleService } from './services/role-service.js';
import { WeeklyAwardService } from './services/weekly-award-service.js';
import { ReservedProtectionService } from './services/reserved-protection-service.js';
import { SecondaryReservedService } from './services/secondary-reserved-service.js';
import { GatherService } from './services/gather-service.js';
import { loadLocalEnv } from './load-env.js';

loadLocalEnv();
process.umask(0o077);

let runtime = null;
let shuttingDown = false;

function validateRconCapabilities(capabilities) {
  const routes = capabilities.routes.map((route) => String(route)
    .trim()
    .replace(/\{[^}]+\}|:[^/\s]+/g, '*')
    .replace(/\s+/g, ' '));
  for (const required of ['GET /v1/status', 'GET /v1/players']) {
    if (!routes.includes(required)) throw new Error(`WARDOGS RCON does not advertise ${required}`);
  }
  if (!routes.includes('POST /v1/players/*/message')) {
    throw new Error('WARDOGS RCON does not advertise the whisper route required for secure linking');
  }
  if (!routes.includes('GET /v1/reserved-slots')) {
    throw new Error('WARDOGS RCON cannot verify live reserved slots, so safe VIP timing is unavailable');
  }
  const liveVip = routes.includes('POST /v1/reserved-slots')
    && routes.includes('DELETE /v1/reserved-slots/*');
  return liveVip ? 'live' : 'live-slots-unavailable';
}

function isTemporaryRconFailure(error) {
  return error instanceof RconError
    && (error.code === 'unreachable' || [429, 502, 503, 504].includes(error.status));
}

async function shutdown(reason, exitCode = 0) {
  if (shuttingDown) return;
  shuttingDown = true;
  runtime?.logger.info('shutdown_started', { reason });
  runtime?.backupService?.stop();
  runtime?.reportService?.stop();
  runtime?.vipService?.stop();
  runtime?.secondaryReservedService?.stop();
  runtime?.weeklyAwardService?.stop();
  runtime?.roleService?.stop();
  runtime?.donationService?.stop();
  runtime?.seedingService?.stop();
  runtime?.gatherService?.stop();
  runtime?.lease?.stop();
  runtime?.rcon?.close();
  runtime?.rconSecondary?.close();
  runtime?.discord?.destroy();
  runtime?.database?.close();
  process.exitCode = exitCode;
}

async function main() {
  const config = readConfig();
  const logger = createLogger({
    level: config.logLevel,
    secrets: [
      config.discord.token,
      config.rcon.password,
      config.rconSecondary.password,
      config.security.masterKey.toString('base64'),
      config.security.backupKey.toString('base64'),
      config.donations.feedToken,
      config.donations.checkoutSecret
    ]
  });
  const crypto = new CryptoBox(config.security.masterKey);
  const database = new SeedingDatabase({
    path: config.storage.databasePath,
    crypto,
    guildId: config.discord.guildId,
    logger
  });
  const integrity = database.verifyIntegrity();
  logger.info('database_verified', { users: integrity.users });

  const rcon = new WardogsRconClient(config.rcon);
  const rconSecondary = new WardogsRconClient(config.rconSecondary);
  const discord = new Client({
    intents: [
      GatewayIntentBits.Guilds,
      GatewayIntentBits.GuildVoiceStates,
      GatewayIntentBits.GuildMessages
    ],
    allowedMentions: { parse: [], repliedUser: false },
    failIfNotExists: false
  });
  const lease = new LeaseService({
    database,
    logger,
    onLost: () => shutdown('instance_lease_lost', 1).finally(() => setTimeout(() => process.exit(1), 250).unref())
  });
  const linkService = new LinkService({ database, rcon, rconSecondary, crypto, config, logger });
  const seedingService = new SeedingService({ database, rcon, rconSecondary, discord, config, logger, lease });
  const donationService = new DonationService({ database, config, logger });
  const roleService = new RoleService({ database, discord, config, logger, lease,
    donations: donationService });
  const reservedProtectionService = new ReservedProtectionService({ database });
  logger.info('protected_reserved_slots_verified', { count: reservedProtectionService.count() });
  const weeklyAwardService = new WeeklyAwardService({
    database, rcon, roleService, config, logger, lease, reservedProtectionService,
    donations: donationService
  });
  const secondaryReservedService = new SecondaryReservedService({
    database, rcon: rconSecondary, primaryRcon: rcon, config, logger, lease, weeklyAwardService,
    reservedProtectionService, donations: donationService
  });
  weeklyAwardService.secondaryReservedService = secondaryReservedService;
  const vipService = new VipService({
    database, rcon, config, logger, lease, roleService, weeklyAwardService,
    reservedProtectionService, donations: donationService, secondaryReservedService
  });
  const reportService = new ReportService({
    database, discord, config, logger, lease, weeklyAwardService
  });
  const backupService = new BackupService({ database, discord, config, logger, lease });
  const gatherService = new GatherService({ database, discord, config, logger, lease, rcon, rconSecondary });

  runtime = {
    config, logger, crypto, database, rcon, rconSecondary, discord, lease, linkService,
    seedingService, roleService, reservedProtectionService,
    weeklyAwardService, vipService, secondaryReservedService,
    donationService, reportService, backupService, gatherService
  };
  installInteractionHandler(runtime);
  installRulesChannelGuard(runtime);
  discord.on(Events.Error, (error) => logger.error('discord_client_error', { error }));
  discord.on(Events.Warn, (message) => logger.warn('discord_client_warning', { message }));

  discord.once(Events.ClientReady, async (client) => {
    try {
      const guild = client.guilds.cache.get(config.discord.guildId);
      if (!guild) throw new Error('The configured Discord server is not visible to this bot');
      lease.start();
      await validateConfiguredChannels({ discord, config });
      const [capabilitiesResult, secondaryCapabilitiesResult, shopResult, rulesResult] = await Promise.allSettled([
        rcon.capabilities(),
        rconSecondary.capabilities(),
        publishShopPanel({ database, discord, config, logger }),
        publishRulesPanel({ database, discord, config, logger })
      ]);
      if (shopResult.status === 'rejected') throw shopResult.reason;
      if (rulesResult.status === 'rejected') throw rulesResult.reason;

      let rconState = 'connected';
      let vipMode = 'pending-rcon';
      if (capabilitiesResult.status === 'fulfilled') {
        vipMode = validateRconCapabilities(capabilitiesResult.value);
      } else if (isTemporaryRconFailure(capabilitiesResult.reason)) {
        rconState = 'degraded';
        logger.warn('startup_rcon_unavailable', { error: capabilitiesResult.reason });
      } else {
        throw capabilitiesResult.reason;
      }
      let secondaryRconState = 'connected';
      let secondaryVipMode = 'pending-rcon';
      try {
        if (secondaryCapabilitiesResult.status === 'rejected') throw secondaryCapabilitiesResult.reason;
        secondaryVipMode = validateRconCapabilities(secondaryCapabilitiesResult.value);
      } catch (error) {
        secondaryRconState = 'degraded';
        logger.warn('startup_secondary_rcon_unavailable', { error });
      }
      if (donationService.enabled) {
        try { await donationService.refresh(); }
        catch (error) { logger.warn('donation_feed_initial_unavailable', { error }); }
        donationService.start(async () => {
          await roleService.reconcile();
          await vipService.reconcile();
        });
      }
      await roleService.reconcile();
      await weeklyAwardService.reconcile();
      seedingService.start();
      gatherService.start();
      roleService.start({ reconcileImmediately: false });
      weeklyAwardService.start({ reconcileImmediately: false });
      vipService.start();
      secondaryReservedService.start();
      reportService.start();
      backupService.start();
      logger.info('bot_ready', {
        botUser: client.user.tag,
        guild: config.discord.guildId,
        rconTransport: config.rcon.url.protocol,
        rconState,
        secondaryRconState,
        vipMode,
        secondaryVipMode,
        donationFeed: donationService.enabled ? 'enabled' : 'disabled'
      });
    } catch (error) {
      logger.error('startup_self_check_failed', { error });
      await shutdown('startup_self_check_failed', 1);
      setTimeout(() => process.exit(1), 250).unref();
    }
  });

  await registerGuildCommands(config);
  logger.info('discord_commands_registered');
  await discord.login(config.discord.token);
}

process.on('SIGINT', () => shutdown('SIGINT').finally(() => setTimeout(() => process.exit(0), 250).unref()));
process.on('SIGTERM', () => shutdown('SIGTERM').finally(() => setTimeout(() => process.exit(0), 250).unref()));
process.on('uncaughtException', (error) => {
  runtime?.logger.error('uncaught_exception', { error });
  shutdown('uncaught_exception', 1).finally(() => setTimeout(() => process.exit(1), 250).unref());
});
process.on('unhandledRejection', (error) => {
  runtime?.logger.error('unhandled_rejection', { error });
  shutdown('unhandled_rejection', 1).finally(() => setTimeout(() => process.exit(1), 250).unref());
});

main().catch(async (error) => {
  if (runtime?.logger) runtime.logger.error('startup_failed', { error });
  else console.error(JSON.stringify({ timestamp: new Date().toISOString(), level: 'error', event: 'startup_failed', message: error.message }));
  await shutdown('startup_failed', 1);
  setTimeout(() => process.exit(1), 250).unref();
});

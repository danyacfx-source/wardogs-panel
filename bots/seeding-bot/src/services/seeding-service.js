import { isInsideSeedWindow, weekKey } from '../time.js';
import { RconError } from '../rcon-client.js';

export class SeedingService {
  constructor({ database, rcon, rconSecondary = null, discord, config, logger, lease }) {
    this.database = database;
    this.servers = [{ id: 'main', label: 'Сервер 1', rcon, gateKey: 'population_gate' }];
    if (rconSecondary) this.servers.push({
      id: 'second', label: 'Сервер 2', rcon: rconSecondary, gateKey: 'population_gate_second'
    });
    this.discord = discord;
    this.config = config;
    this.logger = logger;
    this.lease = lease;
    this.timer = null;
    this.running = false;
    this.lastStatus = { state: 'starting', updatedAt: Date.now(), servers: [] };
    this.rconOutages = new Map();
  }

  #recordRconFailure(server, error, now) {
    const logIntervalMs = this.config.rcon.outageLogIntervalSeconds * 1000;
    let outage = this.rconOutages.get(server.id);
    if (!outage) {
      outage = { since: now, lastLoggedAt: now };
      this.rconOutages.set(server.id, outage);
      this.logger.warn('rcon_connection_lost', { server: server.id, error });
      return outage;
    }
    if (now - outage.lastLoggedAt >= logIntervalMs) {
      outage.lastLoggedAt = now;
      this.logger.warn('rcon_still_unreachable', {
        server: server.id, error, outageSeconds: Math.floor((now - outage.since) / 1000)
      });
    }
    return outage;
  }

  #recordRconSuccess(server, now) {
    const outage = this.rconOutages.get(server.id);
    if (!outage) return;
    this.logger.info('rcon_connection_restored', {
      server: server.id, outageSeconds: Math.floor((now - outage.since) / 1000)
    });
    this.rconOutages.delete(server.id);
  }

  start() {
    this.database.resetSessions(Date.now());
    for (const server of this.servers) {
      const gate = this.database.getState(server.gateKey, { blocked: false, belowSince: null });
      if (gate.blocked) this.database.setState(server.gateKey, { blocked: true, belowSince: null });
    }
    this.tick().catch((error) => this.logger.error('seed_initial_tick_failed', { error }));
    this.timer = setInterval(() => {
      this.tick().catch((error) => this.logger.error('seed_tick_unhandled', { error }));
    }, this.config.seed.tickSeconds * 1000);
    this.timer.unref();
  }

  stop() {
    if (this.timer) clearInterval(this.timer);
  }

  #pause(now) {
    return this.database.applySeedingTick({
      eligibleMembers: [], now, weekKey: weekKey(now),
      graceSeconds: this.config.seed.sessionGraceSeconds,
      maxDeltaSeconds: this.config.seed.tickSeconds * 2
    });
  }

  async #pollServer(server, now) {
    try {
      const [players, status] = await Promise.all([server.rcon.players(), server.rcon.status()]);
      this.#recordRconSuccess(server, now);
      const population = Math.max(players.length, status.playerCount);
      let gate = this.database.getState(server.gateKey, { blocked: false, belowSince: null });
      if (population >= this.config.seed.stopPlayerCount) {
        gate = { blocked: true, belowSince: null };
      } else if (gate.blocked) {
        gate.belowSince ||= now;
        if (now - gate.belowSince >= this.config.seed.resumeBelowSeconds * 1000) {
          gate = { blocked: false, belowSince: null };
        }
      } else {
        gate.belowSince = null;
      }
      this.database.setState(server.gateKey, gate);
      return {
        id: server.id, label: server.label,
        state: gate.blocked ? 'population_limit' : 'active',
        population, maxPlayers: status.maxPlayers, belowSince: gate.belowSince, players
      };
    } catch (error) {
      if (!(error instanceof RconError)) throw error;
      const outage = this.#recordRconFailure(server, error, now);
      return {
        id: server.id, label: server.label, state: 'rcon_unavailable', players: [],
        population: null, outageSince: outage.since,
        errorCode: error.code || 'unavailable', networkCode: error.networkCode || ''
      };
    }
  }

  async tick() {
    if (this.running || !this.lease.held) return;
    this.running = true;
    const now = Date.now();
    try {
      const inside = isInsideSeedWindow(now, this.config.seed.start.minutes, this.config.seed.end.minutes);
      if (!inside) {
        this.#pause(now);
        this.lastStatus = { state: 'closed', updatedAt: now, servers: [], eligibleCount: 0 };
        return;
      }

      const snapshots = await Promise.all(this.servers.map((server) => this.#pollServer(server, now)));
      const active = snapshots.filter((server) => server.state === 'active');
      const eligible = new Map();
      if (active.length) {
        const guild = this.discord.guilds.cache.get(this.config.discord.guildId);
        const channel = guild?.channels.cache.get(this.config.discord.voiceChannelId)
          || await guild?.channels.fetch(this.config.discord.voiceChannelId);
        if (!channel?.isVoiceBased()) throw new Error('Configured seeding channel is not a voice channel');
        const voice = new Map(
          [...guild.voiceStates.cache.values()]
            .filter((state) => state.channelId === this.config.discord.voiceChannelId)
            .map((state) => [state.id, state])
        );
        for (const server of active) {
          for (const player of server.players) {
            const user = this.database.getUserBySteam(player.steamId);
            if (!user) continue;
            const state = voice.get(user.discordId);
            if (!state || state.member?.user?.bot) continue;
            eligible.set(user.discordId, {
              discordId: user.discordId,
              displayName: state.member?.displayName || user.displayName
            });
          }
        }
      }

      const awards = this.database.applySeedingTick({
        eligibleMembers: [...eligible.values()], now, weekKey: weekKey(now),
        graceSeconds: this.config.seed.sessionGraceSeconds,
        maxDeltaSeconds: this.config.seed.tickSeconds * 2
      });
      const state = active.length ? 'active'
        : snapshots.some((server) => server.state === 'population_limit') ? 'population_limit'
          : 'rcon_unavailable';
      this.lastStatus = {
        state, updatedAt: now, eligibleCount: eligible.size,
        servers: snapshots.map(({ players, ...server }) => server),
        population: snapshots.reduce((sum, server) => sum + (server.population || 0), 0)
      };
      if (awards.length) this.logger.info('seed_points_awarded', {
        users: awards.length, points: awards.reduce((n, a) => n + a.points, 0)
      });
    } catch (error) {
      try { this.#pause(now); }
      catch (databaseError) { this.logger.error('seed_pause_failed', { error: databaseError }); }
      this.lastStatus = { state: 'error', updatedAt: now, errorCode: error?.code || 'unavailable', servers: [] };
      this.logger.warn('seed_tick_failed', { error });
    } finally {
      this.running = false;
    }
  }
}

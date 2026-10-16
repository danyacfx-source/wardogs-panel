import http from 'node:http';
import https from 'node:https';
import tls from 'node:tls';
import { createHash } from 'node:crypto';
import { setTimeout as delay } from 'node:timers/promises';
import { readReservedIds } from './reserved-config.js';

const STEAM_ID = /^7656119\d{10}$/;
const ALLOWED_ROUTES = [
  /^\/v1\/(?:capabilities|status|players|reserved-slots|config)$/,
  /^\/v1\/players\/7656119\d{10}\/message$/,
  /^\/v1\/reserved-slots\/7656119\d{10}$/
];
const TRANSIENT_HTTP_STATUS = new Set([429, 502, 503, 504]);
const SAFE_NETWORK_CODES = new Set([
  'ECONNREFUSED',
  'ECONNRESET',
  'EHOSTUNREACH',
  'ENETUNREACH',
  'ENOTFOUND',
  'EAI_AGAIN',
  'ETIMEDOUT',
  'ERR_SOCKET_CLOSED',
  'ERR_TLS_CERT_ALTNAME_INVALID',
  'DEPTH_ZERO_SELF_SIGNED_CERT'
]);

function validRoute(path) {
  return ALLOWED_ROUTES.some((pattern) => pattern.test(path));
}

function fingerprint(raw) {
  return createHash('sha256').update(raw).digest('hex').toUpperCase();
}

export class RconError extends Error {
  constructor(message, {
    code = 'rcon_error',
    status = 0,
    route = '',
    missingRoute = false,
    networkCode = ''
  } = {}) {
    super(message);
    this.name = 'RconError';
    this.code = code;
    this.status = status;
    this.route = route;
    this.missingRoute = missingRoute;
    this.networkCode = networkCode;
  }
}

export class WardogsRconClient {
  constructor(config) {
    this.baseUrl = config.url;
    this.password = config.password;
    this.timeoutMs = config.timeoutMs;
    this.maxResponseBytes = config.maxResponseBytes;
    this.tlsPin = config.tlsPin;
    this.retryAttempts = config.retryAttempts ?? 1;
    this.retryBaseMs = config.retryBaseMs ?? 300;
    this.agent = this.#createAgent();
    this.reservedMutationTail = Promise.resolve();
    this.health = {
      lastSuccessAt: null,
      lastFailureAt: null,
      outageStartedAt: null,
      consecutiveFailures: 0,
      lastErrorCode: '',
      lastNetworkCode: '',
      lastRoute: ''
    };
  }

  close() {
    this.agent.destroy();
  }

  #createAgent() {
    return this.baseUrl.protocol === 'https:'
      ? new https.Agent({ keepAlive: true, maxSockets: 4, rejectUnauthorized: true })
      : new http.Agent({ keepAlive: true, maxSockets: 4 });
  }

  #resetAgent(failedAgent) {
    if (this.agent !== failedAgent) return;
    this.agent = this.#createAgent();
    failedAgent.destroy();
  }

  #recordSuccess() {
    this.health.lastSuccessAt = Date.now();
    this.health.outageStartedAt = null;
    this.health.consecutiveFailures = 0;
    this.health.lastErrorCode = '';
    this.health.lastNetworkCode = '';
    this.health.lastRoute = '';
  }

  #recordFailure(error) {
    const now = Date.now();
    this.health.lastFailureAt = now;
    this.health.outageStartedAt ||= now;
    this.health.consecutiveFailures += 1;
    this.health.lastErrorCode = typeof error?.code === 'string' ? error.code : 'rcon_error';
    this.health.lastNetworkCode = typeof error?.networkCode === 'string' ? error.networkCode : '';
    this.health.lastRoute = typeof error?.route === 'string' ? error.route : '';
  }

  healthSnapshot() {
    return Object.freeze({ ...this.health });
  }

  #networkError(error, method, path) {
    const rawCode = typeof error?.code === 'string' ? error.code.toUpperCase() : '';
    const networkCode = SAFE_NETWORK_CODES.has(rawCode) ? rawCode : 'NETWORK_ERROR';
    return new RconError('RCON is unreachable', {
      code: 'unreachable',
      route: `${method} ${path}`,
      networkCode
    });
  }

  async #request(method, path, options = {}) {
    const canRetry = method === 'GET';
    let lastError;
    for (let attempt = 0; attempt <= this.retryAttempts; attempt += 1) {
      try {
        const response = await this.#requestOnce(method, path, options);
        if (
          canRetry
          && !response.ok
          && TRANSIENT_HTTP_STATUS.has(response.status)
          && attempt < this.retryAttempts
        ) {
          await delay(this.retryBaseMs * (attempt + 1));
          continue;
        }
        if (response.ok) this.#recordSuccess();
        else this.#recordFailure(response.error);
        return response;
      } catch (error) {
        lastError = error instanceof RconError ? error : this.#networkError(error, method, path);
        if (
          canRetry
          && (lastError.code === 'unreachable' || TRANSIENT_HTTP_STATUS.has(lastError.status))
          && attempt < this.retryAttempts
        ) {
          await delay(this.retryBaseMs * (attempt + 1));
          continue;
        }
        this.#recordFailure(lastError);
        throw lastError;
      }
    }
    this.#recordFailure(lastError);
    throw lastError;
  }

  async #requestOnce(method, path, {
    json,
    text,
    headers = {},
    allowFailure = false
  } = {}) {
    if (!validRoute(path)) throw new Error('RCON route is not allowlisted');
    if (json !== undefined && text !== undefined) throw new Error('Only one RCON body type is allowed');

    let body;
    let contentType;
    if (json !== undefined) {
      body = Buffer.from(JSON.stringify(json), 'utf8');
      contentType = 'application/json';
    } else if (text !== undefined) {
      body = Buffer.from(String(text), 'utf8');
      contentType = 'text/plain; charset=utf-8';
    }
    if (body && body.length > this.maxResponseBytes) throw new Error('RCON request is too large');

    const transport = this.baseUrl.protocol === 'https:' ? https : http;
    const requestAgent = this.agent;
    const options = {
      protocol: this.baseUrl.protocol,
      hostname: this.baseUrl.hostname,
      port: this.baseUrl.port || undefined,
      method,
      path,
      agent: requestAgent,
      headers: {
        Accept: 'application/json',
        Authorization: `Bearer ${this.password}`,
        'User-Agent': 'wardogs-seeding-bot/1.5.1',
        ...(body ? { 'Content-Type': contentType, 'Content-Length': String(body.length) } : {}),
        ...headers
      }
    };

    if (this.baseUrl.protocol === 'https:' && this.tlsPin) {
      options.checkServerIdentity = (host, certificate) => {
        const standardError = tls.checkServerIdentity(host, certificate);
        if (standardError) return standardError;
        if (!certificate.raw || fingerprint(certificate.raw) !== this.tlsPin) {
          return new Error('RCON TLS certificate fingerprint does not match');
        }
        return undefined;
      };
    }

    return new Promise((resolve, reject) => {
      const request = transport.request(options, (response) => {
        const chunks = [];
        let bytes = 0;
        response.on('data', (chunk) => {
          bytes += chunk.length;
          if (bytes > this.maxResponseBytes) {
            response.destroy(new RconError('RCON response exceeded the configured size limit', {
              code: 'response_too_large',
              route: `${method} ${path}`
            }));
            return;
          }
          chunks.push(chunk);
        });
        response.on('error', (error) => {
          if (error instanceof RconError) reject(error);
          else {
            this.#resetAgent(requestAgent);
            reject(this.#networkError(error, method, path));
          }
        });
        response.on('end', () => {
          const raw = Buffer.concat(chunks).toString('utf8');
          let parsed = null;
          if (raw) {
            try {
              parsed = JSON.parse(raw);
            } catch {
              const contentTypeHeader = String(response.headers['content-type'] || '').toLowerCase();
              const isHtml = contentTypeHeader.includes('text/html') || /^\s*(?:<!doctype\s+html|<html\b)/i.test(raw);
              reject(new RconError(
                isHtml
                  ? 'RCON URL points to an HTML control panel, not the WDRCON API'
                  : 'RCON returned malformed JSON',
                {
                  code: isHtml ? 'wrong_endpoint' : 'bad_response',
                  route: `${method} ${path}`
                }
              ));
              return;
            }
          }
          const status = response.statusCode || 0;
          if (status < 200 || status >= 300) {
            const code = typeof parsed?.error?.code === 'string' ? parsed.error.code : 'request_failed';
            const serverMessage = typeof parsed?.error?.message === 'string' ? parsed.error.message : '';
            const missingRoute =
              status === 405 ||
              (status === 404 && code === 'not_found' && /no such endpoint/i.test(serverMessage));
            const error = new RconError(`RCON request failed (${status || 'no status'})`, {
              status,
              code,
              route: `${method} ${path}`,
              missingRoute
            });
            if (allowFailure) resolve({ ok: false, status, parsed, headers: response.headers, error });
            else reject(error);
            return;
          }
          resolve({ ok: true, status, parsed: parsed ?? {}, headers: response.headers });
        });
      });
      request.setTimeout(this.timeoutMs, () => {
        const error = new Error('RCON request timed out');
        error.code = 'ETIMEDOUT';
        request.destroy(error);
      });
      request.on('error', (error) => {
        if (error instanceof RconError) {
          reject(error);
          return;
        }
        this.#resetAgent(requestAgent);
        reject(this.#networkError(error, method, path));
      });
      if (body) request.write(body);
      request.end();
    });
  }

  async capabilities() {
    const { parsed } = await this.#request('GET', '/v1/capabilities');
    return {
      routes: Array.isArray(parsed.routes) ? parsed.routes.map(String) : [],
      configWritable: parsed.config?.writable === true
    };
  }

  async assertLiveReservedWritable() {
    const { routes } = await this.capabilities();
    const normalized = routes.map((route) => route.trim()
      .replace(/\{[^}]+\}|:[^/\s]+/g, '*').replace(/\s+/g, ' '));
    if (!['GET /v1/reserved-slots', 'POST /v1/reserved-slots',
      'DELETE /v1/reserved-slots/*'].every((route) => normalized.includes(route))) {
      throw new RconError('This WDRCON build cannot change reserved slots without a server restart', {
        code: 'live_reserved_unavailable', route: 'GET /v1/capabilities'
      });
    }
  }

  async status() {
    const { parsed } = await this.#request('GET', '/v1/status');
    const current = Number(parsed.players?.current);
    const maximum = Number(parsed.players?.max);
    if (!Number.isInteger(current) || current < 0) {
      throw new RconError('RCON returned an invalid status', { code: 'bad_response', route: 'GET /v1/status' });
    }
    return {
      playerCount: current,
      maxPlayers: Number.isInteger(maximum) && maximum >= 0 ? maximum : null,
      serverName: typeof parsed.serverName === 'string' ? parsed.serverName.slice(0, 200) : '',
      map: typeof parsed.map === 'string' ? parsed.map.slice(0, 100) : ''
    };
  }

  async players() {
    const { parsed } = await this.#request('GET', '/v1/players');
    if (!Array.isArray(parsed.players)) {
      throw new RconError('RCON returned an invalid player list', {
        code: 'bad_response',
        route: 'GET /v1/players'
      });
    }
    return parsed.players.map((player) => {
      const steamId = String(player?.steamId ?? '');
      if (!STEAM_ID.test(steamId)) {
        throw new RconError('RCON returned a player with an invalid SteamID64', {
          code: 'bad_response',
          route: 'GET /v1/players'
        });
      }
      return {
        steamId,
        name: String(player?.name ?? 'Игрок').replace(/[\r\n\0]/g, '').slice(0, 80)
      };
    });
  }

  async whisper(steamId, message) {
    if (!STEAM_ID.test(String(steamId))) throw new Error('Invalid SteamID64');
    const safeMessage = String(message).replace(/[\r\n\0]/g, ' ').trim().slice(0, 180);
    if (!safeMessage) throw new Error('RCON whisper cannot be empty');
    await this.#request('POST', `/v1/players/${steamId}/message`, {
      json: { message: safeMessage }
    });
  }

  async liveReservedIds() {
    const response = await this.#request('GET', '/v1/reserved-slots', { allowFailure: true });
    if (!response.ok) {
      if (response.error.missingRoute) return null;
      throw response.error;
    }
    const ids = response.parsed.reservedSlots;
    if (!Array.isArray(ids) || ids.some((id) => !STEAM_ID.test(String(id)))) {
      throw new RconError('RCON returned an invalid reserved-slot list', {
        code: 'bad_response',
        route: 'GET /v1/reserved-slots'
      });
    }
    return [...new Set(ids.map(String))];
  }

  async readConfigDocument() {
    const { parsed, headers } = await this.#request('GET', '/v1/config');
    const etag = String(headers.etag || '').replace(/^W\//, '').replaceAll('"', '').trim();
    const revision = String(parsed.revision || etag || '');
    if (typeof parsed.text !== 'string' || !revision || !Array.isArray(parsed.sections)) {
      throw new RconError('RCON returned an invalid configuration document', {
        code: 'bad_response',
        route: 'GET /v1/config'
      });
    }
    return { text: parsed.text, revision, writable: parsed.writable !== false };
  }

  async documentReservedIds() {
    try {
      const document = await this.readConfigDocument();
      return { ids: readReservedIds(document.text), document };
    } catch (error) {
      if (error instanceof RconError && error.missingRoute) return null;
      throw error;
    }
  }

  async reservedSnapshot() {
    const [live, document] = await Promise.all([
      this.liveReservedIds(),
      this.documentReservedIds().catch((error) => {
        // The config document is only ownership context; the live list is authoritative.
        if (error instanceof RconError) return null;
        throw error;
      })
    ]);
    return { live, document: document?.ids ?? null };
  }

  async setReserved(steamId, desired) {
    if (!STEAM_ID.test(String(steamId))) throw new Error('Invalid SteamID64');
    let release;
    const previous = this.reservedMutationTail;
    this.reservedMutationTail = new Promise((resolve) => { release = resolve; });
    await previous;
    try {
      return await this.#setReservedUnlocked(steamId, desired);
    } finally {
      release();
    }
  }

  async #setReservedUnlocked(steamId, desired) {
    const live = await this.liveReservedIds();
    if (live === null) {
      throw new RconError('WDRCON cannot verify the live reserved-slot list', {
        code: 'live_reserved_unavailable', route: 'GET /v1/reserved-slots'
      });
    }
    if (live.includes(steamId) === desired) {
      return { active: desired, pendingRestart: false, changed: false, via: 'live' };
    }

    const directPath = desired ? '/v1/reserved-slots' : `/v1/reserved-slots/${steamId}`;
    const direct = await this.#request(desired ? 'POST' : 'DELETE', directPath, {
      ...(desired ? { json: { steamId } } : {}),
      allowFailure: true
    });
    if (direct.ok) return this.#confirmLiveReserved(steamId, desired, true);
    const alreadyOkay =
      (desired && direct.status === 409 && direct.error.code === 'already_reserved') ||
      (!desired && direct.status === 404 && direct.error.code === 'reserved_not_found');
    if (alreadyOkay) return this.#confirmLiveReserved(steamId, desired, false);
    if (direct.error.missingRoute) {
      throw new RconError('This WDRCON build cannot change reserved slots without a server restart', {
        code: 'live_reserved_unavailable', route: `${desired ? 'POST' : 'DELETE'} ${directPath}`,
        status: direct.status, missingRoute: true
      });
    }
    throw direct.error;
  }

  async #confirmLiveReserved(steamId, desired, changed) {
    for (let attempt = 0; attempt < 3; attempt += 1) {
      if (attempt) await delay(250 * attempt);
      const live = await this.liveReservedIds();
      if (live?.includes(steamId) === desired && live !== null) {
        return { active: desired, pendingRestart: false, changed, via: 'live' };
      }
    }
    throw new RconError('RCON accepted the command but the live reserved-slot list did not change', {
      code: 'reserved_not_applied', route: 'GET /v1/reserved-slots'
    });
  }
}

import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { once } from 'node:events';
import { RconError, WardogsRconClient } from '../src/rcon-client.js';
import { readReservedIds } from '../src/reserved-config.js';

const STEAM = '76561198000000001';

test('RCON client never edits config when live slot commands are unavailable', async (t) => {
  let configText = '[/Script/WDGame.WDGameSession]\n!DefaultReservedPlayerIds=ClearArray\n';
  let revision = '1';
  let live = [];
  let configWrites = 0;
  let whispered = '';
  const server = http.createServer(async (request, response) => {
    assert.equal(request.headers.authorization, 'Bearer test-rcon-password');
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    const body = Buffer.concat(chunks).toString('utf8');
    const send = (status, value, headers = {}) => {
      response.writeHead(status, { 'Content-Type': 'application/json', ...headers });
      response.end(JSON.stringify(value));
    };
    if (request.method === 'GET' && request.url === '/v1/capabilities') {
      send(200, { routes: ['GET /v1/status', 'GET /v1/players', 'GET /v1/config', 'PUT /v1/config'] });
    } else if (request.method === 'GET' && request.url === '/v1/status') {
      send(200, { players: { current: 1, max: 100 }, serverName: 'Test' });
    } else if (request.method === 'GET' && request.url === '/v1/players') {
      send(200, { players: [{ steamId: STEAM, name: 'Tester' }] });
    } else if (request.method === 'POST' && request.url === `/v1/players/${STEAM}/message`) {
      whispered = JSON.parse(body).message;
      send(200, { ok: true });
    } else if (request.method === 'GET' && request.url === '/v1/reserved-slots') {
      send(200, { reservedSlots: live });
    } else if ((request.method === 'POST' && request.url === '/v1/reserved-slots')
      || (request.method === 'DELETE' && request.url === `/v1/reserved-slots/${STEAM}`)) {
      send(404, { error: { code: 'not_found', message: 'No such endpoint.' } });
    } else if (request.method === 'GET' && request.url === '/v1/config') {
      send(200, { revision, writable: true, text: configText, sections: [] }, { ETag: `"${revision}"` });
    } else if (request.method === 'PUT' && request.url === '/v1/config') {
      configWrites++;
      assert.equal(request.headers['if-match'], `"${revision}"`);
      configText = body;
      revision = String(Number(revision) + 1);
      send(200, { ok: true, revision });
    } else {
      send(404, { error: { code: 'not_found', message: 'No such endpoint.' } });
    }
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  t.after(() => server.close());
  const { port } = server.address();
  const client = new WardogsRconClient({
    url: new URL(`http://127.0.0.1:${port}`),
    password: 'test-rcon-password',
    timeoutMs: 2000,
    maxResponseBytes: 1_048_576,
    tlsPin: ''
  });
  t.after(() => client.close());

  assert.equal((await client.status()).playerCount, 1);
  assert.equal((await client.players())[0].steamId, STEAM);
  await client.whisper(STEAM, 'link code ABCDEFGH');
  assert.equal(whispered, 'link code ABCDEFGH');

  await assert.rejects(client.assertLiveReservedWritable(),
    (error) => error.code === 'live_reserved_unavailable');
  await assert.rejects(client.setReserved(STEAM, true),
    (error) => error.code === 'live_reserved_unavailable');
  assert.deepEqual(readReservedIds(configText), []);
  assert.equal(configWrites, 0);
  live = [STEAM];
  const active = await client.setReserved(STEAM, true);
  assert.equal(active.active, true);

  await assert.rejects(client.setReserved(STEAM, false),
    (error) => error.code === 'live_reserved_unavailable');
  assert.deepEqual(readReservedIds(configText), []);
  assert.equal(configWrites, 0);
});

test('successful live response is verified against actual live slot state', async (t) => {
  const server = http.createServer((request, response) => {
    response.writeHead(200, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify(request.method === 'GET'
      ? { reservedSlots: [] } : { message: 'accepted, but not applied' }));
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  t.after(() => server.close());
  const client = new WardogsRconClient({
    url: new URL(`http://127.0.0.1:${server.address().port}`),
    password: 'test-rcon-password', timeoutMs: 2000,
    maxResponseBytes: 1_048_576, tlsPin: ''
  });
  t.after(() => client.close());
  await assert.rejects(client.setReserved(STEAM, true),
    (error) => error.code === 'reserved_not_applied');
});

test('RCON client replaces a broken keep-alive connection and retries safe GET requests', async (t) => {
  let requests = 0;
  const server = http.createServer((request, response) => {
    requests += 1;
    if (requests === 1) {
      request.socket.destroy();
      return;
    }
    response.writeHead(200, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify({ players: { current: 4, max: 100 }, serverName: 'Recovered' }));
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  t.after(() => server.close());
  const { port } = server.address();
  const client = new WardogsRconClient({
    url: new URL(`http://127.0.0.1:${port}`),
    password: 'test-rcon-password',
    timeoutMs: 1000,
    retryAttempts: 1,
    retryBaseMs: 10,
    maxResponseBytes: 1_048_576,
    tlsPin: ''
  });
  t.after(() => client.close());

  assert.equal((await client.status()).playerCount, 4);
  assert.equal(requests, 2);
  const health = client.healthSnapshot();
  assert.equal(health.consecutiveFailures, 0);
  assert.equal(typeof health.lastSuccessAt, 'number');
});

test('RCON client reports an HTML control panel as the wrong endpoint', async (t) => {
  const server = http.createServer((_request, response) => {
    response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    response.end('<!doctype html><html><title>WARDOGS console</title></html>');
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  t.after(() => server.close());
  const { port } = server.address();
  const client = new WardogsRconClient({
    url: new URL(`http://127.0.0.1:${port}`),
    password: 'test-rcon-password',
    timeoutMs: 1000,
    retryAttempts: 1,
    retryBaseMs: 10,
    maxResponseBytes: 1_048_576,
    tlsPin: ''
  });
  t.after(() => client.close());

  await assert.rejects(
    client.capabilities(),
    (error) => error instanceof RconError
      && error.code === 'wrong_endpoint'
      && /HTML control panel/.test(error.message)
  );
  assert.equal(client.healthSnapshot().lastErrorCode, 'wrong_endpoint');
});

test('reserved-slot mutations are serialized so a later protection add wins', async (t) => {
  let live = [STEAM];
  const mutations = [];
  const server = http.createServer(async (request, response) => {
    const send = (status, value) => {
      response.writeHead(status, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify(value));
    };
    if (request.method === 'GET' && request.url === '/v1/reserved-slots') {
      send(200, { reservedSlots: live });
      return;
    }
    if (request.method === 'DELETE' && request.url === `/v1/reserved-slots/${STEAM}`) {
      mutations.push('remove');
      await new Promise((resolve) => setTimeout(resolve, 30));
      live = [];
      send(200, { ok: true });
      return;
    }
    if (request.method === 'POST' && request.url === '/v1/reserved-slots') {
      mutations.push('protect');
      live = [STEAM];
      send(200, { ok: true });
      return;
    }
    send(404, { error: { code: 'not_found', message: 'No such endpoint.' } });
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  t.after(() => server.close());
  const { port } = server.address();
  const client = new WardogsRconClient({
    url: new URL(`http://127.0.0.1:${port}`),
    password: 'test-rcon-password',
    timeoutMs: 2000,
    maxResponseBytes: 1_048_576,
    tlsPin: ''
  });
  t.after(() => client.close());

  await Promise.all([
    client.setReserved(STEAM, false),
    client.setReserved(STEAM, true)
  ]);
  assert.deepEqual(mutations, ['remove', 'protect']);
  assert.deepEqual(live, [STEAM]);
});

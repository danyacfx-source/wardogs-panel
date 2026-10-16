import test from 'node:test';
import assert from 'node:assert/strict';
import { readReservedIds, writeReservedIds } from '../src/reserved-config.js';

const A = '76561198000000001';
const B = '76561198000000002';

test('reserved ids are replaced without changing unrelated config', () => {
  const source = [
    '[/Script/Other.Section]',
    'Password=do-not-touch',
    '',
    '[/Script/WDGame.WDGameSession]',
    `+DefaultReservedPlayerIds="${A}"`,
    'ServerName=WARDOGS',
    `+DefaultReservedPlayerIds=${B}`,
    '',
    '[Next.Section]',
    'Value=1'
  ].join('\r\n');
  const output = writeReservedIds(source, [B]);
  assert.deepEqual(readReservedIds(output), [B]);
  assert.match(output, /Password=do-not-touch/);
  assert.match(output, /ServerName=WARDOGS/);
  assert.match(output, /\r\n/);
  assert.match(output, /!DefaultReservedPlayerIds=ClearArray/);
});

test('session section is added when absent', () => {
  const output = writeReservedIds('[A]\nX=1\n', [A]);
  assert.deepEqual(readReservedIds(output), [A]);
  assert.match(output, /\[\/Script\/WDGame\.WDGameSession\]/);
});

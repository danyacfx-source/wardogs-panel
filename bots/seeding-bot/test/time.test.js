import test from 'node:test';
import assert from 'node:assert/strict';
import { isInsideSeedWindow, previousWeekKey, weekKey } from '../src/time.js';

test('seeding window uses Moscow time and excludes the end boundary', () => {
  assert.equal(isInsideSeedWindow(Date.parse('2026-09-23T04:59:59Z'), 8 * 60, 22 * 60), false);
  assert.equal(isInsideSeedWindow(Date.parse('2026-09-23T05:00:00Z'), 8 * 60, 22 * 60), true);
  assert.equal(isInsideSeedWindow(Date.parse('2026-09-23T18:59:59Z'), 8 * 60, 22 * 60), true);
  assert.equal(isInsideSeedWindow(Date.parse('2026-09-23T19:00:00Z'), 8 * 60, 22 * 60), false);
});

test('week key starts on Monday in Moscow', () => {
  assert.equal(weekKey(Date.parse('2026-09-20T20:59:59Z')), '2026-09-14');
  assert.equal(weekKey(Date.parse('2026-09-20T21:00:00Z')), '2026-09-21');
  assert.equal(previousWeekKey(Date.parse('2026-09-20T21:00:00Z')), '2026-09-14');
});

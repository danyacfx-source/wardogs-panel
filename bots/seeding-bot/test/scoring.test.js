import test from 'node:test';
import assert from 'node:assert/strict';
import { incrementalPoints, pointsForSessionSeconds } from '../src/scoring.js';

test('one point is awarded every six minutes during the first hour', () => {
  assert.equal(pointsForSessionSeconds(359), 0);
  assert.equal(pointsForSessionSeconds(360), 1);
  assert.equal(pointsForSessionSeconds(3599), 9);
  assert.equal(pointsForSessionSeconds(3600), 10);
});

test('rate doubles after the first hour', () => {
  assert.equal(pointsForSessionSeconds(3779), 10);
  assert.equal(pointsForSessionSeconds(3780), 11);
  assert.equal(pointsForSessionSeconds(7200), 30);
  assert.equal(incrementalPoints(3590, 3790), 2);
});

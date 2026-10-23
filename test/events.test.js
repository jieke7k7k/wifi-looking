import test from 'node:test';
import assert from 'node:assert/strict';
import { diffWifiState } from '../src/events.js';

const base = { connected: true, ssid: 'Xms', timestamp: '2026-10-18T00:00:00.000Z' };

test('detects disconnect', () => {
  const current = { connected: false, ssid: null, timestamp: '2026-10-18T00:00:01.000Z' };
  const event = diffWifiState(base, current);
  assert.equal(event.type, 'disconnected');
  assert.equal(event.previous.ssid, 'Xms');
  assert.equal(event.current.ssid, null);
});

test('detects ssid change', () => {
  const current = { connected: true, ssid: 'Other', timestamp: '2026-10-18T00:00:01.000Z' };
  const event = diffWifiState(base, current);
  assert.equal(event.type, 'changed');
});

test('ignores repeated state', () => {
  const current = { connected: true, ssid: 'xms', timestamp: '2026-10-18T00:00:01.000Z' };
  assert.equal(diffWifiState(base, current), null);
});

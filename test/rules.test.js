import test from 'node:test';
import assert from 'node:assert/strict';
import { matchesRule, RuleEngine } from '../src/rules.js';

const disconnectedEvent = {
  type: 'disconnected',
  previous: { connected: true, ssid: 'Xms' },
  current: { connected: false, ssid: null }
};

test('matches previous ssid case-insensitively', () => {
  const rule = { id: 'test', when: { event: 'disconnected', previousSsid: 'xms' }, actions: [] };
  assert.equal(matchesRule(rule, disconnectedEvent), true);
});

test('does not match another ssid', () => {
  const rule = { id: 'test', when: { event: 'disconnected', previousSsid: 'Other' }, actions: [] };
  assert.equal(matchesRule(rule, disconnectedEvent), false);
});

test('supports multiple events', () => {
  const rule = { id: 'test', when: { event: ['disconnected', 'changed'], previousSsid: 'Xms' }, actions: [] };
  assert.equal(matchesRule(rule, disconnectedEvent), true);
});

test('cooldown suppresses repeat execution', () => {
  const rule = { id: 'test', cooldownSeconds: 10, when: { event: 'disconnected' }, actions: [] };
  const engine = new RuleEngine([rule]);
  assert.equal(engine.evaluate(disconnectedEvent, 1000).length, 1);
  assert.equal(engine.evaluate(disconnectedEvent, 5000).length, 0);
  assert.equal(engine.evaluate(disconnectedEvent, 11000).length, 1);
});

import fs from 'node:fs';

export function loadConfig(configPath) {
  const raw = fs.readFileSync(configPath, 'utf8');
  let config;
  try {
    config = JSON.parse(raw);
  } catch (error) {
    throw new Error('Invalid JSON in ' + configPath + ': ' + error.message);
  }
  validateConfig(config, configPath);
  return config;
}

export function validateConfig(config, source = 'config') {
  if (!config || typeof config !== 'object') throw new Error(source + ' must be a JSON object');
  if (!Array.isArray(config.rules)) throw new Error(source + '.rules must be an array');
  const ids = new Set();
  for (const [index, rule] of config.rules.entries()) {
    if (!rule || typeof rule !== 'object') throw new Error(source + '.rules[' + index + '] must be an object');
    if (typeof rule.id !== 'string' || rule.id.trim() === '') throw new Error(source + '.rules[' + index + '].id must be a non-empty string');
    if (ids.has(rule.id)) throw new Error('Duplicate rule id: ' + rule.id);
    ids.add(rule.id);
    if (!rule.when || typeof rule.when !== 'object') throw new Error('Rule ' + rule.id + ' requires a when object');
    if (!isValidEvent(rule.when.event)) throw new Error('Rule ' + rule.id + ' has an invalid event');
    if (!Array.isArray(rule.actions) || rule.actions.length === 0) throw new Error('Rule ' + rule.id + ' requires at least one action');
    for (const [actionIndex, action] of rule.actions.entries()) {
      if (!action || typeof action !== 'object' || typeof action.type !== 'string') throw new Error('Rule ' + rule.id + ' action ' + actionIndex + ' requires a type');
    }
    if (rule.cooldownSeconds !== undefined && (typeof rule.cooldownSeconds !== 'number' || rule.cooldownSeconds < 0)) throw new Error('Rule ' + rule.id + ' cooldownSeconds must be a non-negative number');
  }
  if (config.monitor !== undefined) {
    if (typeof config.monitor !== 'object') throw new Error('monitor must be an object');
    if (config.monitor.intervalSeconds !== undefined && (typeof config.monitor.intervalSeconds !== 'number' || config.monitor.intervalSeconds < 1)) throw new Error('monitor.intervalSeconds must be a number >= 1');
  }
  return true;
}

function isValidEvent(value) {
  const valid = new Set(['connected', 'disconnected', 'changed', 'any']);
  if (typeof value === 'string') return valid.has(value);
  if (Array.isArray(value)) return value.length > 0 && value.every(item => valid.has(item));
  return false;
}

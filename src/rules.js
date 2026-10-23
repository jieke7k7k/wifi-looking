export class RuleEngine {
  constructor(rules = []) {
    this.rules = rules;
    this.lastRun = new Map();
  }

  evaluate(event, now = Date.now()) {
    const ready = this.rules.filter(rule => {
      if (!matchesRule(rule, event)) return false;
      const cooldownMs = Number(rule.cooldownSeconds ?? 0) * 1000;
      const last = this.lastRun.get(rule.id) ?? Number.NEGATIVE_INFINITY;
      return now - last >= cooldownMs;
    });
    for (const rule of ready) this.lastRun.set(rule.id, now);
    return ready;
  }
}

export function matchesRule(rule, event) {
  if (!rule || rule.enabled === false) return false;
  if (!eventMatches(rule.when?.event, event.type)) return false;
  if (!ssidMatches(rule.when?.previousSsid, event.previous?.ssid)) return false;
  if (!ssidMatches(rule.when?.currentSsid, event.current?.ssid)) return false;
  return true;
}

function eventMatches(expected, actual) {
  if (expected === 'any' || expected === undefined) return true;
  if (typeof expected === 'string') return expected === actual;
  if (Array.isArray(expected)) return expected.includes(actual) || expected.includes('any');
  return false;
}

function ssidMatches(expected, actual) {
  if (expected === undefined) return true;
  const list = Array.isArray(expected) ? expected : [expected];
  if (list.length === 0) return true;
  return list.some(pattern => {
    if (pattern === '*') return true;
    if (pattern === null || pattern === undefined || pattern === '') return actual === null || actual === undefined || actual === '';
    if (actual === null || actual === undefined) return false;
    return String(pattern).trim().toLowerCase() === String(actual).trim().toLowerCase();
  });
}

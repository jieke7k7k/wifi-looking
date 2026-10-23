import path from 'node:path';
import { loadConfig } from './config.js';
import { readWifiState } from './wifi.js';

const args = parseArgs(process.argv.slice(2));
try {
  const configPath = path.resolve(args.config ?? 'config/config.json');
  const config = loadConfig(configPath);
  const state = await readWifiState();
  console.log(JSON.stringify({
    ok: true,
    configPath,
    nodeVersion: process.version,
    platform: process.platform,
    wifi: state,
    ruleCount: config.rules.length,
    enabledRuleCount: config.rules.filter(rule => rule.enabled !== false).length
  }, null, 2));
} catch (error) {
  console.error(JSON.stringify({ ok: false, error: error.message }, null, 2));
  process.exitCode = 1;
}

function parseArgs(argv) {
  const result = {};
  for (let index = 0; index < argv.length; index++) {
    if (argv[index] === '--config') result.config = argv[++index];
  }
  return result;
}

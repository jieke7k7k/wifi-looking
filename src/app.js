import path from 'node:path';
import { loadConfig } from './config.js';
import { Logger } from './logger.js';
import { readWifiState } from './wifi.js';
import { diffWifiState } from './events.js';
import { RuleEngine } from './rules.js';
import { runAction } from './actions.js';

const args = parseArgs(process.argv.slice(2));

try {
  const configPath = path.resolve(args.config ?? 'config/config.json');
  const config = loadConfig(configPath);
  const logger = new Logger(config.logging ?? {});
  const intervalSeconds = args.interval ?? config.monitor?.intervalSeconds ?? 2;
  const engine = new RuleEngine(config.rules);

  const state = await readWifiState();
  logger.info('WiFi-Looking started', {
    config: configPath,
    intervalSeconds,
    dryRun: Boolean(args.dryRun),
    state
  });

  if (args.once) {
    process.exit(0);
  }

  let previous = state;
  let running = true;
  process.once('SIGINT', () => {
    running = false;
    logger.info('Stopping WiFi-Looking');
  });

  while (running) {
    await sleep(intervalSeconds * 1000);
    if (!running) break;
    try {
      const current = await readWifiState();
      const event = diffWifiState(previous, current);
      if (event) logger.info('WiFi event', event);
      const rules = event ? engine.evaluate(event) : [];
      for (const rule of rules) {
        logger.info('Rule matched', { ruleId: rule.id, dryRun: Boolean(args.dryRun) });
        for (const action of rule.actions) {
          if (args.dryRun) {
            logger.info('Dry-run action', { ruleId: rule.id, action });
            continue;
          }
          const result = await runAction(action, { event, rule });
          logger[result.ok ? 'info' : 'warn']('Action finished', { ruleId: rule.id, result });
        }
      }
      previous = current;
    } catch (error) {
      logger.error('Monitor tick failed', { message: error.message });
    }
  }
} catch (error) {
  console.error('WiFi-Looking failed to start: ' + error.message);
  process.exitCode = 1;
}

function parseArgs(argv) {
  const result = {};
  for (let index = 0; index < argv.length; index++) {
    const arg = argv[index];
    if (arg === '--config') result.config = argv[++index];
    else if (arg === '--interval') result.interval = Number(argv[++index]);
    else if (arg === '--dry-run') result.dryRun = true;
    else if (arg === '--once') result.once = true;
    else if (arg === '--help' || arg === '-h') result.help = true;
  }
  if (result.help) {
    console.log('Usage: node src/app.js --config config/config.json [--interval 2] [--dry-run] [--once]');
    process.exit(0);
  }
  return result;
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

import fs from 'node:fs';
import path from 'node:path';

const LEVELS = { debug: 10, info: 20, warn: 30, error: 40 };

export class Logger {
  constructor({ level = 'info', file } = {}) {
    this.level = LEVELS[level] ?? LEVELS.info;
    this.file = file ? path.resolve(file) : null;
  }

  #write(level, message, details) {
    if (LEVELS[level] < this.level) return;
    const time = new Date().toISOString();
    const suffix = details === undefined ? '' : ' ' + JSON.stringify(details);
    const line = '[' + time + '] ' + level.toUpperCase() + ' ' + message + suffix;
    console.log(line);
    if (this.file) {
      fs.mkdirSync(path.dirname(this.file), { recursive: true });
      fs.appendFileSync(this.file, line + '\n');
    }
  }

  debug(message, details) { this.#write('debug', message, details); }
  info(message, details) { this.#write('info', message, details); }
  warn(message, details) { this.#write('warn', message, details); }
  error(message, details) { this.#write('error', message, details); }
}

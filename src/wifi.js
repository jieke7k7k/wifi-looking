import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const execFileAsync = promisify(execFile);

export async function readWifiState() {
  const { stdout } = await execFileAsync('netsh', ['wlan', 'show', 'interfaces'], {
    windowsHide: true,
    maxBuffer: 1024 * 1024,
    encoding: null
  });
  return parseNetshOutput(decodeWindowsOutput(stdout));
}

export function parseNetshOutput(text) {
  const lines = text.split(/\r?\n/);
  let ssid = null;
  let stateText = null;

  for (const line of lines) {
    const index = line.indexOf(':');
    if (index === -1) continue;
    const key = line.slice(0, index).trim();
    const value = line.slice(index + 1).trim();
    if (/^SSID$/i.test(key)) ssid = value || null;
    if (/^(State|状态)$/i.test(key)) stateText = value || null;
  }

  const connected = Boolean(ssid) && isConnectedState(stateText);
  return {
    connected,
    ssid: connected ? ssid : null,
    stateText,
    timestamp: new Date().toISOString()
  };
}

function isConnectedState(value) {
  if (!value) return false;
  return /connected/i.test(value) || value.includes('已连接');
}

function decodeWindowsOutput(buffer) {
  if (buffer.length >= 2 && buffer[0] === 0xff && buffer[1] === 0xfe) {
    return new TextDecoder('utf-16le').decode(buffer.subarray(2));
  }
  if (buffer.length >= 2 && buffer[0] === 0xfe && buffer[1] === 0xff) {
    return new TextDecoder('utf-16be').decode(buffer.subarray(2));
  }
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(buffer);
  } catch {
    return new TextDecoder('gb18030').decode(buffer);
  }
}

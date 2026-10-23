import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const execFileAsync = promisify(execFile);

export async function runAction(action, context = {}) {
  switch (action.type) {
    case 'log':
      return { ok: true, type: 'log', message: action.message ?? 'Rule matched' };
    case 'http':
      return httpRequest(action);
    case 'clash.closeConnections':
      return httpRequest({
        method: 'DELETE',
        url: normalizeBaseUrl(action.baseUrl) + '/connections',
        headers: clashHeaders(action.secret),
        timeoutMs: action.timeoutMs
      });
    case 'command':
      return runCommand(action);
    default:
      throw new Error('Unsupported action type: ' + action.type);
  }
}

async function httpRequest({ method, url, headers = {}, body, timeoutMs = 5000 }) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const options = { method, headers, signal: controller.signal };
    if (body !== undefined && method !== 'GET' && method !== 'HEAD') options.body = body;
    const response = await fetch(url, options);
    const text = await response.text();
    return {
      ok: response.ok,
      type: 'http',
      status: response.status,
      body: text.slice(0, 2000)
    };
  } catch (error) {
    return { ok: false, type: 'http', error: error.message };
  } finally {
    clearTimeout(timer);
  }
}

async function runCommand(action) {
  const timeoutMs = action.timeoutMs ?? 5000;
  try {
    const { stdout, stderr } = await execFileAsync(action.command, action.args ?? [], {
      windowsHide: true,
      timeout: timeoutMs,
      maxBuffer: 1024 * 1024
    });
    return {
      ok: true,
      type: 'command',
      stdout: String(stdout).slice(0, 2000),
      stderr: String(stderr).slice(0, 2000)
    };
  } catch (error) {
    return {
      ok: false,
      type: 'command',
      error: error.message,
      stdout: String(error.stdout ?? '').slice(0, 2000),
      stderr: String(error.stderr ?? '').slice(0, 2000)
    };
  }
}

function normalizeBaseUrl(baseUrl) {
  return String(baseUrl).replace(/\/+$/, '');
}

function clashHeaders(secret) {
  if (!secret) return {};
  return { Authorization: 'Bearer ' + secret };
}

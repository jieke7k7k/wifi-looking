export function diffWifiState(previous, current, timestamp = new Date().toISOString()) {
  if (!previous || !current) return null;
  if (!previous.connected && current.connected) {
    return createEvent('connected', previous, current, timestamp);
  }
  if (previous.connected && !current.connected) {
    return createEvent('disconnected', previous, current, timestamp);
  }
  if (previous.connected && current.connected && !ssidEquals(previous.ssid, current.ssid)) {
    return createEvent('changed', previous, current, timestamp);
  }
  return null;
}

function createEvent(type, previous, current, timestamp) {
  return {
    type,
    previous: { connected: previous.connected, ssid: previous.ssid, timestamp: previous.timestamp },
    current: { connected: current.connected, ssid: current.ssid, timestamp: current.timestamp },
    timestamp
  };
}

function ssidEquals(left, right) {
  return String(left ?? '').trim().toLowerCase() === String(right ?? '').trim().toLowerCase();
}

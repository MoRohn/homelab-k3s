// "● Local connection" (§63) in words, from what the server reports (ConnectionInfo) and whether the live
// stream is up. Never claims more than is known: before the first status it says "Checking…".
import type { ConnectionInfo, Health } from '@/api/contracts.gen';
import type { ConnectionState } from '@/api/sse';

export interface ConnectionSummary {
  health: Health;
  label: string;
  /** Second line: encryption, or why it's not connected. */
  detail: string;
}

export function connectionSummary(info: ConnectionInfo | undefined, live: ConnectionState, reachable: boolean): ConnectionSummary {
  if (!reachable || live === 'offline') return { health: 'offline', label: 'Not connected', detail: 'Labzilla is unreachable from this device' };
  if (!info) return { health: 'unknown', label: 'Checking connection…', detail: '' };
  // The server's snapshot is shared by every viewer, so its `secure` only says whether the configured address is
  // HTTPS. Whether *this* page is encrypted is something only this browser knows.
  const secure = typeof location !== 'undefined' ? location.protocol === 'https:' : info.secure;
  const detail = secure ? 'Encrypted (HTTPS)' : 'Not encrypted: see Trust this device';
  if (live === 'reconnecting') return { health: 'busy', label: 'Reconnecting…', detail };
  return info.local
    ? { health: 'healthy', label: 'Local connection', detail }
    : { health: 'attention', label: 'Remote connection', detail };
}

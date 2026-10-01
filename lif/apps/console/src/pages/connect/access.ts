// How this console is reached from other devices (GET /api/access): the configured public address, the
// alternatives, and whether the connection is trusted. Rarely changes, so it is cached for minutes.
import { get } from '@/api/client';
import type { AccessInfo } from '@/api/contracts.gen';
import { useResource, type Resource } from '@/api/store';

export const ACCESS_KEY = 'access';

export function useAccess(enabled = true): Resource<AccessInfo> {
  return useResource(enabled ? ACCESS_KEY : null, () => get<AccessInfo>('/api/access'), { maxAgeMs: 5 * 60_000 });
}

/**
 * The address a phone should open for `path`. Uses the configured public URL, not location.origin: the
 * browser here may be on a dev port, an IP or localhost that a phone can't reach.
 */
export function mobileUrl(path: string, access: AccessInfo | undefined): string {
  const base = access?.public_url || location.origin;
  try {
    return new URL(path, base).toString();
  } catch {
    return location.origin + path;
  }
}

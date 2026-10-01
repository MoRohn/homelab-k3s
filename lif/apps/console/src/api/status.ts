// The system status everyone shares (Home, TopBar, command bar answers). The SSE `status` event carries
// a full SystemStatus, so Layout primes this cache from the stream and nothing polls.
import { get } from './client';
import type { SystemStatus } from './contracts.gen';
import { useResource, type Resource } from './store';

export const STATUS_KEY = 'system/status';

export function useSystemStatus(): Resource<SystemStatus> {
  return useResource(STATUS_KEY, () => get<SystemStatus>('/api/system/status'), { maxAgeMs: 10_000 });
}

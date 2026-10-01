// The signed-in user and permission checks. The server enforces every permission (brief §3); the UI
// uses can() only to explain and to avoid offering what will be refused (§99: phones get safe ops).
import { ApiError, get, post } from './client';
import type { Perm, User } from './contracts.gen';
import { invalidate, prime, useResource, type Resource } from './store';
import * as sse from './sse';

export const ME_KEY = 'auth/me';

/** /api/auth/me without the automatic login redirect: data is null when signed out, a User when signed in. */
export function useMe(): Resource<User | null> {
  return useResource<User | null>(ME_KEY, async () => {
    try {
      return await get<User>('/api/auth/me', { allow401: true });
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return null;
      throw e;
    }
  }, { maxAgeMs: 60_000 });
}

/**
 * Call after a successful login, setup or pairing approval, BEFORE routing into the app: it replaces
 * the cached "signed out" answer so the auth gate doesn't bounce back to /login.
 */
export function signedIn(user: User): void {
  prime(ME_KEY, user);
}

export function can(user: User | null | undefined, perm: Perm): boolean {
  return !!user && user.perms.includes(perm);
}

export async function logout(): Promise<void> {
  try {
    await post('/api/auth/logout', {}, { allow401: true });
  } finally {
    sse.stop();
    invalidate('');
    location.assign('/login');
  }
}

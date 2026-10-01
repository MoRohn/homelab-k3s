// Small helpers shared by the public pages (Login, Setup, Pair).
import { ApiError, get } from '@/api/client';
import type { User } from '@/api/contracts.gen';
import { signedIn } from '@/api/session';

/**
 * A safe post-login destination: an app path on this origin only. Rejects "//host", "/\host" and
 * absolute URLs (open redirect), and the public pages themselves (loops).
 */
export function safeNext(raw: string | undefined | null): string {
  if (!raw || !raw.startsWith('/') || raw.startsWith('//') || raw.startsWith('/\\')) return '/';
  if (/^\/(login|setup|pair)(\/|\?|#|$)/.test(raw)) return '/';
  return raw;
}

/**
 * After the server set a session cookie (login, setup, pairing approval): load the user and prime the
 * session cache so the auth gate lets us in. Returns null when the server didn't actually sign us in.
 */
export async function confirmSession(): Promise<User | null> {
  try {
    const user = await get<User>('/api/auth/me', { allow401: true });
    signedIn(user);
    return user;
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) return null;
    throw e;
  }
}

/** A friendly default device name from the browser, e.g. "iPhone" — the user can change it. */
export function guessDeviceName(): string {
  const ua = navigator.userAgent;
  const nav = navigator as Navigator & { userAgentData?: { platform?: string; mobile?: boolean } };
  if (/iPhone/.test(ua)) return 'iPhone';
  if (/iPad/.test(ua)) return 'iPad';
  // iPadOS reports a Mac user agent; a touch screen gives it away.
  if (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1) return 'iPad';
  if (/Android/.test(ua)) return /Mobile/.test(ua) ? 'Android phone' : 'Android tablet';
  if (/CrOS/.test(ua)) return 'Chromebook';
  if (/Macintosh|Mac OS X/.test(ua)) return 'Mac';
  if (/Windows/.test(ua)) return 'Windows PC';
  if (/Linux/.test(ua)) return nav.userAgentData?.mobile ? 'Linux phone' : 'Linux PC';
  return 'My device';
}

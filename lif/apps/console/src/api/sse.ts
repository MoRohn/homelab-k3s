// One EventSource('/api/events') per tab, shared by every screen (spec §61: server-driven, not polling).
// - subscribe(type, cb) is typed by contracts EventPayloads.
// - Own reconnect with exponential backoff + jitter: on error the native EventSource is closed so it
//   never retries in parallel with us. A failed reconnect probes /api/auth/me to tell "signed out"
//   (→ /login) from "unreachable" (→ offline banner, §62).
// - start() only after the session is confirmed (app.tsx); stop() on logout.
import { useEffect, useRef } from 'preact/hooks';
import { ApiError, OfflineError, get, loginRedirect, reachable } from './client';
import type { EventPayloads, EventType } from './contracts.gen';
import { observable, useObservable } from './observable';

export type ConnectionState = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'offline';

/** Live-connection state for ConnectionBanner and the "● Local connection" indicator (§62, §63). */
export const connection = observable<ConnectionState>('idle');

// Record (not an array) so adding an EventType to the contracts without listing it here is a type error.
const EVENT_TYPES: Record<EventType, true> = {
  status: true,
  activity: true,
  jobs: true,
  approval: true,
  notification: true,
  thread: true,
  pairing: true,
  model: true,
};

type Listener = (data: unknown) => void;
const listeners = new Map<string, Set<Listener>>();
const reconnectListeners = new Set<() => void>();

let es: EventSource | null = null;
let wanted = false;
let attempt = 0;
let timer: ReturnType<typeof setTimeout> | undefined;

const BACKOFF_MS = [1000, 2000, 4000, 8000, 15000, 30000];

function emit(type: string, raw: string): void {
  const subs = listeners.get(type);
  if (!subs?.size) return;
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return;
  }
  for (const fn of [...subs]) fn(data);
}

function open(): void {
  clearTimeout(timer);
  if (!wanted) return;
  es?.close();
  connection.set(attempt === 0 ? 'connecting' : 'reconnecting');
  const source = new EventSource('/api/events', { withCredentials: true });
  es = source;
  source.onopen = () => {
    const wasReconnect = attempt > 0;
    attempt = 0;
    connection.set('open');
    reachable.set(true);
    if (wasReconnect) for (const fn of [...reconnectListeners]) fn();
  };
  source.onerror = () => {
    source.close();
    if (es === source) es = null;
    void retry();
  };
  for (const type of Object.keys(EVENT_TYPES)) source.addEventListener(type, (e) => emit(type, (e as MessageEvent<string>).data));
}

async function retry(): Promise<void> {
  if (!wanted) return;
  const delay = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)] ?? 30000;
  attempt += 1;
  connection.set(navigator.onLine === false ? 'offline' : 'reconnecting');
  if (attempt >= 2) {
    try {
      await get('/api/auth/me', { allow401: true });
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        stop();
        loginRedirect();
        return;
      }
      if (e instanceof OfflineError) connection.set('offline');
    }
  }
  timer = setTimeout(open, delay * (0.8 + Math.random() * 0.4));
}

/** Connect (idempotent). Call once the user is signed in. */
export function start(): void {
  if (wanted) return;
  wanted = true;
  attempt = 0;
  open();
}

/** Disconnect (logout, or the auth gate lost the session). */
export function stop(): void {
  wanted = false;
  clearTimeout(timer);
  es?.close();
  es = null;
  connection.set('idle');
}

/** Retry now (the banner's Retry button; also when the tab becomes visible again or the network returns). */
export function reconnectNow(): void {
  if (!wanted || connection.get() === 'open') return;
  attempt = Math.max(attempt, 1);
  open();
}

/** Typed subscription; returns an unsubscribe function. */
export function subscribe<K extends EventType>(type: K, cb: (data: EventPayloads[K]) => void): () => void {
  let subs = listeners.get(type);
  if (!subs) listeners.set(type, (subs = new Set()));
  const fn = cb as Listener;
  subs.add(fn);
  return () => void subs.delete(fn);
}

/** Called after a dropped connection comes back, so caches can revalidate what they may have missed. */
export function onReconnect(cb: () => void): () => void {
  reconnectListeners.add(cb);
  return () => void reconnectListeners.delete(cb);
}

/** Hook form of subscribe(); the latest callback is always used without resubscribing. */
export function useEvent<K extends EventType>(type: K, cb: (data: EventPayloads[K]) => void): void {
  const ref = useRef(cb);
  ref.current = cb;
  useEffect(() => subscribe(type, (d) => ref.current(d)), [type]);
}

export function useConnectionState(): ConnectionState {
  return useObservable(connection);
}

/** After the tab slept a while (phone locked, laptop lid): a mobile browser often resumes with a stream that
 *  still reads "open" but delivers nothing. Start a fresh one, which also refetches what may have been missed. */
const STALE_AFTER_HIDDEN_MS = 20_000;
let hiddenAt = 0;

function onVisibility(): void {
  if (document.visibilityState === 'hidden') {
    hiddenAt = Date.now();
    return;
  }
  const slept = hiddenAt > 0 && Date.now() - hiddenAt > STALE_AFTER_HIDDEN_MS;
  hiddenAt = 0;
  if (!wanted) return;
  if (slept && connection.get() === 'open') {
    attempt = Math.max(attempt, 1);       // onopen then counts as a reconnect: caches revalidate
    open();
  } else reconnectNow();
}

if (typeof window !== 'undefined') {
  window.addEventListener('online', reconnectNow);
  window.addEventListener('offline', () => wanted && connection.set('offline'));
  document.addEventListener('visibilitychange', onVisibility);
}

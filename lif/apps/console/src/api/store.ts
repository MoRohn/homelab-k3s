// Tiny server-state cache (spec §60, §89): stale-while-revalidate per key, refreshed by SSE events.
// Not a global store: each screen asks for what it shows; the server stays authoritative.
//
//   const status = useResource('system/status', () => get<SystemStatus>('/api/system/status'),
//                              { refreshOn: ['status'] });
//   invalidate('models/')   // after a mutation: every key starting with 'models/' refetches if in use
import { useCallback, useEffect, useRef, useState } from 'preact/hooks';
import { toHumanError } from './client';
import type { EventType, HumanError } from './contracts.gen';
import { onReconnect, subscribe } from './sse';

export interface Resource<T> {
  data: T | undefined;
  error: HumanError | undefined;
  /** True only while there is nothing to show yet (first load); use for skeletons. */
  loading: boolean;
  /** True while a background refresh runs over data already shown. */
  refreshing: boolean;
  refresh: () => Promise<void>;
  /** Optimistic local update (e.g. after a safe action); the next revalidation wins. */
  mutate: (data: T | ((prev: T | undefined) => T)) => void;
}

export interface ResourceOptions {
  /** SSE event types that make this resource refetch (throttled). */
  refreshOn?: EventType[];
  /** Data younger than this is shown without refetching on mount (default 5 s). */
  maxAgeMs?: number;
  /** Poll interval while mounted — only for data no SSE event covers. Default: none. */
  pollMs?: number;
}

interface Entry {
  data?: unknown;
  error?: HumanError;
  ts: number;
  /** Bumped by invalidate() and local writes: a fetch that started before then answers for the old state. */
  gen: number;
  inflight?: Promise<void>;
  fetcher?: () => Promise<unknown>;
  subs: Set<() => void>;
}

const cache = new Map<string, Entry>();

function entry(key: string): Entry {
  let e = cache.get(key);
  if (!e) cache.set(key, (e = { ts: 0, gen: 0, subs: new Set() }));
  return e;
}

function notify(e: Entry): void {
  for (const fn of [...e.subs]) fn();
}

function revalidate(key: string): Promise<void> {
  const e = entry(key);
  if (e.inflight) return e.inflight;
  const fetcher = e.fetcher;
  if (!fetcher) return Promise.resolve();
  const gen = e.gen;
  let outdated = false;
  e.inflight = fetcher()
    .then(
      (data) => {
        // Invalidated (or written locally) while this was in flight: the answer predates the change.
        if ((outdated = e.gen !== gen)) return;
        e.data = data;
        e.error = undefined;
        e.ts = Date.now();
      },
      (err: unknown) => {
        if ((outdated = e.gen !== gen)) return;
        e.error = toHumanError(err);
      },
    )
    .finally(() => {
      e.inflight = undefined;
      notify(e);
      if (outdated && e.subs.size) void revalidate(key);
    });
  notify(e);
  return e.inflight;
}

/** Refetch every cached key starting with `keyPrefix` that is on screen; drop the rest. */
export function invalidate(keyPrefix: string): void {
  for (const [key, e] of cache) {
    if (!key.startsWith(keyPrefix)) continue;
    e.ts = 0;
    e.gen++;
    // A fetch already in flight is not reused: it re-runs once it settles (revalidate).
    if (e.subs.size && !e.inflight) void revalidate(key);
  }
}

/** Put known-fresh data in the cache (e.g. a mutation response or an SSE payload). */
export function prime<T>(key: string, data: T): void {
  const e = entry(key);
  e.data = data;
  e.error = undefined;
  e.ts = Date.now();
  e.gen++;      // an older fetch still in flight must not overwrite this
  notify(e);
}

export function peek<T>(key: string): T | undefined {
  return cache.get(key)?.data as T | undefined;
}

onReconnect(() => invalidate(''));

/** Stale-while-revalidate resource. key=null skips fetching (e.g. waiting for a route param). */
export function useResource<T>(key: string | null, fetcher: () => Promise<T>, opts: ResourceOptions = {}): Resource<T> {
  const [, force] = useState(0);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  const { refreshOn, maxAgeMs = 5000, pollMs } = opts;
  const events = refreshOn?.join(',') ?? '';

  useEffect(() => {
    if (key === null) return;
    const e = entry(key);
    e.fetcher = () => fetcherRef.current();
    const rerender = () => force((n) => n + 1);
    e.subs.add(rerender);
    if (Date.now() - e.ts > maxAgeMs || e.error) void revalidate(key);
    else rerender();

    let throttle: ReturnType<typeof setTimeout> | undefined;
    const kick = () => {
      if (throttle) return;
      throttle = setTimeout(() => {
        throttle = undefined;
        void revalidate(key);
      }, 400);
    };
    const unsubs = events ? (events.split(',') as EventType[]).map((t) => subscribe(t, kick)) : [];
    const poll = pollMs ? setInterval(() => document.visibilityState === 'visible' && void revalidate(key), pollMs) : undefined;
    return () => {
      e.subs.delete(rerender);
      unsubs.forEach((u) => u());
      clearTimeout(throttle);
      if (poll) clearInterval(poll);
    };
  }, [key, events, maxAgeMs, pollMs]);

  const e = key === null ? undefined : cache.get(key);
  const refresh = useCallback(() => (key === null ? Promise.resolve() : revalidate(key)), [key]);
  const mutate = useCallback(
    (next: T | ((prev: T | undefined) => T)) => {
      if (key === null) return;
      const cur = entry(key);
      const value = typeof next === 'function' ? (next as (p: T | undefined) => T)(cur.data as T | undefined) : next;
      prime(key, value);
    },
    [key],
  );
  return {
    data: e?.data as T | undefined,
    error: e?.data === undefined ? e?.error : undefined,
    loading: key !== null && e?.data === undefined && !e?.error,
    refreshing: !!e?.inflight && e.data !== undefined,
    refresh,
    mutate,
  };
}

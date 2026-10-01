// A tiny observable value + hook. The console has no global store (spec §89): this is only for the
// few process-wide facts every screen shares (connection state, reachability, theme).
import { useEffect, useState } from 'preact/hooks';

export interface Observable<T> {
  get(): T;
  set(value: T): void;
  subscribe(fn: (value: T) => void): () => void;
}

export function observable<T>(initial: T): Observable<T> {
  let value = initial;
  const subs = new Set<(value: T) => void>();
  return {
    get: () => value,
    set(next) {
      if (Object.is(next, value)) return;
      value = next;
      for (const fn of [...subs]) fn(value);
    },
    subscribe(fn) {
      subs.add(fn);
      return () => void subs.delete(fn);
    },
  };
}

/** Re-render when the observable changes. */
export function useObservable<T>(o: Observable<T>): T {
  const [value, setValue] = useState(o.get);
  useEffect(() => {
    setValue(o.get());
    return o.subscribe(setValue);
  }, [o]);
  return value;
}

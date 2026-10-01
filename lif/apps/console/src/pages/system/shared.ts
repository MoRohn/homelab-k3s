// Small helpers shared by the System tabs (and Home's lazy Mobile Gateway).
import type { ComponentType } from 'preact';
import { useEffect, useState } from 'preact/hooks';
import { toHumanError } from '@/api/client';
import type { Health } from '@/api/contracts.gen';
import { toast } from '@/ui';

/**
 * Load a component chunk on first render without Suspense: the caller shows its own skeleton while
 * this returns null. Keeps lazy parts (timeline, Mobile Gateway) out of the route chunk without
 * relying on the router to catch a thrown promise when the part appears mid-route (tab or width change).
 */
export function useLazy<P>(load: () => Promise<ComponentType<P>>): ComponentType<P> | null {
  const [comp, setComp] = useState<{ c: ComponentType<P> } | null>(null);
  useEffect(() => {
    let live = true;
    load().then(
      (c) => live && setComp({ c }),
      () => undefined, // a failed chunk load leaves the skeleton; the shell's offline banner explains
    );
    return () => void (live = false);
  }, []);
  return comp?.c ?? null;
}

/** Worst first, so what needs attention is read first (§2). */
const HEALTH_RANK: Record<Health, number> = { offline: 0, attention: 1, degraded: 2, busy: 3, paused: 4, unknown: 5, healthy: 6 };

export function byHealth<T extends { health: Health }>(items: T[]): T[] {
  return [...items].sort((a, b) => HEALTH_RANK[a.health] - HEALTH_RANK[b.health]);
}

/** Run a mutation; success → toast, failure → human toast (never a status code). Returns whether it worked. */
export async function act(run: () => Promise<unknown>, done: string, body?: string): Promise<boolean> {
  try {
    await run();
    toast({ title: done, body, tone: 'success' });
    return true;
  } catch (e) {
    const err = toHumanError(e);
    toast({ title: err.title, body: [err.impact, err.next_step].filter(Boolean).join(' '), tone: 'error' });
    return false;
  }
}

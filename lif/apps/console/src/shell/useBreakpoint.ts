// Behavioural breakpoints by available width — never the user agent (spec §46, §47).
//   compact < 640 px: phone layout (bottom nav, Mobile Gateway home, tables → cards)
//   medium 640–1024 px: tablet / narrow window (collapsed side nav, touch-friendly)
//   wide > 1024 px: full desktop (expanded side nav, command bar bottom-centre)
import { useEffect, useState } from 'preact/hooks';

export type Breakpoint = 'compact' | 'medium' | 'wide';

const COMPACT = '(max-width: 639.98px)';
const WIDE = '(min-width: 1024.02px)';

export function currentBreakpoint(): Breakpoint {
  if (typeof window === 'undefined' || !window.matchMedia) return 'wide';
  if (window.matchMedia(COMPACT).matches) return 'compact';
  if (window.matchMedia(WIDE).matches) return 'wide';
  return 'medium';
}

export function useBreakpoint(): Breakpoint {
  const [bp, setBp] = useState<Breakpoint>(currentBreakpoint);
  useEffect(() => {
    const queries = [window.matchMedia(COMPACT), window.matchMedia(WIDE)];
    const update = () => setBp(currentBreakpoint());
    queries.forEach((q) => q.addEventListener('change', update));
    update();
    return () => queries.forEach((q) => q.removeEventListener('change', update));
  }, []);
  return bp;
}

/** True when the primary pointer is coarse (touch): bigger targets, no hover-only affordances (§48). */
export function useCoarsePointer(): boolean {
  const [coarse, setCoarse] = useState(() => typeof window !== 'undefined' && window.matchMedia?.('(pointer: coarse)').matches);
  useEffect(() => {
    const q = window.matchMedia('(pointer: coarse)');
    const update = () => setCoarse(q.matches);
    q.addEventListener('change', update);
    return () => q.removeEventListener('change', update);
  }, []);
  return coarse;
}

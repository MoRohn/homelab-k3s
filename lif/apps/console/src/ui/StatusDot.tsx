import type { Health } from '@/api/contracts.gen';
import { cx, HEALTH } from './tone';

export interface StatusDotProps {
  health: Health;
  /** Gentle pulse for live activity (disabled under reduced motion). */
  pulse?: boolean;
  /** Screen-reader text (default: the health label). Set '' when adjacent text already says it. */
  label?: string;
  class?: string;
}

/** The "●" of "● Local AI Ready". Shape differs per state too (ring for paused/unknown) so color is not the only cue. */
export function StatusDot({ health, pulse, label, class: cls }: StatusDotProps) {
  const meta = HEALTH[health];
  const text = label ?? meta.label;
  return (
    <span class={cx('lz-dot', `lz-tone-${meta.tone}`, `lz-dot-${health}`, pulse && 'pulse', cls)} role={text ? 'img' : undefined} aria-label={text || undefined} aria-hidden={text ? undefined : 'true'} />
  );
}

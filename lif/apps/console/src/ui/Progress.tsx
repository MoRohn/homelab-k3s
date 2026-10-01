import { cx, type Tone } from './tone';

export interface ProgressProps {
  /** 0..1; undefined/null = indeterminate (animated only when motion is allowed). */
  value?: number | null;
  /** Accessible label ("Discovery progress"). */
  label: string;
  tone?: Extract<Tone, 'brand' | 'success' | 'warning' | 'danger' | 'info' | 'inactive'>;
  /** Show "62%" next to the bar. */
  showValue?: boolean;
  size?: 'sm' | 'md';
  class?: string;
}

export function Progress({ value, label, tone = 'brand', showValue, size = 'md', class: cls }: ProgressProps) {
  const known = typeof value === 'number' && !Number.isNaN(value);
  const pct = known ? Math.round(Math.min(1, Math.max(0, value)) * 100) : undefined;
  return (
    <div class={cx('lz-progress', `lz-progress-${size}`, `lz-tone-${tone}`, cls)}>
      <div
        class={cx('lz-progress-track', !known && 'indeterminate')}
        role="progressbar"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={pct}
      >
        <div class="lz-progress-fill" style={known ? { width: `${pct}%` } : undefined} />
      </div>
      {showValue && known && <span class="lz-progress-value num">{pct}%</span>}
    </div>
  );
}

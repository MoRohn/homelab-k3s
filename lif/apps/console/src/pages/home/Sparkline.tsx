// A tiny inline-SVG trend line for Home's tiles (§92: no chart library). Gaps (null) stay gaps: no data
// is never drawn as 0. An optional threshold is a dashed line, and the accessible label says the numbers
// in words, so colour is never the only signal (§57).
import { cx } from '@/ui';

export interface SparklineProps {
  values: (number | null)[];
  /** Words for the screen-reader summary, e.g. "Free memory over the last 6 hours". */
  label: string;
  unit: string;
  threshold?: number | null;
  thresholdLabel?: string | null;
  /** "low": values under the threshold are the problem (free memory); "high": values over it. */
  bad?: 'low' | 'high';
  class?: string;
}

const W = 120;
const H = 32;

function fmtv(v: number): string {
  return Math.abs(v) >= 100 ? v.toFixed(0) : Math.abs(v) >= 10 ? v.toFixed(1) : v.toFixed(2).replace(/\.?0+$/, '');
}

export function Sparkline({ values, label, unit, threshold, thresholdLabel, bad = 'low', class: cls }: SparklineProps) {
  const known = values.filter((v): v is number => v != null);
  if (known.length < 2)
    return (
      <span class={cx('lz-spark lz-spark-empty', cls)} role="img" aria-label={`${label}: no data yet`}>
        <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden="true">
          <line x1="0" y1={H - 1} x2={W} y2={H - 1} />
        </svg>
      </span>
    );
  const top = Math.max(...known, threshold ?? 0) * 1.12 || 1;
  const x = (i: number) => (values.length < 2 ? 0 : (i / (values.length - 1)) * W);
  const y = (v: number) => H - 2 - (v / top) * (H - 4);
  // Split into runs of consecutive known values: a missing sample breaks the line.
  const runs: string[] = [];
  let cur: string[] = [];
  values.forEach((v, i) => {
    if (v == null) {
      if (cur.length) runs.push(cur.join(' '));
      cur = [];
    } else cur.push(`${x(i).toFixed(1)},${y(v).toFixed(1)}`);
  });
  if (cur.length) runs.push(cur.join(' '));
  const last = known[known.length - 1] as number;
  const breach = threshold != null && (bad === 'low' ? last < threshold : last > threshold);
  const min = Math.min(...known);
  const max = Math.max(...known);
  const said =
    `${label}: now ${fmtv(last)} ${unit}, range ${fmtv(min)}–${fmtv(max)} ${unit}` +
    (threshold != null ? `; ${breach ? (bad === 'low' ? 'below' : 'above') : 'within'} the ${thresholdLabel ?? `${threshold} ${unit} line`}` : '');
  return (
    <span class={cx('lz-spark', breach && 'lz-spark-breach', cls)} role="img" aria-label={said}>
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden="true">
        {threshold != null && <line class="lz-spark-threshold" x1="0" x2={W} y1={y(threshold)} y2={y(threshold)} />}
        {runs.map((pts, i) =>
          pts.includes(' ') ? (
            <polyline key={i} points={pts} />
          ) : (
            <circle key={i} cx={pts.split(',')[0]} cy={pts.split(',')[1]} r="1.2" />
          ),
        )}
      </svg>
    </span>
  );
}

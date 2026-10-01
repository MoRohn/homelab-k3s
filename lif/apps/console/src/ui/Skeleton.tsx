import { cx } from './tone';

export interface SkeletonProps {
  /** CSS width (default 100%). */
  width?: string;
  /** CSS height of one line (default 1em). */
  height?: string;
  /** Render n stacked lines (last one shorter). */
  lines?: number;
  radius?: string;
  class?: string;
}

/** Placeholder while the first load is in flight (§60). Hidden from assistive tech; pair with aria-busy on the region. */
export function Skeleton({ width = '100%', height = '1em', lines = 1, radius, class: cls }: SkeletonProps) {
  return (
    <span class={cx('lz-skeleton-group', cls)} aria-hidden="true">
      {Array.from({ length: lines }, (_, i) => (
        <span key={i} class="lz-skeleton" style={{ width: lines > 1 && i === lines - 1 ? '60%' : width, height, borderRadius: radius }} />
      ))}
    </span>
  );
}

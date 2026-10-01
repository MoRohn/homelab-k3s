import { useState } from 'preact/hooks';
import type { RouteStep } from '@/api/contracts.gen';
import { Icon, type IconName } from './Icon';
import { cx } from './tone';

export interface RouteTrailProps {
  steps: RouteStep[];
  /** Inline "Auto → Local Fast" (receipts) vs stacked "Auto ↓ Jev ↓ Local Reasoning" (details, §11). */
  layout?: 'inline' | 'stacked';
  /** Inline trail with a toggle that switches to the stacked layout ("compact, expandable", §11). */
  expandable?: boolean;
  class?: string;
}

const KIND_ICON: Record<RouteStep['kind'], IconName> = {
  mode: 'sparkle',
  decision: 'decision',
  model: 'cube',
  external: 'external',
};

const KIND_WORD: Record<RouteStep['kind'], string> = {
  mode: 'Mode',
  decision: 'Decided by',
  model: 'Model',
  external: 'External model',
};

/** How a request was routed (§11). An ordered list, so screen readers hear the hops in order; arrows are
 *  decorative. External hops are flagged in words, not only color. */
export function RouteTrail({ steps, layout = 'inline', expandable, class: cls }: RouteTrailProps) {
  const [expanded, setExpanded] = useState(false);
  if (!steps.length) return null;
  const mode = expandable ? (expanded ? 'stacked' : 'inline') : layout;
  const stacked = mode === 'stacked';
  const trail = (
    <ol role="list" class={cx('lz-route', `lz-route-${mode}`, !expandable && cls)} aria-label="Route">
      {steps.map((s, i) => (
        <li key={`${s.label}-${i}`} class={`lz-route-step kind-${s.kind}`}>
          {i > 0 && <Icon name={stacked ? 'arrow-down' : 'chevron-right'} size={stacked ? 14 : 12} class="lz-route-arrow" />}
          <span class="lz-route-label">
            {stacked && <Icon name={KIND_ICON[s.kind]} size={14} class="lz-route-kind" />}
            {stacked && <span class="lz-route-kindword">{KIND_WORD[s.kind]}: </span>}
            {s.label}
            {s.kind === 'external' && !stacked && <span class="lz-route-ext"> (external)</span>}
          </span>
        </li>
      ))}
    </ol>
  );
  if (!expandable) return trail;
  return (
    <div class={cx('lz-route-wrap', cls)}>
      {trail}
      {steps.length > 1 && (
        <button type="button" class="lz-route-toggle" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>
          <span class="sr-only">{expanded ? 'Collapse route' : 'Expand route'}</span>
          <Icon name={expanded ? 'chevron-up' : 'chevron-down'} size={14} />
        </button>
      )}
    </div>
  );
}

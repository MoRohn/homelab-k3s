import type { ComponentChildren } from 'preact';
import { cx } from './tone';

export interface FactItem {
  label: string;
  value: ComponentChildren;
  /** Optional one-line explanation under the value. */
  hint?: string;
}

export interface FactListProps {
  items: FactItem[];
  /** Two columns on wide screens (default 1). */
  columns?: 1 | 2;
  class?: string;
}

/** Label/value pairs: model detail (§27), command answers, decision "why" rows. Plain numbers, no charts (§56). */
export function FactList({ items, columns = 1, class: cls }: FactListProps) {
  return (
    <dl class={cx('lz-facts', columns === 2 && 'two', cls)}>
      {items.map((f) => (
        <div key={f.label} class="lz-fact">
          <dt>{f.label}</dt>
          <dd>
            {f.value}
            {f.hint && <span class="lz-hint">{f.hint}</span>}
          </dd>
        </div>
      ))}
    </dl>
  );
}

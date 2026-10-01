import { useId } from 'preact/hooks';
import type { ComponentChildren } from 'preact';
import { cx, type Tone } from './tone';

export interface CardProps {
  title?: ComponentChildren;
  subtitle?: ComponentChildren;
  /** Right side of the header (a link, a small button). */
  actions?: ComponentChildren;
  children?: ComponentChildren;
  /** Default true; false for cards that hold a List/Table edge-to-edge. */
  padded?: boolean;
  /** A left accent for cards that need attention; never decoration. */
  tone?: Extract<Tone, 'neutral' | 'warning' | 'danger' | 'info' | 'success'>;
  as?: 'section' | 'article' | 'div';
  /** Heading level for the title (default h2). */
  level?: 2 | 3;
  id?: string;
  class?: string;
}

export function Card({ title, subtitle, actions, children, padded = true, tone = 'neutral', as = 'section', level = 2, id, class: cls }: CardProps) {
  const Tag = as;
  const H = level === 3 ? 'h3' : 'h2';
  // A titled section is named by its heading, so it is a navigable region for assistive technology.
  const hid = useId();
  return (
    <Tag id={id} class={cx('lz-card', tone !== 'neutral' && `lz-card-${tone}`, cls)}
      aria-labelledby={title && Tag !== 'div' ? hid : undefined}>
      {(title || actions) && (
        <header class="lz-card-header">
          <div class="grow">
            {title && (
              <H id={hid} class="lz-card-title">
                {title}
              </H>
            )}
            {subtitle && <p class="lz-card-subtitle">{subtitle}</p>}
          </div>
          {actions && <div class="lz-card-actions">{actions}</div>}
        </header>
      )}
      <div class={padded ? 'lz-card-body' : 'lz-card-body flush'}>{children}</div>
    </Tag>
  );
}

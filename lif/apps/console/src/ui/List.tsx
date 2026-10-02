import type { ComponentChildren } from 'preact';
import { safeHref } from './href';
import { Icon } from './Icon';
import { cx } from './tone';

export interface ListProps {
  children: ComponentChildren;
  /** Hairlines between rows (default true). */
  divided?: boolean;
  'aria-label'?: string;
  class?: string;
}

export function List({ children, divided = true, class: cls, ...aria }: ListProps) {
  return (
    <ul role="list" class={cx('lz-list', divided && 'divided', cls)} aria-label={aria['aria-label']}>
      {children}
    </ul>
  );
}

export interface ListItemProps {
  title: ComponentChildren;
  subtitle?: ComponentChildren;
  /** Left slot: a StatusDot or an Icon. */
  leading?: ComponentChildren;
  /** Right slot: a value, a badge or a small action. */
  trailing?: ComponentChildren;
  /** Secondary right-aligned text (time, size). */
  meta?: ComponentChildren;
  /** Makes the whole row a link (adds a chevron). */
  href?: string;
  onClick?: () => void;
  class?: string;
}

export function ListItem({ title, subtitle, leading, trailing, meta, href: rawHref, onClick, class: cls }: ListItemProps) {
  const href = safeHref(rawHref);
  const inner = (
    <>
      {leading && <span class="lz-li-leading">{leading}</span>}
      <span class="lz-li-main">
        <span class="lz-li-title">{title}</span>
        {subtitle && <span class="lz-li-subtitle">{subtitle}</span>}
      </span>
      {meta && <span class="lz-li-meta num">{meta}</span>}
      {trailing && <span class="lz-li-trailing">{trailing}</span>}
      {(href || onClick) && <Icon name="chevron-right" size={16} class="lz-li-chevron" />}
    </>
  );
  return (
    <li class={cx('lz-li', cls)}>
      {href ? (
        <a class="lz-li-row interactive" href={href}>
          {inner}
        </a>
      ) : onClick ? (
        <button type="button" class="lz-li-row interactive" onClick={onClick}>
          {inner}
        </button>
      ) : (
        <div class="lz-li-row">{inner}</div>
      )}
    </li>
  );
}

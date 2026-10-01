import type { ComponentChildren } from 'preact';
import { Button } from './Button';
import { Icon, type IconName } from './Icon';
import { cx } from './tone';

export interface EmptyAction {
  label: string;
  onClick?: () => void;
  /** Navigate instead of clicking. */
  href?: string;
  icon?: IconName;
}

export interface EmptyStateProps {
  icon?: IconName;
  title: string;
  body?: ComponentChildren;
  /** Every empty state offers a next step (§95): "No agents running [Run an agent]". */
  action?: EmptyAction;
  secondary?: EmptyAction;
  children?: ComponentChildren;
  compact?: boolean;
  class?: string;
}

function ActionButton({ a, primary }: { a: EmptyAction; primary: boolean }) {
  if (a.href)
    return (
      <a class={cx('lz-btn', primary ? 'lz-btn-primary' : 'lz-btn-ghost', 'lz-btn-md')} href={a.href}>
        {a.icon && <Icon name={a.icon} size={18} />}
        <span class="lz-btn-label">{a.label}</span>
      </a>
    );
  return (
    <Button variant={primary ? 'primary' : 'ghost'} icon={a.icon} onClick={a.onClick}>
      {a.label}
    </Button>
  );
}

export function EmptyState({ icon, title, body, action, secondary, children, compact, class: cls }: EmptyStateProps) {
  return (
    <div class={cx('lz-empty', compact && 'compact', cls)}>
      {icon && <Icon name={icon} size={compact ? 22 : 28} class="lz-empty-icon" />}
      <p class="lz-empty-title">{title}</p>
      {body && <p class="lz-empty-body">{body}</p>}
      {children}
      {(action || secondary) && (
        <div class="lz-empty-actions">
          {action && <ActionButton a={action} primary />}
          {secondary && <ActionButton a={secondary} primary={false} />}
        </div>
      )}
    </div>
  );
}

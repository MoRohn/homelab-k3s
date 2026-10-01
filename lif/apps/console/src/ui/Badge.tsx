import type { ComponentChildren } from 'preact';
import { Icon, type IconName } from './Icon';
import { cx, type Tone } from './tone';

export interface BadgeProps {
  tone?: Tone;
  icon?: IconName;
  size?: 'sm' | 'md';
  /** Subtle = tinted background (default); outline = border only, for low-emphasis metadata. */
  appearance?: 'subtle' | 'outline';
  title?: string;
  children: ComponentChildren;
  class?: string;
}

export function Badge({ tone = 'neutral', icon, size = 'md', appearance = 'subtle', title, children, class: cls }: BadgeProps) {
  return (
    <span class={cx('lz-badge', `lz-tone-${tone}`, `lz-badge-${size}`, appearance === 'outline' && 'outline', cls)} title={title}>
      {icon && <Icon name={icon} size={size === 'sm' ? 12 : 14} />}
      {children}
    </span>
  );
}

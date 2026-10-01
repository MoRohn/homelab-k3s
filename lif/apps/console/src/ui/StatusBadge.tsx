import type { Health } from '@/api/contracts.gen';
import { Icon } from './Icon';
import { cx, HEALTH } from './tone';

export interface StatusBadgeProps {
  health: Health;
  /** Override the default word ("Ready", "Standby", "On demand"); the icon still comes from health. */
  label?: string;
  size?: 'sm' | 'md';
  class?: string;
}

/** Health in human words with icon + color (§38, §57): Healthy, Busy, Degraded, Paused, Needs attention, Offline. */
export function StatusBadge({ health, label, size = 'md', class: cls }: StatusBadgeProps) {
  const meta = HEALTH[health];
  return (
    <span class={cx('lz-badge', `lz-tone-${meta.tone}`, `lz-badge-${size}`, cls)}>
      <Icon name={meta.icon} size={size === 'sm' ? 12 : 14} />
      {label ?? meta.label}
    </span>
  );
}

import type { PrivacyChoice, PrivacyUsed } from '@/api/contracts.gen';
import { Badge } from './Badge';
import type { IconName } from './Icon';
import type { Tone } from './tone';

export interface PrivacyBadgeProps {
  /** What happened (receipt) or what is allowed (composer). */
  privacy: PrivacyUsed | PrivacyChoice;
  size?: 'sm' | 'md';
  /** Prefix "Processing:" as on file intake (§20). */
  prefix?: string;
}

const META: Record<PrivacyUsed | PrivacyChoice, { label: string; tone: Tone; icon: IconName }> = {
  local_only: { label: 'Local only', tone: 'success', icon: 'lock' },
  local_jev: { label: 'Local + Jev', tone: 'info', icon: 'shield' },
  allow_jev: { label: 'Local + Jev allowed', tone: 'info', icon: 'shield' },
  external: { label: 'External model used', tone: 'warning', icon: 'external' },
};

/** Per-request privacy indicator (§64): Local only / Local + Jev / External model used. */
export function PrivacyBadge({ privacy, size = 'sm', prefix }: PrivacyBadgeProps) {
  const m = META[privacy];
  return (
    <Badge tone={m.tone} icon={m.icon} size={size} appearance="outline">
      {prefix ? `${prefix} ${m.label}` : m.label}
    </Badge>
  );
}

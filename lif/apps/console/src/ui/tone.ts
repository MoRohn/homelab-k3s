// Shared visual vocabulary: health → tone, icon and words (spec §38, §57). Color is never the only
// signal — every tone ships with an icon and a label.
import type { Health, Severity } from '@/api/contracts.gen';
import type { IconName } from './Icon';

export type Tone = 'neutral' | 'brand' | 'success' | 'warning' | 'danger' | 'info' | 'inactive';

export interface HealthMeta {
  label: string;
  tone: Tone;
  icon: IconName;
}

export const HEALTH: Record<Health, HealthMeta> = {
  healthy: { label: 'Healthy', tone: 'success', icon: 'success' },
  busy: { label: 'Busy', tone: 'warning', icon: 'busy' },
  degraded: { label: 'Degraded', tone: 'warning', icon: 'warning' },
  paused: { label: 'Paused', tone: 'inactive', icon: 'paused' },
  attention: { label: 'Needs attention', tone: 'danger', icon: 'warning' },
  offline: { label: 'Offline', tone: 'danger', icon: 'offline' },
  unknown: { label: 'Unknown', tone: 'inactive', icon: 'unknown' },
};

export const SEVERITY: Record<Severity, { tone: Tone; icon: IconName; label: string }> = {
  info: { tone: 'info', icon: 'info', label: 'Info' },
  success: { tone: 'success', icon: 'success', label: 'Finished' },
  warning: { tone: 'warning', icon: 'warning', label: 'Warning' },
  error: { tone: 'danger', icon: 'error', label: 'Error' },
};

/** Join class names, skipping falsy values. */
export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(' ');
}

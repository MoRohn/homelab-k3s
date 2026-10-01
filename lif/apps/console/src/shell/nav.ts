// Navigation model shared by SideNav, BottomNav and the command palette (spec §5, §45, §50).
// GPU, K3s, Decision Fabric, Hugging Face, Benchmarks and Logs are never top-level: they live inside
// System, Models and Agents and are reachable from the palette/command bar.
import type { IconName } from '@/ui/Icon';

export interface NavItem {
  id: 'home' | 'ask' | 'agents' | 'models' | 'jobs' | 'knowledge' | 'system';
  label: string;
  href: string;
  icon: IconName;
}

export const NAV_ITEMS: NavItem[] = [
  { id: 'home', label: 'Home', href: '/', icon: 'home' },
  { id: 'ask', label: 'Ask', href: '/ask', icon: 'ask' },
  { id: 'agents', label: 'Agents', href: '/agents', icon: 'agents' },
  { id: 'models', label: 'Models', href: '/models', icon: 'models' },
  { id: 'jobs', label: 'Jobs', href: '/jobs', icon: 'jobs' },
  { id: 'knowledge', label: 'Knowledge', href: '/knowledge', icon: 'knowledge' },
  { id: 'system', label: 'System', href: '/system', icon: 'system' },
];

/** Mobile bottom nav (§45): Home, Ask, Agents, More. */
export const BOTTOM_NAV: NavItem['id'][] = ['home', 'ask', 'agents'];

/** Items behind "More" on compact, plus the device/trust pages. */
export const MORE_ITEMS: { label: string; href: string; icon: IconName; hint: string }[] = [
  { label: 'Models', href: '/models', icon: 'models', hint: 'Which model handles each kind of request' },
  { label: 'Jobs', href: '/jobs', icon: 'jobs', hint: 'Batch and background work' },
  { label: 'Knowledge', href: '/knowledge', icon: 'knowledge', hint: 'Decisions, evidence and project memory' },
  { label: 'System', href: '/system', icon: 'system', hint: 'Compute, services, storage and logs' },
  { label: 'Connect a phone', href: '/connect', icon: 'qr', hint: 'Pair a phone or tablet, manage paired devices' },
  { label: 'Trust this device', href: '/trust', icon: 'shield', hint: 'Secure connection, install and voice' },
];

/** Which top-level item a path belongs to ('/models/roles/fast' → 'models'). */
export function activeNav(path: string): NavItem['id'] | null {
  if (path === '/' || path === '') return 'home';
  const first = path.split('/')[1] ?? '';
  return (NAV_ITEMS.find((n) => n.href === `/${first}`)?.id as NavItem['id'] | undefined) ?? null;
}

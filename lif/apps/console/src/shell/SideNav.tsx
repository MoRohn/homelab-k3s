import type { User } from '@/api/contracts.gen';
import { Icon } from '@/ui/Icon';
import { IconButton } from '@/ui/IconButton';
import { StatusDot } from '@/ui/StatusDot';
import { cx } from '@/ui/tone';
import type { ConnectionSummary } from './connection';
import { NAV_ITEMS, activeNav } from './nav';

export interface SideNavProps {
  /** Icons only (medium widths, or the user collapsed it). */
  collapsed: boolean;
  onToggle: () => void;
  /** Current path (for aria-current). */
  current: string;
  /** Pending approvals badge on Agents. */
  approvalsPending?: number;
  /** Footer "● Local connection" (§63); links to Trust this device. */
  connection?: ConnectionSummary;
  user?: User | null;
  onSignOut?: () => void;
}

/** Compact left nav (§5, §44): Home, Ask, Agents, Models, Jobs, Knowledge, System. Logo sparingly (§94). */
export function SideNav({ collapsed, onToggle, current, approvalsPending = 0, connection, user, onSignOut }: SideNavProps) {
  const active = activeNav(current);
  return (
    <nav class={cx('lz-sidenav', collapsed && 'collapsed')} aria-label="Main">
      <a class="lz-sidenav-brand" href="/" aria-label="Labzilla home">
        <img src="/logo/mark.png" alt="" width={28} height={28} decoding="async" />
        {!collapsed && <span>Labzilla</span>}
      </a>
      <ul role="list">
        {NAV_ITEMS.map((n) => (
          <li key={n.id}>
            <a href={n.href} class={cx('lz-sidenav-item', active === n.id && 'is-active')} aria-current={active === n.id ? 'page' : undefined} title={collapsed ? n.label : undefined}>
              <Icon name={n.icon} />
              <span class={collapsed ? 'sr-only' : undefined}>{n.label}</span>
              {n.id === 'agents' && approvalsPending > 0 && (
                <span class="lz-sidenav-badge num">
                  <span aria-hidden="true">{approvalsPending}</span>
                  <span class="sr-only">, {approvalsPending} waiting for approval</span>
                </span>
              )}
            </a>
          </li>
        ))}
      </ul>

      <div class="lz-sidenav-foot">
        {user?.perms.includes('devices.manage') && (
          <a
            href="/connect"
            class={cx('lz-sidenav-item', current.startsWith('/connect') && 'is-active')}
            aria-current={current.startsWith('/connect') ? 'page' : undefined}
            title={collapsed ? 'Connect a phone' : undefined}
          >
            <Icon name="qr" />
            <span class={collapsed ? 'sr-only' : undefined}>Connect a phone</span>
          </a>
        )}
        {connection && (
          <a
            href="/trust"
            class="lz-sidenav-conn"
            aria-label={`${connection.label}${connection.detail ? `. ${connection.detail}` : ''}. Open Trust this device`}
            title={collapsed ? `${connection.label}${connection.detail ? ` · ${connection.detail}` : ''}` : undefined}
          >
            <StatusDot health={connection.health} label="" />
            {!collapsed && (
              <span class="lz-sidenav-conn-text">
                <span>{connection.label}</span>
                {connection.detail && <span class="xsmall faint">{connection.detail}</span>}
              </span>
            )}
          </a>
        )}
        {user && !collapsed && (
          <div class="lz-sidenav-user">
            <Icon name={user.role === 'device' ? 'phone' : 'user'} size={18} />
            <span class="grow truncate small" title={user.device_name ?? user.name}>
              {user.device_name ?? user.name}
            </span>
            {onSignOut && <IconButton icon="logout" label="Sign out" size="sm" variant="ghost" onClick={onSignOut} />}
          </div>
        )}
        <button type="button" class="lz-sidenav-toggle" onClick={onToggle} aria-label={collapsed ? 'Expand navigation' : 'Collapse navigation'} aria-expanded={!collapsed}>
          <Icon name={collapsed ? 'chevron-right' : 'chevron-left'} size={18} />
        </button>
      </div>
    </nav>
  );
}

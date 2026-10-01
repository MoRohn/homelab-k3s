import { Icon } from '@/ui/Icon';
import { cx } from '@/ui/tone';
import { BOTTOM_NAV, NAV_ITEMS, activeNav } from './nav';

export interface BottomNavProps {
  current: string;
  /** Opens the More sheet (Models, Jobs, Knowledge, System, Connect, Trust). */
  onMore: () => void;
  approvalsPending?: number;
}

/** Mobile bottom nav (§45): Home, Ask, Agents, More. */
export function BottomNav({ current, onMore, approvalsPending = 0 }: BottomNavProps) {
  const active = activeNav(current);
  const items = NAV_ITEMS.filter((n) => BOTTOM_NAV.includes(n.id));
  const moreActive = active !== null && !BOTTOM_NAV.includes(active);
  return (
    <nav class="lz-bottomnav" aria-label="Main">
      {items.map((n) => (
        <a key={n.id} href={n.href} class={cx('lz-bottomnav-item', active === n.id && 'is-active')} aria-current={active === n.id ? 'page' : undefined}>
          <span class="lz-bottomnav-icon">
            <Icon name={n.icon} size={22} />
            {n.id === 'agents' && approvalsPending > 0 && <span class="lz-bottomnav-badge" aria-label={`${approvalsPending} waiting for approval`} />}
          </span>
          {n.label}
        </a>
      ))}
      <button type="button" class={cx('lz-bottomnav-item', moreActive && 'is-active')} onClick={onMore} aria-haspopup="dialog">
        <span class="lz-bottomnav-icon">
          <Icon name="more" size={22} />
        </span>
        More
      </button>
    </nav>
  );
}

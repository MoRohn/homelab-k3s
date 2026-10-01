import type { Health } from '@/api/contracts.gen';
import { Icon } from '@/ui/Icon';
import { IconButton } from '@/ui/IconButton';
import { StatusDot } from '@/ui/StatusDot';

export interface TopBarProps {
  /** compact: "Labzilla ●" header (§45); wide/medium: slim bar with connection + notifications. */
  variant: 'compact' | 'wide';
  health?: Health;
  /** "Labzilla is healthy" — announced next to the dot. */
  headline?: string;
  /** "● Local connection" (§63): shown when the server reports a local, direct connection. */
  localConnection?: boolean;
  secure?: boolean;
  notificationCount?: number;
  onOpenNotifications?: () => void;
  onOpenPalette?: () => void;
}

export function TopBar({ variant, health = 'unknown', headline, localConnection, secure, notificationCount = 0, onOpenNotifications, onOpenPalette }: TopBarProps) {
  return (
    <header class={`lz-topbar lz-topbar-${variant}`}>
      {variant === 'compact' ? (
        <a href="/" class="lz-topbar-brand" aria-label={`Labzilla home. ${headline ?? ''}`}>
          <img src="/logo/mark.png" alt="" width={24} height={24} />
          <span>Labzilla</span>
          <StatusDot health={health} label={headline ?? undefined} />
        </a>
      ) : (
        <p class="lz-topbar-status small muted">
          <StatusDot health={health} label="" /> {headline}
        </p>
      )}
      <div class="lz-topbar-actions">
        {localConnection && variant === 'wide' && (
          <span class="lz-topbar-conn small muted" title={secure ? 'Direct, encrypted connection on your network' : 'Direct connection on your network'}>
            <StatusDot health="healthy" label="" /> Local connection
          </span>
        )}
        {localConnection && variant === 'compact' && (
          <a
            href="/trust"
            class="lz-iconbtn lz-topbar-conn-link"
            aria-label={`Local connection${secure ? ', encrypted' : ', not encrypted'}. Open Trust this device`}
            title={secure ? 'Local connection · encrypted' : 'Local connection · not encrypted'}
          >
            <Icon name={secure ? 'lock' : 'wifi'} size={18} />
          </a>
        )}
        {variant === 'wide' && onOpenPalette && <IconButton icon="command" label="Command palette (Ctrl+K)" onClick={onOpenPalette} />}
        {onOpenNotifications && <IconButton icon="bell" label="Notifications" badge={notificationCount} onClick={onOpenNotifications} />}
      </div>
    </header>
  );
}

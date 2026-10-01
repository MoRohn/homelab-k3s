// Notifications: only things that may need the user (§39, §100). The list is the server's
// SystemStatus.notifications (current conditions) plus `notification` SSE events that arrived since that
// status was built. There is no "mark as read" on the server, so nothing here pretends to dismiss.
// Browser (OS-level) notifications are opt-in from an explicit click, only in a secure context, and only
// fire while the tab is hidden: a visible tab already shows the bell count.
import { useEffect, useState } from 'preact/hooks';
import { useLocation } from 'preact-iso/router';
import type { Notification as LzNotification, NotificationKind, SystemStatus } from '@/api/contracts.gen';
import { useEvent } from '@/api/sse';
import { Button } from '@/ui/Button';
import { Drawer } from '@/ui/Drawer';
import { EmptyState } from '@/ui/EmptyState';
import { Icon } from '@/ui/Icon';
import { ago } from '@/ui/format';
import { SEVERITY } from '@/ui/tone';

// Record so a new NotificationKind in the contracts must be consciously allowed here.
const ALLOWED: Record<NotificationKind, true> = {
  fallback: true,
  memory_pressure: true,
  candidate: true,
  approval: true,
  service_down: true,
  blerbz_contention: true,
  job_done: true,
};
const allowed = (n: LzNotification) => (ALLOWED as Record<string, true | undefined>)[n.kind] === true;

type Permission = 'unsupported' | 'insecure' | NotificationPermission;

function browserPermission(): Permission {
  if (typeof window === 'undefined' || !('Notification' in window)) return 'unsupported';
  if (!window.isSecureContext) return 'insecure';
  return window.Notification.permission;
}

function showBrowserNotification(n: LzNotification): void {
  if (browserPermission() !== 'granted' || document.visibilityState !== 'hidden') return;
  try {
    // tag = id: several open tabs (or a repeat event) collapse into one OS notification.
    const note = new window.Notification(n.title, { body: n.body, tag: n.id, icon: '/icons/icon-192.png' });
    note.onclick = () => {
      window.focus();
      if (n.href) location.assign(n.href);
      note.close();
    };
  } catch {
    /* some mobile browsers only allow notifications from a service worker: the in-app bell still works */
  }
}

/** Current notifications for the bell: status list ∪ newer SSE events, newest first, allowed kinds only. */
export function useNotifications(status: SystemStatus | undefined): LzNotification[] {
  const [extra, setExtra] = useState<LzNotification[]>([]);
  const updated = status?.updated_at ?? 0;

  // A fresh status already reflects everything up to its timestamp; keep only events newer than it.
  useEffect(() => setExtra((xs) => xs.filter((x) => x.ts > updated)), [updated]);

  useEvent('notification', (n) => {
    if (!allowed(n)) return;
    setExtra((xs) => [n, ...xs.filter((x) => x.id !== n.id)].slice(0, 20));
    showBrowserNotification(n);
  });

  const base = (status?.notifications ?? []).filter(allowed);
  const ids = new Set(base.map((n) => n.id));
  return [...extra.filter((n) => !ids.has(n.id)), ...base].sort((a, b) => b.ts - a.ts);
}

export interface NotificationsProps {
  open: boolean;
  onClose: () => void;
  items: LzNotification[];
}

/** Only what may need you (§39, §100): fallback active, memory pressure, new candidate, approval, service down. */
export function Notifications({ open, onClose, items }: NotificationsProps) {
  const { route } = useLocation();
  const [perm, setPerm] = useState<Permission>(browserPermission);
  useEffect(() => {
    if (open) setPerm(browserPermission());
  }, [open]);

  const ask = async () => {
    try {
      setPerm(await window.Notification.requestPermission());
    } catch {
      setPerm(browserPermission());
    }
  };

  const footer =
    perm === 'default' ? (
      <Button variant="secondary" icon="bell" block onClick={() => void ask()} subtitle="Only for approvals, failures and finished important work">
        Also notify me when this tab is in the background
      </Button>
    ) : perm === 'granted' ? (
      <p class="xsmall faint">Background notifications are on for this browser.</p>
    ) : perm === 'denied' ? (
      <p class="xsmall faint">Background notifications are blocked in this browser's site settings.</p>
    ) : perm === 'insecure' ? (
      <p class="xsmall faint">
        Background notifications need a trusted HTTPS connection. <a href="/trust" onClick={onClose}>Trust this device</a>
      </p>
    ) : undefined;

  return (
    <Drawer open={open} onClose={onClose} title="Notifications" subtitle="Only things that may need you" footer={footer}>
      {items.length === 0 ? (
        <EmptyState
          icon="bell"
          title="Nothing needs you right now"
          body="Labzilla tells you when a model falls back, memory runs low, a candidate is ready or something needs approval."
          action={{
            label: 'View recent activity',
            icon: 'history',
            onClick: () => {
              onClose();
              route('/system/logs');
            },
          }}
          compact
        />
      ) : (
        <ul role="list" class="lz-list divided">
          {items.map((n) => {
            const s = SEVERITY[n.severity];
            const body = (
              <>
                <Icon name={s.icon} size={18} label={s.label} class={`lz-tone-${s.tone}`} />
                <span class="lz-li-main">
                  <span class="lz-li-title">{n.title}</span>
                  <span class="lz-li-subtitle">{n.body}</span>
                </span>
                <span class="lz-li-meta num">{ago(n.ts)}</span>
              </>
            );
            return (
              <li key={n.id}>
                {n.href ? (
                  <a class="lz-li-row interactive" href={n.href} onClick={onClose}>
                    {body}
                  </a>
                ) : (
                  <div class="lz-li-row">{body}</div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </Drawer>
  );
}

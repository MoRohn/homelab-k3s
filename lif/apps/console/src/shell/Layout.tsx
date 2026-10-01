import type { ComponentChildren } from 'preact';
import { useEffect, useRef, useState } from 'preact/hooks';
import { useLocation } from 'preact-iso/router';
import { reachable } from '@/api/client';
import { useObservable } from '@/api/observable';
import { logout, useMe } from '@/api/session';
import { STATUS_KEY, useSystemStatus } from '@/api/status';
import { invalidate, prime } from '@/api/store';
import { useConnectionState, useEvent } from '@/api/sse';
import { THREADS_KEY, streaming } from '@/pages/ask/state';
import { installAvailable, promptInstall } from '@/pwa';
import { Icon } from '@/ui/Icon';
import { Sheet } from '@/ui/Sheet';
import { StatusDot } from '@/ui/StatusDot';
import { Toaster, toast } from '@/ui/Toast';
import { BottomNav } from './BottomNav';
import { CommandBar } from './CommandBar';
import { CommandPalette } from './CommandPalette';
import { ConnectionBanner } from './ConnectionBanner';
import { Notifications, useNotifications } from './Notifications';
import { SideNav } from './SideNav';
import { ThemeToggle } from './ThemeToggle';
import { TopBar } from './TopBar';
import { runCommand } from './commands';
import { connectionSummary } from './connection';
import { MORE_ITEMS } from './nav';
import { useBreakpoint } from './useBreakpoint';
import './shell.css';

export interface LayoutProps {
  children: ComponentChildren;
  /** Hide the command bar input (pages with their own prompt: Ask, compact Home). Commands still resolve. */
  hideCommandBar?: boolean;
}

const NAV_PREF = 'lz-nav-collapsed';

function readCollapsed(): boolean {
  try {
    return localStorage.getItem(NAV_PREF) === '1';
  } catch {
    return false;
  }
}

function typingIn(t: EventTarget | null): boolean {
  const el = t as HTMLElement | null;
  return !!el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));
}

/**
 * Desktop: NAV | CONTENT with the command bar along the bottom of the content column (§44).
 * Tablet: icon rail, touch-sized controls (§46). Phone: "Labzilla ●" header, content,
 * [Ask Labzilla…] bar, bottom nav Home/Ask/Agents/More (§45). Layout is chosen by available width (§47).
 */
export function Layout({ children, hideCommandBar }: LayoutProps) {
  const bp = useBreakpoint();
  const { path, query, url, route } = useLocation();
  const status = useSystemStatus();
  const me = useMe();
  const live = useConnectionState();
  const ok = useObservable(reachable);
  const canInstall = useObservable(installAvailable);
  const [userCollapsed, setUserCollapsed] = useState(readCollapsed);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [notifOpen, setNotifOpen] = useState(false);
  const [moreOpen, setMoreOpen] = useState(false);
  const commandInput = useRef<HTMLInputElement>(null);

  // The SSE `status` event carries a full SystemStatus: nothing polls for it (§61).
  useEvent('status', (s) => prime(STATUS_KEY, s));

  // §68 "Open on desktop": a conversation answering that this tab didn't start (the phone, usually) is
  // offered once, so moving from phone to desktop needs no trip to History.
  const offered = useRef(new Set<string>());
  useEvent('thread', (t) => {
    if (bp === 'compact' || t.status !== 'streaming' || offered.current.has(t.thread_id)) return;
    if (streaming.get().has(t.thread_id) || path === `/ask/${t.thread_id}`) return;
    offered.current.add(t.thread_id);
    invalidate(THREADS_KEY);
    toast({
      title: 'Conversation started on another device',
      body: 'Open it here to follow the answer and continue.',
      tone: 'info',
      action: { label: 'Open', onClick: () => route(`/ask/${encodeURIComponent(t.thread_id)}`) },
    });
  });
  const notifications = useNotifications(status.data);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && !e.altKey && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setPaletteOpen((o) => !o);
        return;
      }
      // "/" focuses the page's own prompt (Ask, Mobile Gateway) or the command bar — unless typing (§49).
      if (e.key === '/' && !typingIn(e.target) && !e.metaKey && !e.ctrlKey && !e.altKey && !document.querySelector('dialog[open]')) {
        const target = document.querySelector<HTMLElement>('[data-slash-focus]') ?? commandInput.current;
        if (target) {
          e.preventDefault();
          target.focus();
        }
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  // Move focus to the main landmark on navigation so screen readers land on the new page (§86).
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    document.getElementById('main')?.focus({ preventScroll: true });
    window.scrollTo(0, 0);
  }, [path]);

  // Arrival from a just-approved phone pairing (Pair → /?paired=1): say so once, then drop the flag
  // from the URL so a reload or a shared link doesn't repeat it.
  useEffect(() => {
    if (query.paired !== '1') return;
    toast({ title: 'This device is paired', body: 'You can ask Labzilla, approve agent requests and check status from here.', tone: 'success' });
    const rest = new URLSearchParams(url.split('?')[1] ?? '');
    rest.delete('paired');
    const qs = rest.toString();
    route(`${path}${qs ? `?${qs}` : ''}`, true);
  }, [query.paired]);

  const s = status.data;
  const compact = bp === 'compact';
  const collapsed = bp === 'medium' || userCollapsed;
  const conn = connectionSummary(s?.connection, live, ok);
  const signOut = () => void logout();

  const goMore = (href: string) => {
    setMoreOpen(false);
    route(href);
  };

  return (
    <div class={`lz-shell lz-shell-${bp}${collapsed ? ' nav-collapsed' : ''}`}>
      <a class="skip-link" href="#main">
        Skip to content
      </a>
      {!compact && (
        <SideNav
          collapsed={collapsed}
          current={path}
          approvalsPending={s?.approvals_pending}
          connection={conn}
          user={me.data}
          onSignOut={signOut}
          onToggle={() => {
            const next = !userCollapsed;
            setUserCollapsed(next);
            try {
              localStorage.setItem(NAV_PREF, next ? '1' : '0');
            } catch {
              /* convenience only */
            }
          }}
        />
      )}
      <div class="lz-shell-main">
        <ConnectionBanner />
        <TopBar
          variant={compact ? 'compact' : 'wide'}
          health={s?.health}
          headline={s?.headline}
          // §63 on phones: the side nav's footer isn't there, and Mobile Home's hero says it itself.
          localConnection={compact && path !== '/' && conn.label === 'Local connection'}
          secure={conn.detail.startsWith('Encrypted')}
          notificationCount={notifications.length}
          onOpenNotifications={() => setNotifOpen(true)}
          onOpenPalette={() => setPaletteOpen(true)}
        />
        <main id="main" tabIndex={-1} class="lz-content">
          {children}
        </main>
        <CommandBar placement={compact ? 'mobile' : 'desktop'} inputRef={commandInput} hidden={hideCommandBar} />
      </div>
      {compact && <BottomNav current={path} onMore={() => setMoreOpen(true)} approvalsPending={s?.approvals_pending} />}

      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} onCommand={runCommand} />
      <Notifications open={notifOpen} onClose={() => setNotifOpen(false)} items={notifications} />
      <Sheet open={moreOpen} onClose={() => setMoreOpen(false)} title="More">
        <div class="stack">
          <ul role="list" class="lz-list divided">
            {MORE_ITEMS.map((m) => (
              <li key={m.href}>
                <button type="button" class="lz-li-row interactive" onClick={() => goMore(m.href)}>
                  <Icon name={m.icon} />
                  <span class="lz-li-main">
                    <span class="lz-li-title">{m.label}</span>
                    <span class="lz-li-subtitle">{m.hint}</span>
                  </span>
                  <Icon name="chevron-right" size={18} class="lz-li-chevron" />
                </button>
              </li>
            ))}
            {canInstall && (
              <li>
                <button
                  type="button"
                  class="lz-li-row interactive"
                  onClick={() => {
                    setMoreOpen(false);
                    void promptInstall();
                  }}
                >
                  <Icon name="download" />
                  <span class="lz-li-main">
                    <span class="lz-li-title">Install app</span>
                    <span class="lz-li-subtitle">Open Labzilla from your home screen, in its own window</span>
                  </span>
                </button>
              </li>
            )}
          </ul>
          <div class="lz-more-settings stack-sm">
            <p class="section-title">Appearance</p>
            <ThemeToggle />
          </div>
          <div class="lz-more-foot row-between">
            <a href="/trust" class="lz-more-conn small" onClick={() => setMoreOpen(false)}>
              <StatusDot health={conn.health} label="" /> {conn.label}
              {conn.detail && <span class="xsmall faint"> · {conn.detail}</span>}
            </a>
            {me.data && (
              <button type="button" class="lz-more-signout small" onClick={signOut}>
                <Icon name="logout" size={16} /> Sign out
              </button>
            )}
          </div>
        </div>
      </Sheet>
      <Toaster offsetBottom={compact ? 'calc(var(--bottomnav-h) + var(--commandbar-h) + var(--safe-bottom) + 16px)' : undefined} />
    </div>
  );
}

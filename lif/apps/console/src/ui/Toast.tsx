// Toasts: brief confirmation of what just happened ("Batch paused"), announced via one polite live
// region. Call toast() from anywhere; <Toaster/> is mounted once by the shell Layout, before any
// toast can fire, so the live region already exists when content is added (required for announcements).
// Timers pause while the pointer or focus is on a toast so it can be read or acted on (WCAG 2.2.1).
import { Icon } from './Icon';
import { IconButton } from './IconButton';
import { observable, useObservable } from '@/api/observable';
import { SEVERITY } from './tone';
import type { Severity } from '@/api/contracts.gen';

export interface ToastOptions {
  title: string;
  body?: string;
  tone?: Severity;
  /** One follow-up action ("Undo", "View"). */
  action?: { label: string; onClick: () => void };
  /** Auto-dismiss after ms (default 5000; errors 8000; toasts with an action 8000; 0 = sticky). */
  durationMs?: number;
}

interface ToastItem extends ToastOptions {
  id: string;
}

interface Timer {
  handle?: ReturnType<typeof setTimeout>;
  remaining: number;
  startedAt: number;
  /** The countdown runs only while neither is true. */
  hovered: boolean;
  focused: boolean;
}

const MAX_VISIBLE = 4;
const toasts = observable<ToastItem[]>([]);
const timers = new Map<string, Timer>();
let seq = 0;

function arm(id: string): void {
  const t = timers.get(id);
  if (!t || t.handle || t.hovered || t.focused || t.remaining <= 0) return;
  t.startedAt = Date.now();
  t.handle = setTimeout(() => dismissToast(id), t.remaining);
}

/** Record hover/focus on a toast and pause or resume its countdown accordingly. */
function hold(id: string, why: 'hovered' | 'focused', on: boolean): void {
  const t = timers.get(id);
  if (!t) return;
  t[why] = on;
  if (on && t.handle) {
    clearTimeout(t.handle);
    t.handle = undefined;
    t.remaining = Math.max(1000, t.remaining - (Date.now() - t.startedAt));
  } else if (!on) arm(id);
}

export function toast(opts: ToastOptions): string {
  const id = `t${++seq}`;
  const list = [...toasts.get(), { ...opts, id }];
  for (const old of list.slice(0, Math.max(0, list.length - MAX_VISIBLE))) clearTimer(old.id);
  toasts.set(list.slice(-MAX_VISIBLE));
  const ms = opts.durationMs ?? (opts.tone === 'error' || opts.action ? 8000 : 5000);
  if (ms > 0) {
    timers.set(id, { remaining: ms, startedAt: Date.now(), hovered: false, focused: false });
    arm(id);
  }
  return id;
}

function clearTimer(id: string): void {
  const t = timers.get(id);
  if (t?.handle) clearTimeout(t.handle);
  timers.delete(id);
}

export function dismissToast(id: string): void {
  clearTimer(id);
  toasts.set(toasts.get().filter((t) => t.id !== id));
}

export interface ToasterProps {
  /** Lift above the mobile bottom nav + command bar. */
  offsetBottom?: string;
}

export function Toaster({ offsetBottom }: ToasterProps) {
  const items = useObservable(toasts);
  return (
    // A plain container (not a landmark): an always-present empty region would clutter landmark navigation.
    <div class="lz-toaster" style={offsetBottom ? { bottom: offsetBottom } : undefined}>
      <ol role="list" class="lz-toast-list" aria-live="polite" aria-relevant="additions text" aria-label="Status messages">
        {items.map((t) => {
          const s = SEVERITY[t.tone ?? 'info'];
          return (
            <li
              key={t.id}
              class={`lz-toast lz-tone-${s.tone}`}
              onMouseEnter={() => hold(t.id, 'hovered', true)}
              onMouseLeave={() => hold(t.id, 'hovered', false)}
              onFocusIn={() => hold(t.id, 'focused', true)}
              onFocusOut={(e) => {
                if (!(e.currentTarget as HTMLElement).contains(e.relatedTarget as Node | null)) hold(t.id, 'focused', false);
              }}
            >
              <Icon name={s.icon} size={18} label={s.label} />
              <div class="grow">
                <p class="lz-toast-title">{t.title}</p>
                {t.body && <p class="lz-toast-body">{t.body}</p>}
              </div>
              {t.action && (
                <button
                  type="button"
                  class="lz-toast-action"
                  onClick={() => {
                    t.action?.onClick();
                    dismissToast(t.id);
                  }}
                >
                  {t.action.label}
                </button>
              )}
              <IconButton icon="x" label={`Dismiss: ${t.title}`} size="sm" onClick={() => dismissToast(t.id)} />
            </li>
          );
        })}
      </ol>
    </div>
  );
}

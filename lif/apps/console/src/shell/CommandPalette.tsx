import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'preact/hooks';
import { useLocation } from 'preact-iso/router';
import { useObservable } from '@/api/observable';
import { logout, useMe } from '@/api/session';
import { installAvailable, promptInstall } from '@/pwa';
import { Dialog } from '@/ui/Dialog';
import { Icon } from '@/ui/Icon';
import { cx } from '@/ui/tone';
import { NAV_ITEMS } from './nav';
import { PALETTE_COMMANDS, type PaletteCommand } from './commands';
import { THEME_OPTIONS, setTheme, theme } from './theme';

export interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
  /** Resolve free text / command entries through the command bar flow (shows answers and impact). */
  onCommand: (text: string) => void;
}

const FREE_TEXT_ID = 'free-text';

/**
 * Ctrl/Cmd+K palette (§49, §50): the §50 entries, every nav target, Connect a phone, theme, install and
 * sign out. Anything typed can also be sent as-is: it falls through to the command bar's resolver, so a
 * question or "pause batch jobs" works here exactly as it does in the bar.
 * ARIA combobox (input) + listbox (options); focus stays in the input, arrows move the active option.
 */
export function CommandPalette({ open, onClose, onCommand }: CommandPaletteProps) {
  const { route } = useLocation();
  const me = useMe();
  const pref = useObservable(theme);
  const canInstall = useObservable(installAvailable);
  const [q, setQ] = useState('');
  const [idx, setIdx] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLUListElement>(null);

  const entries = useMemo<PaletteCommand[]>(() => {
    const nav: PaletteCommand[] = NAV_ITEMS.map((n) => ({ id: `go-${n.id}`, label: `Go to ${n.label}`, hint: n.href, icon: n.icon, kind: 'navigate', href: n.href }));
    const local: PaletteCommand[] = THEME_OPTIONS.filter((o) => o.value !== pref).map((o) => ({
      id: `theme-${o.value}`,
      label: o.value === 'system' ? 'Follow system theme' : `Use ${o.label.toLowerCase()} theme`,
      hint: 'Appearance on this browser',
      icon: o.icon,
      kind: 'action',
      keywords: 'theme appearance dark light mode color',
      run: () => setTheme(o.value),
    }));
    if (canInstall)
      local.push({ id: 'install', label: 'Install app', hint: 'Open Labzilla in its own window', icon: 'download', kind: 'action', keywords: 'pwa install app home screen', run: () => void promptInstall() });
    if (me.data)
      local.push({ id: 'sign-out', label: 'Sign out', hint: me.data.device_name ?? me.data.name, icon: 'logout', kind: 'action', keywords: 'logout log out session', run: () => void logout() });

    const all = [...PALETTE_COMMANDS, ...nav, ...local];
    const needle = q.trim().toLowerCase();
    if (!needle) return all;
    const words = needle.split(/\s+/);
    const hits = all.filter((c) => {
      const hay = `${c.label} ${c.hint} ${c.keywords ?? ''}`.toLowerCase();
      return words.every((w) => hay.includes(w));
    });
    // Free text always stays one Enter away: it is resolved like the command bar would (§7, §50).
    return [...hits, { id: FREE_TEXT_ID, label: `Ask or run “${q.trim()}”`, hint: 'Labzilla works out what you mean', icon: 'sparkle', kind: 'command', text: q.trim() }];
  }, [q, pref, canInstall, me.data]);

  // Layout effect: reset before the first keystroke can land (a plain effect runs after paint and could
  // undo an arrow key pressed right after opening).
  useLayoutEffect(() => {
    if (!open) return;
    setQ('');
    setIdx(0);
    const t = setTimeout(() => input.current?.focus(), 0);
    return () => clearTimeout(t);
  }, [open]);

  useEffect(() => {
    list.current?.querySelector<HTMLElement>(`[data-idx="${idx}"]`)?.scrollIntoView({ block: 'nearest' });
  }, [idx]);

  const choose = (c: PaletteCommand | undefined) => {
    if (!c) return;
    onClose();
    if (c.kind === 'navigate' && c.href) route(c.href);
    else if (c.kind === 'action') c.run?.();
    else if (c.text) onCommand(c.text);
  };

  const activeId = entries[idx] ? `pal-${entries[idx]?.id}` : undefined;

  return (
    <Dialog open={open} onClose={onClose} title="Command palette" size="md">
      <div class="lz-palette">
        <input
          ref={input}
          class="lz-input"
          type="text"
          role="combobox"
          aria-label="Command, question, or where to go"
          aria-expanded="true"
          aria-autocomplete="list"
          aria-controls="lz-palette-list"
          aria-activedescendant={activeId}
          autoComplete="off"
          spellcheck={false}
          enterKeyHint="go"
          placeholder="Type a command, a question, or where to go…"
          value={q}
          onInput={(e) => {
            setQ((e.currentTarget as HTMLInputElement).value);
            setIdx(0);
          }}
          onKeyDown={(e) => {
            if (e.key === 'ArrowDown') setIdx((i) => Math.min(i + 1, entries.length - 1));
            else if (e.key === 'ArrowUp') setIdx((i) => Math.max(i - 1, 0));
            else if (e.key === 'Home' && !q) setIdx(0);
            else if (e.key === 'End' && !q) setIdx(entries.length - 1);
            else if (e.key === 'Enter') choose(entries[idx]);
            else return;
            e.preventDefault();
          }}
        />
        <ul id="lz-palette-list" ref={list} role="listbox" class="lz-palette-list" aria-label="Commands">
          {entries.map((c, i) => (
            <li
              key={c.id}
              id={`pal-${c.id}`}
              data-idx={i}
              role="option"
              aria-selected={i === idx}
              class={cx('lz-palette-item', i === idx && 'is-active', c.id === FREE_TEXT_ID && 'is-free')}
              // Keep focus in the input (combobox pattern); the click still selects.
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => choose(c)}
              onMouseMove={() => i !== idx && setIdx(i)}
            >
              <Icon name={c.icon} size={18} />
              <span class="grow truncate">{c.label}</span>
              <span class="small muted truncate lz-palette-hint">{c.hint}</span>
            </li>
          ))}
        </ul>
        <p class="xsmall faint lz-palette-keys" aria-hidden="true">
          <kbd>↑</kbd> <kbd>↓</kbd> to move · <kbd>Enter</kbd> to run · <kbd>Esc</kbd> to close
        </p>
      </div>
    </Dialog>
  );
}

import { useObservable } from '@/api/observable';
import { Icon } from '@/ui/Icon';
import { cx } from '@/ui/tone';
import { THEME_OPTIONS, setTheme, theme } from './theme';

/** System / Dark / Light (§52): follows the system unless the user picks one; a per-browser convenience. */
export function ThemeToggle({ compact }: { compact?: boolean }) {
  const current = useObservable(theme);
  // Radio-group keyboard pattern: one tab stop, arrows move the selection.
  const onKeyDown = (e: KeyboardEvent) => {
    const step = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -1 : 0;
    if (!step) return;
    e.preventDefault();
    const i = THEME_OPTIONS.findIndex((o) => o.value === current);
    const next = THEME_OPTIONS[(i + step + THEME_OPTIONS.length) % THEME_OPTIONS.length];
    if (!next) return;
    setTheme(next.value);
    (e.currentTarget as HTMLElement).querySelector<HTMLElement>(`[data-theme-opt="${next.value}"]`)?.focus();
  };
  return (
    <div class={cx('lz-theme-toggle', compact && 'is-compact')} role="radiogroup" aria-label="Theme" onKeyDown={onKeyDown}>
      {THEME_OPTIONS.map((o) => (
        <button
          key={o.value}
          type="button"
          role="radio"
          aria-checked={current === o.value}
          tabIndex={current === o.value ? 0 : -1}
          data-theme-opt={o.value}
          class={cx('lz-theme-opt', current === o.value && 'is-active')}
          title={compact ? `${o.label} theme` : undefined}
          onClick={() => setTheme(o.value)}
        >
          <Icon name={o.icon} size={16} />
          <span class={compact ? 'sr-only' : undefined}>{o.label}</span>
        </button>
      ))}
    </div>
  );
}

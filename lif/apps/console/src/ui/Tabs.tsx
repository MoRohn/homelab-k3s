import type { ComponentChildren } from 'preact';
import { useEffect, useRef, useState } from 'preact/hooks';
import { Icon, type IconName } from './Icon';
import { cx } from './tone';

export interface TabItem<T extends string = string> {
  id: T;
  label: string;
  icon?: IconName;
  badge?: number | string;
}

export interface TabsProps<T extends string = string> {
  tabs: TabItem<T>[];
  value: T;
  onChange: (id: T) => void;
  ariaLabel: string;
  /** id prefix linking tabs to panels: panel id = `${idBase}-panel-${id}`. Unique per page. */
  idBase?: string;
  variant?: 'line' | 'pill';
  class?: string;
}

const tabId = (base: string, id: string) => `${base}-tab-${id}`;
const panelId = (base: string, id: string) => `${base}-panel-${id}`;

/** WAI-ARIA tabs with automatic activation: one tab stop (roving tabindex), Left/Right move and select,
 *  Home/End jump. Render the active panel with <TabPanel idBase value> (or the same ids by hand). */
export function Tabs<T extends string = string>({ tabs, value, onChange, ariaLabel, idBase = 'tabs', variant = 'line', class: cls }: TabsProps<T>) {
  const list = useRef<HTMLDivElement>(null);
  // On narrow screens the strip scrolls sideways with no scrollbar: fade the edge that hides more tabs,
  // so Logs or Settings past the edge are discoverable (§102).
  const [more, setMore] = useState<{ start: boolean; end: boolean }>({ start: false, end: false });
  useEffect(() => {
    const el = list.current;
    if (!el) return;
    const update = () => {
      const start = el.scrollLeft > 1;
      const end = el.scrollLeft + el.clientWidth < el.scrollWidth - 1;
      setMore((m) => (m.start === start && m.end === end ? m : { start, end }));
    };
    // Keep the selected tab in view (a deep link to /system/logs must not hide "Logs" past the edge).
    // Adjust the strip only: scrollIntoView could also scroll the page.
    const active = el.querySelector<HTMLElement>('[aria-selected="true"]');
    if (active) {
      const left = active.offsetLeft - el.offsetLeft;
      if (left < el.scrollLeft) el.scrollLeft = left - 16;
      else if (left + active.offsetWidth > el.scrollLeft + el.clientWidth) el.scrollLeft = left + active.offsetWidth - el.clientWidth + 16;
    }
    update();
    el.addEventListener('scroll', update, { passive: true });
    const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(update) : null;
    ro?.observe(el);
    return () => {
      el.removeEventListener('scroll', update);
      ro?.disconnect();
    };
  }, [tabs.length, value]);
  const focusTab = (i: number) => {
    const t = tabs[(i + tabs.length) % tabs.length];
    if (!t) return;
    if (t.id !== value) onChange(t.id);
    const el = list.current?.querySelector<HTMLButtonElement>(`#${CSS.escape(tabId(idBase, t.id))}`);
    el?.focus();
    el?.scrollIntoView({ block: 'nearest', inline: 'nearest' }); // tab strips scroll sideways on compact
  };
  return (
    <div
      ref={list}
      role="tablist"
      aria-label={ariaLabel}
      aria-orientation="horizontal"
      class={cx('lz-tabs', `lz-tabs-${variant}`, more.start && 'more-start', more.end && 'more-end', cls)}
      onKeyDown={(e) => {
        const i = tabs.findIndex((t) => t.id === value);
        if (e.key === 'ArrowRight') focusTab(i + 1);
        else if (e.key === 'ArrowLeft') focusTab(i - 1);
        else if (e.key === 'Home') focusTab(0);
        else if (e.key === 'End') focusTab(tabs.length - 1);
        else return;
        e.preventDefault();
      }}
    >
      {tabs.map((t) => {
        const selected = t.id === value;
        return (
          <button
            key={t.id}
            type="button"
            role="tab"
            id={tabId(idBase, t.id)}
            aria-selected={selected}
            aria-controls={panelId(idBase, t.id)}
            tabIndex={selected ? 0 : -1}
            class={cx('lz-tab', selected && 'is-active')}
            onClick={() => onChange(t.id)}
          >
            {t.icon && <Icon name={t.icon} size={16} />}
            {t.label}
            {t.badge !== undefined && t.badge !== 0 && t.badge !== '' && (
              <span class="lz-tab-badge num">
                <span class="sr-only">(</span>
                {t.badge}
                <span class="sr-only">)</span>
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}

export interface TabPanelProps {
  /** Same idBase as the Tabs. */
  idBase?: string;
  /** The active tab id. */
  value: string;
  children: ComponentChildren;
  class?: string;
}

/** The panel for the active tab: labelled by its tab and focusable so keyboard users can reach
 *  panel content that starts with plain text. */
export function TabPanel({ idBase = 'tabs', value, children, class: cls }: TabPanelProps) {
  return (
    <div role="tabpanel" id={panelId(idBase, value)} aria-labelledby={tabId(idBase, value)} tabIndex={0} class={cx('lz-tabpanel', cls)}>
      {children}
    </div>
  );
}

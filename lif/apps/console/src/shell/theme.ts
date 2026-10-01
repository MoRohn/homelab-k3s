// Theme preference: follows the system by default (§52); a manual choice is a per-browser convenience
// kept in localStorage (never important state, §70).
import { observable } from '@/api/observable';

export type ThemePref = 'system' | 'dark' | 'light';
const KEY = 'lz-theme';

function read(): ThemePref {
  try {
    const v = localStorage.getItem(KEY);
    return v === 'dark' || v === 'light' ? v : 'system';
  } catch {
    return 'system';
  }
}

export const theme = observable<ThemePref>(read());

export function applyTheme(pref: ThemePref = theme.get()): void {
  const root = document.documentElement;
  if (pref === 'system') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', pref);
  // index.html tints the browser chrome per system scheme; a manual choice pins both tags to the chosen surface.
  const metas = document.querySelectorAll<HTMLMetaElement>('meta[name="theme-color"]');
  metas.forEach((m) => {
    m.dataset.lzDefault ??= m.content;
    m.content = pref === 'system' ? m.dataset.lzDefault : CHROME[pref];
  });
}

const CHROME: Record<'dark' | 'light', string> = { dark: '#07110f', light: '#f4f8f6' };

export function setTheme(pref: ThemePref): void {
  try {
    if (pref === 'system') localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, pref);
  } catch {
    /* storage unavailable: the choice lasts for this page only */
  }
  theme.set(pref);
  applyTheme(pref);
}

/** For the theme toggle in the More sheet and the palette. */
export const THEME_OPTIONS: { value: ThemePref; label: string; icon: 'desktop' | 'moon' | 'sun' }[] = [
  { value: 'system', label: 'System', icon: 'desktop' },
  { value: 'dark', label: 'Dark', icon: 'moon' },
  { value: 'light', label: 'Light', icon: 'sun' },
];

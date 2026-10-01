// Display density (§54): normal by default, compact on request; never dense by default. Like the theme,
// it is a per-browser convenience in localStorage, not important state (§70).
import { observable } from '@/api/observable';

export type DensityPref = 'normal' | 'compact';
const KEY = 'lz-density';

function read(): DensityPref {
  try {
    return localStorage.getItem(KEY) === 'compact' ? 'compact' : 'normal';
  } catch {
    return 'normal';
  }
}

export const density = observable<DensityPref>(read());

/** Put the density class on <html>; tables keep their own compact density either way. */
export function applyDensity(pref: DensityPref = density.get()): void {
  if (typeof document === 'undefined') return;
  const cl = document.documentElement.classList;
  cl.toggle('density-compact', pref === 'compact');
  cl.toggle('density-normal', pref === 'normal');
}

export function setDensity(pref: DensityPref): void {
  try {
    if (pref === 'normal') localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, pref);
  } catch {
    /* storage unavailable: the choice lasts for this page only */
  }
  density.set(pref);
  applyDensity(pref);
}

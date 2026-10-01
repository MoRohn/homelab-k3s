// Display formatting shared by every screen: plain numbers, honest dashes for unknowns (§56).
// Timestamps from the API are Unix epoch seconds.

export const DASH = '—';

export function num(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined || Number.isNaN(v)) return DASH;
  return v.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits });
}

/** 0.42 → "42%"; pass pct=true when the value is already 0..100. */
export function percent(v: number | null | undefined, opts: { fromPct?: boolean; digits?: number } = {}): string {
  if (v === null || v === undefined || Number.isNaN(v)) return DASH;
  return `${num(opts.fromPct ? v : v * 100, opts.digits ?? 0)}%`;
}

export function gb(v: number | null | undefined, digits = 0): string {
  return v === null || v === undefined ? DASH : `${num(v, digits)} GB`;
}

export function ms(v: number | null | undefined): string {
  if (v === null || v === undefined) return DASH;
  return v >= 1000 ? `${num(v / 1000, 1)} sec` : `${num(v)} ms`;
}

/** Seconds → "2 min", "1 h 5 min". */
export function duration(sec: number | null | undefined): string {
  if (sec === null || sec === undefined) return DASH;
  if (sec < 60) return `${Math.max(1, Math.round(sec))} sec`;
  const m = Math.round(sec / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  return m % 60 ? `${h} h ${m % 60} min` : `${h} h`;
}

/** Epoch seconds → "just now", "5 min ago", "yesterday 14:02", "3 Sep". */
export function ago(ts: number | null | undefined, now = Date.now() / 1000): string {
  if (!ts) return DASH;
  const d = now - ts;
  if (d < 45) return 'just now';
  if (d < 3600) return `${Math.round(d / 60)} min ago`;
  if (d < 6 * 3600) return `${Math.round(d / 3600)} h ago`;
  const date = new Date(ts * 1000);
  const today = new Date(now * 1000);
  const yesterday = new Date((now - 86400) * 1000);
  if (date.toDateString() === today.toDateString()) return clock(ts);
  if (date.toDateString() === yesterday.toDateString()) return `yesterday ${clock(ts)}`;
  return date.toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
}

/** Epoch seconds → "10:32". */
export function clock(ts: number | null | undefined): string {
  if (!ts) return DASH;
  return new Date(ts * 1000).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}

/** Epoch seconds → ISO string for <time dateTime>. */
export function iso(ts: number | null | undefined): string | undefined {
  return ts ? new Date(ts * 1000).toISOString() : undefined;
}

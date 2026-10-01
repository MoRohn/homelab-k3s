// Shared helpers for the console e2e specs.
import type { APIRequestContext, Page } from 'playwright/test';
import { expect } from 'playwright/test';
import { FAKE } from './env';

export type Scenario = 'healthy' | 'fallback' | 'blerbz-busy' | 'offline';

/** Switch (and reset) the fake world. Posting the same name again rebuilds it from scratch. */
export async function setScenario(name: Scenario): Promise<void> {
  const r = await fetch(`${FAKE}/__scenario`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ name }) });
  if (!r.ok) throw new Error(`scenario ${name}: ${r.status}`);
}

/**
 * Wait until the page shows real content: a top-level heading in <main>, nothing marked busy and no
 * skeletons. Never waits for network idle: /api/events (SSE) stays open for as long as the page lives.
 */
export async function waitReady(page: Page, timeout = 15_000): Promise<void> {
  await expect(page.locator('main h1').first()).toBeAttached({ timeout });
  await expect(page.locator('main [aria-busy="true"], main.lz-content[aria-busy="true"]')).toHaveCount(0, { timeout });
  await expect(page.locator('main .lz-skeleton:visible')).toHaveCount(0, { timeout });
  await page.evaluate(() => document.fonts.ready.then(() => undefined));
}

/** Text that must never reach the main UI (§38, §62, §81, §91). */
export const RAW_TERMS = /\bHTTP[ /]?\d{3}\b|\b50[0-4] (Service|Bad|Gateway|Internal)|Service Unavailable|Internal Server Error|CrashLoopBackOff|ImagePullBackOff|\bundefined\b|\[object Object\]|\bNaN\b|Traceback/;

/** Authenticated mutating call (CSRF header from the lz_csrf cookie + same-origin Origin). */
export async function apiPost(page: Page, path: string, body: unknown) {
  const base = new URL(page.url()).origin;
  const csrf = (await page.context().cookies()).find((c) => c.name === 'lz_csrf')?.value ?? '';
  return page.request.post(`${base}${path}`, { data: body, headers: { 'X-Labzilla-CSRF': csrf, Origin: base } });
}

export interface Target {
  name: string;
  path: string;
}

/** Every page the responsive and a11y specs visit; dynamic ids come from the API. */
export async function targets(request: APIRequestContext, baseURL: string): Promise<Target[]> {
  const cands = (await (await request.get(`${baseURL}/api/models/candidates`)).json()) as { deployment: { id: string } }[];
  const kh = (await (await request.get(`${baseURL}/api/knowledge`)).json()) as { decisions?: { key: string }[]; recent_changes?: { key: string }[] };
  const candidate = cands[0]?.deployment.id;
  const knowledgeKey = kh.decisions?.[0]?.key ?? kh.recent_changes?.[0]?.key;
  if (!candidate) throw new Error('no model candidate in the fake world');
  if (!knowledgeKey) throw new Error('no knowledge object in the bundled knowledge');
  const list: Target[] = [
    { name: 'home', path: '/' },
    { name: 'ask', path: '/ask' },
    { name: 'agents', path: '/agents' },
    { name: 'models', path: '/models' },
    { name: 'discovery', path: '/models/discovery' },
    { name: 'candidate', path: `/models/candidates/${encodeURIComponent(candidate)}` },
    { name: 'jobs', path: '/jobs' },
    { name: 'knowledge', path: '/knowledge' },
    { name: 'knowledge-object', path: `/knowledge/o/${knowledgeKey.split('/').map(encodeURIComponent).join('/')}` },
  ];
  for (const tab of ['compute', 'services', 'storage', 'network', 'logs', 'settings']) list.push({ name: `system-${tab}`, path: `/system/${tab}` });
  list.push({ name: 'connect', path: '/connect' }, { name: 'trust', path: '/trust' });
  return list;
}

// README screenshots (assets/screenshots/*.png) from the dev harness, never from the real machine: fake
// upstreams, demo prompts with curated answers (fake_upstreams.DEMO_ANSWERS), 2× pixel density, dark theme.
//
//   e2e/run_dev.sh                                         # fresh DB + fake upstreams
//   npx playwright test -c e2e/playwright.config.ts --project=setup   # creates the admin session
//   node e2e/readme_shots.mjs                              # writes ../../../../assets/screenshots/
import { chromium } from 'playwright';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const BASE = process.env.E2E_BASE_URL ?? 'http://127.0.0.1:8090';
const STATE = path.join(process.env.E2E_OUT ?? path.join(os.tmpdir(), 'labzilla-console-e2e'), 'admin-state.json');
const OUT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../../assets/screenshots');
const VIEW = { width: 1440, height: 1240 };

const PROMPTS = [
  'Explain unified memory on the DGX Spark in two sentences',
  'Write a Python function that retries an HTTP GET with exponential backoff',
  'Draft a two-line status update about the new Home cockpit',
  'How does Labzilla keep my conversations private?',
];

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: VIEW, deviceScaleFactor: 2, colorScheme: 'dark', storageState: STATE });
const page = await ctx.newPage();
await page.goto(`${BASE}/`);

// A calm Home: answer the fake's demo review the way the console does (session cookie + CSRF + origin).
const csrf = (await ctx.cookies()).find((c) => c.name === 'lz_csrf')?.value ?? '';
const headers = { 'X-Labzilla-CSRF': csrf, Origin: BASE, 'Content-Type': 'application/json' };
for (const a of (await (await ctx.request.get(`${BASE}/api/approvals`)).json()).filter((x) => x.status === 'pending'))
  await ctx.request.post(`${BASE}/api/approvals/${encodeURIComponent(a.id)}`, { headers, data: { answer: a.options?.[0]?.value ?? 'yes' } });

for (const q of PROMPTS) {
  await page.goto(`${BASE}/ask?q=${encodeURIComponent(q)}&send=1`);
  await page.locator('.ask-answer').last().getByText(/Answered|Details|Local/).first().waitFor({ timeout: 60_000 });
  await page.waitForFunction(() => !document.querySelector('[aria-busy="true"].ask-answer'), null, { timeout: 60_000 });
}

await page.goto(`${BASE}/`);
await page.locator('.lz-tile').first().waitFor();
await page.waitForTimeout(6000);                 // trends, conversations and the status snapshot settle
// Crop just above the fixed command bar, so it never covers a half-visible row.
const bar = await page.locator('.lz-commandbar').boundingBox();
await page.screenshot({ path: path.join(OUT, 'home.png'), clip: { x: 0, y: 0, width: VIEW.width, height: Math.round((bar?.y ?? VIEW.height) - 12) } });

const threads = await (await ctx.request.get(`${BASE}/api/ai/threads`)).json();
const privacy = threads.find((t) => t.title.startsWith('How does Labzilla keep'));
await page.goto(`${BASE}/ask/${encodeURIComponent(privacy.id)}`);
await page.locator('.ask-answer').last().waitFor();
await page.waitForTimeout(1500);
// Crop below the composer: the empty page under it adds nothing.
const composer = await page.locator('.ask-composer').boundingBox();
await page.screenshot({ path: path.join(OUT, 'ask.png'), clip: { x: 0, y: 0, width: VIEW.width, height: Math.min(VIEW.height, Math.round((composer?.y ?? 0) + (composer?.height ?? 900) + 32)) } });

await browser.close();
console.log(`wrote ${OUT}/home.png and ask.png`);

// §44–47, §101–102: every page at phone, tablet and desktop sizes in both color schemes. No horizontal
// page scroll, a prompt is always one tap away, and the nav pattern follows the available width.
// Screenshots go to E2E_SHOTS (outside the repo) as <width>-<scheme>-<page>.png.
import fs from 'node:fs';
import path from 'node:path';
import { expect, test, type Page } from 'playwright/test';
import { SHOTS } from './env';
import { setScenario, targets, waitReady, type Target } from './helpers';

const SIZES = [
  [360, 740],
  [390, 844],
  [768, 1024],
  [1024, 768],
  [1280, 800],
  [1440, 900],
  [1920, 1080],
] as const;
const SCHEMES = ['dark', 'light'] as const;

/** Behavioural breakpoints (useBreakpoint): compact < 640, medium 640–1024, wide > 1024. */
function breakpoint(width: number): 'compact' | 'medium' | 'wide' {
  return width < 640 ? 'compact' : width <= 1024 ? 'medium' : 'wide';
}

let pages: Target[] = [];
test.beforeAll(async ({ request, baseURL }) => {
  await setScenario('healthy');
  pages = await targets(request, baseURL!);
  fs.mkdirSync(SHOTS, { recursive: true });
});

async function checkPage(page: Page, width: number, t: Target) {
  const bp = breakpoint(width);
  await page.goto(t.path);
  await waitReady(page);
  expect.soft(page.url(), `${t.name}: stayed on the page (not bounced to login/404)`).toContain(t.path.split('?')[0]!.split('/').slice(0, 3).join('/'));

  // §102: no horizontal page scroll.
  const { scrollWidth, innerWidth } = await page.evaluate(() => ({ scrollWidth: document.documentElement.scrollWidth, innerWidth: window.innerWidth }));
  expect.soft(scrollWidth, `${t.name}@${width}: page scrolls horizontally`).toBeLessThanOrEqual(innerWidth);

  // Nothing clipped sideways inside the page either (an overflow:auto box hides the overflow from the check above).
  // Code blocks may scroll, as may the tab strip on narrow screens (it shows a scroll affordance).
  const sideways = await page.evaluate(() =>
    [...document.querySelectorAll<HTMLElement>('main *')]
      .filter((el) => {
        if (el.closest('pre, code, .lz-codeblock, [role="tablist"]')) return false;
        const ox = getComputedStyle(el).overflowX;
        return (ox === 'auto' || ox === 'scroll') && el.scrollWidth > el.clientWidth + 1 && el.offsetParent !== null;
      })
      .map((el) => `${el.tagName.toLowerCase()}.${[...el.classList].join('.')} (${el.scrollWidth}>${el.clientWidth})`),
  );
  expect.soft(sideways, `${t.name}@${width}: content scrolls sideways inside the page`).toEqual([]);

  // §7: the command bar (or the page's own prompt on Ask and the phone home) is on screen.
  const prompt = page.locator('.lz-commandbar:not(.is-hidden) input, [data-slash-focus]:visible').first();
  await expect.soft(prompt, `${t.name}@${width}: no prompt/command bar on screen`).toBeInViewport();

  // A selected tab is never scrolled out of its strip (deep links like /system/logs).
  const selectedTab = page.locator('[role="tab"][aria-selected="true"]');
  if (await selectedTab.count()) await expect.soft(selectedTab.first(), `${t.name}@${width}: selected tab is off-screen`).toBeInViewport({ ratio: 0.9 });

  // §44–47: nav pattern by width.
  if (bp === 'compact') {
    await expect.soft(page.locator('.lz-bottomnav'), `${t.name}@${width}: bottom nav`).toBeInViewport();
    await expect.soft(page.locator('.lz-sidenav'), `${t.name}@${width}: no side nav on phones`).toHaveCount(0);
  } else {
    await expect.soft(page.locator('.lz-bottomnav'), `${t.name}@${width}: no bottom nav`).toHaveCount(0);
    const nav = page.locator('.lz-sidenav');
    await expect.soft(nav, `${t.name}@${width}: side nav`).toBeVisible();
    if (bp === 'medium') await expect.soft(nav, `${t.name}@${width}: icon rail on tablets`).toHaveClass(/\bcollapsed\b/);
    else await expect.soft(nav, `${t.name}@${width}: full side nav on desktop`).not.toHaveClass(/\bcollapsed\b/);
  }
}

for (const [width, height] of SIZES) {
  for (const scheme of SCHEMES) {
    test.describe(`${width}x${height} ${scheme}`, () => {
      test.use({ viewport: { width, height }, colorScheme: scheme, hasTouch: width < 1024, isMobile: false });
      test(`every page fits ${width}x${height} (${scheme})`, async ({ page }) => {
        test.setTimeout(240_000);
        for (const t of pages) {
          await test.step(t.name, async () => {
            await checkPage(page, width, t);
            await page.screenshot({ path: path.join(SHOTS, `${width}-${scheme}-${t.name}.png`), fullPage: true });
          });
        }
      });
    });
  }
}

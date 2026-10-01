// §86: axe (WCAG 2.0/2.1 A + AA) on every page at phone and desktop width in both schemes, plus the
// keyboard contract (§49): skip link, Tab reaches nav and command bar, Ctrl+K palette with arrows and
// Enter, "/" focuses the prompt, Escape closes dialogs and focus returns. And §59: reduced motion stops animation.
import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from 'playwright/test';
import { apiPost, setScenario, targets, waitReady, type Target } from './helpers';

let pages: Target[] = [];
test.beforeAll(async ({ request, baseURL }) => {
  await setScenario('healthy');
  pages = await targets(request, baseURL!);
});

async function seriousViolations(page: Page) {
  const r = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze();
  return r.violations
    .filter((v) => v.impact === 'serious' || v.impact === 'critical')
    .map((v) => `${v.id} (${v.impact}): ${v.nodes.length}× e.g. ${v.nodes.slice(0, 3).map((n) => n.target.join(' ')).join(' | ')} — ${v.nodes[0]?.failureSummary?.split('\n').slice(0, 2).join(' ')}`);
}

for (const [width, height] of [
  [390, 844],
  [1440, 900],
] as const) {
  for (const scheme of ['dark', 'light'] as const) {
    test.describe(`axe ${width} ${scheme}`, () => {
      test.use({ viewport: { width, height }, colorScheme: scheme, hasTouch: width < 1024 });
      test(`no serious or critical axe violations at ${width} (${scheme})`, async ({ page }) => {
        test.setTimeout(240_000);
        for (const t of pages) {
          await test.step(t.name, async () => {
            await page.goto(t.path);
            await waitReady(page);
            expect.soft(await seriousViolations(page), `${t.name}@${width} ${scheme}`).toEqual([]);
          });
        }
      });
    });
  }
}

test.describe('Ask history', () => {
  /** A few conversations so the panel has groups, rows and the search box. */
  async function seed(page: Page): Promise<string> {
    await page.goto('/ask');
    await waitReady(page);
    const tag = `k${Date.now().toString(36)}`;          // the database outlives one test: keep rows distinguishable
    const titles = ['GPU memory notes', `Parser review ${tag}`, 'Weekend plan', 'Lighthouse story'];
    for (const title of titles) expect((await apiPost(page, '/api/ai/threads', { title })).ok()).toBe(true);
    await page.reload();
    await waitReady(page);
    return tag;
  }

  for (const scheme of ['dark', 'light'] as const) {
    test(`panel and sheet pass axe (${scheme})`, async ({ page }) => {
      await page.emulateMedia({ colorScheme: scheme });
      await page.setViewportSize({ width: 1440, height: 900 });
      await seed(page);
      const panel = page.getByRole('complementary', { name: 'History' });
      await expect(panel.locator('a.ask-hrow-link').first()).toBeVisible();
      await panel.locator('a.ask-hrow-link').first().hover();
      expect.soft(await seriousViolations(page), `ask history panel ${scheme}`).toEqual([]);
      await page.setViewportSize({ width: 390, height: 844 });
      await page.getByRole('button', { name: 'History' }).click();
      const sheet = page.getByRole('dialog', { name: 'History' });
      await expect(sheet.locator('a.ask-hrow-link').first()).toBeVisible();
      expect.soft(await seriousViolations(page), `ask history sheet ${scheme}`).toEqual([]);
    });
  }

  test('keyboard: arrows move between conversations, search filters, the panel collapses and comes back', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    const tag = await seed(page);
    // Ask puts the cursor in the prompt once it has loaded; start from there, as a person would.
    await expect(page.locator('[data-slash-focus]')).toBeFocused();
    const panel = page.getByRole('complementary', { name: 'History' });
    const rows = panel.locator('a.ask-hrow-link');
    await rows.first().focus();
    await page.keyboard.press('ArrowDown');
    await expect(rows.nth(1)).toBeFocused();
    await page.keyboard.press('End');
    await expect(rows.last()).toBeFocused();
    await page.keyboard.press('Home');
    await expect(rows.first()).toBeFocused();
    const search = panel.getByRole('searchbox', { name: 'Search conversations' });
    await search.fill(tag);
    await expect(rows).toHaveCount(1);
    await search.press('ArrowDown');
    await expect(rows.first()).toBeFocused();
    await expect(rows.first()).toContainText('Parser review');
    await page.keyboard.press('Enter');
    await expect(page).toHaveURL(/\/ask\/th_/);
    await expect(rows.first()).toHaveAttribute('aria-current', 'page');
    await search.fill('');
    // Collapse: the header offers History again, with its state.
    await panel.getByRole('button', { name: 'Hide history' }).click();
    await expect(panel).toHaveCount(0);
    const show = page.getByRole('button', { name: 'History' });
    await expect(show).toHaveAttribute('aria-expanded', 'false');
    await show.click();
    await expect(page.getByRole('complementary', { name: 'History' })).toBeVisible();
  });
});

test.describe('keyboard (desktop)', () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test('skip link is the first stop and moves focus to the content', async ({ page }) => {
    await page.goto('/models');
    await waitReady(page);
    await page.locator('body').focus();
    await page.keyboard.press('Tab');
    const skip = page.getByRole('link', { name: 'Skip to content' });
    await expect(skip).toBeFocused();
    await expect(skip).toBeInViewport();
    await page.keyboard.press('Enter');
    await expect(page.locator('#main')).toBeFocused();
    // The next Tab continues inside the content, not back in the nav.
    await page.keyboard.press('Tab');
    expect(await page.evaluate(() => !!document.activeElement?.closest('#main'))).toBe(true);
  });

  test('Tab reaches every nav item and the command bar', async ({ page }) => {
    await page.goto('/jobs');
    await waitReady(page);
    await page.locator('body').focus();
    const seen = new Set<string>();
    for (let i = 0; i < 120 && !seen.has('commandbar'); i++) {
      await page.keyboard.press('Tab');
      const where = await page.evaluate(() => {
        const el = document.activeElement as HTMLElement | null;
        if (!el) return '';
        if (el.closest('.lz-commandbar') && el.tagName === 'INPUT') return 'commandbar';
        if (el.closest('.lz-sidenav') && el.tagName === 'A') return `nav:${el.getAttribute('href')}`;
        return '';
      });
      if (where) seen.add(where);
    }
    for (const href of ['/', '/ask', '/agents', '/models', '/jobs', '/knowledge', '/system']) expect(seen, `nav ${href}`).toContain(`nav:${href}`);
    expect(seen).toContain('commandbar');
  });

  test('Ctrl+K opens the palette; arrows and Enter run an entry', async ({ page }) => {
    await page.goto('/');
    await waitReady(page);
    await page.keyboard.press('Control+k');
    const dialog = page.getByRole('dialog', { name: 'Command palette' });
    await expect(dialog).toBeVisible();
    const input = page.getByRole('combobox', { name: /Command, question/ });
    await expect(input).toBeFocused();
    // Arrows move the active option (combobox + listbox with aria-activedescendant).
    const active = page.locator('[role="option"][aria-selected="true"]');
    const first = await active.textContent();
    await page.keyboard.press('ArrowDown');
    await expect(active).not.toHaveText(first!);
    await expect(input).toHaveAttribute('aria-activedescendant', (await active.getAttribute('id'))!);
    await page.keyboard.press('ArrowUp');
    await expect(active).toHaveText(first!);
    // Filter, move down to the free-text fallback and back up, then Enter runs the entry.
    await input.fill('go to jobs');
    await expect(page.getByRole('option')).toHaveCount(2);
    await expect(active).toContainText('Go to Jobs');
    await page.keyboard.press('ArrowDown');
    await expect(active).toContainText('Ask or run');
    await page.keyboard.press('ArrowUp');
    await expect(active).toContainText('Go to Jobs');
    await page.keyboard.press('Enter');
    await expect(dialog).toBeHidden();
    await expect(page).toHaveURL(/\/jobs$/);
    // Ctrl+K toggles: opening again and Escape closes it.
    await page.keyboard.press('Control+k');
    await expect(dialog).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(dialog).toBeHidden();
  });

  test('"/" focuses the command bar, or the page\'s own prompt', async ({ page }) => {
    await page.goto('/jobs');
    await waitReady(page);
    await page.locator('#main').focus();
    await page.keyboard.press('/');
    await expect(page.locator('.lz-commandbar input')).toBeFocused();
    await expect(page.locator('.lz-commandbar input')).toHaveValue('');
    await page.goto('/ask');
    await waitReady(page);
    await page.locator('#main').focus();
    await page.keyboard.press('/');
    await expect(page.locator('[data-slash-focus]')).toBeFocused();
  });

  test('Escape closes dialogs and focus returns to the opener', async ({ page }) => {
    await page.goto('/');
    await waitReady(page);
    for (const name of [/^Notifications/, /^Command palette/]) {
      const opener = page.getByRole('button', { name }).first();
      await opener.focus();
      await page.keyboard.press('Enter');
      const dialog = page.locator('dialog[open]');
      await expect(dialog).toBeVisible();
      expect(await page.evaluate(() => !!document.activeElement?.closest('dialog[open]')), 'focus moves into the dialog').toBe(true);
      await page.keyboard.press('Escape');
      await expect(dialog).toHaveCount(0);
      await expect(opener).toBeFocused();
    }
  });
});

test.describe('reduced motion', () => {
  /** Animations that keep moving: running, and longer than a blink or repeating forever. */
  const moving = (page: Page) =>
    page.evaluate(() =>
      document
        .getAnimations()
        .filter((a) => {
          const t = a.effect?.getComputedTiming();
          return a.playState === 'running' && t && (Number(t.duration) > 1 || t.iterations === Infinity);
        })
        .map((a) => `${(a as CSSAnimation).animationName ?? "transition"} on ${(a.effect as KeyframeEffect).target?.className ?? "?"} (${a.effect?.getComputedTiming().duration} ms)`),
    );

  async function streamSomething(page: Page) {
    await page.goto('/ask');
    await waitReady(page);
    await page.locator('[data-slash-focus]').fill('Explain what a reduced-motion preference is, in a long paragraph.');
    await page.keyboard.press('Enter');
    await expect(page.locator('.lz-caret').first()).toBeVisible({ timeout: 15_000 });
  }

  test.describe('without the preference', () => {
    test.use({ contextOptions: { reducedMotion: 'no-preference' } });
    test('streaming shows a moving caret (sanity check for the test below)', async ({ page }) => {
      expect(await page.evaluate(() => matchMedia('(prefers-reduced-motion: reduce)').matches)).toBe(false);
      await streamSomething(page);
      expect((await moving(page)).length).toBeGreaterThan(0);
    });
  });

  test.describe('with prefers-reduced-motion', () => {
    test.use({ contextOptions: { reducedMotion: 'reduce' } });
    test('nothing keeps animating, including the streaming caret', async ({ page }) => {
      await page.goto('/');
      expect(await page.evaluate(() => matchMedia('(prefers-reduced-motion: reduce)').matches)).toBe(true);
      for (const p of ['/', '/agents', '/jobs', '/models/discovery', '/system/compute']) {
        await page.goto(p);
        await waitReady(page);
        expect(await moving(page), p).toEqual([]);
      }
      await streamSomething(page);
      expect(await moving(page), 'Ask while streaming').toEqual([]);
    });
  });
});

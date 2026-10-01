// §103–§105 as a first-time user would do them, without instructions: ask and see who answered, understand
// a fallback, check the GPU and BLERBZ, pause a batch job, check for better models, approve a review,
// connect a phone and continue its conversation on the desktop. §62: an outage reads as a human message.
import { devices, expect, test, type Page } from 'playwright/test';
import { RAW_TERMS, setScenario, waitReady } from './helpers';

test.use({ viewport: { width: 1440, height: 900 }, colorScheme: 'dark' });
test.afterEach(async () => {
  await setScenario('healthy');
});

/** The visible text of the page never shows raw HTTP codes, k8s states or JS leftovers (§38, §81, §91). */
async function expectHumanText(page: Page, where: string) {
  const text = await page.locator('body').innerText();
  expect(text.match(RAW_TERMS)?.[0] ?? null, `${where}: raw technical text on screen`).toBeNull();
}

async function sendPrompt(page: Page, text: string) {
  const box = page.getByRole('textbox', { name: 'What do you want to do?' });
  await box.fill(text);
  await page.getByRole('button', { name: /^Send/ }).click();
}

test('ask a question: streamed answer, "Handled by", and which model handled it (§9, §13, §21)', async ({ page }) => {
  await page.goto('/');
  await waitReady(page);
  // From Home, the command bar is the fastest path: a question goes to Ask and is sent there.
  const bar = page.locator('.lz-commandbar input');
  await bar.fill('Write a haiku about local AI on a quiet evening');
  await bar.press('Enter');
  await expect(page).toHaveURL(/\/ask\/[^/]+$/, { timeout: 15_000 });
  const answer = page.locator('.ask-thread article').last();
  // Streaming: text grows while the caret shows, then the receipt appears.
  await expect(page.locator('.lz-caret')).toBeVisible({ timeout: 15_000 });
  const early = (await answer.innerText()).length;
  await expect.poll(async () => (await answer.innerText()).length, { timeout: 15_000 }).toBeGreaterThan(early);
  const receipt = page.getByText(/^Handled by local\//);
  await expect(receipt).toBeVisible({ timeout: 45_000 });
  await expect(page.locator('.lz-caret')).toHaveCount(0);
  await expect(page.getByText('Local only').first()).toBeVisible();

  // Which model handled it: Open Details names the mode, model and route.
  await page.getByRole('button', { name: 'Open Details' }).click();
  const drawer = page.getByRole('dialog', { name: 'Answer details' });
  await expect(drawer).toBeVisible();
  await expect(drawer.getByText('Model', { exact: true })).toBeVisible();
  await expect(drawer).toContainText(/Qwen|Llama|Phi/);
  await expect(drawer.getByRole('heading', { name: 'Route' })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(drawer).toBeHidden();
  await expect(page.getByRole('button', { name: 'Open Details' })).toBeFocused();
  await expectHumanText(page, 'ask');
});

test('Ask answers questions about Labzilla from live state, and the model on request (§9, §13, §88)', async ({ page }) => {
  await page.goto('/ask');
  await waitReady(page);
  // A status question typed into Ask: answered by the rules from the snapshot, no conversation is created.
  await sendPrompt(page, 'Why is the GPU busy?');
  const turn = page.getByRole('region', { name: 'Answered by Labzilla' });
  await expect(turn.getByRole('heading', { name: 'What the GPU is doing' })).toBeVisible();
  await expect(turn.getByText('Understood by Labzilla rules')).toBeVisible();
  await expect(page).toHaveURL(/\/ask$/);
  await expect(page.getByRole('textbox', { name: 'What do you want to do?' })).toHaveValue('');
  // One click sends the same words to the model instead.
  await turn.getByRole('button', { name: 'Ask the model instead' }).click();
  await expect(page).toHaveURL(/\/ask\/[^/]+$/, { timeout: 15_000 });
  await expect(page.getByText(/^Handled by local\//)).toBeVisible({ timeout: 45_000 });
  await expect(turn).toHaveCount(0);

  // An operational command proposes its action (nothing runs until pressed); a request to explain goes to the model.
  await sendPrompt(page, 'Pause batch jobs');
  await expect(page.getByRole('region', { name: 'Answered by Labzilla' }).getByRole('button', { name: /Pause all batch jobs/ })).toBeVisible();
  await page.getByRole('region', { name: 'Answered by Labzilla' }).getByRole('button', { name: 'Close' }).click();
  const answers = await page.locator('.ask-thread article').count();
  await sendPrompt(page, 'Explain how GPU memory works');
  await expect(page.getByRole('region', { name: 'Answered by Labzilla' })).toHaveCount(0);
  await expect.poll(() => page.locator('.ask-thread article').count(), { timeout: 15_000 }).toBeGreaterThan(answers);
  await expectHumanText(page, 'ask routing');
});

test('understand why AI fell back: Home explains, the role page shows the cause (§81, §82)', async ({ page }) => {
  await setScenario('fallback');
  await page.goto('/');
  await expect(page.getByRole('heading', { level: 1, name: 'Running on fallback model' })).toBeVisible({ timeout: 20_000 });
  const attention = page.getByRole('region', { name: 'Needs your attention' });
  await expect(attention).toBeVisible();
  const card = attention.getByRole('alert').first();
  await expect(card).toContainText(/model unavailable/);
  await expect(card).toContainText('Labzilla is using');
  await expect(card).toContainText('Responses may be less capable');
  await expect(card.getByRole('button', { name: 'Retry default' })).toBeVisible();
  await expectHumanText(page, 'home (fallback)');

  await card.getByRole('button', { name: 'View details' }).click();
  await expect(page).toHaveURL(/\/models\/roles\/\w+$/);
  await waitReady(page);
  const main = page.locator('main');
  await expect(main).toContainText(/backup/i);
  await expect(main).toContainText('Qwen3 1.7B');
  await expectHumanText(page, 'role page (fallback)');

  // The command bar answers the same question in words (§8).
  const bar = page.locator('.lz-commandbar input');
  await bar.fill('Why did local/default fall back?');
  await bar.press('Enter');
  const result = page.getByRole('region', { name: 'Command result' });
  await expect(result).toBeVisible({ timeout: 10_000 });
  await expect(result).toContainText(/1\.7B|backup|fallback/i);
});

test('check GPU load and what BLERBZ uses (System › Compute, §36)', async ({ page }) => {
  await setScenario('blerbz-busy');
  await page.goto('/system/compute');
  await waitReady(page);
  const main = page.locator('main');
  await expect(main).toContainText(/GPU reserved for BLERBZ/, { timeout: 20_000 });
  await expect(main).toContainText(/GPU load\s*\d+\s*%/);
  await expect(main).toContainText(/BLERBZ[\s\S]{0,40}\d+(\.\d)?\s*GB/);
  await expect(main).toContainText(/\/\s*128(\.0)?\s*GB/);
  await expectHumanText(page, 'compute (blerbz-busy)');

  // Batch work says why it waits and that it resumes by itself (§31).
  await page.goto('/jobs');
  await waitReady(page);
  await expect(page.locator('main')).toContainText('GPU reserved for BLERBZ video generation');
  await expect(page.locator('main')).toContainText('Resumes automatically');

  // "Why is the GPU busy?" from the command bar.
  const bar = page.locator('.lz-commandbar input');
  await bar.fill('Why is the GPU busy?');
  await bar.press('Enter');
  const result = page.getByRole('region', { name: 'Command result' });
  await expect(result).toBeVisible({ timeout: 10_000 });
  await expect(result).toContainText('BLERBZ');
});

test('pause a batch job and resume it (Jobs, §30, §31)', async ({ page }) => {
  await page.goto('/jobs');
  await waitReady(page);
  const pause = page.getByRole('button', { name: /^Pause: / }).first();
  await expect(pause).toBeVisible();
  const title = ((await pause.getAttribute('aria-label')) ?? '').replace(/^Pause: /, '');
  await pause.click();
  // Routine, safe: no confirmation; the row updates and offers Resume.
  await expect(page.getByRole('dialog')).toHaveCount(0);
  const resume = page.getByRole('button', { name: `Resume: ${title}` });
  await expect(resume).toBeVisible({ timeout: 10_000 });
  await expect(page.getByText(/paused/i).first()).toBeVisible();
  await resume.click();
  await expect(page.getByRole('button', { name: `Pause: ${title}` })).toBeVisible({ timeout: 10_000 });
});

test('check for better models and see progress, then a summary (§28)', async ({ page }) => {
  await page.goto('/models');
  await waitReady(page);
  await page.getByRole('button', { name: /Check for better models/i }).click();
  await expect(page.getByText('Checking for better models…')).toBeVisible({ timeout: 10_000 });
  await expect(page.getByRole('list', { name: 'Discovery stages' }).first()).toBeVisible();
  for (const stage of ['Discovering', 'Filtering', 'Evaluating', 'Benchmarking'])
    await expect(page.getByRole('list', { name: 'Discovery stages' }).first()).toContainText(stage);
  // The fake run finishes about 6 s later and reports a summary instead of the running title.
  await expect(page.getByText('Checking for better models…')).toHaveCount(0, { timeout: 30_000 });
  await expect(page.locator('main')).toContainText(/upgrade|candidate|No better/i);
  await expectHumanText(page, 'models after a check');
});

test('approve a review from Home and it leaves the queue (§40)', async ({ page }) => {
  await page.goto('/');
  await waitReady(page);
  const card = page.locator('article.lz-approval').first();
  await expect(card).toBeVisible({ timeout: 15_000 });
  // The card states action, why and impact before any button is pressed.
  for (const label of ['Action', 'Why', 'Impact']) await expect(card.getByText(label, { exact: true })).toBeVisible();
  const title = (await card.locator('.lz-card-title').innerText()).trim();
  await card.getByRole('group', { name: /^Answer:/ }).getByRole('button').first().click();
  await expect(page.getByText('Answer recorded')).toBeVisible({ timeout: 10_000 });
  await page.goto('/agents');
  await waitReady(page);
  await expect(page.locator('article.lz-approval', { hasText: title }).getByRole('group', { name: /^Answer:/ })).toHaveCount(0);
});

test('connect a phone, ask from it, and continue on the desktop (§15–17, §104, §105)', async ({ page, browser, baseURL }) => {
  test.setTimeout(120_000);
  // Desktop: Connect a phone → Start pairing → QR + address.
  await page.goto('/connect');
  await waitReady(page);
  const started = page.waitForResponse((r) => r.url().endsWith('/api/pair/start') && r.request().method() === 'POST');
  await page.getByRole('button', { name: /^Start pairing/ }).click();
  const pairing = await (await started).json();
  await expect(page.getByRole('img', { name: 'QR code to pair a phone with Labzilla' })).toBeVisible();
  expect(pairing.url).toContain('/pair#');
  const fragment = String(pairing.url).slice(String(pairing.url).indexOf('#'));

  // Phone: a separate browser context (no session), phone-sized with an iPhone user agent.
  const { defaultBrowserType: _ignored, ...iphone } = devices['iPhone 13'];
  const phoneCtx = await browser.newContext({ ...iphone, baseURL, colorScheme: 'dark', reducedMotion: 'reduce' });
  try {
    const phone = await phoneCtx.newPage();
    await phone.goto(`/pair${fragment}`);
    await expect(phone.getByRole('heading', { name: 'Name this device' })).toBeVisible();
    await phone.getByLabel('Device name').fill('Test phone');
    await phone.getByRole('button', { name: 'Pair this device' }).click();
    await expect(phone.getByRole('heading', { name: 'Confirm this code on your desktop' })).toBeVisible();
    const phoneCode = (await phone.locator('.lz-pair-code').innerText()).trim();
    expect(phoneCode).toMatch(/^\d{6}$/);

    // Desktop sees the claim live, with the same code, and approves.
    await expect(page.getByRole('heading', { name: /“Test phone” wants to connect/ })).toBeVisible({ timeout: 15_000 });
    await expect(page.locator('.lz-pair-code')).toHaveText(phoneCode);
    await page.getByRole('button', { name: 'Approve' }).click();
    await expect(page.getByText(/is paired/).first()).toBeVisible();
    await expect(page.getByRole('list', { name: 'Paired devices' })).toContainText('Test phone');

    // Phone lands on the Mobile Gateway home, signed in.
    await expect(phone).toHaveURL(/\/$/, { timeout: 15_000 });
    await expect(phone.getByText('This device is paired')).toBeVisible();
    await expect(phone.getByRole('heading', { name: 'LABZILLA' })).toBeVisible();
    await expect(phone.locator('.lz-bottomnav')).toBeVisible();

    // §104: tap Code → "Review this code snippet" + pasted code → Send → streamed answer.
    await phone.getByRole('link', { name: 'Code' }).click();
    await expect(phone).toHaveURL(/\/ask/);
    const box = phone.getByRole('textbox', { name: 'What do you want to do?' });
    await expect(box).toBeFocused();
    await box.fill('Review this code snippet\n\n');
    const code = 'def add(a, b):\n    return a - b\n';
    await box.evaluate((el, text) => {
      const dt = new DataTransfer();
      dt.setData('text/plain', text);
      const ev = new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true });
      if (el.dispatchEvent(ev)) {
        // Not intercepted (text paste): the browser would insert it; do the same.
        const ta = el as HTMLTextAreaElement;
        ta.setRangeText(text, ta.selectionStart, ta.selectionEnd, 'end');
        ta.dispatchEvent(new Event('input', { bubbles: true }));
      }
    }, code);
    await expect(box).toHaveValue(/return a - b/);
    await phone.getByRole('button', { name: /^Send/ }).tap();
    await expect(phone).toHaveURL(/\/ask\/[^/?]+/, { timeout: 15_000 });
    await expect(phone.locator('.lz-caret')).toBeVisible({ timeout: 15_000 });
    await expect(phone.getByText(/^Handled by local\//)).toBeVisible({ timeout: 45_000 });
    const threadPath = new URL(phone.url()).pathname;

    // §105: the desktop opens the same task — no copy/paste.
    await page.goto(threadPath);
    await waitReady(page);
    const thread = page.getByRole('region', { name: 'Conversation' });
    await expect(thread).toContainText('Review this code snippet');
    await expect(thread).toContainText('return a - b');
    await expect(thread.getByText(/^Handled by local\//)).toBeVisible();
    await expectHumanText(phone, 'phone ask');
  } finally {
    await phoneCtx.close();
  }
});

test('phone Home prompt resolves commands like the command bar, and pasted code keeps its lines (§7, §12, §104)', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  await waitReady(page);
  const prompt = page.getByRole('textbox', { name: 'Ask Labzilla' });
  await prompt.fill('Why is the GPU busy?');
  await prompt.press('Enter');
  // A snapshot answer in a sheet, not a model-generated guess in Ask.
  const sheet = page.getByRole('dialog', { name: 'Labzilla' });
  await expect(sheet).toBeVisible({ timeout: 10_000 });
  await expect(sheet).toContainText('BLERBZ');
  await expect(page).toHaveURL(/\/$/);
  await page.keyboard.press('Escape');

  // Multi-line paste moves the draft into Ask's composer with its line breaks, unsent.
  await prompt.fill('Review this code snippet');
  await prompt.evaluate((el) => {
    const dt = new DataTransfer();
    dt.setData('text/plain', 'def sub(a, b):\n    return a - b\n');
    el.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }));
  });
  await expect(page).toHaveURL(/\/ask/);
  const box = page.getByRole('textbox', { name: 'What do you want to do?' });
  await expect(box).toHaveValue('Review this code snippet\n\ndef sub(a, b):\n    return a - b\n');
});

test('failed work explains itself in words, without protocol codes (§81)', async ({ page }) => {
  // The fake world has a failed discovery run (upstream: "rate limited (HTTP 429)") and a failed batch job.
  for (const p of ['/jobs', '/models/discovery', '/agents']) {
    await page.goto(p);
    await waitReady(page);
    await expect(page.locator('main')).toContainText(/fail/i);
    await expectHumanText(page, p);
  }
  await page.goto('/jobs');
  await waitReady(page);
  await expect(page.locator('main')).toContainText('Hugging Face API rate limited');
});

test('an outage reads as a human message with Retry, never a cryptic error (§62, §81)', async ({ page, context }) => {
  await setScenario('offline');
  await page.goto('/');
  await expect(page.getByRole('heading', { level: 1, name: /unreachable|unavailable|offline/i })).toBeVisible({ timeout: 20_000 });
  await expect(page.getByRole('button', { name: /Retry/ }).first()).toBeVisible();
  await expectHumanText(page, 'home (offline upstreams)');
  for (const p of ['/models', '/jobs', '/system/services', '/ask']) {
    await page.goto(p);
    await expect(page.locator('main h1').first()).toBeVisible({ timeout: 15_000 });
    await page.waitForTimeout(500);
    await expectHumanText(page, `${p} (offline upstreams)`);
  }
  // No healthy-looking service while every upstream is down.
  await page.goto('/system/services');
  await waitReady(page);
  await expect(page.locator('main')).toContainText(/Not answering/);

  // The browser itself loses the network: the §62 banner with Retry; it clears once Labzilla answers again.
  await setScenario('healthy');
  await page.goto('/');
  await waitReady(page);
  await context.setOffline(true);
  const banner = page.getByRole('status').filter({ hasText: 'Labzilla unavailable' });
  await expect(banner).toBeVisible({ timeout: 20_000 });
  await expect(banner).toContainText('Not connected to your local network');
  await expect(banner.getByRole('button', { name: 'Retry' })).toBeVisible();
  await expectHumanText(page, 'browser offline');
  // Retry while still offline is honest: the banner stays.
  await banner.getByRole('button', { name: 'Retry' }).click();
  await expect(banner).toBeVisible();
  await expect(page.locator('main h1').first()).toBeVisible(); // what was loaded stays on screen (§60)
  // Back online: Retry (or the live connection on its own) clears it.
  await context.setOffline(false);
  await banner.getByRole('button', { name: 'Retry' }).click({ timeout: 3_000 }).catch(() => undefined);
  await expect(banner).toBeHidden({ timeout: 15_000 });
});

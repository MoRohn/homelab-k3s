// §83–84: first-run setup with the dev setup code → "Labzilla is ready" → Ask. Saves the admin session
// for the other specs (login is rate-limited, and setup succeeds only once per database).
import { expect, test } from 'playwright/test';
import { ADMIN, STATE } from './env';

test('first-run setup lands on "Labzilla is ready" and opens Ask', async ({ page }) => {
  const setup = await (await page.request.get('/api/setup')).json();
  await page.goto('/');
  if (!setup.needs_setup) {
    // A reused dev server: setup already happened in an earlier run, so sign in with the same account.
    await expect(page).toHaveURL(/\/login/);
    await page.getByLabel('Name').fill(ADMIN.name);
    await page.getByLabel('Passphrase').fill(ADMIN.pass);
    await page.getByRole('button', { name: /^Sign in/ }).click();
    await expect(page).not.toHaveURL(/\/login/);
    await expect(page.locator('.lz-shell')).toBeVisible();
    expect((await page.context().cookies()).map((c) => c.name)).toContain('lz_session');
    await page.context().storageState({ path: STATE });
    test.info().annotations.push({ type: 'note', description: 'setup already done on this server; signed in instead' });
    return;
  }

  // An unconfigured first visit goes straight to setup.
  await expect(page).toHaveURL(/\/setup$/);
  await expect(page.getByRole('heading', { name: 'Welcome to Labzilla' })).toBeVisible();
  await expect(page.getByText('Step 1 of 5')).toBeVisible();
  await page.getByRole('button', { name: 'Get started' }).click();

  await expect(page.getByRole('heading', { name: 'System detected' })).toBeFocused();
  await page.getByRole('button', { name: 'Continue' }).click();

  await expect(page.getByRole('heading', { name: 'Create the admin account' })).toBeVisible();
  // Validation explains itself before anything is sent.
  await page.getByRole('button', { name: 'Continue' }).click();
  await expect(page.getByText('Enter the setup code')).toBeVisible();
  await page.getByLabel('Setup code').fill(ADMIN.code);
  await page.getByLabel('Your name').fill(ADMIN.name);
  await page.getByLabel('Passphrase', { exact: true }).fill(ADMIN.pass);
  await page.getByLabel('Repeat passphrase').fill(ADMIN.pass);
  await page.getByRole('button', { name: 'Continue' }).click();

  await expect(page.getByRole('heading', { name: 'Mobile gateway' })).toBeVisible();
  await expect(page.getByText('Main address')).toBeVisible();
  await page.getByRole('button', { name: 'Continue' }).click();

  await expect(page.getByRole('heading', { name: 'AI defaults' })).toBeVisible();
  await expect(page.getByRole('radio', { name: /Auto \(recommended\)/ })).toBeChecked();
  await page.getByRole('button', { name: /Finish setup/ }).click();

  await expect(page.getByRole('heading', { name: 'Labzilla is ready.' })).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText('Local AI is available.')).toBeVisible();
  await page.getByRole('button', { name: 'Ask Labzilla' }).click();
  await expect(page).toHaveURL(/\/ask$/);
  await expect(page.getByRole('heading', { level: 1, name: 'Ask Labzilla' })).toBeVisible();

  const cookies = (await page.context().cookies()).map((c) => c.name);
  expect(cookies).toEqual(expect.arrayContaining(['lz_session', 'lz_csrf']));
  await page.context().storageState({ path: STATE });
});

// Playwright config for the console's browser e2e (setup, responsive, a11y, first-time-user tasks).
// It drives the dev harness (run_dev.sh: fake upstreams on :8091, console on :8090). Start that first;
// this config never starts or kills servers. Everything it writes (screenshots, traces, session state)
// goes to a temp dir outside the repo: E2E_OUT (default <tmpdir>/labzilla-console-e2e).
// One worker, no parallelism: the fake world and its scenario are global, and the host is memory-tight.
import { defineConfig, devices } from 'playwright/test';
import { OUT, STATE } from './env';

export default defineConfig({
  testDir: '.',
  testMatch: '*.spec.ts',
  outputDir: `${OUT}/test-results`,
  globalSetup: './global-setup.ts',
  workers: 1,
  fullyParallel: false,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [['list']],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://127.0.0.1:8090',
    trace: 'off',
    screenshot: 'only-on-failure',
    // CSS transitions mid-flight skew contrast checks and screenshots; specs that test motion opt back in.
    // (reducedMotion is not a fixture option: it goes through contextOptions.)
    contextOptions: { reducedMotion: 'reduce' },
  },
  projects: [
    { name: 'setup', testMatch: 'setup.spec.ts', use: { ...devices['Desktop Chrome'] } },
    {
      name: 'console',
      testIgnore: 'setup.spec.ts',
      dependencies: ['setup'],
      use: { ...devices['Desktop Chrome'], storageState: STATE },
    },
  ],
});

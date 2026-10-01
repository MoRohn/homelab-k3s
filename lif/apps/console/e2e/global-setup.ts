// Refuse to start a browser on a memory-starved host (the primary workload owns most of the RAM), and
// fail fast with a clear message when the dev harness isn't running.
import fs from 'node:fs';
import type { FullConfig } from 'playwright/test';
import { FAKE, OUT } from './env';

const MIN_AVAILABLE_KIB = 3 * 1024 * 1024;

export default async function globalSetup(config: FullConfig) {
  try {
    const m = /MemAvailable:\s+(\d+) kB/.exec(fs.readFileSync('/proc/meminfo', 'utf8'));
    if (m && Number(m[1]) < MIN_AVAILABLE_KIB)
      throw new Error(`MemAvailable is ${(Number(m[1]) / 1048576).toFixed(1)} GiB (< 3 GiB): not launching chromium`);
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code !== 'ENOENT') throw e;
  }
  const base = config.projects[0]?.use.baseURL ?? 'http://127.0.0.1:8090';
  for (const url of [`${base}/healthz`, `${FAKE}/__scenario`]) {
    const ok = await fetch(url).then((r) => r.ok, () => false);
    if (!ok) throw new Error(`${url} is not answering: start lif/apps/console/e2e/run_dev.sh first`);
  }
  // Every run starts from the healthy world.
  await fetch(`${FAKE}/__scenario`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ name: 'healthy' }) });
  fs.mkdirSync(OUT, { recursive: true });
}

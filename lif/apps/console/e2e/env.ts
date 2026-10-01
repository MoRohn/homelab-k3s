// Paths and endpoints shared by the e2e config and specs. Outputs never land in the (public) repo.
import os from 'node:os';
import path from 'node:path';

export const OUT = process.env.E2E_OUT ?? path.join(os.tmpdir(), 'labzilla-console-e2e');
export const STATE = path.join(OUT, 'admin-state.json');
export const SHOTS = process.env.E2E_SHOTS ?? path.join(OUT, 'shots');
export const FAKE = process.env.E2E_FAKE_URL ?? 'http://127.0.0.1:8091';

/** Dev-harness admin (run_dev.sh uses setup code dev-setup-code and a fresh DB per run). */
export const ADMIN = { code: 'dev-setup-code', name: 'owner', pass: 'e2e correct horse battery' };

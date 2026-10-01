// Shared pieces of the Models area (§10, §26–§29): cache keys and fetchers, permission reasons, and the one
// action runner every model operation goes through (preview → confirm → POST → toast → refresh, §41, §98).
// Kept inside pages/models so the lazy Models chunk carries it and the Home/Ask bundles don't.
import { useState } from 'preact/hooks';
import { get, post, qs, toHumanError } from '@/api/client';
import type {
  ActionPreview,
  CandidateComparison,
  CandidateItem,
  DeploymentDetail,
  DiscoveryState,
  HumanError,
  ModelRole,
  ModelsOverview,
  Perm,
  PreviewAction,
  RoleKey,
  User,
} from '@/api/contracts.gen';
import { observable, useObservable } from '@/api/observable';
import { can, useMe } from '@/api/session';
import { invalidate, useResource } from '@/api/store';
import { ConfirmDialog, toast } from '@/ui';
import './models.css';

// ── roles ────────────────────────────────────────────────────────────────────────────────────────

/** The roles people choose between (§10), in the order the portfolio shows them. */
export const MAIN_ROLES: RoleKey[] = ['fast', 'balanced', 'deep', 'code', 'vision', 'auto'];
/** Plumbing roles: real aliases, but nobody picks them for a prompt, so they sit behind "Advanced" (§97). */
export const ADVANCED_ROLES: RoleKey[] = ['instant', 'batch', 'embedding', 'rerank'];

/**
 * A sentence or two per role for the role page (§96). The server's `blurb` stays the one-liner; this is the
 * longer "what is this for" that only the detail page has room for. Copy only — no claims about state.
 */
export const ROLE_HELP: Record<RoleKey, string> = {
  auto: 'Auto reads each request and hands it to the role that fits it best. You never need to pick a model to get a good answer.',
  fast: 'Fast answers quickly. Good for short questions, rewrites and quick checks where waiting matters more than depth.',
  balanced: "Balanced is the everyday default: capable enough for most work and still responsive.",
  deep: 'Deep takes longer and uses the largest model available, for multi-step reasoning and hard problems.',
  code: 'Code is tuned for writing, reviewing and explaining code.',
  vision: 'Vision reads images and screenshots.',
  instant: 'Instant is the smallest, quickest model. Labzilla uses it for classification and routing, not for answers.',
  batch: 'Batch serves background jobs that can wait, so they never compete with interactive requests.',
  embedding: 'Embedding turns text into vectors for search and knowledge lookups.',
  rerank: 'Rerank reorders search results by relevance.',
};

/** Roles are routed by alias; the auto role is synthetic and has no model of its own to roll back. */
export const ROLLBACK_EXEMPT: RoleKey[] = ['auto'];

/** The plain-words reason a role isn't on its usual model, or null when it is. */
export function causeLine(r: ModelRole): string | null {
  if (r.cause_label) return r.cause_label;
  if (r.fallback_active) return 'Served by a backup model';
  if (r.degraded) return 'Running on a smaller model than this role expects';
  return null;
}

export function roleHref(role: RoleKey | string): string {
  return `/models/roles/${encodeURIComponent(role)}`;
}

export function deploymentHref(id: string): string {
  return `/models/deployments/${encodeURIComponent(id)}`;
}

export function candidateHref(id: string): string {
  return `/models/candidates/${encodeURIComponent(id)}`;
}

/** alias ("local/fast") → its ModelRole, from the cached overview. */
export function roleForAlias(roles: ModelRole[] | undefined, alias: string): ModelRole | undefined {
  return roles?.find((r) => r.alias === alias || r.role === alias);
}

// ── data ─────────────────────────────────────────────────────────────────────────────────────────
// Every key starts with "models/" so one invalidate('models/') after a mutation refreshes whatever is on screen.
// The poller publishes `model` events when a role, deployment or discovery run changes (§61): no polling.

export const KEY = {
  overview: 'models/overview',
  role: (r: string) => `models/role/${r}`,
  deployment: (id: string) => `models/deployment/${id}`,
  discovery: 'models/discovery',
  candidates: 'models/candidates',
  compare: (id: string) => `models/compare/${id}`,
} as const;

const enc = encodeURIComponent;

export const useOverview = () =>
  useResource(KEY.overview, () => get<ModelsOverview>('/api/models'), { refreshOn: ['model'] });

export const useRole = (role: string | undefined) =>
  useResource(role ? KEY.role(role) : null, () => get<ModelRole>(`/api/models/roles/${enc(role!)}`), { refreshOn: ['model'] });

export const useDeployment = (id: string | undefined) =>
  useResource(id ? KEY.deployment(id) : null, () => get<DeploymentDetail>(`/api/models/deployments/${enc(id!)}`), {
    refreshOn: ['model', 'jobs'],
  });

export const useCandidates = () =>
  useResource(KEY.candidates, () => get<CandidateItem[]>('/api/models/candidates'), { refreshOn: ['model'] });

export const useComparison = (id: string | undefined) =>
  useResource(id ? KEY.compare(id) : null, () => get<CandidateComparison>(`/api/models/candidates/${enc(id!)}/compare`), {
    refreshOn: ['model', 'jobs'],
  });

/**
 * Discovery state. The controller writes stage counts only when a run finishes and a running check may not
 * emit events of its own, so while one runs we also poll gently as a backstop (useDiscoveryStart does the same
 * for the gap between pressing the button and the run appearing).
 */
export const fetchDiscovery = () => get<DiscoveryState>('/api/models/discovery');

export function useDiscovery() {
  const res = useResource(KEY.discovery, fetchDiscovery, { refreshOn: ['model', 'jobs'] });
  // A second subscription to the same key only adds the poll; the cache entry is shared.
  useResource(res.data?.current?.status === 'running' ? KEY.discovery : null, fetchDiscovery, { pollMs: 4000 });
  return res;
}

// ── permissions (the server enforces; this only explains, §99) ───────────────────────────────────

/** Why this session can't do `perm`, or null when it can. Paired phones get safe operations only. */
export function whyNot(user: User | null | undefined, perm: Perm): string | null {
  if (can(user, perm)) return null;
  if (user?.role === 'device') return 'Needs admin — not available from a paired device';
  return 'Needs admin';
}

export function usePermReason(perm: Perm): string | null {
  const me = useMe();
  return me.data === undefined ? 'Checking your access…' : whyNot(me.data, perm);
}

// ── ignore (local, honest) ──────────────────────────────────────────────────────────────────────
// The backend has no "ignore candidate" operation (block is a release decision with lasting effect). Ignoring
// is therefore a per-device view preference: the candidate disappears from this browser's list, nothing changes
// on Labzilla, and the toast says exactly that. localStorage is fine here — losing it only re-shows a row.

const IGNORED = 'lz.models.ignoredCandidates';

function loadIgnored(): Set<string> {
  try {
    const raw = localStorage.getItem(IGNORED);
    return new Set(raw ? (JSON.parse(raw) as string[]) : []);
  } catch {
    return new Set();
  }
}

/** Candidate ids ignored on this device; observable so an Undo toast updates a list already on screen. */
export const ignored = observable<Set<string>>(loadIgnored());

export function setIgnored(id: string, on: boolean): void {
  const next = new Set(ignored.get());
  if (on) next.add(id);
  else next.delete(id);
  ignored.set(next);
  try {
    localStorage.setItem(IGNORED, JSON.stringify([...next]));
  } catch {
    /* storage blocked: the choice lasts for this tab only */
  }
}

export const useIgnored = () => useObservable(ignored);

// ── action runner ───────────────────────────────────────────────────────────────────────────────

export interface ModelOp {
  /** Button verb, reused as the confirm label ("Promote"). */
  label: string;
  /** POST target. */
  path: string;
  body?: Record<string, unknown>;
  /** When set, GET this ActionPreview first; the server decides whether (and how strongly) to confirm (§98). */
  preview?: string;
  /** Toast on success; the server's `message` (OkResponse) is shown as the body when present. */
  done: string;
  /** Destructive ops get the red confirm button. */
  tone?: 'danger' | 'primary';
  /** Called after a successful POST (e.g. navigate away). */
  after?: () => void;
}

/** GET /api/models/deployments/{id}/preview?action=… (alias/percent when they shape the answer). */
export function previewPath(id: string, action: PreviewAction, extra: { alias?: string | null; percent?: number | null } = {}): string {
  return `/api/models/deployments/${enc(id)}/preview${qs({ action, alias: extra.alias, percent: extra.percent })}`;
}

interface Pending {
  op: ModelOp;
  preview: ActionPreview;
}

/**
 * A refusal preview: the server explains why the action can't run and nothing would happen (routes/models.py
 * _Plan: confirm "none" with no rollback line). The UI shows the reason instead of POSTing into a 409.
 */
export function isRefusal(p: ActionPreview): boolean {
  return p.confirm === 'none' && !p.rollback;
}

/** Why an action that is otherwise offered can't run right now (from its preview), or null. */
export function useRefusal(previewUrl: string | null): string | null {
  const res = useResource<ActionPreview>(previewUrl ? `models/refusal/${previewUrl}` : null, () => get<ActionPreview>(previewUrl!), {
    refreshOn: ['model'],
    maxAgeMs: 30_000,
  });
  const p = res.data;
  return p && isRefusal(p) ? (p.changes[0] ?? p.title) : null;
}

function failToast(err: HumanError): void {
  toast({ title: err.title, body: [err.impact, err.next_step].filter(Boolean).join(' '), tone: 'error' });
}

/**
 * One runner per page: `run(op)` fetches the preview (if any), opens ConfirmDialog unless the server says the
 * op needs no confirmation, POSTs, toasts the outcome and invalidates every models/ key. Routine safe ops
 * (test, benchmark) carry no preview and run on the click (§41: never confirm routine safe actions).
 * Render `dialog` once in the page.
 */
export function useModelOps() {
  const [busy, setBusy] = useState<string | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const [error, setError] = useState<HumanError | null>(null);

  const execute = async (op: ModelOp, typed?: string) => {
    setBusy(op.label);
    setError(null);
    try {
      const body = typed === undefined ? (op.body ?? {}) : { ...op.body, confirm: typed };
      const res = await post<{ message?: string | null } | undefined>(op.path, body);
      setPending(null);
      toast({ title: op.done, body: res?.message ?? undefined, tone: 'success' });
      invalidate('models/');
      invalidate('jobs');
      op.after?.();
    } catch (e) {
      const err = toHumanError(e);
      // Inside the dialog the refusal stays next to what the user was confirming; otherwise toast it.
      if (pending) setError(err);
      else failToast(err);
    } finally {
      setBusy(null);
    }
  };

  const run = async (op: ModelOp) => {
    if (!op.preview) return execute(op);
    setBusy(op.label);
    setError(null);
    let preview: ActionPreview;
    try {
      preview = await get<ActionPreview>(op.preview);
    } catch (e) {
      setBusy(null);
      failToast(toHumanError(e));
      return;
    }
    setBusy(null);
    if (isRefusal(preview)) {
      toast({ title: preview.title, body: preview.changes.join(' '), tone: 'info' });
      return;
    }
    if (preview.confirm === 'none') return execute({ ...op, preview: undefined });
    setPending({ op, preview });
  };

  const dialog = pending ? (
    <ConfirmDialog
      open
      preview={pending.preview}
      confirmLabel={pending.op.label}
      tone={pending.op.tone ?? 'danger'}
      busy={busy === pending.op.label}
      error={error}
      onCancel={() => {
        setPending(null);
        setError(null);
      }}
      onConfirm={(typed) => void execute(pending.op, typed)}
    />
  ) : null;

  return { run, busy, dialog };
}

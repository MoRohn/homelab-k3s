// One place for job actions, shared by the Jobs list and the job page (§30, §31, §41).
// Pause/resume/retry are routine and safe: they run on click with no confirmation and a toast that says
// what happened; pause and resume show the new state before the server answers (§60). Cancel cannot be undone, so it goes through ConfirmDialog with the consequences spelled out.
// The server owns validity (it answers 409 "This job can't be paused" for non-batch work); we never guess.
import type { VNode } from 'preact';
import { useState } from 'preact/hooks';
import { post, toHumanError } from '@/api/client';
import type { ActionPreview, HumanError, Job, JobAction, JobGroups, JobsResponse } from '@/api/contracts.gen';
import { invalidate, peek, prime } from '@/api/store';
import { ConfirmDialog, toast } from '@/ui';

const DONE: Record<JobAction, (title: string) => string> = {
  pause: (t) => `Paused ${t}`,
  resume: (t) => `Resumed ${t}`,
  cancel: (t) => `Cancelled ${t}`,
  retry: (t) => `Retrying ${t}`,
};

const DONE_BODY: Partial<Record<JobAction, string>> = {
  pause: 'Items already running finish; nothing new starts until you resume it.',
  resume: 'It continues from where it stopped when resources allow.',
};

/** Facts from the batch engine: in-flight items of a cancelled job still finish and keep their results. */
function cancelPreview(job: Job): ActionPreview {
  return {
    title: `Cancel ${job.title}?`,
    // JobCounts doesn't say whether `done` includes failures, so no remaining-item arithmetic here.
    changes: ["Items that haven't started yet will not run.", 'Results already produced are kept.'],
    interrupts: ['Items already in progress finish; nothing new starts.'],
    rollback: 'No undo: submit the job again to run it.',
    confirm: 'simple',
  };
}

/** Pause/resume show at once (§60: optimistic safe actions); the server's answer replaces it either way. */
const OPTIMISTIC: Partial<Record<JobAction, Pick<Job, 'status' | 'status_label'>>> = {
  pause: { status: 'paused', status_label: 'Paused' },
  resume: { status: 'running', status_label: 'Resuming…' },
};

function optimistic(job: Job, action: JobAction): () => void {
  const next = OPTIMISTIC[action];
  if (!next) return () => undefined;
  const upd = (j: Job): Job => (j.id === job.id ? { ...j, ...next, actions: [] } : j);
  const itemKey = `jobs/item/${job.id}`;
  const one = peek<Job>(itemKey);
  const list = peek<JobsResponse>('jobs/list');
  if (one) prime(itemKey, upd(one));
  if (list) {
    const groups = Object.fromEntries(Object.entries(list.groups).map(([g, js]) => [g, (js as Job[]).map(upd)])) as unknown as JobGroups;
    prime('jobs/list', { ...list, groups });
  }
  return () => {
    if (one) prime(itemKey, one);
    if (list) prime('jobs/list', list);
  };
}

export interface JobActions {
  busy: { id: string; action: JobAction } | null;
  run: (job: Job, action: JobAction) => void;
  /** Render once; it is the cancel confirmation. */
  dialog: VNode | null;
}

export function useJobAction(onDone?: () => void): JobActions {
  const [busy, setBusy] = useState<JobActions['busy']>(null);
  const [confirming, setConfirming] = useState<Job | null>(null);
  const [error, setError] = useState<HumanError | null>(null);

  const send = async (job: Job, action: JobAction): Promise<boolean> => {
    setBusy({ id: job.id, action });
    const undo = optimistic(job, action);
    try {
      await post(`/api/jobs/${encodeURIComponent(job.id)}/${action}`, {});
      toast({ title: DONE[action](job.title), body: DONE_BODY[action], tone: 'success' });
      return true;
    } catch (e) {
      undo();
      const err = toHumanError(e);
      if (action === 'cancel') setError(err);
      else toast({ title: err.title, body: err.impact || err.next_step, tone: 'error' });
      return false;
    } finally {
      setBusy(null);
      invalidate('jobs/');
      onDone?.();
    }
  };

  const run = (job: Job, action: JobAction) => {
    if (action === 'cancel') {
      setError(null);
      setConfirming(job);
      return;
    }
    void send(job, action);
  };

  const dialog = confirming ? (
    <ConfirmDialog
      open
      preview={cancelPreview(confirming)}
      confirmLabel="Cancel job"
      busy={busy?.id === confirming.id}
      error={error}
      onCancel={() => setConfirming(null)}
      onConfirm={() => {
        void send(confirming, 'cancel').then((ok) => ok && setConfirming(null));
      }}
    />
  ) : null;

  return { busy, run, dialog };
}

// Agents: anything waiting on you first, then what agents are doing now and recently (§22, §40, §95).
// Two honest kinds of "approval" (understand.json): a blocking request gates something; a review is a
// decision spot-check whose answer only tunes future decisions — "Nothing is waiting on this answer".
// Live through SSE `approval` and `activity` events; nothing polls.
import { useEffect, useRef, useState } from 'preact/hooks';
import { useLocation } from 'preact-iso/router';
import { ApiError, get, post, toHumanError } from '@/api/client';
import type { AgentRun, AgentsOverview, Approval, User } from '@/api/contracts.gen';
import { can, useMe } from '@/api/session';
import { invalidate, useResource } from '@/api/store';
import { usePageTitle } from '@/shell/usePageTitle';
import { AgentCard, ApprovalCard, Badge, Button, Card, EmptyState, HumanErrorCard, Progress, Skeleton, Table, fmt, toast, type Column } from '@/ui';
import { AGENTS_KEY, APPROVALS_KEY, runHref, runStatus } from './labels';
import { RunAgentSheet } from './RunAgentSheet';
import './agents.css';

/** Reviews can number in the tens; show a few and let the user ask for the rest (§4). */
const REVIEWS_SHOWN = 3;

function permFor(a: Approval, user: User | null | undefined): { ok: boolean; whyNot?: string } {
  if (a.kind === 'device_pairing')
    return can(user, 'devices.manage') ? { ok: true } : { ok: false, whyNot: 'Only an admin session can approve a new device.' };
  return can(user, 'approvals.answer') ? { ok: true } : { ok: false, whyNot: 'This session can view approvals but not answer them.' };
}

function useAnswer() {
  const [busy, setBusy] = useState<{ id: string; value: string } | null>(null);
  const answer = async (a: Approval, value: string) => {
    setBusy({ id: a.id, value });
    try {
      await post(`/api/approvals/${encodeURIComponent(a.id)}`, { answer: value });
      const label = a.options.find((o) => o.value === value)?.label ?? value;
      toast({
        title: a.blocking ? `${label}: ${a.title}` : 'Answer recorded',
        body: a.blocking ? undefined : 'It will be used to tune future decisions.',
        tone: 'success',
      });
    } catch (e) {
      // 409: someone (or another device) answered first — just show the current state.
      if (e instanceof ApiError && e.status === 409) toast({ title: 'Already answered', body: 'This request was answered elsewhere.', tone: 'info' });
      else {
        const err = toHumanError(e);
        toast({ title: err.title, body: err.impact || err.next_step, tone: 'error' });
      }
    } finally {
      setBusy(null);
      invalidate(APPROVALS_KEY);
      invalidate(AGENTS_KEY);
    }
  };
  return { busy, answer };
}

function Approvals({ items, user, focus }: { items: Approval[]; user: User | null | undefined; focus: boolean }) {
  const ref = useRef<HTMLElement>(null);
  const [allReviews, setAllReviews] = useState(false);
  const { busy, answer } = useAnswer();
  const pending = items.filter((a) => a.status === 'pending');
  const blocking = pending.filter((a) => a.blocking);
  const reviews = pending.filter((a) => !a.blocking);

  useEffect(() => {
    if (focus) ref.current?.focus();
  }, [focus]);

  if (!pending.length && !focus) return null;

  const card = (a: Approval) => {
    const p = permFor(a, user);
    return (
      <ApprovalCard key={a.id} approval={a} canAnswer={p.ok} whyNot={p.whyNot} busy={busy?.id === a.id ? busy.value : null} onAnswer={(v) => answer(a, v)} />
    );
  };
  const shownReviews = allReviews ? reviews : reviews.slice(0, REVIEWS_SHOWN);

  return (
    <section id="approvals" ref={ref} tabIndex={-1} class="stack lz-agents-section" aria-labelledby="approvals-h">
      <h2 id="approvals-h" class="sr-only">
        Waiting on you
      </h2>
      {!pending.length && <EmptyState compact icon="success" title="Nothing is waiting on you" body="Approvals and decision reviews appear here when they need an answer." />}
      {blocking.length > 0 && (
        <div class="stack-sm">
          <h3 class="lz-agents-h">Needs your approval</h3>
          {blocking.map(card)}
        </div>
      )}
      {reviews.length > 0 && (
        <div class="stack-sm">
          <div class="row-between wrap">
            <h3 class="lz-agents-h">
              Decision reviews <Badge size="sm" tone="info">{reviews.length}</Badge>
            </h3>
          </div>
          <p class="muted small">Nothing is waiting on these answers. Each one is a spot check that helps tune future decisions.</p>
          {shownReviews.map(card)}
          {reviews.length > shownReviews.length && (
            <Button variant="ghost" size="sm" onClick={() => setAllReviews(true)}>
              Show all {reviews.length} reviews
            </Button>
          )}
        </div>
      )}
    </section>
  );
}

const columns: Column<AgentRun>[] = [
  { key: 'agent', header: 'Agent', primary: true, width: '22%', render: (r) => <strong>{r.agent}</strong> },
  { key: 'task', header: 'Task', render: (r) => r.task },
  {
    key: 'status',
    header: 'Status',
    width: '9rem',
    render: (r) => (
      <Badge size="sm" tone={runStatus(r.status).tone}>
        {runStatus(r.status).label}
      </Badge>
    ),
  },
  { key: 'started', header: 'Started', width: '8rem', render: (r) => <span class="num">{fmt.ago(r.started_at)}</span> },
  {
    key: 'progress',
    header: 'Progress',
    width: '9rem',
    hideOnCompact: true,
    render: (r) =>
      r.progress !== null && r.progress !== undefined ? <Progress value={r.progress} label={`${r.agent} progress`} size="sm" showValue /> : <span class="muted">{fmt.DASH}</span>,
  },
];

export default function Agents() {
  usePageTitle('Agents');
  const { query, route } = useLocation();
  const me = useMe();
  const overview = useResource(AGENTS_KEY, () => get<AgentsOverview>('/api/agents'), { refreshOn: ['activity', 'approval', 'jobs'] });
  const approvals = useResource(APPROVALS_KEY, () => get<Approval[]>('/api/approvals'), { refreshOn: ['approval'] });
  const [sheet, setSheet] = useState(query.run === '1');

  // Palette / command bar deep links: ?run=1 opens the sheet.
  useEffect(() => {
    if (query.run === '1') setSheet(true);
  }, [query.run]);

  const closeSheet = () => {
    setSheet(false);
    if (query.run) route('/agents', true);
  };

  const data = overview.data;
  const canRun = !!data?.catalog.some((c) => c.runnable);
  const runAction = { label: 'Run an agent', onClick: () => setSheet(true), icon: 'play' as const };

  return (
    <div class="page">
      <header class="page-header">
        <div>
          <h1>Agents</h1>
          <p>What agents are doing, and anything waiting on you.</p>
        </div>
        {canRun && (
          <Button variant="primary" icon="play" onClick={() => setSheet(true)}>
            Run an agent
          </Button>
        )}
      </header>

      {approvals.error && !approvals.data && <HumanErrorCard compact error={approvals.error} onRetry={() => void approvals.refresh()} />}
      {approvals.data && <Approvals items={approvals.data} user={me.data} focus={query.tab === 'decisions' || query.tab === 'approvals' || location.hash === '#approvals'} />}

      {overview.error && !data && <HumanErrorCard error={overview.error} onRetry={() => void overview.refresh()} />}
      {overview.loading && (
        <div aria-busy="true" class="stack">
          <Skeleton height="5rem" />
          <Skeleton lines={4} />
        </div>
      )}

      {data && (
        <>
          {data.note && (
            <p class="lz-agents-note small" role="status">
              {data.note}
            </p>
          )}

          <section class="stack-sm lz-agents-section" aria-labelledby="running-h">
            <h2 id="running-h" class="lz-agents-h">
              Running{data.running.length ? ` · ${data.running.length}` : ''}
            </h2>
            {data.running.length ? (
              <ul role="list" class="lz-agents-running">
                {data.running.map((r) => (
                  <li key={r.id}>
                    <AgentCard run={r} href={runHref(r)} />
                  </li>
                ))}
              </ul>
            ) : (
              <EmptyState
                compact
                icon="agents"
                title="No agents running"
                body={canRun ? 'Start one to check for better models or evaluate a candidate.' : 'No agent can be started right now; the reason is in the Run sheet.'}
                action={runAction}
              />
            )}
          </section>

          <Card title="Recent" subtitle="Finished and failed agent work, newest first." padded={false}>
            <Table
              // A Progress column of dashes is noise: show it only when some run reports progress.
              columns={data.recent.some((r) => r.progress != null) ? columns : columns.filter((c) => c.key !== 'progress')}
              rows={data.recent}
              rowKey={(r) => r.id}
              onRowClick={(r) => route(runHref(r))}
              caption="Recent agent runs"
              hideCaption
              empty={<EmptyState compact icon="history" title="No agent work recorded yet" body="Runs appear here once an agent finishes." action={runAction} />}
            />
          </Card>

          <RunAgentSheet open={sheet} onClose={closeSheet} catalog={data.catalog} user={me.data} />
        </>
      )}
    </div>
  );
}

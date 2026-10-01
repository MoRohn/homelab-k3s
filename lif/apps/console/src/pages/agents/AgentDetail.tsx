// One agent run (§23–§25): goal, current step, a timeline a non-expert can follow, tools, decisions with
// "Why this route?" (structured fields only — never hidden reasoning), artifacts, cost and result.
// Runs are synthesized from real records, so the source line says where this one came from, and fields
// the backend doesn't record (often cost) say so instead of showing an invented value.
import { get } from '@/api/client';
import type { AgentRun } from '@/api/contracts.gen';
import { useResource } from '@/api/store';
import { usePageTitle } from '@/shell/usePageTitle';
import { Badge, Card, DecisionBadge, FactList, HumanErrorCard, Icon, List, ListItem, Progress, Skeleton, TechDetails, Timeline, fmt, type FactItem } from '@/ui';
import { RUN_SOURCE, RUN_STATUS } from './labels';
import './agents.css';

function summary(run: AgentRun): FactItem[] {
  const items: FactItem[] = [{ label: 'Started', value: run.started_at ? fmt.ago(run.started_at) : 'Not started yet' }];
  if (run.finished_at) items.push({ label: 'Finished', value: fmt.ago(run.finished_at) });
  if (run.started_at && run.finished_at) items.push({ label: 'Took', value: fmt.duration(run.finished_at - run.started_at) });
  items.push({
    label: 'Cost',
    value: run.cost_label ?? 'Not tracked yet',
    hint: run.cost_label ? undefined : 'Per-run cost is not recorded by the backend yet.',
  });
  return items;
}

function resultText(run: AgentRun): string {
  if (run.result) return run.result;
  if (run.status === 'running' || run.status === 'queued') return 'Not finished yet.';
  if (run.status === 'waiting_approval') return 'Waiting for an answer before it can finish.';
  return 'No result was recorded for this run.';
}

export default function AgentDetail({ id }: { id?: string }) {
  // preact-iso already decoded the route param; encode exactly once for the API path.
  const res = useResource(id ? `agents/run/${id}` : null, () => get<AgentRun>(`/api/agents/${encodeURIComponent(id ?? '')}`), {
    refreshOn: ['activity', 'jobs', 'model'],
  });
  const run = res.data;
  usePageTitle(run ? `${run.agent}: ${run.task}` : 'Agent run');
  const active = run?.status === 'running' || run?.status === 'queued';

  return (
    <div class="page">
      <a class="lz-crumb small" href="/agents">
        <Icon name="chevron-left" size={16} />
        Agents
      </a>

      {res.error && !run && <HumanErrorCard error={res.error} onRetry={() => void res.refresh()} />}
      {res.loading && (
        <div aria-busy="true" class="stack">
          <Skeleton height="2rem" width="50%" />
          <Skeleton lines={5} />
        </div>
      )}

      {run && (
        <>
          <header class="page-header">
            <div class="stack-sm">
              <h1>{run.agent}</h1>
              <div class="row wrap">
                <Badge tone={RUN_STATUS[run.status].tone}>{RUN_STATUS[run.status].label}</Badge>
                <span class="muted small">{RUN_SOURCE[run.source]}</span>
              </div>
            </div>
          </header>

          <Card title="Goal">
            <div class="stack-sm">
              <p>{run.task}</p>
              {active && (
                <>
                  <p>
                    <span class="muted">Current step: </span>
                    {run.current_step ?? 'Starting…'}
                  </p>
                  <Progress value={run.progress} label={`${run.agent} progress`} showValue />
                </>
              )}
              {run.status === 'waiting_approval' && (
                <p class="lz-tone-warning">
                  Waiting for an answer. <a href="/agents?tab=decisions">Open approvals</a>
                </p>
              )}
              <FactList items={summary(run)} columns={2} />
            </div>
          </Card>

          <Card title="Timeline" subtitle="What happened, in order.">
            <Timeline steps={run.steps} live={run.status === 'running'} emptyLabel="No steps were recorded for this run." />
          </Card>

          <Card title="Decisions" subtitle="Each decision lists the facts it was based on.">
            {run.decisions.length ? (
              <div class="stack-sm">
                {run.decisions.map((d, i) => (
                  <DecisionBadge key={`${d.decision}-${i}`} decision={d} />
                ))}
              </div>
            ) : (
              <p class="muted small">No decisions were recorded for this run.</p>
            )}
          </Card>

          <Card title="Tools used">
            {run.tools.length ? (
              <ul role="list" class="lz-agent-tools">
                {run.tools.map((t) => (
                  <li key={t}>
                    <Badge appearance="outline">{t}</Badge>
                  </li>
                ))}
              </ul>
            ) : (
              <p class="muted small">No tool use was recorded.</p>
            )}
          </Card>

          <Card title="Artifacts" padded={!run.artifacts.length}>
            {run.artifacts.length ? (
              <List aria-label="Artifacts">
                {run.artifacts.map((a, i) => (
                  <ListItem key={`${a.label}-${i}`} title={a.label} leading={<Icon name="file" size={18} />} href={a.href ?? undefined} />
                ))}
              </List>
            ) : (
              <p class="muted small">This run produced no artifacts.</p>
            )}
          </Card>

          <Card title="Result" tone={run.status === 'failed' ? 'danger' : run.status === 'done' ? 'success' : 'neutral'}>
            <p class="lz-agent-result">{resultText(run)}</p>
          </Card>

          <div>
            <TechDetails items={run.tech} />
          </div>
        </>
      )}
    </div>
  );
}

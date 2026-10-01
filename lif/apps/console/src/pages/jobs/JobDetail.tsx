// One job: status in words, why it is waiting, progress, safe actions (§31). Raw engine states, ids and
// reasons live behind Technical details (§42, §79). Live through the SSE `jobs` event.
import { get } from '@/api/client';
import type { Job, JobAction, JobKind } from '@/api/contracts.gen';
import { can, useMe } from '@/api/session';
import { useResource } from '@/api/store';
import { usePageTitle } from '@/shell/usePageTitle';
import { Badge, Button, Card, FactList, HumanErrorCard, Icon, Progress, Skeleton, TechDetails, fmt, type FactItem, type Tone } from '@/ui';
import { useJobAction } from './useJobAction';
import './jobs.css';

const KIND: Record<JobKind, string> = {
  batch: 'Batch job',
  discovery: 'Model discovery',
  download: 'Model download',
  benchmark: 'Benchmark',
  evaluation: 'Model evaluation',
  backup: 'Backup',
  maintenance: 'Maintenance',
  agent: 'Background agent',
};

const TONE: Record<Job['status'], Tone> = {
  running: 'brand',
  queued: 'inactive',
  waiting: 'warning',
  paused: 'inactive',
  completed: 'success',
  failed: 'danger',
  cancelled: 'inactive',
};

/** What each action does, said on the button before it is pressed (§110). */
const CONSEQUENCE: Record<JobAction, string> = {
  pause: 'Running items finish; nothing new starts',
  resume: 'Continues when resources allow',
  cancel: 'Remaining items will not run',
  retry: 'Runs the failed work again',
};
const LABEL: Record<JobAction, string> = { pause: 'Pause', resume: 'Resume', cancel: 'Cancel job', retry: 'Retry' };

function facts(job: Job): FactItem[] {
  const items: FactItem[] = [{ label: 'Kind', value: KIND[job.kind] }];
  if (job.counts)
    items.push({
      label: 'Items',
      value: `${fmt.num(job.counts.done)} of ${fmt.num(job.counts.total)} done${job.counts.failed ? ` · ${fmt.num(job.counts.failed)} failed` : ''}`,
    });
  items.push({ label: 'Started', value: job.started_at ? fmt.ago(job.started_at) : 'Not started yet' });
  if (job.finished_at) items.push({ label: 'Finished', value: fmt.ago(job.finished_at) });
  if (job.started_at && job.finished_at) items.push({ label: 'Took', value: fmt.duration(job.finished_at - job.started_at) });
  if (job.owner) items.push({ label: 'Submitted by', value: job.owner });
  return items;
}

export default function JobDetail({ id }: { id?: string }) {
  const me = useMe();
  // preact-iso already decoded the route param; encode exactly once for the API path.
  const res = useResource(id ? `jobs/item/${id}` : null, () => get<Job>(`/api/jobs/${encodeURIComponent(id ?? '')}`), { refreshOn: ['jobs'] });
  const actions = useJobAction();
  const job = res.data;
  usePageTitle(job?.title ?? 'Job');
  const canControl = can(me.data, 'jobs.control');

  return (
    <div class="page">
      <a class="lz-crumb small" href="/jobs">
        <Icon name="chevron-left" size={16} />
        Jobs
      </a>

      {res.error && !job && <HumanErrorCard error={res.error} onRetry={() => void res.refresh()} />}
      {res.loading && (
        <div aria-busy="true" class="stack">
          <Skeleton height="2rem" width="60%" />
          <Skeleton lines={4} />
        </div>
      )}

      {job && (
        <>
          <header class="page-header">
            <div class="stack-sm">
              <h1>{job.title}</h1>
              <div class="row wrap">
                <Badge tone={TONE[job.status]}>{job.status_label || job.status}</Badge>
              </div>
            </div>
          </header>

          {job.reason && (
            <Card tone={job.status === 'failed' ? 'danger' : job.status === 'waiting' ? 'warning' : 'neutral'} title="Why">
              <p>{job.reason}</p>
              {job.resumes_automatically && <p class="muted small">Resumes automatically. Nothing to do.</p>}
            </Card>
          )}

          <Card>
            <div class="stack">
              {job.progress !== null && job.progress !== undefined && (
                <Progress value={job.progress} label={`${job.title} progress`} showValue tone={job.status === 'failed' ? 'danger' : 'brand'} />
              )}
              <FactList items={facts(job)} columns={2} />
            </div>
          </Card>

          {job.actions.length > 0 &&
            (canControl ? (
              <div class="row wrap">
                {job.actions.map((a) => (
                  <Button
                    key={a}
                    variant={a === 'cancel' ? 'danger' : 'secondary'}
                    subtitle={CONSEQUENCE[a]}
                    loading={actions.busy?.action === a}
                    disabled={!!actions.busy && actions.busy.action !== a}
                    onClick={() => actions.run(job, a)}
                  >
                    {LABEL[a]}
                  </Button>
                ))}
              </div>
            ) : (
              <p class="muted small">This session can view jobs but not pause or cancel them.</p>
            ))}

          <div>
            <TechDetails items={job.tech} />
          </div>
        </>
      )}
      {actions.dialog}
    </div>
  );
}

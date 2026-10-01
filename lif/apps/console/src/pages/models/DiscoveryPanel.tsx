// "Check for better models" (§28): the start action and the live progress panel, shared by the Models home and
// the Discovery page. Upstream truth shapes this: the controller's refresh call returns no run id and writes the
// stage counts only when a run finishes, so the panel shows an indeterminate bar with the stage names while a
// run is going and the real funnel counts afterwards. It never makes up a count.
import { useEffect, useState } from 'preact/hooks';
import { useLocation } from 'preact-iso/router';
import { post, toHumanError } from '@/api/client';
import type { DiscoveryRun, DiscoveryStage, DiscoveryState } from '@/api/contracts.gen';
import { invalidate, useResource } from '@/api/store';
import { Badge, Button, Card, Icon, Progress, TechDetails, cx, fmt, toast } from '@/ui';
import { KEY, fetchDiscovery, usePermReason } from './shared';

const STAGES: { key: DiscoveryStage['key']; label: string }[] = [
  { key: 'discovering', label: 'Discovering' },
  { key: 'filtering', label: 'Filtering' },
  { key: 'evaluating', label: 'Evaluating' },
  { key: 'benchmarking', label: 'Benchmarking' },
];

/** How long a requested check may stay invisible before we say so instead of spinning forever. */
const STALL_MS = 30_000;

export function upgradesLine(n: number): string {
  if (n <= 0) return 'No meaningful upgrades found';
  return `${fmt.num(n)} meaningful upgrade${n === 1 ? '' : 's'} found`;
}

/** Kinds of model a check can look for (controller discovery categories), in the order people think of them. */
export const DISCOVERY_KINDS: { value: string; label: string }[] = [
  { value: 'general', label: 'General' },
  { value: 'fast', label: 'Fast' },
  { value: 'coding', label: 'Coding' },
  { value: 'reasoning', label: 'Reasoning' },
  { value: 'vision', label: 'Vision' },
  { value: 'embedding', label: 'Embedding' },
  { value: 'reranking', label: 'Reranking' },
];

export interface DiscoveryStart {
  /** No categories (or an empty list) = every kind. */
  start: (categories?: string[]) => Promise<void>;
  starting: boolean;
  /** Why the button is disabled (permission, already running, turned off), or null. */
  blocked: string | null;
  /** The run this browser started, once it shows up (it may already be finished — runs can take seconds). */
  mine: DiscoveryRun | undefined;
  /** Requested, but no new run has appeared yet. */
  waiting: boolean;
  /** Requested more than STALL_MS ago and still nothing: discovery may be off upstream. */
  stalled: boolean;
}

/**
 * Starts a check and finds "our" run without a run id: remember which runs existed before the POST, and the
 * first run not in that set is the one we started. Skew-free, unlike comparing browser and server clocks.
 */
export function useDiscoveryStart(state: DiscoveryState | undefined): DiscoveryStart {
  const permReason = usePermReason('models.discover');
  const [starting, setStarting] = useState(false);
  const [baseline, setBaseline] = useState<{ ids: Set<string>; at: number } | null>(null);
  const [, tick] = useState(0);

  const runs = state ? [state.current, ...state.recent].filter((r): r is DiscoveryRun => !!r) : [];
  const mine = baseline ? runs.find((r) => !baseline.ids.has(r.id)) : undefined;
  const waiting = !!baseline && !mine;
  const stalled = waiting && Date.now() - baseline.at > STALL_MS;
  useResource(waiting && !stalled ? KEY.discovery : null, fetchDiscovery, { pollMs: 3000 });

  useEffect(() => {
    if (!waiting || stalled) return;
    const t = setTimeout(() => tick((n) => n + 1), STALL_MS + 500);
    return () => clearTimeout(t);
  }, [waiting, stalled]);

  const blocked =
    permReason ??
    (!state
      ? 'Checking discovery status…'
      : !state.enabled
        ? (state.disabled_reason ?? 'Checking for new models is turned off')
        : state.current?.status === 'running' || (waiting && !stalled)
          ? 'A check is already in progress'
          : null);

  const start = async (categories?: string[]) => {
    setStarting(true);
    const ids = new Set(runs.map((r) => r.id));
    const kinds = categories?.length ? categories : null;
    try {
      await post('/api/models/discovery', { categories: kinds });
      setBaseline({ ids, at: Date.now() });
      const what = kinds ? DISCOVERY_KINDS.filter((k) => kinds.includes(k.value)).map((k) => k.label.toLowerCase()).join(', ') : '';
      toast({ title: `Checking for better ${what ? `${what} ` : ''}models`, body: 'Progress shows here. Nothing is installed or switched by this check alone.', tone: 'info' });
      invalidate(KEY.discovery);
      invalidate('jobs');
    } catch (e) {
      const err = toHumanError(e);
      toast({ title: err.title, body: [err.impact, err.next_step].filter(Boolean).join(' '), tone: 'error' });
    } finally {
      setStarting(false);
    }
  };

  return { start, starting, blocked, mine, waiting, stalled };
}

export function CheckButton({ ds, size = 'md', categories }: { ds: DiscoveryStart; size?: 'md' | 'lg'; categories?: string[] }) {
  return (
    <Button
      variant="primary"
      icon="scout"
      size={size}
      loading={ds.starting}
      disabled={!!ds.blocked}
      onClick={() => void ds.start(categories)}
      subtitle={ds.blocked ?? 'Searches Hugging Face and screens new models'}
    >
      Check for better models
    </Button>
  );
}

function stageRows(run: DiscoveryRun | null): (DiscoveryStage & { label: string })[] {
  return STAGES.map((s) => {
    const got = run?.stages.find((x) => x.key === s.key);
    return got ? { ...got, label: got.label || s.label } : { key: s.key, label: s.label, count: null, done: false };
  });
}

/** The four funnel stages with their counts; a stage the run never reports shows a dash, not a number. */
export function StageList({ run }: { run: DiscoveryRun | null }) {
  const running = !run || run.status === 'running';
  return (
    <ol class="lz-disc-stages" aria-label="Discovery stages">
      {stageRows(run).map((s) => (
        <li key={s.key} class={cx('lz-disc-stage', s.done ? 'done' : running ? 'pending' : 'skipped')}>
          <Icon
            name={s.done ? 'success' : running ? 'clock' : 'unknown'}
            size={16}
            label={s.done ? 'Done' : running ? 'Not reported yet' : 'Not reached'}
            class={s.done ? 'lz-tone-success' : 'faint'}
          />
          <span class="grow">{s.label}</span>
          <span class="num muted small">{typeof s.count === 'number' ? fmt.num(s.count) : fmt.DASH}</span>
        </li>
      ))}
    </ol>
  );
}

function title(run: DiscoveryRun | null): string {
  if (!run || run.status === 'running') return 'Checking for better models…';
  if (run.status === 'failed') return 'The check failed';
  if (run.status === 'interrupted') return 'The check was interrupted';
  return run.summary || upgradesLine(run.upgrades);
}

export interface DiscoveryProgressProps {
  /** null = requested, waiting for the run to appear. */
  run: DiscoveryRun | null;
  stalled?: boolean;
  /** Offer "See candidates" when the run found upgrades (hidden on the Models home, where they're listed below). */
  candidatesLink?: boolean;
}

/** Discovering → Filtering → Evaluating → Benchmarking → "N meaningful upgrades found" (§28). */
export function DiscoveryProgress({ run, stalled, candidatesLink }: DiscoveryProgressProps) {
  const { route } = useLocation();
  const running = !run || run.status === 'running';
  const anyCount = !!run?.stages.some((s) => typeof s.count === 'number');
  const tone = !run || run.status === 'running' || run.status === 'succeeded' ? 'info' : 'warning';

  if (!run && stalled)
    return (
      <Card tone="warning" title="No check has started yet" level={2}>
        <p class="muted">
          Labzilla accepted the request, but no check has appeared after 30 seconds. Checking may be turned off in
          System → Settings, or the model controller may be busy or restarting.
        </p>
        <div class="row wrap lz-models-gap">
          <Button size="sm" icon="settings" onClick={() => route('/system/settings')}>
            Open settings
          </Button>
        </div>
      </Card>
    );

  return (
    <Card tone={tone} level={2} title={<span aria-live="polite">{title(run)}</span>} subtitle={run ? meta(run) : 'Starting…'}>
      <div class="stack-sm">
        {running && <Progress value={null} label="Discovery progress" size="sm" />}
        <StageList run={run} />
        {running && !anyCount && <p class="small muted">Counts appear when the check finishes; Labzilla doesn't report them mid-run yet.</p>}
        {run?.status === 'interrupted' && (
          <p class="small muted">It stopped before finishing, usually because the model controller restarted. Start a new check.</p>
        )}
        {run?.status === 'failed' && <p class="small muted">Nothing changed. The technical details say what failed; you can start a new check.</p>}
        {candidatesLink && run?.status === 'succeeded' && run.upgrades > 0 && (
          <div>
            <Button size="sm" variant="secondary" iconRight="arrow-right" onClick={() => route('/models#candidates')}>
              See candidates
            </Button>
          </div>
        )}
        {run && <TechDetails inline items={run.tech} triggerLabel="Internal steps" />}
      </div>
    </Card>
  );
}

function meta(run: DiscoveryRun) {
  const parts = [`Started ${fmt.ago(run.started_at)}`];
  if (run.finished_at) parts.push(`took ${fmt.duration(run.finished_at - run.started_at)}`);
  return (
    <span class="row wrap">
      <span>{parts.join(' · ')}</span>
      {run.categories.map((c) => (
        <Badge key={c} size="sm" appearance="outline">
          {c}
        </Badge>
      ))}
    </span>
  );
}

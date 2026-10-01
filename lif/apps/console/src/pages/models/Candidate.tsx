// Candidate comparison (/models/candidates/:id; §29, §41, §98): current vs candidate side by side with a verdict
// in words and icons (never color alone, §57), the recommendation, Jev's advice labelled as advice, and three
// actions: Ignore, Benchmark more, Canary. Ignore is a view preference on this device — the backend has no
// "ignore" operation and blocking is a release decision, so we don't pretend otherwise.
import { useState } from 'preact/hooks';
import { useLocation } from 'preact-iso/router';
import type { CandidateComparison, ComparisonRow } from '@/api/contracts.gen';
import { useMe } from '@/api/session';
import { usePageTitle } from '@/shell/usePageTitle';
import { Button, Card, HumanErrorCard, Icon, Select, Skeleton, StatusBadge, Table, TechDetails, cx, fmt, toast, type Column, type IconName } from '@/ui';
import { causeLine, previewPath, roleForAlias, roleHref, setIgnored, useComparison, useModelOps, useOverview, whyNot } from './shared';

const VERDICT: Record<ComparisonRow['verdict'], { icon: IconName; word: string }> = {
  better: { icon: 'success', word: 'Better' },
  worse: { icon: 'warning', word: 'Worse' },
  same: { icon: 'check', word: 'About the same' },
  unknown: { icon: 'unknown', word: 'Not measured' },
};

const RECOMMENDATION_TONE: Record<CandidateComparison['recommendation_code'], 'success' | 'info' | 'warning' | 'neutral'> = {
  canary: 'success',
  hold: 'info',
  reject: 'warning',
  benchmark_first: 'info',
  unknown: 'neutral',
};

const CANARY_SHARES = ['5', '10', '25', '50'] as const;
type Share = (typeof CANARY_SHARES)[number];

/** Units are whatever the server sends; format the common ones and pass the rest through. */
function value(v: number | null | undefined, unit: string): string {
  if (v === null || v === undefined) return fmt.DASH;
  if (unit === '%') return v <= 1 ? fmt.percent(v) : fmt.percent(v, { fromPct: true });
  if (unit === 'ms') return fmt.ms(v);
  if (unit === 'GB') return fmt.gb(v, 1);
  return unit ? `${fmt.num(v, Math.abs(v) < 10 ? 2 : 0)} ${unit}` : fmt.num(v, 2);
}

const COLUMNS: Column<ComparisonRow>[] = [
  {
    key: 'metric',
    header: 'Metric',
    primary: true,
    render: (r) => (
      <span class="lz-role-cell">
        <span>{r.metric}</span>
        <span class="lz-cmp-head">{r.better === 'higher' ? 'Higher is better' : 'Lower is better'}</span>
      </span>
    ),
  },
  { key: 'current', header: 'Current', align: 'end', render: (r) => <span class="num">{value(r.current, r.unit)}</span> },
  { key: 'candidate', header: 'Candidate', align: 'end', render: (r) => <span class="num">{value(r.candidate, r.unit)}</span> },
  {
    key: 'verdict',
    header: 'Candidate is',
    render: (r) => {
      const v = VERDICT[r.verdict];
      return (
        <span class="lz-role-cell">
          <span class={cx('lz-verdict', r.verdict)}>
            <Icon name={v.icon} size={14} />
            {v.word}
          </span>
          {r.note && <span class="lz-cmp-head">{r.note}</span>}
        </span>
      );
    },
  },
];

export default function Candidate({ id }: { id?: string }) {
  const res = useComparison(id);
  const overview = useOverview();
  const me = useMe();
  const ops = useModelOps();
  const { route } = useLocation();
  const [share, setShare] = useState<Share>('10');
  const c = res.data;
  usePageTitle(c ? `Compare ${c.candidate.name}` : 'Candidate');

  const back = (
    <a href="/models" class="small row">
      <Icon name="chevron-left" size={14} />
      Models
    </a>
  );
  if (res.loading)
    return (
      <div class="page">
        {back}
        <Skeleton height="28px" width="50%" />
        <Skeleton lines={7} />
      </div>
    );
  if (!c)
    return (
      <div class="page">
        {back}
        {res.error && <HumanErrorCard error={res.error} onRetry={() => void res.refresh()} />}
      </div>
    );

  const d = c.candidate;
  const role = roleForAlias(overview.data?.roles, c.role);
  const roleLabel = role?.label ?? c.role;
  const access = (perm: 'models.operate' | 'models.release') => (me.data === undefined ? 'Checking your access…' : whyNot(me.data, perm));

  // "Benchmark more" needs the files on disk; a shortlisted model offers Download instead, which is the honest next step.
  const canBench = d.actions.includes('benchmark');
  const canDownload = !canBench && d.actions.includes('download');
  const benchWhy =
    access('models.operate') ?? (canBench || canDownload ? null : `Not available while this model is ${d.state_label.toLowerCase()}`);
  const canaryWhy =
    access('models.release') ??
    (d.actions.includes('canary')
      ? null
      : c.recommendation_code === 'benchmark_first'
        ? 'Benchmark it first so the trial can be judged'
        : `Not available while this model is ${d.state_label.toLowerCase()}`);
  const percent = Number(share);
  // Nothing serves this role yet (the first vision model): there is no incumbent to share traffic with, so the
  // controller refuses a trial. The honest step is Promote, previewed and confirmed like everywhere else.
  const firstForRole = !c.incumbent;
  const promoteWhy =
    access('models.release') ??
    (d.actions.includes('promote')
      ? null
      : c.recommendation_code === 'benchmark_first'
        ? 'Benchmark it first so you know what you are promoting'
        : `Not available while this model is ${d.state_label.toLowerCase()}`);

  const ignore = () => {
    setIgnored(d.id, true);
    toast({
      title: 'Ignored for now',
      body: 'Hidden from the candidate list on this device. Nothing changed on Labzilla.',
      tone: 'info',
      action: { label: 'Undo', onClick: () => setIgnored(d.id, false) },
    });
    route('/models');
  };

  const incumbentLine = c.incumbent
    ? `Current: ${c.incumbent.name} · Candidate: ${d.name}`
    : 'Nothing serves this role yet, so there is nothing to compare against.';

  return (
    <div class="page">
      {back}
      <header class="page-header">
        <div>
          <h1>{d.name}</h1>
          <p>
            Candidate for {role ? <a href={roleHref(role.role)}>{roleLabel}</a> : roleLabel}
            {role && causeLine(role) ? ` · ${roleLabel} is currently ${role.state_label.toLowerCase()}` : ''}
          </p>
        </div>
        <StatusBadge health={d.health} label={d.state_label} />
      </header>

      <Card title="Recommendation" tone={RECOMMENDATION_TONE[c.recommendation_code]}>
        <div class="stack-sm">
          <p>{c.recommendation}</p>
          {c.advisory && (
            <p class="small muted row">
              <Icon name="decision" size={14} />
              <span>Jev’s advice (advisory only, not a decision): {c.advisory}</span>
            </p>
          )}
        </div>
      </Card>

      <Card title="Current vs candidate" subtitle={incumbentLine} padded={false}>
        <Table
          columns={COLUMNS}
          rows={c.rows}
          rowKey={(r) => r.metric}
          caption={`${c.incumbent?.name ?? 'Current'} compared with ${d.name}`}
          hideCaption
          density="normal"
          empty={<p class="lz-card-body small muted">No measurements to compare yet. Benchmark the candidate to fill this in.</p>}
        />
      </Card>

      <Card title="Actions">
        <div class="stack-sm">
          {firstForRole && (
            <p class="small muted">
              Nothing answers for {roleLabel} yet, so there is nothing to share a trial with. Promote makes this the {roleLabel} model; you can roll it
              back later.
            </p>
          )}
          {!firstForRole && <Select
            class="lz-canary-pick"
            label="Trial share"
            value={share}
            options={CANARY_SHARES.map((s) => ({ value: s, label: `${s}% of ${roleLabel} requests` }))}
            onChange={setShare}
            disabled={!!canaryWhy}
          />}
          <div class="lz-model-actions">
            <Button icon="eye" variant="ghost" onClick={ignore} subtitle="Hides it on this device; nothing changes on Labzilla">
              Ignore
            </Button>
            <Button
              icon={canDownload ? 'download' : 'benchmark'}
              disabled={!!benchWhy}
              loading={ops.busy === 'Benchmark more' || ops.busy === 'Download'}
              subtitle={
                benchWhy ??
                (canDownload ? 'Downloads the files so it can be benchmarked; uses disk and bandwidth' : 'Runs the quality suite again; takes a few minutes')
              }
              onClick={() =>
                void ops.run({
                  label: canDownload ? 'Download' : 'Benchmark more',
                  path: `/api/models/deployments/${encodeURIComponent(d.id)}/${canDownload ? 'download' : 'benchmark'}`,
                  done: canDownload ? 'Download started' : 'Benchmark started',
                })
              }
            >
              {canDownload ? 'Download' : 'Benchmark more'}
            </Button>
            {firstForRole ? (
              <Button
                icon="promote"
                variant="primary"
                disabled={!!promoteWhy}
                loading={ops.busy === 'Promote'}
                subtitle={promoteWhy ?? `Makes this the ${roleLabel} model`}
                onClick={() =>
                  void ops.run({
                    label: 'Promote',
                    path: `/api/models/deployments/${encodeURIComponent(d.id)}/promote`,
                    body: { alias: c.role },
                    preview: previewPath(d.id, 'promote', { alias: c.role }),
                    done: `${d.name} now answers for ${roleLabel}`,
                    tone: 'primary',
                  })
                }
              >
                Promote
              </Button>
            ) : (
            <Button
              icon="layers"
              variant="primary"
              disabled={!!canaryWhy}
              loading={ops.busy === 'Start trial'}
              subtitle={canaryWhy ?? `Sends ${percent}% of ${roleLabel} requests to this model`}
              onClick={() =>
                void ops.run({
                  label: 'Start trial',
                  path: `/api/models/deployments/${encodeURIComponent(d.id)}/canary`,
                  body: { alias: c.role, percent },
                  preview: previewPath(d.id, 'canary', { alias: c.role, percent }),
                  done: `Trial started: ${percent}% of ${roleLabel}`,
                  tone: 'primary',
                })
              }
            >
              Canary
            </Button>
            )}
          </div>
        </div>
      </Card>

      <TechDetails items={[...c.tech, ...d.tech]} />
      {ops.dialog}
    </div>
  );
}

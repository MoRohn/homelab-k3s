// Physical model detail (/models/deployments/:id; §27, §41, §97, §98). The advanced view behind "View physical
// model": every fact says where its number comes from (configured / estimated / measured / live), and every
// action is shown — when this session or the model's state can't do it, the button is disabled with the reason
// rather than hidden. Dangerous actions fetch the server's preview first and confirm through ConfirmDialog.
import { useState } from 'preact/hooks';
import type { BenchmarkSummary, MemoryBasis, ModelAction, ModelDeployment, ModelRole, Perm, PreviewAction, SpeedBasis } from '@/api/contracts.gen';
import { useMe } from '@/api/session';
import { usePageTitle } from '@/shell/usePageTitle';
import {
  ActivityRow,
  Badge,
  Button,
  Card,
  EmptyState,
  FactList,
  HumanErrorCard,
  Icon,
  List,
  Select,
  Skeleton,
  StatusBadge,
  Table,
  TechDetails,
  fmt,
  type Column,
  type FactItem,
  type IconName,
} from '@/ui';
import { previewPath, roleForAlias, roleHref, useDeployment, useModelOps, useOverview, useRefusal, whyNot, type ModelOp } from './shared';

const MEMORY_BASIS: Record<MemoryBasis, string> = {
  configured: 'configured budget, not measured',
  estimated: 'estimate from the model’s size',
  measured: 'measured',
};

const SPEED_BASIS: Record<SpeedBasis, string> = {
  measured: 'measured in the last benchmark',
  estimated: 'estimate, not measured yet',
  live: 'live, from recent requests',
};

const BENCH_COLUMNS: Column<BenchmarkSummary>[] = [
  { key: 'ts', header: 'When', primary: true, render: (b) => <time dateTime={fmt.iso(b.ts)}>{fmt.ago(b.ts)}</time> },
  { key: 'suite', header: 'Suite', render: (b) => b.suite },
  { key: 'quality', header: 'Quality', align: 'end', render: (b) => <span class="num">{fmt.percent(b.quality)}</span> },
  { key: 'ttft', header: 'First token', align: 'end', render: (b) => <span class="num">{fmt.ms(b.ttft_ms_p50)}</span> },
  {
    key: 'tps',
    header: 'Speed',
    align: 'end',
    render: (b) => <span class="num">{b.decode_tps_p50 == null ? fmt.DASH : `${fmt.num(b.decode_tps_p50, 1)} tok/s`}</span>,
  },
  {
    key: 'errors',
    header: 'Errors',
    align: 'end',
    render: (b) => <span class="num">{b.items ? `${fmt.num(b.errors)} of ${fmt.num(b.items)}` : fmt.num(b.errors)}</span>,
  },
];

interface ActionDef {
  key: ModelAction | 'rollback';
  label: string;
  icon: IconName;
  perm: Perm;
  /** Consequence line under the button (§110). */
  says: string;
  preview?: PreviewAction;
  tone?: 'danger' | 'primary';
}

const ACTIONS: ActionDef[] = [
  { key: 'test', label: 'Test', icon: 'test', perm: 'models.operate', says: 'Sends one short prompt and checks the reply' },
  { key: 'benchmark', label: 'Benchmark', icon: 'benchmark', perm: 'models.operate', says: 'Runs the quality suite; takes a few minutes and uses extra memory' },
  { key: 'load', label: 'Load', icon: 'play', perm: 'models.operate', says: 'Starts this model so it can answer', preview: 'load', tone: 'primary' },
  { key: 'unload', label: 'Unload', icon: 'stop', perm: 'models.operate', says: 'Stops this model and frees its memory', preview: 'unload' },
  { key: 'promote', label: 'Promote', icon: 'promote', perm: 'models.release', says: 'Makes this the main model for its role', preview: 'promote', tone: 'primary' },
  { key: 'rollback', label: 'Rollback', icon: 'rollback', perm: 'models.release', says: 'Returns the role to the model it used before', preview: 'rollback' },
];

function facts(d: ModelDeployment, latest: BenchmarkSummary | undefined, roles: ModelRole[] | undefined): FactItem[] {
  const roleLinks = d.roles.length
    ? d.roles.map((alias, i) => {
        const r = roleForAlias(roles, alias);
        return (
          <span key={alias}>
            {i > 0 && ', '}
            {r ? <a href={roleHref(r.role)}>{r.label}</a> : alias}
          </span>
        );
      })
    : 'Not serving a role';
  const runtime = [d.runtime, d.device, d.precision].filter(Boolean).join(' · ');
  const size = [d.params_b ? `${fmt.num(d.params_b, 1)}B parameters` : null, d.context ? `${fmt.num(d.context)} token context` : null]
    .filter(Boolean)
    .join(' · ');
  const speed =
    d.speed_tps == null && d.ttft_ms == null
      ? 'Not measured yet'
      : [d.speed_tps == null ? null : `${fmt.num(d.speed_tps, 1)} tokens/sec`, d.ttft_ms == null ? null : `first token in ${fmt.ms(d.ttft_ms)}`]
          .filter(Boolean)
          .join(' · ');
  return [
    { label: 'Role', value: roleLinks },
    { label: 'Physical model', value: <span class="mono small">{d.model_id || d.name}</span> },
    { label: 'Source', value: d.source || fmt.DASH },
    { label: 'Revision', value: <span class="mono small">{d.revision ? d.revision.slice(0, 12) : fmt.DASH}</span>, hint: d.revision || undefined },
    { label: 'Runtime', value: size ? `${runtime} (${size})` : runtime || fmt.DASH },
    { label: 'Memory', value: d.memory_gb == null ? 'Unknown' : fmt.gb(d.memory_gb, 1), hint: d.memory_gb == null ? undefined : MEMORY_BASIS[d.memory_basis] },
    { label: 'Speed', value: speed, hint: d.speed_basis && speed !== 'Not measured yet' ? SPEED_BASIS[d.speed_basis] : undefined },
    {
      label: 'Quality',
      value: d.quality == null ? 'Not benchmarked yet' : `${fmt.percent(d.quality)} of the quality suite passed`,
      hint: d.quality != null && latest?.items ? `${fmt.num(latest.items)} items, ${latest.suite}` : undefined,
    },
    { label: 'Usage', value: d.usage_24h == null ? 'Not tracked yet' : `${fmt.num(d.usage_24h)} requests in the last 24 h` },
    { label: 'Last evaluated', value: d.last_evaluated ? fmt.ago(d.last_evaluated) : 'Never benchmarked' },
    {
      label: 'Status',
      value: (
        <span class="row wrap">
          <StatusBadge health={d.health} label={d.state_label} size="sm" />
          {d.pinned && (
            <Badge size="sm" icon="pin" title="Kept even when cleaning up">
              Pinned
            </Badge>
          )}
          {d.blocked && (
            <Badge size="sm" tone="danger" icon="block" title="Labzilla won't suggest or deploy it">
              Blocked
            </Badge>
          )}
        </span>
      ),
    },
  ];
}

export default function DeploymentDetail({ id }: { id?: string }) {
  const res = useDeployment(id);
  const overview = useOverview();
  const me = useMe();
  const ops = useModelOps();
  const detail = res.data;
  const d = detail?.deployment;
  const [alias, setAlias] = useState<string | null>(null);
  usePageTitle(d ? d.name : 'Model');
  const rollbackAlias = alias ?? d?.roles[0] ?? null;
  const rollbackRefused = useRefusal(d && rollbackAlias ? previewPath(d.id, 'rollback', { alias: rollbackAlias }) : null);

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
        <Skeleton lines={8} />
      </div>
    );
  if (!detail || !d)
    return (
      <div class="page">
        {back}
        {res.error && <HumanErrorCard error={res.error} onRetry={() => void res.refresh()} />}
      </div>
    );

  const roles = overview.data?.roles;
  const target = alias ?? d.roles[0] ?? null;
  const targetRole = target ? roleForAlias(roles, target) : undefined;
  const latest = detail.benchmarks[0];

  /** Why this button is disabled, or null. Permission first: it's the reason the person can act on. */
  const reason = (a: ActionDef): string | null => {
    // Fixed first: the gateway routes by role, so no session or state can target one model.
    if (a.key === 'test') return 'Not available yet: requests are routed by role, so one model can’t be targeted. Use Benchmark.';
    const perm = me.data === undefined ? 'Checking your access…' : whyNot(me.data, a.perm);
    if (perm) return perm;
    if (a.key === 'rollback') {
      if (!target) return 'This model doesn’t serve a role, so there is nothing to roll back';
      if (!targetRole) return 'Loading roles…';
      return rollbackRefused;
    }
    if (a.key === 'unload' && d.roles.length && !d.actions.includes('unload'))
      return 'A role still uses this model: promote another model for that role first';
    if (!d.actions.includes(a.key)) return `Not available while this model is ${d.state_label.toLowerCase()}`;
    return null;
  };

  const opFor = (a: ActionDef): ModelOp => {
    if (a.key === 'rollback')
      return {
        label: a.label,
        path: `/api/models/roles/${encodeURIComponent(targetRole!.role)}/rollback`,
        preview: previewPath(d.id, 'rollback', { alias: target }),
        done: `${targetRole!.label} rolled back`,
      };
    const withAlias = a.key === 'promote' && target ? { alias: target } : {};
    return {
      label: a.label,
      path: `/api/models/deployments/${encodeURIComponent(d.id)}/${a.key}`,
      body: withAlias,
      preview: a.preview ? previewPath(d.id, a.preview, withAlias) : undefined,
      done:
        a.key === 'test' ? 'Test finished' : a.key === 'benchmark' ? 'Benchmark started' : `${a.label} requested for ${d.name}`,
      tone: a.tone,
    };
  };

  return (
    <div class="page">
      {back}
      <header class="page-header">
        <div>
          <h1>{d.name}</h1>
          <p>{d.state_label}</p>
        </div>
        <StatusBadge health={d.health} label={d.state_label} />
      </header>

      <Card title="Overview">
        <FactList columns={2} items={facts(d, latest, roles)} />
      </Card>

      <Card title="Actions" subtitle="Promote, unload and rollback show what changes before anything happens.">
        <div class="stack-sm">
          {d.roles.length > 1 && (
            <Select
              class="lz-canary-pick"
              label="Role for promote and rollback"
              value={target ?? ''}
              options={d.roles.map((a) => ({ value: a, label: roleForAlias(roles, a)?.label ?? a }))}
              onChange={setAlias}
            />
          )}
          <div class="lz-model-actions">
            {ACTIONS.map((a) => {
              const why = reason(a);
              return (
                <Button
                  key={a.key}
                  icon={a.icon}
                  variant={a.key === 'promote' ? 'primary' : 'secondary'}
                  disabled={!!why || (ops.busy !== null && ops.busy !== a.label)}
                  loading={ops.busy === a.label}
                  subtitle={why ?? a.says}
                  onClick={() => void ops.run(opFor(a))}
                >
                  {a.label}
                </Button>
              );
            })}
          </div>
        </div>
      </Card>

      <Card title="Benchmark history" subtitle="Measured on this machine. Newest first." padded={detail.benchmarks.length === 0}>
        <Table
          columns={BENCH_COLUMNS}
          rows={detail.benchmarks}
          rowKey={(b) => `${b.ts}-${b.suite}`}
          caption="Benchmark history"
          hideCaption
          empty={
            <EmptyState
              compact
              icon="benchmark"
              title="Not benchmarked yet"
              body="Benchmark measures quality and speed on this machine. Until then, speed and memory are estimates."
            />
          }
        />
      </Card>

      {detail.activity.length > 0 && (
        <Card title="Recent activity" padded={false}>
          <List aria-label="Recent activity for this model">
            {detail.activity.map((ev) => (
              <ActivityRow key={ev.id} event={ev} />
            ))}
          </List>
        </Card>
      )}

      <TechDetails items={d.tech} />
      {ops.dialog}
    </div>
  );
}

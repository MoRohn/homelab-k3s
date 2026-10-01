// Models home (§26): the portfolio by role, ROLE / MODEL / STATE, rather than every downloaded file. People
// pick capabilities, not physical models (§10); fallbacks and degraded roles are explained in plain words.
// Primary action: "Check for better models" with live progress (§28). Plumbing roles and physical models
// stay behind an "Advanced" disclosure (§97).
import { useLocation } from 'preact-iso/router';
import type { ModelRole, RoleKey } from '@/api/contracts.gen';
import { usePageTitle } from '@/shell/usePageTitle';
import {
  Badge,
  Button,
  Card,
  EmptyState,
  HumanErrorCard,
  Icon,
  List,
  ListItem,
  Skeleton,
  StatusBadge,
  Table,
  fmt,
  type Column,
} from '@/ui';
import { CheckButton, DiscoveryProgress, useDiscoveryStart, type DiscoveryStart } from './DiscoveryPanel';
import {
  ADVANCED_ROLES,
  MAIN_ROLES,
  candidateHref,
  causeLine,
  deploymentHref,
  roleForAlias,
  roleHref,
  setIgnored,
  useCandidates,
  useDiscovery,
  useIgnored,
  useOverview,
} from './shared';

const COLUMNS: Column<ModelRole>[] = [
  {
    key: 'role',
    header: 'Role',
    primary: true,
    width: '34%',
    render: (r) => (
      <span class="lz-role-cell">
        <span class="lz-role-name">{r.label}</span>
        <span class="lz-role-blurb">{r.blurb}</span>
      </span>
    ),
  },
  {
    key: 'model',
    header: 'Model',
    render: (r) => {
      const cause = causeLine(r);
      return (
        <span class="lz-role-cell">
          <span class={r.model_name ? undefined : 'muted'}>{r.model_name || 'Not deployed'}</span>
          {cause && (
            <span class="lz-role-cause">
              <Icon name="warning" size={14} />
              {cause}
            </span>
          )}
        </span>
      );
    },
  },
  {
    key: 'state',
    header: 'State',
    align: 'end',
    render: (r) => (
      <span class="lz-state-cell">
        <StatusBadge health={r.state} label={r.state_label || undefined} size="sm" />
        {r.canary && (
          <Badge size="sm" tone="info" icon="layers" title={`${r.canary.model} answers ${r.canary.percent}% of requests`}>
            Trial {r.canary.percent}%
          </Badge>
        )}
      </span>
    ),
  },
];

function byOrder(order: RoleKey[]) {
  return (a: ModelRole, b: ModelRole) => order.indexOf(a.role) - order.indexOf(b.role);
}

function RoleTable({ roles, caption }: { roles: ModelRole[]; caption: string }) {
  const { route } = useLocation();
  return <Table columns={COLUMNS} rows={roles} rowKey={(r) => r.role} caption={caption} hideCaption density="normal" onRowClick={(r) => route(roleHref(r.role))} />;
}

function Portfolio() {
  const overview = useOverview();
  if (overview.loading) return <Skeleton lines={6} />;
  if (overview.error && !overview.data) return <HumanErrorCard error={overview.error} onRetry={() => void overview.refresh()} />;
  const roles = overview.data?.roles ?? [];
  if (!roles.length)
    return (
      <EmptyState
        icon="models"
        title="No model roles reported"
        body="The model controller didn't list any roles. It may still be starting."
        action={{ label: 'Retry', icon: 'refresh', onClick: () => void overview.refresh() }}
      />
    );
  // Anything the server adds later that we don't know shows with the main roles rather than vanishing.
  const main = roles.filter((r) => !ADVANCED_ROLES.includes(r.role)).sort(byOrder(MAIN_ROLES));
  return <RoleTable roles={main} caption="Model roles" />;
}

/** The Vision row has no model: offer the one step that can change that (a check for vision models). Kept
 *  outside the table, whose rows are links: a button inside a link row is a nested control. */
function VisionGap({ ds }: { ds: DiscoveryStart }) {
  const overview = useOverview();
  const vision = overview.data?.roles.find((r) => r.role === 'vision');
  if (!vision || vision.cause !== 'not_deployed') return null;
  return (
    <div class="lz-vision-gap row wrap">
      <Icon name="image" size={16} />
      <span class="grow small">
        <strong>Vision</strong> <span class="muted">— no vision model is installed, so Ask can’t read images yet.</span>
      </span>
      <Button
        size="sm"
        variant="secondary"
        icon="scout"
        loading={ds.starting}
        disabled={!!ds.blocked}
        title={ds.blocked ?? 'Searches Hugging Face for vision models that fit this machine'}
        onClick={() => void ds.start(['vision'])}
      >
        Find a vision model
      </Button>
    </div>
  );
}

function Candidates() {
  const cands = useCandidates();
  const overview = useOverview();
  const ignored = useIgnored();
  if (cands.error && !cands.data) return <HumanErrorCard compact error={cands.error} onRetry={() => void cands.refresh()} />;
  const all = cands.data ?? [];
  const shown = all.filter((c) => !ignored.has(c.deployment.id));
  const hidden = all.length - shown.length;
  if (!all.length) return null;
  const unhide = () => {
    for (const c of all) setIgnored(c.deployment.id, false);
  };
  return (
    <Card
      id="candidates"
      title="Candidates"
      subtitle="Models found by a check that might do a role better. Compare before trying one."
      padded={false}
    >
      {shown.length > 0 && (
        <List aria-label="Model candidates">
          {shown.map((c) => {
            const role = c.role ? roleForAlias(overview.data?.roles, c.role) : undefined;
            return (
              <ListItem
                key={c.deployment.id}
                href={candidateHref(c.deployment.id)}
                title={c.deployment.name}
                subtitle={c.comparison_hint || c.deployment.state_label}
                meta={role ? `For ${role.label}` : undefined}
                trailing={
                  <span class="row">
                    <StatusBadge health={c.deployment.health} label={c.deployment.state_label} size="sm" />
                    <span class="lz-compare-link">Compare</span>
                  </span>
                }
              />
            );
          })}
        </List>
      )}
      {hidden > 0 && (
        <div class="row-between lz-card-body">
          <span class="small muted">
            {fmt.num(hidden)} ignored on this device
          </span>
          <Button size="sm" variant="ghost" onClick={unhide}>
            Show again
          </Button>
        </div>
      )}
    </Card>
  );
}

function Advanced() {
  const overview = useOverview();
  const roles = (overview.data?.roles ?? []).filter((r) => ADVANCED_ROLES.includes(r.role)).sort(byOrder(ADVANCED_ROLES));
  const deployments = overview.data?.deployments ?? [];
  return (
    <details class="lz-advanced">
      <summary>Advanced: supporting roles and physical models</summary>
      <div class="stack">
        {roles.length > 0 && (
          <section class="stack-sm" aria-labelledby="adv-roles">
            <h2 id="adv-roles" class="section-title">
              Supporting roles
            </h2>
            <RoleTable roles={roles} caption="Supporting roles" />
          </section>
        )}
        <section class="stack-sm" aria-labelledby="adv-physical">
          <h2 id="adv-physical" class="section-title">
            Physical models in use or on standby
          </h2>
          {deployments.length ? (
            <Card padded={false}>
              <List aria-label="Physical models">
                {deployments.map((d) => (
                  <ListItem
                    key={d.id}
                    href={deploymentHref(d.id)}
                    title={d.name}
                    subtitle={[d.runtime, d.params_b ? `${fmt.num(d.params_b, 1)}B parameters` : null].filter(Boolean).join(' · ')}
                    meta={d.roles.length ? `Serves ${d.roles.map((a) => roleForAlias(overview.data?.roles, a)?.label ?? a).join(', ')}` : undefined}
                    trailing={<StatusBadge health={d.health} label={d.state_label} size="sm" />}
                  />
                ))}
              </List>
            </Card>
          ) : (
            <p class="small muted">{overview.data ? 'No physical models reported.' : 'Loading…'}</p>
          )}
        </section>
      </div>
    </details>
  );
}

function LastCheck() {
  const disc = useDiscovery();
  const last = disc.data?.recent[0];
  if (disc.error && !disc.data) return <p class="small muted">Couldn't load the last check: {disc.error.title}.</p>;
  if (!disc.data) return null;
  return (
    <p class="small muted row wrap">
      <Icon name="history" size={14} />
      <span>{last ? `Last check ${fmt.ago(last.finished_at ?? last.started_at)}: ${last.summary || 'no summary'}.` : 'No check has run yet.'}</span>
      <a href="/models/discovery">History</a>
      {disc.data.enabled && disc.data.note && <span>· {disc.data.note}</span>}
    </p>
  );
}

export default function Models() {
  usePageTitle('Models');
  const disc = useDiscovery();
  const ds = useDiscoveryStart(disc.data);
  const live = ds.mine ?? disc.data?.current ?? (ds.waiting ? null : undefined);
  return (
    <div class="page">
      <header class="page-header">
        <div>
          <h1>Models</h1>
          <p>Labzilla sends each request to a role. You choose roles, not model files.</p>
        </div>
        <CheckButton ds={ds} />
      </header>
      {live !== undefined ? <DiscoveryProgress run={live} stalled={ds.stalled} /> : <LastCheck />}
      <Portfolio />
      <VisionGap ds={ds} />
      <Candidates />
      <Advanced />
    </div>
  );
}

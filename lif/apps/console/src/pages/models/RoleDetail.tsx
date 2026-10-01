// Role detail (/models/roles/:role; §10, §27, §41, §98): what the role is for, which model answers right now
// and why, the fallback chain in words, any trial (canary), and the one release action that belongs to a role:
// Rollback, behind the server's preview and a typed confirmation. "View physical model" is the advanced path.
import type { ModelRole, RoleCause } from '@/api/contracts.gen';
import { usePageTitle } from '@/shell/usePageTitle';
import { Button, Card, FactList, HumanErrorCard, Icon, Skeleton, StatusBadge, TechDetails, cx } from '@/ui';
import { useLocation } from 'preact-iso/router';
import { ROLE_HELP, ROLLBACK_EXEMPT, deploymentHref, previewPath, useModelOps, usePermReason, useRefusal, useRole } from './shared';

/** What a cause means for the person asking, and what happens next — never "connection refused" (§81, §82). */
const CAUSE_HELP: Record<RoleCause, { meaning: string; next: string }> = {
  canary: {
    meaning: 'A trial model is answering a share of this role’s requests.',
    next: 'Compare its results before promoting it, or roll the trial back.',
  },
  primary_unavailable: {
    meaning: 'The usual model isn’t responding, so a backup answers instead. Replies may be less capable.',
    next: 'Labzilla tries the usual model first on every request and goes back to it as soon as it responds.',
  },
  below_quality_floor: {
    meaning: 'The largest model running right now is smaller than this role expects, so replies may be less capable.',
    next: 'This clears on its own once a model of the expected size is running again.',
  },
  not_deployed: {
    meaning: 'No model is installed for this role yet.',
    next: 'Run a check for better models to look for one.',
  },
  shed_by_memory_guard: {
    meaning: 'This role’s model was paused to free memory for the primary workload.',
    next: 'Memory protection starts it again automatically when enough memory is free.',
  },
  yielded_to_primary: {
    meaning: 'Labzilla switched to a faster model while the primary workload is running.',
    next: 'It switches back automatically when the primary workload finishes.',
  },
  unknown: {
    meaning: 'Labzilla couldn’t tell why this role isn’t on its usual model.',
    next: 'The technical details show the raw reason.',
  },
};

function causeHelp(r: ModelRole) {
  if (!r.cause) return null;
  const help = CAUSE_HELP[r.cause];
  // A check can look for vision models (with their image projector); "Find a vision model" below starts one.
  if (r.cause === 'not_deployed' && r.role === 'vision')
    return { ...help, next: 'Check for vision models to find one that fits this machine; you choose whether to install it.' };
  return help;
}

/** "Labzilla tries Qwen3 4B first. If it isn't available, it uses Qwen3 1.7B." */
function chainSentence(chain: string[]): string {
  const [first, ...rest] = chain;
  if (!first) return 'No models are configured for this role.';
  if (!rest.length) return `Only ${first} can answer for this role. There is no backup.`;
  return `Labzilla tries ${first} first. If it isn’t available, it uses ${rest.join(', then ')}.`;
}

function Chain({ r }: { r: ModelRole }) {
  return (
    <Card title="Fallback chain" subtitle={chainSentence(r.chain)}>
      {r.chain.length > 0 && (
        <ol class="lz-chain" aria-label="Models in fallback order">
          {r.chain.map((name, i) => {
            const serving = name === r.model_name;
            return (
              <li key={`${name}-${i}`} class={cx(serving && 'serving')}>
                <span class="lz-chain-step" aria-hidden="true">
                  {i + 1}
                </span>
                <span class="grow">{name}</span>
                {serving && <span class="small muted">Answering now</span>}
              </li>
            );
          })}
        </ol>
      )}
    </Card>
  );
}

export default function RoleDetail({ role }: { role?: string }) {
  const res = useRole(role);
  const ops = useModelOps();
  const { route } = useLocation();
  const releaseWhy = usePermReason('models.release');
  const r = res.data;
  usePageTitle(r ? `${r.label} role` : 'Model role');
  // No history, or still on its first version: say so on the button instead of failing after the click.
  const rollbackRefused = useRefusal(
    r?.served_by && !ROLLBACK_EXEMPT.includes(r.role) ? previewPath(r.served_by, 'rollback', { alias: r.alias }) : null,
  );

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
        <Skeleton height="28px" width="40%" />
        <Skeleton lines={5} />
      </div>
    );
  if (!r)
    return (
      <div class="page">
        {back}
        {res.error && <HumanErrorCard error={res.error} onRetry={() => void res.refresh()} />}
      </div>
    );

  const help = causeHelp(r);
  const rollbackWhy =
    releaseWhy ??
    (ROLLBACK_EXEMPT.includes(r.role)
      ? 'Auto has no model of its own; roll back the role it uses instead'
      : !r.served_by
        ? 'No model serves this role, so there is nothing to roll back'
        : rollbackRefused);

  return (
    <div class="page">
      {back}
      <header class="page-header">
        <div>
          <h1>{r.label}</h1>
          <p>{r.blurb}</p>
        </div>
        <StatusBadge health={r.state} label={r.state_label || undefined} />
      </header>

      <Card title="What this role is for">
        <p>{ROLE_HELP[r.role]}</p>
      </Card>

      <Card
        title="Answering now"
        tone={help && r.cause !== 'canary' ? 'warning' : 'neutral'}
        actions={
          r.served_by ? (
            <Button size="sm" variant="ghost" iconRight="arrow-right" onClick={() => route(deploymentHref(r.served_by!))}>
              View physical model
            </Button>
          ) : r.role === 'vision' && r.cause === 'not_deployed' ? (
            <Button size="sm" variant="secondary" icon="scout" onClick={() => route('/models/discovery?category=vision')}>
              Find a vision model
            </Button>
          ) : undefined
        }
      >
        <div class="stack-sm">
          <FactList
            items={[
              { label: 'Model', value: r.model_name || 'Not deployed' },
              { label: 'Status', value: <StatusBadge health={r.state} label={r.state_label || undefined} size="sm" /> },
              ...(r.cause_label ? [{ label: 'Why', value: r.cause_label }] : []),
            ]}
          />
          {help && (
            <div class="stack-sm">
              <p>{help.meaning}</p>
              <p class="small muted">{help.next}</p>
            </div>
          )}
        </div>
      </Card>

      {r.canary && (
        <Card title="Trial in progress" tone="info">
          <p>
            {r.canary.model} answers {r.canary.percent}% of {r.label} requests
            {r.chain[0] ? `; the rest go to ${r.chain[0]}` : ''}. Labzilla compares the two before anything is promoted.
          </p>
        </Card>
      )}

      <Chain r={r} />

      <Card title="Actions" subtitle="You'll see exactly what changes before anything happens.">
        <div class="lz-model-actions">
          <Button
            icon="rollback"
            variant="secondary"
            disabled={!!rollbackWhy}
            loading={ops.busy === 'Roll back'}
            subtitle={rollbackWhy ?? `Returns ${r.label} to the model it used before`}
            onClick={() =>
              void ops.run({
                label: 'Roll back',
                path: `/api/models/roles/${encodeURIComponent(r.role)}/rollback`,
                preview: previewPath(r.served_by!, 'rollback', { alias: r.alias }),
                done: `${r.label} rolled back`,
              })
            }
          >
            Roll back
          </Button>
        </div>
      </Card>

      <TechDetails items={[{ label: 'alias', value: r.alias }, ...(r.physical_model ? [{ label: 'physical model', value: r.physical_model }] : []), ...r.tech]} />
      {ops.dialog}
    </div>
  );
}

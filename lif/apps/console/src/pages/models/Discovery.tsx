// Discovery (/models/discovery; §28): the check in progress (or the one just started) and the recent checks with
// their funnel counts. Per-run internals (raw funnel keys, timings) stay behind "Internal steps".
import { useLocation } from 'preact-iso/router';
import type { DiscoveryRun, Health } from '@/api/contracts.gen';
import { usePageTitle } from '@/shell/usePageTitle';
import { Button, Card, EmptyState, HumanErrorCard, Icon, Skeleton, StatusBadge, TechDetails, fmt } from '@/ui';
import { CheckButton, DiscoveryProgress, StageList, upgradesLine, useDiscoveryStart } from './DiscoveryPanel';
import { useDiscovery } from './shared';

const RUN_STATUS: Record<DiscoveryRun['status'], { health: Health; label: string }> = {
  running: { health: 'busy', label: 'Running' },
  succeeded: { health: 'healthy', label: 'Finished' },
  failed: { health: 'attention', label: 'Failed' },
  interrupted: { health: 'paused', label: 'Interrupted' },
};

function RunRow({ run }: { run: DiscoveryRun }) {
  const s = RUN_STATUS[run.status];
  const headline = run.status === 'succeeded' ? run.summary || upgradesLine(run.upgrades) : run.summary || s.label;
  return (
    <li class="lz-li">
      <details class="lz-advanced lz-card-body">
        <summary>
          <span class="grow row wrap">
            <span class="lz-role-name">{headline}</span>
            <StatusBadge health={s.health} label={s.label} size="sm" />
          </span>
          <time class="small muted num" dateTime={fmt.iso(run.started_at)}>
            {fmt.ago(run.started_at)}
            {run.finished_at ? ` · ${fmt.duration(run.finished_at - run.started_at)}` : ''}
          </time>
        </summary>
        <div class="stack-sm">
          {run.categories.length > 0 && <p class="small muted">Looked at: {run.categories.join(', ')}</p>}
          <StageList run={run} />
          <TechDetails inline items={run.tech} triggerLabel="Internal steps" />
        </div>
      </details>
    </li>
  );
}

export default function Discovery() {
  usePageTitle('Check for better models');
  const { route } = useLocation();
  const disc = useDiscovery();
  const ds = useDiscoveryStart(disc.data);
  const state = disc.data;
  const live = ds.mine ?? state?.current ?? (ds.waiting ? null : undefined);
  const recent = (state?.recent ?? []).filter((r) => r.id !== live?.id);

  return (
    <div class="page">
      <a href="/models" class="small row">
        <Icon name="chevron-left" size={14} />
        Models
      </a>
      <header class="page-header">
        <div>
          <h1>Check for better models</h1>
          <p>Labzilla searches Hugging Face, screens what it finds, and benchmarks the most promising models on this machine. Trying one is always a separate step.</p>
        </div>
        <CheckButton ds={ds} />
      </header>

      {state && !state.enabled && (
        <Card tone="warning" title="Checking for new models is turned off">
          <div class="stack-sm">
            <p>{state.disabled_reason ?? 'Discovery is disabled in the model controller’s settings.'}</p>
            <div>
              <Button size="sm" icon="settings" onClick={() => route('/system/settings')}>
                Open settings
              </Button>
            </div>
          </div>
        </Card>
      )}

      {state?.enabled && state.note && (
        <p class="small row" role="note">
          <Icon name="info" size={14} />
          <span>{state.note}</span>
        </p>
      )}

      {live !== undefined && <DiscoveryProgress run={live} stalled={ds.stalled} candidatesLink />}

      <section class="stack-sm" aria-labelledby="recent-checks">
        <h2 id="recent-checks" class="section-title">
          Recent checks
        </h2>
        {disc.loading ? (
          <Skeleton lines={4} />
        ) : disc.error && !state ? (
          <HumanErrorCard error={disc.error} onRetry={() => void disc.refresh()} />
        ) : recent.length ? (
          <Card padded={false}>
            <ul class="lz-list divided" aria-label="Recent checks">
              {recent.map((r) => (
                <RunRow key={r.id} run={r} />
              ))}
            </ul>
          </Card>
        ) : (
          live === undefined && (
            <EmptyState
              icon="scout"
              title="No checks yet"
              body="A check looks for newer models that could do a role better. It doesn't change anything on its own."
              action={ds.blocked ? undefined : { label: 'Check for better models', icon: 'scout', onClick: () => void ds.start() }}
            />
          )
        )}
      </section>
    </div>
  );
}

// Services (§38, §42, §82): each platform service in human health words with what it means and what
// to do. Recovery offers only what the backend can really do: "Check again" (re-probe, system.safe).
// Restart and "use fallback" have no console endpoint yet, so they are not offered — never kubectl.
import { useState } from 'preact/hooks';
import { useLocation } from 'preact-iso/router';
import { get, post } from '@/api/client';
import type { AlertsResponse, Health, ServiceHealth } from '@/api/contracts.gen';
import { can, useMe } from '@/api/session';
import { invalidate, useResource } from '@/api/store';
import { Button, Card, EmptyState, HumanErrorCard, Icon, List, ListItem, SEVERITY, Skeleton, StatusBadge, TechDetails, fmt } from '@/ui';
import { act, byHealth } from './shared';

/** Busy and paused are normal operation (§39); these are the states worth a look. */
const NEEDS_LOOK = new Set<Health>(['offline', 'attention', 'degraded']);

function ServiceRow({ svc, canRetry }: { svc: ServiceHealth; canRetry: boolean }) {
  const { route } = useLocation();
  const [busy, setBusy] = useState(false);
  const retry = async () => {
    setBusy(true);
    const ok = await act(() => post(`/api/system/services/${encodeURIComponent(svc.key)}/retry`), `Checked ${svc.name} again`, 'Its status updates as soon as the result is in.');
    if (ok) {
      invalidate('system/services');
      invalidate('system/status');
    }
    setBusy(false);
  };
  const offersRetry = svc.actions.includes('retry');
  const offersLogs = svc.actions.includes('view_logs');
  return (
    <li class="lz-sys-service">
      <div class="row-between wrap">
        <h3 class="lz-sys-service-name">{svc.name}</h3>
        <StatusBadge health={svc.health} size="sm" />
      </div>
      {svc.summary && <p>{svc.summary}</p>}
      {svc.impact && (
        <p class="muted small">
          <strong>Impact:</strong> {svc.impact}
        </p>
      )}
      <div class="row wrap">
        {offersRetry &&
          (canRetry ? (
            // A service that needs a look gets the full button with its consequence; on a healthy one it's a quiet
            // ghost button (ten identical "Re-checks it now" lines were the loudest thing on the tab).
            <Button
              size="sm"
              variant={NEEDS_LOOK.has(svc.health) ? 'secondary' : 'ghost'}
              icon="refresh"
              loading={busy}
              onClick={() => void retry()}
              subtitle={NEEDS_LOOK.has(svc.health) ? 'Re-checks it now; nothing restarts' : undefined}
              title="Re-checks it now; nothing restarts"
            >
              Check again
            </Button>
          ) : (
            <span class="muted small">Checking again needs a session allowed to run safe recovery.</span>
          ))}
        {offersLogs && (
          <Button size="sm" variant="ghost" icon="file" onClick={() => route(`/system/logs?q=${encodeURIComponent(svc.name)}`)}>
            Related events
          </Button>
        )}
        <TechDetails items={svc.tech} title={`${svc.name}: technical details`} />
      </div>
    </li>
  );
}

function Alerts() {
  const alerts = useResource<AlertsResponse>('system/alerts', () => get<AlertsResponse>('/api/system/alerts'), { refreshOn: ['status'], maxAgeMs: 15_000 });
  if (alerts.loading) return <Skeleton lines={2} />;
  if (!alerts.data) return alerts.error ? <HumanErrorCard error={alerts.error} onRetry={() => void alerts.refresh()} compact /> : null;
  const { alerts: firing, available, reason } = alerts.data;
  if (!available) return <p class="muted small">Alerts aren't available right now{reason ? `: ${reason}` : '.'}</p>;
  if (!firing.length) return <p class="muted small">No alerts firing.</p>;
  return (
    <List aria-label="Firing alerts">
      {firing.map((a) => {
        const s = SEVERITY[a.severity];
        return (
          <ListItem
            key={a.id}
            leading={<Icon name={s.icon} size={18} label={s.label} class={`lz-tone-${s.tone}`} />}
            title={a.summary || a.name}
            meta={a.since ? `since ${fmt.ago(a.since)}` : undefined}
            trailing={<TechDetails items={a.tech} title={`${a.name}: technical details`} triggerLabel="Details" />}
          />
        );
      })}
    </List>
  );
}

export function ServicesTab() {
  const me = useMe();
  const services = useResource<ServiceHealth[]>('system/services', () => get<ServiceHealth[]>('/api/system/services'), { refreshOn: ['status'] });
  const canRetry = can(me.data, 'system.safe');

  let body;
  if (services.loading) body = <Skeleton lines={6} />;
  else if (!services.data) body = services.error ? <HumanErrorCard error={services.error} onRetry={() => void services.refresh()} /> : null;
  else if (!services.data.length)
    body = (
      <EmptyState
        icon="server"
        title="No service reports yet"
        body="Labzilla hasn't finished its first check of the platform services."
        action={{ label: 'Check again', onClick: () => void services.refresh(), icon: 'refresh' }}
        compact
      />
    );
  else {
    const list = byHealth(services.data);
    const bad = list.filter((s) => NEEDS_LOOK.has(s.health)).length;
    body = (
      <div class="stack-sm">
        <p class="muted">
          {bad === 0 ? `All ${list.length} services are working.` : `${bad} of ${list.length} services need a look. They are listed first.`}
        </p>
        <ul role="list" class="lz-sys-services">
          {list.map((s) => (
            <ServiceRow key={s.key} svc={s} canRetry={canRetry} />
          ))}
        </ul>
      </div>
    );
  }

  return (
    <div class="stack">
      <Card title="Services">{body}</Card>
      <Card title="Alerts" subtitle="Raised by the monitoring stack">
        <Alerts />
      </Card>
    </div>
  );
}

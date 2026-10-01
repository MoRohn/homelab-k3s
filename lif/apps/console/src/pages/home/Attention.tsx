// Home's attention strip: rendered only when something may need the user (§39) — pending approvals
// answered inline (§40), the fallback explanation with a safe recovery (§81, §82), and the other
// actionable notifications. Nothing here for normal autonomy, so a healthy Home stays calm.
import type { ComponentChildren } from 'preact';
import { useState } from 'preact/hooks';
import { get, post } from '@/api/client';
import type { Approval, HumanError, Notification, SystemStatus } from '@/api/contracts.gen';
import { can, useMe } from '@/api/session';
import { invalidate, useResource } from '@/api/store';
import { act } from '@/pages/system/shared';
import { ApprovalCard, HumanErrorCard, Icon, List, ListItem, SEVERITY } from '@/ui';

/** Notification kinds that already have their own treatment in this strip. */
const OWN_TREATMENT = new Set<Notification['kind']>(['fallback', 'approval']);
const SHOWN_APPROVALS = 2;

function Approvals({ pending }: { pending: number }) {
  const me = useMe();
  const approvals = useResource<Approval[]>('approvals', () => get<Approval[]>('/api/approvals'), { refreshOn: ['approval'] });
  const [busy, setBusy] = useState<string | null>(null);
  const open = (approvals.data ?? []).filter((a) => a.status === 'pending');
  if (!open.length) {
    if (approvals.error) return <HumanErrorCard error={approvals.error} onRetry={() => void approvals.refresh()} compact />;
    return null;
  }

  const answer = async (a: Approval, value: string) => {
    setBusy(`${a.id}:${value}`);
    if (await act(() => post(`/api/approvals/${encodeURIComponent(a.id)}`, { answer: value }), 'Answer recorded', a.title)) {
      invalidate('approvals');
      invalidate('system/status');
    }
    setBusy(null);
  };

  // Pairings need devices.manage; reviews and gated actions need approvals.answer (server enforces both).
  const allowed = (a: Approval) => can(me.data, a.kind === 'device_pairing' ? 'devices.manage' : 'approvals.answer');
  const more = Math.max(open.length, pending) - SHOWN_APPROVALS;
  return (
    <div class="stack-sm">
      {open.slice(0, SHOWN_APPROVALS).map((a) => (
        <ApprovalCard
          key={a.id}
          approval={a}
          busy={busy?.startsWith(`${a.id}:`) ? busy.slice(a.id.length + 1) : null}
          canAnswer={allowed(a)}
          whyNot={a.kind === 'device_pairing' ? 'Approving a new device needs an admin session.' : 'This session cannot answer approvals.'}
          onAnswer={(v) => answer(a, v)}
        />
      ))}
      {more > 0 && (
        <a href="/agents" class="small">
          {more} more waiting for approval
        </a>
      )}
    </div>
  );
}

/** "Default model unavailable — Labzilla is using the fast fallback model…" with [Retry default] [View details]. */
function Fallback({ note }: { note: Notification }) {
  const me = useMe();
  const [retrying, setRetrying] = useState(false);
  const canRetry = can(me.data, 'system.safe');
  const details = note.href || '/models';
  const error: HumanError = {
    title: note.title,
    impact: note.body,
    next_step: canRetry
      ? 'Retry checks the default model again; nothing restarts. Labzilla also switches back on its own once the default is healthy.'
      : 'Labzilla switches back on its own once the default model is healthy.',
    actions: [
      ...(canRetry ? [{ label: retrying ? 'Checking…' : 'Retry default', action: 'retry-default' }] : []),
      { label: 'View details', action: details },
    ],
    tech: [],
  };
  // HumanErrorCard routes '/…' actions itself; only the client verb lands here.
  // Re-probe the gateway (system.safe): refreshes alias availability, so a recovered default is picked
  // up without waiting for the next poll. There is no "force switch back" upstream (brief §5).
  const onAction = async (action: string) => {
    if (action !== 'retry-default' || retrying) return;
    setRetrying(true);
    if (await act(() => post('/api/system/services/gateway/retry'), 'Checked the default model again', 'Status updates as soon as the result is in.'))
      invalidate('system/status');
    setRetrying(false);
  };
  return <HumanErrorCard error={error} onAction={(a) => void onAction(a)} />;
}

/** True when the strip has something to show (a fallback, a pending approval, or an actionable notice). */
export function needsAttention(status: SystemStatus): boolean {
  return (
    status.approvals_pending > 0 ||
    status.notifications.some((n) => n.kind === 'fallback' || (!OWN_TREATMENT.has(n.kind) && n.severity !== 'success'))
  );
}

/** `whenCalm`: what to show when nothing needs the user (the cockpit says so; elsewhere nothing renders). */
export function Attention({ status, whenCalm = null }: { status: SystemStatus; whenCalm?: ComponentChildren }) {
  const fallback = status.notifications.find((n) => n.kind === 'fallback');
  const others = status.notifications.filter((n) => !OWN_TREATMENT.has(n.kind) && n.severity !== 'success');
  if (!needsAttention(status)) return <>{whenCalm}</>;
  return (
    <section class="lz-home-attention stack-sm" aria-labelledby="home-attention-title">
      <h2 id="home-attention-title" class="section-title">
        Needs your attention
      </h2>
      {fallback && <Fallback note={fallback} />}
      {status.approvals_pending > 0 && <Approvals pending={status.approvals_pending} />}
      {others.length > 0 && (
        <List aria-label="Other notices">
          {others.map((n) => {
            const s = SEVERITY[n.severity];
            return (
              <ListItem
                key={n.id}
                leading={<Icon name={s.icon} size={18} label={s.label} class={`lz-tone-${s.tone}`} />}
                title={n.title}
                subtitle={n.body}
                href={n.href ?? undefined}
              />
            );
          })}
        </List>
      )}
    </section>
  );
}

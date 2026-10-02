// Earn: the earning system (namespace earn) in plain language. The lead sentence first (§110), then why it
// isn't trading, then the safety controls. Pause/Stop/Kill only reduce risk and run on the click (§41);
// Resume is admin-only behind the server's preview and a typed confirmation. Money is shown exactly as the
// ledger reports it; reward ESTIMATES are always labelled and kept apart from credited rewards.
// Live: polls every 15 s (no SSE event covers the earn service).
import { useState } from 'preact/hooks';
import { get, post, toHumanError } from '@/api/client';
import type { ActionPreview, EarnMarket, EarnOverview, EarnVenue, HumanError } from '@/api/contracts.gen';
import { can, useMe } from '@/api/session';
import { invalidate, useResource } from '@/api/store';
import { usePageTitle } from '@/shell/usePageTitle';
import { Badge, Button, Card, ConfirmDialog, EmptyState, FactList, HumanErrorCard, Skeleton, StatusBadge, Table,
  TechDetails, fmt, toast } from '@/ui';
import './earn.css';

const KEY = 'earn/overview';
type Action = 'pause' | 'stop' | 'kill' | 'resume' | 'close_only';

const usd = (v: string | null | undefined) => (v == null ? fmt.DASH : `$${v}`);
const ratio = (v: number | null | undefined) => (v == null ? fmt.DASH : fmt.percent(v, { digits: 2 }));
const seconds = (v: number | null | undefined) => (v == null ? fmt.DASH : `${fmt.num(v, 1)} s`);

function useControl(refresh: () => void) {
  const [busy, setBusy] = useState<string | null>(null);
  const [pending, setPending] = useState<{ action: Action; scope: string; preview: ActionPreview } | null>(null);
  const [error, setError] = useState<HumanError | null>(null);

  const send = async (action: Action, scope: string, confirm?: string) => {
    setBusy(`${action}:${scope}`);
    setError(null);
    try {
      const res = await post<{ message?: string | null }>('/api/earn/control', { action, scope, confirm });
      setPending(null);
      toast({ title: 'Done', body: res?.message ?? undefined, tone: 'success' });
      invalidate(KEY);
      refresh();
    } catch (e) {
      const err = toHumanError(e);
      if (pending) setError(err);
      else toast({ title: err.title, body: err.impact || err.next_step, tone: 'error' });
    } finally {
      setBusy(null);
    }
  };

  const run = async (action: Action, scope: string) => {
    if (action === 'pause' || action === 'stop' || action === 'kill') return send(action, scope);
    try {
      const preview = await get<ActionPreview>(`/api/earn/control/preview?action=${action}&scope=${encodeURIComponent(scope)}`);
      setPending({ action, scope, preview });
    } catch (e) {
      const err = toHumanError(e);
      toast({ title: err.title, body: err.impact || err.next_step, tone: 'error' });
    }
  };

  const dialog = pending ? (
    <ConfirmDialog
      open
      preview={pending.preview}
      confirmLabel={pending.action === 'resume' ? 'Resume' : 'Allow closing only'}
      tone="primary"
      busy={busy === `${pending.action}:${pending.scope}`}
      error={error}
      onCancel={() => {
        setPending(null);
        setError(null);
      }}
      onConfirm={(typed) => void send(pending.action, pending.scope, typed)}
    />
  ) : null;
  return { run, busy, dialog };
}

function Tile({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div class="lz-earn-tile">
      <span class="lz-earn-tile-label">{label}</span>
      <span class="lz-earn-tile-value num">{value}</span>
      {hint && <span class="lz-hint">{hint}</span>}
    </div>
  );
}

function Controls({ o, ctl, safeOk, adminOk }: { o: EarnOverview; ctl: ReturnType<typeof useControl>; safeOk: boolean; adminOk: boolean }) {
  const g = o.controls.find((c) => c.scope === 'global');
  const b = (a: Action) => ctl.busy === `${a}:global`;
  return (
    <Card title="Safety controls" subtitle="Pause, stop and kill only reduce risk and run at once. Kill also cancels every resting order.">
      <div class="stack-sm">
        <p class="small">
          Everything: <b>{g ? g.state : 'running'}</b>
          {g?.reason ? ` — ${g.reason} (${g.actor}, ${fmt.ago(g.updated_at)})` : ''}
        </p>
        {safeOk ? (
          <div class="row lz-earn-actions">
            <Button icon="pause" loading={b('pause')} onClick={() => void ctl.run('pause', 'global')}>Pause all</Button>
            <Button icon="stop" loading={b('stop')} onClick={() => void ctl.run('stop', 'global')}>Stop all</Button>
            <Button icon="block" variant="danger" loading={b('kill')} onClick={() => void ctl.run('kill', 'global')}>
              Kill: cancel all orders
            </Button>
            {adminOk && (
              <Button icon="play" variant="ghost" onClick={() => void ctl.run('resume', 'global')}>Resume…</Button>
            )}
          </div>
        ) : (
          <p class="small muted">This session can't change trading state.</p>
        )}
        {o.controls.filter((c) => c.scope !== 'global').length > 0 && (
          <Table
            caption="Other scopes"
            rows={o.controls.filter((c) => c.scope !== 'global')}
            rowKey={(c) => c.scope}
            columns={[
              { key: 'scope', header: 'Scope', render: (c) => c.scope, primary: true },
              { key: 'state', header: 'State', render: (c) => c.state },
              { key: 'why', header: 'Why', render: (c) => `${c.reason} (${c.actor})` },
              {
                key: 'act', header: '', align: 'end',
                render: (c) => (adminOk && c.state !== 'running' ? (
                  <Button size="sm" variant="ghost" onClick={() => void ctl.run('resume', c.scope)}>Resume…</Button>
                ) : null),
              },
            ]}
          />
        )}
      </div>
    </Card>
  );
}

function VenueTable({ o, ctl, safeOk }: { o: EarnOverview; ctl: ReturnType<typeof useControl>; safeOk: boolean }) {
  return (
    <Card title="Venues" padded={false}>
      <Table<EarnVenue>
        caption="Venues"
        hideCaption
        rows={o.venues}
        rowKey={(v) => v.venue}
        empty={<EmptyState title="No venues configured" />}
        columns={[
          { key: 'v', header: 'Venue', primary: true, render: (v) => <span class="row"><StatusBadge health={v.health} size="sm" label={v.label} /></span> },
          { key: 'm', header: 'Markets', render: (v) => fmt.num(v.markets), align: 'end' },
          { key: 'age', header: 'Data age', render: (v) => seconds(v.feed_age_s), align: 'end' },
          { key: 'skew', header: 'Clock skew', render: (v) => seconds(v.clock_skew_s), align: 'end', hideOnCompact: true },
          { key: 'c', header: 'Committed', render: (v) => usd(v.committed_usd), align: 'end' },
          { key: 'cash', header: 'Paper cash', render: (v) => usd(v.paper_cash_usd), align: 'end', hideOnCompact: true },
          { key: 'u', header: 'Unresolved orders', render: (v) => fmt.num(v.unresolved_orders), align: 'end' },
          {
            key: 'a', header: '', align: 'end',
            render: (v) => (safeOk ? (
              <Button size="sm" variant="ghost" icon="pause" loading={ctl.busy === `pause:venue:${v.venue}`}
                onClick={() => void ctl.run('pause', `venue:${v.venue}`)}>Pause</Button>
            ) : null),
          },
        ]}
      />
      {o.venues.some((v) => v.last_error) && (
        <ul class="lz-earn-list small">
          {o.venues.filter((v) => v.last_error).map((v) => <li key={v.venue}>{v.label}: {v.last_error}</li>)}
        </ul>
      )}
    </Card>
  );
}

function MarketTable({ markets }: { markets: EarnMarket[] }) {
  return (
    <Card title={`Markets · ${markets.length}`} subtitle="What the liquidity engine is watching and exactly why it quotes or abstains." padded={false}>
      <Table<EarnMarket>
        caption="Tracked markets"
        hideCaption
        rows={markets}
        rowKey={(m) => `${m.venue}:${m.market}`}
        empty={<EmptyState title="No markets tracked yet" body="Markets appear after the first incentive-program scan." />}
        columns={[
          { key: 'm', header: 'Market', primary: true, render: (m) => (
            <span class="stack-sm"><span>{m.title || m.market}</span><span class="xsmall muted mono">{m.market}</span></span>) },
          { key: 'q', header: 'Bid / ask', render: (m) => `${m.best_bid ?? fmt.DASH} / ${m.best_ask ?? fmt.DASH}`, align: 'end' },
          { key: 's', header: 'Quoting', render: (m) => (m.quoting
            ? <Badge tone="success" size="sm">{m.quotes.join(' · ') || 'Yes'}</Badge>
            : <Badge tone="inactive" size="sm">Abstaining</Badge>) },
          { key: 'r', header: 'Why', render: (m) => <span class="small">{m.reasons.slice(0, 3).join('; ') || fmt.DASH}</span> },
          { key: 'p', header: 'Program', render: (m) => <span class="xsmall">{m.program ?? 'None'}</span>, hideOnCompact: true },
        ]}
      />
    </Card>
  );
}

export default function Earn() {
  usePageTitle('Earn');
  const me = useMe();
  const res = useResource(KEY, () => get<EarnOverview>('/api/earn'), { pollMs: 15000 });
  const ctl = useControl(() => void res.refresh());
  const o = res.data;
  const safeOk = can(me.data, 'system.safe');
  const adminOk = can(me.data, 'system.settings');
  const all = o?.pnl.find((p) => p.engine === 'all');

  return (
    <div class="page page-wide">
      <header class="page-header">
        <div>
          <h1>Earn</h1>
          <p class="row">
            {o && <StatusBadge health={o.health} size="sm" />}
            <span>{o ? o.lead : 'Liquidity incentives, verified arbitrage and Synth forecasting — paper first.'}</span>
          </p>
        </div>
      </header>

      {res.error && !o && <HumanErrorCard error={res.error} onRetry={() => void res.refresh()} />}
      {res.loading && (
        <div aria-busy="true" class="stack">
          <Skeleton height="3.5rem" />
          <Skeleton height="1.2rem" lines={4} />
        </div>
      )}

      {o && !o.available && (
        <Card tone={o.configured ? 'danger' : 'info'}>
          <EmptyState title={o.lead} body={o.reason ?? undefined} />
          {o.tech.length > 0 && <TechDetails items={o.tech} inline />}
        </Card>
      )}

      {o && o.available && (
        <div class="stack">
          {o.not_trading.length > 0 && (
            <Card title="Why it isn't trading" tone="warning">
              <ul class="lz-earn-list">{o.not_trading.map((r, i) => <li key={i}>{r}</li>)}</ul>
            </Card>
          )}

          <Controls o={o} ctl={ctl} safeOk={safeOk} adminOk={adminOk} />

          <Card title="Results" subtitle={`${o.book === 'live' ? 'Live' : 'Paper'} book. Realized figures come from the ledger; marks use executable depth.`}>
            <div class="lz-earn-tiles">
              <Tile label="Net, incremental" value={usd(all?.net_incremental_usd)} hint="Trading, rebates and credited rewards minus fees and new costs" />
              <Tile label="Net, fully loaded" value={usd(all?.net_fully_loaded_usd)} hint="Also charges allocated infrastructure" />
              <Tile label="Marked, unrealized" value={usd(o.marked_unrealized_usd)} hint="Open positions at executable prices" />
              <Tile label="Rewards credited" value={usd(o.rewards_credited_usd)} hint="Paid by venues" />
              <Tile label="Rewards estimated" value={usd(o.rewards_estimated_usd)}
                hint={`Estimate, not cash · band ${usd(o.rewards_estimated_low_usd)}–${usd(o.rewards_estimated_high_usd)}`} />
              <Tile label="Committed collateral" value={usd(o.committed_usd)} hint="Open orders + positions, worst case" />
            </div>
            <p class="xsmall muted">{o.rewards_note}</p>
          </Card>

          <VenueTable o={o} ctl={ctl} safeOk={safeOk} />
          <MarketTable markets={o.markets} />

          {o.trips.length > 0 && (
            <Card title={`Safety stops · ${o.trips.length}`} subtitle="Automatic stops hold until their cause clears or an operator clears them." padded={false}>
              <Table
                caption="Safety stops"
                hideCaption
                rows={o.trips}
                rowKey={(t) => `${t.scope}:${t.trigger}`}
                columns={[
                  { key: 's', header: 'Scope', primary: true, render: (t) => t.scope },
                  { key: 't', header: 'Trigger', render: (t) => t.trigger.replace(/_/g, ' ') },
                  { key: 'e', header: 'Effect', render: (t) => t.effect },
                  { key: 'd', header: 'Detail', render: (t) => <span class="small">{t.detail}</span> },
                  { key: 'w', header: 'Since', render: (t) => fmt.ago(t.since), hideOnCompact: true },
                ]}
              />
            </Card>
          )}

          <div class="lz-earn-grid">
            <Card title="Arbitrage" subtitle={`${o.arbitrage_total} candidates recorded, including rejections`}>
              <FactList items={o.arbitrage_counts.map((f) => ({ label: f.label.replace(/_/g, ' '), value: f.value }))} />
              {o.arbitrage_recent.length > 0 && (
                <ul class="lz-earn-list small">
                  {o.arbitrage_recent.slice(0, 5).map((c, i) => (
                    <li key={i}><b>{c.classification.replace(/_/g, ' ')}</b> · {c.group} · edge {usd(c.net_edge_usd)} — {c.reason}</li>
                  ))}
                </ul>
              )}
            </Card>
            <Card title="Synth forecasting (SN50)">
              {o.synth.available ? (
                <FactList items={[
                  { label: 'Mode', value: o.synth.mode },
                  { label: 'Champion model', value: o.synth.champion_model ?? fmt.DASH },
                  { label: 'Registration', value: o.synth.registration_state.replace(/_/g, ' '), hint: 'Only the operator can approve a registration spend' },
                  { label: 'Miner', value: o.synth.miner_enabled ? 'Enabled' : 'Off' },
                  ...o.synth.last_benchmark.map((f) => ({ label: f.label, value: f.value })),
                ]} />
              ) : <p class="small muted">{o.synth.reason}</p>}
              {o.synth.notes.length > 0 && <ul class="lz-earn-list xsmall muted">{o.synth.notes.map((n, i) => <li key={i}>{n}</li>)}</ul>}
            </Card>
            <Card title="Learning" subtitle="Challengers must pass replay, holdout and a bounded canary before promotion.">
              <FactList items={[...o.learning.champions.map((f) => ({ label: f.label, value: f.value })),
                ...o.learning.by_status.map((f) => ({ label: `Experiments ${f.label.replace(/_/g, ' ')}`, value: f.value }))]} />
              {o.learning.champions.length + o.learning.by_status.length === 0 && <p class="small muted">No experiments yet.</p>}
            </Card>
            <Card title="Availability this month" subtitle="This service is measured separately from venue and data outages.">
              <FactList items={o.availability.map((a) => ({ label: a.label, value: ratio(a.ratio), hint: `${fmt.num(a.observed_minutes)} min observed` }))} />
              {o.availability.length === 0 && <p class="small muted">Not measured yet.</p>}
            </Card>
          </div>

          <Card title="Startup checks and live permission">
            <ul class="lz-earn-list small">
              {o.boot.map((b) => <li key={b.step}>{b.ok ? '✓' : '✗'} {b.label}{b.detail ? ` — ${b.detail}` : ''}</li>)}
            </ul>
            <ul class="lz-earn-list small">
              {o.live.map((l) => (
                <li key={`${l.engine}:${l.venue}`}>
                  Live {l.engine} on {l.venue}: {l.allowed ? 'allowed' : `not allowed — ${l.reasons.slice(0, 3).join('; ')}`}
                </li>
              ))}
            </ul>
            <p class="xsmall muted">
              Modes: {o.modes.map((m) => `${m.label} ${m.value}`).join(' · ')} · CPU {fmt.num(o.costs.cpu_seconds, 0)} s · memory{' '}
              {fmt.num(o.costs.max_rss_mib, 0)} MiB · electricity {o.costs.electricity}
            </p>
            {o.loop_errors.length > 0 && <p class="small">Loop errors: {o.loop_errors.join('; ')}</p>}
          </Card>

          <TechDetails items={o.tech} intro="Raw identifiers as reported by the earning service" />
          {ctl.dialog}
        </div>
      )}
    </div>
  );
}

// Settings (§97, §110): operator switches, each stating its consequence before it is flipped; the
// dangerous ones go through ConfirmDialog (§41). Advanced controls are read-only views behind one
// collapsed disclosure, so the default stays minimal. Theme and density are per-browser conveniences.
import { useState } from 'preact/hooks';
import { useObservable } from '@/api/observable';
import { get, post } from '@/api/client';
import type { ActionPreview, SettingItem, SettingsView, TechDetail } from '@/api/contracts.gen';
import { can, useMe } from '@/api/session';
import { useSystemStatus } from '@/api/status';
import { invalidate, useResource } from '@/api/store';
import { setTheme, theme, type ThemePref } from '@/shell/theme';
import { Card, ConfirmDialog, FactList, HumanErrorCard, Select, Skeleton, Switch, TechDetails, fmt } from '@/ui';
import { density, setDensity, type DensityPref } from './prefs';
import { act } from './shared';

const KEY = 'system/settings';

const THEMES: { value: ThemePref; label: string }[] = [
  { value: 'system', label: 'Match this device' },
  { value: 'dark', label: 'Dark' },
  { value: 'light', label: 'Light' },
];

const DENSITIES: { value: DensityPref; label: string }[] = [
  { value: 'normal', label: 'Normal' },
  { value: 'compact', label: 'Compact' },
];

/** status.tech rows the backend may carry for the read-only advanced views; matched by label words. */
const ROUTING = /threshold|routing|route/i;
const DECISION = /decision|jev|provider/i;

function preview(item: SettingItem, next: boolean): ActionPreview {
  return {
    title: `${next ? 'Turn on' : 'Turn off'} ${item.label.toLowerCase()}?`,
    changes: [item.consequence || item.description],
    interrupts: [],
    rollback: `You can turn it ${next ? 'off' : 'on'} again here at any time.`,
    confirm: 'simple',
  };
}

function ReadOnly({ items, empty }: { items: TechDetail[]; empty: string }) {
  if (!items.length) return <p class="muted small">{empty}</p>;
  return <FactList items={items.map((t) => ({ label: t.label, value: <span class="mono">{t.value}</span> }))} />;
}

function Advanced({ numbers }: { numbers: SettingItem[] }) {
  const status = useSystemStatus();
  const tech = status.data?.tech ?? [];
  return (
    <details class="lz-sys-advanced">
      <summary>Advanced controls</summary>
      <div class="stack">
        <p class="muted small">Read-only views for operators. Changing these is not available from the console.</p>
        <section class="stack-sm" aria-labelledby="adv-models">
          <h3 id="adv-models" class="section-title">Physical models</h3>
          <p class="small">
            Every model the platform runs, with revision, runtime and memory: <a href="/models">Models → physical models</a>
          </p>
        </section>
        <section class="stack-sm" aria-labelledby="adv-routing">
          <h3 id="adv-routing" class="section-title">Routing thresholds</h3>
          <ReadOnly items={tech.filter((t) => ROUTING.test(t.label))} empty="Not reported by the console yet." />
        </section>
        <section class="stack-sm" aria-labelledby="adv-gpu">
          <h3 id="adv-gpu" class="section-title">GPU reservation</h3>
          {numbers.length ? (
            <FactList
              items={numbers.map((n) => ({
                label: n.label,
                value: <span class="num">{n.value == null ? fmt.DASH : `${fmt.num(Number(n.value))}${n.unit ? ` ${n.unit}` : ''}`}</span>,
                hint: n.description || undefined,
              }))}
            />
          ) : (
            <p class="muted small">Not reported by the console yet.</p>
          )}
        </section>
        <section class="stack-sm" aria-labelledby="adv-decision">
          <h3 id="adv-decision" class="section-title">Decision Fabric providers</h3>
          <ReadOnly items={tech.filter((t) => DECISION.test(t.label))} empty="Not reported by the console yet." />
        </section>
        <TechDetails items={tech} inline triggerLabel="All status details" />
      </div>
    </details>
  );
}

function Toggle({ item, allowed }: { item: SettingItem; allowed: boolean }) {
  const settings = useResource<SettingsView>(KEY, () => get<SettingsView>('/api/system/settings'));
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState<boolean | null>(null);
  const checked = item.value === true;

  const apply = async (next: boolean) => {
    setBusy(true);
    const prev = settings.data;
    // Optimistic: safe switches flip at once; the next revalidation (or a failure) restores the truth.
    settings.mutate((cur) => ({ ...(cur ?? { available: true, items: [] }), items: (cur?.items ?? []).map((i) => (i.key === item.key ? { ...i, value: next } : i)) }));
    const ok = await act(() => post(`/api/system/settings`, { key: item.key, value: next }), `${item.label}: ${next ? 'on' : 'off'}`);
    if (!ok && prev) settings.mutate(prev);
    invalidate(KEY);
    invalidate('system/status');
    setBusy(false);
    setConfirming(null);
  };

  return (
    <div class="lz-sys-setting">
      <Switch
        label={item.label}
        description={item.consequence || item.description}
        checked={checked}
        busy={busy}
        disabled={!allowed}
        onChange={(next) => (item.dangerous ? setConfirming(next) : void apply(next))}
      />
      {!allowed && <p class="lz-hint">This session can't change this setting.</p>}
      {item.dangerous && confirming !== null && (
        <ConfirmDialog
          open
          preview={preview(item, confirming)}
          confirmLabel={confirming ? 'Turn on' : 'Turn off'}
          busy={busy}
          onConfirm={() => void apply(confirming)}
          onCancel={() => setConfirming(null)}
        />
      )}
    </div>
  );
}

export function SettingsTab() {
  const me = useMe();
  const settings = useResource<SettingsView>(KEY, () => get<SettingsView>('/api/system/settings'), { refreshOn: ['status'], maxAgeMs: 15_000 });
  const themePref = useObservable(theme);
  const densityPref = useObservable(density);

  let body;
  let numbers: SettingItem[] = [];
  if (settings.loading) body = <Skeleton lines={6} />;
  else if (!settings.data) body = settings.error ? <HumanErrorCard error={settings.error} onRetry={() => void settings.refresh()} /> : null;
  else {
    const { items, available, reason } = settings.data;
    numbers = items.filter((i) => i.kind === 'number');
    const toggles = items.filter((i) => i.kind === 'toggle');
    body = (
      <div class="stack-sm">
        {!available && <p class="muted">Settings can't be changed right now{reason ? `: ${reason}` : '.'}</p>}
        {toggles.map((i) => (
          <Toggle key={i.key} item={i} allowed={available && can(me.data, i.perm)} />
        ))}
      </div>
    );
  }

  return (
    <div class="stack">
      <Card title="Operations" subtitle="Each switch says what it does before you flip it">
        {body}
      </Card>
      <Card title="Display" subtitle="Saved in this browser only">
        <div class="lz-sys-prefs">
          <Select label="Theme" value={themePref} options={THEMES} onChange={setTheme} />
          <Select label="Density" value={densityPref} options={DENSITIES} onChange={setDensity} hint="Compact fits more rows on screen." />
        </div>
      </Card>
      <Card>
        <Advanced numbers={numbers} />
      </Card>
    </div>
  );
}

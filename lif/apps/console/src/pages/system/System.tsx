// System: infrastructure in human terms, for when something needs a closer look (§35) — not needed for
// normal operation. Tabs are URL state (/system/<tab>, or /system?tab=<tab> from older links) so every
// view is linkable from the command bar, the palette and Home.
import { useLocation } from 'preact-iso/router';
import { usePageTitle } from '@/shell/usePageTitle';
import { Tabs, type TabItem } from '@/ui';
import { useSystemStatus } from '@/api/status';
import { ComputeTab } from './ComputeTab';
import { LogsTab } from './LogsTab';
import { NetworkTab } from './NetworkTab';
import { ServicesTab } from './ServicesTab';
import { SettingsTab } from './SettingsTab';
import { StorageTab } from './StorageTab';
import './system.css';

type TabId = 'compute' | 'services' | 'storage' | 'network' | 'logs' | 'settings';

const TABS: (TabItem<TabId> & { title: string })[] = [
  { id: 'compute', label: 'Compute', icon: 'gpu', title: 'Compute' },
  { id: 'services', label: 'Services', icon: 'server', title: 'Services' },
  { id: 'storage', label: 'Storage', icon: 'database', title: 'Storage' },
  { id: 'network', label: 'Network', icon: 'network', title: 'Network' },
  { id: 'logs', label: 'Logs', icon: 'file', title: 'Logs' },
  { id: 'settings', label: 'Settings', icon: 'settings', title: 'Settings' },
];

function isTab(v: string | undefined): v is TabId {
  return !!v && TABS.some((t) => t.id === v);
}

export default function System({ tab }: { tab?: string }) {
  const { query, route } = useLocation();
  const status = useSystemStatus();
  const fromQuery = query.tab;
  const current: TabId = isTab(tab) ? tab : isTab(fromQuery) ? fromQuery : 'compute';
  const meta = TABS.find((t) => t.id === current) ?? TABS[0]!;
  usePageTitle(`System · ${meta.title}`);

  // Badge only what needs a look: services not healthy (§39); no counts for normal operation.
  const unhealthy = status.data?.services.filter((s) => s.health === 'offline' || s.health === 'attention' || s.health === 'degraded').length ?? 0;
  const tabs = TABS.map((t) => (t.id === 'services' && unhealthy ? { ...t, badge: unhealthy } : t));

  return (
    <div class="page lz-sys">
      <header class="page-header">
        <div>
          <h1>System</h1>
          <p>Compute, services, storage, network and logs. Not needed for normal use.</p>
        </div>
      </header>
      <Tabs
        tabs={tabs}
        value={current}
        onChange={(id) => route(`/system/${id}`, true)}
        ariaLabel="System sections"
        idBase="sys"
        class="lz-sys-tabs"
      />
      <div role="tabpanel" id={`sys-panel-${current}`} aria-labelledby={`sys-tab-${current}`} class="lz-sys-panel">
        {current === 'compute' && <ComputeTab />}
        {current === 'services' && <ServicesTab />}
        {current === 'storage' && <StorageTab />}
        {current === 'network' && <NetworkTab />}
        {current === 'logs' && <LogsTab initialQuery={query.q ?? ''} />}
        {current === 'settings' && <SettingsTab />}
      </div>
    </div>
  );
}

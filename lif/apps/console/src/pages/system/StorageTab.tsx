// Storage: plain numbers (§56). Each row says what it measures — registry bookkeeping is labelled as
// such, never presented as a disk measurement.
import { get } from '@/api/client';
import type { StorageItem, StorageSummary } from '@/api/contracts.gen';
import { useResource } from '@/api/store';
import { Card, EmptyState, HumanErrorCard, Skeleton, StatusBadge, Table, TechDetails, fmt, type Column } from '@/ui';

function used(i: StorageItem): string {
  if (i.used_gb == null) return fmt.DASH;
  return i.total_gb == null ? fmt.gb(i.used_gb, 1) : `${fmt.num(i.used_gb, 1)} / ${fmt.gb(i.total_gb, 1)}`;
}

const COLUMNS: Column<StorageItem>[] = [
  { key: 'label', header: 'What', primary: true, render: (i) => i.label },
  { key: 'health', header: 'Status', render: (i) => <StatusBadge health={i.health} size="sm" /> },
  { key: 'used', header: 'Used', align: 'end', render: (i) => <span class="num">{used(i)}</span> },
  { key: 'count', header: 'Items', align: 'end', render: (i) => <span class="num">{fmt.num(i.count)}</span> },
  { key: 'note', header: 'Note', hideOnCompact: false, render: (i) => <span class="muted small">{i.note}</span> },
  { key: 'tech', header: 'Details', render: (i) => <TechDetails items={i.tech} title={`${i.label}: technical details`} triggerLabel="Details" /> },
];

export function StorageTab() {
  const storage = useResource<StorageSummary>('system/storage', () => get<StorageSummary>('/api/system/storage'), { maxAgeMs: 30_000 });
  let body;
  if (storage.loading) body = <Skeleton lines={4} />;
  else if (!storage.data) body = storage.error ? <HumanErrorCard error={storage.error} onRetry={() => void storage.refresh()} /> : null;
  else if (!storage.data.items.length)
    body = (
      <EmptyState
        icon="database"
        title="No storage figures yet"
        body={storage.data.note || 'Labzilla has no storage measurements to show.'}
        action={{ label: 'Check again', onClick: () => void storage.refresh(), icon: 'refresh' }}
        compact
      />
    );
  else
    body = (
      <div class="stack-sm">
        <Table columns={COLUMNS} rows={storage.data.items} rowKey={(i) => i.key} caption="Storage" hideCaption density="normal" />
        {storage.data.note && <p class="muted small">{storage.data.note}</p>}
      </div>
    );
  return <Card title="Storage">{body}</Card>;
}

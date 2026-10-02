// Logs (§80): Errors / Warnings / Relevant events first, with search. Raw container logs are not
// readable through the console yet; that is said plainly, and the terminal command lives only inside
// the advanced technical drawer — never as primary guidance (§82).
import { useEffect, useState } from 'preact/hooks';
import { get, qs } from '@/api/client';
import type { ActivityEvent, LogsResponse } from '@/api/contracts.gen';
import { useResource } from '@/api/store';
import {
  ActivityRow,
  Badge,
  Button,
  Card,
  Drawer,
  EmptyState,
  FactList,
  HumanErrorCard,
  Input,
  List,
  SEVERITY,
  Skeleton,
  Tabs,
  TechDetails,
  fmt,
  safeHref,
} from '@/ui';

type Level = 'error' | 'warning' | 'all';

const LEVELS: { id: Level; label: string }[] = [
  { id: 'error', label: 'Errors' },
  { id: 'warning', label: 'Warnings' },
  { id: 'all', label: 'Relevant events' },
];

const EMPTY: Record<Level, string> = {
  error: 'No errors recorded recently.',
  warning: 'No warnings recorded recently.',
  all: 'Nothing has happened recently.',
};

/** For the advanced drawer only: how an operator would read raw logs from a terminal on the host. */
const RAW_LOG_COMMANDS = [
  { label: 'Controller', value: 'kubectl -n ai-system logs deploy/controller --tail=200' },
  { label: 'AI gateway', value: 'kubectl -n ai-system logs deploy/gateway --tail=200' },
  { label: 'Batch', value: 'kubectl -n ai-system logs deploy/batch --tail=200' },
];

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

function EventDrawer({ event, onClose }: { event: ActivityEvent | null; onClose: () => void }) {
  const s = event ? SEVERITY[event.severity] : null;
  const link = safeHref(event?.href);
  return (
    <Drawer open={!!event} onClose={onClose} title={event?.title ?? ''} subtitle={event ? fmt.ago(event.ts) : undefined}>
      {event && s && (
        <div class="stack">
          <FactList
            items={[
              { label: 'Kind', value: <Badge tone={s.tone} icon={s.icon} size="sm">{s.label}</Badge> },
              { label: 'Area', value: event.category },
              { label: 'When', value: <time dateTime={fmt.iso(event.ts)}>{new Date(event.ts * 1000).toLocaleString()}</time> },
              ...(event.detail ? [{ label: 'Detail', value: event.detail }] : []),
            ]}
          />
          {link && (
            <a href={link} onClick={onClose}>
              Open the related item
            </a>
          )}
          <TechDetails items={event.tech} inline />
        </div>
      )}
    </Drawer>
  );
}

function RawLogsNote({ data }: { data: LogsResponse }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button size="sm" variant="ghost" icon="file" onClick={() => setOpen(true)}>
        View raw logs
      </Button>
      <Drawer open={open} onClose={() => setOpen(false)} title="Raw logs">
        <div class="stack">
          <p>
            {data.raw_logs_note ||
              (data.raw_logs_available ? 'Raw logs are available from the services themselves.' : "Raw container logs can't be read through the console yet.")}
          </p>
          <p class="muted">The events on this page cover what Labzilla did and why. For a problem with a service, start from System → Services.</p>
          <TechDetails items={RAW_LOG_COMMANDS} inline triggerLabel="Advanced: read raw logs from a terminal on the host" />
        </div>
      </Drawer>
    </>
  );
}

export function LogsTab({ initialQuery }: { initialQuery: string }) {
  const [level, setLevel] = useState<Level>('all');
  const [q, setQ] = useState(initialQuery);
  const [open, setOpen] = useState<ActivityEvent | null>(null);
  const query = useDebounced(q.trim(), 300);
  const key = `system/logs?level=${level}&q=${query}`;
  const logs = useResource<LogsResponse>(key, () => get<LogsResponse>(`/api/system/logs${qs({ level, q: query })}`), { refreshOn: ['activity'] });

  let body;
  if (logs.loading) body = <Skeleton lines={6} />;
  else if (!logs.data) body = logs.error ? <HumanErrorCard error={logs.error} onRetry={() => void logs.refresh()} /> : null;
  else if (!logs.data.events.length)
    body = query ? (
      <EmptyState icon="search" title={`Nothing matches "${query}"`} body="Try fewer words, or look at all relevant events." action={{ label: 'Clear search', onClick: () => setQ('') }} compact />
    ) : (
      <EmptyState
        icon="success"
        title={EMPTY[level]}
        body={level === 'all' ? undefined : 'Relevant events show everything Labzilla did recently.'}
        action={level === 'all' ? { label: 'Refresh', onClick: () => void logs.refresh(), icon: 'refresh' } : { label: 'Show relevant events', onClick: () => setLevel('all') }}
        compact
      />
    );
  else
    body = (
      <List aria-label={LEVELS.find((l) => l.id === level)?.label}>
        {logs.data.events.map((e) => (
          <ActivityRow key={e.id} event={e} onOpen={setOpen} />
        ))}
      </List>
    );

  return (
    <Card title="Logs" actions={logs.data && <RawLogsNote data={logs.data} />}>
      <div class="stack">
        <div class="lz-sys-logs-filters">
          <Tabs tabs={LEVELS} value={level} onChange={setLevel} ariaLabel="Show" idBase="logs" variant="pill" />
          <Input
            label="Search events"
            hideLabel
            icon="search"
            type="search"
            placeholder="Search events"
            value={q}
            onInput={(e) => setQ((e.currentTarget as HTMLInputElement).value)}
          />
        </div>
        <div role="tabpanel" id={`logs-panel-${level}`} aria-labelledby={`logs-tab-${level}`} aria-busy={logs.refreshing || undefined}>
          {body}
        </div>
      </div>
      <EventDrawer event={open} onClose={() => setOpen(null)} />
    </Card>
  );
}

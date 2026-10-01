// Home's one activity list (§6): the last eight human-readable events. Seeded from the relevant-events
// feed (/api/system/logs) and kept live by SSE `activity` events, so nothing polls (§61).
import { get, qs } from '@/api/client';
import type { ActivityEvent, LogsResponse } from '@/api/contracts.gen';
import { useEvent } from '@/api/sse';
import { useResource } from '@/api/store';
import { ActivityRow, EmptyState, HumanErrorCard, List, Skeleton } from '@/ui';

const SHOWN = 8;
const KEY = 'home/activity';

export function RecentActivity() {
  const feed = useResource<ActivityEvent[]>(KEY, async () => {
    const res = await get<LogsResponse>(`/api/system/logs${qs({ level: 'all' })}`);
    return res.events.slice(0, SHOWN);
  });
  useEvent('activity', (ev) =>
    feed.mutate((prev) => [ev, ...(prev ?? []).filter((e) => e.id !== ev.id)].sort((a, b) => b.ts - a.ts).slice(0, SHOWN)),
  );

  return (
    <section class="stack-sm" aria-labelledby="home-activity-title">
      <div class="row-between">
        <h2 id="home-activity-title" class="section-title">
          Recent activity
        </h2>
        <a href="/system/logs" class="small">
          All activity
        </a>
      </div>
      {feed.loading ? (
        <div aria-busy="true" class="stack-sm">
          <span class="sr-only">Loading recent activity</span>
          {Array.from({ length: 4 }, (_, i) => (
            <Skeleton key={i} height="20px" width={`${80 - i * 10}%`} />
          ))}
        </div>
      ) : feed.error ? (
        <HumanErrorCard error={feed.error} onRetry={() => void feed.refresh()} compact />
      ) : feed.data?.length ? (
        <List aria-label="Recent activity">
          {feed.data.map((e) => (
            <ActivityRow key={e.id} event={e} compact />
          ))}
        </List>
      ) : (
        <EmptyState
          icon="history"
          title="Nothing has happened yet"
          body="Model refreshes, finished agents and BLERBZ work show up here as they happen."
          action={{ label: 'Ask Labzilla', href: '/ask', icon: 'ask' }}
          compact
        />
      )}
    </section>
  );
}

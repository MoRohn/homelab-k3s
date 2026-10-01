// Lightweight prompt history (§67): Today / Yesterday / Earlier, server-side so every paired device sees
// the same list (§70). Conversation management is deliberately not the product — open, or start fresh.
import { useEffect } from 'preact/hooks';
import type { ThreadSummary } from '@/api/contracts.gen';
import { useResource } from '@/api/store';
import { Button, Drawer, EmptyState, HumanErrorCard, List, ListItem, Skeleton, fmt } from '@/ui';
import { THREADS_KEY, fetchThreads } from './state';

const GROUPS: { key: ThreadSummary['group']; label: string }[] = [
  { key: 'today', label: 'Today' },
  { key: 'yesterday', label: 'Yesterday' },
  { key: 'earlier', label: 'Earlier' },
];

/** Today / Yesterday / Earlier by this browser's calendar day. The server's `group` uses the container's
 *  clock (UTC), which is off by the owner's offset; the timestamps are what count. */
export function localGroup(ts: number, now = new Date()): ThreadSummary['group'] {
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);      // DST-safe
  const t = ts * 1000;
  return t >= today.getTime() ? 'today' : t >= yesterday.getTime() ? 'yesterday' : 'earlier';
}

export interface HistoryProps {
  open: boolean;
  onClose: () => void;
  currentId?: string;
  onOpen: (id: string) => void;
  onNew: () => void;
}

export function History({ open, onClose, currentId, onOpen, onNew }: HistoryProps) {
  // Fetch only while open: history is detail on demand, not something to keep polling.
  const threads = useResource(open ? THREADS_KEY : null, fetchThreads, { maxAgeMs: 10_000 });
  useEffect(() => {
    if (open) void threads.refresh();
  }, [open]);
  const list = threads.data ?? [];
  return (
    <Drawer
      open={open}
      onClose={onClose}
      title="History"
      subtitle="Synced across your devices"
      footer={
        <Button variant="primary" icon="plus" block onClick={onNew}>
          New conversation
        </Button>
      }
    >
      {threads.loading && <Skeleton lines={5} />}
      {threads.error && <HumanErrorCard error={threads.error} onRetry={() => void threads.refresh()} compact />}
      {threads.data && list.length === 0 && (
        <EmptyState icon="history" title="No prompts yet" body="Everything you ask appears here, on every paired device." action={{ label: 'Ask something', icon: 'ask', onClick: onNew }} compact />
      )}
      {GROUPS.map((g) => {
        const rows = list.filter((t) => localGroup(t.updated_at) === g.key);
        if (!rows.length) return null;
        return (
          <section key={g.key} class="ask-history-group" aria-labelledby={`hist-${g.key}`}>
            <h3 id={`hist-${g.key}`} class="section-title">
              {g.label}
            </h3>
            <List aria-label={g.label}>
              {rows.map((t) => (
                <ListItem
                  key={t.id}
                  class={t.id === currentId ? 'is-current' : undefined}
                  title={t.title || 'Untitled'}
                  subtitle={t.preview ? <span class="ask-history-preview">{t.preview}</span> : undefined}
                  meta={fmt.ago(t.updated_at)}
                  onClick={() => onOpen(t.id)}
                />
              ))}
            </List>
          </section>
        );
      })}
    </Drawer>
  );
}

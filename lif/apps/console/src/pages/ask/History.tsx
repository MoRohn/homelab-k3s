// Ask history (§67–§70): every conversation of the owner, live on every open console (desktop and paired
// phones). Server-side, so all devices see the same list; hub `thread` events patch it in place (state.ts).
// - Wide screens: a persistent, collapsible panel next to the conversation (Ask.tsx). Phones and tablets:
//   the same list in a sheet, one tap from Ask's header.
// - Grouped Today / Yesterday / Earlier by this browser's calendar day (the server runs in UTC).
// - A row: title, one-line preview, "Answering…" with a live dot while an answer is being written anywhere,
//   otherwise the relative time, and "from iPhone" when a paired device started it. Rename in place; Delete
//   asks first because it removes the conversation from every device.
// - Keyboard: rows are links (Enter opens). ↑/↓/Home/End move between rows, F2 renames, Delete deletes.
import { useEffect, useRef, useState } from 'preact/hooks';
import type { ThreadSummary } from '@/api/contracts.gen';
import { useResource } from '@/api/store';
import { Button, Dialog, EmptyState, HumanErrorCard, Icon, IconButton, Skeleton, cx, fmt, toast } from '@/ui';
import { THREADS_KEY, deleteThread, fetchThreads, prefetchThread, renameThread } from './state';

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

/** "from iPhone": only for a paired device, and never for the device you're holding. */
function fromDevice(row: ThreadSummary, here: string | null | undefined): string | null {
  return row.origin_device && row.origin_device !== here ? `from ${row.origin_device}` : null;
}

export interface HistoryProps {
  currentId?: string;
  /** This session's paired-device name (null on an admin browser). */
  deviceName?: string | null;
  onNew: () => void;
  /** A row was chosen (the link navigates by itself; a sheet closes here). */
  onPicked?: () => void;
  /** Panel (wide) or sheet (phone, tablet): the sheet focuses search when it opens. */
  variant: 'panel' | 'sheet';
}

export function History({ currentId, deviceName, onNew, onPicked, variant }: HistoryProps) {
  const threads = useResource(THREADS_KEY, fetchThreads, { maxAgeMs: 15_000 });
  const [q, setQ] = useState('');
  const [renaming, setRenaming] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<ThreadSummary | null>(null);
  const [deleting, setDeleting] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);
  const [, tick] = useState(0);

  // Relative times ("3 min ago") stay true while the panel sits open.
  useEffect(() => {
    const t = setInterval(() => document.visibilityState === 'visible' && tick((n) => n + 1), 30_000);
    return () => clearInterval(t);
  }, []);

  const all = threads.data ?? [];
  const needle = q.trim().toLowerCase();
  const list = needle ? all.filter((t) => `${t.title}\n${t.preview}\n${t.origin_device ?? ''}`.toLowerCase().includes(needle)) : all;
  const answering = all.filter((t) => t.active).length;

  const links = () => [...(listRef.current?.querySelectorAll<HTMLAnchorElement>('a.ask-hrow-link') ?? [])];
  const onRowKey = (e: KeyboardEvent, row: ThreadSummary) => {
    const items = links();
    const i = items.indexOf(e.currentTarget as HTMLAnchorElement);
    const go = (n: number) => {
      e.preventDefault();
      items[Math.max(0, Math.min(items.length - 1, n))]?.focus();
    };
    if (e.key === 'ArrowDown') go(i + 1);
    else if (e.key === 'ArrowUp') go(i - 1);
    else if (e.key === 'Home') go(0);
    else if (e.key === 'End') go(items.length - 1);
    else if (e.key === 'F2') {
      e.preventDefault();
      setRenaming(row.id);
    } else if (e.key === 'Delete') {
      e.preventDefault();
      setConfirm(row);
    }
  };

  const doDelete = async () => {
    if (!confirm) return;
    const row = confirm;
    const items = links();
    const next = items[items.findIndex((a) => a.dataset.id === row.id) + 1] ?? items[items.findIndex((a) => a.dataset.id === row.id) - 1];
    setDeleting(true);
    const err = await deleteThread(row.id);
    setDeleting(false);
    setConfirm(null);
    if (err) return toast({ title: err.title, body: err.impact || err.next_step, tone: 'error' });
    toast({ title: 'Conversation deleted', body: row.title, tone: 'success' });
    if (row.id === currentId) onNew();
    else setTimeout(() => next?.focus(), 0);
  };

  return (
    <div class={cx('ask-hist', `is-${variant}`)}>
      <div class="ask-hist-tools">
        {all.length > 0 && (
          <Button variant="secondary" icon="plus" block onClick={onNew}>
            New conversation
          </Button>
        )}
        {all.length > 3 && (
          <div class="ask-hist-search">
            <Icon name="search" size={16} />
            <input
              type="search"
              value={q}
              placeholder="Search conversations"
              aria-label="Search conversations"
              autoComplete="off"
              onInput={(e) => setQ((e.currentTarget as HTMLInputElement).value)}
              onKeyDown={(e) => {
                if (e.key === 'Escape' && q) {
                  e.preventDefault();
                  e.stopPropagation();
                  setQ('');
                } else if (e.key === 'ArrowDown') {
                  e.preventDefault();
                  links()[0]?.focus();
                }
              }}
            />
          </div>
        )}
      </div>

      <p class="sr-only" role="status" aria-live="polite">
        {answering ? `${answering === 1 ? 'One conversation is' : `${answering} conversations are`} being answered.` : ''}
      </p>

      <div class="ask-hist-list" ref={listRef}>
        {threads.loading && <Skeleton lines={5} />}
        {threads.error && !threads.data && <HumanErrorCard error={threads.error} onRetry={() => void threads.refresh()} compact />}
        {threads.data && all.length === 0 && (
          <EmptyState
            compact
            icon="history"
            title="No conversations yet"
            body="Everything you ask appears here, on every device you’ve paired."
            action={{ label: 'Ask something', icon: 'ask', onClick: onNew }}
          />
        )}
        {all.length > 0 && list.length === 0 && (
          <p class="ask-hist-none small muted">
            No conversation matches “{q.trim()}”.{' '}
            <button type="button" class="ask-linkbtn small" onClick={() => setQ('')}>
              Clear search
            </button>
          </p>
        )}
        {GROUPS.map((g) => {
          const rows = list.filter((t) => localGroup(t.updated_at) === g.key);
          if (!rows.length) return null;
          return (
            <section key={g.key} class="ask-hist-group" aria-labelledby={`hist-${variant}-${g.key}`}>
              <h3 id={`hist-${variant}-${g.key}`} class="ask-hist-heading">
                {g.label}
              </h3>
              <ul role="list" class="ask-hist-rows">
                {rows.map((t) => (
                  <Row
                    key={t.id}
                    row={t}
                    current={t.id === currentId}
                    device={fromDevice(t, deviceName)}
                    renaming={renaming === t.id}
                    onRename={() => setRenaming(t.id)}
                    onRenamed={() => setRenaming(null)}
                    onDelete={() => setConfirm(t)}
                    onKeyDown={(e) => onRowKey(e, t)}
                    onPicked={onPicked}
                  />
                ))}
              </ul>
            </section>
          );
        })}
      </div>

      <Dialog
        open={!!confirm}
        onClose={() => setConfirm(null)}
        title="Delete this conversation?"
        size="sm"
        role="alertdialog"
        dismissible={!deleting}
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirm(null)} disabled={deleting}>
              Keep it
            </Button>
            <Button variant="danger" icon="trash" loading={deleting} onClick={() => void doDelete()}>
              Delete
            </Button>
          </>
        }
      >
        <p>
          “{confirm?.title || 'Untitled'}” is removed from every device{confirm?.active ? ', and the answer being written stops' : ''}. This can’t be undone.
        </p>
      </Dialog>
    </div>
  );
}

interface RowProps {
  row: ThreadSummary;
  current: boolean;
  device: string | null;
  renaming: boolean;
  onRename: () => void;
  onRenamed: () => void;
  onDelete: () => void;
  onKeyDown: (e: KeyboardEvent) => void;
  onPicked?: () => void;
}

function Row({ row, current, device, renaming, onRename, onRenamed, onDelete, onKeyDown, onPicked }: RowProps) {
  const title = row.title || 'Untitled';
  const link = useRef<HTMLAnchorElement>(null);
  const href = `/ask/${encodeURIComponent(row.id)}`;
  const warm = () => prefetchThread(row.id);
  return (
    <li class={cx('ask-hrow', current && 'is-current', row.active && 'is-active', renaming && 'is-renaming')}>
      {renaming ? (
        <RenameField
          initial={title}
          onDone={(focusRow) => {
            onRenamed();
            if (focusRow) setTimeout(() => link.current?.focus() ?? document.querySelector<HTMLAnchorElement>(`a[data-id="${row.id}"]`)?.focus(), 0);
          }}
          id={row.id}
        />
      ) : (
        <a
          ref={link}
          href={href}
          data-id={row.id}
          class="ask-hrow-link"
          aria-current={current ? 'page' : undefined}
          onClick={() => onPicked?.()}
          onPointerEnter={warm}
          onFocus={warm}
          onKeyDown={onKeyDown}
        >
          <span class="ask-hrow-title">{title}</span>
          {row.preview && row.preview !== title && <span class="ask-hrow-preview">{row.preview}</span>}
          <span class="ask-hrow-meta">
            {row.active ? (
              <span class="ask-hrow-live">
                <span class="ask-live-dot" aria-hidden="true" />
                Answering…
              </span>
            ) : (
              <time dateTime={fmt.iso(row.updated_at)}>{fmt.ago(row.updated_at)}</time>
            )}
            {device && <span class="ask-hrow-device">{device}</span>}
          </span>
        </a>
      )}
      {!renaming && (
        <span class="ask-hrow-actions">
          <IconButton icon="edit" size="sm" label={`Rename “${title}”`} onClick={onRename} />
          <IconButton icon="trash" size="sm" label={`Delete “${title}”`} onClick={onDelete} />
        </span>
      )}
    </li>
  );
}

function RenameField({ id, initial, onDone }: { id: string; initial: string; onDone: (focusRow: boolean) => void }) {
  const [value, setValue] = useState(initial);
  const input = useRef<HTMLInputElement>(null);
  const done = useRef(false);
  useEffect(() => {
    input.current?.focus();
    input.current?.select();
  }, []);
  const finish = async (save: boolean, focusRow: boolean) => {
    if (done.current) return;
    done.current = true;
    const title = value.replace(/\s+/g, ' ').trim();
    onDone(focusRow);
    if (!save || !title || title === initial) return;
    const err = await renameThread(id, title);
    if (err) toast({ title: err.title, body: err.next_step || err.impact, tone: 'error' });
  };
  return (
    <input
      ref={input}
      class="ask-hrow-rename"
      value={value}
      maxLength={120}
      aria-label="Conversation name"
      onInput={(e) => setValue((e.currentTarget as HTMLInputElement).value)}
      onKeyDown={(e) => {
        if (e.key === 'Enter') {
          e.preventDefault();
          void finish(true, true);
        } else if (e.key === 'Escape') {
          e.preventDefault();
          e.stopPropagation();
          void finish(false, true);
        }
      }}
      onBlur={() => void finish(true, false)}
    />
  );
}

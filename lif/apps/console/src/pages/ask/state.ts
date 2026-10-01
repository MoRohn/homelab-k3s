// Ask conversation state. The server owns threads (§70); this module only keeps the copy on screen
// fresh while an answer streams.
// - Streams run at module scope, not in a component: moving /ask → /ask/:id, opening history or a
//   remount mid-answer must not drop the stream or its abort handle.
// - The thread lives in the shared store cache under threadKey(id). While a local stream is in flight
//   the fetcher returns the local copy, so an SSE reconnect (store invalidates everything) or a
//   remount can't replace a live answer with an older server snapshot.
// - Hub `thread` events (~1 s partial content) keep a thread followed from another device live (§68),
//   and give us the server message id of our own stream, which Stop needs to cancel upstream work.
import { askStream, get, post, toHumanError } from '@/api/client';
import type {
  AiCapabilities,
  AskMode,
  Attachment,
  AttachmentIn,
  HumanError,
  Message,
  MessageRequest,
  PrivacyChoice,
  Receipt,
  StreamRoute,
  Thread,
  ThreadEvent,
  ThreadSummary,
} from '@/api/contracts.gen';
import { observable } from '@/api/observable';
import { subscribe } from '@/api/sse';
import { invalidate, peek, prime } from '@/api/store';

export const THREADS_KEY = 'ask/threads';
export const CAPS_KEY = 'ask/capabilities';
export const threadKey = (id: string) => `ask/thread/${id}`;

const enc = encodeURIComponent;

export const fetchThreads = () => get<ThreadSummary[]>('/api/ai/threads');
export const fetchCapabilities = () => get<AiCapabilities>('/api/ai/capabilities');

/** Files handed from the Mobile Gateway's Upload action. The picker must open inside the user's tap,
 *  so Home reads the files and Ask picks them up (same pattern as shell promptHandoff). */
export const fileHandoff = observable<File[] | null>(null);

interface Inflight {
  ctrl: AbortController;
  /** Placeholder id of the assistant message until the server's id is known. */
  localId: string;
  /** The server's assistant message id, learned from hub `thread` events or `done`. */
  serverId?: string;
}

const inflight = new Map<string, Inflight>();
/** The exact last request per thread (attachment text included) so Retry resends what failed. */
const lastBody = new Map<string, MessageRequest>();

/** Thread ids with a stream running in this tab; components re-render on change. */
export const streaming = observable<ReadonlySet<string>>(new Set());
const publish = () => streaming.set(new Set(inflight.keys()));

/** A prompt offered to Agents by "Run as Agent", kept out of the URL like shell promptHandoff. */
export const agentTaskHandoff = observable<string | null>(null);

/** What this tab saw at `done`, keyed by thread: the server may persist the final message a moment
 *  after emitting `done`, and a refetch in between must not flip the answer back to "streaming". */
const finished = new Map<string, Message>();

/** Fetcher for useResource(threadKey(id)). */
export async function loadThread(id: string): Promise<Thread> {
  const live = inflight.has(id) ? peek<Thread>(threadKey(id)) : undefined;
  if (live) return live;
  const t = await get<Thread>(`/api/ai/threads/${enc(id)}`);
  const seen = finished.get(id);
  if (!seen) return t;
  const stale = t.messages.find((m) => m.id === seen.id);
  if (!stale || stale.status !== 'streaming') {
    finished.delete(id);
    return t;
  }
  return { ...t, messages: t.messages.map((m) => (m.id === seen.id ? { ...m, ...seen } : m)) };
}

export async function createThread(mode: AskMode, privacy: PrivacyChoice, firstPrompt: string): Promise<Thread> {
  const title = firstPrompt.replace(/\s+/g, ' ').trim().slice(0, 80) || null;
  const t = await post<Thread>('/api/ai/threads', { mode, privacy, title });
  prime(threadKey(t.id), t);
  invalidate(THREADS_KEY);
  return t;
}

function patch(threadId: string, fn: (messages: Message[]) => Message[]): void {
  const t = peek<Thread>(threadKey(threadId));
  if (t) prime(threadKey(threadId), { ...t, messages: fn(t.messages), updated_at: Date.now() / 1000 });
}

function patchMsg(threadId: string, msgId: string, fn: (m: Message) => Message): void {
  patch(threadId, (ms) => ms.map((m) => (m.id === msgId ? fn(m) : m)));
}

/** A receipt that only knows the route so far (alias ''); MessageView shows just the trail until the real one lands. */
function routeOnly(r: StreamRoute): Receipt {
  return { alias: '', role_label: '', fallback: false, degraded: false, privacy: r.privacy, route: r.route, clamped: false, tech: [] };
}

function asStored(a: AttachmentIn): Attachment {
  return { name: a.name, kind: a.kind, size: a.size, included: a.text !== undefined && a.text !== null, note: null };
}

let seq = 0;

/**
 * Send a prompt on an existing thread and stream the answer into the cache. Resolves once the stream
 * ends (done, error or stop). Never throws: failures become a HumanError on the assistant message.
 * `attachmentNotes` carries the client-side "not processed" reasons for files sent without text.
 */
export async function send(threadId: string, body: MessageRequest, attachmentNotes: Record<string, string> = {}): Promise<void> {
  if (inflight.has(threadId)) return;
  const n = ++seq;
  const now = Date.now() / 1000;
  const localId = `local-a-${n}`;
  const user: Message = {
    id: `local-u-${n}`,
    role: 'user',
    content: body.content,
    created_at: now,
    status: 'done',
    attachments: body.attachments.map((a) => ({ ...asStored(a), note: attachmentNotes[a.name] ?? null })),
  };
  const answer: Message = { id: localId, role: 'assistant', content: '', created_at: now, status: 'streaming', attachments: [] };
  const job: Inflight = { ctrl: new AbortController(), localId };
  inflight.set(threadId, job);
  lastBody.set(threadId, body);
  publish();
  patch(threadId, (ms) => [...ms, user, answer]);

  // Deltas arrive token by token; coalesce them so Markdown re-renders at most ~20×/s.
  let buf = '';
  let timer: ReturnType<typeof setTimeout> | undefined;
  const flush = () => {
    clearTimeout(timer);
    timer = undefined;
    if (!buf) return;
    const add = buf;
    buf = '';
    patchMsg(threadId, localId, (m) => ({ ...m, content: m.content + add }));
  };
  const finish = (status: Message['status'], error?: HumanError) =>
    patchMsg(threadId, localId, (m) => (m.status === 'streaming' ? { ...m, status, error: error ?? m.error ?? null } : m));
  // postStream turns any exception thrown inside the read loop into "Labzilla unavailable"; a render
  // bug in a handler must not masquerade as a network failure.
  const safe = <T,>(fn: (d: T) => void) => (d: T) => {
    try {
      fn(d);
    } catch (e) {
      console.error('ask stream handler', e);
    }
  };

  try {
    await askStream(
      threadId,
      body,
      {
        route: safe((r) => {
          // The route event names the server-side assistant message, so Stop can cancel from the first frame.
          if (r.message_id) job.serverId ??= r.message_id;
          patchMsg(threadId, localId, (m) => ({ ...m, receipt: routeOnly(r) }));
        }),
        delta: safe((d) => {
          buf += d.text;
          timer ??= setTimeout(flush, 50);
        }),
        receipt: safe((r) => {
          flush();
          patchMsg(threadId, localId, (m) => ({ ...m, receipt: r }));
        }),
        error: safe((e) => {
          flush();
          finish('error', e);
        }),
        done: safe((d) => {
          flush();
          job.serverId = d.message_id;
          finish('done');
          const m = peek<Thread>(threadKey(threadId))?.messages.find((x) => x.id === localId);
          if (m) finished.set(threadId, { ...m, id: d.message_id });
        }),
      },
      { signal: job.ctrl.signal },
    );
    flush();
    // A stream that ended without `done` or `error` (proxy cut it) still needs a final state.
    finish('done');
  } catch (e) {
    flush();
    if (e instanceof DOMException && e.name === 'AbortError') finish('cancelled');
    else finish('error', toHumanError(e));
  } finally {
    clearTimeout(timer);
    inflight.delete(threadId);
    publish();
    // After a Stop, stop() refetches once the server has accepted the cancel; refetching here would
    // briefly show the server's still-streaming copy again.
    if (!job.ctrl.signal.aborted) invalidate(threadKey(threadId));
    invalidate(THREADS_KEY);
  }
}

/** Resend the last request (Retry on an error card). Falls back to the last prompt in the thread when
 *  this tab didn't send it (e.g. it failed on another device); attachment text isn't stored server-side. */
export function retry(thread: Thread): Promise<void> {
  const body =
    lastBody.get(thread.id) ??
    (() => {
      const lastUser = [...thread.messages].reverse().find((m) => m.role === 'user');
      // Local only: allowing Jev is a per-prompt choice, never inherited from an earlier send.
      return lastUser ? { content: lastUser.content, mode: thread.mode, privacy: 'local_only' as const, attachments: [] } : null;
    })();
  return body ? send(thread.id, body) : Promise.resolve();
}

/**
 * Stop the answer: abort our fetch and ask the server to cancel (it otherwise keeps consuming upstream
 * so another device can follow, brief §5). Works for a stream started on another device too.
 * Returns a HumanError when the server refused the cancel.
 */
export async function stop(threadId: string, messageId?: string): Promise<HumanError | null> {
  const job = inflight.get(threadId);
  job?.ctrl.abort();
  let mid = job?.serverId ?? (messageId && !messageId.startsWith('local-') ? messageId : undefined);
  try {
    if (!mid) {
      // Server id not seen yet (no hub event so far): the newest streaming assistant message is ours.
      const t = await get<Thread>(`/api/ai/threads/${enc(threadId)}`);
      mid = [...t.messages].reverse().find((m) => m.role === 'assistant' && m.status === 'streaming')?.id;
    }
    if (mid) await post(`/api/ai/threads/${enc(threadId)}/messages/${enc(mid)}/cancel`, {});
    return null;
  } catch (e) {
    return toHumanError(e);
  } finally {
    invalidate(threadKey(threadId));
  }
}

const RANK: Record<Message['status'], number> = { streaming: 0, done: 1, error: 1, cancelled: 1 };

function onThreadEvent(ev: ThreadEvent): void {
  const job = inflight.get(ev.thread_id);
  if (job) {
    // Our own stream: the deltas already carry the text; only learn the server id for Stop.
    job.serverId ??= ev.message_id;
    return;
  }
  const t = peek<Thread>(threadKey(ev.thread_id));
  if (!t) return;
  const m = t.messages.find((x) => x.id === ev.message_id);
  if (!m) {
    // A prompt sent from another device: fetch it (user message, receipt) once.
    invalidate(threadKey(ev.thread_id));
    return;
  }
  // Never let a late "streaming" frame downgrade a finished message.
  if (RANK[ev.status] < RANK[m.status]) return;
  patchMsg(ev.thread_id, ev.message_id, (x) => ({ ...x, content: ev.content.length >= x.content.length ? ev.content : x.content, status: ev.status }));
  if (ev.status !== 'streaming') {
    invalidate(threadKey(ev.thread_id));
    invalidate(THREADS_KEY);
  }
}

subscribe('thread', onThreadEvent);

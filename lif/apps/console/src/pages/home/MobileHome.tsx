// The Mobile Gateway (§12, §13, §63, §66, §99): what a phone shows at "/". Not a shrunk desktop —
// LABZILLA, whether local AI is ready, a prompt that is already on screen (tap = interaction 1, then
// type), quick actions, anything waiting on the owner, and a few recent prompts.
// Rendered by Home on compact widths. The prompt goes through the command resolver like the command bar on
// every other page (the bar is hidden here, but mounted): "Pause batch jobs" gets its proposed action and
// "Why is the GPU busy?" its snapshot answer, while plain prompts continue to Ask. Pasting multi-line text
// (code) moves the draft into Ask's composer, where line breaks survive. Upload opens the picker inside
// the tap (browsers block it otherwise) and hands the files to Ask.
import { useEffect, useRef, useState } from 'preact/hooks';
import { useLocation } from 'preact-iso/router';
import { get, post, toHumanError } from '@/api/client';
import type { Approval, Health } from '@/api/contracts.gen';
import { can, useMe } from '@/api/session';
import { useConnectionState } from '@/api/sse';
import { useSystemStatus } from '@/api/status';
import { invalidate, useResource } from '@/api/store';
import { promptDraft, runCommand } from '@/shell/commands';
import { usePageTitle } from '@/shell/usePageTitle';
import { ApprovalCard, CommandInput, Icon, List, ListItem, StatusDot, fmt, toast, type IconName } from '@/ui';
import { THREADS_KEY, fetchThreads, fileHandoff } from '@/pages/ask/state';
import '@/pages/ask/ask.css';

interface QuickAction {
  label: string;
  icon: IconName;
  href?: string;
  /** Opens the file picker within the tap. */
  upload?: true;
}

// §66: Ask, Code, Research, Summarize, Status, Run Agent (+ Upload from §12). Research maps to the Deep mode.
const ACTIONS: QuickAction[] = [
  { label: 'Ask', icon: 'ask', href: '/ask?focus=1' },
  { label: 'Code', icon: 'code', href: '/ask?mode=code&focus=1' },
  { label: 'Research', icon: 'search', href: '/ask?mode=deep&focus=1' },
  { label: 'Summarize', icon: 'file', href: `/ask?q=${encodeURIComponent('Summarize this:\n\n')}&focus=1` },
  { label: 'Agent', icon: 'agents', href: '/agents?run=1' },
  { label: 'Upload', icon: 'upload', upload: true },
  { label: 'Status', icon: 'system', href: '/system' },
];

const APPROVALS_KEY = 'mobile/approvals';

export default function MobileHome() {
  usePageTitle('Home');
  const { route } = useLocation();
  const status = useSystemStatus();
  const me = useMe();
  const conn = useConnectionState();
  const [text, setText] = useState('');
  const promptRef = useRef<HTMLInputElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const [answering, setAnswering] = useState<string | null>(null);

  const s = status.data;
  const pendingCount = s?.approvals_pending ?? 0;
  // Fetch approvals only when the status says something is waiting (keeps the phone's first load light).
  const approvals = useResource<Approval[]>(pendingCount > 0 ? APPROVALS_KEY : null, () => get<Approval[]>('/api/approvals'), { refreshOn: ['approval'] });
  const recent = useResource(THREADS_KEY, fetchThreads, { maxAgeMs: 30_000 });

  // CommandInput doesn't forward extra attributes; "/" focuses whatever carries data-slash-focus.
  useEffect(() => {
    promptRef.current?.setAttribute('data-slash-focus', '');
  }, []);

  const offline = conn === 'offline';
  const aiHealth: Health = offline ? 'offline' : s?.local_ai ?? 'unknown';
  const aiLabel = offline ? 'Not connected' : s?.local_ai_label ?? 'Checking local AI…';

  const submit = (value: string) => {
    setText('');
    // The resolver takes short text (4,000 characters); a long prompt goes straight to Ask's composer.
    if (value.length > 4000) {
      promptDraft.set(value);
      route('/ask?focus=1');
      return;
    }
    runCommand(value);
  };

  const paste = (e: ClipboardEvent) => {
    const pasted = e.clipboardData?.getData('text/plain') ?? '';
    if (!pasted.includes('\n')) return;
    e.preventDefault();
    promptDraft.set(text.trim() ? `${text.trim()}\n\n${pasted}` : pasted);
    setText('');
    route('/ask?focus=1');
  };

  const answer = async (a: Approval, value: string) => {
    setAnswering(`${a.id}:${value}`);
    try {
      await post(`/api/approvals/${encodeURIComponent(a.id)}`, { answer: value });
      toast({ title: 'Answer sent', body: a.title, tone: 'success' });
      invalidate(APPROVALS_KEY);
    } catch (e) {
      const err = toHumanError(e);
      toast({ title: err.title, body: err.impact || err.next_step, tone: 'error' });
    } finally {
      setAnswering(null);
    }
  };

  const pending = (approvals.data ?? []).filter((a) => a.status === 'pending');
  const threads = (recent.data ?? []).slice(0, 3);

  return (
    <div class="page mh">
      <header class="mh-hero">
        <h1 class="mh-mark">
          <img src="/logo/mark.png" alt="" width={36} height={36} />
          <span class="mh-wordmark">LABZILLA</span>
        </h1>
        <p class="mh-ready" aria-live="polite">
          <StatusDot health={aiHealth} label="" pulse={aiHealth === 'busy'} />
          <strong>{aiLabel}</strong>
          {s?.connection.local && !offline && <span class="xsmall">· Local connection</span>}
        </p>
      </header>

      <div class="mh-ask">
        <CommandInput
          value={text}
          onInput={setText}
          onSubmit={submit}
          placeholder="Ask Labzilla…"
          label="Ask Labzilla"
          inputRef={promptRef}
          enterKeyHint="send"
          onPaste={paste}
        />
      </div>

      <nav aria-label="Quick actions">
        <ul role="list" class="mh-actions">
          {ACTIONS.map((a) => (
            <li key={a.label}>
              {a.upload ? (
                <button type="button" class="mh-action" onClick={() => fileRef.current?.click()}>
                  <Icon name={a.icon} size={18} />
                  {a.label}
                </button>
              ) : (
                <a class="mh-action" href={a.href}>
                  <Icon name={a.icon} size={18} />
                  {a.label}
                </a>
              )}
            </li>
          ))}
        </ul>
        <input
          ref={fileRef}
          type="file"
          multiple
          hidden
          onChange={(e) => {
            const el = e.currentTarget as HTMLInputElement;
            if (!el.files?.length) return;
            fileHandoff.set([...el.files]);
            el.value = '';
            route('/ask?focus=1');
          }}
        />
      </nav>

      {pending.length > 0 && (
        <section class="mh-section" aria-labelledby="mh-approvals">
          <div class="row-between">
            {/* Same words as desktop Home; the card's own badge already says "Waiting for you". */}
            <h2 id="mh-approvals" class="section-title">
              Needs your attention
            </h2>
            {pending.length > 1 && (
              <a class="small" href="/agents?tab=approvals">
                All {pending.length}
              </a>
            )}
          </div>
          {pending.slice(0, 1).map((a) => {
            const perm = a.kind === 'device_pairing' ? 'devices.manage' : 'approvals.answer';
            const allowed = can(me.data, perm);
            const busy = answering?.startsWith(`${a.id}:`) ? answering.slice(a.id.length + 1) : null;
            return (
              <ApprovalCard
                key={a.id}
                approval={a}
                busy={busy}
                canAnswer={allowed}
                whyNot={a.kind === 'device_pairing' ? 'Approve new devices from an admin session on your desktop.' : 'This session can’t answer approvals.'}
                onAnswer={(v) => answer(a, v)}
              />
            );
          })}
        </section>
      )}

      {threads.length > 0 && (
        <section class="mh-section" aria-labelledby="mh-recent">
          <div class="row-between">
            <h2 id="mh-recent" class="section-title">
              Recent
            </h2>
            <a class="small" href="/ask">
              New prompt
            </a>
          </div>
          <List aria-label="Recent prompts">
            {threads.map((t) => (
              <ListItem key={t.id} title={<span class="truncate">{t.title || 'Untitled'}</span>} meta={fmt.ago(t.updated_at)} href={`/ask/${encodeURIComponent(t.id)}`} />
            ))}
          </List>
        </section>
      )}
    </div>
  );
}

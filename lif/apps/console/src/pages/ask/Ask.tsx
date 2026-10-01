// Ask Labzilla (§9–§11, §13, §18–§21, §64–§70): a focused prompt screen, not a chatbot. One prompt, the
// capability (mode), the privacy choice and a streaming answer with an understated receipt.
// - /ask starts a conversation; the first send creates a server thread and moves to /ask/:id (replace),
//   so the URL opens the same conversation on any paired device (§68–§70).
// - Arrivals: shell promptHandoff (command bar, Mobile Gateway) and ?q=&mode=&send=1 (shortcuts) send
//   once; ?focus=1 focuses the prompt; fileHandoff brings files picked on the Mobile Gateway.
// - Streaming and its state live in ./state at module scope, so navigation never drops an answer.
import { useEffect, useLayoutEffect, useRef, useState } from 'preact/hooks';
import { useLocation } from 'preact-iso/router';
import type { AskMode, CommandResolution, HumanError, Message, PrivacyChoice, PromptSuggestion, Thread } from '@/api/contracts.gen';
import { post, toHumanError } from '@/api/client';
import { useObservable } from '@/api/observable';
import { useMe } from '@/api/session';
import { useResource } from '@/api/store';
import { CommandResult } from '@/shell/CommandResult';
import { promptDraft, promptHandoff } from '@/shell/commands';
import { useBreakpoint, useCoarsePointer } from '@/shell/useBreakpoint';
import { usePageTitle } from '@/shell/usePageTitle';
import { Button, EmptyState, HumanErrorCard, Icon, IconButton, Skeleton, toast, type IconName } from '@/ui';
import { Composer, isAskMode } from './Composer';
import { ContinueElsewhere } from './ContinueElsewhere';
import { History } from './History';
import { AnswerView, PromptView } from './MessageView';
import { MAX_FILES, intake, toAttachmentIn, type Picked } from './files';
import { CAPS_KEY, createThread, fetchCapabilities, fileHandoff, loadThread, retry, send, stop, streaming, threadKey } from './state';
import './ask.css';

/** First-time discovery (§77, §78): shown only while the prompt is empty. Questions about Labzilla itself are
 *  sent like a typed prompt, so the resolver answers them from live state; the last one only fills the prompt. */
const SUGGESTIONS: { label: string; icon: IconName; command?: string; draft?: string; mode?: AskMode }[] = [
  { label: 'Why is the GPU busy?', icon: 'gpu', command: 'Why is the GPU busy?' },
  { label: 'What changed today?', icon: 'history', command: 'What changed today?' },
  { label: 'Check for better local models', icon: 'scout', command: 'Check for better models' },
  { label: 'Write a Python script…', icon: 'code', draft: 'Write a Python script that ', mode: 'code' },
];

/** Prompts the command resolver may answer instead of a model (§9: no mode to choose first). Only Auto mode
 *  without files, and only a short single-line question or command: pasted text, code and long prompts are
 *  always for the model. The rules are deterministic and local, so nothing leaves the box for this check. */
const ROUTABLE_MAX = 160;
/** In Ask, a request to explain or teach is for the model even when it names the GPU ("Explain how GPU memory
 *  works"); the command bar elsewhere keeps the resolver's own reading. */
const FOR_THE_MODEL = /^(?:explain|teach|describe how|how (?:do|does|can|to|would)|what is the difference)\b/i;
export function routable(text: string, mode: AskMode, hasFiles: boolean): boolean {
  const t = text.trim();
  return mode === 'auto' && !hasFiles && t.length > 0 && t.length <= ROUTABLE_MAX && !t.includes('\n') && !FOR_THE_MODEL.test(t);
}

/** A question Labzilla answered itself (status, action, navigation). Shown in the page only: it isn't a model
 *  turn, so it is never stored in the conversation or replayed as context. */
interface CommandTurn {
  text: string;
  resolution: CommandResolution;
}

export default function Ask({ id }: { id?: string }) {
  const { url, path, query, route } = useLocation();
  const bp = useBreakpoint();
  const compact = bp === 'compact';
  const coarse = useCoarsePointer();
  const me = useMe();
  const caps = useResource(CAPS_KEY, fetchCapabilities, { maxAgeMs: 60_000, refreshOn: ['model'] });
  const thread = useResource<Thread>(id ? threadKey(id) : null, () => loadThread(id ?? ''), { maxAgeMs: 3000 });
  const liveHere = useObservable(streaming);

  const [text, setText] = useState('');
  const [mode, setMode] = useState<AskMode>('auto');
  const [privacy, setPrivacy] = useState<PrivacyChoice>('local_only');
  const [files, setFiles] = useState<Picked[]>([]);
  const [creating, setCreating] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [startError, setStartError] = useState<HumanError | null>(null);
  const [modeNotice, setModeNotice] = useState<string | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [continueOpen, setContinueOpen] = useState(false);
  const [announce, setAnnounce] = useState('');
  const [resolving, setResolving] = useState(false);
  const [commandTurn, setCommandTurn] = useState<CommandTurn | null>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const dock = useRef<HTMLDivElement>(null);
  const [dockH, setDockH] = useState(0);

  const t = id ? thread.data : undefined;
  const messages = t?.messages ?? [];
  const last = messages[messages.length - 1];
  const streamingHere = !!id && liveHere.has(id);
  const streamingAnywhere = streamingHere || last?.status === 'streaming';
  usePageTitle(t?.title ? `${t.title} · Ask` : 'Ask Labzilla');

  // Layout focuses #main after every navigation, and parent effects run after ours: defer, or it's stolen.
  const focusPrompt = () => setTimeout(() => input.current?.focus({ preventScroll: compact }), 60);

  // A thread opened from History or another device: adopt its mode once per thread. Privacy deliberately
  // resets to Local only — allowing Jev is a per-prompt choice, never inherited silently.
  const adopted = useRef<string | null>(null);
  useEffect(() => {
    if (t && adopted.current !== t.id) {
      adopted.current = t.id;
      setMode(t.mode);
    }
  }, [t?.id]);

  // If the chosen mode turns out to be unavailable, say so and fall back to Auto rather than fail on send.
  useEffect(() => {
    const m = caps.data?.modes.find((x) => x.mode === mode);
    if (m && !m.available) {
      setModeNotice(`${m.label} isn’t available${m.reason ? `: ${m.reason}` : ''}. Using Auto.`);
      setMode('auto');
    }
  }, [caps.data, mode]);

  /** Create the thread if needed, then stream. The draft is kept until the thread exists; after that a
   *  failure shows on the answer with Retry, which resends the exact request (attachment text included). */
  const start = async (content: string, m: AskMode, p: PrivacyChoice, picked: Picked[], threadId: string | undefined, fromComposer: boolean) => {
    const prompt = content.trim();
    if (!prompt || creating) return;
    if (threadId && liveHere.has(threadId)) return;
    setStartError(null);
    setModeNotice(null);
    setCommandTurn(null);
    let tid = threadId;
    if (!tid) {
      setCreating(true);
      try {
        tid = (await createThread(m, p, prompt)).id;
      } catch (e) {
        setStartError(toHumanError(e));
        return;
      } finally {
        setCreating(false);
      }
      route(`/ask/${encodeURIComponent(tid)}`, true);
    }
    if (fromComposer) {
      setText('');
      setFiles([]);
      // The hero composer is replaced by the docked one after the first send: keep keyboard focus.
      if (!coarse) focusPrompt();
    }
    const notes = Object.fromEntries(picked.filter((f) => !f.included && f.note).map((f) => [f.name, f.note ?? '']));
    void send(tid, { content: prompt, mode: m, privacy: p, attachments: picked.map(toAttachmentIn) }, notes);
  };
  const startRef = useRef(start);
  startRef.current = start;

  /** Composer send: questions about Labzilla itself ("Why is the GPU busy?", "Pause batch jobs") are answered by
   *  the command resolver from live state; everything else streams from the model. If the resolver can't be
   *  reached, the prompt goes to the model as before: the check must never block asking. */
  const submit = async (content: string, viaResolver = routable(content, mode, files.length > 0)) => {
    const prompt = content.trim();
    if (!prompt || creating || resolving) return;
    if (viaResolver) {
      setResolving(true);
      let r: CommandResolution | null = null;
      try {
        r = await post<CommandResolution>('/api/command', { text: prompt });
      } catch {
        r = null;
      } finally {
        setResolving(false);
      }
      if (r && r.kind !== 'ai_prompt') {
        setStartError(null);
        setModeNotice(null);
        setText('');
        setCommandTurn({ text: prompt, resolution: { ...r, prompt: { text: prompt, mode: 'auto' } } });
        setAnnounce(r.title);
        return;
      }
    }
    void start(prompt, mode, privacy, files, id, true);
  };
  const sendComposer = () => void submit(text);

  // Command bar / Mobile Gateway hand-off: subscribe for the page's lifetime (a palette prompt sent while
  // already on /ask doesn't remount us). Each prompt starts a new conversation and is sent Local only.
  useEffect(() => {
    const take = (s: PromptSuggestion | null) => {
      if (!s) return;
      promptHandoff.set(null);
      const m = isAskMode(s.mode) ? s.mode : 'auto';
      setMode(m);
      setPrivacy('local_only');
      void startRef.current(s.text, m, 'local_only', [], undefined, false);
    };
    take(promptHandoff.get());
    return promptHandoff.subscribe(take);
  }, []);

  // Mobile Gateway paste: an unsent draft for the composer (multi-line code keeps its line breaks).
  useEffect(() => {
    const take = (d: string | null) => {
      if (d === null) return;
      promptDraft.set(null);
      setText(d);
      focusPrompt();
    };
    take(promptDraft.get());
    return promptDraft.subscribe(take);
  }, []);

  const addFiles = async (list: File[]) => {
    const room = MAX_FILES - files.length;
    if (list.length > room) toast({ title: `Up to ${MAX_FILES} files per prompt`, body: room > 0 ? `Added the first ${room}.` : undefined, tone: 'warning' });
    const picked = await Promise.all(list.slice(0, Math.max(0, room)).map(intake));
    setFiles((prev) => [...prev, ...picked].slice(0, MAX_FILES));
  };
  const addFilesRef = useRef(addFiles);
  addFilesRef.current = addFiles;

  useEffect(() => {
    const take = (list: File[] | null) => {
      if (!list) return;
      fileHandoff.set(null);
      void addFilesRef.current(list).then(focusPrompt);
    };
    take(fileHandoff.get());
    return fileHandoff.subscribe(take);
  }, []);

  // Shortcut parameters (?q= ?mode= ?send=1 ?focus=1): consumed once, then stripped so a refresh can't resend.
  useEffect(() => {
    const q = query.q;
    const m = isAskMode(query.mode) ? query.mode : null;
    const wantsSend = query.send === '1';
    const wantsFocus = query.focus === '1';
    if (q === undefined && !m && !wantsSend && !wantsFocus) return;
    if (m) setMode(m);
    route(path, true);
    if (q && wantsSend) {
      setPrivacy('local_only');
      void startRef.current(q, m ?? 'auto', 'local_only', [], undefined, false);
      return;
    }
    if (q) setText(q);
    focusPrompt();
  }, [url]);

  // Desktop/keyboard: the prompt is ready to type into. Touch: only when asked (?focus=1 above), since a
  // programmatic focus outside a tap can't raise the keyboard on iOS anyway.
  useEffect(() => {
    if (!coarse) focusPrompt();
  }, [id]);

  // Announce the outcome of an answer once (the streaming text itself is not a live region: it would be
  // re-read on every token).
  const lastStatus = useRef<Message['status'] | undefined>(undefined);
  useEffect(() => {
    if (!last || last.role !== 'assistant') return;
    if (lastStatus.current === 'streaming' && last.status !== 'streaming')
      setAnnounce(last.status === 'done' ? 'Answer ready.' : last.status === 'cancelled' ? 'Stopped.' : last.error?.title ?? 'The answer failed.');
    lastStatus.current = last.status;
  }, [last?.id, last?.status]);

  // Follow the answer while it streams, unless the reader scrolled up.
  useEffect(() => {
    if (!streamingAnywhere) return;
    const doc = document.documentElement;
    const nearBottom = window.innerHeight + window.scrollY >= doc.scrollHeight - dockH - 160;
    if (nearBottom) window.scrollTo({ top: doc.scrollHeight });
  }, [last?.content.length, streamingAnywhere]);

  // The docked composer (compact) floats over content: reserve exactly its height.
  useLayoutEffect(() => {
    const el = dock.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(() => setDockH(el.offsetHeight));
    ro.observe(el);
    return () => ro.disconnect();
  }, [compact]);

  const onStop = async () => {
    if (!id) return;
    setStopping(true);
    const err = await stop(id, last?.role === 'assistant' ? last.id : undefined);
    setStopping(false);
    if (err) toast({ title: err.title, body: err.impact, tone: 'warning' });
  };

  const newConversation = () => {
    setHistoryOpen(false);
    setStartError(null);
    route('/ask');
    focusPrompt();
  };

  const empty = !id || (!!t && messages.length === 0);
  const composer = (
    <Composer
      value={text}
      onInput={setText}
      mode={mode}
      onMode={(m) => {
        setModeNotice(null);
        setMode(m);
      }}
      privacy={privacy}
      onPrivacy={setPrivacy}
      caps={caps.data}
      capsFailed={!!caps.error}
      files={files}
      onFiles={(l) => void addFiles(l)}
      onRemoveFile={(fid) => setFiles((prev) => prev.filter((f) => f.id !== fid))}
      streaming={streamingAnywhere}
      busy={creating || stopping || resolving}
      onSend={sendComposer}
      onStop={() => void onStop()}
      textareaRef={input}
      compact={compact}
      coarse={coarse}
      hero={empty && !compact}
    />
  );

  return (
    <div class={`page ask-page${compact ? ' is-compact' : ''}${empty ? ' is-empty' : ''}`} style={compact && dockH ? { paddingBottom: `${dockH}px` } : undefined}>
      <header class="page-header ask-header">
        <div class="grow">
          <h1>{t?.title && !empty ? <span class="ask-title truncate">{t.title}</span> : 'Ask Labzilla'}</h1>
          {empty && !compact && <p>Runs on Labzilla’s local models. Nothing leaves your network unless you allow it.</p>}
        </div>
        <div class="row">
          {id && t && messages.length > 0 && (
            <IconButton icon="phone" label="Continue on another device" onClick={() => setContinueOpen(true)} />
          )}
          {id && <IconButton icon="plus" label="New conversation" onClick={newConversation} />}
          <Button variant="ghost" size="sm" icon="history" onClick={() => setHistoryOpen(true)}>
            History
          </Button>
        </div>
      </header>

      {modeNotice && (
        <p class="ask-note lz-tone-info small" role="status">
          <Icon name="info" size={16} /> <span>{modeNotice}</span>
        </p>
      )}
      {startError && <HumanErrorCard error={startError} onRetry={sendComposer} />}

      {/* Empty conversation on a wide screen: the prompt is the page (§9). */}
      {empty && !compact && composer}
      {empty && !text && files.length === 0 && !commandTurn && !resolving && (
        <ul class="ask-suggestions" aria-label="Suggestions">
          {SUGGESTIONS.map((g) => (
            <li key={g.label}>
              <button
                type="button"
                class="ask-suggestion"
                onClick={() => {
                  if (g.command) return void submit(g.command, true);
                  if (g.mode) setMode(g.mode);
                  setText(g.draft ?? '');
                  focusPrompt();
                }}
              >
                <Icon name={g.icon} size={14} />
                {g.label}
              </button>
            </li>
          ))}
        </ul>
      )}

      {id && thread.loading && (
        <div class="stack" aria-busy="true" aria-label="Loading conversation">
          <Skeleton width="60%" />
          <Skeleton lines={4} />
        </div>
      )}
      {id && thread.error && !t && (
        <div class="stack-sm">
          <HumanErrorCard error={thread.error} onRetry={() => void thread.refresh()} />
          <div>
            <Button variant="ghost" icon="plus" onClick={newConversation}>
              Start a new conversation
            </Button>
          </div>
        </div>
      )}
      {id && t && messages.length === 0 && compact && (
        <EmptyState compact icon="ask" title="Nothing asked yet" body="Type below to start this conversation." />
      )}

      {messages.length > 0 && t && (
        <section class="ask-thread" aria-label="Conversation">
          {messages.map((m, i) => {
            if (m.role === 'user') return <PromptView key={m.id} msg={m} />;
            if (m.role !== 'assistant') return null;
            const prompt = [...messages.slice(0, i)].reverse().find((x) => x.role === 'user')?.content ?? '';
            return (
              <AnswerView
                key={m.id}
                msg={m}
                thread={t}
                prompt={prompt}
                user={me.data}
                remote={m.status === 'streaming' && !streamingHere}
                onContinue={() => {
                  input.current?.scrollIntoView({ block: 'center', behavior: 'smooth' });
                  focusPrompt();
                }}
                onRetry={() => void retry(t)}
              />
            );
          })}
        </section>
      )}

      {commandTurn && (
        <section class="ask-command" aria-label="Answered by Labzilla">
          <p class="ask-prompt-text">{commandTurn.text}</p>
          <CommandResult
            resolution={commandTurn.resolution}
            askLabel="Ask the model instead"
            onClose={() => {
              setCommandTurn(null);
              focusPrompt();
            }}
            onNavigate={(href) => {
              setCommandTurn(null);
              route(href);
            }}
            onAsk={(p) => void start(p.text, 'auto', privacy, [], id, false)}
          />
        </section>
      )}

      {(compact || !empty) && (
        <div ref={dock} class={compact ? 'ask-dock is-fixed' : 'ask-dock'}>
          {composer}
        </div>
      )}

      <p class="sr-only" role="status" aria-live="polite">
        {announce}
      </p>

      <History
        open={historyOpen}
        onClose={() => setHistoryOpen(false)}
        currentId={id}
        onNew={newConversation}
        onOpen={(tid) => {
          setHistoryOpen(false);
          route(`/ask/${encodeURIComponent(tid)}`);
        }}
      />
      {id && <ContinueElsewhere open={continueOpen} onClose={() => setContinueOpen(false)} threadId={id} />}
    </div>
  );
}

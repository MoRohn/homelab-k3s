// The Ask composer (§9, §10, §13, §19, §20, §64): one large prompt plus the few choices that matter —
// mode (logical capability, never a physical model), privacy, and input by keyboard, paste, voice or file.
// Wide/medium: mode and privacy are compact controls in the prompt's own toolbar (Attach · Auto ▾ · Local only ·
// Send). Compact: a one-line summary opens them in a sheet so the prompt stays the first thing the thumb reaches.
import type { ComponentChildren, Ref } from 'preact';
import { useRef, useState } from 'preact/hooks';
import type { AiCapabilities, AskMode, PrivacyChoice } from '@/api/contracts.gen';
import { Button, Dialog, IconButton, Icon, PrivacyBadge, Select, Sheet, Switch, Textarea, Tooltip, cx } from '@/ui';
import { MAX_FILES, fileSize, isImage, type Picked } from './files';
import { useDictation } from './voice';

export interface ComposerProps {
  value: string;
  onInput: (v: string) => void;
  mode: AskMode;
  onMode: (m: AskMode) => void;
  privacy: PrivacyChoice;
  onPrivacy: (p: PrivacyChoice) => void;
  caps?: AiCapabilities;
  capsFailed?: boolean;
  files: Picked[];
  onFiles: (files: File[]) => void;
  onRemoveFile: (id: string) => void;
  /** An answer is streaming on this thread: Send becomes Stop. */
  streaming: boolean;
  /** Creating the thread / stopping: buttons show progress. */
  busy?: boolean;
  onSend: () => void;
  onStop: () => void;
  textareaRef: Ref<HTMLTextAreaElement>;
  compact: boolean;
  /** Touch-first device: Enter inserts a newline, the Send button sends. */
  coarse: boolean;
  /** Larger prompt for an empty conversation (desktop). */
  hero?: boolean;
  /** Something to send: text, or an image on its own. */
  canSend: boolean;
  /** Why Send is held back right now (e.g. images but no vision model), shown under the box. */
  blocked?: ComponentChildren;
  /** "Switched to Vision · Undo", shown under the box. */
  notice?: ComponentChildren;
}

const DICTATION_OK = 'lz-dictation-ok';
const ALL_MODES: AskMode[] = ['auto', 'fast', 'balanced', 'deep', 'code', 'vision'];
const MODE_LABEL: Record<AskMode, string> = { auto: 'Auto', fast: 'Fast', balanced: 'Balanced', deep: 'Deep', code: 'Code', vision: 'Vision' };

export function isAskMode(v: unknown): v is AskMode {
  return typeof v === 'string' && (ALL_MODES as string[]).includes(v);
}

function dictationAcknowledged(): boolean {
  try {
    return localStorage.getItem(DICTATION_OK) === '1';
  } catch {
    return false;
  }
}

export function Composer(p: ComposerProps) {
  const { value, onInput, mode, privacy, caps, files, streaming, compact, coarse, canSend } = p;
  const fileInput = useRef<HTMLInputElement>(null);
  const cameraInput = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [optionsOpen, setOptionsOpen] = useState(false);
  const [disclose, setDisclose] = useState(false);
  const dictation = useDictation((t) => {
    if (!t) return;
    onInput(value && !/\s$/.test(value) ? `${value} ${t}` : value + t);
  });

  const modes = caps?.modes.length ? caps.modes : null;
  const current = modes?.find((m) => m.mode === mode);
  // Until capabilities arrive (or if they can't be fetched) offer every mode unchecked, so the select
  // always shows the mode that will actually be sent (e.g. ?mode=code from a shortcut).
  const modeOptions = modes
    ? modes.map((m) => ({
        value: m.mode,
        label: m.available ? m.label : `${m.label} — ${m.reason ?? 'unavailable'}`,
        description: m.blurb,
        disabled: !m.available && m.mode !== mode,
      }))
    : ALL_MODES.map((m) => ({ value: m, label: MODE_LABEL[m] }));
  const jevOption = caps?.privacy_options.find((o) => o.value === 'allow_jev');
  const jevAvailable = jevOption ? jevOption.available : true;
  const externalNote = caps?.external_note || 'External models: off (privacy policy)';
  const modeLabel = current?.label ?? MODE_LABEL[mode];
  // The server sends a message with included files as Local only, whatever the switch says (Jev routing
  // would see the file text); say so here instead of promising otherwise.
  const filesIncluded = files.some((f) => f.included);
  const jev = privacy === 'allow_jev' && !filesIncluded;
  // Jev only picks the model in Auto; in other modes the switch has no effect, so don't claim it.
  const jevActive = jev && mode === 'auto';
  const privacyLabel = jev ? 'Local + Jev allowed' : privacy === 'allow_jev' ? 'Local only (files attached)' : 'Local only';
  const images = files.filter(isImage);
  const others = files.filter((f) => !isImage(f));

  const toggleVoice = () => {
    if (dictation.listening) return dictation.stop();
    if (!dictationAcknowledged()) return setDisclose(true);
    dictation.start();
  };

  const onKeyDown = (e: KeyboardEvent) => {
    if (e.key !== 'Enter' || e.isComposing) return;
    const force = e.metaKey || e.ctrlKey;
    if (force || (!coarse && !e.shiftKey)) {
      e.preventDefault();
      if (!streaming && canSend) p.onSend();
    }
  };

  const onPaste = (e: ClipboardEvent) => {
    const list = e.clipboardData?.files;
    // Only intercept pasted files (a screenshot, a copied file); pasted text goes into the prompt as usual.
    // A copied image often carries its URL as text too: the image wins.
    if (list?.length && ([...list].some((f) => f.type.startsWith('image/')) || !e.clipboardData?.getData('text/plain'))) {
      e.preventDefault();
      p.onFiles([...list]);
    }
  };

  // Only say something under the prompt when it changes what will happen.
  // The privacy consequence is visible text whenever the switch is on (a tooltip alone never shows on touch).
  const modeHint = p.capsFailed
    ? 'Couldn’t check which modes are available right now.'
    : current && !current.available
      ? `${current.label} isn’t available${current.reason ? `: ${current.reason}` : ''}.`
      : privacy !== 'allow_jev'
        ? null
        : filesIncluded
          ? 'Files are attached, so this message stays Local only; Jev won’t see it.'
          : mode === 'auto'
            ? 'Marked public: Jev may choose which local model answers.'
            : 'Jev routing only applies in Auto; this mode goes straight to its local model.';

  const modeSelect = (
    <Select<AskMode>
      label="Mode"
      value={mode}
      options={modeOptions}
      onChange={p.onMode}
      hint={current?.blurb || (p.capsFailed ? 'Couldn’t check which modes are available right now.' : modes ? undefined : 'Availability not checked yet.')}
      class="ask-mode"
    />
  );
  const privacyDescription = !jevAvailable
    ? jevOption?.reason ?? 'Not available right now.'
    : mode === 'auto'
      ? 'Marks this prompt as public so Jev may pick the model. Messages with files always stay Local only.'
      : 'Only used in Auto mode; other modes go straight to their local model.';
  // Wide/medium: the same two choices, inline in the toolbar. The select keeps a real (sr-only) label and the
  // privacy chip is a switch named "Allow Jev routing"; what each does is in its tooltip and title.
  const inlineControls = (
    <>
      <span class="ask-mode-inline" title={current?.blurb || undefined}>
        <Select<AskMode> label="Mode" hideLabel value={mode} options={modeOptions} onChange={p.onMode} />
      </span>
      <Tooltip content={`${privacyDescription} ${externalNote}.`} side="top">
        <button
          type="button"
          role="switch"
          aria-checked={privacy === 'allow_jev'}
          aria-label={`${jevActive ? 'Local + Jev' : 'Local only'}: allow Jev routing`}
          disabled={!jevAvailable}
          class={cx('ask-privacy-chip', jevActive && 'is-on')}
          onClick={() => p.onPrivacy(privacy === 'allow_jev' ? 'local_only' : 'allow_jev')}
        >
          <Icon name={jevActive ? 'decision' : 'lock'} size={14} />
          <span>{jevActive ? 'Local + Jev' : 'Local only'}</span>
        </button>
      </Tooltip>
    </>
  );
  const privacySwitch = (
    <Switch
      class="ask-privacy"
      label="Allow Jev routing"
      checked={privacy === 'allow_jev'}
      disabled={!jevAvailable}
      onChange={(on) => p.onPrivacy(on ? 'allow_jev' : 'local_only')}
      description={privacyDescription}
    />
  );

  return (
    <div
      class={cx('ask-composer', compact && 'is-compact', p.hero && 'is-hero', dragging && 'is-dragging')}
      onDragOver={(e) => {
        if (!e.dataTransfer?.types.includes('Files')) return;
        e.preventDefault();
        setDragging(true);
      }}
      onDragLeave={(e) => {
        if (!(e.currentTarget as HTMLElement).contains(e.relatedTarget as Node | null)) setDragging(false);
      }}
      onDrop={(e) => {
        e.preventDefault();
        setDragging(false);
        if (e.dataTransfer?.files.length) p.onFiles([...e.dataTransfer.files]);
      }}
    >
      {compact && (
        <button type="button" class="ask-summary-chip" onClick={() => setOptionsOpen(true)} aria-haspopup="dialog">
          <Icon name="sliders" size={14} />
          <span>
            {modeLabel} · {privacyLabel}
          </span>
        </button>
      )}

      <form
        class="ask-box"
        onSubmit={(e) => {
          e.preventDefault();
          if (streaming) p.onStop();
          else if (canSend) p.onSend();
        }}
      >
        <Textarea
          label="What do you want to do?"
          hideLabel
          placeholder="What do you want to do?"
          value={value}
          onInput={(e) => onInput((e.currentTarget as HTMLTextAreaElement).value)}
          onKeyDown={onKeyDown}
          onPaste={onPaste}
          autoGrow
          maxRows={compact ? 6 : 14}
          rows={p.hero ? 4 : compact ? 1 : 2}
          textareaRef={p.textareaRef}
          enterkeyhint={coarse ? 'enter' : 'send'}
          autoCapitalize="sentences"
          spellcheck
          data-slash-focus=""
          class="ask-textarea"
        />
        {(dictation.listening || dictation.interim) && (
          <p class="ask-interim small" aria-live="polite">
            <Icon name="mic" size={14} /> {dictation.interim || 'Listening…'}
          </p>
        )}
        {dictation.error && (
          <p class="small lz-tone-warning" role="status">
            {dictation.error}
          </p>
        )}

        {images.length > 0 && (
          <ul role="list" class="ask-thumbs" aria-label="Attached images">
            {images.map((f) => (
              <li key={f.id} class="ask-thumb">
                <img src={f.image} alt={f.name} width={f.width} height={f.height} decoding="async" />
                <span class="ask-thumb-meta xsmall num" title={`${f.name} · ${fileSize(f.size)} after resizing`}>
                  {f.width}×{f.height}
                </span>
                <IconButton icon="x" size="sm" variant="secondary" class="ask-thumb-x" label={`Remove ${f.name}`} onClick={() => p.onRemoveFile(f.id)} />
              </li>
            ))}
          </ul>
        )}
        {others.length > 0 && (
          <div class="ask-files">
            <ul role="list" aria-label="Attached files">
              {others.map((f) => (
                <li key={f.id} class={cx('ask-file', !f.included && 'is-skipped')}>
                  <Icon name={f.kind === 'image' ? 'image' : f.kind === 'code' ? 'code' : 'file'} size={16} />
                  <span class="grow">
                    <span class="ask-file-name truncate">{f.name}</span>
                    <span class="ask-file-meta xsmall">
                      {fileSize(f.size)} · {f.included ? 'Text included' : f.note}
                    </span>
                  </span>
                  <IconButton icon="x" size="sm" label={`Remove ${f.name}`} onClick={() => p.onRemoveFile(f.id)} />
                </li>
              ))}
            </ul>
            <PrivacyBadge privacy="local_only" prefix="Processing:" />
            {privacy === 'allow_jev' && filesIncluded && <span class="xsmall muted">Messages with files never go to Jev.</span>}
          </div>
        )}

        <div class="ask-toolbar">
          {[fileInput, cameraInput].map((ref, i) => (
            <input
              key={i}
              ref={ref}
              type="file"
              multiple={i === 0}
              accept={i === 1 ? 'image/*' : undefined}
              capture={i === 1 ? 'environment' : undefined}
              hidden
              onChange={(e) => {
                const el = e.currentTarget as HTMLInputElement;
                if (el.files?.length) p.onFiles([...el.files]);
                el.value = '';
              }}
            />
          ))}
          <IconButton
            icon="attach"
            label={files.length >= MAX_FILES ? `Up to ${MAX_FILES} files` : 'Attach files or images (read on this device)'}
            disabled={files.length >= MAX_FILES}
            onClick={() => fileInput.current?.click()}
          />
          {coarse && (
            <IconButton icon="camera" label="Take a photo" disabled={files.length >= MAX_FILES} onClick={() => cameraInput.current?.click()} />
          )}
          {dictation.supported && <IconButton icon="mic" label={dictation.listening ? 'Stop dictation' : 'Dictate'} pressed={dictation.listening} onClick={toggleVoice} />}
          {!compact && inlineControls}
          <span class="grow" />
          {!compact && canSend && !streaming && (
            <span class="xsmall faint ask-keyhint" aria-hidden="true">
              {coarse ? '' : 'Enter to send · Shift+Enter for a new line'}
            </span>
          )}
          {streaming ? (
            <Button type="submit" variant="secondary" icon="stop" loading={p.busy} class="ask-send">
              Stop
            </Button>
          ) : (
            <Button type="submit" variant="primary" icon="send" loading={p.busy} disabled={!canSend} class="ask-send">
              Send
            </Button>
          )}
        </div>
      </form>

      {p.notice && (
        <p class="ask-hint ask-hint-row xsmall" role="status">
          {p.notice}
        </p>
      )}
      {p.blocked && (
        <p class="ask-note lz-tone-info ask-blocked" role="note">
          <Icon name="info" size={16} />
          <span>{p.blocked}</span>
        </p>
      )}
      {!compact && modeHint && !p.blocked && <p class="ask-hint xsmall muted">{modeHint}</p>}

      {compact && (
        <Sheet open={optionsOpen} onClose={() => setOptionsOpen(false)} title="Ask options" footer={<Button variant="primary" block onClick={() => setOptionsOpen(false)}>Done</Button>}>
          <div class="stack">
            {modeSelect}
            {privacySwitch}
            <p class="small muted">
              <Icon name="lock" size={14} /> {externalNote}
            </p>
          </div>
        </Sheet>
      )}

      <Dialog
        open={disclose}
        onClose={() => setDisclose(false)}
        title="Dictation uses your browser’s speech service"
        size="sm"
        footer={
          <>
            <Button variant="ghost" onClick={() => setDisclose(false)}>
              Keep typing
            </Button>
            <Button
              variant="primary"
              icon="mic"
              onClick={() => {
                try {
                  localStorage.setItem(DICTATION_OK, '1');
                } catch {
                  /* convenience only: ask again next time */
                }
                setDisclose(false);
                dictation.start();
              }}
            >
              Use dictation
            </Button>
          </>
        }
      >
        <p>
          Labzilla never receives your audio, but your browser may send it to its vendor’s cloud service to turn it into text (Chrome and Edge
          do). If that matters for this prompt, type or paste instead. The resulting text follows this prompt’s privacy setting like anything typed.
        </p>
      </Dialog>
    </div>
  );
}

import type { Ref } from 'preact';
import { useEffect, useRef, useState } from 'preact/hooks';
import { useLocation } from 'preact-iso/router';
import { post, toHumanError } from '@/api/client';
import type { CommandResolution, HumanError, PromptSuggestion } from '@/api/contracts.gen';
import { CommandInput } from '@/ui/CommandInput';
import { HumanErrorCard } from '@/ui/HumanErrorCard';
import { IconButton } from '@/ui/IconButton';
import { Sheet } from '@/ui/Sheet';
import { cx } from '@/ui/tone';
import { CommandResult } from './CommandResult';
import { commandRequest, promptHandoff } from './commands';

export interface CommandBarProps {
  /** desktop: bottom-centre of the content column; mobile: fixed above the bottom nav (§7, §44, §45). */
  placement: 'desktop' | 'mobile';
  /** Focused by "/" (§49). */
  inputRef?: Ref<HTMLInputElement>;
  placeholder?: string;
  /** Hide the input only (pages with their own prompt). The resolver and result sheet stay mounted so
   *  palette commands ("Pause Batch", free text) still resolve on those pages. */
  hidden?: boolean;
}

/**
 * The persistent LABZILLA COMMAND BAR: one input that routes intent (§7, §8) via POST /api/command.
 * - ai_prompt → Ask, prompt handed over (not in the URL) and sent there;
 * - navigation → go; knowledge_query → Knowledge search;
 * - system/model/agent/operational → answered in place, with at most one ProposedAction whose impact
 *   is shown before it runs (§110). Desktop shows the answer inline above the bar; phones use a sheet.
 */
export function CommandBar({ placement, inputRef, placeholder = 'Ask Labzilla or type a command…', hidden }: CommandBarProps) {
  const { route } = useLocation();
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<CommandResolution | null>(null);
  const [error, setError] = useState<HumanError | null>(null);
  const panel = useRef<HTMLDivElement>(null);
  const root = useRef<HTMLDivElement>(null);
  const inline = placement === 'desktop' && !hidden;
  const open = !!result || !!error;

  const ask = (p: PromptSuggestion) => {
    promptHandoff.set(p);
    setResult(null);
    setError(null);
    setText('');
    route('/ask');
  };

  const go = (href: string) => {
    setResult(null);
    setError(null);
    setText('');
    route(href);
  };

  const submit = async (value: string) => {
    const t = value.trim();
    if (!t || busy) return;
    setBusy(true);
    setError(null);
    try {
      const r = await post<CommandResolution>('/api/command', { text: t });
      const plain = !r.answer && !r.proposed_action;
      if (r.kind === 'ai_prompt' && plain) return ask(r.prompt ?? { text: t, mode: 'auto' });
      if (r.kind === 'navigation' && r.navigate && plain) return go(r.navigate);
      if (r.kind === 'knowledge_query' && plain) return go(r.navigate ?? `/knowledge?q=${encodeURIComponent(t)}`);
      setResult(r);
    } catch (e) {
      setResult(null);
      setError(toHumanError(e));
    } finally {
      setBusy(false);
    }
  };

  useEffect(
    () =>
      commandRequest.subscribe((req) => {
        if (!req) return;
        setText(req.text);
        void submit(req.text);
      }),
    [],
  );

  const close = () => {
    setResult(null);
    setError(null);
  };

  // Inline answers take focus (screen readers read the answer) and Escape returns to the input.
  useEffect(() => {
    if (inline && open) panel.current?.focus();
  }, [inline, open, result, error]);

  const body = (
    <>
      {result && <CommandResult resolution={result} onClose={close} onAsk={ask} onNavigate={go} onActionDone={() => setText('')} />}
      {error && <HumanErrorCard error={error} onRetry={() => void submit(text)} />}
    </>
  );

  return (
    <div ref={root} class={cx('lz-commandbar', `lz-commandbar-${placement}`, hidden && 'is-hidden')}>
      {inline && open && (
        <div
          ref={panel}
          class="lz-cmd-panel"
          role="region"
          aria-label="Command result"
          tabIndex={-1}
          onKeyDown={(e) => {
            // A ConfirmDialog opened from the result handles its own Escape.
            if (e.key !== 'Escape' || (e.target as HTMLElement).closest('dialog')) return;
            e.stopPropagation();
            close();
            root.current?.querySelector<HTMLInputElement>('input')?.focus();
          }}
        >
          <IconButton icon="x" label="Close result" size="sm" variant="ghost" class="lz-cmd-panel-close" onClick={close} />
          {body}
        </div>
      )}
      {!hidden && (
        <CommandInput
          value={text}
          onInput={setText}
          onSubmit={(v) => void submit(v)}
          busy={busy}
          inputRef={inputRef}
          placeholder={placeholder}
          shortcutHint={placement === 'desktop'}
          enterKeyHint="go"
        />
      )}
      {!inline && (
        <Sheet open={open} onClose={close} title={result ? 'Labzilla' : 'Command'}>
          {body}
        </Sheet>
      )}
    </div>
  );
}

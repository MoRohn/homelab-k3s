import type { Ref } from 'preact';
import { Icon } from './Icon';
import { IconButton } from './IconButton';
import { cx } from './tone';

export interface CommandInputProps {
  value: string;
  onInput: (value: string) => void;
  onSubmit: (value: string) => void;
  placeholder?: string;
  /** Accessible label (default "Ask Labzilla or type a command"). */
  label?: string;
  /** Submission in flight: shows progress, keeps the text. */
  busy?: boolean;
  /** Present only when speech recognition is available (§19); the parent feature-detects. */
  onVoice?: () => void;
  listening?: boolean;
  /** Present when file intake is offered here (§20). */
  onAttach?: () => void;
  /** Show the "/" shortcut hint (desktop). */
  shortcutHint?: boolean;
  inputRef?: Ref<HTMLInputElement>;
  autoFocus?: boolean;
  /** Enter key label on mobile keyboards. */
  enterKeyHint?: 'send' | 'go' | 'search';
  /** Paste hook: a one-line input drops line breaks, so callers can take multi-line text elsewhere. */
  onPaste?: (e: ClipboardEvent) => void;
  class?: string;
}

/** The one-line prompt/command field used by the command bar and the mobile gateway (§7, §12, §85). */
export function CommandInput({
  value,
  onInput,
  onSubmit,
  placeholder = 'Ask Labzilla…',
  label = 'Ask Labzilla or type a command',
  busy,
  onVoice,
  listening,
  onAttach,
  shortcutHint,
  inputRef,
  autoFocus,
  enterKeyHint = 'send',
  onPaste,
  class: cls,
}: CommandInputProps) {
  return (
    <form
      class={cx('lz-command', busy && 'is-busy', cls)}
      role="search"
      onSubmit={(e) => {
        e.preventDefault();
        if (value.trim() && !busy) onSubmit(value.trim());
      }}
    >
      <Icon name="sparkle" size={18} class="lz-command-icon" />
      <input
        ref={inputRef}
        class="lz-command-input"
        type="text"
        value={value}
        placeholder={placeholder}
        aria-label={label}
        autoComplete="off"
        autoCapitalize="sentences"
        spellcheck
        enterKeyHint={enterKeyHint}
        autoFocus={autoFocus}
        onInput={(e) => onInput((e.currentTarget as HTMLInputElement).value)}
        onPaste={onPaste}
      />
      {shortcutHint && !value && (
        <kbd class="lz-command-kbd" aria-hidden="true">
          /
        </kbd>
      )}
      {onAttach && <IconButton icon="attach" label="Attach a file" size="sm" onClick={onAttach} />}
      {onVoice && <IconButton icon="mic" label={listening ? 'Stop dictation' : 'Dictate'} size="sm" pressed={listening} onClick={onVoice} />}
      <IconButton icon="send" label="Send" variant="primary" size="sm" type="submit" disabled={!value.trim() || busy} aria-busy={busy || undefined} />
    </form>
  );
}

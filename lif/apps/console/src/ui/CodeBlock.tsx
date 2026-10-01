import { useState } from 'preact/hooks';
import { useCopy } from './clipboard';
import { IconButton } from './IconButton';
import { cx } from './tone';

export interface CodeBlockProps {
  code: string;
  /** Shown as a small label ("python", "json"); no syntax highlighter is shipped (§93). */
  language?: string;
  /** Copy button (default true). Works on insecure LAN origins too (clipboard.ts fallback). */
  copy?: boolean;
  /** Initial soft-wrap state; the user can flip it with the wrap toggle. Default false (scroll inside the block). */
  wrap?: boolean;
  /** Show the wrap toggle (default true when the code has a line longer than ~60 chars). */
  wrapToggle?: boolean;
  maxHeight?: string;
  class?: string;
}

/** Monospaced block for model output, logs and raw values. Long lines scroll inside the block, never the page (§102). */
export function CodeBlock({ code, language, copy = true, wrap = false, wrapToggle, maxHeight, class: cls }: CodeBlockProps) {
  const [wrapped, setWrapped] = useState(wrap);
  const [copied, doCopy] = useCopy();
  const showWrap = wrapToggle ?? code.split('\n').some((l) => l.length > 60);
  return (
    <div class={cx('lz-code', wrapped && 'wrap', cls)}>
      {(language || copy || showWrap) && (
        <div class="lz-code-bar">
          <span class="lz-code-lang">{language}</span>
          <span class="lz-code-tools">
            <span class="sr-only" aria-live="polite">
              {copied === 'ok' ? 'Copied to clipboard' : copied === 'failed' ? 'Copy failed — select the text and copy it manually' : ''}
            </span>
            {showWrap && <IconButton icon="wrap" label="Wrap long lines" size="sm" pressed={wrapped} onClick={() => setWrapped(!wrapped)} class="lz-code-wrap-toggle" />}
            {copy && <IconButton icon={copied === 'ok' ? 'check' : 'copy'} label={copied === 'ok' ? 'Copied' : 'Copy'} size="sm" onClick={() => doCopy(code)} />}
          </span>
        </div>
      )}
      <pre style={maxHeight ? { maxHeight } : undefined} tabIndex={0} aria-label={language ? `${language} code` : 'Code'}>
        <code>{code}</code>
      </pre>
    </div>
  );
}

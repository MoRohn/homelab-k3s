import type { Ref, TextareaHTMLAttributes } from 'preact';
import { useId, useLayoutEffect, useRef } from 'preact/hooks';
import { cx } from './tone';

export interface TextareaProps extends Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, 'class' | 'className' | 'label'> {
  label?: string;
  hideLabel?: boolean;
  hint?: string;
  error?: string;
  /** Grow with content up to maxRows (prompt inputs). */
  autoGrow?: boolean;
  maxRows?: number;
  textareaRef?: Ref<HTMLTextAreaElement>;
  class?: string;
}

export function Textarea({ label, hideLabel, hint, error, autoGrow, maxRows = 10, textareaRef, class: cls, id, value, ...rest }: TextareaProps) {
  const auto = useId();
  const fid = (id as string | undefined) ?? auto;
  const own = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    const el = own.current;
    if (!autoGrow || !el) return;
    el.style.height = 'auto';
    const line = parseFloat(getComputedStyle(el).lineHeight) || 22;
    el.style.height = `${Math.min(el.scrollHeight, line * maxRows + 16)}px`;
  }, [value, autoGrow, maxRows]);
  const setRef = (el: HTMLTextAreaElement | null) => {
    own.current = el;
    if (typeof textareaRef === 'function') textareaRef(el);
    else if (textareaRef) textareaRef.current = el;
  };
  return (
    <div class={cx('lz-field', cls)}>
      {label && (
        <label for={fid} class={hideLabel ? 'sr-only' : 'lz-label'}>
          {label}
        </label>
      )}
      <textarea
        {...rest}
        id={fid}
        ref={setRef}
        value={value}
        class={cx('lz-input', 'lz-textarea', error && 'is-invalid')}
        aria-invalid={error ? true : undefined}
        aria-describedby={hint || error ? `${fid}-msg` : undefined}
      />
      {(hint || error) && (
        <p id={`${fid}-msg`} class={error ? 'lz-error-text' : 'lz-hint'}>
          {error ?? hint}
        </p>
      )}
    </div>
  );
}

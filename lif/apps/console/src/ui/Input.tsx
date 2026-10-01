import type { InputHTMLAttributes, Ref } from 'preact';
import { useId } from 'preact/hooks';
import { Icon, type IconName } from './Icon';
import { cx } from './tone';

export interface InputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'class' | 'className' | 'icon' | 'label'> {
  label?: string;
  /** Visually hide the label but keep it for screen readers. */
  hideLabel?: boolean;
  hint?: string;
  /** Human error text; sets aria-invalid. */
  error?: string;
  icon?: IconName;
  inputRef?: Ref<HTMLInputElement>;
  class?: string;
}

export function Input({ label, hideLabel, hint, error, icon, inputRef, class: cls, id, ...rest }: InputProps) {
  const auto = useId();
  const fid = (id as string | undefined) ?? auto;
  const describedBy = [hint && `${fid}-hint`, error && `${fid}-err`].filter(Boolean).join(' ') || undefined;
  return (
    <div class={cx('lz-field', cls)}>
      {label && (
        <label for={fid} class={hideLabel ? 'sr-only' : 'lz-label'}>
          {label}
        </label>
      )}
      <div class={cx('lz-input-wrap', icon && 'has-icon', error && 'is-invalid')}>
        {icon && <Icon name={icon} size={18} class="lz-input-icon" />}
        <input {...rest} id={fid} ref={inputRef} class="lz-input" aria-invalid={error ? true : undefined} aria-describedby={describedBy} />
      </div>
      {hint && !error && <p id={`${fid}-hint`} class="lz-hint">{hint}</p>}
      {error && <p id={`${fid}-err`} class="lz-error-text">{error}</p>}
    </div>
  );
}

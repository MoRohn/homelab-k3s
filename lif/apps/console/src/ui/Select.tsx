import { useId } from 'preact/hooks';
import { cx } from './tone';

export interface SelectOption<T extends string = string> {
  value: T;
  label: string;
  description?: string;
  disabled?: boolean;
}

export interface SelectProps<T extends string = string> {
  label: string;
  hideLabel?: boolean;
  value: T;
  options: SelectOption<T>[];
  onChange: (value: T) => void;
  hint?: string;
  disabled?: boolean;
  id?: string;
  class?: string;
}

/** Native <select>: the most accessible and touch-friendly choice on every platform (§43). */
export function Select<T extends string = string>({ label, hideLabel, value, options, onChange, hint, disabled, id, class: cls }: SelectProps<T>) {
  const auto = useId();
  const fid = id ?? auto;
  return (
    <div class={cx('lz-field', cls)}>
      <label for={fid} class={hideLabel ? 'sr-only' : 'lz-label'}>
        {label}
      </label>
      <select
        id={fid}
        class="lz-input lz-select"
        value={value}
        disabled={disabled}
        aria-describedby={hint ? `${fid}-hint` : undefined}
        onChange={(e) => onChange((e.currentTarget as HTMLSelectElement).value as T)}
      >
        {options.map((o) => (
          <option key={o.value} value={o.value} disabled={o.disabled} title={o.description}>
            {o.label}
          </option>
        ))}
      </select>
      {hint && <p id={`${fid}-hint`} class="lz-hint">{hint}</p>}
    </div>
  );
}

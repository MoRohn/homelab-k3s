import { useId } from 'preact/hooks';
import { cx } from './tone';

export interface SwitchProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label: string;
  /** What flipping it does, shown before the user flips it (§110). */
  description?: string;
  disabled?: boolean;
  /** Change in flight: the switch shows progress and ignores input. */
  busy?: boolean;
  class?: string;
}

/** role="switch" on a real button: Space/Enter toggle, state read as on/off. The visible label is a
 *  <label for> so tapping the words toggles too — a 44px-wide target is easy to miss on a phone (§48). */
export function Switch({ checked, onChange, label, description, disabled, busy, class: cls }: SwitchProps) {
  const id = useId();
  return (
    <div class={cx('lz-switch-row', cls)}>
      <div class="lz-switch-text">
        <label id={`${id}-l`} for={`${id}-s`} class="lz-switch-label">
          {label}
        </label>
        {description && <span id={`${id}-d`} class="lz-hint">{description}</span>}
      </div>
      <button
        type="button"
        role="switch"
        id={`${id}-s`}
        class={cx('lz-switch', checked && 'is-on', busy && 'is-busy')}
        aria-checked={checked}
        aria-labelledby={`${id}-l`}
        aria-describedby={description ? `${id}-d` : undefined}
        aria-busy={busy || undefined}
        disabled={disabled || busy}
        onClick={() => onChange(!checked)}
      >
        <span class="lz-switch-thumb" />
      </button>
    </div>
  );
}

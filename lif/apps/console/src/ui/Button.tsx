import type { ButtonHTMLAttributes, ComponentChildren } from 'preact';
import { Icon, type IconName } from './Icon';
import { cx } from './tone';

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger';
export type ButtonSize = 'sm' | 'md' | 'lg';

export interface ButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'size' | 'icon' | 'class' | 'className' | 'loading'> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** Shows a spinner, sets aria-busy and blocks clicks; keep the label so the button doesn't jump. */
  loading?: boolean;
  icon?: IconName;
  iconRight?: IconName;
  /** One-line consequence under the label ("Pauses 3 jobs; they resume when you un-pause") — §110. */
  subtitle?: string;
  /** Stretch to the container width. */
  block?: boolean;
  type?: 'button' | 'submit' | 'reset';
  class?: string;
  children?: ComponentChildren;
}

export function Button({
  variant = 'secondary',
  size = 'md',
  loading = false,
  icon,
  iconRight,
  subtitle,
  block,
  type = 'button',
  class: cls,
  children,
  disabled,
  ...rest
}: ButtonProps) {
  return (
    <button
      {...rest}
      type={type}
      class={cx('lz-btn', `lz-btn-${variant}`, `lz-btn-${size}`, block && 'lz-btn-block', subtitle && 'lz-btn-sub', cls)}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
    >
      {loading ? <span class="lz-spinner" aria-hidden="true" /> : icon && <Icon name={icon} size={size === 'sm' ? 16 : 18} />}
      {(children || subtitle) && (
        <span class="lz-btn-text">
          <span class="lz-btn-label">{children}</span>
          {subtitle && <span class="lz-btn-subtitle">{subtitle}</span>}
        </span>
      )}
      {iconRight && !loading && <Icon name={iconRight} size={size === 'sm' ? 16 : 18} />}
    </button>
  );
}

import type { ButtonHTMLAttributes } from 'preact';
import type { ButtonVariant } from './Button';
import { Icon, type IconName } from './Icon';
import { cx } from './tone';

export interface IconButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'size' | 'icon' | 'class' | 'className' | 'label'> {
  icon: IconName;
  /** Required accessible name (also the tooltip). */
  label: string;
  variant?: Exclude<ButtonVariant, 'primary'> | 'primary';
  size?: 'sm' | 'md';
  /** Small count badge (notifications, approvals); 0/undefined hides it. */
  badge?: number;
  pressed?: boolean;
  type?: 'button' | 'submit';
  class?: string;
}

export function IconButton({ icon, label, variant = 'ghost', size = 'md', badge, pressed, type = 'button', class: cls, ...rest }: IconButtonProps) {
  return (
    <button
      {...rest}
      type={type}
      class={cx('lz-iconbtn', `lz-btn-${variant}`, `lz-iconbtn-${size}`, cls)}
      aria-label={badge ? `${label} (${badge})` : label}
      aria-pressed={pressed}
      title={label}
    >
      <Icon name={icon} size={size === 'sm' ? 16 : 20} />
      {!!badge && <span class="lz-iconbtn-badge num" aria-hidden="true">{badge > 99 ? '99+' : badge}</span>}
    </button>
  );
}

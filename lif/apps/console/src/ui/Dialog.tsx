import type { ComponentChildren } from 'preact';
import { useId } from 'preact/hooks';
import { IconButton } from './IconButton';
import { Modal } from './Modal';

export interface DialogProps {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: string;
  children?: ComponentChildren;
  /** Action buttons, right-aligned (primary last; stacked full-width on compact screens). */
  footer?: ComponentChildren;
  role?: 'dialog' | 'alertdialog';
  size?: 'sm' | 'md' | 'lg';
  /** False while something is in flight: no Close button, Escape and backdrop do nothing. */
  dismissible?: boolean;
}

/** Use sparingly (§4: no needless modals) — confirmations and focused tasks only. */
export function Dialog({ open, onClose, title, description, children, footer, role = 'dialog', size = 'md', dismissible = true }: DialogProps) {
  const id = useId();
  return (
    <Modal
      open={open}
      onClose={onClose}
      variant="dialog"
      class={`lz-dialog-${size}`}
      labelledBy={`${id}-t`}
      describedBy={description ? `${id}-d` : undefined}
      role={role}
      dismissible={dismissible}
    >
      <header class="lz-modal-header">
        <h2 id={`${id}-t`}>{title}</h2>
        {dismissible && <IconButton icon="x" label="Close" onClick={onClose} size="sm" />}
      </header>
      {description && (
        <p id={`${id}-d`} class="lz-modal-desc">
          {description}
        </p>
      )}
      {children && <div class="lz-modal-content">{children}</div>}
      {footer && <footer class="lz-modal-footer">{footer}</footer>}
    </Modal>
  );
}

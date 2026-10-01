import type { ComponentChildren } from 'preact';
import { useId } from 'preact/hooks';
import { IconButton } from './IconButton';
import { Modal } from './Modal';

export interface DrawerProps {
  open: boolean;
  onClose: () => void;
  title: string;
  subtitle?: string;
  side?: 'right' | 'left';
  /** CSS width on wide screens (default 440px); full width on compact. */
  width?: string;
  children: ComponentChildren;
  footer?: ComponentChildren;
}

/** Side panel for detail on demand (Technical Details §79, notifications, navigation on medium). */
export function Drawer({ open, onClose, title, subtitle, side = 'right', width, children, footer }: DrawerProps) {
  const id = useId();
  return (
    <Modal open={open} onClose={onClose} variant={side === 'left' ? 'drawer-left' : 'drawer-right'} labelledBy={`${id}-t`}>
      <div class="lz-drawer" style={width ? { '--drawer-w': width } : undefined}>
        <header class="lz-modal-header">
          <div class="grow">
            <h2 id={`${id}-t`}>{title}</h2>
            {subtitle && <p class="muted small">{subtitle}</p>}
          </div>
          <IconButton icon="x" label="Close" onClick={onClose} size="sm" />
        </header>
        <div class="lz-drawer-content">{children}</div>
        {footer && <footer class="lz-modal-footer">{footer}</footer>}
      </div>
    </Modal>
  );
}

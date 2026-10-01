import type { ComponentChildren } from 'preact';
import { useId } from 'preact/hooks';
import { IconButton } from './IconButton';
import { Modal } from './Modal';

export interface SheetProps {
  open: boolean;
  onClose: () => void;
  title?: string;
  children: ComponentChildren;
  footer?: ComponentChildren;
  /** 'auto' hugs content (max 85vh); 'full' fills the viewport below the status bar. */
  height?: 'auto' | 'full';
  /** Accessible name when there is no title (e.g. "More"). */
  ariaLabel?: string;
}

/** Mobile bottom sheet (More menu, command results, consent §65). On wide screens it renders as a centred dialog.
 *  Always has a visible Close: touch screen-reader users have no Escape key and may not find the backdrop. */
export function Sheet({ open, onClose, title, children, footer, height = 'auto', ariaLabel }: SheetProps) {
  const id = useId();
  return (
    <Modal
      open={open}
      onClose={onClose}
      variant="sheet"
      class={height === 'full' ? 'lz-sheet-full' : undefined}
      labelledBy={title ? `${id}-t` : undefined}
      label={ariaLabel}
    >
      <div class="lz-sheet-grip" aria-hidden="true" />
      <header class="lz-modal-header lz-sheet-header">
        {title ? <h2 id={`${id}-t`}>{title}</h2> : <span class="grow" />}
        <IconButton icon="x" label="Close" onClick={onClose} size="sm" />
      </header>
      <div class="lz-sheet-content">{children}</div>
      {footer && <footer class="lz-modal-footer">{footer}</footer>}
    </Modal>
  );
}

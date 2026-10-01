// Shared modal surface for Dialog, Drawer and Sheet: native <dialog>.showModal() gives focus
// containment, Escape (the `cancel` event), an inert background and top-layer stacking in every
// modern browser, with no focus-trap library. What the platform does not guarantee everywhere we
// do by hand: focus returns to the opener (also when the owner unmounts us while open), aria-modal
// for older screen readers, and a backdrop click that ignores drag-selections ending outside.
import type { ComponentChildren } from 'preact';
import { useEffect, useLayoutEffect, useRef } from 'preact/hooks';
import { cx } from './tone';

export interface ModalProps {
  open: boolean;
  onClose: () => void;
  /** Visual variant: centred dialog, side drawer, or bottom sheet. */
  variant: 'dialog' | 'drawer-right' | 'drawer-left' | 'sheet';
  labelledBy?: string;
  describedBy?: string;
  /** Accessible name when there is no visible heading to point labelledBy at. */
  label?: string;
  role?: 'dialog' | 'alertdialog';
  /** Clicking the backdrop or pressing Escape closes (default true; false while a confirmation is in flight). */
  dismissible?: boolean;
  class?: string;
  children: ComponentChildren;
}

const FOCUSABLE = 'input:not([disabled]):not([type=hidden]), textarea:not([disabled]), select:not([disabled]), button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])';

export function Modal({ open, onClose, variant, labelledBy, describedBy, label, role, dismissible = true, class: cls, children }: ModalProps) {
  const ref = useRef<HTMLDialogElement>(null);
  const opener = useRef<HTMLElement | null>(null);
  const downOnBackdrop = useRef(false);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  const restoreFocus = () => {
    const el = opener.current;
    opener.current = null;
    // Only restore when focus would otherwise be lost (the user may have clicked elsewhere on purpose).
    if (el?.isConnected && (!document.activeElement || document.activeElement === document.body || ref.current?.contains(document.activeElement)))
      el.focus({ preventScroll: true });
  };

  // Layout effect: open before paint so the first frame already has the backdrop and the focus.
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (open && !el.open) {
      opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      el.showModal();
      // showModal focuses the first focusable element, which is usually the header's Close button.
      // Prefer an explicit [autofocus], then the first field or action in the content.
      const preferred = el.querySelector<HTMLElement>('[autofocus]') ?? el.querySelector<HTMLElement>(`.lz-modal-content :is(${FOCUSABLE}), .lz-drawer-content :is(${FOCUSABLE}), .lz-sheet-content :is(${FOCUSABLE})`);
      preferred?.focus();
    } else if (!open && el.open) {
      el.close();
      restoreFocus();
    }
  }, [open]);

  useEffect(
    () => () => {
      if (ref.current?.open) ref.current.close();
      restoreFocus();
    },
    [],
  );

  return (
    <dialog
      ref={ref}
      class={cx('lz-modal', `lz-modal-${variant}`, cls)}
      role={role === 'alertdialog' ? 'alertdialog' : undefined}
      aria-modal="true"
      aria-labelledby={labelledBy}
      aria-label={labelledBy ? undefined : label}
      aria-describedby={describedBy}
      onCancel={(e) => {
        // Escape: never let the browser close it behind our back; the owner's state decides.
        e.preventDefault();
        if (dismissible) closeRef.current();
      }}
      onPointerDown={(e) => {
        downOnBackdrop.current = e.target === ref.current;
      }}
      onClick={(e) => {
        if (dismissible && downOnBackdrop.current && e.target === ref.current) closeRef.current();
        downOnBackdrop.current = false;
      }}
    >
      {open && <div class="lz-modal-body">{children}</div>}
    </dialog>
  );
}

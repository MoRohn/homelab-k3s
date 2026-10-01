import { useEffect, useId, useState } from 'preact/hooks';
import type { ActionPreview, HumanError } from '@/api/contracts.gen';
import { Button } from './Button';
import { Dialog } from './Dialog';
import { HumanErrorCard } from './HumanErrorCard';
import { Icon } from './Icon';
import { Input } from './Input';

export interface ConfirmDialogProps {
  open: boolean;
  /** From GET …/preview: what changes, what may be interrupted, whether rollback exists (§98). */
  preview: ActionPreview;
  /** Receives the typed text for confirm == "typed" (send it as `confirm` in the request body). */
  onConfirm: (typed?: string) => void;
  onCancel: () => void;
  busy?: boolean;
  /** Primary button text (default "Confirm"; prefer the verb: "Promote", "Delete model"). */
  confirmLabel?: string;
  tone?: 'danger' | 'primary';
  /** Server refusal shown inside the dialog without closing it. */
  error?: HumanError | null;
}

/** Strong confirmation for dangerous operations only (§41, §98): what changes, what may be interrupted,
 *  and whether it can be undone — before the button is pressed. confirm == "typed" also requires typing
 *  the exact confirm_text. Focus starts on Cancel (or the typed field) so a stray Enter never confirms.
 *  Routine safe actions (confirm == "none") should run directly and not open this at all. */
export function ConfirmDialog({ open, preview, onConfirm, onCancel, busy, confirmLabel = 'Confirm', tone = 'danger', error }: ConfirmDialogProps) {
  const [typed, setTyped] = useState('');
  const formId = useId();
  useEffect(() => {
    if (open) setTyped('');
  }, [open]);
  const needs = preview.confirm === 'typed' ? (preview.confirm_text ?? '') : null;
  const ok = needs === null || typed.trim() === needs;
  const submit = () => {
    if (ok && !busy) onConfirm(needs === null ? undefined : typed.trim());
  };
  return (
    <Dialog
      open={open}
      onClose={onCancel}
      title={preview.title}
      role="alertdialog"
      dismissible={!busy}
      footer={
        <>
          <Button variant="ghost" onClick={onCancel} disabled={busy} autofocus={needs === null}>
            Cancel
          </Button>
          <Button type="submit" form={formId} variant={tone} loading={busy} disabled={!ok}>
            {confirmLabel}
          </Button>
        </>
      }
    >
      <form
        id={formId}
        class="stack-sm lz-confirm"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        {preview.changes.length > 0 && (
          <section>
            <h3 class="section-title">What changes</h3>
            <ul class="lz-confirm-list">{preview.changes.map((c, i) => <li key={i}>{c}</li>)}</ul>
          </section>
        )}
        {preview.interrupts.length > 0 && (
          <section>
            <h3 class="section-title">What may be interrupted</h3>
            <ul class="lz-confirm-list">{preview.interrupts.map((c, i) => <li key={i}>{c}</li>)}</ul>
          </section>
        )}
        <section>
          <h3 class="section-title">Undo</h3>
          {preview.rollback ? (
            <p class="row lz-confirm-undo">
              <Icon name="rollback" size={16} class="lz-tone-info" />
              <span>{preview.rollback}</span>
            </p>
          ) : (
            <p class="row lz-confirm-undo">
              <Icon name="warning" size={16} class="lz-tone-warning" />
              <span>No rollback is offered for this action.</span>
            </p>
          )}
        </section>
        {needs !== null && (
          <Input
            label={`Type ${needs} to confirm`}
            hint="This can't be done by accident: the text must match exactly."
            value={typed}
            autoComplete="off"
            autoCapitalize="off"
            spellcheck={false}
            autofocus
            class="lz-confirm-typed"
            onInput={(e) => setTyped((e.currentTarget as HTMLInputElement).value)}
          />
        )}
        {error && <HumanErrorCard error={error} compact />}
      </form>
    </Dialog>
  );
}

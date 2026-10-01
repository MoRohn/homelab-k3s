import type { ComponentChildren } from 'preact';
import { useEffect, useId, useRef, useState } from 'preact/hooks';
import { cx } from './tone';

export interface TooltipProps {
  /** Short supplementary text. Never put essential information only in a tooltip (touch has no hover). */
  content: string;
  /** The trigger. Should contain one focusable element (button, link); otherwise set `focusable`. */
  children: ComponentChildren;
  side?: 'top' | 'bottom';
  /** Make the wrapper itself focusable when the trigger is plain text (e.g. an abbreviation). */
  focusable?: boolean;
  class?: string;
}

const FOCUSABLE = 'button, a[href], input, select, textarea, [tabindex]:not([tabindex="-1"])';

/** Shows on hover and on keyboard focus; Escape hides it without moving focus (WCAG 1.4.13). The bubble is
 *  wired to the trigger with aria-describedby, so screen readers hear it as the description. */
export function Tooltip({ content, children, side = 'top', focusable, class: cls }: TooltipProps) {
  const id = useId();
  const wrap = useRef<HTMLSpanElement>(null);
  const [shown, setShown] = useState(false);

  // Point the trigger's aria-describedby at the bubble (merging any ids it already has).
  useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const target = focusable ? el : el.querySelector<HTMLElement>(FOCUSABLE);
    if (!target) return;
    const prev = target.getAttribute('aria-describedby');
    const ids = new Set((prev ?? '').split(/\s+/).filter(Boolean));
    ids.add(id);
    target.setAttribute('aria-describedby', [...ids].join(' '));
    return () => {
      if (prev) target.setAttribute('aria-describedby', prev);
      else target.removeAttribute('aria-describedby');
    };
  }, [id, focusable]);

  return (
    <span
      ref={wrap}
      class={cx('lz-tooltip', `lz-tooltip-${side}`, shown && 'is-shown', cls)}
      tabIndex={focusable ? 0 : undefined}
      onMouseEnter={() => setShown(true)}
      onMouseLeave={() => setShown(false)}
      onFocusIn={(e) => setShown((e.target as HTMLElement).matches(':focus-visible'))}
      onFocusOut={() => setShown(false)}
      onKeyDown={(e) => {
        if (e.key === 'Escape' && shown) {
          // Hide only; don't let the same Escape close a surrounding dialog.
          e.preventDefault();
          e.stopPropagation();
          setShown(false);
        }
      }}
    >
      {children}
      <span role="tooltip" id={id} class="lz-tooltip-bubble">
        {content}
      </span>
    </span>
  );
}

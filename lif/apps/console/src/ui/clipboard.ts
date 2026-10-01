// Copy text with a fallback for insecure contexts. The console is often opened over plain-HTTP LAN
// or a not-yet-trusted certificate (brief §3, Trust page), where navigator.clipboard is undefined;
// the hidden-textarea + execCommand path still works there, so Copy buttons never silently vanish.
import { useEffect, useRef, useState } from 'preact/hooks';

export async function copyText(text: string): Promise<boolean> {
  try {
    if (typeof navigator !== 'undefined' && navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* permission denied or document not focused: try the legacy path */
  }
  if (typeof document === 'undefined') return false;
  const prev = document.activeElement as HTMLElement | null;
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', '');
  ta.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;opacity:0;pointer-events:none';
  // Inside a modal <dialog> the rest of the page is inert, so append next to the focused element.
  (prev?.closest('dialog') ?? document.body).appendChild(ta);
  ta.select();
  let ok = false;
  try {
    ok = document.execCommand('copy');
  } catch {
    ok = false;
  }
  ta.remove();
  prev?.focus({ preventScroll: true });
  return ok;
}

/** `[copied, copy]`: copied is 'ok' | 'failed' for ~1.5 s after a copy, then null (drives the check icon + live text). */
export function useCopy(): [null | 'ok' | 'failed', (text: string) => void] {
  const [state, setState] = useState<null | 'ok' | 'failed'>(null);
  const timer = useRef<ReturnType<typeof setTimeout>>();
  useEffect(() => () => clearTimeout(timer.current), []);
  const copy = (text: string) => {
    void copyText(text).then((ok) => {
      setState(ok ? 'ok' : 'failed');
      clearTimeout(timer.current);
      timer.current = setTimeout(() => setState(null), 1500);
    });
  };
  return [state, copy];
}

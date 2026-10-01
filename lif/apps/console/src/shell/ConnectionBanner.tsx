import { useEffect, useState } from 'preact/hooks';
import { get, reachable } from '@/api/client';
import { useObservable } from '@/api/observable';
import { reconnectNow, useConnectionState, type ConnectionState } from '@/api/sse';
import { invalidate } from '@/api/store';
import { Button } from '@/ui/Button';
import { Icon } from '@/ui/Icon';

export interface ConnectionBannerProps {
  /** Override for tests/stories; defaults to the live SSE + fetch state. */
  state?: ConnectionState;
  /** Grace period before showing "reconnecting" so brief blips don't flash a banner (default 4 s). */
  graceMs?: number;
}

/**
 * "Labzilla unavailable — Not connected to your local network — [Retry]" (§62). Non-blocking: it sits
 * above the content, and whatever was last loaded stays on screen (§60). Renders nothing when healthy.
 */
export function ConnectionBanner({ state, graceMs = 4000 }: ConnectionBannerProps) {
  const live = useConnectionState();
  const ok = useObservable(reachable);
  const s = state ?? live;
  const offline = s === 'offline' || !ok;
  const [show, setShow] = useState(false);
  const [retrying, setRetrying] = useState(false);
  useEffect(() => {
    if (!offline && s !== 'reconnecting') return setShow(false);
    const t = setTimeout(() => setShow(true), offline ? 0 : graceMs);
    return () => clearTimeout(t);
  }, [offline, s, graceMs]);

  // A real probe, not an optimistic hide: the client marks Labzilla reachable again only when it answers.
  const retry = async () => {
    setRetrying(true);
    try {
      await get('/api/auth/me', { allow401: true });
      invalidate('');
    } catch {
      /* still unreachable (or signed out, which the auth gate handles); the banner stays */
    } finally {
      reconnectNow();
      setRetrying(false);
    }
  };

  if (!show) return null;
  return (
    <div class="lz-conn-banner" role="status" aria-live="polite">
      <Icon name="wifi-off" size={18} />
      <p class="grow">
        <strong>{offline ? 'Labzilla unavailable' : 'Reconnecting to Labzilla…'}</strong>
        <span class="muted"> — {offline ? 'Not connected to your local network. What you see was last updated before the connection dropped.' : 'Live updates are paused; what you see may be out of date.'}</span>
      </p>
      <Button size="sm" loading={retrying} onClick={() => void retry()}>
        Retry
      </Button>
    </div>
  );
}

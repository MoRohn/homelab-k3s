// Phone side of device pairing (§15–§17, brief §3).
//   desktop shows QR → phone opens /pair#<token> → names itself → claims → both screens show the same
//   6-digit code → the desktop approves → the server sets this device's session cookie → Home.
// The token travels in the URL fragment (never sent to servers or logged) and is removed from the address
// bar as soon as it's read, so it can't leak through history or a shared screenshot. There is no session
// (and so no SSE) yet, so this page polls the claim status every 2 s until it settles.
import { useEffect, useRef, useState } from 'preact/hooks';
import { useLocation } from 'preact-iso/router';
import { ApiError, OfflineError, get, post, toHumanError } from '@/api/client';
import type { HumanError, PairClaimRequest, Pairing } from '@/api/contracts.gen';
import { usePageTitle } from '@/shell/usePageTitle';
import { Button } from '@/ui/Button';
import { HumanErrorCard } from '@/ui/HumanErrorCard';
import { Icon } from '@/ui/Icon';
import { Input } from '@/ui/Input';
import { Skeleton } from '@/ui/Skeleton';
import { confirmSession, guessDeviceName } from './authUtil';
import './auth.css';

type Phase = 'checking' | 'no-token' | 'name' | 'claimed' | 'approved' | 'rejected' | 'expired' | 'error';

const POLL_MS = 2000;

function readToken(): string | null {
  const raw = location.hash.replace(/^#/, '');
  if (!raw) return null;
  // Accept "#<token>" and "#token=<token>".
  const token = raw.startsWith('token=') ? new URLSearchParams(raw).get('token') : decodeURIComponent(raw);
  history.replaceState(history.state, '', location.pathname + location.search);
  return token && /^[A-Za-z0-9_-]{16,256}$/.test(token) ? token : null;
}

function useSecondsLeft(expiresAt: number | undefined): number | null {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!expiresAt) return;
    const t = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(t);
  }, [expiresAt]);
  return expiresAt ? Math.max(0, Math.round(expiresAt - now)) : null;
}

export default function Pair() {
  usePageTitle('Pair this device');
  const { route } = useLocation();
  const token = useRef<string | null>(null);
  const [phase, setPhase] = useState<Phase>('checking');
  const [name, setName] = useState(guessDeviceName);
  const [nameError, setNameError] = useState<string | undefined>();
  const [pairing, setPairing] = useState<Pairing | null>(null);
  const [error, setError] = useState<HumanError | null>(null);
  const [busy, setBusy] = useState(false);
  const [offline, setOffline] = useState(false);
  const left = useSecondsLeft(phase === 'claimed' ? pairing?.expires_at : undefined);

  const settle = async (p: Pairing) => {
    setPairing(p);
    if (p.status === 'approved') {
      setPhase('approved');
      try {
        const user = await confirmSession();
        if (user) return route('/?paired=1', true);
        setError({
          title: 'Approved, but this browser didn’t keep the session',
          impact: 'Labzilla approved this device, but the sign-in cookie was not stored.',
          next_step: 'Allow cookies for this site (and leave private browsing), then pair again from your desktop.',
          actions: [],
          tech: [],
        });
        setPhase('error');
      } catch (e) {
        setError(toHumanError(e));
        setPhase('error');
      }
    } else if (p.status === 'rejected' || p.status === 'expired') setPhase(p.status);
    else if (p.status === 'claimed') setPhase('claimed');
  };

  // On open: take the token from the fragment; without one, resume a claim already in progress (the
  // server knows it from the lz_pair cookie), e.g. after the phone reloaded the page.
  useEffect(() => {
    token.current = readToken();
    if (token.current) {
      // Give the server a chance to set its CSRF cookie before the claim POST (there's no session yet).
      void get('/api/setup', { allow401: true }).catch(() => undefined);
      return setPhase('name');
    }
    let live = true;
    get<Pairing>('/api/pair/status', { allow401: true }).then(
      (p) => live && (p.status === 'waiting' ? setPhase('no-token') : void settle(p)),
      () => live && setPhase('no-token'),
    );
    return () => {
      live = false;
    };
  }, []);

  useEffect(() => {
    if (phase !== 'claimed') return;
    let live = true;
    const t = setInterval(async () => {
      try {
        const p = await get<Pairing>('/api/pair/status', { allow401: true });
        if (!live) return;
        setOffline(false);
        if (p.status !== 'claimed') void settle(p);
      } catch (e) {
        if (!live) return;
        if (e instanceof OfflineError) return setOffline(true);
        // The claim cookie is gone or the pairing vanished server-side: treat as expired, never spin forever.
        if (e instanceof ApiError && (e.status === 401 || e.status === 404 || e.status === 410)) setPhase('expired');
      }
    }, POLL_MS);
    return () => {
      live = false;
      clearInterval(t);
    };
  }, [phase]);

  useEffect(() => {
    if (phase === 'claimed' && left === 0) setPhase('expired');
  }, [left, phase]);

  const claim = async (e: Event) => {
    e.preventDefault();
    const deviceName = name.trim();
    if (!deviceName) return setNameError('Give this device a name, e.g. “Kitchen iPad”');
    if (deviceName.length > 60) return setNameError('Keep the name under 60 characters');
    if (!token.current) return setPhase('no-token');
    setNameError(undefined);
    setBusy(true);
    setError(null);
    try {
      const p = await post<Pairing | undefined>('/api/pair/claim', { token: token.current, device_name: deviceName } satisfies PairClaimRequest, { allow401: true });
      token.current = null; // single use
      // If the claim answered with a bare acknowledgement, ask for the pairing itself (it holds the code).
      void settle(p?.status ? p : await get<Pairing>('/api/pair/status', { allow401: true }));
    } catch (err) {
      setError(toHumanError(err));
      // An expired or used token can't be retried from here; anything else (offline) can.
      if (err instanceof ApiError && [400, 404, 409, 410].includes(err.status)) {
        token.current = null;
        setPhase('error');
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div class="lz-auth">
      <header class="lz-auth-brand">
        <img src="/logo/mark.png" alt="" width={64} height={64} />
        <h1>Pair with Labzilla</h1>
      </header>

      <section class="lz-auth-card" aria-live="polite">
        {phase === 'checking' && <Skeleton lines={3} />}

        {phase === 'no-token' && (
          <div class="stack">
            <h2>Scan the code from your desktop</h2>
            <p>This page pairs a phone or tablet. On a computer that's already signed in, open <strong>Connect a phone</strong> and scan the QR code it shows with this device's camera.</p>
            <p class="small muted">If you scanned one already, it may have expired or been used. Start a new pairing on the desktop.</p>
            <a href="/login" class="small">
              Sign in with a passphrase instead
            </a>
          </div>
        )}

        {phase === 'name' && (
          <form onSubmit={(e) => void claim(e)} noValidate>
            <h2>Name this device</h2>
            <p class="small muted">You'll see this name on your desktop when you approve it, and in the list of paired devices.</p>
            <Input label="Device name" value={name} error={nameError} maxLength={60} autoComplete="off" onInput={(e) => setName((e.currentTarget as HTMLInputElement).value)} />
            {error && <HumanErrorCard error={error} compact />}
            <Button type="submit" variant="primary" size="lg" block loading={busy}>
              Pair this device
            </Button>
          </form>
        )}

        {phase === 'claimed' && pairing && (
          <div class="stack lz-center">
            <h2>Confirm this code on your desktop</h2>
            <p class="lz-pair-code num" aria-label={`Verification code ${(pairing.code ?? '').split('').join(' ')}`}>
              {pairing.code ?? '——————'}
            </p>
            <p class="small muted">Check that your desktop shows the same six digits, then approve there. This page continues on its own.</p>
            <p class="xsmall faint num" aria-live="off">
              {offline ? 'Waiting for the network…' : left !== null ? `Waiting for approval · expires in ${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')}` : 'Waiting for approval…'}
            </p>
          </div>
        )}

        {phase === 'approved' && (
          <div class="stack lz-center">
            <Icon name="success" size={40} class="lz-tone-success" />
            <h2>Paired</h2>
            <p class="muted">Opening Labzilla…</p>
          </div>
        )}

        {phase === 'rejected' && (
          <div class="stack lz-center">
            <Icon name="block" size={40} class="lz-tone-warning" />
            <h2>Pairing was declined</h2>
            <p class="muted">The desktop rejected this device, so nothing was set up here. If that was a mistake, start a new pairing on the desktop and scan again.</p>
          </div>
        )}

        {phase === 'expired' && (
          <div class="stack lz-center">
            <Icon name="clock" size={40} class="lz-tone-inactive" />
            <h2>This pairing code expired</h2>
            <p class="muted">Codes only last a couple of minutes, to keep strangers on your network out. On your desktop, start pairing again and scan the new code.</p>
          </div>
        )}

        {phase === 'error' && error && (
          <div class="stack">
            <HumanErrorCard error={error} />
            <p class="small muted">Start a new pairing on your desktop and scan the new code.</p>
          </div>
        )}
      </section>
    </div>
  );
}

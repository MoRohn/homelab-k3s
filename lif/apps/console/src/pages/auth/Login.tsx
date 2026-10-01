// Sign in (brief §3): name + passphrase. Sends first-run visitors to /setup (§83) and honours ?next=
// (same-origin paths only). Errors are human: a wrong passphrase, the rate limit, an unreachable server.
import { useEffect, useRef, useState } from 'preact/hooks';
import { useLocation } from 'preact-iso/router';
import { ApiError, get, post, toHumanError } from '@/api/client';
import type { HumanError, LoginRequest, SetupState } from '@/api/contracts.gen';
import { useMe } from '@/api/session';
import { usePageTitle } from '@/shell/usePageTitle';
import { Button } from '@/ui/Button';
import { HumanErrorCard } from '@/ui/HumanErrorCard';
import { Icon } from '@/ui/Icon';
import { Input } from '@/ui/Input';
import { confirmSession, safeNext } from './authUtil';
import './auth.css';

function loginError(e: unknown): HumanError {
  if (e instanceof ApiError && e.status === 429)
    return {
      title: 'Too many sign-in attempts',
      impact: 'Labzilla is pausing sign-ins from this device for a short while to protect your account.',
      next_step: 'Wait a minute, then try again.',
      actions: [],
      tech: e.error.tech,
    };
  if (e instanceof ApiError && e.status === 401 && !e.error.impact)
    return { title: "That name and passphrase don't match", impact: '', next_step: 'Check both and try again. Passphrases are case-sensitive.', actions: [], tech: [] };
  return toHumanError(e);
}

export default function Login() {
  usePageTitle('Sign in');
  const { query, route } = useLocation();
  const next = safeNext(query.next);
  const me = useMe();
  const [name, setName] = useState('');
  const [pass, setPass] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<HumanError | null>(null);
  const [missing, setMissing] = useState<{ name?: string; pass?: string }>({});
  const passRef = useRef<HTMLInputElement>(null);

  // Already signed in (e.g. the back button): go straight to where we were heading.
  useEffect(() => {
    if (me.data) route(next, true);
  }, [me.data]);

  // First run: no admin yet → setup. This GET also gives the server a chance to set the CSRF cookie
  // before the sign-in POST.
  useEffect(() => {
    let live = true;
    get<SetupState>('/api/setup', { allow401: true }).then(
      (s) => live && s.needs_setup && route('/setup', true),
      () => undefined,
    );
    return () => {
      live = false;
    };
  }, []);

  const submit = async (e: Event) => {
    e.preventDefault();
    const m = { name: name.trim() ? undefined : 'Enter your name', pass: pass ? undefined : 'Enter your passphrase' };
    setMissing(m);
    if (m.name || m.pass) return;
    setBusy(true);
    setError(null);
    try {
      await post('/api/auth/login', { name: name.trim(), passphrase: pass } satisfies LoginRequest, { allow401: true });
      const user = await confirmSession();
      if (!user) {
        setError({
          title: "Signed in, but this browser didn't keep the session",
          impact: 'The session cookie was not stored, so Labzilla still sees you as signed out.',
          next_step: 'Open Labzilla over HTTPS (see the address your owner shared), or allow cookies for this site.',
          actions: [],
          tech: [],
        });
        return;
      }
      route(next, true);
    } catch (err) {
      setPass('');
      setError(loginError(err));
      passRef.current?.focus();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div class="lz-auth">
      <header class="lz-auth-brand">
        <img src="/logo/mark.png" alt="" width={64} height={64} />
        <h1>Sign in to Labzilla</h1>
        <p>Your local AI console</p>
      </header>

      <section class="lz-auth-card" aria-label="Sign in">
        <form onSubmit={(e) => void submit(e)} noValidate>
          <Input
            label="Name"
            autoComplete="username"
            autoCapitalize="none"
            spellcheck={false}
            value={name}
            error={missing.name}
            onInput={(e) => setName((e.currentTarget as HTMLInputElement).value)}
            autoFocus
          />
          <Input
            label="Passphrase"
            type="password"
            autoComplete="current-password"
            value={pass}
            error={missing.pass}
            inputRef={passRef}
            onInput={(e) => setPass((e.currentTarget as HTMLInputElement).value)}
          />
          {error && <HumanErrorCard error={error} compact />}
          <Button type="submit" variant="primary" size="lg" block loading={busy}>
            Sign in
          </Button>
        </form>
      </section>

      <p class="lz-auth-note">
        <Icon name="phone" size={18} />
        <span>
          On a phone? You don't need a passphrase: on your desktop open <strong>Connect a phone</strong> and scan the QR code.
        </span>
      </p>
    </div>
  );
}

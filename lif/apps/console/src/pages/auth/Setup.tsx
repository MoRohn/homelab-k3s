// First-run setup (§83): Welcome → System detected → Create admin → Mobile gateway → AI defaults → Ready (§84).
// Nothing is sent until the last step, so going back and forth is free. Creating the admin needs the
// one-time setup code that only someone with access to the Labzilla host can read (brief §3): being on
// the LAN is not authorization (§16).
import { useEffect, useRef, useState } from 'preact/hooks';
import { useLocation } from 'preact-iso/router';
import { ApiError, get, post, toHumanError } from '@/api/client';
import type { AskMode, HumanError, SetupRequest, SetupState, SystemStatus } from '@/api/contracts.gen';
import { usePageTitle } from '@/shell/usePageTitle';
import { Button } from '@/ui/Button';
import { HumanErrorCard } from '@/ui/HumanErrorCard';
import { Icon } from '@/ui/Icon';
import { Input } from '@/ui/Input';
import { Skeleton } from '@/ui/Skeleton';
import { StatusDot } from '@/ui/StatusDot';
import { confirmSession } from './authUtil';
import './auth.css';

const STEPS = ['welcome', 'detected', 'account', 'gateway', 'defaults'] as const;
type Step = (typeof STEPS)[number] | 'ready';

const MIN_PASS = 10;

/** The modes a fresh install can default to. Vision is left out: no local vision model is deployed yet. */
const MODES: { value: AskMode; label: string; blurb: string }[] = [
  { value: 'auto', label: 'Auto (recommended)', blurb: 'Labzilla picks a local model for each request.' },
  { value: 'fast', label: 'Fast', blurb: 'Lowest latency; best for quick questions.' },
  { value: 'balanced', label: 'Balanced', blurb: 'A middle ground between speed and depth.' },
  { value: 'deep', label: 'Deep', blurb: 'The most capable local model; slower.' },
  { value: 'code', label: 'Code', blurb: 'Tuned for writing and reviewing code.' },
];

interface Account {
  code: string;
  name: string;
  pass: string;
  pass2: string;
}

function accountErrors(a: Account): Partial<Record<keyof Account, string>> {
  return {
    code: a.code.trim() ? undefined : 'Enter the setup code',
    name: a.name.trim() ? undefined : 'Choose a name to sign in with',
    pass: a.pass.length >= MIN_PASS ? undefined : `Use at least ${MIN_PASS} characters`,
    pass2: a.pass2 === a.pass ? undefined : "The passphrases don't match",
  };
}

export default function Setup() {
  usePageTitle('Set up Labzilla');
  const { route } = useLocation();
  const [state, setState] = useState<SetupState | null>(null);
  const [loadError, setLoadError] = useState<HumanError | null>(null);
  const [step, setStep] = useState<Step>('welcome');
  const [account, setAccount] = useState<Account>({ code: '', name: '', pass: '', pass2: '' });
  const [shown, setShown] = useState<Partial<Record<keyof Account, string>>>({});
  const [mode, setMode] = useState<AskMode>('auto');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<HumanError | null>(null);
  const [signedIn, setSignedIn] = useState(false);
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);

  const load = async () => {
    setLoadError(null);
    try {
      const s = await get<SetupState>('/api/setup', { allow401: true });
      // Someone already finished setup: this page has nothing to offer.
      if (!s.needs_setup && step !== 'ready') return route('/login', true);
      setState(s);
    } catch (e) {
      setLoadError(toHumanError(e));
    }
  };
  useEffect(() => void load(), []);

  // Each step change moves focus to its heading, so keyboard and screen-reader users start at the top.
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    heading.current?.focus();
  }, [step]);

  const go = (s: Step) => {
    setError(null);
    setStep(s);
  };

  const finish = async () => {
    setBusy(true);
    setError(null);
    try {
      const body: SetupRequest = { setup_code: account.code.trim(), name: account.name.trim(), passphrase: account.pass, default_mode: mode };
      await post('/api/setup', body, { allow401: true });
      const user = await confirmSession().catch(() => null);
      setSignedIn(!!user);
      if (user) setStatus(await get<SystemStatus>('/api/system/status').catch(() => null));
      setAccount((a) => ({ ...a, pass: '', pass2: '', code: '' }));
      setStep('ready');
    } catch (e) {
      const err = toHumanError(e);
      setError(err);
      // A refused code or name is fixed on the account step; keep everything typed so far except the code.
      if (e instanceof ApiError && [400, 401, 403, 409, 422].includes(e.status)) {
        setAccount((a) => ({ ...a, code: '' }));
        setStep('account');
      }
    } finally {
      setBusy(false);
    }
  };

  if (loadError)
    return (
      <div class="lz-auth">
        <Brand />
        <HumanErrorCard error={loadError} onRetry={() => void load()} />
      </div>
    );
  if (!state)
    return (
      <div class="lz-auth" aria-busy="true">
        <Brand />
        <div class="lz-auth-card">
          <Skeleton lines={4} />
        </div>
      </div>
    );

  if (!state.setup_code_configured)
    return (
      <div class="lz-auth">
        <Brand />
        <section class="lz-auth-card" aria-labelledby="setup-blocked">
          <h2 id="setup-blocked">Setup is locked until a setup code exists</h2>
          <p>
            To make sure only the owner can create the first admin account, Labzilla asks for a one-time setup code. None is configured on
            this installation yet.
          </p>
          <p class="lz-auth-note">
            <Icon name="key" size={18} />
            <span>
              On the Labzilla host, the homelab secrets script creates it as <code>secrets/lif-console-setup.code</code>. After it exists, the
              console needs a restart to pick it up. Then reload this page.
            </span>
          </p>
          <div class="lz-auth-actions">
            <Button variant="primary" icon="refresh" onClick={() => location.reload()}>
              Check again
            </Button>
          </div>
        </section>
      </div>
    );

  const index = step === 'ready' ? STEPS.length : STEPS.indexOf(step);

  return (
    <div class="lz-auth is-wide">
      <Brand />
      <section class="lz-auth-card" aria-labelledby="setup-step-title">
        {step !== 'ready' && (
          <div class="lz-steps">
            <p class="xsmall muted" aria-live="polite">
              Step {index + 1} of {STEPS.length}
            </p>
            <ol class="lz-steps-bar" aria-hidden="true">
              {STEPS.map((s, i) => (
                <li key={s} class={i < index ? 'is-done' : i === index ? 'is-current' : undefined} />
              ))}
            </ol>
          </div>
        )}

        {step === 'welcome' && (
          <>
            <h2 id="setup-step-title" ref={heading} tabIndex={-1}>
              Welcome to Labzilla
            </h2>
            <p>Labzilla runs AI models, agents and jobs on your own hardware. This takes about a minute: check the system, create your admin account, and choose how Ask behaves by default.</p>
            <div class="lz-auth-actions">
              <Button variant="primary" iconRight="arrow-right" onClick={() => go('detected')}>
                Get started
              </Button>
            </div>
          </>
        )}

        {step === 'detected' && (
          <>
            <h2 id="setup-step-title" ref={heading} tabIndex={-1}>
              System detected
            </h2>
            {state.detected.length ? (
              <ul class="lz-detected" aria-label="What Labzilla found">
                {state.detected.map((d) => (
                  <li key={d.label}>
                    <StatusDot health={d.ok ? 'healthy' : 'attention'} label={d.ok ? 'Found' : 'Needs attention'} />
                    <span class="lz-detected-label">{d.label}</span>
                    <span class="lz-detected-value">{d.value}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p class="muted">Labzilla couldn't inspect the hardware from here. That's fine: setup doesn't depend on it, and System shows it once you're in.</p>
            )}
            {state.detected.some((d) => !d.ok) && <p class="small muted">Items that need attention don't block setup; System explains them after you sign in.</p>}
            <StepNav onBack={() => go('welcome')} onNext={() => go('account')} />
          </>
        )}

        {step === 'account' && (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              const errs = accountErrors(account);
              setShown(errs);
              if (!Object.values(errs).some(Boolean)) go('gateway');
            }}
            noValidate
          >
            <h2 id="setup-step-title" ref={heading} tabIndex={-1}>
              Create the admin account
            </h2>
            {error && <HumanErrorCard error={error} compact />}
            <Input
              label="Setup code"
              autoComplete="one-time-code"
              autoCapitalize="none"
              spellcheck={false}
              value={account.code}
              error={shown.code}
              hint="Only someone with access to the Labzilla host can read it."
              onInput={(e) => setAccount({ ...account, code: (e.currentTarget as HTMLInputElement).value })}
            />
            <p class="lz-auth-note">
              <Icon name="key" size={18} />
              <span>
                Where to find it: on the Labzilla host, in <code>secrets/lif-console-setup.code</code> inside the labzilla checkout. It is used once.
              </span>
            </p>
            <Input
              label="Your name"
              autoComplete="username"
              autoCapitalize="none"
              spellcheck={false}
              value={account.name}
              error={shown.name}
              hint="You'll sign in with this."
              onInput={(e) => setAccount({ ...account, name: (e.currentTarget as HTMLInputElement).value })}
            />
            <Input
              label="Passphrase"
              type="password"
              autoComplete="new-password"
              value={account.pass}
              error={shown.pass}
              hint={`At least ${MIN_PASS} characters. A few unrelated words work well.`}
              onInput={(e) => setAccount({ ...account, pass: (e.currentTarget as HTMLInputElement).value })}
            />
            <Input
              label="Repeat passphrase"
              type="password"
              autoComplete="new-password"
              value={account.pass2}
              error={shown.pass2}
              onInput={(e) => setAccount({ ...account, pass2: (e.currentTarget as HTMLInputElement).value })}
            />
            <StepNav onBack={() => go('detected')} submit />
          </form>
        )}

        {step === 'gateway' && (
          <>
            <h2 id="setup-step-title" ref={heading} tabIndex={-1}>
              Mobile gateway
            </h2>
            <p>Phones and tablets on your home network can use Labzilla too. You pair each one by scanning a QR code from <strong>Connect a phone</strong>; nobody types an address or a key.</p>
            <dl class="lz-detected">
              <AddressRow label="Main address" url={state.public_url} />
              {state.alt_urls.map((u) => (
                <AddressRow key={u} label="Also works at" url={u} />
              ))}
              {state.lan_url && <AddressRow label="Fallback on this network" url={state.lan_url} />}
            </dl>
            <p class="lz-auth-note">
              <Icon name="shield" size={18} />
              <span>
                Being on your network is not enough to get in: every device signs in or is approved by you. Pages are served over HTTPS; if a
                browser warns about the certificate, <strong>Trust this device</strong> (after setup) explains how to make it trusted and what
                works until then.
              </span>
            </p>
            <StepNav onBack={() => go('account')} onNext={() => go('defaults')} />
          </>
        )}

        {step === 'defaults' && (
          <>
            <h2 id="setup-step-title" ref={heading} tabIndex={-1}>
              AI defaults
            </h2>
            <fieldset class="lz-mode-list">
              <legend class="small muted">Default mode for Ask. Anyone can still pick another mode per request.</legend>
              {MODES.map((m) => (
                <label key={m.value} class="lz-mode-opt">
                  <input type="radio" name="default-mode" value={m.value} checked={mode === m.value} onChange={() => setMode(m.value)} />
                  <span>
                    <strong>{m.label}</strong>
                    <span class="small muted">{m.blurb}</span>
                  </span>
                </label>
              ))}
            </fieldset>
            <p class="lz-auth-note">
              <Icon name="lock" size={18} />
              <span>Ask starts in <strong>Local only</strong> privacy: prompts are answered by models running on Labzilla.</span>
            </p>
            {error && <HumanErrorCard error={error} compact />}
            <div class="lz-auth-actions">
              <Button variant="ghost" class="grow-left" onClick={() => go('gateway')} disabled={busy}>
                Back
              </Button>
              <Button variant="primary" loading={busy} onClick={() => void finish()} subtitle="Creates the admin account and signs you in">
                Finish setup
              </Button>
            </div>
          </>
        )}

        {step === 'ready' && (
          <div class="stack lz-center">
            <Icon name="success" size={40} class="lz-tone-success" />
            <h2 id="setup-step-title" ref={heading} tabIndex={-1}>
              Labzilla is ready.
            </h2>
            {signedIn ? (
              <>
                <p>{readyLine(status)}</p>
                <Button variant="primary" size="lg" icon="ask" onClick={() => route('/ask')}>
                  Ask Labzilla
                </Button>
                <a href="/" class="small">
                  Go to Home instead
                </a>
              </>
            ) : (
              <>
                <p>Your admin account exists. Sign in with it to start.</p>
                <Button variant="primary" size="lg" onClick={() => route('/login')}>
                  Sign in
                </Button>
              </>
            )}
          </div>
        )}
      </section>
    </div>
  );
}

/** §84 says "Local AI is available" — but only when the status says so; otherwise say what it does report. */
function readyLine(s: SystemStatus | null): string {
  if (!s) return 'You are signed in.';
  if (s.local_ai === 'healthy' || s.local_ai === 'busy') return 'Local AI is available.';
  return `${s.local_ai_label || 'Local AI is not ready yet'}. Home shows what's happening.`;
}

function Brand() {
  return (
    <header class="lz-auth-brand">
      <img src="/logo/mark.png" alt="" width={64} height={64} />
      <h1>Set up Labzilla</h1>
    </header>
  );
}

function StepNav({ onBack, onNext, submit }: { onBack: () => void; onNext?: () => void; submit?: boolean }) {
  return (
    <div class="lz-auth-actions">
      <Button variant="ghost" class="grow-left" onClick={onBack}>
        Back
      </Button>
      <Button variant="primary" iconRight="arrow-right" type={submit ? 'submit' : 'button'} onClick={submit ? undefined : onNext}>
        Continue
      </Button>
    </div>
  );
}

function AddressRow({ label, url }: { label: string; url: string }) {
  return (
    <div class="lz-detected-row">
      <dt class="lz-detected-label">{label}</dt>
      <dd class="lz-detected-value mono">{url}</dd>
    </div>
  );
}

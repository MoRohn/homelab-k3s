// Trust this device (§78, §74, §71): whether this connection is trusted, what works on this device right
// now, and the owner's options to make it trusted. Everything shown comes from GET /api/access, live
// feature detection and what actually happened to the service worker in this tab — no hostnames or
// addresses are baked into the app. Security is never silently switched off: when something can't work
// over an untrusted connection, this page says so and why.
import { useObservable } from '@/api/observable';
import { useAccess } from './access';
import { capabilities, installAvailable, installed, promptInstall, swError, swState, type SwState } from '@/pwa';
import { usePageTitle } from '@/shell/usePageTitle';
import { Button } from '@/ui/Button';
import { Card } from '@/ui/Card';
import { FactList } from '@/ui/FactList';
import { HumanErrorCard } from '@/ui/HumanErrorCard';
import { Icon } from '@/ui/Icon';
import { Skeleton } from '@/ui/Skeleton';
import { StatusBadge } from '@/ui/StatusBadge';
import { TechDetails } from '@/ui/TechDetails';
import './connect.css';

const NEEDS_TLS = 'Needs a trusted HTTPS certificate (see below).';

const MDNS_LABEL: Record<'published' | 'not_published' | 'unknown', string> = {
  published: 'Announced on your network (.local names work on most devices)',
  not_published: 'Not announced: use the main address, or a DNS name from your router',
  unknown: "Couldn't check from here",
};

interface Feature {
  name: string;
  ok: boolean;
  note: string;
}

function offlineFeature(sw: SwState): Feature {
  switch (sw) {
    case 'registered':
      return { name: 'Opens instantly and offline', ok: true, note: 'The app shell is cached; live data always comes from Labzilla.' };
    case 'failed':
      return { name: 'Opens instantly and offline', ok: false, note: 'The browser refused offline support, usually because it doesn’t trust this certificate.' };
    case 'insecure':
      return { name: 'Opens instantly and offline', ok: false, note: NEEDS_TLS };
    case 'dev':
      return { name: 'Opens instantly and offline', ok: false, note: 'Not used on the development server.' };
    case 'unsupported':
      return { name: 'Opens instantly and offline', ok: false, note: "This browser doesn't support it." };
    default:
      return { name: 'Opens instantly and offline', ok: false, note: 'Checking…' };
  }
}

const UNTRUSTED_CERT = 'May be blocked: this browser doesn’t trust Labzilla’s certificate.';

export default function Trust() {
  usePageTitle('Trust this device');
  const access = useAccess();
  const sw = useObservable(swState);
  const swErr = useObservable(swError);
  const canInstall = useObservable(installAvailable);
  const isInstalled = useObservable(installed);
  const caps = capabilities();
  const a = access.data;
  const trusted = caps.secure && sw !== 'failed';

  const features: Feature[] = [
    { name: 'Ask, agents, jobs and status', ok: true, note: 'Work over any connection to Labzilla, including this one.' },
    { name: 'Pairing phones with a QR code', ok: true, note: 'Works over this connection.' },
    offlineFeature(sw),
    {
      name: 'Install as an app',
      ok: isInstalled || sw === 'registered',
      note: isInstalled
        ? 'Installed on this device.'
        : sw === 'registered'
          ? canInstall
            ? 'Available now.'
            : 'Use the browser’s menu: “Add to Home Screen” or “Install app”.'
          : sw === 'unsupported'
            ? "This browser can't install web apps."
            : NEEDS_TLS,
    },
    // HTTPS through a clicked-through certificate still counts as a secure context, but the browser may
    // block or forget microphone and notification permissions there (the service worker failing says so).
    {
      name: 'Voice input',
      ok: caps.voice && sw !== 'failed',
      note: !caps.voice ? (caps.secure ? "This browser doesn't offer speech recognition." : NEEDS_TLS) : sw === 'failed' ? UNTRUSTED_CERT : 'Available in Ask.',
    },
    {
      name: 'Background notifications',
      ok: caps.notifications && sw !== 'failed',
      note: !caps.notifications
        ? caps.secure
          ? "This browser doesn't support them."
          : NEEDS_TLS
        : sw === 'failed'
          ? UNTRUSTED_CERT
          : 'Turn them on from the bell, only if you want them.',
    },
    { name: 'Copy buttons', ok: caps.clipboard, note: caps.clipboard ? 'Available.' : NEEDS_TLS },
  ];

  return (
    <div class="page">
      <header class="page-header">
        <div>
          <h1>Trust this device</h1>
          <p>How this device connects to Labzilla, and what that allows.</p>
        </div>
      </header>

      <Card
        title="This connection"
        actions={<StatusBadge health={trusted ? 'healthy' : 'attention'} label={trusted ? 'Secure' : 'Not trusted yet'} />}
      >
        <div class="stack">
          <p>
            {trusted
              ? 'This browser trusts Labzilla’s certificate: the connection is encrypted and every feature can work.'
              : caps.secure
                ? 'The connection is encrypted, but this browser doesn’t fully trust Labzilla’s certificate, so it holds back some features.'
                : 'This connection isn’t using trusted HTTPS. Sign-in and Ask still work and the server still checks every request, but the browser disables some features.'}
          </p>
          {access.loading && <Skeleton lines={3} />}
          {access.error && <HumanErrorCard error={access.error} onRetry={() => void access.refresh()} compact />}
          {a && (
            <FactList
              columns={2}
              items={[
                { label: 'You are using', value: location.origin },
                { label: 'Main address', value: a.public_url || 'Not configured' },
                ...a.alt_urls.map((u) => ({ label: 'Also works at', value: u })),
                ...(a.lan_url ? [{ label: 'Fallback on your network', value: a.lan_url }] : []),
                { label: 'Local name (mDNS)', value: MDNS_LABEL[a.mdns] },
                { label: 'Server reports', value: a.secure ? 'HTTPS' : 'Plain HTTP' },
              ]}
            />
          )}
          {a?.trusted_hint && <p class="small muted">{a.trusted_hint}</p>}
        </div>
      </Card>

      <Card title="What works on this device">
        <ul class="lz-trust-list" role="list">
          {features.map((f) => (
            <li key={f.name}>
              <Icon name={f.ok ? 'success' : 'info'} size={18} label={f.ok ? 'Works' : 'Not available'} class={f.ok ? 'lz-tone-success' : 'lz-tone-inactive'} />
              <span>
                <strong>{f.name}</strong>
                <br />
                <span class="small muted">{f.note}</span>
              </span>
            </li>
          ))}
        </ul>
        {canInstall && !isInstalled && (
          <div class="row lz-trust-install">
            <Button variant="secondary" icon="download" onClick={() => void promptInstall()} subtitle="Optional: Labzilla works the same in the browser">
              Install app
            </Button>
          </div>
        )}
        {swErr && <TechDetails inline triggerLabel="Browser's reason" items={[{ label: 'Service worker', value: swErr }]} />}
      </Card>

      <Card title="Making it trusted" subtitle="Owner steps, done once on the Labzilla host">
        <div class="stack">
          <ol class="lz-trust-steps">
            <li>
              <strong>Local certificate authority (recommended).</strong> Labzilla’s HTTPS certificate is issued by your own small CA, and you
              install that CA once on each phone and computer. Everything works, including install, voice and notifications.
            </li>
            <li>
              <strong>Tailscale HTTPS.</strong> Expose the console on your tailnet and it gets a publicly trusted certificate automatically. Works
              on devices signed in to your tailnet.
            </li>
            <li>
              <strong>Keep the self-signed certificate.</strong> Sign-in, Ask, agents and pairing work after accepting the browser warning;
              installing, voice and background notifications stay off. The certificate may change when the cluster restarts.
            </li>
          </ol>
          <p class="small muted">
            Labzilla never turns security off to make a feature work. The step-by-step guide is in the Labzilla repository under{' '}
            <code>lif/docs/CONSOLE.md</code> (Deploy &amp; access).
          </p>
        </div>
      </Card>
    </div>
  );
}

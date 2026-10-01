// Network (§14, §63, §74, §78): how to reach this console, whether the connection is secure and trusted,
// and whether the friendly .local name is published. Pairing and certificate trust live on /connect and /trust.
import { useLocation } from 'preact-iso/router';
import { get } from '@/api/client';
import type { AccessInfo } from '@/api/contracts.gen';
import { useSystemStatus } from '@/api/status';
import { useResource } from '@/api/store';
import { Badge, Button, Card, FactList, HumanErrorCard, Skeleton, StatusDot } from '@/ui';

const MDNS: Record<AccessInfo['mdns'], { label: string; hint: string }> = {
  published: { label: 'Published', hint: 'Devices on your network can use the .local name.' },
  not_published: { label: 'Not published', hint: 'Use the main address, or ask the owner to publish the .local name (see Trust this device).' },
  unknown: { label: 'Not reported', hint: "Labzilla can't tell from here whether the .local name is published." },
};

function Url({ href }: { href: string }) {
  return (
    <a href={href} class="mono" rel="noopener">
      {href}
    </a>
  );
}

export function NetworkTab() {
  const access = useResource<AccessInfo>('access', () => get<AccessInfo>('/api/access'), { maxAgeMs: 60_000 });
  const status = useSystemStatus();
  const { route } = useLocation();
  const conn = status.data?.connection;
  // This browser's own view of the connection is the honest answer for "is *this* session secure?".
  const secureHere = typeof window !== 'undefined' && window.isSecureContext && location.protocol === 'https:';

  let body;
  if (access.loading) body = <Skeleton lines={5} />;
  else if (!access.data) body = access.error ? <HumanErrorCard error={access.error} onRetry={() => void access.refresh()} /> : null;
  else {
    const a = access.data;
    const mdns = MDNS[a.mdns];
    body = (
      <FactList
        items={[
          { label: 'Address', value: <Url href={a.public_url} /> },
          ...(a.alt_urls.length
            ? [{ label: 'Also reachable at', value: <span class="stack-sm">{a.alt_urls.map((u) => <Url key={u} href={u} />)}</span> }]
            : []),
          ...(a.lan_url ? [{ label: 'LAN address', value: <Url href={a.lan_url} />, hint: 'Fallback when the name does not resolve.' }] : []),
          {
            label: 'This connection',
            value: (
              <span class="row wrap">
                {secureHere ? (
                  <Badge tone="success" icon="lock" size="sm">
                    Secure (HTTPS)
                  </Badge>
                ) : (
                  <Badge tone="warning" icon="warning" size="sm">
                    Not secure
                  </Badge>
                )}
                {conn?.local && (
                  <span class="row small">
                    <StatusDot health="healthy" /> Local connection
                  </span>
                )}
              </span>
            ),
            hint: secureHere ? undefined : 'Sign-in and Ask still work; install, voice and notifications need a trusted HTTPS certificate.',
          },
          { label: 'Certificate', value: a.secure ? 'HTTPS is configured' : 'HTTPS is not configured', hint: a.trusted_hint || undefined },
          { label: '.local name', value: mdns.label, hint: mdns.hint },
        ]}
      />
    );
  }

  return (
    <Card
      title="Network access"
      actions={
        <div class="row wrap">
          <Button size="sm" variant="secondary" icon="qr" onClick={() => route('/connect')}>
            Connect a phone
          </Button>
          <Button size="sm" variant="ghost" icon="shield" onClick={() => route('/trust')}>
            Trust this device
          </Button>
        </div>
      }
    >
      {body}
    </Card>
  );
}

// Connect a phone (§14–§17): start pairing → QR + address + countdown → the phone claims and both screens
// show the same 6-digit code → Approve / Reject here. Paired devices are listed below with revocation.
// Live status arrives as SSE `pairing` events; if the live stream is down, the pairing is polled instead,
// so approval never silently stalls.
import { useEffect, useState } from 'preact/hooks';
import { del, get, post, toHumanError } from '@/api/client';
import type { Device, HumanError, Pairing } from '@/api/contracts.gen';
import { can, logout, useMe } from '@/api/session';
import { onReconnect, useConnectionState, useEvent } from '@/api/sse';
import { invalidate, useResource } from '@/api/store';
import { usePageTitle } from '@/shell/usePageTitle';
import { Badge } from '@/ui/Badge';
import { Button } from '@/ui/Button';
import { Card } from '@/ui/Card';
import { ConfirmDialog } from '@/ui/ConfirmDialog';
import { EmptyState } from '@/ui/EmptyState';
import { HumanErrorCard } from '@/ui/HumanErrorCard';
import { Icon } from '@/ui/Icon';
import { Skeleton } from '@/ui/Skeleton';
import { toast } from '@/ui/Toast';
import { ago } from '@/ui/format';
import { mobileUrl, useAccess } from './access';
import { ContinueOnMobile } from './ContinueOnMobile';
import { QrCode } from './QrCode';
import './connect.css';

const DEVICES_KEY = 'devices';
const POLL_MS = 3000;

function useSecondsLeft(expiresAt: number | undefined): number | null {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!expiresAt) return;
    setNow(Date.now() / 1000);
    const t = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(t);
  }, [expiresAt]);
  return expiresAt ? Math.max(0, Math.round(expiresAt - now)) : null;
}

const mmss = (s: number) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;

export default function Connect() {
  usePageTitle('Connect a phone');
  const me = useMe();
  const manage = can(me.data, 'devices.manage');
  const access = useAccess();

  return (
    <div class="page">
      <header class="page-header">
        <div>
          <h1>Connect a phone</h1>
          <p>Use Labzilla from a phone or tablet on your home network. No passwords to type, no addresses to remember.</p>
        </div>
      </header>

      {manage ? (
        <PairingCard />
      ) : (
        <Card title="Pair a new device">
          <p class="muted">
            Pairing a new device needs an admin session. Open Labzilla on your desktop, go to <strong>Connect a phone</strong>, and scan the code
            with the new device.
          </p>
        </Card>
      )}

      {access.data && !access.data.secure && (
        <p class="lz-connect-note small">
          <Icon name="shield" size={18} />
          <span>
            Pairing and Ask work over this connection. Installing the app, voice input and background notifications need a trusted HTTPS
            certificate: <a href="/trust">Trust this device</a>.
          </span>
        </p>
      )}

      {manage && <DeviceList />}

      <Card title="Phone already paired?" subtitle="Scan to open Labzilla on it.">
        <div class="row wrap">
          <p class="grow small muted mono lz-connect-url">{mobileUrl('/', access.data)}</p>
          <ContinueOnMobile path="/" label="Show QR code" variant="secondary" />
        </div>
      </Card>
    </div>
  );
}

function PairingCard() {
  const [pairing, setPairing] = useState<Pairing | null>(null);
  const [busy, setBusy] = useState<'start' | 'approve' | 'reject' | null>(null);
  const [error, setError] = useState<HumanError | null>(null);
  const live = useConnectionState();
  const left = useSecondsLeft(pairing && (pairing.status === 'waiting' || pairing.status === 'claimed') ? pairing.expires_at : undefined);

  // SSE payloads may omit the token/url (they go to every admin tab); keep the ones from /pair/start.
  const merge = (p: Pairing) => setPairing((prev) => (prev && prev.id === p.id ? { ...prev, ...p, url: p.url || prev.url, token: p.token ?? prev.token } : prev));

  useEvent('pairing', merge);

  const pending = pairing && (pairing.status === 'waiting' || pairing.status === 'claimed') ? pairing.id : null;
  const refetch = async (id: string) => {
    try {
      merge(await get<Pairing>(`/api/pair/${encodeURIComponent(id)}`));
    } catch {
      /* transient; the next tick or event retries */
    }
  };
  // Catch up on whatever was missed while the stream was down, and poll only while it is down.
  useEffect(() => (pending ? onReconnect(() => void refetch(pending)) : undefined), [pending]);
  useEffect(() => {
    if (!pending || live === 'open') return;
    const t = setInterval(() => void refetch(pending), POLL_MS);
    return () => clearInterval(t);
  }, [pending, live]);

  useEffect(() => {
    if (left === 0 && pairing && (pairing.status === 'waiting' || pairing.status === 'claimed')) setPairing({ ...pairing, status: 'expired' });
  }, [left]);

  const start = async () => {
    setBusy('start');
    setError(null);
    try {
      setPairing(await post<Pairing>('/api/pair/start', {}));
    } catch (e) {
      setError(toHumanError(e));
    } finally {
      setBusy(null);
    }
  };

  const answer = async (verb: 'approve' | 'reject') => {
    if (!pairing) return;
    setBusy(verb);
    setError(null);
    try {
      const res = await post<Pairing | undefined>(`/api/pair/${encodeURIComponent(pairing.id)}/${verb}`, {});
      // Rejecting before any phone claimed it is just "cancel": back to the start, nothing to report.
      if (verb === 'reject' && pairing.status === 'waiting') return setPairing(null);
      setPairing({ ...pairing, ...(res ?? {}), status: verb === 'approve' ? 'approved' : 'rejected' });
      if (verb === 'approve') {
        toast({ title: `${pairing.device_name ?? 'The device'} is paired`, tone: 'success' });
        invalidate(DEVICES_KEY);
      }
    } catch (e) {
      setError(toHumanError(e));
      void refetch(pairing.id);
    } finally {
      setBusy(null);
    }
  };

  const status = pairing?.status;
  return (
    <Card title="Pair a new device" subtitle={!pairing ? 'Scan a QR code with the phone, then approve it here.' : undefined}>
      <div class="lz-pair" aria-live="polite">
        {error && <HumanErrorCard error={error} compact />}

        {(!pairing || status === 'approved' || status === 'rejected' || status === 'expired') && (
          <div class="stack">
            {status === 'approved' && (
              <p class="lz-pair-result">
                <Icon name="success" size={20} class="lz-tone-success" /> <strong>{pairing?.device_name ?? 'The device'}</strong> is paired. It's signed in and
                listed below.
              </p>
            )}
            {status === 'rejected' && (
              <p class="lz-pair-result">
                <Icon name="block" size={20} class="lz-tone-warning" /> Rejected. {pairing?.device_name ? `“${pairing.device_name}”` : 'That device'} wasn't paired.
              </p>
            )}
            {status === 'expired' && (
              <p class="lz-pair-result">
                <Icon name="clock" size={20} class="lz-tone-inactive" /> The code expired before a phone was approved. Nothing was paired.
              </p>
            )}
            <div>
              <Button
                variant="primary"
                icon="qr"
                loading={busy === 'start'}
                onClick={() => void start()}
                subtitle="Shows a QR code for a couple of minutes. Nothing gets access until you approve it here."
              >
                {pairing ? 'Pair another device' : 'Start pairing'}
              </Button>
            </div>
          </div>
        )}

        {status === 'waiting' && pairing && (
          <div class="lz-pair-waiting">
            <QrCode text={pairing.url} label="QR code to pair a phone with Labzilla" />
            <div class="stack-sm">
              <h3>Scan with your phone's camera</h3>
              <p class="small muted">Or open this address on the phone:</p>
              <p class="mono small lz-connect-url">{pairing.url}</p>
              <p class="small muted num">
                <Icon name="clock" size={16} /> Waiting for a phone{left !== null ? ` · code expires in ${mmss(left)}` : '…'}
              </p>
              <p class="xsmall faint">The link works once. Anyone who scans it still needs your approval here.</p>
              <div>
                <Button variant="ghost" size="sm" loading={busy === 'reject'} onClick={() => void answer('reject')}>
                  Cancel pairing
                </Button>
              </div>
            </div>
          </div>
        )}

        {status === 'claimed' && pairing && (
          <div class="stack lz-pair-claimed">
            <h3>
              <Icon name="phone" size={20} /> “{pairing.device_name ?? 'A device'}” wants to connect
            </h3>
            <p>Approve only if the phone shows the same code:</p>
            <p class="lz-pair-code num" aria-label={`Verification code ${(pairing.code ?? '').split('').join(' ')}`}>
              {pairing.code ?? '——————'}
            </p>
            <p class="small muted">
              Once approved, this device can ask Labzilla, approve agent requests, pause and resume jobs and check status. It can't promote or
              delete models, change settings or pair other devices. You can revoke it any time.
            </p>
            <div class="row wrap">
              <Button variant="primary" icon="check" loading={busy === 'approve'} disabled={!!busy} onClick={() => void answer('approve')}>
                Approve
              </Button>
              <Button variant="secondary" loading={busy === 'reject'} disabled={!!busy} onClick={() => void answer('reject')}>
                Reject
              </Button>
              {left !== null && <span class="small muted num">Expires in {mmss(left)}</span>}
            </div>
          </div>
        )}
      </div>
    </Card>
  );
}

function DeviceList() {
  // Refetches on `pairing` events too: an approval in another admin tab adds a device here.
  const devices = useResource<Device[]>(DEVICES_KEY, () => get<Device[]>('/api/devices'), { maxAgeMs: 60_000, refreshOn: ['pairing'] });
  const [revoking, setRevoking] = useState<Device | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<HumanError | null>(null);

  const revoke = async () => {
    if (!revoking) return;
    setBusy(true);
    setError(null);
    try {
      await del(`/api/devices/${encodeURIComponent(revoking.id)}`);
      const d = revoking;
      setRevoking(null);
      if (d.current) return void logout();
      devices.mutate((list) => (list ?? []).filter((x) => x.id !== d.id));
      toast({ title: `${d.name} was signed out`, body: 'It needs to be paired again to use Labzilla.', tone: 'success' });
      void devices.refresh();
    } catch (e) {
      setError(toHumanError(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card title="Paired devices" padded={!devices.data?.length}>
      {devices.loading && <Skeleton lines={2} />}
      {devices.error && <HumanErrorCard error={devices.error} onRetry={() => void devices.refresh()} compact />}
      {devices.data && devices.data.length === 0 && (
        <EmptyState icon="phone" title="No paired devices yet" body="Pair a phone above to use Labzilla from anywhere in the house." compact />
      )}
      {devices.data && devices.data.length > 0 && (
        <ul role="list" class="lz-list divided" aria-label="Paired devices">
          {devices.data.map((d) => (
            <li key={d.id} class="lz-li-row">
              <Icon name="phone" />
              <span class="lz-li-main">
                <span class="lz-li-title">
                  {d.name} {d.current && <Badge size="sm" tone="brand">This device</Badge>}
                </span>
                <span class="lz-li-subtitle">
                  {[d.user_agent_summary, `paired ${ago(d.paired_at)}`, d.last_seen ? `last seen ${ago(d.last_seen)}` : null].filter(Boolean).join(' · ')}
                </span>
              </span>
              <Button variant="ghost" size="sm" onClick={() => setRevoking(d)} aria-label={`Revoke ${d.name}`}>
                Revoke
              </Button>
            </li>
          ))}
        </ul>
      )}
      {revoking && (
        <ConfirmDialog
          open
          tone="danger"
          confirmLabel="Revoke"
          busy={busy}
          error={error}
          preview={{
            title: `Revoke “${revoking.name}”?`,
            changes: [revoking.current ? 'This device will be signed out immediately.' : 'This phone will be signed out immediately.'],
            interrupts: ['Labzilla pages open on it stop working until it is paired again. Its conversations stay in Ask.'],
            rollback: 'To use it again, pair it again with a new QR code.',
            confirm: 'simple',
          }}
          onConfirm={() => void revoke()}
          onCancel={() => {
            setRevoking(null);
            setError(null);
          }}
        />
      )}
    </Card>
  );
}

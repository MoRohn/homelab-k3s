// "Continue on another device" (§68, §69): the thread lives on the server, so its URL is the hand-off —
// scan the QR on a paired phone, or open History on the desktop. No copy/paste of the conversation.
// The QR component belongs to Connect and pulls in qrcode-generator, so both load only when this opens (§93).
import type { FunctionComponent } from 'preact';
import { useEffect, useState } from 'preact/hooks';
import { useSystemStatus } from '@/api/status';
import type { QrCodeProps } from '@/pages/connect/QrCode';
import { useCopy } from '@/ui/clipboard';
import { Button, Dialog, Skeleton } from '@/ui';

export interface ContinueElsewhereProps {
  open: boolean;
  onClose: () => void;
  threadId: string;
}

/** Prefer the address the server advertises for phones (labzilla.local / public URL) over whatever this tab used (maybe localhost). */
function baseUrl(advertised: string | undefined): string {
  try {
    // A bare "https://" passed the old prefix test and produced a broken link; it needs a host too.
    const u = advertised ? new URL(advertised) : null;
    if (u && (u.protocol === 'http:' || u.protocol === 'https:') && u.hostname) return advertised!.replace(/\/+$/, '');
  } catch {
    /* not a URL: fall back to this tab's origin */
  }
  return location.origin;
}

export function ContinueElsewhere({ open, onClose, threadId }: ContinueElsewhereProps) {
  const status = useSystemStatus();
  const [Qr, setQr] = useState<FunctionComponent<QrCodeProps> | null>(null);
  const [failed, setFailed] = useState(false);
  const [copied, copy] = useCopy();
  const url = `${baseUrl(status.data?.connection.url)}/ask/${encodeURIComponent(threadId)}`;

  useEffect(() => {
    if (!open || Qr) return;
    import('@/pages/connect/QrCode').then(
      (m) => {
        setFailed(false); // an earlier failed load must not linger next to the QR that loaded on retry
        setQr(() => m.QrCode);
      },
      () => setFailed(true),
    );
  }, [open]);

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="Continue on another device"
      description="Scan with a paired phone to open this conversation there. On a desktop, it’s in History."
      size="sm"
      footer={
        <Button variant="primary" onClick={onClose}>
          Done
        </Button>
      }
    >
      <div class="ask-continue">
        {Qr ? <Qr text={url} size={200} label="QR code that opens this conversation" /> : failed ? null : <Skeleton width="200px" height="200px" radius="12px" />}
        {failed && <p class="small muted">The QR code couldn’t load. Open the address below on the other device.</p>}
        <p class="mono small ask-continue-url">{url}</p>
        <Button size="sm" variant="secondary" icon={copied === 'ok' ? 'check' : 'link'} onClick={() => copy(url)}>
          {copied === 'ok' ? 'Link copied' : 'Copy link'}
        </Button>
        <p class="xsmall muted">The other device must be paired with Labzilla. Answers keep streaming there even if you close this tab.</p>
      </div>
    </Dialog>
  );
}

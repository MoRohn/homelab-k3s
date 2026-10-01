// "Continue on mobile" (§69): a QR code for the current page (or a given path) on the console's public
// address, so work started on the desktop opens on a phone without copying anything. The session state
// lives on the server (§70), so the phone sees the same thread once it is signed in or paired.
// Kept tiny for the Ask bundle: the QR encoder loads only when the dialog opens.
import { useState } from 'preact/hooks';
import { useLocation } from 'preact-iso/router';
import { Button, type ButtonProps } from '@/ui/Button';
import { Dialog } from '@/ui/Dialog';
import { Skeleton } from '@/ui/Skeleton';
import { mobileUrl, useAccess } from './access';
import { QrCode } from './QrCode';
import './connect.css';

export interface ContinueOnMobileProps {
  /** App path to open on the phone (default: the current page, e.g. "/ask/t_123"). */
  path?: string;
  label?: string;
  variant?: ButtonProps['variant'];
  size?: ButtonProps['size'];
}

export function ContinueOnMobile({ path, label = 'Continue on mobile', variant = 'ghost', size = 'sm' }: ContinueOnMobileProps) {
  const { url } = useLocation();
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button variant={variant} size={size} icon="phone" onClick={() => setOpen(true)} aria-haspopup="dialog">
        {label}
      </Button>
      {open && <ContinueDialog path={path ?? url} onClose={() => setOpen(false)} />}
    </>
  );
}

function ContinueDialog({ path, onClose }: { path: string; onClose: () => void }) {
  const access = useAccess();
  const target = mobileUrl(path, access.data);
  return (
    <Dialog open onClose={onClose} title="Continue on your phone" description="Scan with the phone's camera to open this same page there." size="sm">
      <div class="stack lz-continue">
        {access.loading ? <Skeleton width="200px" height="200px" radius="12px" /> : <QrCode text={target} size={200} label={`QR code for ${target}`} />}
        <p class="small mono lz-continue-url">{target}</p>
        <p class="xsmall muted">The phone needs to be on your home network and paired (Connect a phone) or signed in.</p>
      </div>
    </Dialog>
  );
}

// QR code as an inline SVG (§15, §69). qrcode-generator is imported lazily, so only screens that actually
// show a code pay for it (§93). The SVG is built as one <path> VNode from isDark(), never innerHTML.
// The code is always dark-on-white with a 4-module quiet zone, even in the dark theme: phone cameras often
// fail to read inverted or borderless codes.
import { useEffect, useState } from 'preact/hooks';
import { Skeleton } from '@/ui/Skeleton';

type Matrix = { size: number; d: string };

type QrFactory = Awaited<ReturnType<typeof loadQr>>;
const loadQr = () => import('qrcode-generator').then((m) => m.default);
let loader: Promise<QrFactory> | null = null;

/** Encode `text` (byte mode, error correction M) into an SVG path over a size×size module grid. */
export async function qrMatrix(text: string): Promise<Matrix> {
  // A failed chunk load is forgotten, so the next render retries instead of failing forever.
  loader ??= loadQr().catch((e: unknown) => {
    loader = null;
    throw e;
  });
  const qr = (await loader)(0, 'M');
  qr.addData(text, 'Byte');
  qr.make();
  const n = qr.getModuleCount();
  const quiet = 4;
  let d = '';
  for (let r = 0; r < n; r++) {
    // Merge horizontal runs of dark modules into one rectangle each: a much shorter path than one per module.
    for (let c = 0; c < n; c++) {
      if (!qr.isDark(r, c)) continue;
      let w = 1;
      while (c + w < n && qr.isDark(r, c + w)) w++;
      d += `M${c + quiet} ${r + quiet}h${w}v1h-${w}z`;
      c += w - 1;
    }
  }
  return { size: n + quiet * 2, d };
}

export interface QrCodeProps {
  text: string;
  /** Rendered size in CSS px (default 220). */
  size?: number;
  /** Accessible name, e.g. "QR code to pair a phone". The URL is always printed next to it as text too. */
  label: string;
}

export function QrCode({ text, size = 220, label }: QrCodeProps) {
  const [m, setM] = useState<Matrix | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let live = true;
    setM(null);
    setFailed(false);
    qrMatrix(text).then(
      (v) => live && setM(v),
      () => live && setFailed(true),
    );
    return () => {
      live = false;
    };
  }, [text]);

  if (failed) return <p class="small muted">The QR code couldn't be drawn here. Type the address below on your phone instead.</p>;
  if (!m) return <Skeleton width={`${size}px`} height={`${size}px`} radius="12px" />;
  return (
    <svg class="lz-qr" role="img" aria-label={label} width={size} height={size} viewBox={`0 0 ${m.size} ${m.size}`} shape-rendering="crispEdges">
      <rect width={m.size} height={m.size} fill="#ffffff" />
      <path d={m.d} fill="#000000" />
    </svg>
  );
}

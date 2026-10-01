// File intake for Ask (§20). Everything happens in this browser: text-like files are read here and
// sent to Labzilla as JSON (≤ 256 KB each, brief §1 — no multipart). Images are downscaled here with a
// canvas (longest side ≤ 1024 px, JPEG q0.85, so a 12 MP phone photo becomes ~150 KB) and sent once to the
// local vision model; Labzilla never stores them. PDFs and office documents are listed but NOT processed:
// there is no PDF extraction yet, and saying so beats pretending the model saw them.
import type { AttachmentIn, AttachmentKind } from '@/api/contracts.gen';

export const MAX_TEXT_BYTES = 256 * 1024;
export const MAX_FILES = 8;
export const MAX_IMAGES = 4;
const IMAGE_MAX_PX = 1024;
const IMAGE_QUALITY = 0.85;
/** Above this, ask the decoder for a smaller bitmap first instead of holding a 48 MP one in memory. */
const BIG_IMAGE_BYTES = 6 * 1024 * 1024;

export interface Picked {
  id: string;
  name: string;
  kind: AttachmentKind;
  size: number;
  /** Extracted text; absent when the file is not processed. */
  text?: string;
  included: boolean;
  /** Why it isn't included ("Not processed: PDF text extraction isn't available"). */
  note?: string;
  /** Images: the downscaled JPEG as a data URL (also the thumbnail), and its size in pixels. */
  image?: string;
  width?: number;
  height?: number;
}

const EXT: Record<string, AttachmentKind> = {
  txt: 'text', text: 'text', rst: 'text', ini: 'text', cfg: 'text', conf: 'text', env: 'text', toml: 'text',
  md: 'markdown', markdown: 'markdown', mdx: 'markdown',
  json: 'json', jsonl: 'json', ndjson: 'json', geojson: 'json',
  csv: 'csv', tsv: 'csv',
  log: 'log', out: 'log', err: 'log',
  pdf: 'pdf',
  png: 'image', jpg: 'image', jpeg: 'image', gif: 'image', webp: 'image', heic: 'image', heif: 'image', bmp: 'image', svg: 'image', avif: 'image',
  doc: 'document', docx: 'document', odt: 'document', rtf: 'document', pages: 'document', ppt: 'document', pptx: 'document', xls: 'document', xlsx: 'document', ods: 'document', key: 'document', epub: 'document',
};

const CODE = new Set(
  'py js mjs cjs ts tsx jsx go rs java kt kts c h cc cpp hpp cs rb php swift scala sh bash zsh fish ps1 sql yaml yml xml html htm css scss less vue svelte lua r pl dockerfile makefile gradle tf hcl proto graphql gql nix ex exs erl hs ml clj dart zig'.split(' '),
);

export function kindOf(file: File): AttachmentKind {
  const name = file.name.toLowerCase();
  const ext = name.includes('.') ? name.slice(name.lastIndexOf('.') + 1) : name;
  if (EXT[ext]) return EXT[ext];
  if (CODE.has(ext)) return 'code';
  const t = file.type;
  if (t.startsWith('image/')) return 'image';
  if (t === 'application/pdf') return 'pdf';
  if (t.startsWith('text/') || /json|xml|yaml|javascript|x-sh/.test(t)) return 'text';
  return 'other';
}

const NOT_PROCESSED: Partial<Record<AttachmentKind, string>> = {
  pdf: 'Not processed: PDF text extraction isn’t available locally yet. Paste the text instead.',
  document: 'Not processed: document conversion isn’t available yet. Paste the text instead.',
};

let seq = 0;

async function decode(file: File): Promise<ImageBitmap | HTMLImageElement> {
  if (typeof createImageBitmap === 'function') {
    try {
      // Phone photos carry their rotation in EXIF: apply it, or portraits arrive sideways.
      return await createImageBitmap(file, file.size > BIG_IMAGE_BYTES
        ? { imageOrientation: 'from-image', resizeWidth: IMAGE_MAX_PX * 2, resizeQuality: 'high' }
        : { imageOrientation: 'from-image' });
    } catch {
      /* fall through: older Safari rejects the options */
    }
  }
  const url = URL.createObjectURL(file);
  try {
    const img = new Image();
    img.src = url;
    await img.decode();
    return img;
  } finally {
    URL.revokeObjectURL(url);
  }
}

function asDataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result));
    r.onerror = () => reject(r.error);
    r.readAsDataURL(blob);
  });
}

/** Downscale to ≤ 1024 px on the longest side and re-encode as JPEG (white behind transparency). */
export async function shrinkImage(file: File): Promise<{ image: string; width: number; height: number; bytes: number }> {
  const src = await decode(file);
  const sw = 'naturalWidth' in src ? src.naturalWidth : src.width;
  const sh = 'naturalHeight' in src ? src.naturalHeight : src.height;
  if (!sw || !sh) throw new Error('empty image');
  const scale = Math.min(1, IMAGE_MAX_PX / Math.max(sw, sh));
  const width = Math.max(1, Math.round(sw * scale));
  const height = Math.max(1, Math.round(sh * scale));
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext('2d');
  if (!ctx) throw new Error('no canvas');
  ctx.fillStyle = '#fff';
  ctx.fillRect(0, 0, width, height);
  ctx.imageSmoothingQuality = 'high';
  ctx.drawImage(src, 0, 0, width, height);
  if ('close' in src) src.close();
  const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, 'image/jpeg', IMAGE_QUALITY));
  canvas.width = canvas.height = 0;            // let the pixels go now, not at the next GC
  if (!blob) throw new Error('encode failed');
  return { image: await asDataUrl(blob), width, height, bytes: blob.size };
}

/** Read one file locally. Never throws; the result says whether its text (or image) will be sent. */
export async function intake(file: File): Promise<Picked> {
  const base = { id: `f${++seq}`, name: file.name || 'pasted image', kind: kindOf(file), size: file.size };
  if (base.kind === 'image') {
    try {
      const img = await shrinkImage(file);
      return { ...base, size: img.bytes, image: img.image, width: img.width, height: img.height, included: true };
    } catch {
      const heic = /\.(heic|heif)$/i.test(base.name) || /hei[cf]/.test(file.type);
      return { ...base, included: false, note: heic ? 'This browser can’t read HEIC photos. Share it as JPEG, or take a screenshot.' : 'This browser couldn’t read the image.' };
    }
  }
  const skip = NOT_PROCESSED[base.kind];
  if (skip) return { ...base, included: false, note: skip };
  // Check the size before reading: never load a large log just to reject it.
  if (file.size > MAX_TEXT_BYTES) return { ...base, included: false, note: `Not included: larger than ${MAX_TEXT_BYTES / 1024} KB. Paste the relevant part instead.` };
  try {
    const text = await file.text();
    if (text.includes('\u0000')) return { ...base, kind: 'other', included: false, note: 'Not included: this looks like a binary file.' };
    return { ...base, kind: base.kind === 'other' ? 'text' : base.kind, text, included: true };
  } catch {
    return { ...base, included: false, note: 'Not included: this browser couldn’t read the file.' };
  }
}

export function toAttachmentIn(p: Picked): AttachmentIn {
  if (p.kind === 'image') return { name: p.name, kind: 'image', size: p.size, text: null, image: p.included ? p.image ?? null : null, width: p.width ?? null, height: p.height ?? null };
  return { name: p.name, kind: p.kind, size: p.size, text: p.included ? p.text ?? null : null };
}

export const isImage = (p: Picked) => p.kind === 'image' && p.included;

export function fileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

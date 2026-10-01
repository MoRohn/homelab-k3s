// File intake for Ask (§20). Everything happens in this browser: text-like files are read here and
// sent to Labzilla as JSON (≤ 256 KB each, brief §1 — no multipart). Images, PDFs and office documents
// are listed but NOT processed and never uploaded: no local vision model or PDF extraction exists yet,
// and saying so beats pretending the model saw them.
import type { AttachmentIn, AttachmentKind } from '@/api/contracts.gen';

export const MAX_TEXT_BYTES = 256 * 1024;
export const MAX_FILES = 8;

export interface Picked {
  id: string;
  name: string;
  kind: AttachmentKind;
  size: number;
  /** Extracted text; absent when the file is not processed. */
  text?: string;
  included: boolean;
  /** Why it isn't included ("Not processed: no local vision model yet"). */
  note?: string;
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
  image: 'Not processed: no local vision model yet.',
  pdf: 'Not processed: PDF text extraction isn’t available locally yet. Paste the text instead.',
  document: 'Not processed: document conversion isn’t available yet. Paste the text instead.',
};

let seq = 0;

/** Read one file locally. Never throws; the result says whether its text will be sent. */
export async function intake(file: File): Promise<Picked> {
  const base = { id: `f${++seq}`, name: file.name || 'pasted file', kind: kindOf(file), size: file.size };
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
  return { name: p.name, kind: p.kind, size: p.size, text: p.included ? p.text ?? null : null };
}

export function fileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

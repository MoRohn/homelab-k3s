// Inline SVG icon set (no icon font, no external requests, ~10 KB raw before gzip). 24×24 grid,
// 1.75 stroke, currentColor, so icons take the tone of the text they sit in.
// Decorative by default (aria-hidden); pass `label` when the icon alone carries meaning.
// Some names are aliases (spark = sparkle, queue = jobs, …) so callers can use the word they think of.

const P: Record<IconName, string> = {
  // navigation
  home: 'M3 10.5 12 3l9 7.5M5 9v11h5v-6h4v6h5V9',
  ask: 'M4 5h16v11H9l-5 4V5zM8 9h8M8 12h5',
  agents: 'M12 3v3M7 7h10a3 3 0 0 1 3 3v6a3 3 0 0 1-3 3H7a3 3 0 0 1-3-3v-6a3 3 0 0 1 3-3zM9 12h.01M15 12h.01M9.5 16h5',
  models: 'M12 3 3 7.5l9 4.5 9-4.5L12 3zM3 12l9 4.5 9-4.5M3 16.5 12 21l9-4.5',
  jobs: 'M4 6h16M4 12h16M4 18h10M18 16v4M16 18h4',
  knowledge: 'M4 4.5A2.5 2.5 0 0 1 6.5 2H20v17H6.5A2.5 2.5 0 0 0 4 21.5v-17zM4 19.5A2.5 2.5 0 0 1 6.5 17H20',
  system: 'M4 4h16v6H4zM4 14h16v6H4zM8 7h.01M8 17h.01',
  more: 'M5 12h.01M12 12h.01M19 12h.01',
  menu: 'M4 6h16M4 12h16M4 18h16',
  search: 'M11 4a7 7 0 1 1 0 14 7 7 0 0 1 0-14zM20 20l-4-4',
  command: 'M9 6a3 3 0 1 0-3 3h12a3 3 0 1 0-3-3v12a3 3 0 1 0 3-3H6a3 3 0 1 0 3 3V6z',
  'chevron-right': 'M9 6l6 6-6 6',
  'chevron-left': 'M15 6l-6 6 6 6',
  'chevron-down': 'M6 9l6 6 6-6',
  'chevron-up': 'M6 15l6-6 6 6',
  'arrow-right': 'M5 12h14M13 6l6 6-6 6',
  'arrow-down': 'M12 5v14M6 13l6 6 6-6',
  external: 'M14 4h6v6M20 4l-9 9M18 14v6H4V6h6',
  x: 'M6 6l12 12M18 6 6 18',
  check: 'M5 12.5 10 17 19 7',
  plus: 'M12 5v14M5 12h14',
  // actions
  send: 'M4 12 20 4l-6 16-3-7-7-1z',
  stop: 'M7 7h10v10H7z',
  mic: 'M12 3a3 3 0 0 1 3 3v6a3 3 0 0 1-6 0V6a3 3 0 0 1 3-3zM5 11a7 7 0 0 0 14 0M12 18v3',
  attach: 'M20 11.5 12 19.5a5 5 0 0 1-7-7l8.5-8.5a3.5 3.5 0 0 1 5 5L10 17.5a2 2 0 0 1-3-3L14.5 7',
  copy: 'M9 9h11v11H9zM5 15H4V4h11v1',
  refresh: 'M20 11a8 8 0 0 0-14.5-4.5L4 8M4 4v4h4M4 13a8 8 0 0 0 14.5 4.5L20 16M20 20v-4h-4',
  pause: 'M8 5v14M16 5v14',
  play: 'M7 4.5v15L19 12 7 4.5z',
  download: 'M12 4v11M7 10l5 5 5-5M5 20h14',
  upload: 'M12 20V9M7 14l5-5 5 5M5 4h14',
  trash: 'M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13',
  pin: 'M9 4h6l-1 5 3 3v2H7v-2l3-3-1-5zM12 14v6',
  block: 'M12 3a9 9 0 1 1 0 18 9 9 0 0 1 0-18zM5.6 5.6l12.8 12.8',
  promote: 'M12 19V5M6 11l6-6 6 6',
  rollback: 'M9 14 4 9l5-5M4 9h10a6 6 0 0 1 0 12h-3',
  benchmark: 'M4 20V10M10 20V4M16 20v-7M22 20H2',
  test: 'M9 3h6M10 3v6L4.5 18.5A1.7 1.7 0 0 0 6 21h12a1.7 1.7 0 0 0 1.5-2.5L14 9V3M7 15h10',
  edit: 'M4 20h4L19 9l-4-4L4 16v4zM13 7l4 4',
  save: 'M5 4h11l3 3v13H5zM8 4v5h7M8 20v-6h8v6',
  filter: 'M4 5h16l-6 8v6l-4-2v-4L4 5z',
  sliders: 'M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0M14 4v4M8 10v4M16 16v4',
  eye: 'M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12zM12 9a3 3 0 1 1 0 6 3 3 0 0 1 0-6z',
  logout: 'M15 4h4v16h-4M10 8l-4 4 4 4M6 12h11',
  // status
  info: 'M12 3a9 9 0 1 1 0 18 9 9 0 0 1 0-18zM12 11v5M12 8h.01',
  success: 'M12 3a9 9 0 1 1 0 18 9 9 0 0 1 0-18zM8 12.5l3 3 5-6',
  warning: 'M12 3 2 20h20L12 3zM12 10v4M12 17h.01',
  error: 'M12 3a9 9 0 1 1 0 18 9 9 0 0 1 0-18zM9 9l6 6M15 9l-6 6',
  paused: 'M12 3a9 9 0 1 1 0 18 9 9 0 0 1 0-18zM10 9v6M14 9v6',
  offline: 'M12 3a9 9 0 1 1 0 18 9 9 0 0 1 0-18zM8 12h8',
  busy: 'M12 3a9 9 0 1 1 0 18 9 9 0 0 1 0-18zM12 7v5l3 2',
  unknown: 'M12 3a9 9 0 1 1 0 18 9 9 0 0 1 0-18zM9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .8-1 1.5v.2M12 17h.01',
  bell: 'M6 9a6 6 0 1 1 12 0c0 6 2 7 2 7H4s2-1 2-7zM10 20a2 2 0 0 0 4 0',
  clock: 'M12 3a9 9 0 1 1 0 18 9 9 0 0 1 0-18zM12 7v5l3 2',
  // things
  gpu: 'M3 7h18v10H3zM7 10h4v4H7zM14 10h3M14 14h3M6 17v3M18 17v3',
  memory: 'M4 7h16v10H4zM8 7v10M12 7v10M16 7v10M4 20h16',
  cpu: 'M7 7h10v10H7zM10 10h4v4h-4zM9 3v4M15 3v4M9 17v4M15 17v4M3 9h4M3 15h4M17 9h4M17 15h4',
  bolt: 'M13 3 5 14h6l-1 7 8-11h-6l1-7z',
  server: 'M4 4h16v7H4zM4 13h16v7H4zM8 7.5h.01M8 16.5h.01',
  database: 'M12 3c4.4 0 8 1.3 8 3s-3.6 3-8 3-8-1.3-8-3 3.6-3 8-3zM4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3',
  network: 'M12 3v6M5 21v-4h14v4M12 9a3 3 0 1 1 0 6M5 17v-2h14v2M12 15v2',
  shield: 'M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6l-8-3z',
  lock: 'M6 11h12v10H6zM8 11V7a4 4 0 0 1 8 0v4',
  key: 'M14 4a6 6 0 1 1-4.9 9.5L4 18.5V21h3v-2h2v-2h2l1.6-1.6A6 6 0 0 1 14 4zM16 8h.01',
  phone: 'M8 2h8a1 1 0 0 1 1 1v18a1 1 0 0 1-1 1H8a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1zM11 18h2',
  desktop: 'M3 4h18v12H3zM8 20h8M12 16v4',
  qr: 'M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h2v2h-2zM18 14h2M14 18h2v2M18 18h2v2h-2',
  link: 'M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7L11.5 6.8M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1.5-1.5',
  wifi: 'M2 9a15 15 0 0 1 20 0M5 12.5a10 10 0 0 1 14 0M8.5 16a5 5 0 0 1 7 0M12 19.5h.01',
  'wifi-off': 'M2 9a15 15 0 0 1 6-3.5M22 9a15 15 0 0 0-9-4M5 12.5a10 10 0 0 1 4-2.3M19 12.5a10 10 0 0 0-2-1.5M8.5 16a5 5 0 0 1 7 0M12 19.5h.01M3 3l18 18',
  user: 'M12 4a4 4 0 1 1 0 8 4 4 0 0 1 0-8zM4 21a8 8 0 0 1 16 0',
  settings: 'M12 9a3 3 0 1 1 0 6 3 3 0 0 1 0-6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z',
  sun: 'M12 8a4 4 0 1 1 0 8 4 4 0 0 1 0-8zM12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4',
  moon: 'M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z',
  decision: 'M12 3v4M12 7l-6 5v4M12 7l6 5v4M4 16h4v4H4zM16 16h4v4h-4z',
  route: 'M6 4a2 2 0 1 1 0 4 2 2 0 0 1 0-4zM18 16a2 2 0 1 1 0 4 2 2 0 0 1 0-4zM6 8v3a3 3 0 0 0 3 3h6a3 3 0 0 1 3 3',
  sparkle: 'M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3zM19 16l.8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8L19 16z',
  code: 'M8 7l-5 5 5 5M16 7l5 5-5 5M14 4l-4 16',
  file: 'M6 3h8l5 5v13H6zM14 3v5h5',
  image: 'M4 5h16v14H4zM4 16l5-5 4 4 3-3 4 4M15 9h.01',
  layers: 'M12 3 3 8l9 5 9-5-9-5zM3 13l9 5 9-5',
  scout: 'M10 4a6 6 0 1 1 0 12 6 6 0 0 1 0-12zM20 20l-5.5-5.5M10 7v6M7 10h6',
  history: 'M3 12a9 9 0 1 0 3-6.7L3 8M3 3v5h5M12 7v5l3 2',
  cube: 'M12 2.5 20.5 7v10L12 21.5 3.5 17V7L12 2.5zM3.5 7 12 11.5 20.5 7M12 11.5v10',
  chip: 'M7 7h10v10H7zM10 10h4v4h-4zM9 3v4M15 3v4M9 17v4M15 17v4M3 9h4M3 15h4M17 9h4M17 15h4',
  wrap: 'M4 6h16M4 12h13a3 3 0 0 1 0 6h-4M15 16l-2 2 2 2M4 18h6',
  // single filled circle (rendered with fill, see FILLED)
  dot: 'M12 8a4 4 0 1 1 0 8 4 4 0 0 1 0-8z',
  // aliases
  spark: 'M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3zM19 16l.8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8L19 16z',
  queue: 'M4 6h16M4 12h16M4 18h10M18 16v4M16 18h4',
  book: 'M4 4.5A2.5 2.5 0 0 1 6.5 2H20v17H6.5A2.5 2.5 0 0 0 4 21.5v-17zM4 19.5A2.5 2.5 0 0 1 6.5 17H20',
  paperclip: 'M20 11.5 12 19.5a5 5 0 0 1-7-7l8.5-8.5a3.5 3.5 0 0 1 5 5L10 17.5a2 2 0 0 1-3-3L14.5 7',
  alert: 'M12 3 2 20h20L12 3zM12 10v4M12 17h.01',
};

/** Icons drawn as a solid shape instead of a stroke. */
const FILLED: Partial<Record<IconName, true>> = { dot: true };

export const ICON_NAMES = Object.keys(P) as IconName[];

export type IconName =
  | 'home' | 'ask' | 'agents' | 'models' | 'jobs' | 'knowledge' | 'system' | 'more' | 'menu' | 'search' | 'command'
  | 'chevron-right' | 'chevron-left' | 'chevron-down' | 'chevron-up' | 'arrow-right' | 'arrow-down' | 'external'
  | 'x' | 'check' | 'plus'
  | 'send' | 'stop' | 'mic' | 'attach' | 'copy' | 'refresh' | 'pause' | 'play' | 'download' | 'upload' | 'trash'
  | 'pin' | 'block' | 'promote' | 'rollback' | 'benchmark' | 'test' | 'edit' | 'save' | 'filter' | 'sliders' | 'eye'
  | 'logout'
  | 'info' | 'success' | 'warning' | 'error' | 'paused' | 'offline' | 'busy' | 'unknown' | 'bell' | 'clock'
  | 'gpu' | 'memory' | 'cpu' | 'bolt' | 'server' | 'database' | 'network' | 'shield' | 'lock' | 'key' | 'phone'
  | 'desktop' | 'qr' | 'link' | 'wifi' | 'wifi-off' | 'user' | 'settings' | 'sun' | 'moon' | 'decision' | 'route'
  | 'sparkle' | 'code' | 'file' | 'image' | 'layers' | 'scout' | 'history' | 'cube' | 'chip' | 'dot' | 'wrap'
  | 'spark' | 'queue' | 'book' | 'paperclip' | 'alert';

export interface IconProps {
  name: IconName;
  /** Pixel size (square). Default 20. */
  size?: number;
  /** Accessible name; omit for decorative icons next to text. */
  label?: string;
  strokeWidth?: number;
  class?: string;
}

export function Icon({ name, size = 20, label, strokeWidth = 1.75, class: cls }: IconProps) {
  return (
    <svg
      class={cls ? `lz-icon ${cls}` : 'lz-icon'}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill={FILLED[name] ? 'currentColor' : 'none'}
      stroke={FILLED[name] ? 'none' : 'currentColor'}
      stroke-width={strokeWidth}
      stroke-linecap="round"
      stroke-linejoin="round"
      role={label ? 'img' : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : 'true'}
      focusable="false"
    >
      <path d={P[name]} />
    </svg>
  );
}

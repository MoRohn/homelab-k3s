// PWA plumbing (spec §71, §72): service-worker registration and the browser's install prompt.
// - The worker is registered only in a secure context. Being "secure" isn't enough by itself: on an
//   untrusted (self-signed) certificate Chromium refuses the registration with an SSL error. So the
//   real outcome is published in `swState`, and the Trust page reports install/offline support from it
//   rather than guessing from the protocol.
// - `beforeinstallprompt` fires early and only once, so init() must run at boot (main.tsx), before any
//   screen that offers "Install app". Installing is optional and never required (§72).
import { observable } from '@/api/observable';

export type SwState = 'unsupported' | 'insecure' | 'dev' | 'registering' | 'registered' | 'failed';

/** What happened to the service worker in this tab (drives the Trust page's honest feature list). */
export const swState = observable<SwState>('registering');
/** Browser's reason when registration failed (e.g. an SSL certificate error); shown in technical details only. */
export const swError = observable<string | null>(null);

/** Not in lib.dom: Chromium's install prompt event. */
interface BeforeInstallPromptEvent extends Event {
  prompt(): Promise<void>;
  readonly userChoice: Promise<{ outcome: 'accepted' | 'dismissed'; platform: string }>;
}

let deferred: BeforeInstallPromptEvent | null = null;

/** True while the browser has offered an install prompt we can show from "Install app". */
export const installAvailable = observable<boolean>(false);
/** True when running as an installed app (standalone window). */
export const installed = observable<boolean>(false);

function standalone(): boolean {
  return (
    window.matchMedia?.('(display-mode: standalone)').matches === true ||
    (navigator as Navigator & { standalone?: boolean }).standalone === true
  );
}

/** Show the browser's install dialog. Resolves true when the user accepted. Only call from a click. */
export async function promptInstall(): Promise<boolean> {
  const e = deferred;
  if (!e) return false;
  deferred = null;
  installAvailable.set(false);
  await e.prompt();
  const choice = await e.userChoice;
  return choice.outcome === 'accepted';
}

let started = false;

/** Call once at boot. */
export function initPwa(): void {
  if (started || typeof window === 'undefined') return;
  started = true;
  installed.set(standalone());

  // After a redeploy, a tab opened on the old build asks for chunk hashes that no longer exist (the server
  // answers 404). Reload once to pick up the new index; the session flag stops a loop if the server is broken.
  window.addEventListener('vite:preloadError', (e) => {
    try {
      if (sessionStorage.getItem('lz-reloaded-for-chunk')) return;
      sessionStorage.setItem('lz-reloaded-for-chunk', '1');
    } catch {
      return;                                  // no storage: can't guard against a loop, let the error show
    }
    e.preventDefault();
    location.reload();
  });
  // Re-arm only once this load has survived a while, so a chunk that fails right at boot can't reload forever.
  setTimeout(() => {
    try {
      sessionStorage.removeItem('lz-reloaded-for-chunk');
    } catch {
      /* ignore */
    }
  }, 30_000);

  window.addEventListener('beforeinstallprompt', (e) => {
    // Keep the browser's mini-infobar from popping up on its own; the app offers a quiet "Install app" item instead.
    e.preventDefault();
    deferred = e as BeforeInstallPromptEvent;
    installAvailable.set(true);
  });
  window.addEventListener('appinstalled', () => {
    deferred = null;
    installAvailable.set(false);
    installed.set(true);
  });

  if (!('serviceWorker' in navigator)) return swState.set('unsupported');
  if (!window.isSecureContext) return swState.set('insecure');
  // The dev server serves unhashed modules; caching them would only get in the way.
  if (!import.meta.env.PROD) return swState.set('dev');

  const register = () =>
    navigator.serviceWorker.register('/sw.js', { scope: '/' }).then(
      () => swState.set('registered'),
      (err: unknown) => {
        swError.set(err instanceof Error ? err.message : String(err));
        swState.set('failed');
      },
    );
  if (document.readyState === 'complete') void register();
  else window.addEventListener('load', () => void register(), { once: true });
}

/** Browser capabilities that need a trusted HTTPS connection (Trust page, §78). Feature-detected, never UA-sniffed. */
export function capabilities() {
  const w = window as Window & { SpeechRecognition?: unknown; webkitSpeechRecognition?: unknown };
  const secure = window.isSecureContext;
  return {
    secure,
    voice: secure && !!(w.SpeechRecognition ?? w.webkitSpeechRecognition),
    notifications: secure && 'Notification' in window,
    clipboard: secure && !!navigator.clipboard?.writeText,
    serviceWorker: 'serviceWorker' in navigator,
  };
}

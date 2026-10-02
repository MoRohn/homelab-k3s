// Dictation through the browser's Web Speech API (§19), feature-detected and hidden when absent.
// Honesty note: Chrome and Edge recognise speech on their vendors' cloud services, not on this device
// or on Labzilla — so the first use shows a one-time disclosure (Ask.tsx). The transcribed TEXT then
// follows the prompt's own privacy setting like anything typed.
import { useEffect, useRef, useState } from 'preact/hooks';

// Not in TypeScript's DOM lib: the minimal surface we use.
interface SpeechAlternative {
  transcript: string;
}
interface SpeechResult {
  readonly isFinal: boolean;
  readonly length: number;
  [i: number]: SpeechAlternative;
}
interface SpeechEvent {
  resultIndex: number;
  results: { readonly length: number; [i: number]: SpeechResult };
}
interface Recognition {
  lang: string;
  interimResults: boolean;
  continuous: boolean;
  start(): void;
  stop(): void;
  abort(): void;
  onresult: ((e: SpeechEvent) => void) | null;
  onerror: ((e: { error: string }) => void) | null;
  onend: (() => void) | null;
}
type RecognitionCtor = new () => Recognition;

/** The constructor when dictation can work here: needs a secure context for microphone access. */
export function speechSupported(): RecognitionCtor | null {
  if (typeof window === 'undefined' || !window.isSecureContext) return null;
  const w = window as unknown as { SpeechRecognition?: RecognitionCtor; webkitSpeechRecognition?: RecognitionCtor };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

const ERRORS: Record<string, string> = {
  'not-allowed': 'Microphone access was denied. Allow it in the browser’s site settings to dictate.',
  'service-not-allowed': 'This browser doesn’t allow dictation here.',
  'audio-capture': 'No microphone was found.',
  network: 'The browser’s speech service couldn’t be reached. Type or paste instead.',
  'no-speech': 'Didn’t catch anything. Try again closer to the microphone.',
  'language-not-supported': 'Dictation doesn’t support this language in this browser.',
};

export interface Dictation {
  supported: boolean;
  listening: boolean;
  /** Words recognised so far that aren't final yet (shown greyed). */
  interim: string;
  error: string | null;
  start: () => void;
  stop: () => void;
}

/** onFinal receives each finished phrase; the caller appends it to the prompt. */
export function useDictation(onFinal: (text: string) => void): Dictation {
  const Ctor = speechSupported();
  const rec = useRef<Recognition | null>(null);
  const cb = useRef(onFinal);
  cb.current = onFinal;
  const [listening, setListening] = useState(false);
  const [interim, setInterim] = useState('');
  const [error, setError] = useState<string | null>(null);

  useEffect(
    () => () => {
      // Unmounting: detach first so a late onend/onerror can't touch this component's state.
      const r = rec.current;
      rec.current = null;
      if (!r) return;
      r.onresult = r.onerror = r.onend = null;
      r.abort();
    },
    [],
  );

  const start = () => {
    if (!Ctor || rec.current) return;
    const r = new Ctor();
    r.lang = navigator.language || 'en-US';
    r.interimResults = true;
    r.continuous = true;
    r.onresult = (e) => {
      // One event can carry several final phrases; hand them over in one call so none is lost.
      let pending = '';
      const done: string[] = [];
      // Finals from resultIndex on are new; the interim line is every result not yet final, including
      // unchanged ones before resultIndex (else words flicker out of the grey preview).
      for (let i = 0; i < e.results.length; i++) {
        const res = e.results[i];
        const text = res?.[0]?.transcript ?? '';
        if (res?.isFinal) {
          if (i >= e.resultIndex) done.push(text.trim());
        } else pending += text;
      }
      if (done.length) cb.current(done.filter(Boolean).join(' '));
      setInterim(pending);
    };
    r.onerror = (e) => {
      if (e.error !== 'aborted') setError(ERRORS[e.error] ?? 'Dictation stopped unexpectedly.');
    };
    r.onend = () => {
      rec.current = null;
      setListening(false);
      setInterim('');
    };
    setError(null);
    try {
      r.start();
      rec.current = r;
      setListening(true);
    } catch {
      setError('Dictation couldn’t start in this browser.');
    }
  };

  const stop = () => rec.current?.stop();

  return { supported: !!Ctor, listening, interim, error, start, stop };
}

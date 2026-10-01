// Fetch wrapper for the console API (/api/*, same origin only).
// - Cookies ride along (credentials: same-origin); mutations echo the lz_csrf cookie in X-Labzilla-CSRF.
// - Every failure becomes a HumanError (spec §81): ApiError for server answers, OfflineError when the
//   console can't be reached at all (§62). Pages never see a bare status code.
// - 401 sends the user to /login?next=<here> unless the caller opts out (login/setup/pair, /auth/me probes).
import type {
  AskStreamEvent,
  AskStreamPayloads,
  HumanError,
  MessageRequest,
  ProposedAction,
} from './contracts.gen';
import { observable } from './observable';

export const CSRF_COOKIE = 'lz_csrf';
export const CSRF_HEADER = 'X-Labzilla-CSRF';

/** False after a network failure, true again after any successful response (drives ConnectionBanner). */
export const reachable = observable<boolean>(true);

export class ApiError extends Error {
  readonly status: number;
  readonly error: HumanError;
  constructor(status: number, error: HumanError) {
    super(error.title);
    this.name = 'ApiError';
    this.status = status;
    this.error = error;
  }
}

export class OfflineError extends Error {
  readonly error: HumanError;
  constructor(cause?: unknown) {
    super('Labzilla unavailable');
    this.name = 'OfflineError';
    this.error = humanError(
      'Labzilla unavailable',
      'Not connected to your local network, or Labzilla is restarting.',
      'Check that this device is on your home network, then retry.',
      [{ label: 'Retry', action: 'retry' }],
      cause instanceof Error ? cause.message : undefined,
    );
  }
}

export interface RequestOptions {
  signal?: AbortSignal;
  /** Return the 401 as an ApiError instead of redirecting to /login (auth pages, session probes). */
  allow401?: boolean;
  headers?: Record<string, string>;
}

export function humanError(
  title: string,
  impact = '',
  next_step = '',
  actions: HumanError['actions'] = [],
  techDetail?: string,
): HumanError {
  return { title, impact, next_step, actions, tech: techDetail ? [{ label: 'detail', value: techDetail }] : [] };
}

/** Any thrown value → a HumanError for HumanErrorCard. */
export function toHumanError(e: unknown): HumanError {
  if (e instanceof ApiError || e instanceof OfflineError) return e.error;
  if (e instanceof DOMException && e.name === 'AbortError') return humanError('Cancelled', 'The request was stopped.');
  return humanError(
    'Something went wrong',
    "This view couldn't finish loading.",
    'Retry; if it keeps happening, check System → Logs.',
    [{ label: 'Retry', action: 'retry' }],
    e instanceof Error ? e.message : String(e),
  );
}

export function readCookie(name: string): string | null {
  for (const part of document.cookie.split(';')) {
    const [k, ...v] = part.trim().split('=');
    if (k === name) return decodeURIComponent(v.join('='));
  }
  return null;
}

/** Send the browser to sign in, returning here afterwards. No-op on the public auth pages. */
export function loginRedirect(): void {
  const here = location.pathname + location.search;
  if (/^\/(login|setup|pair)(\/|$)/.test(location.pathname)) return;
  location.assign(`/login?next=${encodeURIComponent(here)}`);
}

const SAFE = new Set(['GET', 'HEAD', 'OPTIONS']);

async function send(method: string, path: string, body: unknown, opts: RequestOptions, accept: string): Promise<Response> {
  const headers: Record<string, string> = { Accept: accept, ...opts.headers };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (!SAFE.has(method)) {
    const token = readCookie(CSRF_COOKIE);
    if (token) headers[CSRF_HEADER] = token;
  }
  let res: Response;
  try {
    res = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: 'same-origin',
      cache: 'no-store',
      signal: opts.signal,
    });
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') throw e;
    reachable.set(false);
    throw new OfflineError(e);
  }
  reachable.set(true);
  if (res.ok) return res;
  throw await failure(res, opts);
}

async function failure(res: Response, opts: RequestOptions): Promise<ApiError> {
  let error: HumanError | undefined;
  try {
    const parsed = (await res.json()) as { error?: HumanError };
    if (parsed && typeof parsed.error === 'object' && parsed.error && 'title' in parsed.error) error = parsed.error;
  } catch {
    /* not JSON: a proxy page (Traefik 502/504) or an empty body */
  }
  error ??= fallbackError(res.status);
  if (res.status === 401 && !opts.allow401) loginRedirect();
  return new ApiError(res.status, error);
}

function fallbackError(status: number): HumanError {
  if (status === 401) return humanError('Sign in to continue', 'Your session has ended.', 'Sign in again.', [{ label: 'Sign in', action: 'login' }]);
  if (status === 403) return humanError('Not allowed from this session', 'Nothing was changed.', 'Use an admin session for this action.');
  if (status === 404) return humanError('Not found', "This item doesn't exist or is no longer available.", 'Go back and refresh.');
  if (status === 429) return humanError('Too many attempts', 'Labzilla is slowing requests from this device.', 'Wait a minute and try again.');
  if (status >= 500)
    return humanError('Labzilla is temporarily unavailable', 'Some features may not work right now.', 'Retry in a moment.', [
      { label: 'Retry', action: 'retry' },
    ]);
  return humanError("That request wasn't accepted", 'Nothing was changed.', 'Check the input and try again.');
}

async function parse<T>(res: Response): Promise<T> {
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

export async function request<T>(method: string, path: string, body?: unknown, opts: RequestOptions = {}): Promise<T> {
  return parse<T>(await send(method, path, body, opts, 'application/json'));
}

export const get = <T>(path: string, opts?: RequestOptions) => request<T>('GET', path, undefined, opts);
export const post = <T>(path: string, body?: unknown, opts?: RequestOptions) => request<T>('POST', path, body ?? {}, opts);
export const del = <T>(path: string, opts?: RequestOptions) => request<T>('DELETE', path, undefined, opts);
export const patch = <T>(path: string, body?: unknown, opts?: RequestOptions) => request<T>('PATCH', path, body ?? {}, opts);

/** Build a query string, skipping undefined/null/'' values: qs({status: 'failed'}) → '?status=failed'. */
export function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== '') sp.set(k, String(v));
  const s = sp.toString();
  return s ? `?${s}` : '';
}

/** Run a command-bar ProposedAction — only ever after the user pressed its button (and confirmed if asked). */
export function runProposedAction<T = unknown>(action: ProposedAction, confirmText?: string): Promise<T> {
  const body = confirmText === undefined ? action.body : { ...action.body, confirm: confirmText };
  return request<T>(action.method, action.path, action.method === 'DELETE' ? undefined : body);
}

// ── streaming POST (Ask): EventSource can't POST, so parse text/event-stream from fetch ──────────

export type StreamHandlers<M> = { [K in keyof M]?: (data: M[K]) => void };

/**
 * POST a JSON body and dispatch each SSE frame (`event:` + `data:` JSON) to the matching handler.
 * Resolves when the stream ends; rejects with ApiError/OfflineError like request(). Abort via opts.signal.
 */
export async function postStream<M>(path: string, body: unknown, handlers: StreamHandlers<M>, opts: RequestOptions = {}): Promise<void> {
  const res = await send('POST', path, body, opts, 'text/event-stream');
  if (!res.body) return;
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buf = '';
  const dispatch = (frame: string) => {
    let event = 'message';
    const data: string[] = [];
    for (const line of frame.split('\n')) {
      if (line.startsWith(':')) continue;
      const i = line.indexOf(':');
      const field = i < 0 ? line : line.slice(0, i);
      const value = i < 0 ? '' : line.slice(i + 1).replace(/^ /, '');
      if (field === 'event') event = value;
      else if (field === 'data') data.push(value);
    }
    if (!data.length) return;
    const fn = (handlers as Record<string, ((d: unknown) => void) | undefined>)[event];
    if (fn) fn(JSON.parse(data.join('\n')));
  };
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += value.replace(/\r\n?/g, '\n');
      let cut: number;
      while ((cut = buf.indexOf('\n\n')) >= 0) {
        dispatch(buf.slice(0, cut));
        buf = buf.slice(cut + 2);
      }
    }
    if (buf.trim()) dispatch(buf);
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') throw e;
    reachable.set(false);
    throw new OfflineError(e);
  } finally {
    reader.releaseLock();
  }
}

/** Typed Ask stream: POST /api/ai/threads/{id}/messages → route, phase, delta, receipt, error, done. */
export function askStream(
  threadId: string,
  body: MessageRequest,
  handlers: StreamHandlers<Pick<AskStreamPayloads, AskStreamEvent>>,
  opts?: RequestOptions,
): Promise<void> {
  return postStream(`/api/ai/threads/${encodeURIComponent(threadId)}/messages`, body, handlers, opts);
}

// Quick Ask popover. Plain script, no build step: Tauri injects window.__TAURI__ (withGlobalTauri).
// Sending hands the prompt to the console's own deep link (/ask?q=…&send=1) in the main window, so
// sign-in, privacy defaults and history stay the console's job.
const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;

const $ = (id) => document.getElementById(id);
const q = $('q');

function setStatus(text, cls, title = '') {
  const el = $('status');
  el.textContent = text;
  el.className = 'status ' + cls;
  el.title = title;
}

// The tray's probe is a TCP connect: it can't see certificate trust. This web view uses the same trust
// store as the console window, so a no-cors fetch fails exactly when the console would show
// "Unacceptable TLS certificate" (the Labzilla CA isn't installed on this computer yet).
async function checkTrust(url) {
  try {
    await fetch(new URL('/healthz', url), { mode: 'no-cors', cache: 'no-store' });
    setStatus('Online', 'ok');
    $('fix-trust').hidden = true;
  } catch {
    setStatus('Certificate not trusted', 'down',
      'This computer does not trust the Labzilla CA yet. "How to fix" opens Trust this device to download it.');
    $('fix-trust').hidden = false;
  }
}

function render(s) {
  if (s.online !== true) $('fix-trust').hidden = true;
  if (s.online === null) setStatus('Checking…', '');
  else if (!s.online) setStatus('Not reachable', 'down');
  else checkTrust(s.url);
  $('server').textContent = s.host;
  $('server-url').value = s.url.replace(/\/$/, '');
}

async function send() {
  const text = q.value.trim();
  if (!text) return;
  try {
    await invoke('ask', { text });
    q.value = '';
  } catch (e) {
    $('status').textContent = String(e);
  }
}

q.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    send();
  }
});
$('ask').addEventListener('submit', (e) => {
  e.preventDefault();
  send();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') invoke('hide_quick');
});
$('open').addEventListener('click', () => invoke('open_console'));
$('fix-trust').addEventListener('click', () => invoke('open_trust_page'));
$('server').addEventListener('click', () => {
  $('server-form').hidden = !$('server-form').hidden;
  if (!$('server-form').hidden) $('server-url').focus();
});
$('server-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  $('server-error').textContent = '';
  try {
    render(await invoke('set_server', { url: $('server-url').value }));
    $('server-form').hidden = true;
    q.focus();
  } catch (err) {
    $('server-error').textContent = String(err);
  }
});

listen('status', (e) => render(e.payload));
listen('focus-input', () => q.focus());
invoke('get_status').then(render);

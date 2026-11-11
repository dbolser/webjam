// The one document 'paste' listener (docs/phase1b-shell.md §4.3, docs/phase1-ops-api.md §5.3).
//
//   register({ name, priority, handle(payload, event) -> boolean | Promise<boolean> })
//
// payload = { json, html, text, target, inOmnibox }. Higher priority goes first. A handler decides
// synchronously: it returns true (or a promise, for async work) to take the paste, anything else
// to pass it on. Lane A registers
// 'playlist-edit' (priority 10), Lane C registers 'crate' (priority 0).
// Pastes into inputs, textareas and contenteditable are left alone, except the omnibox when the
// pasted text contains a URL.

const handlers = [];
const URL_RE = /https?:\/\/\S+|(?:^|\s)\/(?:p|t)\/\S+/i;

export function register(h) {
  if (!h || typeof h.handle !== 'function') throw new TypeError('pasteRouter.register: handle is required');
  handlers.push({ priority: 0, ...h });
  handlers.sort((a, b) => b.priority - a.priority);
}

function decode(dt) {
  if (!dt) return { json: null, html: '', text: '' };
  let json = null;
  try {
    const raw = dt.getData('application/x-webjam+json');
    if (raw) json = JSON.parse(raw);
  } catch {
    json = null;
  }
  let html = '';
  let text = '';
  try { html = dt.getData('text/html') || ''; } catch { html = ''; }
  try { text = dt.getData('text/plain') || ''; } catch { text = ''; }
  return { json, html, text };
}

let started = false;
export function startPasteRouter() {
  if (started) return;
  started = true;
  document.addEventListener('paste', (event) => {
    const target = event.target instanceof Element ? event.target : null;
    const inOmnibox = !!(target && target.closest('#omnibox'));
    const inField = !!(target && target.closest('input, textarea, select, [contenteditable=""], [contenteditable="true"]'));
    const payload = { ...decode(event.clipboardData), target, inOmnibox };
    if (inField && !(inOmnibox && URL_RE.test(payload.text))) return;
    if (!payload.json && !payload.html && !payload.text.trim()) return;
    for (const h of handlers) {
      let out;
      try {
        out = h.handle(payload, event);
      } catch (err) {
        console.error(`paste handler ${h.name}`, err);
        continue;
      }
      if (out === true || (out && typeof out.then === 'function')) {
        event.preventDefault();
        if (out !== true) out.catch((err) => console.error(`paste handler ${h.name}`, err));
        return;
      }
    }
  });
}

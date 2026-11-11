// One delegated listener per event type on document, dispatching on the closest [data-action].
// No on* attributes or handler properties anywhere: register here instead.
//
//   registerActions({ 'name': (el, event, ctx) => {} })   duplicate names throw
//
// Dispatch rules:
//   click   closest [data-action] that is not a form, input, select or textarea (use <button>)
//   submit  the <form data-action>; preventDefault() is called for you
//   change  / input   the target itself when it carries data-action (checkboxes, selects, ranges,
//           text fields); the handler sees event.type to tell them apart
// ctx = { trackId, playlistId, videoId, row, card, list } read from the closest row / card / list.
// A handler on a link (<a data-action href>) gets the click; it calls event.preventDefault() if it
// handles navigation itself, otherwise the router follows the href.

const handlers = new Map();

export function registerActions(map) {
  for (const [name, fn] of Object.entries(map)) {
    if (handlers.has(name)) throw new Error(`Action "${name}" is already registered`);
    if (typeof fn !== 'function') throw new TypeError(`Action "${name}" must be a function`);
    handlers.set(name, fn);
  }
}

export function hasAction(name) {
  return handlers.has(name);
}

/** Run an action by name (keys and menus use this). */
export function runAction(name, el, event, ctx) {
  const fn = handlers.get(name);
  if (!fn) return false;
  fn(el, event, ctx || contextOf(el));
  return true;
}

function num(v) {
  const n = Number(v);
  return Number.isFinite(n) && v !== '' && v != null ? n : null;
}

export function contextOf(el) {
  if (!el || !el.closest) return { trackId: null, playlistId: null, videoId: null, row: null, card: null, list: null };
  const row = el.closest('[data-track-id]');
  const card = el.closest('[data-card-playlist-id]');
  const list = el.closest('[data-list-key]');
  const pidEl = el.closest('[data-playlist-id]');
  const vidEl = el.closest('[data-video-id]');
  return {
    trackId: row ? num(row.dataset.trackId) : null,
    playlistId: pidEl ? num(pidEl.dataset.playlistId) : card ? num(card.dataset.cardPlaylistId) : null,
    videoId: vidEl ? vidEl.dataset.videoId : null,
    row,
    card,
    list,
  };
}

const FIELD = 'input, select, textarea';

function dispatch(name, el, event) {
  const fn = handlers.get(name);
  if (!fn) {
    if (name) console.warn(`No handler for data-action="${name}"`);
    return;
  }
  try {
    const out = fn(el, event, contextOf(el));
    if (out && typeof out.catch === 'function') out.catch((err) => console.error(`action ${name}`, err));
  } catch (err) {
    console.error(`action ${name}`, err);
  }
}

let started = false;
export function startActions() {
  if (started) return;
  started = true;
  document.addEventListener('click', (event) => {
    if (event.defaultPrevented) return;
    const el = event.target instanceof Element ? event.target.closest('[data-action]') : null;
    if (!el || el.tagName === 'FORM' || el.matches(FIELD)) return;
    if (el.matches('[disabled], [aria-disabled="true"]')) return;
    if (el.tagName === 'BUTTON' && !el.closest('form')) event.preventDefault();
    dispatch(el.dataset.action, el, event);
  });
  document.addEventListener('submit', (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.dataset.action) return;
    event.preventDefault();
    dispatch(form.dataset.action, form, event);
  });
  for (const type of ['change', 'input']) {
    document.addEventListener(type, (event) => {
      const el = event.target;
      if (!(el instanceof Element) || !el.dataset.action || el.tagName === 'FORM') return;
      if (!el.matches('input, select, textarea')) return;
      dispatch(el.dataset.action, el, event);
    });
  }
}

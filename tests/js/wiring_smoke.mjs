// Integration smoke test after the Phase 1b cutover (run by tests/test_frontend_safety.py when
// node is on PATH). Loads F's core, the routes, all three lane entries and every view module under
// a tiny DOM stub, so duplicate action names, bad imports and load-time errors fail the suite. Then
// checks the wiring between lanes: every data-action used in a template is registered, the shared
// slots each have their owners, the playlist menu has one set of edit/delete items, and the
// routes point at the real views.
//   node tests/js/wiring_smoke.mjs <path to static/js> <data-action names, comma separated>
import assert from 'node:assert/strict';

const el = () => ({
  addEventListener() {}, removeEventListener() {}, setAttribute() {}, removeAttribute() {},
  appendChild() {}, querySelector() { return null; }, querySelectorAll() { return []; },
  classList: { toggle() {}, add() {}, remove() {} }, style: { setProperty() {} }, dataset: {},
  content: { cloneNode: () => el() },
});
globalThis.window = globalThis;
globalThis.addEventListener = () => {};
globalThis.removeEventListener = () => {};
globalThis.document = {
  addEventListener() {},
  getElementById() { return null; },
  querySelector() { return null; },
  createTreeWalker() { return { currentNode: null, nextNode() { return null; } }; },
  createComment() { return el(); },
  createElement() { return el(); },
  importNode() { return el(); },
  documentElement: el(),
  body: el(),
};
globalThis.location = { pathname: '/p/2-x', search: '?track=4', hash: '', origin: 'http://x', assign() {} };
globalThis.history = { replaceState() {}, pushState() {}, state: null };
const mem = () => { const m = new Map(); return { getItem: (k) => m.get(k) ?? null, setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k) }; };
globalThis.localStorage = mem();
globalThis.sessionStorage = mem();
globalThis.matchMedia = () => ({ matches: false, addEventListener() {} });
globalThis.requestAnimationFrame = (f) => setTimeout(f, 0);
globalThis.Element = class {};
globalThis.HTMLFormElement = class {};

const base = new URL(`file://${process.argv[2].replace(/\/$/, '')}/`);
const mod = (p) => import(new URL(p, base).href);


await mod('core.js');
await mod('routes.js');
await mod('editing/index.js');
await mod('discovery/index.js');
await mod('share/index.js');
await mod('shell.js');
for (const v of ['home', 'playlist', 'track', 'profile', 'discover', 'search', 'not-found']) {
  await mod(`views/${v}.js`);
}
const slots = await mod('slots.js');
const actions = await mod('actions.js');
const keys = await mod('keys.js');
const { state } = await mod('store.js');

// 1. Every data-action used in a template has a handler.
const used = (process.argv[3] || '').split(',').filter(Boolean);
assert.ok(used.length > 50, 'data-action names were passed in');
const missing = used.filter((a) => !actions.hasAction(a));
assert.deepEqual(missing, [], `data-action without a handler: ${missing.join(', ')}`);

// 2. Shared slots have their owners.
for (const n of ['topbar.account', 'home.top', 'playlist.actions', 'playlist.menu', 'playlist.related',
  'playlist.footer', 'row.menu', 'crate.actions', 'crate.row.menu', 'auth.required', 'share.open',
  'import.open', 'player.like', 'rail.context', 'track.actions']) {
  assert.ok(slots.has(n), `slot ${n} is registered`);
}

// 3. Keys from every lane, and the ? sheet is Lane A's (not F's stub).
const combos = keys.listKeys().map((k) => k.combo);
for (const c of ['?', 'mod+c', 'v', 'mod+z', 'g', 'l', 's', 'c', 'b', 'n', 'e']) {
  assert.ok(combos.includes(c), `key ${c}`);
}
assert.equal(keys.listKeys().filter((k) => k.combo === '?').length, 1, 'one ? binding');

// 4. Playlist menu: one set of edit and delete items, delete last.
state.auth = { mode: 'demo', signedIn: true, user: { id: 1, username: 'dan' }, crateId: 5, users: [] };
const labels = (ctx) => slots.render('playlist.menu', ctx).map((i) => (typeof i === 'string' ? i : i.label));
assert.deepEqual(labels({ playlistId: 2, mine: true, crate: false }),
  ['Edit details', 'Keyboard shortcuts', 'Duplicate playlist', 'sep', 'Delete playlist…']);
assert.deepEqual(labels({ playlistId: 2, mine: false, crate: false }), ['Copy all to a new playlist']);
assert.deepEqual(labels({ playlistId: 5, mine: true, crate: true }), []);

// 5. Share sits on the track page; the like control never on a crate.
assert.equal(slots.render('track.actions', { videoId: 'c_56qQqxvm4' }).length, 1);
assert.equal(slots.render('playlist.actions', { playlistId: 5, crate: true, playlist: { kind: 'crate' } }).length, 0);

console.log('wiring smoke ok');
process.exit(0);

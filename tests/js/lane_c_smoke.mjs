// Lane C module smoke test (run by tests/test_lane_c.py when node is on PATH).
// Evaluates F's core, the routes and the lane C entry under a tiny DOM stub, so load-time errors
// (duplicate action names, bad imports, TDZ) fail the suite, then checks the registrations and the
// pure helpers. No rendering: lit only needs a few document methods at import time.
//   node tests/js/lane_c_smoke.mjs <path to static/js>
import assert from 'node:assert/strict';

const el = () => ({
  addEventListener() {}, removeEventListener() {}, setAttribute() {}, removeAttribute() {},
  appendChild() {}, querySelector() { return null; }, querySelectorAll() { return []; },
  classList: { toggle() {}, add() {}, remove() {} }, style: { setProperty() {} }, dataset: {},
  content: { cloneNode: () => el() },
});
globalThis.window = globalThis;
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
await mod('share/index.js');
await mod('shell.js');
const slots = await mod('slots.js');
const actions = await mod('actions.js');
const keys = await mod('keys.js');
const { state } = await mod('store.js');

for (const n of ['topbar.account', 'home.top', 'playlist.actions', 'playlist.menu', 'row.menu', 'crate.actions',
  'crate.row.menu', 'auth.required', 'share.open', 'import.open']) {
  assert.ok(slots.has(n), `slot ${n} is registered`);
}
for (const a of ['import-open', 'share-open', 'share-copy', 'share-text', 'crate-unload', 'crate-unload-to',
  'crate-promote', 'crate-promote-submit', 'crate-clear-unloaded', 'crate-fold', 'account-menu', 'handle-save',
  'handle-keep', 'persona-create', 'settings-save', 'signout']) {
  assert.ok(actions.hasAction(a), `action ${a}`);
}
assert.equal(keys.listKeys().filter((k) => k.combo === 's').length, 1, 'S shares');

// Paste detection: links and ids yes, free text no.
const { looksPasteable } = await mod('import/paste-to-crate.js');
for (const t of ['https://youtu.be/c_56qQqxvm4?t=3', 'http://localhost:8000/p/2?track=4', '/t/c_56qQqxvm4',
  'c_56qQqxvm4 TNsSBhl_2LI', 'PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG']) {
  assert.ok(looksPasteable({ text: t }), t);
}
for (const t of ['really interesting information about programming', 'hello world', '']) {
  assert.ok(!looksPasteable({ text: t }), t);
}
assert.ok(looksPasteable({ json: { webjam: 1, items: [] } }));

// Account slot: Google signed out is a bare sign-in link (no ?next=), personas only in demo mode.
const { personaItems } = await mod('account/personas.js');
state.auth = { mode: 'google', signedIn: false, user: null, crateId: null, users: [{ id: 2, username: 'maya' }] };
assert.equal(personaItems().length, 0, 'no personas in google mode');
const pill = slots.render('topbar.account', {})[0];
assert.ok(pill.strings.join('').includes('href="/auth/google/login"'), 'bare sign-in link');
state.auth = { mode: 'demo', signedIn: true, user: { id: 1, username: 'dan' }, crateId: 5, users: [{ id: 1, username: 'dan' }, { id: 2, username: 'maya' }] };
assert.deepEqual(personaItems().map((i) => i.label), ['Act as @maya', 'New persona…']);

// Handle card: Google mode and needs_handle only.
assert.equal(slots.render('home.top', { home: { user: { username: 'dan', needs_handle: true } } }).length, 0, 'demo: no prompt');
state.auth = { mode: 'google', signedIn: true, user: { id: 1, username: 'dan', needs_handle: true }, crateId: 5, users: [] };
assert.equal(slots.render('home.top', { home: { user: state.auth.user } }).length, 1);
assert.equal(slots.render('home.top', { home: { user: { username: 'dan', needs_handle: false } } }).length, 0);
const { handleProblem, cleanHandle } = await mod('account/handle-prompt.js');
assert.equal(cleanHandle(' @Dan_B '), 'dan_b');
assert.equal(handleProblem('dan_b'), '');
assert.ok(handleProblem('da'));
assert.ok(handleProblem('dan-b'));

// Menus: fork for everyone (not the crate); settings and delete only for the owner.
state.playlists.set(2, { meta: { id: 2, title: 'T', visibility: 'public', username: 'maya' }, order: [], kind: 'playlist', ownerId: 3 });
const labels = (ctx) => slots.render('playlist.menu', ctx).map((i) => (typeof i === 'string' ? i : i.label));
assert.deepEqual(labels({ playlistId: 2, mine: false, crate: false }), ['Copy all to a new playlist']);
assert.ok(labels({ playlistId: 2, mine: true, crate: false }).includes('Settings…'));
assert.deepEqual(labels({ playlistId: 5, mine: true, crate: true }), []);
assert.equal(slots.render('playlist.actions', { playlistId: 5, crate: true }).length, 0, 'no Share on the crate');

// Sign-in intents: stored only for replayable actions, return to the page (never an /api/ path).
const { goSignIn } = await mod('account/signin-intent.js');
let went = '';
location.assign = (u) => { went = u; };
goSignIn({ action: 'crate-add', payload: { items: [{ video_id: 'c_56qQqxvm4', extra: 'x' }] }, path: '/p/2-x' });
assert.equal(went, '/auth/google/login?next=%2Fp%2F2-x');
const saved = JSON.parse(sessionStorage.getItem('webjam.intent'));
assert.deepEqual(saved.payload, { items: [{ video_id: 'c_56qQqxvm4', start_seconds: null, end_seconds: null }] });
sessionStorage.removeItem('webjam.intent');
goSignIn({ action: 'like', payload: { playlist_id: 2 }, error: {}, path: '/api/playlists/2/like' });
assert.equal(went, '/auth/google/login?next=%2Fp%2F2-x%3Ftrack%3D4', 'an API 401 returns to the page');
goSignIn({ action: 'fork', path: '/p/2' });
assert.equal(sessionStorage.getItem('webjam.intent') && JSON.parse(sessionStorage.getItem('webjam.intent')).action, 'like');
goSignIn({ action: 'crate-add', path: '//evil.example' });
assert.equal(went, '/auth/google/login', 'unsafe paths are dropped');

console.log('lane C smoke ok');
process.exit(0);

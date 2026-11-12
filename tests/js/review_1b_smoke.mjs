// Regression checks for the Phase 1b review fixes that live in front-end logic (run by
// tests/test_review_1b.py when node is on PATH). Same tiny DOM stub as wiring_smoke.mjs.
//   node tests/js/review_1b_smoke.mjs <path to static/js>
import assert from 'node:assert/strict';

const el = () => ({
  addEventListener() {}, removeEventListener() {}, setAttribute() {}, removeAttribute() {},
  appendChild() {}, querySelector() { return null; }, querySelectorAll() { return []; },
  classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } }, style: { setProperty() {} }, dataset: {},
  content: { cloneNode: () => el() },
});
globalThis.window = globalThis;
globalThis.addEventListener = () => {};
globalThis.removeEventListener = () => {};
globalThis.document = {
  addEventListener() {},
  getElementById() { return null; },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  createTreeWalker() { return { currentNode: null, nextNode() { return null; } }; },
  createComment() { return el(); },
  createElement() { return el(); },
  importNode() { return el(); },
  documentElement: el(),
  body: el(),
};
globalThis.location = { pathname: '/', search: '', hash: '', origin: 'http://x', assign() {} };
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
await mod('editing/index.js');
await mod('discovery/index.js');
const store = await mod('store.js');
const ops = await mod('editing/ops.js');
const api = await mod('api.js');
const keys = await mod('keys.js');
const { nothing } = await mod('lit.js');
const { badgesSection } = await mod('discovery/badges.js');
const { state } = store;
const tick = () => new Promise((r) => setTimeout(r, 0));

// 1. String row ids (search `s:VID`, discover `d:VID`, track page `a:VID`) survive the id helpers.
assert.equal(ops.rowKey('s:abcdefghijk'), 's:abcdefghijk');
assert.equal(ops.rowKey('a:abc-def_123'), 'a:abc-def_123');
assert.equal(ops.rowKey('12'), 12);
assert.equal(ops.rowKey('-3'), -3);
assert.equal(ops.rowKey(''), null);
assert.equal(ops.real('s:abcdefghijk'), 's:abcdefghijk');
assert.ok(!(ops.real('s:abcdefghijk') > 0), 'a synthetic id is never sent as a track id');
assert.equal(ops.real('7'), 7);

// 2. V, V, V faster than the round trip: the first response keeps the later pending temp rows.
state.auth = { mode: 'demo', signedIn: true, user: { id: 1, username: 'dan' }, crateId: 5, users: [] };
store.ingestPlaylist({ id: 4, version: 1, user_id: 1, kind: 'playlist', tracks: [
  { id: 10, video_id: 'aaaaaaaaaaa' }, { id: 20, video_id: 'bbbbbbbbbbb' }, { id: 30, video_id: 'ccccccccccc' },
] });
const t1 = store.optimistic({ kind: 'insert', playlist_id: 4, after_track_id: 20, items: [{ video_id: 'bbbbbbbbbbb' }] });
const p4 = state.playlists.get(4);
const temp1 = p4.order[2];
assert.ok(temp1 < 0);
const t2 = store.optimistic({ kind: 'insert', playlist_id: 4, after_track_id: temp1, items: [{ video_id: 'bbbbbbbbbbb' }] });
const temp2 = p4.order[3];
assert.ok(temp2 < 0 && temp2 !== temp1);
assert.deepEqual(p4.order, [10, 20, temp1, temp2, 30]);
const res1 = { results: [{ kind: 'insert', track_ids: [31] }], order: { 4: [10, 20, 31, 30] }, versions: { 4: 2 }, rows: { 31: { video_id: 'bbbbbbbbbbb' } } };
store.settle(t1, res1);
store.reconcile(res1);
assert.deepEqual(p4.order, [10, 20, 31, temp2, 30], 'the queued paste keeps its place after the landed one');
assert.ok(state.tracks.has(temp2), 'the queued temp row is not deleted');
assert.ok(!state.tracks.has(temp1), 'the landed temp row is replaced');
assert.equal(p4.version, 2);
const res2 = { results: [{ kind: 'insert', track_ids: [32] }], order: { 4: [10, 20, 31, 32, 30] }, versions: { 4: 3 }, rows: { 32: { video_id: 'bbbbbbbbbbb' } } };
store.settle(t2, res2);
store.reconcile(res2);
assert.deepEqual(p4.order, [10, 20, 31, 32, 30]);
assert.ok(!state.tracks.has(temp2));
// A failed insert takes out only its own temp rows, not edits reconciled since.
const t3 = store.optimistic({ kind: 'insert', playlist_id: 4, items: [{ video_id: 'ddddddddddd' }] });
store.reconcile({ order: { 4: [10, 20, 31, 32, 30, 40] }, versions: { 4: 4 }, rows: { 40: { video_id: 'eeeeeeeeeee' } }, results: [] });
store.rollback(t3);
assert.deepEqual(p4.order, [10, 20, 31, 32, 30, 40]);
assert.deepEqual(store.keepPending([1, -2, 3], [1, 3], new Set([-2])), [1, -2, 3]);
assert.deepEqual(store.keepPending([-9, -2, 3], [5, 3], new Set([-2]), new Map([[-9, 5]])), [5, -2, 3]);

// 3. stale_version on a dock playlist that is not loaded adopts the server's versions.
state.dock = { crate: { kind: 'crate', playlist_id: 5, version: 1, track_count: 0 }, pinned: [], recent: [{ playlist_id: 17, title: 'x', version: 1, track_count: 3, slot: 2 }] };
const calls = [];
let reply = () => ({ status: 409, body: { detail: 'Playlist 17 changed since you loaded it.', code: 'stale_version', conflicts: [{ playlist_id: 17, expected: 1, current: 2 }], versions: { 17: 2 } } });
globalThis.fetch = async (url, init) => {
  calls.push({ url, body: JSON.parse(init.body || '{}'), headers: init.headers });
  const r = reply();
  return { ok: r.status < 400, status: r.status, text: async () => JSON.stringify(r.body) };
};
await assert.rejects(api.ops.send([{ kind: 'insert', playlist_id: 17, items: [{ video_id: 'aaaaaaaaaaa' }] }], { expectedVersions: () => store.expectedFor([17]) }));
await tick();
assert.equal(calls[0].body.expected_versions['17'], 1);
assert.equal(calls[0].headers['X-Webjam'], '1');
assert.equal(state.dock.recent[0].version, 2, 'the chip version follows the 409');
reply = () => ({ status: 200, body: { results: [{ kind: 'insert', track_ids: [99] }], order: { 17: [1, 2, 3, 99] }, versions: { 17: 3 } } });
await api.ops.send([{ kind: 'insert', playlist_id: 17, items: [{ video_id: 'aaaaaaaaaaa' }] }], { expectedVersions: () => store.expectedFor([17]) });
assert.equal(calls[1].body.expected_versions['17'], 2, 'the retry sends the adopted version');

// 4. A function for expectedVersions is read at send time (after earlier queued edits land).
calls.length = 0;
state.dock.crate.version = 1;
reply = () => ({ status: 200, body: { results: [{ kind: 'delete' }], order: { 5: [] }, versions: { 5: Number(calls.length) + 1 } } });
const a = api.ops.send([{ kind: 'delete', playlist_id: 5, track_ids: [1] }], { expectedVersions: () => store.expectedFor([5]) });
const b = api.ops.send([{ kind: 'delete', playlist_id: 5, track_ids: [2] }], { expectedVersions: () => store.expectedFor([5]) });
await Promise.all([a, b]);
assert.equal(calls[0].body.expected_versions['5'], 1);
assert.equal(calls[1].body.expected_versions['5'], 2);

// 5. Badges: nothing for a visitor when nothing is earned; "to find" only for the owner.
const catalog = [{ key: 'k1', name: 'One', icon: '◈', description: 'd' }];
assert.equal(badgesSection({ badge_catalog: catalog, earned_badges: [] }, { owner: false }), nothing);
assert.notEqual(badgesSection({ badge_catalog: catalog, earned_badges: [] }, { owner: true }), nothing);

// 6. The ? sheet has one Esc row: every labelled escape binding shares a label.
const escLabels = new Set(keys.listKeys().filter((k) => k.combo === 'escape').map((k) => k.label));
assert.equal(escLabels.size, 1, `escape labels: ${[...escLabels].join(' | ')}`);

console.log('review 1b smoke ok');

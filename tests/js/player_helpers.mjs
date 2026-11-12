// Unit tests for the player's phone helpers (docs/phase1b-shell.md §12), run by
// tests/test_player_helpers.py when node is on PATH:
//   resume.js         serialisation, validation, 7-day expiry, per-user keys, clip bounds
//   wake-lock.js      the acquire/release state machine against a fake navigator and document
//   media-session.js  metadata, position state and handler wiring against a fake mediaSession
//   node tests/js/player_helpers.mjs <path to static/js>
import assert from 'node:assert/strict';

const base = new URL(`file://${process.argv[2].replace(/\/$/, '')}/`);
const mod = (p) => import(new URL(p, base).href);

const R = await mod('player/resume.js');
const W = await mod('player/wake-lock.js');
const M = await mod('player/media-session.js');

const mem = () => {
  const m = new Map();
  return { m, getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k) };
};
const tick = () => new Promise((r) => setTimeout(r, 0));

// --- resume.js -----------------------------------------------------------------------------------
const NOW = 1_700_000_000_000;
const item = { trackId: 7, playlistId: 3, video_id: 'abcdefghijk', start: 30, end: 200, title: 'Song', channel: 'Chan', thumb: 'x' };
const src = { playlistId: 3, title: 'Night bus', kind: 'playlist' };

assert.equal(R.resumeKey(5), 'webjam.resume.u5');
assert.equal(R.resumeKey(null), 'webjam.resume.anon');
assert.equal(R.resumeKey('nope'), 'webjam.resume.anon');

{
  const rec = R.snapshot({ item, index: 4, source: src, time: 151.26, duration: 300, userId: 5, now: NOW });
  assert.deepEqual(rec, {
    v: 1, src: { kind: 'playlist', playlistId: 3, title: 'Night bus' }, trackId: 7, videoId: 'abcdefghijk',
    index: 4, start: 30, end: 200, title: 'Song', channel: 'Chan', pos: 151.3, at: NOW,
  });
  // Only what is needed: no thumb, no queue, no other tracks.
  assert.equal('thumb' in rec, false);
  const st = mem();
  assert.ok(R.writeResume(st, 5, rec));
  assert.deepEqual([...st.m.keys()], ['webjam.resume.u5']);
  assert.deepEqual(R.readResume(st, 5, NOW + 1000), rec);
  // Another person on the same browser sees nothing.
  assert.equal(R.readResume(st, 6, NOW), null);
  assert.equal(R.readResume(st, null, NOW), null);
  // Expiry after 7 days, and the stale record is removed.
  assert.ok(R.readResume(st, 5, NOW + R.RESUME_TTL_MS - 1));
  assert.equal(R.readResume(st, 5, NOW + R.RESUME_TTL_MS + 1), null);
  assert.equal(st.m.size, 0);
  // Clearing.
  R.writeResume(st, 5, rec);
  R.clearResume(st, 5);
  assert.equal(st.m.size, 0);
}

// Clip bounds: position below the clip start is raised; the tail of the clip counts as finished.
assert.equal(R.snapshot({ item, time: 5, now: NOW }).pos, 30);
assert.equal(R.snapshot({ item, time: 198, now: NOW }), null);
assert.equal(R.snapshot({ item: { ...item, end: null }, time: 298, duration: 300, now: NOW }), null);
assert.ok(R.snapshot({ item: { ...item, end: null }, time: 250, duration: 0, now: NOW }), 'unknown duration still saves');
assert.equal(R.resumeAt({ start: 30, end: 200, pos: 10 }), 30);
assert.equal(R.resumeAt({ start: 30, end: 200, pos: 120 }), 120);
assert.equal(R.resumeAt({ start: 30, end: 200, pos: 500 }), 197);
assert.equal(R.resumeAt({ start: null, end: null, pos: 42 }), 42);

// Crates: never stored for a signed-out visitor, never offered from the anonymous key.
{
  const crate = { playlistId: 9, title: 'Crate', kind: 'crate' };
  assert.equal(R.snapshot({ item, source: crate, time: 60, userId: null, now: NOW }), null);
  const rec = R.snapshot({ item, source: crate, time: 60, userId: 5, now: NOW });
  assert.equal(rec.src.kind, 'crate');
  const st = mem();
  assert.equal(R.writeResume(st, null, rec), false);
  st.setItem('webjam.resume.anon', JSON.stringify(rec));
  assert.equal(R.readResume(st, null, NOW), null);
}

// Junk and bad input.
for (const raw of [null, '', '{', '[]', '{"v":2}', JSON.stringify({ v: 1, videoId: 'bad', at: NOW, pos: 1 }),
  JSON.stringify({ v: 1, videoId: 'abcdefghijk', at: NOW, pos: -1 }),
  JSON.stringify({ v: 1, videoId: 'abcdefghijk', at: NOW + 10 * 60_000, pos: 1 })]) {
  assert.equal(R.parse(raw, NOW), null, String(raw));
}
assert.equal(R.snapshot({ item: { ...item, video_id: '<script>' }, time: 50, now: NOW }), null);
assert.equal(R.snapshot({ item: null }), null);
{
  const throwing = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('quota'); }, removeItem() { throw new Error('x'); } };
  assert.equal(R.readResume(throwing, 1, NOW), null);
  assert.equal(R.writeResume(throwing, 1, R.snapshot({ item, time: 50, now: NOW })), false);
  R.clearResume(throwing, 1);
}
// Long titles are capped.
assert.equal(R.snapshot({ item: { ...item, title: 'x'.repeat(5000) }, time: 50, now: NOW }).title.length, 200);

// Finding the track again in a reloaded queue.
{
  const rec = R.snapshot({ item, index: 1, source: src, time: 50, userId: 5, now: NOW });
  const q = [{ trackId: 1, video_id: 'zzzzzzzzzzz' }, { trackId: 2, video_id: 'abcdefghijk' }, { trackId: 7, video_id: 'abcdefghijk' }];
  assert.equal(R.findInQueue(rec, q), 2, 'by track id first');
  assert.equal(R.findInQueue({ ...rec, trackId: null }, q), 1, 'then by index if the video matches');
  assert.equal(R.findInQueue({ ...rec, trackId: null, index: 0 }, q), 1, 'then by video');
  assert.equal(R.findInQueue({ ...rec, trackId: 99, videoId: 'qqqqqqqqqqq' }, q), -1);
  const one = R.itemFromRecord(rec);
  assert.equal(one.video_id, 'abcdefghijk');
  assert.equal(one.start, 30);
  assert.equal(one.end, 200);
}

// --- wake-lock.js --------------------------------------------------------------------------------
function fakeNav({ reject = false } = {}) {
  const log = [];
  const nav = {
    log,
    held: 0,
    wakeLock: {
      request(type) {
        log.push(`request:${type}`);
        if (reject) return Promise.reject(new Error('NotAllowedError'));
        nav.held += 1;
        const listeners = [];
        const s = {
          released: false,
          addEventListener(ev, fn) { if (ev === 'release') listeners.push(fn); },
          release() {
            if (!s.released) { s.released = true; nav.held -= 1; log.push('release'); listeners.forEach((f) => f()); }
            return Promise.resolve();
          },
        };
        nav.last = s;
        return Promise.resolve(s);
      },
    },
  };
  return nav;
}

{
  // Unsupported: nothing happens, nothing throws.
  const w = W.createWakeLock({ nav: {}, doc: { visibilityState: 'visible' }, storage: mem(), touch: true });
  assert.equal(w.supported, false);
  await w.setPlaying(true);
  assert.equal(w.held(), false);
}
{
  // Default on for touch, off otherwise; the stored choice wins.
  assert.equal(W.createWakeLock({ nav: fakeNav(), storage: mem(), touch: true }).enabled(), true);
  assert.equal(W.createWakeLock({ nav: fakeNav(), storage: mem(), touch: false }).enabled(), false);
  const st = mem();
  st.setItem(W.WAKE_KEY, '0');
  assert.equal(W.createWakeLock({ nav: fakeNav(), storage: st, touch: true }).enabled(), false);
  const blocked = { getItem() { throw new Error('x'); }, setItem() { throw new Error('x'); } };
  const w = W.createWakeLock({ nav: fakeNav(), storage: blocked, touch: true });
  assert.equal(w.enabled(), true);
  await w.setEnabled(false); // storage throws, still flips
  assert.equal(w.enabled(), false);
}
{
  const nav = fakeNav();
  const doc = { visibilityState: 'visible' };
  const st = mem();
  const w = W.createWakeLock({ nav, doc, storage: st, touch: true });
  await w.setPlaying(true);
  assert.equal(w.held(), true);
  assert.equal(nav.held, 1);
  await w.setPlaying(true); // no second request
  assert.deepEqual(nav.log, ['request:screen']);
  await w.setPlaying(false);
  assert.equal(w.held(), false);
  assert.equal(nav.held, 0);

  // Hidden: the browser releases it; back to visible while still playing, it is requested again.
  await w.setPlaying(true);
  doc.visibilityState = 'hidden';
  nav.last.release(); // what the browser does
  await w.visibility();
  assert.equal(w.held(), false);
  doc.visibilityState = 'visible';
  await w.visibility();
  assert.equal(w.held(), true);
  assert.equal(nav.log.filter((l) => l === 'request:screen').length, 3);

  // Not re-acquired on return when paused in the meantime.
  doc.visibilityState = 'hidden';
  await w.visibility();
  await w.setPlaying(false);
  doc.visibilityState = 'visible';
  await w.visibility();
  assert.equal(w.held(), false);
  assert.equal(nav.held, 0);

  // Turning the toggle off releases and is remembered.
  await w.setPlaying(true);
  await w.setEnabled(false);
  assert.equal(w.held(), false);
  assert.equal(st.getItem(W.WAKE_KEY), '0');
  await w.setEnabled(true);
  assert.equal(w.held(), true);
  assert.equal(st.getItem(W.WAKE_KEY), '1');
  await w.setPlaying(false);
}
{
  // Paused while the request is still in flight: the late sentinel is released at once.
  const nav = fakeNav();
  const w = W.createWakeLock({ nav, doc: { visibilityState: 'visible' }, storage: mem(), touch: true });
  const p = w.setPlaying(true);
  w.setPlaying(false);
  await p;
  await tick();
  assert.equal(w.held(), false);
  assert.equal(nav.held, 0);
}
{
  // Rejections are swallowed, and a later play asks again.
  const nav = fakeNav({ reject: true });
  const w = W.createWakeLock({ nav, doc: { visibilityState: 'visible' }, storage: mem(), touch: true });
  await w.setPlaying(true);
  assert.equal(w.held(), false);
  await w.setPlaying(false);
  await w.setPlaying(true);
  assert.equal(nav.log.length, 2);
}
{
  // request() throwing synchronously is swallowed too.
  const nav = { wakeLock: { request() { throw new Error('boom'); } } };
  const w = W.createWakeLock({ nav, doc: null, storage: null, touch: true });
  await w.setPlaying(true);
  assert.equal(w.held(), false);
}

// --- media-session.js ----------------------------------------------------------------------------
{
  const meta = M.metadataFor(item, src, 'dan');
  assert.equal(meta.title, 'Song');
  assert.equal(meta.artist, 'Chan');
  assert.equal(meta.album, 'Night bus');
  assert.deepEqual(meta.artwork.map((a) => [a.src, a.sizes]), [
    ['https://i.ytimg.com/vi/abcdefghijk/mqdefault.jpg', '320x180'],
    ['https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg', '480x360'],
  ]);
  assert.equal(M.metadataFor({ ...item, channel: '' }, src, 'dan').artist, '@dan');
  assert.equal(M.metadataFor(item, { kind: 'crate', playlistId: 9, title: 'Crate' }).album, 'Your crate');
  assert.equal(M.metadataFor(item, null).album, 'webjam');
  assert.deepEqual(M.metadataFor({ ...item, video_id: 'x"y' }).artwork, []);
  assert.equal(M.metadataFor(null), null);

  assert.deepEqual(M.positionFor(item, 60, 300), { duration: 170, position: 30, playbackRate: 1 });
  assert.deepEqual(M.positionFor({ ...item, start: null, end: null }, 10, 300), { duration: 300, position: 10, playbackRate: 1 });
  assert.equal(M.positionFor({ ...item, end: null }, 10, 0), null, 'duration unknown');
  assert.equal(M.positionFor({ ...item, end: null }, 10, NaN), null);
  assert.equal(M.positionFor({ ...item, start: 50, end: 40 }, 45, 300), null);
  assert.equal(M.positionFor(item, 999, 300).position, 170, 'clamped to the clip');
  assert.equal(M.positionFor(item, 1, 300).position, 0);

  const calls = [];
  const handlers = new Map();
  const nav = { mediaSession: {
    setActionHandler(name, fn) { if (name === 'seekforward') throw new Error('unsupported'); handlers.set(name, fn); },
    setPositionState(p) { if (p && !(p.duration > 0)) throw new TypeError('bad'); calls.push(p); },
  } };
  M.wireActions(nav, {
    play: () => calls.push('play'), pause: () => calls.push('pause'), prev: () => calls.push('prev'),
    next: () => calls.push('next'), seekTo: (t) => calls.push(['to', t]), seekBy: (d) => calls.push(['by', d]),
  });
  assert.deepEqual([...handlers.keys()].sort(), ['nexttrack', 'pause', 'play', 'previoustrack', 'seekbackward', 'seekto']);
  handlers.get('play')();
  handlers.get('pause')();
  handlers.get('seekto')({ seekTime: 12 });
  handlers.get('seekto')({});
  handlers.get('seekbackward')({});
  handlers.get('seekbackward')({ seekOffset: 5 });
  assert.deepEqual(calls, ['play', 'pause', ['to', 12], ['by', -10], ['by', -5]]);
  calls.length = 0;
  M.setPosition(nav, { duration: 0, position: 0, playbackRate: 1 }); // throws inside, swallowed
  M.setPosition(nav, null);
  assert.deepEqual(calls, [undefined]);
  class Meta { constructor(o) { Object.assign(this, o); } }
  M.setMetadata(nav, Meta, M.metadataFor(item, src));
  assert.equal(nav.mediaSession.metadata.title, 'Song');
  M.setMetadata(nav, null, M.metadataFor(item, src));
  assert.equal(nav.mediaSession.metadata, null);
  M.setPlaybackState(nav, 'playing');
  assert.equal(nav.mediaSession.playbackState, 'playing');
  // No Media Session at all: quiet no-ops.
  M.wireActions({}, {});
  M.setPosition({}, null);
  M.setMetadata(undefined, Meta, {});
  M.setPlaybackState(null, 'paused');
}

console.log('player helpers ok');

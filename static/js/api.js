// The only module that calls fetch. Adds X-Webjam: 1 to every write (CSRF), user_id in demo mode
// only, turns error bodies into ApiError, and runs the serial ops queue (docs/phase1-ops-api.md §3.1).

import { state, reconcile, rollback, settle, refreshPlaylists, adoptVersions } from './store.js';
import * as slots from './slots.js';
import { toast } from './ui/toast.js';
import { signInUrl } from './format.js';

const DEMO_KEY = 'webjam_user_id';

export class ApiError extends Error {
  constructor(status, code, detail, data) {
    super(detail);
    this.name = 'ApiError';
    this.status = status;
    this.code = code || null;
    this.detail = detail; // always a string, fit for display
    this.data = data || {};
    this.handled = false; // true once a toast has been shown for it
  }
}

export function storedDemoUserId() {
  try {
    const v = Number(localStorage.getItem(DEMO_KEY));
    return Number.isSafeInteger(v) && v > 0 ? v : null;
  } catch {
    return null;
  }
}

export function storeDemoUserId(id) {
  try {
    localStorage.setItem(DEMO_KEY, String(Number(id)));
  } catch {
    /* private mode: the persona lasts for this page only */
  }
}

/** The user id to claim, in demo mode only. Google mode never sends one. Before bootstrap the
 *  mode is unknown (null); only demo mode ever writes the stored id, so sending it then is safe. */
function demoUserId() {
  if (state.auth.mode === 'google') return null;
  if (state.auth.mode === 'demo' && state.auth.user && state.auth.user.id) return state.auth.user.id;
  return storedDemoUserId();
}

function detailString(body, status) {
  const d = body && body.detail;
  if (typeof d === 'string') return d;
  if (Array.isArray(d)) {
    const parts = d.map((e) => {
      if (!e || typeof e !== 'object') return String(e);
      const loc = Array.isArray(e.loc) ? e.loc.filter((x) => x !== 'body').join('.') : '';
      return loc ? `${loc}: ${e.msg}` : String(e.msg || '');
    });
    return parts.filter(Boolean).join('; ') || 'That request was not valid.';
  }
  if (status === 429) return 'Slow down a little and try again.';
  if (status >= 500) return 'The server had a problem. Try again in a moment.';
  return `Request failed (${status}).`;
}

async function request(method, path, { query, body, quiet = false } = {}) {
  const url = new URL(path, location.origin);
  const demoId = demoUserId();
  if (query) {
    for (const [k, v] of Object.entries(query)) if (v != null && v !== '') url.searchParams.set(k, String(v));
  }
  const headers = { Accept: 'application/json' };
  const init = { method, headers, credentials: 'same-origin' };
  if (method === 'GET' || method === 'DELETE') {
    // DELETE endpoints read user_id from the query (FastAPI ignores it elsewhere).
    if (demoId && !url.searchParams.has('user_id')) url.searchParams.set('user_id', String(demoId));
  }
  if (method !== 'GET') {
    headers['X-Webjam'] = '1';
    headers['Content-Type'] = 'application/json';
    const payload = body && typeof body === 'object' && !Array.isArray(body) ? { ...body } : (body || {});
    if (demoId && payload && typeof payload === 'object' && !Array.isArray(payload) && payload.user_id == null) {
      payload.user_id = demoId;
    }
    init.body = JSON.stringify(payload);
  }
  if (url.origin !== location.origin) throw new ApiError(0, 'bad_url', 'Refusing a cross-origin request.');
  const res = await fetch(url.pathname + url.search, init);
  let data = null;
  const text = await res.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
  }
  if (!res.ok) {
    const err = new ApiError(res.status, data && data.code, detailString(data, res.status), data || {});
    if (res.status === 401 && !quiet) handleSignedOut(err, { method, path });
    throw err;
  }
  return data;
}

function handleSignedOut(err, intent) {
  err.code = err.code || 'signed_out';
  const handled = slots.call('auth.required', { ...intent, error: err });
  if (handled === undefined) {
    toast.show('Sign in to do that.', { action: { label: 'Sign in', href: signInUrl(location.pathname + location.search) } });
  }
  err.handled = true;
}

// opts.quiet: a 401 does not trigger the sign-in prompt (background calls such as play counts).
export const get = (path, query = {}, opts = {}) => request('GET', path, { query, ...opts });
export const post = (path, body = {}, opts = {}) => request('POST', path, { body, ...opts });
export const put = (path, body = {}, opts = {}) => request('PUT', path, { body, ...opts });
export const del = (path, body = {}, opts = {}) => request('DELETE', path, { body, ...opts });

// --- ids ----------------------------------------------------------------------------------------

export function newId() {
  if (globalThis.crypto && typeof crypto.randomUUID === 'function') {
    try {
      return crypto.randomUUID();
    } catch {
      /* insecure context: fall through */
    }
  }
  const b = new Uint8Array(16);
  crypto.getRandomValues(b);
  return [...b].map((x) => x.toString(16).padStart(2, '0')).join('');
}

// --- the serial queue (ops, undo, redo share it) -----------------------------------------------

let tail = Promise.resolve();
function enqueue(job) {
  const run = tail.then(job, job);
  tail = run.catch(() => {});
  return run;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const RETRY_DELAYS = [500, 1000, 2000];

async function postWithRetry(path, body) {
  for (let attempt = 0; ; attempt++) {
    try {
      return await post(path, body);
    } catch (err) {
      const network = !(err instanceof ApiError);
      if (!network || attempt >= RETRY_DELAYS.length) {
        if (network) throw new ApiError(0, 'network', 'Network problem; that change was not saved.');
        throw err;
      }
      await sleep(RETRY_DELAYS[attempt]);
    }
  }
}

function afterSuccess(res) {
  reconcile(res);
  if (res && Array.isArray(res.new_badges) && res.new_badges.length) toast.badge(res.new_badges);
}

function touchedIds(list, err) {
  const ids = new Set();
  for (const op of list || []) {
    if (op.playlist_id != null) ids.add(Number(op.playlist_id));
    if (op.to_playlist_id != null) ids.add(Number(op.to_playlist_id));
  }
  for (const c of (err && err.data && err.data.conflicts) || []) if (c.playlist_id) ids.add(Number(c.playlist_id));
  return [...ids];
}

/**
 * ops.send(list, { groupId, expectedVersions, label, token, quiet })
 * expectedVersions may be an object or a function returning one (evaluated at send time).
 * One request in flight per tab. Each call gets a fresh op_id (group_id defaults to it); network
 * failures retry with the SAME op_id. Resolves to the server response after reconcile().
 * `token` is an optimistic token from store.optimistic(); it is rolled back on failure.
 * Rejects with ApiError (err.handled is true when a toast was already shown).
 */
export const ops = {
  send(list, { groupId, expectedVersions, token, quiet = false } = {}) {
    const opId = newId();
    const body = { op_id: opId, group_id: groupId || opId, ops: list };
    return enqueue(async () => {
      // A function is read now, at send time, so earlier queued edits' version bumps are seen.
      const ev = typeof expectedVersions === 'function' ? expectedVersions() : expectedVersions;
      if (ev && Object.keys(ev).length) body.expected_versions = ev;
      try {
        const res = await postWithRetry('/api/ops', body);
        if (token != null) settle(token, res);
        afterSuccess(res);
        return res;
      } catch (err) {
        if (token != null) rollback(token);
        if (err instanceof ApiError && err.code === 'track_not_found') {
          // The row was already gone (another tab or device): show the truth instead of a ghost.
          await refreshPlaylists(touchedIds(list, err));
          if (!quiet) toast.show('That track was already gone; refreshed.');
          err.handled = true;
        } else if (err instanceof ApiError && (err.code === 'stale_version' || err.code === 'anchor_missing')) {
          adoptVersions(err.data && err.data.versions);
          await refreshPlaylists(touchedIds(list, err));
          if (!quiet) toast.show('That playlist changed elsewhere; refreshed.');
          err.handled = true;
        } else if (err instanceof ApiError && !err.handled && !quiet) {
          toast.show(err.detail);
          err.handled = true;
        }
        throw err;
      }
    });
  },
};

/** POST /api/ops/undo with the expected group_id. Shares the ops queue. Resolves to the response. */
export function undo({ skip = false, groupId } = {}) {
  return enqueue(async () => {
    const gid = groupId !== undefined ? groupId : state.undo && state.undo.undo_group_id;
    const body = {};
    if (gid) body.group_id = gid;
    if (skip) body.skip = true;
    const res = await postWithRetry('/api/ops/undo', body);
    afterSuccess(res);
    return res;
  });
}

/** POST /api/ops/redo with the expected group_id. Shares the ops queue. */
export function redo({ skip = false, groupId } = {}) {
  return enqueue(async () => {
    const gid = groupId !== undefined ? groupId : state.undo && state.undo.redo_group_id;
    const body = {};
    if (gid) body.group_id = gid;
    if (skip) body.skip = true;
    const res = await postWithRetry('/api/ops/redo', body);
    afterSuccess(res);
    return res;
  });
}

/** Run any other write through the same queue (e.g. promote), reconciling its response. */
export function queued(fn) {
  return enqueue(async () => {
    const res = await fn();
    afterSuccess(res);
    return res;
  });
}

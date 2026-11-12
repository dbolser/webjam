// pushState routing (docs/phase1b-shell.md §2.1, §2.2, §4.3).
//
//   register(name, pattern, loadView)   pattern: '/p/:ref' style string or a RegExp (named groups
//                                       become params). loadView: () => import('./views/x.js').
//                                       Re-registering a name replaces that route in place, so a
//                                       lane can swap in its own view for a route F registered.
//   navigate(path, { replace })         go to a same-origin path
//   replaceUrl(path)                    fix the address bar without a navigation (slugs)
//   refresh()                           re-run the current view's load()
//   rerender()                          re-render the current view from the store (sync)
//
// A view module exports:
//   load(ctx)?    async; fetch into the store. Throw ApiError 404 to show not-found.
//   render(ctx)   sync; returns a TemplateResult built from the store. Called again on every
//                 store change, so keep it pure.
//   title(ctx)?   document title (without " · webjam")
//   unmount()?    cleanup on leaving
// (A module that only has `default async function view(ctx) -> TemplateResult` also works; it is
//  rendered once per navigation.)
// ctx = { name, params, query, hash, path, rerender(), signal } where signal aborts on leaving.

import { html, render } from './lit.js';
import { update } from './store.js';
import { ApiError } from './api.js';
import { closePopover } from './ui/popover.js';

const routes = [];
let notFoundLoader = null;
let current = null; // { name, mod, ctx, render }
let navToken = 0;
let started = false;
let firstRender = true;
let viewEl = null;

function compile(pattern) {
  if (pattern instanceof RegExp) return { re: pattern, keys: null };
  const keys = [];
  const src = String(pattern)
    .replace(/\/+$/, '')
    .split('/')
    .map((seg) => {
      if (seg.startsWith(':')) {
        keys.push(seg.slice(1));
        return '([^/]+)';
      }
      return seg.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    })
    .join('/');
  return { re: new RegExp(`^${src || ''}/?$`), keys };
}

export function register(name, pattern, loadView) {
  if (name === 'not-found') {
    notFoundLoader = loadView;
    return;
  }
  const entry = { name, pattern, ...compile(pattern), load: loadView };
  const at = routes.findIndex((r) => r.name === name);
  if (at >= 0) routes[at] = entry;
  else routes.push(entry);
}

function match(pathname) {
  for (const r of routes) {
    const m = r.re.exec(pathname);
    if (!m) continue;
    const params = {};
    if (r.keys) r.keys.forEach((k, i) => { params[k] = decodeURIComponent(m[i + 1]); });
    else if (m.groups) Object.assign(params, m.groups);
    return { route: r, params };
  }
  return null;
}

/** The current route's { name, ctx } (or null before the first render). */
export function currentRoute() {
  return current ? { name: current.name, ctx: current.ctx } : null;
}

export function rerender() {
  if (!current || !viewEl) return;
  let tpl;
  try {
    tpl = current.render ? current.render(current.ctx) : current.tpl;
  } catch (err) {
    console.error('view render', err);
    tpl = errorView(err);
  }
  render(tpl, viewEl);
}

function errorView(err) {
  return html`<div class="page not-found">
    <h1 tabindex="-1">Something went wrong</h1>
    <p>${(err && err.detail) || (err && err.message) || 'Try again.'}</p>
    <p><a href="/">Go to your page</a></p>
  </div>`;
}

function loadingView() {
  return html`<div class="page"><p class="loading">Loading…</p></div>`;
}

async function resolveModule(loader) {
  const m = typeof loader === 'function' ? await loader() : loader;
  return m && m.default && !m.render && typeof m.default === 'object' ? m.default : m;
}

async function go(url, { pop = false, scrollY = null } = {}) {
  const token = ++navToken;
  const u = new URL(url, location.origin);
  closePopover();
  if (current && current.mod && typeof current.mod.unmount === 'function') {
    try { current.mod.unmount(current.ctx); } catch (err) { console.error(err); }
  }
  if (current && current.abort) current.abort.abort();
  const hit = match(u.pathname);
  const abort = new AbortController();
  const query = Object.fromEntries(u.searchParams.entries());
  const ctx = {
    name: hit ? hit.route.name : 'not-found',
    params: hit ? hit.params : {},
    query,
    hash: u.hash.replace(/^#/, ''),
    path: u.pathname,
    signal: abort.signal,
    rerender: () => rerender(),
  };
  update((s) => {
    s.route = { name: ctx.name, params: ctx.params, query, path: u.pathname };
  }, 'route');

  const slow = setTimeout(() => {
    if (token === navToken && viewEl) render(loadingView(), viewEl);
  }, 180);
  let mod;
  let tpl = null;
  try {
    mod = await resolveModule(hit ? hit.route.load : notFoundLoader);
    if (mod && typeof mod.load === 'function') await mod.load(ctx);
    if (mod && !mod.render && typeof mod.default === 'function') tpl = await mod.default(ctx);
  } catch (err) {
    if (token !== navToken) return;
    if (err instanceof ApiError && (err.status === 404 || err.status === 422)) {
      mod = await resolveModule(notFoundLoader);
      ctx.name = 'not-found';
    } else {
      console.error('route load', err);
      mod = { render: () => errorView(err) };
    }
  } finally {
    clearTimeout(slow);
  }
  if (token !== navToken) return;
  current = { name: ctx.name, mod, ctx, render: mod && mod.render ? mod.render : null, tpl, abort };
  rerender();
  const t = mod && typeof mod.title === 'function' ? mod.title(ctx) : '';
  document.title = t ? `${t} · webjam` : 'webjam';

  // Scroll: restore on back/forward, anchor on #hash, else top.
  requestAnimationFrame(() => {
    if (token !== navToken) return;
    const behavior = 'auto';
    if (pop && scrollY != null) window.scrollTo({ top: scrollY, behavior });
    else if (ctx.hash) {
      const el = document.getElementById(ctx.hash);
      if (el) el.scrollIntoView({ block: 'start', behavior });
    } else if (!pop) window.scrollTo({ top: 0, behavior });
    if (!firstRender) {
      const h1 = viewEl && viewEl.querySelector('h1');
      if (h1) {
        if (!h1.hasAttribute('tabindex')) h1.setAttribute('tabindex', '-1');
        h1.focus({ preventScroll: true });
      }
    }
    firstRender = false;
  });
}

export function navigate(path, { replace = false } = {}) {
  const u = new URL(path, location.origin);
  if (u.origin !== location.origin) {
    location.assign(u.toString());
    return Promise.resolve();
  }
  const target = u.pathname + u.search + u.hash;
  if (replace) history.replaceState({ scrollY: 0 }, '', target);
  else {
    saveScroll();
    history.pushState({ scrollY: 0 }, '', target);
  }
  return go(target);
}

export function replaceUrl(path) {
  const u = new URL(path, location.origin);
  if (u.origin !== location.origin) return;
  history.replaceState(history.state, '', u.pathname + u.search + u.hash);
  update((s) => {
    s.route = { ...s.route, path: u.pathname, query: Object.fromEntries(u.searchParams.entries()) };
  }, 'route');
}

export function refresh() {
  return go(location.pathname + location.search + location.hash, { pop: true, scrollY: window.scrollY });
}

function saveScroll() {
  try {
    history.replaceState({ ...(history.state || {}), scrollY: window.scrollY }, '');
  } catch {
    /* ignore */
  }
}

const SKIP_PREFIXES = ['/api/', '/auth/', '/static/', '/legacy'];

function onLinkClick(e) {
  if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  const a = e.target instanceof Element ? e.target.closest('a[href]') : null;
  if (!a || a.hasAttribute('download') || (a.target && a.target !== '_self')) return;
  if (a.getAttribute('rel') === 'external') return;
  const u = new URL(a.href, location.href);
  if (u.origin !== location.origin) return;
  if (SKIP_PREFIXES.some((p) => u.pathname.startsWith(p))) return;
  if (u.pathname === location.pathname && u.search === location.search && u.hash) return; // in-page anchor
  e.preventDefault();
  navigate(u.pathname + u.search + u.hash);
}

// --- old # links (§2.2) ------------------------------------------------------------------------

const NOTICES = {
  'not-invited': "That Google account isn't on the invite list.",
  'auth-error': "Sign-in didn't complete. Try again.",
};

/** Map a legacy hash to a path. Returns { path, notice } or null when there is nothing to rewrite. */
export async function legacyTarget(hash, { me, getUser } = {}) {
  const h = String(hash || '').replace(/^#/, '');
  if (location.pathname !== '/') return null; // only the old single page used hash routes
  if (h === '' || h === 'feed') return location.hash ? { path: '/' } : null;
  let m;
  if ((m = /^playlist\/(\d{1,18})/.exec(h))) return { path: `/p/${m[1]}` };
  if ((m = /^song\/([A-Za-z0-9_-]{11})/.exec(h))) return { path: `/t/${m[1]}` };
  if ((m = /^user\/(\d{1,18})/.exec(h))) {
    try {
      const u = getUser ? await getUser(Number(m[1])) : null;
      const name = u && (u.username || (u.user && u.user.username));
      return { path: name && /^[a-z0-9_]{3,20}$/.test(name) ? `/u/${name}` : '/' };
    } catch {
      return { path: '/' };
    }
  }
  if (h === 'songs') return { path: '/discover?tab=tracks' };
  if (h === 'curators') return { path: '/discover?tab=people' };
  if (h === 'badges') return { path: me ? `/u/${me}#badges` : '/discover' };
  if (NOTICES[h]) return { path: '/', notice: NOTICES[h] };
  return null;
}

/** Start routing: rewrite a legacy hash, wire links and history, render the first view. */
export async function start({ me, getUser } = {}) {
  if (started) return;
  started = true;
  viewEl = document.getElementById('view');
  const legacy = location.hash ? await legacyTarget(location.hash, { me, getUser }) : null;
  if (legacy) {
    history.replaceState({ scrollY: 0 }, '', legacy.path);
    if (legacy.notice) update((s) => { s.ui.notice = legacy.notice; }, 'notice');
  }
  if ('scrollRestoration' in history) history.scrollRestoration = 'manual';
  document.addEventListener('click', onLinkClick);
  window.addEventListener('popstate', (e) => {
    const y = e.state && typeof e.state.scrollY === 'number' ? e.state.scrollY : 0;
    go(location.pathname + location.search + location.hash, { pop: true, scrollY: y });
  });
  let pending = false;
  window.addEventListener('scroll', () => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => {
      pending = false;
      saveScroll();
    });
  }, { passive: true });
  await go(location.pathname + location.search + location.hash);
}


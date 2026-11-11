// The persistent shell around the view: top bar, dock, right rail, crate panel, player bar.
// renderShell() redraws everything from the store; renderTick() only the player bar.

import { html, render, nothing } from './lit.js';
import { state } from './store.js';
import * as slots from './slots.js';
import { rerender as rerenderView } from './router.js';
import { renderDock } from './panels/dock.js';
import { renderCratePanel } from './panels/crate.js';
import { renderBar, current } from './player/player.js';
import { avatar } from './components/avatar.js';
import { userUrl, trackUrl, playlistUrl, iconHref } from './format.js';
import { themeLabel } from './theme.js';

function accountFallback() {
  const a = state.auth;
  if (!a.signedIn) {
    if (a.mode === 'demo') return nothing;
    return html`<a href="/auth/google/login" rel="external">Sign in</a>`;
  }
  const u = a.user;
  return html`<a class="me" href=${userUrl(u.username)} aria-label="Your profile, @${u.username}">${avatar(u)}<span class="me-name">@${u.username}</span></a>`;
}

function renderTopbar() {
  const el = document.getElementById('topbar');
  if (!el) return;
  const r = state.route;
  const q = r.name === 'search' ? r.query.q || '' : '';
  const theme = state.ui.theme;
  const icon = theme === 'light' ? 'sun' : theme === 'dark' ? 'moon' : 'system';
  const account = slots.render('topbar.account', {});
  render(html`
    <a class="wordmark" href="/" aria-label="webjam">webjam</a>
    <form class="omnibox" role="search" data-action="omnibox-submit">
      <svg class="i" aria-hidden="true"><use href="#i-search"></use></svg>
      <input id="omnibox" type="search" name="q" placeholder="Search or paste a link…" autocomplete="off"
        spellcheck="false" aria-label="Search or paste a link" .value=${q} />
      <kbd aria-hidden="true">/</kbd>
    </form>
    <nav class="topbar-nav" aria-label="Site">
      <a class="discover-link" href="/discover" aria-current=${r.name === 'discover' ? 'page' : nothing}>Discover</a>
      <button type="button" class="icon-btn" data-action="theme-cycle" aria-label=${themeLabel()} title=${themeLabel()}>
        <svg class="i"><use href=${iconHref(icon)}></use></svg>
      </button>
      <div class="account">${account.length ? account : accountFallback()}</div>
    </nav>`, el);
  const strip = document.getElementById('demo-strip');
  if (strip) strip.hidden = state.auth.mode !== 'demo';
}

function renderRail() {
  const el = document.getElementById('rail-context');
  if (!el) return;
  const item = current();
  const p = state.player;
  const src = p.source;
  const upNext = item ? p.queue.slice(p.index + 1, p.index + 6) : [];
  const extra = slots.render('rail.context', { route: state.route });
  render(html`
    ${item
      ? html`<div class="rail-now">
          <p class="label">Now playing</p>
          <a class="t" href=${trackUrl(item.video_id, item.start, item.end)}>${item.title}</a>
          <p class="c">${item.channel}${src && src.playlistId && src.kind !== 'crate' && src.title
            ? html` · <a class="hop" href=${playlistUrl(src.playlistId, src.title)}>‘${src.title}’</a>` : nothing}</p>
        </div>
        ${upNext.length
          ? html`<div class="section"><p class="label">Up next</p>
              <ol class="lines">${upNext.map((it) => html`<li class="line"><span class="line-text">
                <a href=${trackUrl(it.video_id, it.start, it.end)}>${it.title}</a></span></li>`)}</ol></div>`
          : nothing}`
      : html`<p class="rail-empty">Press play on any track. The video shows here.</p>`}
    ${extra.length ? html`<div class="section">${extra}</div>` : nothing}
  `, el);
}

export function renderShell() {
  renderTopbar();
  renderDock();
  renderCratePanel();
  renderRail();
  renderBar();
  rerenderView();
}

export function renderTick() {
  renderBar();
}

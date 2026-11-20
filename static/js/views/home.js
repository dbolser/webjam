// My page (signed in, "/"): my playlists first, then the crate, then the discovery sections
// (docs/phase1b-shell.md §2.4). One call: GET /api/me/home (plus /api/discover for "Start here"
// when I have no playlists yet).

import { html, nothing, repeat } from '../lit.js';
import { state, update } from '../store.js';
import * as api from '../api.js';
import * as slots from '../slots.js';
import { playlistCard } from '../components/playlist-card.js';
import { trackList, compactList } from '../components/track-row.js';
import { avatar } from '../components/avatar.js';
import { empty } from '../components/empty.js';
import { newPlaylistForm } from '../components/new-playlist.js';
import { playlistUrl, trackUrl, userUrl, relTime, isoTs } from '../format.js';

let startHere = null; // discover cards for a new user with no playlists

export async function load() {
  const home = await api.get('/api/me/home');
  startHere = null;
  if (!home.playlists || !home.playlists.length) {
    const d = await api.get('/api/discover', { tab: 'playlists', sort: 'popular', limit: 6 }, { quiet: true }).catch(() => null);
    startHere = (d && d.playlists) || [];
  }
  update((s) => {
    s.home = home;
    if (home.dock) s.dock = home.dock;
    if (home.undo) s.undo = home.undo;
  }, 'home');
}

export function title() {
  return 'Your page';
}

function crateSection(home) {
  const crate = home.crate || { track_count: 0, recent: [] };
  const n = Number(crate.track_count) || 0;
  const recent = (crate.recent || []).map((t) => ({ ...t, playlist_id: crate.id }));
  return html`<section class="section" aria-labelledby="h-crate">
    <h2 class="label" id="h-crate">Crate${n ? html` · <span class="num">${n}</span>` : nothing}</h2>
    ${n
      ? html`${compactList(recent, { key: 'home:crate', label: 'Newest in your crate',
          source: { playlistId: crate.id, title: 'Crate', kind: 'crate', preview: true } })}
        <p class="more-row"><button type="button" class="link-btn" data-action="open-crate">Open crate</button></p>`
      : html`<p class="quiet-line">Paste a YouTube link anywhere to start collecting.</p>`}
  </section>`;
}

function freshRows(home) {
  return (home.fresh || []).map((f) => ({
    ...f.track,
    playlist_id: f.playlist && f.playlist.id,
    source_username: f.user && f.user.username,
    source_playlist_id: f.playlist && f.playlist.id,
    source_playlist_title: f.playlist && f.playlist.title,
    playlist_count: f.playlist_count,
  }));
}

function peopleTpl(neighbours) {
  return html`<ul class="people">
    ${neighbours.map((n) => html`<li class="person">
      ${avatar(n.user, { small: true })}
      <a href=${userUrl(n.user.username)}>@${n.user.username}</a>
      <span class="count">${n.shared_count} shared</span>
    </li>`)}
  </ul>`;
}

function aboutLine(a) {
  const who = a.user ? html`<a href=${userUrl(a.user.username)}>@${a.user.username}</a>` : 'Someone';
  const pl = a.playlist ? html`<a href=${playlistUrl(a.playlist.id, a.playlist.title)}>‘${a.playlist.title}’</a>` : nothing;
  const text = a.type === 'grab'
    ? html`${who} took <a href=${trackUrl(a.video_id)}>‘${a.title}’</a> into ${pl}`
    : html`${who} on ${pl}: <span class="ink-2">${String(a.body || '').slice(0, 140)}</span>`;
  return html`<li class="line"><span class="line-text">${text}</span><time datetime=${isoTs(a.at)}>${relTime(a.at)}</time></li>`;
}

export function activityLine(a) {
  const who = a.username ? html`<a href=${userUrl(a.username)}>@${a.username}</a>` : nothing;
  let target = html`${a.summary}`;
  if (a.link_type === 'playlist' && a.link_id) target = html`<a class="hop" href=${playlistUrl(a.link_id)}>${a.summary}</a>`;
  else if (a.link_type === 'song' && a.link_id) target = html`<a class="hop" href=${trackUrl(a.link_id)}>${a.summary}</a>`;
  return html`<li class="line"><span class="line-text">${who} ${target}</span>
    <time datetime=${isoTs(a.created_at)}>${relTime(a.created_at)}</time></li>`;
}

export function render(ctx) {
  const home = state.home;
  if (!home) return html`<div class="page"><p class="loading">Loading…</p></div>`;
  const me = home.user || state.auth.user || {};
  const playlists = home.playlists || [];
  const hasAny = playlists.length > 0;
  const fresh = freshRows(home);
  const neighbours = home.neighbours || [];
  const about = home.about_you || [];
  const activity = home.activity || [];
  const notice = state.ui.notice;
  return html`<div class="page home">
    ${notice ? html`<p class="notice" role="status">${notice}</p>` : nothing}
    ${slots.render('home.top', { ...ctx, home })}

    <div class="home-title">
      <h1 tabindex="-1">${me.display_name || me.username || 'Your page'}</h1>
      <button type="button" class="btn btn-quiet" data-action="new-playlist" data-where="home">
        <svg class="i"><use href="#i-plus"></use></svg>New playlist</button>
    </div>

    <section class="section" aria-labelledby="h-yours">
      <h2 class="label" id="h-yours">Your playlists${hasAny ? html` · <span class="num">${playlists.length}</span>` : nothing}</h2>
      ${state.ui.newPlaylist === 'home' ? newPlaylistForm('home') : nothing}
      ${hasAny
        ? html`<div class="cards">${repeat(playlists, (p) => p.id, (p) => playlistCard(p, { showCurator: false, showVisibility: true }))}</div>`
        : empty('No playlists yet.', html`
            <button type="button" class="btn btn-primary" data-action="new-playlist" data-where="home">New playlist</button>
            ${slots.has('import.open')
              ? html`<button type="button" class="btn" data-action="import-open">Import from YouTube</button>` : nothing}`)}
    </section>

    ${crateSection(home)}

    ${!hasAny && startHere && startHere.length
      ? html`<section class="section" aria-labelledby="h-start">
          <h2 class="label" id="h-start">Start here</h2>
          <div class="cards">${repeat(startHere, (p) => p.id, (p) => playlistCard(p))}</div>
        </section>`
      : nothing}

    ${fresh.length
      ? html`<section class="section" aria-labelledby="h-fresh">
          <h2 class="label" id="h-fresh">From people who share your taste</h2>
          ${trackList(fresh, { key: 'home:fresh', context: 'home', label: 'From people who share your taste' })}
        </section>`
      : nothing}

    ${neighbours.length
      ? html`<section class="section" aria-labelledby="h-nb">
          <h2 class="label" id="h-nb">Taste neighbours</h2>
          ${peopleTpl(neighbours)}
        </section>`
      : nothing}

    ${about.length
      ? html`<section class="section" aria-labelledby="h-about">
          <h2 class="label" id="h-about">About you</h2>
          <ul class="lines">${about.map(aboutLine)}</ul>
        </section>`
      : nothing}

    ${activity.length
      ? html`<section class="section" aria-labelledby="h-act">
          <h2 class="label" id="h-act">${neighbours.length ? 'Activity' : 'Around webjam'}</h2>
          <ul class="lines">${activity.map(activityLine)}</ul>
        </section>`
      : nothing}

  </div>`;
}

export function unmount() {
  if (state.ui.notice || state.ui.newPlaylist === 'home') {
    update((s) => {
      s.ui.notice = null;
      if (s.ui.newPlaylist === 'home') s.ui.newPlaylist = null;
    }, 'home-unmount');
  }
}

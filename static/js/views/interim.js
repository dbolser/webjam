// Interim track, profile and search pages until Lane B registers its own views for the
// 'track', 'profile' and 'search' routes (router.register with the same name replaces these).
// Deliberately small: enough that every hop link lands somewhere useful.

import { html, nothing } from '../lit.js';
import * as api from '../api.js';
import { trackList } from '../components/track-row.js';
import { playlistCard } from '../components/playlist-card.js';
import { avatar } from '../components/avatar.js';
import { empty } from '../components/empty.js';
import { playlistUrl, userUrl, plural, thumbSrc } from '../format.js';

let data = null;

export async function load(ctx) {
  data = null;
  if (ctx.name === 'track') {
    data = { kind: 'track', song: await api.get(`/api/songs/${encodeURIComponent(ctx.params.vid)}`) };
  } else if (ctx.name === 'profile') {
    const user = await api.get(`/api/users/by-handle/${encodeURIComponent(ctx.params.handle)}`);
    data = { kind: 'profile', user };
  } else {
    data = { kind: 'search', q: ctx.query.q || '' };
  }
}

export function title() {
  if (!data) return '';
  if (data.kind === 'track') return data.song.title;
  if (data.kind === 'profile') return (data.user.user || data.user).display_name || '';
  return 'Search';
}

function trackPage(ctx) {
  const s = data.song;
  const s0 = ctx.query.s != null ? Number(ctx.query.s) : null;
  const e0 = ctx.query.e != null ? Number(ctx.query.e) : null;
  const row = { id: `t:${s.video_id}`, video_id: s.video_id, title: s.title, channel_title: s.channel_title,
    thumbnail_url: s.thumbnail_url, start_seconds: s0, end_seconds: e0 };
  const apps = s.appearances || [];
  const curators = s.curators || [];
  return html`<div class="page">
    <div class="page-head">
      <div>
        <img class="thumb" src=${thumbSrc(s.thumbnail_url, s.video_id)} alt="" />
        <h1 tabindex="-1">${s.title}</h1>
        <p class="byline">${s.channel_title}</p>
      </div>
    </div>
    <section class="section">${trackList([row], { key: `track:${s.video_id}`, context: 'track-page', label: s.title })}</section>
    ${curators.length
      ? html`<p class="byline">Curated by ${curators.map((c, i) => html`${i ? ', ' : ''}<a class="hop" href=${userUrl(c.user.username)}>@${c.user.username}</a>`)}</p>`
      : nothing}
    <section class="section" aria-labelledby="h-in">
      <h2 class="label" id="h-in">In ${plural(s.playlist_count ?? apps.length, 'playlist')}</h2>
      ${apps.length
        ? html`<ul class="lines">${apps.map((a) => html`<li class="line"><span class="line-text">
            <a href=${playlistUrl(a.playlist_id, a.playlist_title, { track: a.track_entry_id })}>${a.playlist_title}</a>
            <span class="muted"> by </span><a class="hop" href=${userUrl(a.username)}>@${a.username}</a>
            ${a.track_note ? html`<span class="ink-2"> · ${a.track_note}</span>` : nothing}</span>
            <span class="num muted">#${a.position}</span></li>`)}</ul>`
        : empty('Not in any public playlist.')}
    </section>
  </div>`;
}

function profilePage() {
  const u = data.user.user || data.user;
  const playlists = (data.user.playlists || []).filter((p) => p.kind !== 'crate');
  const neighbours = data.user.neighbours || [];
  const shared = data.user.shared_with_viewer;
  return html`<div class="page">
    <div class="home-title"><h1 tabindex="-1">${u.display_name || u.username}</h1></div>
    <p class="byline">${avatar(u, { small: true })} @${u.username}</p>
    ${u.bio ? html`<p class="desc">${u.bio}</p>` : nothing}
    ${shared && shared.shared_count ? html`<p class="quiet-line">You share ${plural(shared.shared_count, 'track')}.</p>` : nothing}
    <section class="section" aria-labelledby="h-pl">
      <h2 class="label" id="h-pl">Playlists</h2>
      ${playlists.length
        ? html`<div class="cards">${playlists.map((p) => playlistCard({ ...p, user: { username: u.username } }, { showCurator: false }))}</div>`
        : empty('No public playlists yet.')}
    </section>
    ${neighbours.length
      ? html`<section class="section" aria-labelledby="h-nb">
          <h2 class="label" id="h-nb">Shares tracks with</h2>
          <ul class="people">${neighbours.map((n) => html`<li class="person">${avatar(n.user, { small: true })}
            <a href=${userUrl(n.user.username)}>@${n.user.username}</a>
            <span class="count">${n.shared_count} shared</span></li>`)}</ul>
        </section>`
      : nothing}
  </div>`;
}

export function render(ctx) {
  if (!data) return html`<div class="page"><p class="loading">Loading…</p></div>`;
  if (data.kind === 'track') return trackPage(ctx);
  if (data.kind === 'profile') return profilePage();
  return html`<div class="page">
    <h1 tabindex="-1">Search</h1>
    <p class="intro">Search is on its way. Meanwhile, <a href="/discover">discover playlists</a>.</p>
  </div>`;
}

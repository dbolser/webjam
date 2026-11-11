// A curator's page (/u/{handle}, §2.4): who they are, their playlists, the people whose taste
// overlaps theirs, what they found first and loved, whom they took tracks from and inspired, a
// guestbook, and (last, quietly) their badges. GET /api/users/by-handle/{handle}; an old handle
// resolves to the current one and the address bar is fixed.

import { html, nothing, repeat } from '../lit.js';
import { state, update } from '../store.js';
import * as api from '../api.js';
import { registerActions } from '../actions.js';
import { replaceUrl } from '../router.js';
import { trackList, compactList } from '../components/track-row.js';
import { playlistCard } from '../components/playlist-card.js';
import { avatar } from '../components/avatar.js';
import { empty } from '../components/empty.js';
import { openSheet, closeSheet } from '../ui/sheet.js';
import { toast } from '../ui/toast.js';
import { userUrl, playlistUrl, plural } from '../format.js';
import { commentsSection } from '../discovery/comments.js';
import { badgesSection, resetBadges } from '../discovery/badges.js';
import { redraw, monthYear } from '../discovery/util.js';

const CAP = 8; // rows shown before "Show all"
const cache = new Map(); // lowercased handle -> profile payload
const open = new Set(); // expanded sections: 'shared', 'discovered', 'loved'

function key(handle) {
  return String(handle || '').toLowerCase();
}

export async function load(ctx) {
  const h = key(ctx.params.handle);
  const data = await api.get(`/api/users/by-handle/${encodeURIComponent(h)}`);
  const name = data.user && data.user.username;
  cache.set(h, data);
  if (name && key(name) !== h) cache.set(key(name), data);
  if (cache.size > 16) cache.delete(cache.keys().next().value);
  open.clear();
  resetBadges();
  if (name && location.pathname !== userUrl(name)) {
    replaceUrl(userUrl(name) + location.search + location.hash);
  }
}

export function title(ctx) {
  const d = cache.get(key(ctx.params.handle));
  const u = d && d.user;
  return u ? (u.display_name || `@${u.username}`) : 'Curator';
}

function isOwner(u) {
  return !!(state.auth.signedIn && state.auth.user && u && state.auth.user.id === u.id);
}

function sharedLine(d) {
  const s = d.shared_with_viewer;
  if (!s || !(Number(s.shared_count) > 0)) return nothing;
  const sample = (s.sample || []).map((t, i) => ({ ...t, id: t.id ?? `s:${t.video_id}:${i}` }));
  const isOpen = open.has('shared');
  return html`<div class="dc-shared">
    <button type="button" class="link-btn" data-action="dc-profile-toggle" data-section="shared"
      aria-expanded=${isOpen ? 'true' : 'false'}>You share ${plural(s.shared_count, 'track')}</button>
    ${isOpen && sample.length
      ? compactList(sample, { key: `shared:${d.user.id}`, label: 'Tracks you share' })
      : nothing}
  </div>`;
}

function capped(rows, section) {
  return open.has(section) ? rows : rows.slice(0, CAP);
}

function showAll(rows, section) {
  if (rows.length <= CAP) return nothing;
  const isOpen = open.has(section);
  return html`<p class="more-row"><button type="button" class="link-btn" data-action="dc-profile-toggle"
    data-section=${section} aria-expanded=${isOpen ? 'true' : 'false'}>${isOpen ? 'Show fewer' : `Show all ${rows.length}`}</button></p>`;
}

function uniqueBy(list, nameKey) {
  const counts = new Map();
  for (const g of list || []) {
    const n = g[nameKey];
    if (!n) continue;
    counts.set(n, (counts.get(n) || 0) + 1);
  }
  return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

function handles(pairs) {
  const shown = pairs.slice(0, 8);
  const rest = pairs.length - shown.length;
  return html`${shown.map(([n, c], i) => html`${i ? ', ' : ''}<a href=${userUrl(n)}>@${n}</a>${c > 1 ? html`<span class="num muted"> ${c}</span>` : nothing}`)}${rest > 0 ? html` and ${plural(rest, 'other')}` : nothing}`;
}

function lineage(d, u) {
  const took = uniqueBy(d.grabs_by_user, 'source_username').filter(([n]) => n !== u.username);
  const inspired = uniqueBy(d.grabs_from_user, 'taker_username').filter(([n]) => n !== u.username);
  if (!took.length && !inspired.length) return nothing;
  return html`<section class="section" aria-labelledby="dc-h-lin">
    <h2 class="label" id="dc-h-lin">Lineage</h2>
    <ul class="lines">
      ${took.length ? html`<li class="line"><span class="line-text">Took tracks from ${handles(took)}</span></li>` : nothing}
      ${inspired.length ? html`<li class="line"><span class="line-text">Inspired ${handles(inspired)}</span></li>` : nothing}
    </ul>
  </section>`;
}

function songRows(list, prefix, extra = () => ({})) {
  return (list || []).filter((s) => s && s.video_id).map((s) => ({
    id: `${prefix}:${s.video_id}`,
    video_id: s.video_id,
    title: s.title,
    channel_title: s.channel_title,
    thumbnail_url: s.thumbnail_url,
    playlist_count: s.playlist_count,
    ...extra(s),
  }));
}

function editProfile(u) {
  openSheet({
    title: 'Edit profile',
    label: 'Edit profile',
    body: () => html`<form class="dc-edit" data-action="dc-profile-save" data-user-id=${u.id}>
      <label class="dc-field"><span>Name</span>
        <input class="input" name="display_name" maxlength="200" required autocomplete="name" .value=${u.display_name || ''} /></label>
      <label class="dc-field"><span>About</span>
        <textarea class="input dc-textarea" name="bio" rows="4" maxlength="5000"
          placeholder="What do you collect?" .value=${u.bio || ''}></textarea></label>
      <p class="quiet-line">Your handle is @${u.username}.</p>
      <div class="dc-edit-actions">
        <button type="button" class="btn btn-quiet" data-action="sheet-close">Cancel</button>
        <button type="submit" class="btn btn-primary">Save</button>
      </div>
    </form>`,
  });
}

export function render(ctx) {
  const d = cache.get(key(ctx.params.handle));
  if (!d || !d.user) return html`<div class="page"><p class="loading">Loading…</p></div>`;
  const u = d.user;
  const owner = isOwner(u);
  const playlists = (d.playlists || []).filter((p) => p.kind !== 'crate');
  const neighbours = d.neighbours || [];
  const discovered = songRows(d.discovered_songs, 'f');
  const loved = songRows(d.liked_songs, 'l', (s) => ({ found_by: s.finder_username || null }));
  const lovedPlaylists = (d.liked_playlists || []).filter((p) => p && p.id);
  const since = monthYear(u.created_at);
  return html`<div class="page dc-profile">
    <header class="dc-profile-head">
      <div class="dc-profile-name">
        ${avatar(u)}
        <h1 tabindex="-1">${u.display_name || `@${u.username}`}</h1>
        ${owner
          ? html`<button type="button" class="btn btn-quiet dc-edit-btn" data-action="dc-profile-edit">Edit profile</button>`
          : nothing}
      </div>
      <p class="byline">
        <span>@${u.username}</span>
        <span class="dot-sep num muted">${plural(playlists.length, 'playlist')}${since ? ` · since ${since}` : ''}</span>
      </p>
      ${u.bio ? html`<p class="desc">${u.bio}</p>` : nothing}
      ${owner ? nothing : sharedLine(d)}
    </header>

    <section class="section" aria-labelledby="dc-h-pl">
      <h2 class="label" id="dc-h-pl">Playlists</h2>
      ${playlists.length
        ? html`<div class="cards">${repeat(playlists, (p) => p.id, (p) => playlistCard(
            { ...p, user: p.user || { id: u.id, username: u.username } },
            { showCurator: false, showVisibility: owner },
          ))}</div>`
        : empty(owner ? 'No playlists yet.' : 'No public playlists yet.')}
    </section>

    ${neighbours.length
      ? html`<section class="section" aria-labelledby="dc-h-nb">
          <h2 class="label" id="dc-h-nb">Taste neighbours</h2>
          <ul class="people">${neighbours.map((n) => html`<li class="person">
            ${avatar(n.user, { small: true })}
            <a href=${userUrl(n.user.username)}>@${n.user.username}</a>
            <span class="count">${n.shared_count} shared</span>
          </li>`)}</ul>
        </section>`
      : nothing}

    ${discovered.length
      ? html`<section class="section" aria-labelledby="dc-h-disc">
          <h2 class="label" id="dc-h-disc">Found first · <span class="num">${discovered.length}</span></h2>
          ${trackList(capped(discovered, 'discovered'), { key: `found:${u.id}`, context: 'search', label: `Found first by @${u.username}` })}
          ${showAll(discovered, 'discovered')}
        </section>`
      : nothing}

    ${loved.length || lovedPlaylists.length
      ? html`<section class="section" aria-labelledby="dc-h-loved">
          <h2 class="label" id="dc-h-loved">Loved</h2>
          ${loved.length
            ? html`${trackList(capped(loved, 'loved'), { key: `loved:${u.id}`, context: 'search', label: `Loved by @${u.username}` })}
              ${showAll(loved, 'loved')}`
            : nothing}
          ${lovedPlaylists.length
            ? html`<ul class="lines dc-loved-pl">${lovedPlaylists.map((p) => html`<li class="line"><span class="line-text">
                <a href=${playlistUrl(p.id, p.title)}>${p.title}</a>
                ${p.username && p.username !== u.username ? html` <a class="hop" href=${userUrl(p.username)}>by @${p.username}</a>` : nothing}</span>
                <span class="num muted">${plural(p.track_count, 'track')}</span></li>`)}</ul>`
            : nothing}
        </section>`
      : nothing}

    ${lineage(d, u)}

    ${commentsSection({ type: 'profile', id: u.id, comments: d.comments, hasMore: d.comments_has_more, heading: 'Guestbook', emptyText: 'Nobody has signed yet.' })}

    ${badgesSection(d)}
  </div>`;
}

export function unmount() {
  open.clear();
}

registerActions({
  'dc-profile-toggle': (el) => {
    const s = el.dataset.section;
    if (open.has(s)) open.delete(s);
    else open.add(s);
    redraw('dc-profile');
  },
  'dc-profile-edit': () => {
    const h = state.route && state.route.params && key(state.route.params.handle);
    const d = h && cache.get(h);
    if (d && isOwner(d.user)) editProfile(d.user);
  },
  'dc-profile-save': async (form) => {
    const id = Number(form.dataset.userId);
    const name = form.querySelector('[name="display_name"]');
    const bio = form.querySelector('[name="bio"]');
    const display_name = (name && name.value.trim()) || '';
    if (!display_name) {
      if (name) name.focus();
      return;
    }
    if (form.dataset.busy || !(id > 0)) return;
    form.dataset.busy = '1';
    try {
      await api.put(`/api/users/${id}`, { display_name, bio: (bio && bio.value.trim()) || '' });
      for (const d of cache.values()) {
        if (d.user && d.user.id === id) d.user = { ...d.user, display_name, bio: (bio && bio.value.trim()) || '' };
      }
      update((s) => {
        if (s.auth.user && s.auth.user.id === id) s.auth.user = { ...s.auth.user, display_name };
        if (s.home && s.home.user && s.home.user.id === id) s.home = { ...s.home, user: { ...s.home.user, display_name } };
      }, 'profile');
      closeSheet();
      toast.show('Profile saved');
    } catch (err) {
      if (!err.handled) toast.show(err.detail || 'Could not save your profile.');
    } finally {
      delete form.dataset.busy;
    }
  },
});

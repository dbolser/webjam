// Playlist cards (§1.5): hairlines, no boxes. Title, a mono meta line, curator, a 2-line
// description, then up to three preview tracks.
//
//   playlistCard(card, { showCurator, showVisibility })
// card is a PlaylistCard (§3.1). Fields that an older endpoint lacks (preview, related_count)
// are simply left out.

import { html, nothing } from '../lit.js';
import { playlistUrl, userUrl, relTime, plural, isoTs } from '../format.js';
import { compactList } from './track-row.js';

export function playlistCard(card, { showCurator = true, showVisibility = false } = {}) {
  const user = card.user || (card.username ? { username: card.username } : null);
  const preview = Array.isArray(card.preview) ? card.preview.slice(0, 3).map((t) => ({ ...t, playlist_id: card.id })) : [];
  const related = Number(card.related_count) || 0;
  return html`<article class="card" data-card-playlist-id=${card.id}>
    <div class="card-head">
      <a class="card-title" href=${playlistUrl(card.id, card.title)}>${card.title}</a>
      ${showVisibility && card.visibility && card.visibility !== 'public'
        ? html`<span class="tag">${card.visibility}</span>` : nothing}
      <button type="button" class="icon-btn" data-action="play-playlist" data-playlist-id=${card.id}
        aria-label="Play ${card.title}">
        <svg class="i"><use href="#i-play"></use></svg>
      </button>
    </div>
    <div class="card-meta">
      ${showCurator && user && user.username
        ? html`<a href=${userUrl(user.username)}>by @${user.username}</a>` : nothing}
      <span>${plural(card.track_count, 'track')}</span>
      ${related ? html`<a href=${playlistUrl(card.id, card.title, { hash: 'related' })}>${related} related</a>` : nothing}
      ${card.updated_at ? html`<time datetime=${isoTs(card.updated_at)}>updated ${relTime(card.updated_at)}</time>` : nothing}
    </div>
    ${card.description ? html`<p class="card-desc">${card.description}</p>` : nothing}
    ${preview.length
      ? compactList(preview, {
        key: `card:${card.id}`,
        label: `From ${card.title}`,
        source: { playlistId: card.id, title: card.title, kind: card.kind || 'playlist', preview: true },
      })
      : nothing}
  </article>`;
}

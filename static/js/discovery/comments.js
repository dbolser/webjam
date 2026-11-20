// Comments (§4.4 B) on playlists, tracks and profiles: oldest first, "Older" pages with before_id,
// a plain textarea to post. Never on crates (the playlist view does not render playlist.footer for
// a crate, and the server refuses crate targets anyway).
//
//   commentsSection({ type, id, comments, hasMore, heading, empty })
//   type: 'playlist' | 'song' | 'profile'; comments/hasMore are the page payload's first page.

import { html, nothing, repeat } from '../lit.js';
import { state } from '../store.js';
import * as api from '../api.js';
import * as slots from '../slots.js';
import { registerActions } from '../actions.js';
import { toast } from '../ui/toast.js';
import { avatar } from '../components/avatar.js';
import { userUrl, relTime, isoTs, signInUrl, localUrl } from '../format.js';
import { redraw, askSignIn, demoId } from './util.js';

const MAX = 2000;
const threads = new Map(); // "playlist:12" -> { comments, hasMore, src, older: bool, posting: bool }

function threadFor(type, id, comments, hasMore) {
  const k = `${type}:${id}`;
  let t = threads.get(k);
  // A new payload array means the page was (re)loaded from the server: it wins.
  if (!t || (comments && t.src !== comments && !t.posting && !t.older)) {
    t = { comments: (comments || []).slice(), hasMore: !!hasMore, src: comments, older: false, posting: false };
    threads.set(k, t);
  }
  return t;
}

function commentTpl(c) {
  const user = { username: c.author_username, display_name: c.author_display_name, avatar_color: c.author_avatar_color };
  return html`<li class="dc-comment">
    ${avatar(user, { small: true })}
    <div class="dc-comment-main">
      <p class="dc-comment-by">
        <a href=${userUrl(c.author_username)}>@${c.author_username}</a>
        <time datetime=${isoTs(c.created_at)}>${relTime(c.created_at)}</time>
      </p>
      <p class="dc-comment-body">${c.body}</p>
    </div>
  </li>`;
}

function formTpl(type, id, t) {
  if (!state.auth.signedIn) {
    return state.auth.mode === 'demo'
      ? html`<p class="quiet-line">Pick a persona to leave a comment.</p>`
      : html`<p class="quiet-line"><a href=${localUrl(signInUrl(location.pathname + location.search))}>Sign in</a> to leave a comment.</p>`;
  }
  return html`<form class="dc-comment-form" data-action="comment-submit" data-target-type=${type} data-target-id=${String(id)}>
    <label class="visually-hidden" for=${`dc-c-${type}-${id}`}>Add a comment</label>
    <textarea id=${`dc-c-${type}-${id}`} class="input dc-textarea" name="body" rows="2" maxlength=${MAX}
      placeholder=${type === 'profile' ? 'Sign the guestbook' : 'Add a comment'} ?disabled=${t.posting}></textarea>
    <div class="dc-comment-actions">
      <button type="submit" class="btn" ?disabled=${t.posting}>${t.posting ? 'Posting…' : 'Post'}</button>
    </div>
  </form>`;
}

export function commentsSection({ type, id, comments, hasMore, heading = 'Comments', emptyText = 'No comments yet.' }) {
  if (id == null || id === '') return nothing;
  const t = threadFor(type, id, comments, hasMore);
  const hid = `dc-h-c-${type}`;
  return html`<section class="section dc-comments" aria-labelledby=${hid} id=${type === 'profile' ? 'guestbook' : 'comments'}>
    <h2 class="label" id=${hid}>${heading}${t.comments.length ? html` · <span class="num">${t.comments.length}${t.hasMore ? '+' : ''}</span>` : nothing}</h2>
    ${t.hasMore
      ? html`<p class="dc-older"><button type="button" class="link-btn" data-action="comment-older"
          data-target-type=${type} data-target-id=${String(id)} ?disabled=${t.older}>${t.older ? 'Loading…' : 'Older comments'}</button></p>`
      : nothing}
    ${t.comments.length
      ? html`<ol class="dc-comment-list">${repeat(t.comments, (c) => c.id, commentTpl)}</ol>`
      : html`<p class="quiet-line">${emptyText}</p>`}
    ${formTpl(type, id, t)}
  </section>`;
}

registerActions({
  'comment-submit': async (form) => {
    const type = form.dataset.targetType;
    const id = form.dataset.targetId;
    const k = `${type}:${id}`;
    const t = threads.get(k);
    const field = form.querySelector('textarea[name="body"]');
    const body = (field && field.value.trim()) || '';
    if (!t || t.posting) return;
    if (!body) {
      if (field) field.focus();
      return;
    }
    if (!state.auth.signedIn) {
      askSignIn({ action: 'comment' }, 'comment');
      return;
    }
    t.posting = true;
    redraw('dc-comment');
    try {
      const res = await api.queued(() => api.post('/api/comments', {
        target_type: type, target_id: String(id), body, author_user_id: demoId(),
      }));
      t.comments = res.comments || [];
      t.hasMore = !!res.comments_has_more;
      if (field) field.value = '';
    } catch (err) {
      if (!err.handled) toast.show(err.detail || 'That comment did not post.');
    } finally {
      t.posting = false;
      redraw('dc-comment');
    }
  },
  'comment-older': async (el) => {
    const type = el.dataset.targetType;
    const id = el.dataset.targetId;
    const t = threads.get(`${type}:${id}`);
    if (!t || t.older || !t.comments.length) return;
    t.older = true;
    redraw('dc-comment');
    try {
      const res = await api.get('/api/comments', { target_type: type, target_id: String(id), before_id: t.comments[0].id });
      const seen = new Set(t.comments.map((c) => c.id));
      t.comments = (res.comments || []).filter((c) => !seen.has(c.id)).concat(t.comments);
      t.hasMore = !!res.has_more;
    } catch (err) {
      if (!err.handled) toast.show(err.detail || 'Could not load older comments.');
    } finally {
      t.older = false;
      redraw('dc-comment');
    }
  },
});

// Comments under a playlist's tracks. Not for crates.
slots.register('playlist.footer', (ctx) => {
  if (!ctx || ctx.crate || !ctx.playlist || ctx.playlist.kind === 'crate') return null;
  const meta = ctx.playlist;
  return commentsSection({ type: 'playlist', id: ctx.playlistId, comments: meta.comments, hasMore: meta.comments_has_more });
});

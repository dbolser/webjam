// Small helpers shared by Lane B's modules (discovery/*, views/track|profile|discover|search).

import { html, nothing } from '../lit.js';
import { state, update } from '../store.js';
import * as slots from '../slots.js';
import { toast } from '../ui/toast.js';
import { signInUrl, userUrl, playlistUrl, trackUrl, relTime, isoTs } from '../format.js';

/** Redraw the shell and the current view after a change to Lane B's module-local state. */
export function redraw(reason = 'dc') {
  update(null, reason);
}

/** Ask the visitor to sign in (C's signin-intent when registered, else a quiet toast). */
export function askSignIn(intent, verb = 'do that') {
  const handled = slots.call('auth.required', { ...intent, path: location.pathname + location.search });
  if (handled !== undefined) return;
  if (state.auth.mode === 'demo') {
    toast.show(`Pick a persona to ${verb}.`);
    return;
  }
  toast.show(`Sign in to ${verb}.`, { action: { label: 'Sign in', href: signInUrl(location.pathname + location.search) } });
}

export function me() {
  return state.auth.signedIn ? state.auth.user : null;
}

/** Demo mode needs the author id in bodies whose field is not `user_id`; Google mode never sends one. */
export function demoId() {
  return state.auth.mode === 'demo' && state.auth.user ? state.auth.user.id : undefined;
}

/** "Nov 2026" from a server timestamp. */
export function monthYear(ts) {
  const d = ts ? new Date(String(ts).replace(' ', 'T') + 'Z') : null;
  if (!d || Number.isNaN(d.getTime())) return '';
  return d.toLocaleDateString(undefined, { month: 'short', year: 'numeric' });
}

/** @handle link (quiet hop). */
export function handleLink(username, { hop = true } = {}) {
  if (!username) return nothing;
  return html`<a class=${hop ? 'hop' : ''} href=${userUrl(username)}>@${username}</a>`;
}

/** One activity line, as /api/discover and /api/me/home return them (no badge lines reach here). */
export function activityLine(a) {
  const who = a.username ? html`<a href=${userUrl(a.username)}>@${a.username}</a>` : nothing;
  let target = html`${a.summary}`;
  if (a.link_type === 'playlist' && a.link_id) target = html`<a class="hop" href=${playlistUrl(a.link_id)}>${a.summary}</a>`;
  else if (a.link_type === 'song' && a.link_id) target = html`<a class="hop" href=${trackUrl(a.link_id)}>${a.summary}</a>`;
  else if (a.link_type === 'profile' && a.username) target = html`<span class="ink-2">${a.summary}</span>`;
  return html`<li class="line"><span class="line-text">${who} ${target}</span>
    <time datetime=${isoTs(a.created_at)}>${relTime(a.created_at)}</time></li>`;
}

/** A "More" row for paged lists. */
export function moreButton(action, { busy = false, label = 'More', data = {} } = {}) {
  return html`<p class="more-row"><button type="button" class="btn btn-quiet" data-action=${action}
    data-group=${data.group ?? nothing} ?disabled=${busy}>${busy ? 'Loading…' : label}</button></p>`;
}

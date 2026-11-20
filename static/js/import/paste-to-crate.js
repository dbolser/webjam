// Paste anywhere (docs/phase1b-shell.md §5.5): the 'crate' paste handler, priority 0.
//
// Lane A's 'playlist-edit' handler (priority 10) takes pastes into an editable list first. Every
// other paste that carries a Webjam clipboard payload, a YouTube or Webjam link, or a run of bare
// video ids lands here: /api/resolve-paste turns it into insert items (with refs, so credit
// survives), and they go into my crate with an "Undo" toast. A YouTube playlist link opens the
// import sheet aimed at the crate. Free text with no links is left alone (the omnibox keeps it as
// a search). An anonymous visitor is asked to sign in, and the paste completes afterwards.

import { state } from '../store.js';
import * as api from '../api.js';
import * as slots from '../slots.js';
import { register as registerPaste } from '../paste-router.js';
import { toast } from '../ui/toast.js';
import { plural } from '../format.js';
import { insertIntoCrate } from '../crate/crate-actions.js';
import { openImport } from './import-sheet.js';
import { here, undoToast } from '../share/util.js';

const MAX_TEXT = 100000;
const MAX_HTML = 250000;

// Links worth a round trip. The server decides what they really are.
const YT_RX = /(?:youtube\.com|youtu\.be|youtube-nocookie\.com|music\.youtube\.com)\//i;
const WEBJAM_RX = /(?:https?:\/\/[^\s/]+)?\/(?:p|t)\/[A-Za-z0-9_-]|#(?:playlist|song)\//i;
const LIST_RX = /^\s*(?:PL|OLAK5uy_)[A-Za-z0-9_-]{10,}\s*$/;
const IDS_RX = /^[\s,]*(?:[A-Za-z0-9_-]{11}[\s,]+)*[A-Za-z0-9_-]{11}[\s,]*$/;

function webjamJson(json) {
  return !!(json && typeof json === 'object' && json.webjam === 1 && Array.isArray(json.items));
}

/** Would this paste mean anything to the crate? (Synchronous: the paste router needs an answer now.) */
export function looksPasteable({ json, html, text } = {}) {
  if (webjamJson(json)) return true;
  if (html && /data-webjam\s*=/.test(html)) return true;
  const t = String(text || '');
  if (!t.trim()) return false;
  return YT_RX.test(t) || WEBJAM_RX.test(t) || LIST_RX.test(t) || IDS_RX.test(t.trim());
}

function itemsFromJson(json) {
  return (json.items || []).slice(0, 1000).map((it) => ({
    video_id: it.video_id,
    start_seconds: it.start_seconds ?? null,
    end_seconds: it.end_seconds ?? null,
    note: it.note || '',
    display_title: it.display_title || null,
    ref: it.ref || null,
    title: it.title || '',
    channel_title: it.channel_title || '',
    thumbnail_url: it.thumbnail_url || '',
  }));
}

/** Resolve a paste and put it into my crate. Resolves when done (toasts included). */
export async function pasteIntoCrate({ json = null, html = '', text = '' } = {}) {
  if (!state.auth.signedIn || !state.auth.crateId) {
    slots.call('auth.required', { action: 'crate-add', payload: { text: String(text || '').slice(0, 20000) }, path: here() });
    return null;
  }
  let items = [];
  let pointers = [];
  let unresolved = [];
  if (webjamJson(json)) {
    items = itemsFromJson(json);
  } else {
    try {
      const res = await api.post('/api/resolve-paste', {
        text: String(text || '').slice(0, MAX_TEXT),
        html: html && html.length <= MAX_HTML ? html : null,
      });
      items = res.items || [];
      pointers = res.pointers || [];
      unresolved = res.unresolved || [];
    } catch (err) {
      if (!err.handled) toast.show(err.detail || 'Could not read that paste.');
      return null;
    }
  }

  const ytList = pointers.find((p) => p && p.type === 'youtube_playlist' && p.source_input);
  let out = null;
  if (items.length) {
    out = await insertIntoCrate(items);
    if (out) {
      const miss = unresolved.length ? ` · ${plural(unresolved.length, 'link')} not found` : '';
      undoToast(`${out.added === 1 ? 'Added to your crate' : `Added ${out.added} tracks to your crate`}${miss}`, { groupId: out.groupId });
    }
  } else if (unresolved.length && !ytList) {
    const gone = unresolved.every((u) => u && u.reason === 'not_found');
    toast.show(gone ? 'Nothing to add: that link is private or gone.' : 'YouTube did not answer; try that paste again.');
  }
  if (ytList) openImport({ source: ytList.source_input, target: 'crate', autoInspect: true });
  return out;
}

registerPaste({
  name: 'crate',
  priority: 0,
  handle(payload) {
    if (!looksPasteable(payload)) return false;
    if (payload.inOmnibox && payload.target && payload.target.blur) payload.target.blur();
    return pasteIntoCrate(payload);
  },
});

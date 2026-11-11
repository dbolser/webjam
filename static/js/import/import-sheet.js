// The import sheet (docs/phase1b-shell.md §4.4): a YouTube playlist link or a few video links →
// POST /api/playlists/import/inspect → a checklist (unavailable items start unchecked, with the
// reason) → into the crate (default) or a new playlist → POST /api/playlists/import/commit.
//
//   openImport({ source, target: 'crate' | 'new', autoInspect })
//
// Opened from the home empty state, the crate actions, the 'import.open' slot, and pasted
// YouTube playlist links (paste-to-crate.js).

import { html, nothing } from '../lit.js';
import { state, update, loadPlaylist } from '../store.js';
import * as api from '../api.js';
import * as slots from '../slots.js';
import { registerActions } from '../actions.js';
import { navigate, refresh } from '../router.js';
import { reloadDock } from '../session.js';
import { toast } from '../ui/toast.js';
import { openSheet, closeSheet, rerenderSheet } from '../ui/sheet.js';
import { playlistUrl, plural, thumbSrc, clipLabel } from '../format.js';
import { here, refreshUndo, whenSheetClosed } from '../share/util.js';

const MAX_TRACKS = 50;
let st = null; // { step, source, report, picked:Set, dest, title, error, busy }

function reasonOf(t) {
  if (t.embeddable) return '';
  if (t.reason) return String(t.reason);
  return t.status && t.status !== 'UNKNOWN' ? String(t.status).toLowerCase().replace(/_/g, ' ') : 'not playable outside YouTube';
}

function sourceTpl() {
  return html`<form id="sh-import-src" class="sh-form" data-action="import-inspect" novalidate>
    <label class="sh-field">
      <span class="label">Link</span>
      <textarea id="sh-import-input" class="input sh-textarea" name="source" rows="4" maxlength="100000"
        spellcheck="false" autocomplete="off" data-action="import-source"
        placeholder="https://www.youtube.com/playlist?list=…  or one video link per line"
        .value=${st.source}></textarea>
    </label>
    <p class="quiet-line">A YouTube playlist, or video links one per line. Up to ${MAX_TRACKS} tracks; each is checked for playback here.</p>
    ${st.error ? html`<p class="sh-err" role="alert">${st.error}</p>` : nothing}
  </form>`;
}

function itemTpl(t, i) {
  const off = !t.embeddable;
  const why = reasonOf(t);
  const clip = clipLabel(t.start_seconds, t.end_seconds);
  return html`<li class="sh-import-item ${off ? 'is-off' : ''}">
    <label>
      <input type="checkbox" data-action="import-pick" data-index=${i} .checked=${st.picked.has(i)} />
      <img class="thumb sh-thumb" src=${thumbSrc(t.thumbnail_url, t.video_id)} alt="" loading="lazy" decoding="async" />
      <span class="sh-import-text">
        <span class="sh-import-title">${t.title || t.video_id}</span>
        <span class="sh-import-meta">${t.channel_title || ''}${clip ? html` · <span class="mono">${clip}</span>` : nothing}${why
          ? html` · <span class="sh-why">${why}</span>` : nothing}</span>
      </span>
    </label>
  </li>`;
}

function reviewTpl() {
  const r = st.report || {};
  const tracks = r.tracks || [];
  const off = tracks.filter((t) => !t.embeddable).length;
  return html`<form id="sh-import-commit" class="sh-form" data-action="import-commit" novalidate>
    <p class="sh-import-sum">
      <span class="sh-import-src-title">${r.title || 'Links'}</span>
      <span class="muted mono">${plural(tracks.length, 'track')}${off ? ` · ${off} unavailable` : ''}</span>
    </p>
    <p class="sh-import-tools">
      <button type="button" class="link-btn" data-action="import-pick-all">Select all playable</button>
      <button type="button" class="link-btn" data-action="import-pick-none">None</button>
    </p>
    <ol class="sh-import-list">${tracks.map(itemTpl)}</ol>
    <fieldset class="sh-radios sh-dest">
      <legend class="label">Into</legend>
      <label class="sh-radio"><input type="radio" name="dest" value="crate" data-action="import-dest" .checked=${st.dest === 'crate'} />
        <span>Your crate</span><span class="sh-hint">sort them later</span></label>
      <label class="sh-radio"><input type="radio" name="dest" value="new" data-action="import-dest" .checked=${st.dest === 'new'} />
        <span>A new playlist</span><span class="sh-hint">public; change it in settings</span></label>
    </fieldset>
    ${st.dest === 'new'
      ? html`<label class="sh-field"><span class="label">Title</span>
          <input class="input" name="title" maxlength="200" autocomplete="off" data-action="import-title" .value=${st.title} /></label>`
      : nothing}
    ${st.error ? html`<p class="sh-err" role="alert">${st.error}</p>` : nothing}
  </form>`;
}

function body() {
  if (!st) return nothing;
  return st.step === 'review' ? reviewTpl() : sourceTpl();
}

function foot() {
  if (!st) return nothing;
  if (st.step === 'review') {
    const n = st.picked.size;
    return html`
      <button type="button" class="btn btn-quiet" data-action="import-back" ?disabled=${st.busy}>Back</button>
      <button type="submit" class="btn btn-primary" form="sh-import-commit" ?disabled=${st.busy || !n}>
        ${st.busy ? 'Importing…' : `Import ${n}`}</button>`;
  }
  return html`
    <button type="button" class="btn btn-quiet" data-action="sheet-close">Cancel</button>
    <button type="submit" class="btn btn-primary" form="sh-import-src" ?disabled=${st.busy}>
      ${st.busy ? 'Checking…' : 'Look it up'}</button>`;
}

export function openImport({ source = '', target = 'crate', autoInspect = false } = {}) {
  if (!state.auth.signedIn) {
    slots.call('auth.required', { action: 'import', path: here() });
    return;
  }
  whenSheetClosed(() => start({ source, target, autoInspect }));
}

function start({ source, target, autoInspect }) {
  st = {
    step: 'source',
    source: String(source || ''),
    report: null,
    picked: new Set(),
    dest: target === 'new' ? 'new' : 'crate',
    title: '',
    error: '',
    busy: false,
  };
  openSheet({ title: 'Import from YouTube', body, foot, onClose: () => { st = null; } });
  if (autoInspect && st.source.trim()) inspect();
}

async function inspect() {
  if (!st || st.busy) return;
  const source = st.source.trim();
  if (!source) {
    st.error = 'Paste a YouTube link first.';
    rerenderSheet();
    return;
  }
  st.busy = true;
  st.error = '';
  rerenderSheet();
  try {
    const report = await api.post('/api/playlists/import/inspect', { source_input: source, max_tracks: MAX_TRACKS });
    if (!st) return;
    const tracks = report.tracks || [];
    st.report = report;
    st.picked = new Set(tracks.map((t, i) => (t.embeddable ? i : -1)).filter((i) => i >= 0));
    st.title = report.source_type === 'youtube_playlist' && report.title ? report.title : '';
    st.step = 'review';
  } catch (err) {
    if (st) st.error = err.detail || 'Could not read that link.';
  } finally {
    if (st) {
      st.busy = false;
      rerenderSheet();
    }
  }
}

async function commit() {
  if (!st || st.busy || st.step !== 'review') return;
  const tracks = (st.report.tracks || []).filter((t, i) => st.picked.has(i));
  if (!tracks.length) {
    st.error = 'Pick at least one track.';
    rerenderSheet();
    return;
  }
  const toCrate = st.dest === 'crate';
  const title = toCrate ? 'Crate' : (st.title.trim() || st.report.title || 'Imported from YouTube').slice(0, 200);
  const cid = state.auth.crateId;
  st.busy = true;
  st.error = '';
  rerenderSheet();
  try {
    const res = await api.queued(() => api.post('/api/playlists/import/commit', {
      title,
      description: toCrate ? '' : (st.report.description || '').slice(0, 5000),
      target_playlist_id: toCrate ? cid : null,
      tracks: tracks.map((t) => ({
        video_id: t.video_id,
        start_seconds: t.start_seconds ?? null,
        end_seconds: t.end_seconds ?? null,
      })),
    }));
    const added = (res.added_tracks || []).length;
    const skipped = (res.skipped || []).length;
    const tail = skipped ? ` · ${skipped} skipped` : '';
    closeSheet('done');
    refreshUndo();
    reloadDock().catch(() => {});
    if (toCrate) {
      if (state.playlists.has(Number(cid))) await loadPlaylist(cid, { force: true }).catch(() => {});
      if (state.route.name === 'home') refresh();
      toast.show(`Imported ${plural(added, 'track')} into your crate${tail}`, {
        action: { label: 'Open', run: () => update((s) => { s.ui.cratePanel = true; }, 'crate-panel') },
      });
    } else {
      await navigate(playlistUrl(res.playlist_id, res.title || title));
      toast.show(`Imported ${plural(added, 'track')}${tail}`);
    }
  } catch (err) {
    if (st) {
      st.busy = false;
      st.error = err.detail || 'The import did not go through.';
      rerenderSheet();
    }
  }
}

registerActions({
  'import-open': (el) => {
    const target = el.dataset.target || (state.route.name === 'home' && !(state.home && (state.home.playlists || []).length) ? 'new' : 'crate');
    openImport({ target });
  },
  'import-source': (el, e) => {
    if (st && e.type === 'input') st.source = el.value;
  },
  'import-inspect': (form) => {
    if (!st) return;
    const ta = form.querySelector('textarea[name="source"]');
    if (ta) st.source = ta.value;
    inspect();
  },
  'import-pick': (el, e) => {
    if (!st || e.type !== 'change') return;
    const i = Number(el.dataset.index);
    if (el.checked) st.picked.add(i);
    else st.picked.delete(i);
    rerenderSheet();
  },
  'import-pick-all': () => {
    if (!st || !st.report) return;
    st.picked = new Set((st.report.tracks || []).map((t, i) => (t.embeddable ? i : -1)).filter((i) => i >= 0));
    rerenderSheet();
  },
  'import-pick-none': () => {
    if (!st) return;
    st.picked = new Set();
    rerenderSheet();
  },
  'import-dest': (el, e) => {
    if (!st || e.type !== 'change' || !el.checked) return;
    st.dest = el.value === 'new' ? 'new' : 'crate';
    if (st.dest === 'new' && !st.title && st.report) st.title = st.report.title || '';
    rerenderSheet();
  },
  'import-title': (el, e) => {
    if (st && e.type === 'input') st.title = el.value;
  },
  'import-back': () => {
    if (!st) return;
    st.step = 'source';
    st.error = '';
    rerenderSheet();
  },
  'import-commit': () => commit(),
});

slots.register('import.open', (opts = {}) => {
  openImport(opts);
  return true;
});

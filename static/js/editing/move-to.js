// Move the selection: G (or #) opens a tiny "Move to #__" field beside the cursor row; Enter sends
// one `move` with `position`. Alt-↑/↓ moves one row; Shift-Alt-↑/↓ to the top / bottom.
// Optimistic: the rows jump at once and get the "just landed" wash at their new place.

import { html } from '../lit.js';
import { state, isMine } from '../store.js';
import { registerActions } from '../actions.js';
import { openPopover, closePopover } from '../ui/popover.js';
import { submit, rowKey } from './ops.js';
import { selectedRows, rowsOfList, isEditableList, listEl, focusRow } from './selection.js';
import { plural } from '../format.js';

function blockOf(list) {
  if (!list || !isEditableList(list)) return null;
  const pid = Number(list.dataset.playlistId);
  if (!pid || !isMine(pid)) return null;
  const key = list.dataset.listKey;
  const rows = rowsOfList(key);
  let picked = state.selection.listKey === key ? selectedRows() : [];
  if (!picked.length) {
    const a = document.activeElement;
    const rowEl = a && a.closest ? a.closest('[data-track-id]') : null;
    const r = rowEl && rows.find((x) => x.id === rowKey(rowEl.dataset.trackId));
    if (r) picked = [r];
  }
  if (!picked.length) return null;
  const ids = picked.map((r) => r.id);
  const first = rows.findIndex((r) => r.id === ids[0]);
  return { pid, key, rows, ids, first, n: rows.length, k: ids.length };
}

/** Move the block so its first row becomes position `pos` (1-based) of the resulting list. */
export function moveBlockTo(list, pos) {
  const b = blockOf(list);
  if (!b) return false;
  const max = b.n - b.k + 1;
  const position = Math.max(1, Math.min(max, Math.floor(Number(pos) || 1)));
  // Already there (a contiguous block at that spot)?
  const idx = b.ids.map((id) => b.rows.findIndex((r) => r.id === id));
  const contiguous = idx.every((v, i) => i === 0 || v === idx[i - 1] + 1);
  if (contiguous && b.first + 1 === position) return true;
  submit({ kind: 'move', playlist_id: b.pid, track_ids: b.ids, position }, {
    select: { listKey: b.key, playlistId: b.pid },
    landed: b.ids,
    toastText: `Moved ${b.k === 1 ? 'to' : `${plural(b.k, 'track')} to`} #${position}`,
  });
  focusRow(b.key, b.ids[b.ids.length - 1], { scroll: true });
  return true;
}

export function nudge(list, dir, { toEdge = false } = {}) {
  const b = blockOf(list);
  if (!b) return false;
  let pos;
  if (toEdge) pos = dir < 0 ? 1 : b.n - b.k + 1;
  else pos = dir < 0 ? b.first : b.first + 2; // first is 0-based; up = one earlier, down = one later
  return moveBlockTo(list, pos);
}

// --- the "Move to #__" field -----------------------------------------------------------------------

let target = null; // { key }

export function openMoveTo(list, anchorRow) {
  const b = blockOf(list);
  if (!b) return false;
  target = { key: b.key };
  const anchor = anchorRow || list.querySelector(`[data-track-id="${b.ids[b.ids.length - 1]}"]`) || list;
  openPopover(anchor, () => html`<form class="ed-moveto" data-action="move-to" data-cancel-action="move-to-cancel">
      <label for="ed-moveto-n">Move ${b.k === 1 ? '' : `${b.k} tracks `}to #</label>
      <input id="ed-moveto-n" class="input ed-moveto-n" name="n" type="number" inputmode="numeric"
        min="1" max=${b.n - b.k + 1} autocomplete="off" required />
      <span class="ed-moveto-of">of ${b.n}</span>
    </form>`, { label: 'Move to position' });
  return true;
}

registerActions({
  'move-to': (form) => {
    const n = Number(form.querySelector('input[name="n"]').value);
    const t = target;
    target = null;
    closePopover();
    if (!t || !Number.isFinite(n) || n < 1) return;
    moveBlockTo(listEl(t.key), n);
  },
  'move-to-cancel': () => {
    target = null;
    closePopover({ restoreFocus: true });
  },
});

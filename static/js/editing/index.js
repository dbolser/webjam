// Lane A (Editing) entry: selection, clipboard, keys, drag and drop, move-to, undo, inline edits.
// Wired in through the registries only (keys, actions, slots, paste router); main.js imports this
// once, after F's core actions and keys, so these key bindings run first.

import { state, subscribe, isMine } from '../store.js';
import { registerKeys } from '../keys.js';
import { registerActions } from '../actions.js';
import * as slots from '../slots.js';
import { openMenu } from '../ui/menu.js';
import { chips, openSlot } from '../panels/dock.js';
import {
  startSelection, moveCursor, selectAll, clearSelection, selectionCount, selectedRows, isEditableList,
  pruneSelection, rowsOfList,
} from './selection.js';
import {
  startClipboard, copySelection, cancelCut, pasteInternal, armPasteFallback, duplicateSelection,
} from './clipboard.js';
import { startDnd, cancelDrag } from './dnd.js';
import { nudge, openMoveTo } from './move-to.js';
import { doUndo, doRedo } from './undo.js';
import { openMetaEditor, deletePlaylist, removeRows, openClipEditor, openSwap } from './inline-edit.js';
import { sendToSlot, sendToChip } from './send.js';
import { openCheatsheet } from './cheatsheet.js';

startSelection();
startClipboard();
startDnd();

// Forget a selection whose list left the page.
subscribe((s) => ({ path: s.route.path }), () => requestAnimationFrame(pruneSelection));

// --- helpers --------------------------------------------------------------------------------------------

function editableList(ctx) {
  const list = ctx.list;
  return list && isEditableList(list) && isMine(Number(list.dataset.playlistId)) ? list : null;
}

function rowsFor(ctx) {
  const key = ctx.list && ctx.list.dataset.listKey;
  if (key && state.selection.listKey === key && selectionCount()) return selectedRows();
  if (ctx.row) {
    const id = Number(ctx.row.dataset.trackId);
    const r = rowsOfList(key).find((x) => x.id === id);
    if (r) return [r];
  }
  return selectedRows();
}

function srcPidOf(ctx, rows) {
  const v = ctx.list && ctx.list.dataset.playlistId;
  if (v) return Number(v);
  return rows[0] && rows[0].playlist_id ? Number(rows[0].playlist_id) : null;
}

function cursorRowEl(list) {
  const a = document.activeElement;
  if (a && list.contains(a) && a.matches('[data-track-id]')) return a;
  const id = state.selection.cursorId;
  return id != null ? list.querySelector(`[data-track-id="${id}"]`) : null;
}

// --- keys -----------------------------------------------------------------------------------------------

const digitKeys = [];
for (let n = 0; n <= 9; n++) {
  digitKeys.push({
    combo: String(n), when: 'list', group: 'Send',
    label: n === 0 ? 'Send the selection to a dock slot (0 is the crate)' : undefined,
    display: '0–9',
    run: (e, ctx) => {
      if (!state.auth.signedIn || !state.dock) return false;
      const rows = rowsFor(ctx);
      if (!rows.length) return false;
      sendToSlot(n, rows, srcPidOf(ctx, rows));
    },
  });
  digitKeys.push({
    combo: `alt+${n}`, when: 'always', group: 'Send',
    label: n === 0 ? 'Open a dock slot' : undefined,
    display: `${comboAlt()}0–9`,
    run: () => {
      if (!state.auth.signedIn || !state.dock) return false;
      openSlot(n);
    },
  });
}

function comboAlt() {
  return /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || '') ? '⌥' : 'Alt+';
}

registerKeys([
  { combo: '?', when: 'always', label: 'Keyboard shortcuts', group: 'General', run: () => openCheatsheet() },
  { combo: 'escape', when: 'always', label: 'Cancel drag, cut or selection', group: 'General', run: () => {
    if (cancelDrag()) return true;
    if (cancelCut()) return true;
    if (selectionCount()) return clearSelection();
    return false;
  } },

  { combo: 'arrowdown', when: 'list', label: 'Next track', group: 'Select', run: (e, ctx) => moveCursor(ctx.list, 1) },
  { combo: 'arrowup', when: 'list', label: 'Previous track', group: 'Select', run: (e, ctx) => moveCursor(ctx.list, -1) },
  { combo: 'shift+arrowdown', when: 'list', label: 'Extend the selection', group: 'Select', run: (e, ctx) => moveCursor(ctx.list, 1, { extend: true }) },
  { combo: 'shift+arrowup', when: 'list', label: 'Extend the selection', group: 'Select', run: (e, ctx) => moveCursor(ctx.list, -1, { extend: true }) },
  { combo: 'mod+a', when: 'list', label: 'Select all', group: 'Select', run: (e, ctx) => selectAll(ctx.list) },

  { combo: 'mod+c', when: 'list', label: 'Copy', group: 'Edit', run: () => copySelection() },
  { combo: 'mod+x', when: 'list', label: 'Cut', group: 'Edit', run: () => copySelection({ cut: true }) },
  { combo: 'v', when: 'editable', label: 'Paste after the selection (repeat for more copies)', group: 'Edit', run: (e, ctx) => {
    const list = editableList(ctx);
    if (!list) return false;
    pasteInternal(list);
  } },
  { combo: 'mod+v', when: 'editable', label: 'Paste after the selection (repeat for more copies)', group: 'Edit', run: (e, ctx) => {
    const list = editableList(ctx);
    if (list) armPasteFallback(list);
    return false; // let the browser fire `paste`; the fallback covers browsers that don't
  } },
  { combo: 'mod+d', when: 'editable', label: 'Duplicate', group: 'Edit', run: (e, ctx) => {
    const list = editableList(ctx);
    if (!list) return false;
    duplicateSelection(list);
  } },
  { combo: 'delete', when: 'editable', label: 'Remove', group: 'Edit', run: (e, ctx) => {
    const list = editableList(ctx);
    if (!list) return false;
    removeRows(list);
  } },
  { combo: 'backspace', when: 'editable', label: 'Remove', group: 'Edit', run: (e, ctx) => {
    const list = editableList(ctx);
    if (!list) return false;
    removeRows(list);
  } },
  { combo: 't', when: 'editable', label: 'Note and clip', group: 'Edit', run: (e, ctx) => {
    const list = editableList(ctx);
    const row = list && cursorRowEl(list);
    if (!row) return false;
    openClipEditor(list, row);
  } },
  { combo: 'mod+z', when: 'always', label: 'Undo', group: 'Edit', run: () => {
    if (!state.auth.signedIn) return false;
    doUndo();
  } },
  { combo: 'mod+shift+z', when: 'always', label: 'Redo', group: 'Edit', run: () => {
    if (!state.auth.signedIn) return false;
    doRedo();
  } },
  { combo: 'ctrl+y', when: 'always', label: 'Redo', group: 'Edit', run: () => {
    if (!state.auth.signedIn) return false;
    doRedo();
  } },

  { combo: 'g', when: 'editable', label: 'Move to position…', group: 'Arrange', run: (e, ctx) => {
    const list = editableList(ctx);
    if (!list) return false;
    openMoveTo(list, cursorRowEl(list));
  } },
  { combo: '#', when: 'editable', label: 'Move to position…', group: 'Arrange', run: (e, ctx) => {
    const list = editableList(ctx);
    if (!list) return false;
    openMoveTo(list, cursorRowEl(list));
  } },
  { combo: 'alt+arrowup', when: 'editable', label: 'Move up one', group: 'Arrange', run: (e, ctx) => nudge(editableList(ctx), -1) },
  { combo: 'alt+arrowdown', when: 'editable', label: 'Move down one', group: 'Arrange', run: (e, ctx) => nudge(editableList(ctx), 1) },
  { combo: 'shift+alt+arrowup', when: 'editable', label: 'Move to the top', group: 'Arrange', run: (e, ctx) => nudge(editableList(ctx), -1, { toEdge: true }) },
  { combo: 'shift+alt+arrowdown', when: 'editable', label: 'Move to the bottom', group: 'Arrange', run: (e, ctx) => nudge(editableList(ctx), 1, { toEdge: true }) },

  ...digitKeys,
]);

// --- actions ----------------------------------------------------------------------------------------------

registerActions({
  undo: () => doUndo(),
  redo: () => doRedo(),
  'sel-clear': () => clearSelection(),
  'edit-keys': () => openCheatsheet(),
});

// --- menus -------------------------------------------------------------------------------------------------

function sendMenu(anchor, rows, srcPid) {
  const items = chips()
    .filter((c) => Number(c.playlist_id) !== Number(srcPid))
    .map((c) => ({
      label: c.kind === 'crate' ? 'Crate' : c.title,
      icon: c.kind === 'crate' ? 'crate' : null,
      kbd: c.slot != null ? String(c.slot) : null,
      run: () => sendToChip(c.kind === 'crate' ? `crate:${c.playlist_id}` : `playlist:${c.playlist_id}`, rows, srcPid),
    }));
  items.push('sep', { label: 'New playlist', icon: 'plus', run: () => sendToChip('new', rows, srcPid) });
  openMenu(anchor, items, { label: 'Send to' });
}

function rowMenuItems(m, { crate = false } = {}) {
  const list = m.list && isEditableList(m.list) && isMine(Number(m.list.dataset.playlistId)) ? m.list : null;
  const rowEl = m.rowEl;
  const anchor = rowEl && rowEl.querySelector('[data-action="menu-open"]');
  const items = [];
  if (state.auth.signedIn && state.dock && !crate) {
    items.push({ label: 'Send to…', run: () => {
      const c = { list: m.list, row: rowEl };
      const rows = rowsFor(c);
      if (anchor && rows.length) sendMenu(anchor, rows, srcPidOf(c, rows));
    } });
  }
  if (list && rowEl) {
    items.push(
      { label: 'Note and clip…', icon: 'seam', kbd: 'T', run: () => openClipEditor(list, rowEl) },
      { label: 'Move to position…', kbd: 'G', run: () => openMoveTo(list, rowEl) },
    );
    if (!crate) {
      items.push(
        { label: 'Swap video…', icon: 'link', run: () => openSwap(list, rowEl) },
        'sep',
        { label: 'Remove', icon: 'close', kbd: 'Del', danger: true, run: () => {
          const r = rowsOfList(list.dataset.listKey).find((x) => x.id === Number(rowEl.dataset.trackId));
          if (r) removeRows(list, [r]);
        } },
      );
    }
  }
  return items;
}

slots.register('row.menu', (m) => rowMenuItems(m));
slots.register('crate.row.menu', (m) => rowMenuItems(m, { crate: true }));
slots.register('playlist.menu', (m) => {
  if (!m.mine || m.crate) return [];
  return [
    { label: 'Edit details', run: () => openMetaEditor(m.playlistId) },
    { label: 'Keyboard shortcuts', kbd: '?', run: () => openCheatsheet() },
    'sep',
    { label: 'Delete playlist…', danger: true, run: () => deletePlaylist(m.playlistId) },
  ];
});


// Pointer-based drag and drop, row clicks and the touch "tap to arm" fallback
// (docs/phase1b-shell.md §5.3, §5.4). Native HTML drag never drives in-app drops; the title link
// stays natively draggable, so a track can still be dropped into a chat app as a URL.
//
//   mouse: drag starts after 4 px of movement from a pointerdown on a row (not on a link, button
//          or field). touch / pen: after a 350 ms long-press that moves less than 8 px.
//   targets: gaps in an editable list (the insertion line), dock chips ([data-drop-target]), the
//          crate panel, and a dock chip's spring-open list (hover 600 ms) for an exact spot.
//   Alt toggles copy / move where the rules allow; Esc or pointercancel cancels; auto-scroll near
//   the edges. One drop is one ops batch, optimistic, one undo group.

import { html, render, nothing } from '../lit.js';
import { state, onChange, loadPlaylist, rowsOf, isMine } from '../store.js';
import { runAction } from '../actions.js';
import { openPopover, closePopover, rerenderPopover } from '../ui/popover.js';
import { thumbSrc, plural } from '../format.js';
import { real, rowKey } from './ops.js';
import {
  clickSelect, selectedRows, selectionCount, rowsOfList, isEditableList, drawInsertLine,
  hideAllInsertLines, setDragging, clearSelection, focusRow,
} from './selection.js';
import { modeFor, transfer, sendToChip, isCrate } from './send.js';

const NO_DRAG = 'a, button, input, textarea, select, label, summary, [contenteditable], .ed-no-drag';
const ROW = '.tracks[role="listbox"] > [data-track-id]';
const SPRING_MS = 600;
const EDGE = 48;

let press = null;
let drag = null;
let suppressClick = false;
let ghostEl = null;

const coarse = () => matchMedia('(pointer: coarse)').matches;

// --- press and start --------------------------------------------------------------------------------

function onPointerDown(e) {
  if (e.button !== 0 || drag) return;
  const t = e.target instanceof Element ? e.target : null;
  const rowEl = t && t.closest(ROW);
  if (!rowEl || t.closest(NO_DRAG)) return;
  press = {
    id: e.pointerId, type: e.pointerType, x: e.clientX, y: e.clientY, rowEl,
    list: rowEl.closest('[data-list-key]'), timer: null,
    wasSelected: rowEl.classList.contains('is-selected'),
  };
  document.body.classList.add('ed-press');
  if (e.pointerType !== 'mouse') {
    press.timer = setTimeout(() => {
      if (press && !drag) startDrag(press, press.x, press.y);
    }, 350);
  }
}

function onPointerMove(e) {
  if (drag) {
    if (e.pointerId !== drag.pointerId) return;
    drag.x = e.clientX;
    drag.y = e.clientY;
    drag.alt = e.altKey;
    track();
    return;
  }
  if (!press || e.pointerId !== press.id) return;
  const d = Math.hypot(e.clientX - press.x, e.clientY - press.y);
  if (press.type === 'mouse') {
    if (d > 4) startDrag(press, e.clientX, e.clientY);
  } else if (d > 8) {
    endPress(); // the finger is scrolling
  }
}

function onPointerUp(e) {
  if (drag && e.pointerId === drag.pointerId) {
    drag.alt = e.altKey;
    drop();
    suppressClick = true;
    setTimeout(() => { suppressClick = false; }, 0);
  }
  endPress();
}

function endPress() {
  if (press && press.timer) clearTimeout(press.timer);
  press = null;
  if (!drag) document.body.classList.remove('ed-press');
}

function rowsForDrag(p) {
  const key = p.list && p.list.dataset.listKey;
  const id = rowKey(p.rowEl.dataset.trackId);
  const sel = state.selection;
  if (sel.listKey === key && sel.ids && sel.ids.has(id)) return selectedRows();
  clickSelect(p.rowEl);
  const r = rowsOfList(key).find((x) => x.id === id);
  return r ? [r] : [];
}

function startDrag(p, x, y) {
  const rows = rowsForDrag(p).filter((r) => r && r.video_id && real(r.id) !== 0);
  if (!rows.length) {
    endPress();
    return;
  }
  const srcPid = p.list && p.list.dataset.playlistId ? Number(p.list.dataset.playlistId) : (rows[0].playlist_id ? Number(rows[0].playlist_id) : null);
  drag = {
    pointerId: p.id, type: p.type, x, y, alt: false, rows, srcPid,
    srcKey: p.list ? p.list.dataset.listKey : null,
    target: null, chip: null, springTimer: null, springPid: null, leaveTimer: null, raf: 0,
  };
  if (press && press.timer) clearTimeout(press.timer);
  press = null;
  setDragging(true);
  document.body.classList.add('ed-dragging');
  const sel = window.getSelection && window.getSelection();
  if (sel && sel.removeAllRanges) sel.removeAllRanges();
  markSources(true);
  if (p.type !== 'mouse' && navigator.vibrate) {
    try { navigator.vibrate(10); } catch { /* ignore */ }
  }
  ghost();
  track();
  drag.raf = requestAnimationFrame(autoScroll);
}

function markSources(on) {
  if (!drag || !drag.srcKey) return;
  const list = document.querySelector(`[data-list-key="${CSS.escape(drag.srcKey)}"]`);
  if (!list) return;
  const ids = new Set(drag.rows.map((r) => String(r.id)));
  for (const el of list.querySelectorAll(':scope > [data-track-id]')) {
    if (ids.has(el.dataset.trackId)) el.classList.toggle('is-dragging', on);
  }
}

// --- the ghost ---------------------------------------------------------------------------------------

function ghost() {
  if (!drag) {
    if (ghostEl) {
      render(nothing, ghostEl);
      ghostEl.hidden = true;
    }
    return;
  }
  if (!ghostEl) {
    ghostEl = document.createElement('div');
    ghostEl.className = 'ed-ghost';
    ghostEl.setAttribute('aria-hidden', 'true');
    document.body.append(ghostEl);
  }
  ghostEl.hidden = false;
  const r = drag.rows[0];
  const n = drag.rows.length;
  const mode = drag.target ? targetMode(drag.target) : '';
  render(html`<img class="ed-ghost-thumb" src=${thumbSrc(r.thumbnail_url, r.video_id)} alt="" />
    <span class="ed-ghost-text">${n === 1 ? r.title || r.video_id : plural(n, 'track')}</span>
    ${mode ? html`<span class="ed-ghost-mode">${mode === 'move' ? 'Move' : 'Copy'}</span>` : nothing}`, ghostEl);
  ghostEl.style.setProperty('--x', `${Math.round(drag.x + 14)}px`);
  ghostEl.style.setProperty('--y', `${Math.round(drag.y + 10)}px`);
}

// --- targets -----------------------------------------------------------------------------------------

function targetMode(t) {
  if (t.kind === 'new') return isCrate(drag.srcPid) ? (drag.alt ? 'copy' : 'move') : (drag.alt && isMine(drag.srcPid) ? 'move' : 'copy');
  return modeFor(drag.srcPid, t.pid, drag.alt);
}

/** The gap under y in an editable list: the id of the row it follows (null = the top). */
function gapIn(list, y) {
  const rows = [...list.querySelectorAll(':scope > [data-track-id]')];
  let after = null;
  for (const el of rows) {
    const r = el.getBoundingClientRect();
    if (y < r.top + r.height / 2) return after;
    after = rowKey(el.dataset.trackId);
  }
  return after;
}

function springGap(y) {
  const items = [...document.querySelectorAll('.ed-spring-list > [data-after]')];
  let after = null;
  for (const el of items) {
    const r = el.getBoundingClientRect();
    if (y < r.top + r.height / 2) return { after, el, before: true };
    after = Number(el.dataset.after);
  }
  return { after, el: items[items.length - 1] || null, before: false };
}

function hit(x, y) {
  const el = document.elementFromPoint(x, y);
  if (!el) return null;
  if (el.closest('.ed-spring') && drag.springPid) {
    const g = springGap(y);
    return { kind: 'spring', pid: drag.springPid, after: g.after, el: g.el, before: g.before };
  }
  const chip = el.closest('[data-drop-target]');
  if (chip) {
    const v = chip.dataset.dropTarget;
    if (v === 'new') return { kind: 'new', chip };
    const m = /^(crate|playlist):(\d+)$/.exec(v || '');
    if (m) return { kind: 'chip', pid: Number(m[2]), chip, value: v };
    return null;
  }
  const list = el.closest('[role="listbox"][data-editable]');
  if (list && isEditableList(list) && isMine(Number(list.dataset.playlistId))) {
    return { kind: 'list', pid: Number(list.dataset.playlistId), list, key: list.dataset.listKey, after: gapIn(list, y) };
  }
  const panel = el.closest('#crate-panel');
  if (panel && state.auth.crateId) return { kind: 'list', pid: Number(state.auth.crateId), list: null, after: undefined };
  return null;
}

function clearVisuals() {
  hideAllInsertLines();
  for (const el of document.querySelectorAll('.is-drop')) el.classList.remove('is-drop');
  for (const el of document.querySelectorAll('.ed-gap-before, .ed-gap-after')) el.classList.remove('ed-gap-before', 'ed-gap-after');
}

function track() {
  if (!drag) return;
  const t = hit(drag.x, drag.y);
  drag.target = t;
  clearVisuals();
  if (t && t.kind === 'list' && t.list) drawInsertLine(t.list, t.after);
  if (t && (t.kind === 'chip' || t.kind === 'new')) t.chip.classList.add('is-drop');
  if (t && t.kind === 'spring' && t.el) t.el.classList.add(t.before ? 'ed-gap-before' : 'ed-gap-after');
  spring(t);
  ghost();
}

// --- spring-open: hover a chip for 600 ms to drop at an exact spot ------------------------------------------

function spring(t) {
  const overChip = t && t.kind === 'chip' ? t : null;
  const overSpring = t && t.kind === 'spring';
  if (overSpring || (overChip && overChip.pid === drag.springPid)) {
    clearTimeout(drag.leaveTimer);
    drag.leaveTimer = null;
    return;
  }
  if (drag.springPid && !drag.leaveTimer) {
    drag.leaveTimer = setTimeout(() => closeSpring(), 250);
  }
  if (overChip) {
    if (drag.chip !== overChip.chip) {
      clearTimeout(drag.springTimer);
      drag.chip = overChip.chip;
      drag.springTimer = setTimeout(() => openSpring(overChip.chip, overChip.pid), SPRING_MS);
    }
  } else {
    clearTimeout(drag.springTimer);
    drag.chip = null;
  }
}

function springTpl() {
  if (!drag || !drag.springPid) return nothing;
  const pid = drag.springPid;
  const p = state.playlists.get(pid);
  const rows = p ? rowsOf(pid) : [];
  const title = isCrate(pid) ? 'Crate' : p ? p.meta.title : '';
  return html`<div class="ed-spring">
    <p class="label">Drop into ${title}</p>
    ${!p
      ? html`<p class="ed-quiet">Loading…</p>`
      : rows.length
        ? html`<ol class="ed-spring-list">${rows.map((r, i) => html`<li data-after=${r.id}><span class="ed-spring-n">${i + 1}</span>${r.title || r.video_id}</li>`)}</ol>`
        : html`<p class="ed-quiet">Empty. Drop to add.</p>`}
  </div>`;
}

function openSpring(chip, pid) {
  if (!drag) return;
  drag.springPid = pid;
  clearTimeout(drag.leaveTimer);
  drag.leaveTimer = null;
  openPopover(chip, springTpl, { label: 'Drop at a position' });
  if (!state.playlists.has(pid)) {
    loadPlaylist(pid).then(() => { if (drag && drag.springPid === pid) rerenderPopover(); }).catch(() => {});
  }
}

function closeSpring() {
  if (!drag) return;
  drag.springPid = null;
  drag.leaveTimer = null;
  closePopover();
}

// --- auto-scroll -------------------------------------------------------------------------------------------

function scroller(x, y) {
  const el = document.elementFromPoint(x, y);
  const box = el && el.closest('.crate-body, .ed-spring-list, .popover');
  if (box && box.scrollHeight > box.clientHeight) return box;
  return null;
}

function autoScroll() {
  if (!drag) return;
  const { x, y } = drag;
  const box = scroller(x, y);
  let top = 0;
  let bottom = innerHeight;
  if (box) {
    const r = box.getBoundingClientRect();
    top = r.top;
    bottom = r.bottom;
  } else {
    const bar = document.getElementById('topbar');
    const pb = document.getElementById('player-bar');
    if (bar) top = Math.max(0, bar.getBoundingClientRect().bottom);
    if (pb && !pb.hidden) bottom = pb.getBoundingClientRect().top;
  }
  let dy = 0;
  if (y < top + EDGE) dy = -Math.ceil(((top + EDGE - y) / EDGE) * 14);
  else if (y > bottom - EDGE) dy = Math.ceil(((y - (bottom - EDGE)) / EDGE) * 14);
  if (dy) {
    if (box) box.scrollTop += dy;
    else window.scrollBy(0, dy);
    track();
  }
  drag.raf = requestAnimationFrame(autoScroll);
}

// --- drop / cancel -----------------------------------------------------------------------------------------

function finish() {
  if (!drag) return;
  cancelAnimationFrame(drag.raf);
  clearTimeout(drag.springTimer);
  clearTimeout(drag.leaveTimer);
  if (drag.springPid) closePopover();
  markSources(false);
  clearVisuals();
  drag = null;
  setDragging(false);
  document.body.classList.remove('ed-dragging', 'ed-press');
  ghost();
}

export function cancelDrag() {
  if (!drag) return false;
  finish();
  return true;
}

export function isDragging() {
  return !!drag;
}

/** Same-playlist move: is the result identical to now? */
function noOpMove(pid, ids, after) {
  const p = state.playlists.get(pid);
  if (!p) return false;
  const moving = new Set(ids);
  const rest = p.order.filter((id) => !moving.has(id));
  const block = p.order.filter((id) => moving.has(id));
  let at = after == null ? 0 : rest.indexOf(after) + 1;
  if (after != null && at === 0) return false;
  const next = [...rest.slice(0, at), ...block, ...rest.slice(at)];
  return next.every((id, i) => id === p.order[i]);
}

function drop() {
  const d = drag;
  const t = d && d.target;
  if (!d || !t) {
    finish();
    return;
  }
  const mode = targetMode(t);
  const rows = d.rows;
  const src = d.srcPid;
  finish();
  if (t.kind === 'new') {
    sendToChip('new', rows, src, { alt: d.alt });
    return;
  }
  if (t.kind === 'chip') {
    sendToChip(t.value, rows, src, { alt: d.alt });
    return;
  }
  let after = t.after;
  const dst = t.pid;
  if (mode === 'move' && src === dst && after != null) {
    // Dropped inside its own block: anchor on the nearest row above that isn't moving.
    const ids = new Set(rows.map((r) => real(r.id)));
    const order = (state.playlists.get(dst) || { order: [] }).order;
    let i = order.indexOf(real(after));
    while (i >= 0 && ids.has(order[i])) i -= 1;
    after = i >= 0 ? order[i] : null;
  }
  if (mode === 'move' && src === dst && after !== undefined && noOpMove(dst, rows.map((r) => real(r.id)), after)) return;
  const select = t.key ? { listKey: t.key, playlistId: dst } : null;
  transfer(rows, src, dst, mode, { after, select });
  if (t.key) focusRow(t.key, rows[rows.length - 1].id);
}

// --- clicks: select, double-click to play, touch arming ------------------------------------------------------

function onClickCapture(e) {
  if (suppressClick) {
    suppressClick = false;
    e.preventDefault();
    e.stopPropagation();
    return;
  }
  if (!coarse() || !state.auth.signedIn) return;
  const t = e.target instanceof Element ? e.target : null;
  if (!t) return;
  const chip = t.closest('#dock [data-drop-target]');
  const n = selectionCount();
  if (chip && n) {
    e.preventDefault();
    e.stopPropagation();
    const rows = selectedRows();
    const src = state.selection.playlistId;
    sendToChip(chip.dataset.dropTarget, rows, src);
    return;
  }
  // Tapping outside lists and the dock disarms.
  if (n && !t.closest('[role="listbox"], #dock, .popover, #sheet, #toasts, #player-bar')) clearSelection();
}

function onClick(e) {
  const t = e.target instanceof Element ? e.target : null;
  const rowEl = t && t.closest(ROW);
  if (!rowEl || t.closest(NO_DRAG) || e.defaultPrevented) return;
  const touch = coarse();
  const wasOnly = rowEl.classList.contains('is-selected') && selectionCount() === 1;
  if (touch && wasOnly) {
    const btn = rowEl.querySelector('[data-action="menu-open"]');
    if (btn) runAction('menu-open', btn, e);
    return;
  }
  clickSelect(rowEl, { shift: e.shiftKey, toggle: e.metaKey || e.ctrlKey });
  if (document.activeElement !== rowEl) rowEl.focus({ preventScroll: true });
}

function onDblClick(e) {
  const t = e.target instanceof Element ? e.target : null;
  const rowEl = t && t.closest(ROW);
  if (!rowEl || t.closest(NO_DRAG)) return;
  const btn = rowEl.querySelector('[data-action="play-track"]');
  if (btn) runAction('play-track', btn, e);
}

function onKeyAlt(e) {
  if (!drag || e.key !== 'Alt') return;
  drag.alt = e.type === 'keydown';
  track();
}

// --- the "Send 2 to…" strip on touch screens ---------------------------------------------------------------

function armedLabel() {
  const dock = document.getElementById('dock');
  if (!dock) return;
  const n = coarse() && state.auth.signedIn ? selectionCount() : 0;
  document.body.classList.toggle('ed-armed', n > 0);
  if (n) dock.dataset.edArmed = `Send ${n} to…`;
  else delete dock.dataset.edArmed;
}

let started = false;
export function startDnd() {
  if (started) return;
  started = true;
  document.addEventListener('pointerdown', onPointerDown);
  document.addEventListener('pointermove', onPointerMove);
  document.addEventListener('pointerup', onPointerUp);
  document.addEventListener('pointercancel', () => {
    if (drag) finish();
    endPress();
  });
  // Non-passive so a touch drag can stop the page from scrolling under the finger.
  document.addEventListener('touchmove', (e) => {
    if (drag && e.cancelable) e.preventDefault();
  }, { passive: false });
  document.addEventListener('contextmenu', (e) => {
    if ((press && press.type !== 'mouse') || drag) e.preventDefault();
  });
  document.addEventListener('click', onClickCapture, true);
  document.addEventListener('click', onClick);
  document.addEventListener('dblclick', onDblClick);
  document.addEventListener('keydown', onKeyAlt);
  document.addEventListener('keyup', onKeyAlt);
  window.addEventListener('blur', () => { if (drag) finish(); });
  onChange((reasons) => {
    if (reasons.size === 1 && reasons.has('tick')) return;
    armedLabel();
  });
}


// Theme: system -> light -> dark, written to <html data-theme> and localStorage["webjam.theme"].

import { state, update } from './store.js';

const KEY = 'webjam.theme';
const ORDER = ['system', 'light', 'dark'];

function apply(theme) {
  const root = document.documentElement;
  if (theme === 'light' || theme === 'dark') root.setAttribute('data-theme', theme);
  else root.removeAttribute('data-theme');
}

export function initTheme() {
  let t = 'system';
  try {
    const v = localStorage.getItem(KEY);
    if (ORDER.includes(v)) t = v;
  } catch {
    /* storage blocked: follow the system */
  }
  apply(t);
  state.ui.theme = t;
}

export function cycleTheme() {
  const next = ORDER[(ORDER.indexOf(state.ui.theme) + 1) % ORDER.length];
  apply(next);
  try {
    localStorage.setItem(KEY, next);
  } catch {
    /* ignore */
  }
  update((s) => { s.ui.theme = next; }, 'theme');
  return next;
}

export function themeLabel(t = state.ui.theme) {
  return t === 'light' ? 'Light theme' : t === 'dark' ? 'Dark theme' : 'Theme follows the system';
}

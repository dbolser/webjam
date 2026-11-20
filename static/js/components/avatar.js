// Avatars: a circle of initials. Colour comes from a class, never a style attribute (§1.5, §4.6).

import { html } from '../lit.js';
import { initials } from '../format.js';

const PALETTE = ['#1f5f5b', '#8c3b2b', '#3d4e81', '#6b4226', '#4a6b2c', '#7a3b69'];

export function avatarClass(color) {
  const i = PALETTE.indexOf(String(color || '').toLowerCase());
  return i >= 0 ? `av-${i}` : 'av-x';
}

/** avatar(user, { small }) — 24px (headers) or 20px (inline). Decorative: the name sits beside it. */
export function avatar(user, { small = false } = {}) {
  return html`<span class="av ${small ? 'av-sm' : ''} ${avatarClass(user && user.avatar_color)}" aria-hidden="true">${initials(user)}</span>`;
}

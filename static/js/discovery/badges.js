// Badges (§1.7): only here, at the bottom of a profile, plus the unlock toast (ui/toast.js).
// Earned badges are a wrapped line of small items (glyph + name, a tooltip with the description and
// the context note). One quiet link, "N more to find", lists the locked ones by name and
// description only: no progress bars, thresholds or percentages, for the owner too.

import { html, nothing, repeat } from '../lit.js';
import { registerActions } from '../actions.js';
import { redraw } from './util.js';

let showLocked = false;
let openKey = null; // the earned badge whose description is shown inline (tap to read on touch)

export function resetBadges() {
  showLocked = false;
  openKey = null;
}

function tip(b) {
  return [b.description, b.context_note].filter(Boolean).join(' · ');
}

/** Subtle and rare: a visitor sees the section only when the curator has earned something, and
 *  only the owner sees "N to find". */
export function badgesSection(profile, { owner = false } = {}) {
  const catalog = Array.isArray(profile.badge_catalog) ? profile.badge_catalog : [];
  const earned = Array.isArray(profile.earned_badges) ? profile.earned_badges : [];
  if (!earned.length && !owner) return nothing;
  if (!catalog.length && !earned.length) return nothing;
  const have = new Set(earned.map((b) => b.key));
  const locked = owner ? catalog.filter((b) => !have.has(b.key)) : [];
  return html`<section class="section dc-badges" id="badges" aria-labelledby="dc-h-badges">
    <h2 class="label" id="dc-h-badges">Badges</h2>
    ${earned.length
      ? html`<ul class="dc-badge-line">${repeat(earned, (b) => b.key, (b) => html`<li class="dc-badge">
          <button type="button" class="dc-badge-btn" data-action="badge-info" data-badge-key=${b.key}
            title=${tip(b)} aria-expanded=${openKey === b.key ? 'true' : 'false'}>
            <span class="dc-badge-glyph" aria-hidden="true">${b.icon || ''}</span><span>${b.name}</span>
            ${tip(b) && openKey !== b.key ? html`<span class="visually-hidden">: ${tip(b)}</span>` : nothing}
          </button>
          ${openKey === b.key && tip(b) ? html`<span class="dc-badge-desc">${tip(b)}</span>` : nothing}
        </li>`)}</ul>`
      : html`<p class="quiet-line">None yet.</p>`}
    ${locked.length
      ? html`<p class="dc-badge-more"><button type="button" class="link-btn" data-action="badges-toggle"
          aria-expanded=${showLocked ? 'true' : 'false'} aria-controls="dc-badges-locked">
          ${`${locked.length}${earned.length ? ' more' : ''} to find`}</button></p>
        ${showLocked
          ? html`<ul class="dc-badge-locked" id="dc-badges-locked">${repeat(locked, (b) => b.key, (b) => html`<li>
              <span class="dc-badge-name">${b.name}</span><span class="dc-badge-desc">${b.description || ''}</span></li>`)}</ul>`
          : nothing}`
      : nothing}
  </section>`;
}

registerActions({
  'badge-info': (el) => {
    const key = el.dataset.badgeKey || null;
    openKey = openKey === key ? null : key;
    redraw('dc-badges');
  },
  'badges-toggle': () => {
    showLocked = !showLocked;
    redraw('dc-badges');
  },
});

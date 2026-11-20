// The ? sheet: every registered key from every lane, grouped, generated from the keys.js registry.

import { html } from '../lit.js';
import { listKeys, comboLabel } from '../keys.js';
import { openSheet } from '../ui/sheet.js';

const ORDER = ['General', 'Select', 'Edit', 'Arrange', 'Send', 'Player'];
const WHEN = { list: 'in a list', editable: 'in your playlist', player: 'in the player' };

function groups() {
  const byGroup = new Map();
  for (const k of listKeys()) {
    const g = k.group || 'General';
    if (!byGroup.has(g)) byGroup.set(g, new Map());
    const rows = byGroup.get(g);
    const shown = k.display || comboLabel(k.combo);
    const row = rows.get(k.label) || { label: k.label, combos: [], when: k.when };
    if (!row.combos.includes(shown)) row.combos.push(shown);
    rows.set(k.label, row);
  }
  const names = [...byGroup.keys()].sort((a, b) => {
    const ia = ORDER.indexOf(a);
    const ib = ORDER.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || a.localeCompare(b);
  });
  return names.map((name) => ({ name, rows: [...byGroup.get(name).values()] }));
}

function whenNote(when) {
  const w = Array.isArray(when) ? when[0] : when;
  return WHEN[w] || '';
}

export function openCheatsheet() {
  openSheet({
    title: 'Keyboard shortcuts',
    label: 'Keyboard shortcuts',
    body: () => html`<div class="ed-keys">
      <p class="ed-quiet">Letter keys work when a track list has focus: click a track first.</p>
      ${groups().map((g) => html`<section class="ed-keys-group">
        <h3 class="label">${g.name}</h3>
        <dl>${g.rows.map((r) => html`<div class="ed-keys-row">
          <dt>${r.combos.map((c, i) => html`${i ? html`<span class="ed-or">or</span>` : ''}<kbd>${c}</kbd>`)}</dt>
          <dd>${r.label}${whenNote(r.when) ? html`<span class="ed-when">${whenNote(r.when)}</span>` : ''}</dd>
        </div>`)}</dl>
      </section>`)}
    </div>`,
  });
}

// Demo mode only: act as another persona, or make a new one (POST /api/auth/login). Demo mode
// means anyone can act as anyone; the demo strip says so. Nothing here renders in Google mode.

import { html, nothing } from '../lit.js';
import { state } from '../store.js';
import * as api from '../api.js';
import { registerActions } from '../actions.js';
import { setDemoUser } from '../session.js';
import { toast } from '../ui/toast.js';
import { openSheet, closeSheet, rerenderSheet } from '../ui/sheet.js';
import { whenSheetClosed } from '../share/util.js';

let st = null; // { error, busy }

export function isDemo() {
  return state.auth.mode === 'demo';
}

/** Menu items for the account menu: the other personas, then "New persona…". */
export function personaItems() {
  if (!isDemo()) return [];
  const meId = state.auth.user && state.auth.user.id;
  const others = (state.auth.users || []).filter((u) => u && u.id !== meId).slice(0, 12);
  return [
    ...others.map((u) => ({ label: `Act as @${u.username}`, run: () => switchTo(u) })),
    { label: 'New persona…', icon: 'plus', run: () => openNewPersona() },
  ];
}

export async function switchTo(u) {
  if (!isDemo() || !u) return;
  try {
    await setDemoUser(u.id);
    toast.show(`Now acting as @${u.username}`);
  } catch (err) {
    toast.show(err.detail || 'Could not switch persona.');
  }
}

export function openNewPersona() {
  if (!isDemo()) return;
  whenSheetClosed(() => {
    st = { error: '', busy: false };
    openSheet({
      title: 'New persona',
      body: () => html`<form id="sh-persona" class="sh-form" data-action="persona-create" novalidate>
        <label class="sh-field"><span class="label">Handle</span>
          <input class="input" name="username" maxlength="20" required autocomplete="off" autocapitalize="none"
            spellcheck="false" placeholder="lowercase, 3–20 characters" /></label>
        <label class="sh-field"><span class="label">Display name</span>
          <input class="input" name="display_name" maxlength="200" autocomplete="off" /></label>
        ${st && st.error ? html`<p class="sh-err" role="alert">${st.error}</p>` : nothing}
      </form>`,
      foot: () => html`
        <button type="button" class="btn btn-quiet" data-action="sheet-close">Cancel</button>
        <button type="submit" class="btn btn-primary" form="sh-persona" ?disabled=${!!(st && st.busy)}>Create</button>`,
      onClose: () => { st = null; },
    });
  });
}

registerActions({
  'persona-create': async (form) => {
    if (!st || st.busy) return;
    const fd = new FormData(form);
    const username = String(fd.get('username') || '').trim();
    if (!username) {
      st.error = 'Pick a handle.';
      rerenderSheet();
      return;
    }
    st.busy = true;
    st.error = '';
    rerenderSheet();
    try {
      const res = await api.post('/api/auth/login', { username, display_name: String(fd.get('display_name') || '').trim() || null });
      closeSheet('done');
      await setDemoUser(res.user_id);
      const u = state.auth.user;
      toast.show(res.created ? `Created @${u ? u.username : username}` : `Now acting as @${u ? u.username : username}`);
    } catch (err) {
      if (st) {
        st.busy = false;
        st.error = err.detail || 'Could not create that persona.';
        rerenderSheet();
      }
    }
  },
});

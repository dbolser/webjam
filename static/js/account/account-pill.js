// The account slot in the top bar (docs/phase1b-shell.md §2.3, §4.4).
//
// Signed out (Google mode): a plain "Sign in" link with no ?next=, so a deliberate sign-in lands
// on my own page. Signed in: a small pill (avatar + @handle) that opens the account menu: my page,
// profile, crate, theme, handle (Google), personas (demo only), sign out (Google only).

import { html } from '../lit.js';
import { state, update } from '../store.js';
import * as api from '../api.js';
import * as slots from '../slots.js';
import { registerActions } from '../actions.js';
import { openMenu } from '../ui/menu.js';
import { avatar } from '../components/avatar.js';
import { userUrl } from '../format.js';
import { cycleTheme } from '../theme.js';
import { toast } from '../ui/toast.js';
import { openHandleSheet } from './handle-prompt.js';
import { personaItems, isDemo } from './personas.js';

function themeItem() {
  const t = state.ui.theme;
  const icon = t === 'light' ? 'sun' : t === 'dark' ? 'moon' : 'system';
  const label = t === 'light' ? 'Theme: light' : t === 'dark' ? 'Theme: dark' : 'Theme: system';
  return {
    label,
    icon,
    run: () => {
      const next = cycleTheme();
      toast.show(`Theme: ${next}`, { timeout: 1600 });
    },
  };
}

export async function signOut() {
  try {
    await api.post('/auth/logout', {}, { quiet: true });
  } catch {
    /* the session is gone either way once the cookie is cleared */
  }
  location.assign('/');
}

function menuItems() {
  const a = state.auth;
  const u = a.user;
  const items = [];
  if (u) {
    items.push(
      { label: 'Your page', href: '/' },
      { label: 'Profile', href: userUrl(u.username) },
      { label: 'Crate', icon: 'crate', run: () => update((s) => { s.ui.cratePanel = true; }, 'crate-panel') },
      'sep',
    );
  }
  items.push(themeItem());
  if (u && a.mode === 'google') items.push({ label: 'Change handle…', run: () => openHandleSheet() });
  if (isDemo()) items.push('sep', ...personaItems());
  if (u && a.mode === 'google') items.push('sep', { label: 'Sign out', run: () => signOut() });
  return items;
}

slots.register('topbar.account', () => {
  const a = state.auth;
  if (!a.mode) return null; // before bootstrap: F's fallback
  if (!a.signedIn || !a.user) {
    if (a.mode === 'demo') {
      return html`<button type="button" class="btn btn-quiet sh-pill" data-action="account-menu"
        aria-haspopup="menu" aria-expanded="false">Choose a persona</button>`;
    }
    return html`<a class="sh-signin" href="/auth/google/login" rel="external">Sign in</a>`;
  }
  const u = a.user;
  return html`<button type="button" class="sh-pill" data-action="account-menu" aria-haspopup="menu" aria-expanded="false"
      aria-label=${`Account: @${u.username}`}>
    ${avatar(u)}<span class="me-name">@${u.username}</span>
    <svg class="i sh-chev" aria-hidden="true"><use href="#i-chevron-down"></use></svg>
  </button>`;
});

registerActions({
  'account-menu': (el) => openMenu(el, menuItems(), { label: 'Account' }),
  signout: () => signOut(),
});

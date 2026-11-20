// /request-access (Google mode): a verified Google account that isn't on the invite list asks to
// join. The Google callback puts the identity in the signed session; this page only shows it and
// posts an optional note (GET/POST /api/access-request). Demo mode answers 404 -> not-found.

import { html, nothing } from '../lit.js';
import * as api from '../api.js';
import { registerActions } from '../actions.js';
import { rerender } from '../router.js';
import { toast } from '../ui/toast.js';

const MAX_NOTE = 500;
let data = { identity: null, status: null, invited: false, note: '' };
let sending = false;

export async function load() {
  data = await api.get('/api/access-request', {}, { quiet: true });
  sending = false;
}

export function title() {
  return 'Request access';
}

function signInButton(label) {
  return html`<p class="page-actions">
    <a class="btn btn-primary" href="/auth/google/login" rel="external">${label}</a>
  </p>`;
}

function body() {
  const id = data.identity;
  if (!id) {
    return html`<p class="desc">webjam is invite-only. Sign in with your Google account and you can ask to join.</p>
      ${signInButton('Sign in with Google to request access')}`;
  }
  if (data.invited) {
    return html`<p class="desc">You're in: ${id.email} is on the invite list.</p>${signInButton('Sign in with Google')}`;
  }
  if (data.status === 'pending') {
    return html`<p class="desc">Request sent. We'll email ${id.email} when you're in.</p>
      <p class="quiet-line">You can add or change a note below; it updates your request.</p>
      ${form()}`;
  }
  return html`<p class="desc">You're signed in to Google as ${id.email}, but you're not on the invite list yet.</p>
    ${data.status === 'declined'
      ? html`<p class="quiet-line">An earlier request wasn't approved. You can ask again after a week.</p>`
      : nothing}
    ${form()}`;
}

function form() {
  return html`<form class="sh-form section" data-action="access-request-send">
    <div class="sh-field">
      <label class="label" for="ar-note">A note for the people who run webjam (optional)</label>
      <textarea id="ar-note" name="note" class="input sh-textarea" maxlength=${MAX_NOTE}
        placeholder="Who you are, or who sent you" .value=${data.note || ''}></textarea>
    </div>
    <div><button type="submit" class="btn btn-primary" ?disabled=${sending}>Request access</button></div>
  </form>`;
}

export function render() {
  return html`<div class="page">
    <div class="page-head"><h1 tabindex="-1">Request access</h1></div>
    ${body()}
  </div>`;
}

registerActions({
  'access-request-send': async (form) => {
    if (sending) return;
    const note = String(new FormData(form).get('note') || '').trim().slice(0, MAX_NOTE);
    sending = true;
    rerender();
    try {
      const res = await api.post('/api/access-request', { note }, { quiet: true });
      if (res.status === 'invited') data = { ...data, invited: true };
      else {
        data = { ...data, status: 'pending', note };
        toast.show('Request sent');
      }
    } catch (err) {
      if (err.status === 401) data = { identity: null, status: null, invited: false, note: '' }; // the 30-minute window passed
      toast.show(err.detail || "Couldn't send that. Try again.");
    } finally {
      sending = false;
      rerender();
    }
  },
});

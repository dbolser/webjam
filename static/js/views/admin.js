// /admin (Google mode, admins only): pending access requests with Approve / Decline, recent
// decisions with how the "you're in" email went, and the invite list.
// GET /api/admin/access-requests; POST .../{email}/approve | /decline. Demo mode -> not-found.

import { html, nothing, repeat } from '../lit.js';
import { update } from '../store.js';
import * as api from '../api.js';
import { ApiError } from '../api.js';
import { registerActions } from '../actions.js';
import { rerender } from '../router.js';
import { toast } from '../ui/toast.js';
import { relTime, isoTs } from '../format.js';

let data = null; // the listing, or null when not allowed
let denied = ''; // why the listing isn't shown
const busy = new Set(); // emails with a click in flight

async function fetchListing() {
  try {
    data = await api.get('/api/admin/access-requests', {}, { quiet: true });
    denied = '';
    update((s) => { s.auth = { ...s.auth, pendingRequests: data.pending.length }; }, 'access-requests');
  } catch (err) {
    if (!(err instanceof ApiError) || (err.status !== 401 && err.status !== 403)) throw err;
    data = null;
    denied = err.status === 401 ? 'Sign in with Google to see this page.' : "This page is only for webjam's admins.";
  }
}

export async function load() {
  busy.clear();
  await fetchListing();
}

export function title() {
  return 'Access requests';
}

function when(ts) {
  return ts ? html`<time datetime=${isoTs(ts)}>${relTime(ts)}</time>` : nothing;
}

function who(r) {
  return r.name ? html`${r.name} <span class="muted">${r.email}</span>` : r.email;
}

function pendingCard(r) {
  const off = busy.has(r.email);
  return html`<article class="card" data-email=${r.email}>
    <div class="card-head"><span class="card-title">${who(r)}</span></div>
    <div class="card-meta"><span>asked ${when(r.created_at)}</span></div>
    ${r.note ? html`<p class="desc">${r.note}</p>` : nothing}
    <p class="page-actions">
      <button type="button" class="btn btn-primary" data-action="access-approve" ?disabled=${off}>Approve</button>
      <button type="button" class="btn btn-danger" data-action="access-decline" ?disabled=${off}>Decline</button>
    </p>
  </article>`;
}

const MAIL_LABEL = { sending: 'email sending', sent: 'email sent', failed: 'email failed', 'not configured': 'email not configured' };

function decidedCard(r) {
  // Resend a "you're in" email that didn't go out (only to people who are still invited).
  const resend = r.status === 'approved' && data.invites.includes(r.email)
    && (r.notify_status === 'failed' || r.notify_status === 'not configured' || r.notify_status === 'sending');
  return html`<article class="card" data-email=${r.email}>
    <div class="card-head"><span class="card-title">${who(r)}</span></div>
    <div class="card-meta">
      <span>${r.status} ${when(r.decided_at)}</span>
      ${r.status === 'approved' && r.notify_status ? html`<span>${MAIL_LABEL[r.notify_status] || r.notify_status}</span>` : nothing}
    </div>
    ${resend
      ? html`<p class="page-actions"><button type="button" class="btn" data-action="access-resend"
          ?disabled=${busy.has(r.email)}>Resend email</button></p>`
      : nothing}
  </article>`;
}

export function render() {
  if (!data) {
    return html`<div class="page not-found">
      <h1 tabindex="-1">Access requests</h1>
      <p>${denied}</p>
      <p><a href="/">Go to the front page</a></p>
    </div>`;
  }
  return html`<div class="page">
    <div class="page-head"><h1 tabindex="-1">Access requests</h1></div>
    ${data.invites_managed_by_env
      ? html`<p class="notice">Invites are managed by WEBJAM_INVITE_EMAILS, so approving here is switched off.
          Remove that setting to approve from this page.</p>`
      : nothing}
    <section class="section" aria-labelledby="ad-h-pending">
      <h2 class="label" id="ad-h-pending">Waiting · <span class="num">${data.pending.length}</span></h2>
      ${data.pending.length
        ? repeat(data.pending, (r) => r.email, pendingCard)
        : html`<p class="empty">Nobody is waiting.</p>`}
    </section>
    ${data.decided.length
      ? html`<section class="section" aria-labelledby="ad-h-decided">
          <h2 class="label" id="ad-h-decided">Recent decisions</h2>
          ${repeat(data.decided, (r) => r.email, decidedCard)}
        </section>`
      : nothing}
    <section class="section" aria-labelledby="ad-h-invites">
      <h2 class="label" id="ad-h-invites">Invite list · <span class="num">${data.invites.length}</span></h2>
      <ul>${data.invites.map((e) => html`<li>${e}</li>`)}</ul>
    </section>
  </div>`;
}

async function decide(el, verb) {
  const card = el.closest('[data-email]');
  const email = card && card.dataset.email;
  if (!email || busy.has(email)) return;
  busy.add(email);
  rerender();
  try {
    const res = await api.post(`/api/admin/access-requests/${encodeURIComponent(email)}/${verb}`, {}, { quiet: true });
    const mail = res.request && res.request.notify_status;
    if (verb === 'decline') toast.show(`Declined ${email}`);
    else if (verb === 'resend') toast.show(mail === 'sent' ? `Emailed ${email}` : `Couldn't email ${email} (${MAIL_LABEL[mail] || 'no email'})`);
    else toast.show(mail === 'sent' ? `Approved ${email} and emailed them` : `Approved ${email} (${MAIL_LABEL[mail] || 'no email'})`);
    await fetchListing();
  } catch (err) {
    toast.show(err.detail || "Couldn't do that. Try again.");
  } finally {
    busy.delete(email);
    rerender();
  }
}

registerActions({
  'access-approve': (el) => decide(el, 'approve'),
  'access-decline': (el) => decide(el, 'decline'),
  'access-resend': (el) => decide(el, 'resend'),
});

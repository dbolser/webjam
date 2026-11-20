"""Access requests (Google sign-in mode): someone with a verified Google account that is not on
the invite list asks to join, an admin approves or declines from /admin, and email tells each side.

The requester's identity only ever comes from the signed session, which the Google callback fills
with {email, sub, name, exp} for a verified but uninvited account. Admins are the signed-in users
whose Google email is listed in WEBJAM_ADMIN_EMAILS. Approving adds an invite; it is refused while
WEBJAM_INVITE_EMAILS is set, because that list replaces the invite table at every startup.
"""

import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, Request

from app import mailer
from app.invite import add_invite, is_invited, list_invites, normalize_email

IDENTITY_KEY = "access_request"
IDENTITY_TTL = 30 * 60
MAX_NOTE = 500
MAX_PENDING = 200
DECLINE_COOLDOWN = timedelta(days=7)
RECENT_DECIDED = 50
TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(dt: datetime) -> str:
    return dt.strftime(TS_FORMAT)


def _parse(ts: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.strptime(ts or "", TS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# --- the pending identity in the session -------------------------------------------------------

def remember_identity(request: Request, email: str, sub: str, name: str) -> None:
    request.session[IDENTITY_KEY] = {
        "email": email,
        "sub": str(sub),
        "name": (name or "").strip()[:200],
        "exp": int(time.time()) + IDENTITY_TTL,
    }


def session_identity(request: Request) -> Optional[Dict[str, str]]:
    raw = request.session.get(IDENTITY_KEY)
    ok = (
        isinstance(raw, dict)
        and isinstance(raw.get("email"), str)
        and isinstance(raw.get("sub"), str)
        and isinstance(raw.get("exp"), int)
        and raw["exp"] > time.time()
    )
    if not ok:
        if raw is not None:
            request.session.pop(IDENTITY_KEY, None)
        return None
    return {"email": raw["email"], "sub": raw["sub"], "name": str(raw.get("name") or "")}


# --- admins ------------------------------------------------------------------------------------

def admin_emails() -> List[str]:
    raw = os.environ.get("WEBJAM_ADMIN_EMAILS", "")
    return sorted({e.strip().lower() for e in raw.split(",") if e.strip()})


def is_admin(conn: sqlite3.Connection, user_id: Optional[int]) -> bool:
    admins = admin_emails()
    if not user_id or not admins:
        return False
    row = conn.execute("SELECT email FROM google_accounts WHERE user_id = ?", (user_id,)).fetchone()
    return bool(row and (row["email"] or "").strip().lower() in admins)


def invites_managed_by_env() -> bool:
    return bool(os.environ.get("WEBJAM_INVITE_EMAILS", "").strip())


def pending_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM access_requests WHERE status = 'pending'").fetchone()[0]


# --- requests ----------------------------------------------------------------------------------

def get_request(conn: sqlite3.Connection, email: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM access_requests WHERE email = ?", (email,)).fetchone()
    return dict(row) if row else None


def clean_note(note: str) -> str:
    clean = (note or "").strip()
    if len(clean) > MAX_NOTE:
        raise HTTPException(status_code=400, detail=f"Keep the note under {MAX_NOTE} characters.")
    return clean


def submit_request(conn: sqlite3.Connection, ident: Dict[str, str], note: str) -> Dict[str, Any]:
    """Create or refresh the request for a session identity.

    Returns {"status", "notify_admins"}: status is 'invited' (just sign in) or 'pending'.
    """
    email = ident["email"]
    note = clean_note(note)
    if is_invited(conn, email):
        return {"status": "invited", "notify_admins": False}
    now = _now()
    row = get_request(conn, email)
    if row and row["status"] == "pending":
        # Editing the note of an open request updates it quietly: admins are emailed only when a
        # request opens, so a requester cannot use the site's mail account to repeat themselves.
        conn.execute(
            "UPDATE access_requests SET note = ?, name = ?, google_sub = ? WHERE email = ?",
            (note, ident["name"], ident["sub"], email),
        )
        conn.commit()
        return {"status": "pending", "notify_admins": False}
    if row and row["status"] == "declined":
        decided = _parse(row["decided_at"]) or now
        if now - decided < DECLINE_COOLDOWN:
            until = decided + DECLINE_COOLDOWN
            again = f"{until.day} {until:%B %Y}"
            raise HTTPException(
                status_code=409,
                detail=f"Your last request wasn't approved. You can ask again from {again}.",
            )
    if pending_count(conn) >= MAX_PENDING:
        raise HTTPException(status_code=429, detail="There are too many open requests right now. Try again later.")
    # New, or a fresh start after a decline cooldown (or an approval whose invite was removed since).
    conn.execute(
        """
        INSERT INTO access_requests (email, google_sub, name, note, status, created_at, admins_notified_at)
        VALUES (?, ?, ?, ?, 'pending', ?, ?)
        ON CONFLICT(email) DO UPDATE SET
            google_sub = excluded.google_sub, name = excluded.name, note = excluded.note,
            status = 'pending', created_at = excluded.created_at,
            admins_notified_at = excluded.admins_notified_at,
            decided_at = NULL, decided_by_user_id = NULL, notify_status = NULL
        """,
        (email, ident["sub"], ident["name"], note, _ts(now), _ts(now)),
    )
    conn.commit()
    return {"status": "pending", "notify_admins": True}


def notify_admins(base_url: str, email: str, name: str, note: str) -> None:
    """Email every admin about a new request. Blocking: run it as a background task."""
    who = f"{name} <{email}>" if name else email
    body = (
        f"Someone asked to join webjam. Approve or decline at {base_url}/admin\n\n"
        "The name and note below were written by the requester, not by webjam; "
        "don't follow links in them.\n\n"
        f"{who} asked to join webjam.\n"
    )
    if note:
        body += f"\nTheir note:\n{note}\n"
    for admin in admin_emails():
        mailer.send_mail(admin, "webjam: new access request", body)


def list_requests(conn: sqlite3.Connection) -> Dict[str, Any]:
    cols = "email, name, note, status, created_at, decided_at, notify_status"
    pending = conn.execute(
        f"SELECT {cols} FROM access_requests WHERE status = 'pending' ORDER BY created_at, email"
    ).fetchall()
    decided = conn.execute(
        f"SELECT {cols} FROM access_requests WHERE status != 'pending' ORDER BY decided_at DESC, email LIMIT ?",
        (RECENT_DECIDED,),
    ).fetchall()
    return {
        "pending": [dict(r) for r in pending],
        "decided": [dict(r) for r in decided],
        "invites": list_invites(conn),
        "invites_managed_by_env": invites_managed_by_env(),
    }


def _require_row(conn: sqlite3.Connection, raw_email: str) -> Dict[str, Any]:
    try:
        email = normalize_email(raw_email)
    except ValueError:
        raise HTTPException(status_code=404, detail="No request from that address.")
    row = get_request(conn, email)
    if not row:
        raise HTTPException(status_code=404, detail="No request from that address.")
    return row


def approve(conn: sqlite3.Connection, raw_email: str, admin_uid: int) -> Tuple[Dict[str, Any], bool]:
    """Invite the requester and mark the request approved.

    Returns (row, newly_approved); the caller sends the email when newly_approved.
    """
    if invites_managed_by_env():
        raise HTTPException(
            status_code=409,
            detail="Invites are managed by WEBJAM_INVITE_EMAILS; remove it to approve from the admin page.",
        )
    row = _require_row(conn, raw_email)
    if row["status"] == "approved" and is_invited(conn, row["email"]):
        return row, False  # a second click: no second email
    add_invite(conn, row["email"], admin_uid)
    conn.execute(
        "UPDATE access_requests SET status = 'approved', decided_at = ?, decided_by_user_id = ?, "
        "notify_status = NULL WHERE email = ?",
        (_ts(_now()), admin_uid, row["email"]),
    )
    conn.commit()
    return get_request(conn, row["email"]), True


def welcome_mail(base_url: str, row: Dict[str, Any]) -> str:
    """Send the "you're in" email. Blocking: call it in a thread."""
    hello = f"Hi {row['name']},\n\n" if row.get("name") else "Hi,\n\n"
    body = (
        f"{hello}You're in: your request to join webjam was approved.\n\n"
        f"Sign in at {base_url} with this Google account ({row['email']}).\n"
    )
    return mailer.send_mail(row["email"], "You're in on webjam", body)


def record_notify(conn: sqlite3.Connection, email: str, status: str) -> None:
    conn.execute("UPDATE access_requests SET notify_status = ? WHERE email = ?", (status, email))
    conn.commit()


def decline(conn: sqlite3.Connection, raw_email: str, admin_uid: int) -> Dict[str, Any]:
    row = _require_row(conn, raw_email)
    if row["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"That request is already {row['status']}.")
    conn.execute(
        "UPDATE access_requests SET status = 'declined', decided_at = ?, decided_by_user_id = ? WHERE email = ?",
        (_ts(_now()), admin_uid, row["email"]),
    )
    conn.commit()
    return get_request(conn, row["email"])

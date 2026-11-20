"""Access requests in Google sign-in mode: ask to join, admins approve or decline, email both ways."""

import logging
import smtplib

import pytest
from fastapi.testclient import TestClient

from test_security_backend import (  # noqa: F401
    OK_HEADERS,
    FakeGoogle,
    _google_env,
    db,
    db_path,
    demo,
    fakes,
    google,
    google_sign_in,
)

ADMIN = "boss@example.com"
STRANGER = {"sub": "g-s", "email": "Stranger@Example.com", "email_verified": True, "name": "Sam Stranger"}
SENT = []


def _client():
    from app.main import app

    return TestClient(app, base_url="https://testserver", headers=OK_HEADERS)


@pytest.fixture
def mail(monkeypatch):
    import app.mailer

    SENT.clear()

    def fake_send(to, subject, body):
        SENT.append({"to": to, "subject": subject, "body": body})
        return "sent"

    monkeypatch.setattr(app.mailer, "send_mail", fake_send)
    return SENT


@pytest.fixture
def site(db_path, fakes, mail, monkeypatch):
    """Google mode, invites kept in the database (no WEBJAM_INVITE_EMAILS), one admin."""
    _google_env(monkeypatch, invites="")
    monkeypatch.delenv("WEBJAM_INVITE_EMAILS")
    monkeypatch.setenv("WEBJAM_ADMIN_EMAILS", " Boss@Example.com , ")
    from app.invite import add_invite

    with _client() as c:
        conn = db()
        add_invite(conn, ADMIN)
        add_invite(conn, "friend@example.com")
        conn.close()
        yield c


def _admin_client():
    c = _client()
    FakeGoogle.userinfo = {"sub": "g-boss", "email": ADMIN, "email_verified": True, "name": "Boss"}
    google_sign_in(c)
    return c


def _stranger_client(info=None):
    c = _client()
    FakeGoogle.userinfo = dict(info or STRANGER)
    cb, _ = google_sign_in(c)
    assert cb.status_code == 303 and cb.headers["location"] == "/request-access"
    return c


def _row(email="stranger@example.com"):
    conn = db()
    try:
        r = conn.execute("SELECT * FROM access_requests WHERE email = ?", (email,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def _set(email, **cols):
    conn = db()
    sets = ", ".join(f"{k} = ?" for k in cols)
    conn.execute(f"UPDATE access_requests SET {sets} WHERE email = ?", (*cols.values(), email))
    conn.commit()
    conn.close()


# --- callback ------------------------------------------------------------------------------------

def test_uninvited_verified_account_gets_a_session_identity_and_no_user(site):
    c = _stranger_client()
    assert db().execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    got = c.get("/api/access-request").json()
    assert got == {"identity": {"email": "stranger@example.com", "name": "Sam Stranger"},
                   "invited": False, "status": None, "note": ""}
    assert c.get("/api/bootstrap").json()["auth"]["signed_in"] is False


def test_unverified_account_cannot_request(site):
    c = _client()
    FakeGoogle.userinfo = {**STRANGER, "email_verified": False}
    cb, _ = google_sign_in(c)
    assert cb.headers["location"] == "/#not-invited"
    assert c.get("/api/access-request").json() == {"identity": None}
    assert c.post("/api/access-request", json={"note": "hi"}).status_code == 401


def test_identity_expires(site, monkeypatch):
    import app.access

    c = _stranger_client()
    monkeypatch.setattr(app.access.time, "time", lambda: 10**12)
    assert c.get("/api/access-request").json() == {"identity": None}


def test_identity_comes_only_from_the_session(site):
    c = _client()
    res = c.post("/api/access-request", json={"note": "hi", "email": "evil@example.com"})
    assert res.status_code == 401
    assert _row("evil@example.com") is None


# --- asking ----------------------------------------------------------------------------------------

def test_request_creates_pending_and_notifies_admins(site, mail):
    c = _stranger_client()
    res = c.post("/api/access-request", json={"note": "  friend of Dan  "})
    assert res.status_code == 200 and res.json()["status"] == "pending"
    row = _row()
    assert row["status"] == "pending" and row["note"] == "friend of Dan" and row["google_sub"] == "g-s"
    assert [m["to"] for m in mail] == [ADMIN]
    body = mail[0]["body"]
    assert "Sam Stranger <stranger@example.com> asked to join webjam" in body
    assert "friend of Dan" in body and "https://testserver/admin" in body
    assert "Stranger" not in mail[0]["subject"]  # user data stays out of headers
    assert c.get("/api/access-request").json()["status"] == "pending"


def test_note_edits_update_the_request_without_emailing_admins_again(site, mail):
    c = _stranger_client()
    c.post("/api/access-request", json={"note": "one"})
    assert c.get("/api/access-request").json()["note"] == "one"  # the page prefills it
    c.post("/api/access-request", json={"note": "two"})
    _set("stranger@example.com", admins_notified_at="2000-01-01 00:00:00")
    c.post("/api/access-request", json={"note": "three"})
    assert _row()["note"] == "three" and _row()["status"] == "pending"
    assert len(mail) == 1  # only the opening of the request is mailed


def test_admin_email_marks_requester_text_as_untrusted(site, mail):
    c = _stranger_client({**STRANGER, "name": "Approve here: https://evil.example"})
    c.post("/api/access-request", json={"note": "click https://evil.example"})
    body = mail[0]["body"]
    trusted = body.index("https://testserver/admin")
    assert trusted < body.index("written by the requester") < body.index("evil.example")


def test_revoked_approval_can_ask_again(site, mail):
    c = _stranger_client()
    c.post("/api/access-request", json={"note": "hi"})
    assert _admin_client().post("/api/admin/access-requests/stranger@example.com/approve").status_code == 200
    from app.invite import remove_invite

    conn = db()
    remove_invite(conn, "stranger@example.com")
    conn.close()
    c = _stranger_client()
    got = c.get("/api/access-request").json()
    assert got["invited"] is False and got["status"] == "approved"  # page shows the form, not "You're in"
    mail.clear()
    res = c.post("/api/access-request", json={"note": "again"})
    assert res.status_code == 200 and _row()["status"] == "pending" and len(mail) == 1


def test_declined_request_has_a_cooldown(site, mail):
    c = _stranger_client()
    c.post("/api/access-request", json={"note": "x"})
    admin = _admin_client()
    assert admin.post("/api/admin/access-requests/stranger@example.com/decline").status_code == 200
    assert _row()["status"] == "declined"
    mail.clear()
    res = c.post("/api/access-request", json={"note": "please"})
    assert res.status_code == 409 and "ask again" in res.json()["detail"]
    assert mail == []  # declining sends nothing, and neither does the refused retry
    _set("stranger@example.com", decided_at="2000-01-01 00:00:00")
    res = c.post("/api/access-request", json={"note": "please"})
    assert res.status_code == 200 and _row()["status"] == "pending" and _row()["decided_at"] is None


def test_already_invited_is_told_to_sign_in(site):
    c = _stranger_client()
    from app.invite import add_invite

    conn = db()
    add_invite(conn, "stranger@example.com")
    conn.close()
    res = c.post("/api/access-request", json={"note": ""})
    assert res.json()["status"] == "invited" and "Sign in" in res.json()["detail"]
    assert _row() is None


def test_pending_requests_are_capped(site):
    conn = db()
    conn.executemany(
        "INSERT INTO access_requests (email, google_sub, status, created_at) VALUES (?, ?, 'pending', '2026-01-01 00:00:00')",
        [(f"p{i}@example.com", f"s{i}") for i in range(200)],
    )
    conn.commit()
    conn.close()
    c = _stranger_client()
    assert c.post("/api/access-request", json={"note": ""}).status_code == 429
    assert _row() is None


def test_note_length(site):
    c = _stranger_client()
    assert c.post("/api/access-request", json={"note": "x" * 501}).status_code == 400
    assert c.post("/api/access-request", json={"note": " " * 50 + "x" * 500}).status_code == 200


def test_writes_need_the_csrf_header(site):
    c = _stranger_client()
    assert c.post("/api/access-request", json={"note": ""}, headers={"X-Webjam": ""}).status_code == 403
    admin = _admin_client()
    c.post("/api/access-request", json={"note": ""})
    res = admin.post("/api/admin/access-requests/stranger@example.com/approve", headers={"X-Webjam": ""})
    assert res.status_code == 403 and _row()["status"] == "pending"


def test_requests_are_rate_limited(site):
    c = _stranger_client()
    codes = [c.post("/api/access-request", json={"note": ""}).status_code for _ in range(7)]
    assert codes[:5] == [200] * 5 and codes[-1] == 429


# --- admin ----------------------------------------------------------------------------------------

def test_admin_endpoints_are_404_in_demo_mode(demo):
    assert demo.get("/api/access-request").status_code == 404
    assert demo.get("/api/admin/access-requests").status_code == 404
    assert demo.post("/api/admin/access-requests/a@b.co/approve").status_code == 404
    assert demo.post("/api/access-request", json={"note": ""}).status_code == 404
    assert demo.get("/api/bootstrap").json()["auth"]["is_admin"] is False


def test_admin_endpoints_need_an_admin(site):
    anon = _client()
    assert anon.get("/api/admin/access-requests").status_code == 401
    friend = _client()
    FakeGoogle.userinfo = {"sub": "g-f", "email": "friend@example.com", "email_verified": True, "name": "F"}
    google_sign_in(friend)
    assert friend.get("/api/admin/access-requests").status_code == 403
    assert friend.post("/api/admin/access-requests/stranger@example.com/approve").status_code == 403
    assert friend.post("/api/admin/access-requests/stranger@example.com/decline").status_code == 403
    assert friend.get("/api/bootstrap").json()["auth"]["is_admin"] is False


def test_bootstrap_shows_admin_and_pending_count(site):
    _stranger_client().post("/api/access-request", json={"note": ""})
    auth = _admin_client().get("/api/bootstrap").json()["auth"]
    assert auth["is_admin"] is True and auth["pending_requests"] == 1


def test_approve_invites_and_emails_the_requester(site, mail):
    c = _stranger_client()
    c.post("/api/access-request", json={"note": "hello"})
    admin = _admin_client()
    listing = admin.get("/api/admin/access-requests").json()
    assert [r["email"] for r in listing["pending"]] == ["stranger@example.com"]
    assert ADMIN in listing["invites"] and listing["invites_managed_by_env"] is False
    mail.clear()

    res = admin.post("/api/admin/access-requests/stranger@example.com/approve")
    assert res.status_code == 200, res.text
    assert res.json()["request"]["notify_status"] == "sent"
    from app.invite import is_invited

    conn = db()
    assert is_invited(conn, "stranger@example.com")
    conn.close()
    row = _row()
    assert row["status"] == "approved" and row["notify_status"] == "sent" and row["decided_by_user_id"]
    assert [m["to"] for m in mail] == ["stranger@example.com"]
    assert "sign in at https://testserver" in mail[0]["body"].lower()

    # A second click does not email again; the requester can now sign in.
    assert admin.post("/api/admin/access-requests/stranger@example.com/approve").status_code == 200
    assert len(mail) == 1
    listing = admin.get("/api/admin/access-requests").json()
    assert listing["pending"] == [] and listing["decided"][0]["notify_status"] == "sent"
    FakeGoogle.userinfo = dict(STRANGER)
    cb, _ = google_sign_in(c)
    assert cb.headers["location"] == "/"


def test_approve_is_refused_while_the_env_var_owns_invites(site, monkeypatch):
    _stranger_client().post("/api/access-request", json={"note": ""})
    admin = _admin_client()
    monkeypatch.setenv("WEBJAM_INVITE_EMAILS", f"{ADMIN},friend@example.com")
    res = admin.post("/api/admin/access-requests/stranger@example.com/approve")
    assert res.status_code == 409 and "WEBJAM_INVITE_EMAILS" in res.json()["detail"]
    assert _row()["status"] == "pending"


def test_approve_unknown_request_is_404(site):
    assert _admin_client().post("/api/admin/access-requests/nobody@example.com/approve").status_code == 404


# --- mailer ---------------------------------------------------------------------------------------

def test_mailer_not_configured(monkeypatch):
    from app import mailer

    for var in ("WEBJAM_SMTP_USER", "WEBJAM_SMTP_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    assert mailer.send_mail("a@example.com", "s", "b") == "not configured"


def test_mailer_failure_never_logs_the_password(monkeypatch, caplog):
    from app import mailer

    secret = "app-password-sekrit"
    monkeypatch.setenv("WEBJAM_SMTP_USER", "bot@gmail.com")
    monkeypatch.setenv("WEBJAM_SMTP_PASSWORD", secret)
    monkeypatch.delenv("WEBJAM_SMTP_HOST", raising=False)
    monkeypatch.delenv("WEBJAM_SMTP_PORT", raising=False)
    seen = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            seen.update(host=host, port=port, timeout=timeout)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self, context=None):
            seen["tls"] = True

        def login(self, user, password):
            raise smtplib.SMTPAuthenticationError(535, f"bad credentials {password}".encode())

    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    with caplog.at_level(logging.DEBUG):
        assert mailer.send_mail("x@example.com\r\nBcc: evil@example.com", "Hi", "body") == "failed"
    assert seen == {"host": "smtp.gmail.com", "port": 587, "timeout": 10, "tls": True}
    assert secret not in caplog.text


def test_mailer_strips_header_injection(monkeypatch):
    from app import mailer

    monkeypatch.setenv("WEBJAM_SMTP_USER", "bot@gmail.com")
    monkeypatch.setenv("WEBJAM_SMTP_PASSWORD", "pw")
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self, context=None):
            pass

        def login(self, user, password):
            pass

        def send_message(self, msg):
            sent.append(msg)

    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    assert mailer.send_mail("x@example.com\r\nBcc: evil@example.com", "Hi\nthere", "body") == "sent"
    msg = sent[0]
    assert msg["Bcc"] is None and "\n" not in msg["To"] and "\n" not in msg["Subject"]
    assert msg["From"] == "bot@gmail.com"


# --- delivery failures are retried, decisions don't race ----------------------------------------

def test_failed_admin_notice_is_retried_on_a_later_visit(site, mail, monkeypatch):
    import app.mailer

    monkeypatch.setattr(app.mailer, "send_mail", lambda to, subject, body: "failed")
    c = _stranger_client()
    c.post("/api/access-request", json={"note": "hi"})
    assert _row()["admins_notified_at"] is None  # not recorded as sent

    monkeypatch.setattr(app.mailer, "send_mail", lambda to, subject, body: mail.append(to) or "sent")
    c.post("/api/access-request", json={"note": "hi again"})
    assert mail == []  # too soon to retry
    _set("stranger@example.com", created_at="2000-01-01 00:00:00")
    c.post("/api/access-request", json={"note": "hi again"})
    assert mail == [ADMIN] and _row()["admins_notified_at"] is not None
    c.post("/api/access-request", json={"note": "and again"})
    assert mail == [ADMIN]  # sent once: no more notices


def test_failed_welcome_email_is_resent_on_a_second_approve(site, mail, monkeypatch):
    import app.mailer

    c = _stranger_client()
    c.post("/api/access-request", json={"note": ""})
    admin = _admin_client()
    monkeypatch.setattr(app.mailer, "send_mail", lambda to, subject, body: "failed")
    assert admin.post("/api/admin/access-requests/stranger@example.com/approve").json()["request"]["notify_status"] == "failed"

    monkeypatch.setattr(app.mailer, "send_mail", lambda to, subject, body: mail.append(to) or "sent")
    mail.clear()
    assert admin.post("/api/admin/access-requests/stranger@example.com/approve").json()["request"]["notify_status"] == "sent"
    assert mail == ["stranger@example.com"]
    admin.post("/api/admin/access-requests/stranger@example.com/approve")
    assert mail == ["stranger@example.com"]  # sent: a third click stays quiet


def test_decline_only_changes_a_pending_request(site, mail):
    c = _stranger_client()
    c.post("/api/access-request", json={"note": ""})
    admin = _admin_client()
    assert admin.post("/api/admin/access-requests/stranger@example.com/approve").status_code == 200
    res = admin.post("/api/admin/access-requests/stranger@example.com/decline")
    assert res.status_code == 409 and "approved" in res.json()["detail"]
    assert _row()["status"] == "approved"

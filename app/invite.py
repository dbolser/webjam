"""Invite list for Google sign-in (Webjam is for friends by invite).

Anyone with a link can listen, but only Google accounts whose verified email is
in the ``invites`` table can sign in. Seed it at startup with
``WEBJAM_INVITE_EMAILS=a@example.com,b@example.com`` or manage it from a shell:

    python -m app.invite add someone@example.com
    python -m app.invite remove someone@example.com
    python -m app.invite list

Removing an invite blocks future sign-ins for that address and signs that
person out everywhere (their open sessions stop working on the next request).

When WEBJAM_INVITE_EMAILS is set it is the complete invite list: at startup,
addresses that are not in it are removed (and signed out). Leave it unset to
manage invites only with ``python -m app.invite``.
"""

import logging
import os
import re
import sqlite3
import sys
import time
from typing import Iterable, List, Optional

logger = logging.getLogger("webjam.invite")

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(email: str) -> str:
    clean = (email or "").strip().lower()
    if len(clean) > 254 or not EMAIL_PATTERN.match(clean):
        raise ValueError(f"Not a valid email address: {email!r}")
    return clean


def add_invite(conn: sqlite3.Connection, email: str, invited_by_user_id: Optional[int] = None) -> bool:
    """Add an invite. Returns True if it was new."""
    clean = normalize_email(email)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO invites (email, invited_by_user_id, created_at) VALUES (?, ?, ?)",
        (clean, invited_by_user_id, time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())),
    )
    conn.commit()
    return cur.rowcount > 0


def remove_invite(conn: sqlite3.Connection, email: str) -> bool:
    """Remove an invite and revoke every session of the account(s) signed in with it."""
    clean = normalize_email(email)
    cur = conn.cursor()
    cur.execute("DELETE FROM invites WHERE email = ?", (clean,))
    removed = cur.rowcount > 0
    cur.execute(
        "UPDATE users SET session_epoch = session_epoch + 1 "
        "WHERE id IN (SELECT user_id FROM google_accounts WHERE email = ?)",
        (clean,),
    )
    conn.commit()
    return removed


def list_invites(conn: sqlite3.Connection) -> List[str]:
    return [r[0] for r in conn.execute("SELECT email FROM invites ORDER BY email").fetchall()]


def is_invited(conn: sqlite3.Connection, email: str) -> bool:
    try:
        clean = normalize_email(email)
    except ValueError:
        return False
    return conn.execute("SELECT 1 FROM invites WHERE email = ?", (clean,)).fetchone() is not None


def parse_invite_env(value: Optional[str]) -> List[str]:
    return [e.strip() for e in (value or "").split(",") if e.strip()]


def seed_invites_from_env(conn: sqlite3.Connection, emails: Optional[Iterable[str]] = None) -> int:
    """Make the invite list match WEBJAM_INVITE_EMAILS when it is set.

    Adds every listed address and removes (and signs out) any invite that is not
    listed. With the variable unset nothing changes. Invalid entries fail startup
    loudly. Returns how many invites were added.
    """
    if emails is None:
        raw = os.environ.get("WEBJAM_INVITE_EMAILS")
        if raw is None or not raw.strip():
            return 0
        emails = parse_invite_env(raw)
    wanted = [normalize_email(e) for e in emails]
    added = sum(1 for e in wanted if add_invite(conn, e))
    for stale in sorted(set(list_invites(conn)) - set(wanted)):
        remove_invite(conn, stale)
        logger.warning("Removed invite not listed in WEBJAM_INVITE_EMAILS (signed out): %s", stale)
    return added


def main(argv: Optional[List[str]] = None) -> int:
    from app.database import get_connection, init_db

    args = list(sys.argv[1:] if argv is None else argv)
    usage = "usage: python -m app.invite add|remove EMAIL... | list"
    if not args or args[0] not in ("add", "remove", "list") or (args[0] != "list" and len(args) < 2):
        print(usage, file=sys.stderr)
        return 2

    init_db()
    conn = get_connection()
    try:
        if args[0] == "list":
            for email in list_invites(conn):
                print(email)
            return 0
        for email in args[1:]:
            try:
                if args[0] == "add":
                    print(f"{'invited' if add_invite(conn, email) else 'already invited'}: {normalize_email(email)}")
                else:
                    print(f"{'removed' if remove_invite(conn, email) else 'not invited'}: {normalize_email(email)}")
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 1
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

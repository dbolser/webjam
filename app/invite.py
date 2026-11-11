"""Invite list for Google sign-in (Webjam is for friends by invite).

Anyone with a link can listen, but only Google accounts whose verified email is
in the ``invites`` table can sign in. Seed it at startup with
``WEBJAM_INVITE_EMAILS=a@example.com,b@example.com`` or manage it from a shell:

    python -m app.invite add someone@example.com
    python -m app.invite remove someone@example.com
    python -m app.invite list

Removing an invite blocks future sign-ins for that address.
"""

import os
import re
import sqlite3
import sys
import time
from typing import Iterable, List, Optional

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
    cur = conn.cursor()
    cur.execute("DELETE FROM invites WHERE email = ?", (normalize_email(email),))
    conn.commit()
    return cur.rowcount > 0


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
    """Add every address in WEBJAM_INVITE_EMAILS. Invalid entries fail startup loudly."""
    if emails is None:
        emails = parse_invite_env(os.environ.get("WEBJAM_INVITE_EMAILS"))
    return sum(1 for e in emails if add_invite(conn, e))


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

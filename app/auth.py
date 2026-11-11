"""Auth mode, Google sign-in (OAuth 2.0 authorization-code flow + PKCE) and session helpers.

Auth mode is chosen explicitly with ``WEBJAM_AUTH_MODE=google|demo`` and is fixed
when the app starts (it is never re-read per request):

* ``google`` -- invite-only Google sign-in. Startup fails unless GOOGLE_CLIENT_ID,
  GOOGLE_CLIENT_SECRET, WEBJAM_SESSION_SECRET (32+ chars) and WEBJAM_BASE_URL are
  all set. The acting user always comes from the signed session cookie; any
  user_id the client sends is ignored. Only Google accounts whose *verified*
  email is in the ``invites`` table can sign in (seed it with
  WEBJAM_INVITE_EMAILS=a@x.com,b@y.com or ``python -m app.invite add a@x.com``).
* ``demo`` -- the original persona switcher for local development. The client
  chooses the user and the password field is ignored entirely: anyone can act
  as anyone. Demo mode is only the default when WEBJAM_AUTH_MODE is unset AND no
  Google, session-secret or base-URL variables are set. It refuses to start on a
  database that already holds Google-linked accounts or with a WEBJAM_BASE_URL
  that is not localhost, and it only answers requests from the loopback interface
  (set WEBJAM_DEMO_ALLOW_REMOTE=1 to serve a trusted LAN on purpose).

Sessions are bound to the database they were issued for (a random install id kept
in the ``meta`` table), so wiping the database invalidates every old cookie. When
restoring an older *backup*, also rotate WEBJAM_SESSION_SECRET: the backup's
session epochs are older than the cookies that were revoked after it was taken.
"""

import base64
import hashlib
import logging
import os
import re
import secrets
import sqlite3
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.database import get_connection, get_db
from app.invite import is_invited
from app.ops import ensure_crate

logger = logging.getLogger("webjam.auth")

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
YOUTUBE_API = "https://www.googleapis.com/youtube/v3"
# Sign-in asks for identity only. YouTube access (youtube.readonly) will be requested
# incrementally the first time someone opens "My YouTube" (Phase 4).
SCOPES = "openid email profile"
YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"

SESSION_MAX_AGE = 60 * 60 * 24 * 14  # 14 days, refreshed on every response (sliding)
MIN_SECRET_LEN = 32

HANDLE_PATTERN = re.compile(r"^[a-z0-9_]{3,20}$")
RESERVED_HANDLES = {"guest", "admin", "root", "webjam", "system", "support", "demo", "anonymous"}

AVATAR_PALETTE = ["#1f5f5b", "#8c3b2b", "#3d4e81", "#6b4226", "#4a6b2c", "#7a3b69"]


class AuthConfigError(RuntimeError):
    """Raised at startup when the auth environment is missing or inconsistent."""


@dataclass(frozen=True)
class AuthConfig:
    mode: str  # 'google' | 'demo'
    session_secret: str
    base_url: str  # '' in demo mode when WEBJAM_BASE_URL is unset
    https_only: bool


_config: Optional[AuthConfig] = None


def _client_id() -> str:
    return os.environ.get("GOOGLE_CLIENT_ID", "").strip()


def _client_secret() -> str:
    return os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()


LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")


def load_config_from_env() -> AuthConfig:
    """Read and validate the auth environment. Raises AuthConfigError (fail closed)."""
    raw_mode = os.environ.get("WEBJAM_AUTH_MODE", "").strip().lower()
    has_google_vars = bool(_client_id() or _client_secret())
    secret = os.environ.get("WEBJAM_SESSION_SECRET", "").strip()
    base = os.environ.get("WEBJAM_BASE_URL", "").strip().rstrip("/")
    if not raw_mode:
        if os.environ.get("WEBJAM_DEMO_MODE", "").strip():
            raise AuthConfigError(
                "WEBJAM_DEMO_MODE is no longer supported. Set WEBJAM_AUTH_MODE=demo or WEBJAM_AUTH_MODE=google."
            )
        if has_google_vars:
            # Never fall back to demo just because one Google variable went missing.
            mode = "google"
        elif secret or base:
            # A session secret or public base URL means a real deployment: make the operator choose.
            raise AuthConfigError(
                "WEBJAM_SESSION_SECRET/WEBJAM_BASE_URL are set but WEBJAM_AUTH_MODE is not. "
                "Set WEBJAM_AUTH_MODE=google (or WEBJAM_AUTH_MODE=demo for local development)."
            )
        else:
            mode = "demo"
    elif raw_mode in ("google", "demo"):
        mode = raw_mode
    else:
        raise AuthConfigError(f"WEBJAM_AUTH_MODE must be 'google' or 'demo', not {raw_mode!r}.")

    if mode == "google":
        missing = [
            name
            for name, val in (
                ("GOOGLE_CLIENT_ID", _client_id()),
                ("GOOGLE_CLIENT_SECRET", _client_secret()),
                ("WEBJAM_SESSION_SECRET", secret),
                ("WEBJAM_BASE_URL", base),
            )
            if not val
        ]
        if missing:
            raise AuthConfigError(f"WEBJAM_AUTH_MODE=google requires: {', '.join(missing)}")
        if len(secret) < MIN_SECRET_LEN:
            raise AuthConfigError(f"WEBJAM_SESSION_SECRET must be at least {MIN_SECRET_LEN} characters.")
        parsed = urllib.parse.urlparse(base)
        local_http = parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")
        if not parsed.netloc or not (parsed.scheme == "https" or local_http):
            raise AuthConfigError("WEBJAM_BASE_URL must be an https:// URL (http:// is allowed only for localhost).")
        return AuthConfig(mode="google", session_secret=secret, base_url=base, https_only=True)

    if base and urllib.parse.urlparse(base).hostname not in LOOPBACK_HOSTS:
        raise AuthConfigError(
            "Refusing demo mode with a non-localhost WEBJAM_BASE_URL: demo mode lets anyone act as anyone. "
            "Use WEBJAM_AUTH_MODE=google for a public deployment."
        )
    return AuthConfig(
        mode="demo",
        session_secret=secret or secrets.token_urlsafe(32),
        base_url=base,
        https_only=base.startswith("https://"),
    )


def configure_from_env() -> AuthConfig:
    """(Re)load the auth config. Called once at startup by the app lifespan."""
    global _config
    _config = load_config_from_env()
    return _config


def get_config() -> AuthConfig:
    if _config is None:
        return configure_from_env()
    return _config


def auth_mode() -> str:
    return get_config().mode


def auth_enabled() -> bool:
    return auth_mode() == "google"


def demo_allows_remote() -> bool:
    return os.environ.get("WEBJAM_DEMO_ALLOW_REMOTE", "").strip() == "1"


def check_demo_mode_allowed(conn: sqlite3.Connection) -> None:
    """Demo mode lets anyone impersonate anyone, so never run it over real accounts."""
    if auth_mode() != "demo":
        return
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS cnt FROM google_accounts")
    if cur.fetchone()["cnt"]:
        raise AuthConfigError(
            "Refusing to start in demo mode: this database has Google-linked accounts. "
            "Set WEBJAM_AUTH_MODE=google (with its required variables) or use a fresh WEBJAM_DB_PATH."
        )


def base_url(request: Request) -> str:
    configured = get_config().base_url
    return configured or str(request.base_url).rstrip("/")


def redirect_uri(request: Request) -> str:
    return f"{base_url(request)}/auth/google/callback"


# --- Sessions ---

def session_user_id(request: Request) -> Optional[int]:
    """The signed-in user, only if they still exist and the cookie's epoch is current."""
    if "session" not in request.scope:
        return None
    cached = getattr(request.state, "webjam_session_uid", -1)
    if cached != -1:
        return cached
    uid_val = request.session.get("uid")
    epoch_val = request.session.get("ep")
    result: Optional[int] = None
    try:
        uid = int(uid_val) if uid_val is not None else 0
        epoch = int(epoch_val) if epoch_val is not None else -1
    except (TypeError, ValueError):
        uid, epoch = 0, -1
    install_val = request.session.get("db")
    if uid > 0 and epoch >= 0 and isinstance(install_val, str) and install_val:
        conn = get_connection()
        try:
            # One query: the user exists, the epoch is current, the cookie was issued by
            # this database (not one that was wiped), and their Google email is still invited.
            row = conn.execute(
                """
                SELECT u.session_epoch,
                       (SELECT value FROM meta WHERE key = 'install_id') AS install_id,
                       EXISTS (
                           SELECT 1 FROM google_accounts ga JOIN invites i ON i.email = ga.email
                           WHERE ga.user_id = u.id
                       ) AS invited
                FROM users u WHERE u.id = ?
                """,
                (uid,),
            ).fetchone()
        finally:
            conn.close()
        if (
            row is not None
            and int(row["session_epoch"] or 0) == epoch
            and row["install_id"] == install_val
            and row["invited"]
        ):
            result = uid
    if result is None and (uid_val is not None or epoch_val is not None):
        request.session.pop("uid", None)
        request.session.pop("ep", None)
        request.session.pop("db", None)
    request.state.webjam_session_uid = result
    return result


def install_id(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = 'install_id'").fetchone()
    return row["value"] if row else ""


def start_session(request: Request, conn: sqlite3.Connection, uid: int) -> None:
    row = conn.execute("SELECT session_epoch FROM users WHERE id = ?", (uid,)).fetchone()
    request.session.clear()
    request.session["uid"] = uid
    request.session["ep"] = int(row["session_epoch"] or 0) if row else 0
    request.session["db"] = install_id(conn)
    request.state.webjam_session_uid = uid


def resolve_actor(request: Request, claimed_user_id: Optional[int]) -> int:
    """The user performing a write. Session user in google mode; client's claim in demo mode."""
    if auth_enabled():
        uid = session_user_id(request)
        if not uid:
            raise HTTPException(status_code=401, detail="Sign in with Google to do that.")
        return uid
    if not claimed_user_id:
        raise HTTPException(status_code=400, detail="user_id is required")
    conn = get_connection()
    try:
        exists = conn.execute("SELECT 1 FROM users WHERE id = ?", (int(claimed_user_id),)).fetchone()
    except OverflowError:
        exists = None
    finally:
        conn.close()
    if not exists:
        raise HTTPException(status_code=404, detail="User not found")
    return int(claimed_user_id)


def resolve_viewer(request: Request, claimed_user_id: Optional[int]) -> int:
    """The user viewing a page (for 'liked by me' etc). 0 means anonymous guest."""
    if auth_enabled():
        return session_user_id(request) or 0
    return int(claimed_user_id or 1)


# --- Handles ---

def slugify_handle(text: str) -> str:
    """Lowercase [a-z0-9_] slug of a display name (accents folded, spaces -> '_')."""
    folded = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "_", folded).strip("_")
    return re.sub(r"_+", "_", slug)[:20].strip("_")


def handle_taken(cur: sqlite3.Cursor, handle: str, exclude_user_id: Optional[int] = None) -> bool:
    """In use by someone else, or released by someone else (old feed lines still say
    '@handle', so a freed handle is never handed to a different user)."""
    cur.execute(
        """
        SELECT 1 FROM users WHERE LOWER(username) = ? AND id != ?
        UNION ALL
        SELECT 1 FROM handle_history WHERE handle = ? AND user_id != ?
        LIMIT 1
        """,
        (handle.lower(), exclude_user_id or 0, handle.lower(), exclude_user_id or 0),
    )
    return cur.fetchone() is not None


def release_handle(cur: sqlite3.Cursor, handle: str, user_id: int, now: str) -> None:
    """Reserve a handle someone is giving up, so only they can take it back."""
    cur.execute(
        "INSERT OR IGNORE INTO handle_history (handle, user_id, released_at) VALUES (?, ?, ?)",
        ((handle or "").lower(), user_id, now),
    )


def unique_handle(cur: sqlite3.Cursor, display_name: str) -> str:
    """A public handle derived from the display name, never from the email address."""
    base = slugify_handle(display_name)
    if len(base) < 3 or base in RESERVED_HANDLES:
        base = "curator"
    if not handle_taken(cur, base):
        return base
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    stem = base[:15]
    while True:
        candidate = f"{stem}_{''.join(secrets.choice(alphabet) for _ in range(4))}"
        if not handle_taken(cur, candidate):
            return candidate


def validate_handle(handle: str) -> str:
    clean = (handle or "").strip().lstrip("@").lower()
    if not HANDLE_PATTERN.match(clean):
        raise HTTPException(
            status_code=400,
            detail="Handles are 3-20 characters: lowercase letters, digits and underscores.",
        )
    if clean in RESERVED_HANDLES:
        raise HTTPException(status_code=400, detail="That handle is reserved.")
    return clean


# --- Users & tokens ---

def upsert_google_user(conn: sqlite3.Connection, info: Dict[str, Any], granted_scope: str = "") -> int:
    """Find or create the Webjam user for a Google account.

    No Google tokens are stored: sign-in only asks for identity scopes, so there is
    nothing worth keeping. (YouTube tokens arrive with incremental consent later.)
    """
    cur = conn.cursor()
    sub = info["sub"]
    email = (info.get("email") or "").strip().lower()
    name = (info.get("name") or "").strip()[:200] or "New Curator"
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())

    cur.execute("SELECT user_id FROM google_accounts WHERE google_sub = ?", (sub,))
    row = cur.fetchone()
    if row:
        uid = row["user_id"]
    else:
        cur.execute("SELECT COUNT(*) AS cnt FROM users")
        color = AVATAR_PALETTE[cur.fetchone()["cnt"] % len(AVATAR_PALETTE)]
        cur.execute(
            """
            INSERT INTO users (username, display_name, bio, avatar_color, password_hash, created_at)
            VALUES (?, ?, ?, ?, '', ?)
            """,
            (unique_handle(cur, name), name, "New crate digger on Webjam.", color, now),
        )
        uid = cur.lastrowid
        ensure_crate(conn, uid)

    cur.execute(
        """
        INSERT INTO google_accounts (google_sub, user_id, email, access_token, refresh_token, token_expires_at, scope, updated_at)
        VALUES (?, ?, ?, NULL, NULL, 0, ?, ?)
        ON CONFLICT(google_sub) DO UPDATE SET
            email = excluded.email, access_token = NULL, refresh_token = NULL,
            token_expires_at = 0, scope = excluded.scope, updated_at = excluded.updated_at
        """,
        (sub, uid, email, granted_scope or "", now),
    )
    conn.commit()
    return uid


def _legacy_email_handle(email: str) -> str:
    """The handle the pre-Phase 0 sign-in code derived from the email local part."""
    return re.sub(r"[^a-z0-9_]", "", (email or "").split("@")[0].lower())[:20]


def migrate_legacy_google_accounts(conn: sqlite3.Connection) -> int:
    """One-time cleanup of accounts created by the first Google sign-in code.

    * Drops any stored Google access/refresh tokens: sign-in no longer asks for
      YouTube access, and nothing should keep using the old youtube.readonly grant.
    * Renames handles that were derived from the email local part (they leak the
      address) to a handle derived from the display name. Runs once per database.
    Returns how many handles were renamed.
    """
    cur = conn.cursor()
    cur.execute(
        "UPDATE google_accounts SET access_token = NULL, refresh_token = NULL, token_expires_at = 0 "
        "WHERE access_token IS NOT NULL OR refresh_token IS NOT NULL"
    )
    if cur.execute("SELECT 1 FROM meta WHERE key = 'legacy_handles_migrated'").fetchone():
        conn.commit()
        return 0
    renamed = 0
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    rows = cur.execute(
        "SELECT u.id, u.username, u.display_name, ga.email FROM users u JOIN google_accounts ga ON ga.user_id = u.id"
    ).fetchall()
    for row in rows:
        legacy = _legacy_email_handle(row["email"])
        uname = (row["username"] or "").lower()
        if legacy and (uname == legacy or re.fullmatch(re.escape(legacy) + r"\d+", uname)):
            new_handle = unique_handle(cur, row["display_name"] or "")
            release_handle(cur, uname, row["id"], now)
            cur.execute("UPDATE users SET username = ? WHERE id = ?", (new_handle, row["id"]))
            renamed += 1
    cur.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('legacy_handles_migrated', ?)", (now,))
    conn.commit()
    if renamed:
        logger.warning("Renamed %d handle(s) that were derived from email addresses", renamed)
    return renamed


def has_youtube_access(acct: Optional[sqlite3.Row]) -> bool:
    if not acct:
        return False
    scopes = (acct["scope"] or "").split()
    return YOUTUBE_SCOPE in scopes and bool(acct["access_token"] or acct["refresh_token"])


async def youtube_access_token(conn: sqlite3.Connection, user_id: int) -> str:
    cur = conn.cursor()
    cur.execute("SELECT * FROM google_accounts WHERE user_id = ?", (user_id,))
    acct = cur.fetchone()
    if not has_youtube_access(acct):
        raise HTTPException(status_code=403, detail="YouTube access not connected yet.")
    if acct["access_token"] and int(acct["token_expires_at"] or 0) > time.time():
        return acct["access_token"]
    if not acct["refresh_token"]:
        raise HTTPException(status_code=403, detail="YouTube access not connected yet.")

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": _client_id(),
                "client_secret": _client_secret(),
                "refresh_token": acct["refresh_token"],
                "grant_type": "refresh_token",
            },
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Google session expired — please sign in again.")
    tok = resp.json()
    cur.execute(
        "UPDATE google_accounts SET access_token = ?, token_expires_at = ? WHERE google_sub = ?",
        (tok["access_token"], int(time.time()) + int(tok.get("expires_in", 3600)) - 60, acct["google_sub"]),
    )
    conn.commit()
    return tok["access_token"]


# --- Routes ---

router = APIRouter()

_MAX_PENDING_LOGINS = 3


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@router.get("/auth/google/login")
def google_login(request: Request):
    if not auth_enabled():
        raise HTTPException(status_code=404, detail="Google sign-in is not configured")
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    pending = dict(request.session.get("oauth") or {})
    pending[state] = verifier
    # Keep only the newest few in-flight logins (several tabs) so the cookie stays small.
    request.session["oauth"] = dict(list(pending.items())[-_MAX_PENDING_LOGINS:])
    params = {
        "client_id": _client_id(),
        "redirect_uri": redirect_uri(request),
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "code_challenge": _pkce_challenge(verifier),
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    return RedirectResponse(f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}")


@router.get("/auth/google/callback")
async def google_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    conn: sqlite3.Connection = Depends(get_db),
):
    if not auth_enabled():
        raise HTTPException(status_code=404, detail="Google sign-in is not configured")
    pending = dict(request.session.get("oauth") or {})
    verifier = pending.pop(state, None) if state else None
    request.session["oauth"] = pending
    if error:
        return RedirectResponse("/#auth-error", status_code=303)
    if not code or not verifier:
        raise HTTPException(status_code=400, detail="Invalid or expired sign-in attempt. Please try again.")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            tok_resp = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": _client_id(),
                    "client_secret": _client_secret(),
                    "code": code,
                    "code_verifier": verifier,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri(request),
                },
            )
            if tok_resp.status_code != 200:
                raise HTTPException(status_code=400, detail="Google sign-in failed while exchanging the code.")
            tokens = tok_resp.json()
            access_token = tokens.get("access_token") if isinstance(tokens, dict) else None
            if not access_token:
                raise HTTPException(status_code=400, detail="Google sign-in failed while exchanging the code.")
            info_resp = await client.get(GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})
            if info_resp.status_code != 200:
                raise HTTPException(status_code=400, detail="Could not read your Google profile.")
            info = info_resp.json()
            if not isinstance(info, dict):
                raise ValueError("unexpected userinfo payload")
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Google sign-in failed talking to Google: %s", type(exc).__name__)
        return RedirectResponse("/#auth-error", status_code=303)

    email = (info.get("email") or "").strip().lower()
    verified = info.get("email_verified") in (True, "true")
    if not info.get("sub") or not email or not verified or not is_invited(conn, email):
        logger.info("Rejected sign-in from an uninvited or unverified Google account")
        request.session.clear()
        return RedirectResponse("/#not-invited", status_code=303)

    uid = upsert_google_user(conn, info, str(tokens.get("scope", "") or ""))
    start_session(request, conn, uid)
    return RedirectResponse("/", status_code=303)


@router.post("/auth/logout")
def logout(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    uid = session_user_id(request) if auth_enabled() else None
    if uid:
        # Bumping the epoch revokes every outstanding cookie for this user, not just this browser's.
        conn.execute("UPDATE users SET session_epoch = session_epoch + 1 WHERE id = ?", (uid,))
        conn.execute(
            "UPDATE google_accounts SET access_token = NULL, refresh_token = NULL, token_expires_at = 0 WHERE user_id = ?",
            (uid,),
        )
        conn.commit()
    request.session.clear()
    return {"ok": True}


def _require_youtube_user(request: Request, conn: sqlite3.Connection, what: str) -> int:
    uid = session_user_id(request) if auth_enabled() else None
    if not uid:
        raise HTTPException(status_code=401, detail=f"Sign in with Google to {what}.")
    acct = conn.execute("SELECT * FROM google_accounts WHERE user_id = ?", (uid,)).fetchone()
    if not has_youtube_access(acct):
        raise HTTPException(status_code=403, detail="YouTube access not connected yet.")
    return uid


@router.get("/api/youtube/my-playlists")
async def my_youtube_playlists(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    uid = _require_youtube_user(request, conn, "see your YouTube playlists")
    token = await youtube_access_token(conn, uid)
    playlists = []
    page_token = ""
    async with httpx.AsyncClient(timeout=10) as client:
        for _ in range(5):  # up to 250 playlists
            resp = await client.get(
                f"{YOUTUBE_API}/playlists",
                params={"part": "snippet,contentDetails,status", "mine": "true", "maxResults": 50, "pageToken": page_token},
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code != 200:
                raise HTTPException(status_code=502, detail="YouTube didn't return your playlists.")
            data = resp.json()
            for item in data.get("items", []):
                playlists.append(
                    {
                        "id": item["id"],
                        "title": item["snippet"]["title"],
                        "item_count": item.get("contentDetails", {}).get("itemCount", 0),
                        "privacy": item.get("status", {}).get("privacyStatus", ""),
                    }
                )
            page_token = data.get("nextPageToken", "")
            if not page_token:
                break
    return {"playlists": playlists}


@router.get("/api/youtube/my-playlists/{playlist_id}/videos")
async def my_youtube_playlist_videos(
    playlist_id: str, request: Request, limit: int = 50, conn: sqlite3.Connection = Depends(get_db)
):
    uid = _require_youtube_user(request, conn, "read your YouTube playlists")
    if not re.match(r"^[A-Za-z0-9_-]{2,64}$", playlist_id):
        raise HTTPException(status_code=400, detail="Invalid playlist id")
    token = await youtube_access_token(conn, uid)
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{YOUTUBE_API}/playlistItems",
            params={"part": "contentDetails", "playlistId": playlist_id, "maxResults": max(1, min(limit, 50))},
            headers={"Authorization": f"Bearer {token}"},
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="YouTube didn't return that playlist's videos.")
    ids = [i["contentDetails"]["videoId"] for i in resp.json().get("items", []) if i.get("contentDetails")]
    return {"video_ids": ids}

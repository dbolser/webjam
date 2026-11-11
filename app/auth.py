"""Google sign-in (OAuth 2.0 authorization-code flow) and session helpers.

Auth mode is enabled when GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are set
(unless WEBJAM_DEMO_MODE=1). In auth mode the acting user always comes from the
signed session cookie; any user_id the client sends is ignored. Without Google
credentials the app runs in the original demo mode (persona switcher, client
chooses the user).
"""

import os
import re
import secrets
import sqlite3
import time
import urllib.parse
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.database import get_db

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
YOUTUBE_API = "https://www.googleapis.com/youtube/v3"
SCOPES = "openid email profile https://www.googleapis.com/auth/youtube.readonly"

AVATAR_PALETTE = ["#1f5f5b", "#8c3b2b", "#3d4e81", "#6b4226", "#4a6b2c", "#7a3b69"]


def _client_id() -> str:
    return os.environ.get("GOOGLE_CLIENT_ID", "").strip()


def _client_secret() -> str:
    return os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()


def auth_enabled() -> bool:
    if os.environ.get("WEBJAM_DEMO_MODE", "").strip() == "1":
        return False
    return bool(_client_id() and _client_secret())


def base_url(request: Request) -> str:
    configured = os.environ.get("WEBJAM_BASE_URL", "").strip().rstrip("/")
    return configured or str(request.base_url).rstrip("/")


def redirect_uri(request: Request) -> str:
    return f"{base_url(request)}/auth/google/callback"


def session_user_id(request: Request) -> Optional[int]:
    uid = request.session.get("uid") if "session" in request.scope else None
    return int(uid) if uid else None


def resolve_actor(request: Request, claimed_user_id: Optional[int]) -> int:
    """The user performing a write. Session user in auth mode; client's claim in demo mode."""
    if auth_enabled():
        uid = session_user_id(request)
        if not uid:
            raise HTTPException(status_code=401, detail="Sign in with Google to do that.")
        return uid
    if not claimed_user_id:
        raise HTTPException(status_code=400, detail="user_id is required")
    return int(claimed_user_id)


def resolve_viewer(request: Request, claimed_user_id: Optional[int]) -> int:
    """The user viewing a page (for 'liked by me' etc). 0 means anonymous guest."""
    if auth_enabled():
        return session_user_id(request) or 0
    return int(claimed_user_id or 1)


# --- Users & tokens ---

def _unique_username(cur: sqlite3.Cursor, email: str, name: str) -> str:
    base = re.sub(r"[^a-z0-9_]", "", (email.split("@")[0] or name).lower())[:20] or "curator"
    candidate, n = base, 1
    while True:
        cur.execute("SELECT 1 FROM users WHERE LOWER(username) = ?", (candidate,))
        if not cur.fetchone():
            return candidate
        n += 1
        candidate = f"{base}{n}"


def upsert_google_user(conn: sqlite3.Connection, info: Dict[str, Any], tokens: Dict[str, Any]) -> int:
    cur = conn.cursor()
    sub = info["sub"]
    email = info.get("email", "")
    name = info.get("name") or email.split("@")[0] or "New Curator"
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())

    cur.execute("SELECT user_id, refresh_token FROM google_accounts WHERE google_sub = ?", (sub,))
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
            (_unique_username(cur, email, name), name, "New crate digger on Webjam.", color, now),
        )
        uid = cur.lastrowid

    refresh = tokens.get("refresh_token") or (row["refresh_token"] if row else None)
    cur.execute(
        """
        INSERT INTO google_accounts (google_sub, user_id, email, access_token, refresh_token, token_expires_at, scope, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(google_sub) DO UPDATE SET
            email = excluded.email, access_token = excluded.access_token,
            refresh_token = excluded.refresh_token, token_expires_at = excluded.token_expires_at,
            scope = excluded.scope, updated_at = excluded.updated_at
        """,
        (
            sub,
            uid,
            email,
            tokens.get("access_token"),
            refresh,
            int(time.time()) + int(tokens.get("expires_in", 3600)) - 60,
            tokens.get("scope", ""),
            now,
        ),
    )
    conn.commit()
    return uid


async def youtube_access_token(conn: sqlite3.Connection, user_id: int) -> str:
    cur = conn.cursor()
    cur.execute("SELECT * FROM google_accounts WHERE user_id = ?", (user_id,))
    acct = cur.fetchone()
    if not acct:
        raise HTTPException(status_code=403, detail="This account isn't connected to Google.")
    if acct["access_token"] and int(acct["token_expires_at"] or 0) > time.time():
        return acct["access_token"]
    if not acct["refresh_token"]:
        raise HTTPException(status_code=401, detail="Google session expired — please sign in again.")

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


@router.get("/auth/google/login")
def google_login(request: Request):
    if not auth_enabled():
        raise HTTPException(status_code=404, detail="Google sign-in is not configured")
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    params = {
        "client_id": _client_id(),
        "redirect_uri": redirect_uri(request),
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent select_account",
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
    expected = request.session.pop("oauth_state", None)
    if error:
        return RedirectResponse("/#auth-error")
    if not code or not expected or not secrets.compare_digest(state, expected):
        raise HTTPException(status_code=400, detail="Invalid or expired sign-in attempt. Please try again.")

    async with httpx.AsyncClient(timeout=10) as client:
        tok_resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": _client_id(),
                "client_secret": _client_secret(),
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri(request),
            },
        )
        if tok_resp.status_code != 200:
            raise HTTPException(status_code=400, detail="Google sign-in failed while exchanging the code.")
        tokens = tok_resp.json()
        info_resp = await client.get(
            GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {tokens['access_token']}"}
        )
        if info_resp.status_code != 200:
            raise HTTPException(status_code=400, detail="Could not read your Google profile.")
        info = info_resp.json()

    uid = upsert_google_user(conn, info, tokens)
    request.session.clear()
    request.session["uid"] = uid
    return RedirectResponse("/")


@router.post("/auth/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@router.get("/api/youtube/my-playlists")
async def my_youtube_playlists(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    uid = resolve_actor(request, None) if auth_enabled() else None
    if not uid:
        raise HTTPException(status_code=401, detail="Sign in with Google to see your YouTube playlists.")
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
    uid = resolve_actor(request, None) if auth_enabled() else None
    if not uid:
        raise HTTPException(status_code=401, detail="Sign in with Google to read your YouTube playlists.")
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

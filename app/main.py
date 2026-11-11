import asyncio
import logging
import re
import threading
import time
import urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Annotated, Any, Dict, List, Optional, Tuple, Union
import httpx

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.sessions import SessionMiddleware

from app.auth import (
    SESSION_MAX_AGE,
    auth_enabled,
    auth_mode,
    check_demo_mode_allowed,
    configure_from_env,
    get_config,
    handle_taken,
    resolve_actor,
    resolve_viewer,
    router as auth_router,
    session_user_id,
    validate_handle,
)

from app.badges import BADGE_BY_KEY, BADGE_CATALOG, evaluate_and_award_badges, get_user_badge_progress
from app.database import get_connection, get_db, init_db
from app.invite import seed_invites_from_env
from app.youtube import (
    check_video_embeddability,
    clean_migration_query,
    extract_video_entries,
    extract_video_ids,
    fetch_youtube_metadata,
    inspect_youtube_playlist_or_urls,
    parse_timestamp_seconds,
    search_embeddable_youtube_songs,
)

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

logger = logging.getLogger("webjam")

VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")

# --- Size caps ---
MAX_TITLE = 200
MAX_TEXT = 5000  # liner notes, descriptions, bios
MAX_COMMENT = 2000
MAX_URL_BLOB = 100_000  # urls_or_ids / initial_urls / source_input
MAX_ENTRIES_PER_REQUEST = 50
MAX_TRACKS_PER_PLAYLIST = 1000
MAX_BODY_BYTES = 256 * 1024
# A 1000-row snapshot (undo) is larger than an ordinary request.
SNAPSHOT_PATH = re.compile(r"^/api/playlists/\d+/snapshot$")
MAX_SNAPSHOT_BODY_BYTES = 1024 * 1024
COMMENTS_PAGE_SIZE = 50

# --- Rate limits: bucket -> (burst capacity, tokens refilled per second) ---
RATE_LIMITS: Dict[str, Tuple[float, float]] = {
    "search": (20, 20 / 60),
    "inspect": (6, 6 / 60),
    "add": (30, 30 / 60),
    "comment": (10, 5 / 60),
    "like": (60, 1.0),
    "play": (60, 1.0),
    "audit": (5, 5 / 60),
}
PLAY_DEDUPE_SECONDS = 30 * 60
PLAY_BADGE_EVAL_INTERVAL = 60
TRUSTED_PROXIES = {"127.0.0.1", "::1"}


class TokenBucketLimiter:
    """Small in-process token bucket. Good enough for one uvicorn worker."""

    def __init__(self) -> None:
        self._buckets: Dict[Tuple[str, str], Tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: Tuple[str, str], capacity: float, refill_per_sec: float) -> Tuple[bool, float]:
        now = time.monotonic()
        with self._lock:
            if len(self._buckets) > 50_000:
                cutoff = now - 3600
                self._buckets = {k: v for k, v in self._buckets.items() if v[1] > cutoff}
            tokens, last = self._buckets.get(key, (capacity, now))
            tokens = min(capacity, tokens + (now - last) * refill_per_sec)
            if tokens >= 1:
                self._buckets[key] = (tokens - 1, now)
                return True, 0.0
            self._buckets[key] = (tokens, now)
            return False, (1 - tokens) / refill_per_sec

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


rate_limiter = TokenBucketLimiter()
_play_badge_eval_lock = threading.Lock()
_last_play_badge_eval: Dict[int, float] = {}


def reset_rate_limits() -> None:
    rate_limiter.reset()
    with _play_badge_eval_lock:
        _last_play_badge_eval.clear()


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else ""
    if peer in TRUSTED_PROXIES:
        forwarded = request.headers.get("x-forwarded-for", "")
        # The right-most entry is the one our local reverse proxy appended.
        last = forwarded.split(",")[-1].strip() if forwarded else ""
        if last:
            return last
    return peer or "unknown"


def viewer_key(request: Request) -> str:
    """Who is calling, for rate limits and play dedupe: the signed-in user, else the client IP.

    In demo mode the client-chosen user_id is not trusted, so the IP is used.
    """
    if auth_enabled():
        uid = session_user_id(request)
        if uid:
            return f"u:{uid}"
    return f"ip:{client_ip(request)}"


def enforce_rate_limit(request: Request, bucket: str) -> None:
    capacity, refill = RATE_LIMITS[bucket]
    ok, retry_after = rate_limiter.allow((bucket, viewer_key(request)), capacity, refill)
    if not ok:
        raise HTTPException(
            status_code=429,
            detail="Slow down a little — too many requests. Try again in a moment.",
            headers={"Retry-After": str(max(1, int(retry_after + 0.999)))},
        )


def rate_limit(bucket: str):
    def _dependency(request: Request) -> None:
        enforce_rate_limit(request, bucket)

    return Depends(_dependency)


# --- Middleware (pure ASGI) ---

async def _send_json(scope, receive, send, status: int, detail: str) -> None:
    await JSONResponse({"detail": detail}, status_code=status)(scope, receive, send)


class ConfiguredSessionMiddleware:
    """Starlette's signed-cookie sessions, configured from the auth config chosen at startup."""

    def __init__(self, app) -> None:
        self.app = app
        self._inner: Optional[SessionMiddleware] = None
        self._key: Optional[Tuple[str, bool]] = None

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        cfg = get_config()
        key = (cfg.session_secret, cfg.https_only)
        if self._inner is None or self._key != key:
            self._inner = SessionMiddleware(
                self.app,
                secret_key=cfg.session_secret,
                session_cookie="webjam_session",
                max_age=SESSION_MAX_AGE,
                same_site="lax",
                https_only=cfg.https_only,
            )
            self._key = key
        await self._inner(scope, receive, send)


def _origin_of(url: str) -> str:
    p = urllib.parse.urlparse(url)
    return f"{p.scheme}://{p.netloc}".lower()


def csrf_failure(scope) -> Optional[str]:
    """Why an unsafe request should be refused, or None if it looks same-origin."""
    headers = Headers(scope=scope)
    if headers.get("x-webjam") != "1":
        return "Missing X-Webjam request header."
    if headers.get("sec-fetch-site", "").lower() == "cross-site":
        return "Cross-site request refused."
    origin = headers.get("origin")
    if origin:
        cfg = get_config()
        if cfg.base_url:
            ok = origin.rstrip("/").lower() == _origin_of(cfg.base_url)
        else:
            ok = urllib.parse.urlparse(origin).netloc.lower() == headers.get("host", "").lower()
        if not ok:
            return "Cross-origin request refused."
    return None


class CSRFMiddleware:
    """Unsafe methods on /api/ and /auth/logout need X-Webjam: 1 and a same-origin Origin (if sent)."""

    UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] in self.UNSAFE:
            path = scope.get("path", "")
            if path.startswith("/api/") or path == "/auth/logout":
                reason = csrf_failure(scope)
                if reason:
                    return await _send_json(scope, receive, send, 403, reason)
        await self.app(scope, receive, send)


class BodySizeLimitMiddleware:
    """Refuse request bodies over the cap with 413 before any handler parses them."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS"):
            return await self.app(scope, receive, send)
        limit = MAX_SNAPSHOT_BODY_BYTES if SNAPSHOT_PATH.match(scope.get("path", "")) else MAX_BODY_BYTES
        declared = Headers(scope=scope).get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > limit):
            return await _send_json(scope, receive, send, 413, "Request body too large.")

        chunks: List[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body = message.get("body", b"")
            total += len(body)
            if total > limit:
                return await _send_json(scope, receive, send, 413, "Request body too large.")
            chunks.append(body)
            if not message.get("more_body", False):
                break

        buffered = b"".join(chunks)
        replayed = False

        async def replay_receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": buffered, "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
    # Only anti-framing for now; a full CSP needs the inline handlers gone first (Phase 1).
    "Content-Security-Policy": "frame-ancestors 'none'",
    # Leave autoplay/fullscreen/encrypted-media alone: the YouTube iframe needs them.
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}


class SecurityHeadersMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    if name not in headers:
                        headers[name] = value
                if get_config().base_url.startswith("https://") and "Strict-Transport-Security" not in headers:
                    headers["Strict-Transport-Security"] = "max-age=15552000"
            await send(message)

        await self.app(scope, receive, send_with_headers)


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = configure_from_env()  # raises AuthConfigError: refuse to start on bad config
    init_db()
    conn = get_connection()
    try:
        check_demo_mode_allowed(conn)
        seed_invites_from_env(conn)
    finally:
        conn.close()
    reset_rate_limits()
    if cfg.mode == "demo":
        logger.warning("Webjam auth mode: DEMO — anyone can act as anyone. Never expose this instance publicly.")
    else:
        logger.warning("Webjam auth mode: google (invite-only), base URL %s", cfg.base_url)
    yield


app = FastAPI(title="Webjam — Social YouTube Playlist Curation", lifespan=lifespan)
# Added innermost first: security headers wrap everything, then body cap, CSRF, sessions.
app.add_middleware(ConfiguredSessionMiddleware)
app.add_middleware(CSRFMiddleware)
app.add_middleware(BodySizeLimitMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.include_router(auth_router)


def now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def require_playlist_owner(conn: sqlite3.Connection, playlist_id: int, user_id: int) -> sqlite3.Row:
    cur = conn.cursor()
    cur.execute("SELECT * FROM playlists WHERE id = ?", (playlist_id,))
    pl = cur.fetchone()
    if not pl:
        raise HTTPException(status_code=404, detail="Playlist not found")
    if pl["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Only the playlist creator can change this playlist")
    return pl


GUEST_USER = {
    "id": 0,
    "username": "guest",
    "display_name": "Guest",
    "bio": "",
    "avatar_color": "#888888",
    "created_at": "",
    "playlist_count": 0,
    "songs_discovered": 0,
    "badge_count": 0,
    "times_grabbed": 0,
}


# --- Pydantic Schemas ---

Title = Annotated[str, Field(max_length=MAX_TITLE)]
LongText = Annotated[str, Field(max_length=MAX_TEXT)]
UrlBlob = Annotated[str, Field(max_length=MAX_URL_BLOB)]
VideoRef = Annotated[str, Field(max_length=2048)]  # an id or one URL
TimeValue = Optional[Union[int, float, Annotated[str, Field(max_length=32)]]]


class LoginRequest(BaseModel):
    """Demo mode only. The password is accepted for old clients but ignored entirely."""

    username: Annotated[str, Field(max_length=64)]
    password: Optional[Annotated[str, Field(max_length=200)]] = None
    display_name: Optional[Title] = None
    bio: Optional[LongText] = ""


class UpdateProfileRequest(BaseModel):
    display_name: Title
    bio: LongText


class UpdateHandleRequest(BaseModel):
    user_id: Optional[int] = None  # demo mode only
    handle: Annotated[str, Field(max_length=64)]


class CreatePlaylistRequest(BaseModel):
    user_id: Optional[int] = None
    title: Title
    description: Optional[LongText] = ""
    initial_urls: Optional[UrlBlob] = ""


class UpdatePlaylistRequest(BaseModel):
    user_id: Optional[int] = None
    title: Title
    description: LongText


class AddTracksRequest(BaseModel):
    user_id: Optional[int] = None
    urls_or_ids: UrlBlob
    track_note: Optional[LongText] = ""
    custom_title: Optional[Title] = None
    start_time: TimeValue = None
    end_time: TimeValue = None
    source_playlist_id: Optional[int] = None
    # Ignored: provenance is derived from source_playlist_id on the server.
    source_user_id: Optional[int] = None


class UpdateTrackRequest(BaseModel):
    user_id: Optional[int] = None
    track_note: Optional[LongText] = None
    start_time: TimeValue = None
    end_time: TimeValue = None
    update_clip: Optional[bool] = False
    direction: Optional[Annotated[str, Field(max_length=8)]] = None  # 'up' or 'down'
    new_video_id: Optional[VideoRef] = None
    custom_title: Optional[Title] = None


class InspectPlaylistImportRequest(BaseModel):
    source_input: UrlBlob
    max_tracks: int = 30


class CommitImportedTrackItem(BaseModel):
    video_id: VideoRef
    custom_title: Optional[Title] = None
    track_note: Optional[LongText] = ""
    start_seconds: TimeValue = None
    end_seconds: TimeValue = None
    migrated_from_video_id: Optional[Annotated[str, Field(max_length=32)]] = None
    migrated_from_title: Optional[Annotated[str, Field(max_length=500)]] = None


class CommitPlaylistImportRequest(BaseModel):
    user_id: Optional[int] = None
    title: Title
    description: Optional[LongText] = ""
    target_playlist_id: Optional[int] = None
    tracks: List[CommitImportedTrackItem]


class ReorderTracksRequest(BaseModel):
    user_id: Optional[int] = None
    track_ids: Annotated[List[int], Field(max_length=MAX_TRACKS_PER_PLAYLIST)]


class SnapshotTrackItem(BaseModel):
    id: Optional[int] = None
    video_id: Annotated[str, Field(max_length=32)]
    position: Optional[int] = None
    track_note: Optional[LongText] = ""
    start_seconds: Optional[int] = None
    end_seconds: Optional[int] = None
    migrated_from_video_id: Optional[Annotated[str, Field(max_length=32)]] = None
    migrated_from_title: Optional[Annotated[str, Field(max_length=500)]] = None
    display_title: Optional[Title] = None
    # Attribution fields are accepted for compatibility but never trusted: rows keep
    # the attribution they already had on the server, new rows get the actor/now.
    added_by_user_id: Optional[int] = None
    source_playlist_id: Optional[int] = None
    source_user_id: Optional[int] = None
    created_at: Optional[Annotated[str, Field(max_length=64)]] = None


class RestorePlaylistSnapshotRequest(BaseModel):
    user_id: Optional[int] = None
    title: Title
    description: Optional[LongText] = ""
    tracks: List[SnapshotTrackItem]


class ToggleLikeRequest(BaseModel):
    user_id: Optional[int] = None
    count: int = 1
    reset: bool = False


class RecordPlayRequest(BaseModel):
    video_id: Optional[Annotated[str, Field(max_length=32)]] = None
    count: int = 1  # ignored: every play counts once


class CreateCommentRequest(BaseModel):
    author_user_id: Optional[int] = None
    target_type: Annotated[str, Field(max_length=16)]  # 'playlist' | 'song' | 'profile'
    target_id: Annotated[str, Field(max_length=64)]
    body: Annotated[str, Field(max_length=MAX_COMMENT)]


# --- Helper query functions ---

def fetch_all_users_summary(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT u.id, u.username, u.display_name, u.bio, u.avatar_color, u.created_at,
               (SELECT COUNT(*) FROM playlists p WHERE p.user_id = u.id) AS playlist_count,
               (SELECT COUNT(*) FROM songs s WHERE s.first_added_by_user_id = u.id) AS songs_discovered,
               (SELECT COUNT(*) FROM user_badges ub WHERE ub.user_id = u.id) AS badge_count,
               (SELECT COUNT(*) FROM playlist_tracks pt WHERE pt.source_user_id = u.id AND pt.added_by_user_id != u.id) AS times_grabbed
        FROM users u
        ORDER BY u.id ASC
        """
    )
    return [dict(r) for r in cur.fetchall()]


def fetch_comments_page(
    conn: sqlite3.Connection,
    target_type: str,
    target_id: str,
    before_id: Optional[int] = None,
    limit: int = COMMENTS_PAGE_SIZE,
) -> Tuple[List[Dict[str, Any]], bool]:
    """The newest `limit` comments (older than before_id, if given), oldest first, plus has_more."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT c.id, c.author_user_id, c.target_type, c.target_id, c.body, c.created_at,
               u.username AS author_username, u.display_name AS author_display_name, u.avatar_color AS author_avatar_color
        FROM comments c
        JOIN users u ON u.id = c.author_user_id
        WHERE c.target_type = ? AND c.target_id = ? AND (? IS NULL OR c.id < ?)
        ORDER BY c.id DESC
        LIMIT ?
        """,
        (target_type, str(target_id), before_id, before_id, limit + 1),
    )
    rows = [dict(r) for r in cur.fetchall()]
    has_more = len(rows) > limit
    return list(reversed(rows[:limit])), has_more


class VideoLookupFailed(Exception):
    """YouTube had no metadata for this id (deleted, private or never existed)."""


async def lookup_new_song_metadata(video_id: str) -> Dict[str, Any]:
    meta = await fetch_youtube_metadata(video_id)
    if not meta or meta.get("status") == "NOT_FOUND" or not meta.get("title"):
        raise VideoLookupFailed(video_id)
    return meta


def clean_display_title(custom_title: Optional[str], song_title: Optional[str]) -> Optional[str]:
    """A per-row title override, or None when it is empty or the same as the song's real title."""
    title = (custom_title or "").strip()[:MAX_TITLE]
    return title if title and title != (song_title or "") else None


def ensure_playlist_capacity(conn: sqlite3.Connection, playlist_id: int, adding: int = 1) -> None:
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,)
    ).fetchone()
    if int(row["cnt"]) + adding > MAX_TRACKS_PER_PLAYLIST:
        raise HTTPException(
            status_code=400,
            detail=f"Playlists can hold at most {MAX_TRACKS_PER_PLAYLIST} tracks.",
        )


def ensure_entry_count(count: int) -> None:
    if count > MAX_ENTRIES_PER_REQUEST:
        raise HTTPException(
            status_code=400,
            detail=f"Add at most {MAX_ENTRIES_PER_REQUEST} videos at a time.",
        )


def verified_source(
    conn: sqlite3.Connection, source_playlist_id: Optional[int], video_id: str
) -> Optional[Tuple[int, int]]:
    """(source_playlist_id, owner_id) if that playlist exists and contains the video, else None."""
    if not source_playlist_id:
        return None
    row = conn.execute(
        """
        SELECT p.id, p.user_id FROM playlists p
        WHERE p.id = ? AND EXISTS (
            SELECT 1 FROM playlist_tracks pt WHERE pt.playlist_id = p.id AND pt.video_id = ?
        )
        """,
        (source_playlist_id, video_id),
    ).fetchone()
    return (row["id"], row["user_id"]) if row else None


async def add_single_video_to_playlist(
    conn: sqlite3.Connection,
    playlist_id: int,
    user_id: int,
    video_id: str,
    track_note: str = "",
    custom_title: Optional[str] = None,
    start_seconds: Optional[int] = None,
    end_seconds: Optional[int] = None,
    migrated_from_video_id: Optional[str] = None,
    migrated_from_title: Optional[str] = None,
    source_playlist_id: Optional[int] = None,
    source_user_id: Optional[int] = None,
) -> Dict[str, Any]:
    cur = conn.cursor()
    ts = now_str()

    if start_seconds is not None and end_seconds is not None and end_seconds <= start_seconds:
        raise HTTPException(
            status_code=400,
            detail="End time must be greater than start time.",
        )

    ensure_playlist_capacity(conn, playlist_id)

    # Check if song already exists in Webjam
    cur.execute("SELECT * FROM songs WHERE video_id = ?", (video_id,))
    song_row = cur.fetchone()
    is_new_discovery = False

    if not song_row:
        # songs.title always comes from YouTube; a custom title only applies to this row.
        # Raises VideoLookupFailed before anything is written when YouTube has no such video.
        meta = await lookup_new_song_metadata(video_id)
        cur.execute(
            """
            INSERT INTO songs (video_id, title, channel_title, thumbnail_url, first_added_by_user_id, first_added_playlist_id, first_added_at, total_plays)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                video_id,
                meta["title"],
                meta["channel_title"],
                meta["thumbnail_url"],
                user_id,
                playlist_id,
                ts,
            ),
        )
        is_new_discovery = True
        song_title = meta["title"]
        cur.execute(
            """
            INSERT INTO activity_log (actor_user_id, action_type, summary, link_type, link_id, created_at)
            VALUES (?, 'discovered_song', ?, 'song', ?, ?)
            """,
            (
                user_id,
                f"was the first on Webjam to add '{song_title}'",
                video_id,
                ts,
            ),
        )
    else:
        song_title = song_row["title"]

    display_title = clean_display_title(custom_title, song_title)

    # Determine next position (songs can be added to a playlist as many times as desired!)
    cur.execute(
        "SELECT COALESCE(MAX(position), 0) + 1 AS next_pos FROM playlist_tracks WHERE playlist_id = ?",
        (playlist_id,),
    )
    next_pos = cur.fetchone()["next_pos"]

    cur.execute(
        """
        INSERT INTO playlist_tracks
        (playlist_id, video_id, position, added_by_user_id, track_note, start_seconds, end_seconds, migrated_from_video_id, migrated_from_title, display_title, source_playlist_id, source_user_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            playlist_id,
            video_id,
            next_pos,
            user_id,
            (track_note or "").strip(),
            start_seconds,
            end_seconds,
            migrated_from_video_id if migrated_from_video_id and VIDEO_ID_PATTERN.match(migrated_from_video_id) else None,
            (migrated_from_title or "").strip()[:500] or None,
            display_title,
            source_playlist_id,
            source_user_id,
            ts,
        ),
    )
    inserted_track_id = cur.lastrowid

    cur.execute(
        "SELECT COUNT(*) AS cnt FROM playlist_tracks WHERE playlist_id = ? AND video_id = ?",
        (playlist_id, video_id),
    )
    occurrences_in_playlist = int(cur.fetchone()["cnt"] or 1)

    cur.execute("UPDATE playlists SET updated_at = ? WHERE id = ?", (ts, playlist_id))
    cur.execute("SELECT title FROM playlists WHERE id = ?", (playlist_id,))
    pl_title_row = cur.fetchone()
    pl_title = pl_title_row["title"] if pl_title_row else "playlist"

    if source_user_id and source_user_id != user_id:
        cur.execute("SELECT username FROM users WHERE id = ?", (source_user_id,))
        src_u = cur.fetchone()
        src_uname = src_u["username"] if src_u else "someone"
        cur.execute(
            """
            INSERT INTO activity_log (actor_user_id, action_type, summary, link_type, link_id, created_at)
            VALUES (?, 'grabbed_track', ?, 'playlist', ?, ?)
            """,
            (
                user_id,
                f"grabbed '{song_title}' from @{src_uname} into '{pl_title}'",
                str(playlist_id),
                ts,
            ),
        )
    elif not is_new_discovery:
        cur.execute(
            """
            INSERT INTO activity_log (actor_user_id, action_type, summary, link_type, link_id, created_at)
            VALUES (?, 'added_track', ?, 'playlist', ?, ?)
            """,
            (
                user_id,
                f"added '{song_title}' to '{pl_title}'",
                str(playlist_id),
                ts,
            ),
        )

    conn.commit()
    return {
        "track_id": inserted_track_id,
        "video_id": video_id,
        "title": display_title or song_title,
        "already_in_playlist": False,
        "occurrences_in_playlist": occurrences_in_playlist,
        "is_new_discovery": is_new_discovery,
        "start_seconds": start_seconds,
        "end_seconds": end_seconds,
    }


# --- API Endpoints ---

@app.get("/api/bootstrap")
def get_bootstrap(request: Request, user_id: int = Query(1), conn: sqlite3.Connection = Depends(get_db)):
    users = fetch_all_users_summary(conn)
    if not users:
        raise HTTPException(status_code=404, detail="No users found")

    viewer_id = resolve_viewer(request, user_id)
    if auth_enabled():
        current_user = next((u for u in users if u["id"] == viewer_id), dict(GUEST_USER))
    else:
        current_user = next((u for u in users if u["id"] == viewer_id), users[0])
    actual_uid = current_user["id"]

    badge_info = get_user_badge_progress(conn, actual_uid)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT p.id, p.title, p.description, p.plays, p.created_at, p.updated_at,
               (SELECT COUNT(*) FROM playlist_tracks pt WHERE pt.playlist_id = p.id) AS track_count
        FROM playlists p
        WHERE p.user_id = ?
        ORDER BY p.updated_at DESC, p.id DESC
        """,
        (actual_uid,),
    )
    my_playlists = [dict(r) for r in cur.fetchall()]

    return {
        "users": users,
        "current_user": {
            **current_user,
            "badges": badge_info["earned"],
            "badge_progress": badge_info["catalog"],
            "metrics": badge_info["metrics"],
            "playlists": my_playlists,
        },
        "badge_catalog": BADGE_CATALOG,
        "auth": {
            # 'demo' means anyone can act as anyone; the UI should say so.
            "mode": auth_mode(),
            "signed_in": bool(auth_enabled() and current_user["id"]),
        },
    }


def normalize_demo_username(raw: str) -> str:
    """Demo usernames are restricted to the same [a-z0-9_]{3,20} alphabet as Google handles."""
    name = re.sub(r"[\s.\-]+", "_", (raw or "").strip().lower().lstrip("@"))
    name = re.sub(r"[^a-z0-9_]", "", name).strip("_")[:20]
    if len(name) < 3:
        raise HTTPException(
            status_code=400,
            detail="Usernames are 3-20 characters: lowercase letters, digits and underscores.",
        )
    return name


@app.post("/api/auth/login")
def login_or_register(req: LoginRequest, conn: sqlite3.Connection = Depends(get_db)):
    """Demo-mode persona switch / registration.

    There is deliberately no password check: req.password is ignored entirely and
    no password is stored. Demo mode means anyone can act as anyone.
    """
    if auth_enabled():
        raise HTTPException(status_code=404, detail="Use Google sign-in")
    clean_uname = normalize_demo_username(req.username)

    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE LOWER(username) = ?", (clean_uname,))
    existing = cur.fetchone()
    if existing:
        return {"user_id": existing["id"], "created": False}

    # Register new user
    palette = ["#1f5f5b", "#8c3b2b", "#3d4e81", "#6b4226", "#4a6b2c", "#7a3b69"]
    cur.execute("SELECT COUNT(*) AS cnt FROM users")
    color = palette[cur.fetchone()["cnt"] % len(palette)]
    display = ((req.display_name or "").strip() or clean_uname)[:MAX_TITLE]
    ts = now_str()
    cur.execute(
        """
        INSERT INTO users (username, display_name, bio, avatar_color, password_hash, created_at)
        VALUES (?, ?, ?, ?, '', ?)
        """,
        (
            clean_uname,
            display,
            (req.bio or "New crate digger on Webjam.").strip(),
            color,
            ts,
        ),
    )
    new_uid = cur.lastrowid
    conn.commit()
    return {"user_id": new_uid, "created": True, "username": clean_uname}


@app.put("/api/me/handle")
def update_my_handle(req: UpdateHandleRequest, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    """Choose your public @handle (never derived from your email address)."""
    uid = resolve_actor(request, req.user_id)
    handle = validate_handle(req.handle)
    cur = conn.cursor()
    if not cur.execute("SELECT 1 FROM users WHERE id = ?", (uid,)).fetchone():
        raise HTTPException(status_code=404, detail="User not found")
    if handle_taken(cur, handle, exclude_user_id=uid):
        raise HTTPException(status_code=409, detail="That handle is taken.")
    try:
        cur.execute("UPDATE users SET username = ? WHERE id = ?", (handle, uid))
        conn.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="That handle is taken.")
    return {"ok": True, "username": handle}


@app.put("/api/users/{user_id}")
def update_user_profile(
    user_id: int, req: UpdateProfileRequest, request: Request, conn: sqlite3.Connection = Depends(get_db)
):
    if resolve_actor(request, user_id) != user_id:
        raise HTTPException(status_code=403, detail="You can only edit your own profile")
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET display_name = ?, bio = ? WHERE id = ?",
        (req.display_name.strip(), req.bio.strip(), user_id),
    )
    conn.commit()
    return {"ok": True}


@app.get("/api/feed")
def get_community_feed(request: Request, user_id: int = Query(1), conn: sqlite3.Connection = Depends(get_db)):
    user_id = resolve_viewer(request, user_id)
    cur = conn.cursor()

    # 1. All playlists with curator info, track count, likes, grabs
    cur.execute(
        """
        SELECT p.id, p.user_id, p.title, p.description, p.plays, p.created_at, p.updated_at,
               u.username, u.display_name, u.avatar_color,
               (SELECT COUNT(*) FROM playlist_tracks pt WHERE pt.playlist_id = p.id) AS track_count,
               (SELECT COALESCE(SUM(pl.count), 0) FROM playlist_likes pl WHERE pl.playlist_id = p.id) AS like_count,
               (SELECT COALESCE(pl.count, 0) FROM playlist_likes pl WHERE pl.playlist_id = p.id AND pl.user_id = ?) AS my_likes,
               (SELECT COUNT(*) FROM playlist_tracks pt WHERE pt.source_playlist_id = p.id AND pt.added_by_user_id != p.user_id) AS times_grabbed_from,
               (SELECT COUNT(*) FROM comments c WHERE c.target_type = 'playlist' AND c.target_id = CAST(p.id AS TEXT)) AS comment_count
        FROM playlists p
        JOIN users u ON u.id = p.user_id
        ORDER BY p.updated_at DESC, p.id DESC
        """,
        (user_id,),
    )
    playlists = []
    for row in cur.fetchall():
        pl = dict(row)
        pl["my_likes"] = int(pl.get("my_likes") or 0)
        pl["liked_by_me"] = bool(pl["my_likes"] > 0)
        cur.execute(
            """
            SELECT u.id, u.username, u.display_name, pl.count
            FROM playlist_likes pl
            JOIN users u ON u.id = pl.user_id
            WHERE pl.playlist_id = ?
            ORDER BY pl.count DESC, pl.created_at ASC
            """,
            (pl["id"],),
        )
        pl["liked_by_users"] = [dict(r) for r in cur.fetchall()]
        cur.execute(
            """
            SELECT pt.id, pt.video_id, pt.position, pt.track_note,
                   pt.start_seconds, pt.end_seconds,
                   pt.migrated_from_video_id, pt.migrated_from_title,
                   COALESCE(pt.display_title, s.title) AS title, s.title AS song_title, pt.display_title,
                   s.channel_title, s.thumbnail_url, s.first_added_by_user_id,
                   su.username AS source_username,
                   fu.username AS first_finder_username,
                   (SELECT COALESCE(SUM(sl.count), 0) FROM song_likes sl WHERE sl.video_id = pt.video_id) AS song_like_count,
                   (SELECT COALESCE(sl.count, 0) FROM song_likes sl WHERE sl.video_id = pt.video_id AND sl.user_id = ?) AS song_my_likes
            FROM playlist_tracks pt
            JOIN songs s ON s.video_id = pt.video_id
            LEFT JOIN users su ON su.id = pt.source_user_id
            LEFT JOIN users fu ON fu.id = s.first_added_by_user_id
            WHERE pt.playlist_id = ?
            ORDER BY pt.position ASC
            """,
            (user_id, pl["id"]),
        )
        tracks = []
        for t in cur.fetchall():
            td = dict(t)
            td["song_my_likes"] = int(td.get("song_my_likes") or 0)
            td["song_liked_by_me"] = bool(td["song_my_likes"] > 0)
            tracks.append(td)
        pl["tracks"] = tracks
        playlists.append(pl)

    # 2. Recently discovered songs on Webjam
    cur.execute(
        """
        SELECT s.video_id, s.title, s.channel_title, s.thumbnail_url,
               s.first_added_by_user_id, s.first_added_playlist_id, s.first_added_at, s.total_plays,
               u.username AS finder_username, u.display_name AS finder_display_name, u.avatar_color AS finder_avatar_color,
               p.title AS debut_playlist_title,
               (SELECT COUNT(DISTINCT pt.playlist_id) FROM playlist_tracks pt WHERE pt.video_id = s.video_id) AS playlist_count,
               (SELECT COUNT(*) FROM comments c WHERE c.target_type = 'song' AND c.target_id = s.video_id) AS comment_count,
               (SELECT COALESCE(SUM(sl.count), 0) FROM song_likes sl WHERE sl.video_id = s.video_id) AS like_count,
               (SELECT COALESCE(sl.count, 0) FROM song_likes sl WHERE sl.video_id = s.video_id AND sl.user_id = ?) AS my_likes
        FROM songs s
        JOIN users u ON u.id = s.first_added_by_user_id
        LEFT JOIN playlists p ON p.id = s.first_added_playlist_id
        ORDER BY s.first_added_at DESC
        """,
        (user_id,),
    )
    recent_songs = []
    for r in cur.fetchall():
        sd = dict(r)
        sd["my_likes"] = int(sd.get("my_likes") or 0)
        sd["liked_by_me"] = bool(sd["my_likes"] > 0)
        sd["song_like_count"] = sd["like_count"]
        sd["song_my_likes"] = sd["my_likes"]
        sd["song_liked_by_me"] = sd["liked_by_me"]
        recent_songs.append(sd)

    # 3. Community Activity Log
    cur.execute(
        """
        SELECT a.id, a.actor_user_id, a.action_type, a.summary, a.link_type, a.link_id, a.created_at,
               u.username, u.display_name, u.avatar_color
        FROM activity_log a
        JOIN users u ON u.id = a.actor_user_id
        ORDER BY a.created_at DESC, a.id DESC
        LIMIT 25
        """
    )
    activity = [dict(r) for r in cur.fetchall()]

    # 4. Curators Leaderboard
    users = fetch_all_users_summary(conn)
    for u in users:
        cur.execute(
            "SELECT badge_key FROM user_badges WHERE user_id = ? ORDER BY awarded_at DESC",
            (u["id"],),
        )
        u["badges"] = [
            BADGE_BY_KEY[r["badge_key"]]
            for r in cur.fetchall()
            if r["badge_key"] in BADGE_BY_KEY
        ]

    return {
        "playlists": playlists,
        "recent_songs": recent_songs,
        "activity": activity,
        "curators": users,
    }


@app.get("/api/playlists/{playlist_id}")
def get_playlist_detail(
    playlist_id: int, request: Request, user_id: int = Query(1), conn: sqlite3.Connection = Depends(get_db)
):
    user_id = resolve_viewer(request, user_id)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT p.*, u.username, u.display_name, u.avatar_color, u.bio AS user_bio,
               (SELECT COALESCE(SUM(pl.count), 0) FROM playlist_likes pl WHERE pl.playlist_id = p.id) AS like_count,
               (SELECT COALESCE(pl.count, 0) FROM playlist_likes pl WHERE pl.playlist_id = p.id AND pl.user_id = ?) AS my_likes,
               (SELECT COUNT(*) FROM playlist_tracks pt WHERE pt.source_playlist_id = p.id AND pt.added_by_user_id != p.user_id) AS times_grabbed_from
        FROM playlists p
        JOIN users u ON u.id = p.user_id
        WHERE p.id = ?
        """,
        (user_id, playlist_id),
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Playlist not found")

    playlist = dict(row)
    playlist["my_likes"] = int(playlist.get("my_likes") or 0)
    playlist["liked_by_me"] = bool(playlist["my_likes"] > 0)

    # Fetch who liked this playlist
    cur.execute(
        """
        SELECT u.id, u.username, u.display_name, pl.count
        FROM playlist_likes pl
        JOIN users u ON u.id = pl.user_id
        WHERE pl.playlist_id = ?
        ORDER BY pl.count DESC, pl.created_at ASC
        """,
        (playlist_id,),
    )
    playlist["liked_by_users"] = [dict(r) for r in cur.fetchall()]

    # Fetch tracks with full provenance and song cross-references
    cur.execute(
        """
        SELECT pt.id, pt.playlist_id, pt.video_id, pt.position, pt.added_by_user_id,
               pt.track_note, pt.start_seconds, pt.end_seconds,
               pt.migrated_from_video_id, pt.migrated_from_title,
               pt.source_playlist_id, pt.source_user_id, pt.created_at,
               COALESCE(pt.display_title, s.title) AS title, s.title AS song_title, pt.display_title,
               s.channel_title, s.thumbnail_url, s.total_plays,
               s.first_added_by_user_id, s.first_added_playlist_id, s.first_added_at,
               fu.username AS first_finder_username, fu.display_name AS first_finder_display_name,
               su.username AS source_username, su.display_name AS source_display_name,
               sp.title AS source_playlist_title,
               (SELECT COUNT(DISTINCT pt2.playlist_id) FROM playlist_tracks pt2 WHERE pt2.video_id = pt.video_id) AS playlist_count,
               (SELECT COUNT(*) FROM comments c WHERE c.target_type = 'song' AND c.target_id = pt.video_id) AS song_comment_count,
               (SELECT COALESCE(SUM(sl.count), 0) FROM song_likes sl WHERE sl.video_id = pt.video_id) AS song_like_count,
               (SELECT COALESCE(sl.count, 0) FROM song_likes sl WHERE sl.video_id = pt.video_id AND sl.user_id = ?) AS song_my_likes
        FROM playlist_tracks pt
        JOIN songs s ON s.video_id = pt.video_id
        JOIN users fu ON fu.id = s.first_added_by_user_id
        LEFT JOIN users su ON su.id = pt.source_user_id
        LEFT JOIN playlists sp ON sp.id = pt.source_playlist_id
        WHERE pt.playlist_id = ?
        ORDER BY pt.position ASC, pt.id ASC
        """,
        (user_id, playlist_id),
    )
    tracks = []
    for trow in cur.fetchall():
        t = dict(trow)
        t["song_my_likes"] = int(t.get("song_my_likes") or 0)
        t["song_liked_by_me"] = bool(t["song_my_likes"] > 0)
        t["is_first_discovery_here"] = (
            t["first_added_by_user_id"] == playlist["user_id"]
            and t["first_added_playlist_id"] == playlist["id"]
        )
        # Also list other playlists that contain this track
        cur.execute(
            """
            SELECT p2.id AS playlist_id, p2.title AS playlist_title, u2.id AS user_id, u2.username
            FROM playlist_tracks pt2
            JOIN playlists p2 ON p2.id = pt2.playlist_id
            JOIN users u2 ON u2.id = p2.user_id
            WHERE pt2.video_id = ? AND pt2.playlist_id != ?
            ORDER BY pt2.created_at ASC
            """,
            (t["video_id"], playlist_id),
        )
        t["other_playlists"] = [dict(r) for r in cur.fetchall()]
        tracks.append(t)

    playlist["tracks"] = tracks
    playlist["comments"], playlist["comments_has_more"] = fetch_comments_page(conn, "playlist", str(playlist_id))
    return playlist


@app.post("/api/playlists")
async def create_playlist(req: CreatePlaylistRequest, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    req.user_id = resolve_actor(request, req.user_id)
    title = req.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Playlist title is required")
    entries = extract_video_entries(req.initial_urls) if (req.initial_urls or "").strip() else []
    ensure_entry_count(len(entries))
    if entries:
        enforce_rate_limit(request, "add")

    ts = now_str()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO playlists (user_id, title, description, plays, created_at, updated_at)
        VALUES (?, ?, ?, 0, ?, ?)
        """,
        (req.user_id, title, (req.description or "").strip(), ts, ts),
    )
    playlist_id = cur.lastrowid

    cur.execute(
        """
        INSERT INTO activity_log (actor_user_id, action_type, summary, link_type, link_id, created_at)
        VALUES (?, 'created_playlist', ?, 'playlist', ?, ?)
        """,
        (req.user_id, f"published playlist '{title}'", str(playlist_id), ts),
    )
    conn.commit()

    added_tracks = []
    skipped = []
    for entry in entries:
        try:
            res = await add_single_video_to_playlist(
                conn=conn,
                playlist_id=playlist_id,
                user_id=req.user_id,
                video_id=entry["video_id"],
                start_seconds=entry["start_seconds"],
                end_seconds=entry["end_seconds"],
            )
        except VideoLookupFailed:
            skipped.append({"video_id": entry["video_id"], "reason": "not_found"})
            continue
        added_tracks.append(res)

    new_badges = evaluate_and_award_badges(conn, req.user_id)
    return {
        "playlist_id": playlist_id,
        "added_tracks": added_tracks,
        "skipped": skipped,
        "new_badges": new_badges,
    }


@app.put("/api/playlists/{playlist_id}")
def update_playlist(
    playlist_id: int, req: UpdatePlaylistRequest, request: Request, conn: sqlite3.Connection = Depends(get_db)
):
    req.user_id = resolve_actor(request, req.user_id)
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM playlists WHERE id = ?", (playlist_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Playlist not found")
    if row["user_id"] != req.user_id:
        raise HTTPException(status_code=403, detail="Only the playlist creator can edit liner notes")

    cur.execute(
        "UPDATE playlists SET title = ?, description = ?, updated_at = ? WHERE id = ?",
        (req.title.strip(), req.description.strip(), now_str(), playlist_id),
    )
    conn.commit()
    new_badges = evaluate_and_award_badges(conn, req.user_id)
    return {"ok": True, "new_badges": new_badges}


def derive_grab_source(
    conn: sqlite3.Connection,
    source_playlist_id: Optional[int],
    claimed_source_user_id: Optional[int],
    video_id: str,
) -> Optional[Tuple[int, int]]:
    """Server-side provenance for a grab: (source_playlist_id, source_user_id) or None.

    The source playlist must exist and contain the video, and the credited user is
    always that playlist's owner; otherwise the track is added with no provenance
    (a stale page can still grab, it just can't credit anyone). A client-sent
    source_user_id is never stored; on its own it only means "credit the song's
    first finder", which is checked against songs.first_added_by_user_id and the
    finder's debut playlist.
    """
    if source_playlist_id:
        return verified_source(conn, source_playlist_id, video_id)
    if claimed_source_user_id:
        song = conn.execute(
            "SELECT first_added_by_user_id, first_added_playlist_id FROM songs WHERE video_id = ?",
            (video_id,),
        ).fetchone()
        if song and song["first_added_by_user_id"] == claimed_source_user_id:
            src = verified_source(conn, song["first_added_playlist_id"], video_id)
            if src and src[1] == claimed_source_user_id:
                return src
    return None


@app.post("/api/playlists/{playlist_id}/tracks", dependencies=[rate_limit("add")])
async def add_tracks_to_playlist(
    playlist_id: int, req: AddTracksRequest, request: Request, conn: sqlite3.Connection = Depends(get_db)
):
    req.user_id = resolve_actor(request, req.user_id)
    cur = conn.cursor()
    require_playlist_owner(conn, playlist_id, req.user_id)

    entries = extract_video_entries(req.urls_or_ids)
    if not entries:
        raise HTTPException(
            status_code=400,
            detail="Could not find a valid YouTube link or 11-character video ID. Try pasting any youtube.com/watch?v=... or youtu.be/... link!",
        )
    ensure_entry_count(len(entries))
    ensure_playlist_capacity(conn, playlist_id, len(entries))

    explicit_start = parse_timestamp_seconds(req.start_time)
    explicit_end = parse_timestamp_seconds(req.end_time)

    # Verify provenance for every entry before writing anything.
    sources = {
        e["video_id"]: derive_grab_source(conn, req.source_playlist_id, req.source_user_id, e["video_id"])
        for e in entries
    }

    results = []
    skipped = []
    for entry in entries:
        vid = entry["video_id"]
        start_sec = explicit_start if explicit_start is not None else entry["start_seconds"]
        end_sec = explicit_end if explicit_end is not None else entry["end_seconds"]
        src = sources[vid]
        try:
            res = await add_single_video_to_playlist(
                conn=conn,
                playlist_id=playlist_id,
                user_id=req.user_id,
                video_id=vid,
                track_note=req.track_note or "",
                custom_title=req.custom_title if len(entries) == 1 else None,
                start_seconds=start_sec,
                end_seconds=end_sec,
                source_playlist_id=src[0] if src else None,
                source_user_id=src[1] if src else None,
            )
        except VideoLookupFailed:
            skipped.append({"video_id": vid, "reason": "not_found"})
            continue
        results.append(res)

    if not results:
        raise HTTPException(
            status_code=400,
            detail="YouTube couldn't find that video (it may be private, deleted or mistyped).",
        )

    # Evaluate badges for the user who added/grabbed the track
    new_badges = evaluate_and_award_badges(conn, req.user_id)

    # Also evaluate badges for the (verified) source curators and original song discoverers
    others = {src[1] for src in sources.values() if src and src[1] != req.user_id}
    for res in results:
        cur.execute("SELECT first_added_by_user_id FROM songs WHERE video_id = ?", (res["video_id"],))
        srow = cur.fetchone()
        if srow and srow["first_added_by_user_id"] != req.user_id:
            others.add(srow["first_added_by_user_id"])
    for uid in others:
        evaluate_and_award_badges(conn, uid)

    return {
        "results": results,
        "skipped": skipped,
        "new_badges": new_badges,
    }


@app.put("/api/playlists/{playlist_id}/tracks/{track_id}")
async def update_playlist_track(
    playlist_id: int,
    track_id: int,
    req: UpdateTrackRequest,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
):
    req.user_id = resolve_actor(request, req.user_id)
    require_playlist_owner(conn, playlist_id, req.user_id)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT pt.*, p.user_id AS owner_id, s.title AS current_song_title, s.channel_title AS current_channel_title
        FROM playlist_tracks pt
        JOIN playlists p ON p.id = pt.playlist_id
        JOIN songs s ON s.video_id = pt.video_id
        WHERE pt.id = ? AND pt.playlist_id = ?
        """,
        (track_id, playlist_id),
    )
    track = cur.fetchone()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found in playlist")

    migrated_video_id = track["video_id"]
    current_row_title = track["display_title"] or track["current_song_title"]
    migrated_title = current_row_title
    migrated_channel = track["current_channel_title"]
    orig_from_vid = track["migrated_from_video_id"]
    orig_from_title = track["migrated_from_title"]

    if req.new_video_id and req.new_video_id.strip():
        vids = extract_video_ids(req.new_video_id)
        if not vids:
            raise HTTPException(
                status_code=400,
                detail="Could not find a valid YouTube link or 11-character video ID for migration.",
            )
        new_vid = vids[0]
        ts = now_str()

        cur.execute("SELECT * FROM songs WHERE video_id = ?", (new_vid,))
        srow = cur.fetchone()
        if not srow:
            try:
                meta = await lookup_new_song_metadata(new_vid)
            except VideoLookupFailed:
                raise HTTPException(
                    status_code=400,
                    detail="YouTube couldn't find that video (it may be private, deleted or mistyped).",
                )
            cur.execute(
                """
                INSERT INTO songs (video_id, title, channel_title, thumbnail_url, first_added_by_user_id, first_added_playlist_id, first_added_at, total_plays)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    new_vid,
                    meta["title"],
                    meta["channel_title"],
                    meta["thumbnail_url"],
                    req.user_id,
                    playlist_id,
                    ts,
                ),
            )
            migrated_title = meta["title"]
            migrated_channel = meta["channel_title"]
            cur.execute(
                """
                INSERT INTO activity_log (actor_user_id, action_type, summary, link_type, link_id, created_at)
                VALUES (?, 'discovered_song', ?, 'song', ?, ?)
                """,
                (
                    req.user_id,
                    f"swapped a track to embeddable version '{migrated_title}'",
                    new_vid,
                    ts,
                ),
            )
        else:
            migrated_title = srow["title"]
            migrated_channel = srow["channel_title"]

        # The custom title (if any) is this row's display title; it never renames the song.
        new_display_title = clean_display_title(req.custom_title, migrated_title)
        if new_display_title:
            migrated_title = new_display_title

        if new_vid != track["video_id"]:
            if orig_from_vid and new_vid == orig_from_vid:
                # Reverted back to the original un-migrated track
                orig_from_vid = None
                orig_from_title = None
            elif not orig_from_vid:
                orig_from_vid = track["video_id"]
                orig_from_title = f"{current_row_title}" + (
                    f" ({track['current_channel_title']})"
                    if track["current_channel_title"] and track["current_channel_title"] not in ("YouTube", "YouTube Artist")
                    else ""
                )

        cur.execute(
            """
            UPDATE playlist_tracks
            SET video_id = ?, migrated_from_video_id = ?, migrated_from_title = ?, display_title = ?
            WHERE id = ? AND playlist_id = ?
            """,
            (new_vid, orig_from_vid, orig_from_title, new_display_title, track_id, playlist_id),
        )
        migrated_video_id = new_vid
    elif req.custom_title is not None:
        # Rename just this row (an empty string goes back to the song's real title).
        new_display_title = clean_display_title(req.custom_title, track["current_song_title"])
        cur.execute(
            "UPDATE playlist_tracks SET display_title = ? WHERE id = ? AND playlist_id = ?",
            (new_display_title, track_id, playlist_id),
        )
        migrated_title = new_display_title or track["current_song_title"]

    if req.track_note is not None:
        cur.execute(
            "UPDATE playlist_tracks SET track_note = ? WHERE id = ?",
            (req.track_note.strip(), track_id),
        )

    final_start = track["start_seconds"]
    final_end = track["end_seconds"]
    if req.update_clip or req.start_time is not None or req.end_time is not None:
        start_sec = parse_timestamp_seconds(req.start_time)
        end_sec = parse_timestamp_seconds(req.end_time)
        if start_sec is not None and end_sec is not None and end_sec <= start_sec:
            raise HTTPException(
                status_code=400,
                detail="End time must be greater than start time.",
            )
        cur.execute(
            "UPDATE playlist_tracks SET start_seconds = ?, end_seconds = ? WHERE id = ?",
            (start_sec, end_sec, track_id),
        )
        final_start = start_sec
        final_end = end_sec

    if req.direction in ("up", "down"):
        cur.execute(
            "SELECT id, position FROM playlist_tracks WHERE playlist_id = ? ORDER BY position ASC, id ASC",
            (playlist_id,),
        )
        ordered = [dict(r) for r in cur.fetchall()]
        idx = next((i for i, item in enumerate(ordered) if item["id"] == track_id), None)
        if idx is not None:
            swap_idx = idx - 1 if req.direction == "up" else idx + 1
            if 0 <= swap_idx < len(ordered):
                ordered[idx], ordered[swap_idx] = ordered[swap_idx], ordered[idx]
                for pos, item in enumerate(ordered, start=1):
                    cur.execute(
                        "UPDATE playlist_tracks SET position = ? WHERE id = ?",
                        (pos, item["id"]),
                    )

    cur.execute("UPDATE playlists SET updated_at = ? WHERE id = ?", (now_str(), playlist_id))
    conn.commit()
    new_badges = evaluate_and_award_badges(conn, req.user_id)
    return {
        "ok": True,
        "video_id": migrated_video_id,
        "title": migrated_title,
        "channel_title": migrated_channel,
        "migrated_from_video_id": orig_from_vid,
        "migrated_from_title": orig_from_title,
        "start_seconds": final_start,
        "end_seconds": final_end,
        "new_badges": new_badges,
    }


@app.post("/api/playlists/import/inspect", dependencies=[rate_limit("inspect")])
async def inspect_playlist_import(req: InspectPlaylistImportRequest, request: Request):
    if auth_enabled():
        resolve_actor(request, None)
    try:
        report = await inspect_youtube_playlist_or_urls(
            req.source_input,
            max_tracks=max(1, min(req.max_tracks, 50)),
        )
        return report
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/playlists/import/commit", dependencies=[rate_limit("add")])
async def commit_playlist_import(
    req: CommitPlaylistImportRequest,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
):
    req.user_id = resolve_actor(request, req.user_id)
    if req.target_playlist_id:
        require_playlist_owner(conn, req.target_playlist_id, req.user_id)
    if not req.tracks:
        raise HTTPException(status_code=400, detail="Select at least one track to import.")
    ensure_entry_count(len(req.tracks))
    if req.target_playlist_id:
        ensure_playlist_capacity(conn, req.target_playlist_id, len(req.tracks))

    cur = conn.cursor()
    ts = now_str()

    if req.target_playlist_id:
        cur.execute("SELECT id, title FROM playlists WHERE id = ?", (req.target_playlist_id,))
        pl_row = cur.fetchone()
        if not pl_row:
            raise HTTPException(status_code=404, detail="Target playlist not found")
        playlist_id = pl_row["id"]
        playlist_title = pl_row["title"]
    else:
        playlist_title = (req.title or "").strip() or "Imported YouTube Playlist"
        cur.execute(
            """
            INSERT INTO playlists (user_id, title, description, plays, created_at, updated_at)
            VALUES (?, ?, ?, 0, ?, ?)
            """,
            (req.user_id, playlist_title, (req.description or "").strip(), ts, ts),
        )
        playlist_id = cur.lastrowid
        cur.execute(
            """
            INSERT INTO activity_log (actor_user_id, action_type, summary, link_type, link_id, created_at)
            VALUES (?, 'created_playlist', ?, 'playlist', ?, ?)
            """,
            (req.user_id, f"imported YouTube playlist '{playlist_title}'", str(playlist_id), ts),
        )
        conn.commit()

    added_tracks = []
    skipped = []
    for item in req.tracks:
        vids = extract_video_ids(item.video_id)
        if not vids:
            continue
        vid = vids[0]
        start_sec = parse_timestamp_seconds(item.start_seconds)
        end_sec = parse_timestamp_seconds(item.end_seconds)
        try:
            res = await add_single_video_to_playlist(
                conn=conn,
                playlist_id=playlist_id,
                user_id=req.user_id,
                video_id=vid,
                track_note=item.track_note or "",
                custom_title=item.custom_title,
                start_seconds=start_sec,
                end_seconds=end_sec,
                migrated_from_video_id=item.migrated_from_video_id,
                migrated_from_title=item.migrated_from_title,
            )
        except VideoLookupFailed:
            skipped.append({"video_id": vid, "reason": "not_found"})
            continue
        added_tracks.append(res)

    new_badges = evaluate_and_award_badges(conn, req.user_id)
    return {
        "playlist_id": playlist_id,
        "title": playlist_title,
        "added_tracks": added_tracks,
        "skipped": skipped,
        "new_badges": new_badges,
    }


@app.get("/api/playlists/{playlist_id}/audit-embeds")
async def audit_playlist_embeds(
    playlist_id: int,
    request: Request,
    user_id: Optional[int] = Query(None),
    conn: sqlite3.Connection = Depends(get_db),
):
    # Owner-only: each unique track costs outbound YouTube requests.
    require_playlist_owner(conn, playlist_id, resolve_actor(request, user_id))
    enforce_rate_limit(request, "audit")
    cur = conn.cursor()

    cur.execute(
        """
        SELECT pt.id AS track_id, pt.video_id, pt.position, pt.track_note,
               pt.start_seconds, pt.end_seconds,
               pt.migrated_from_video_id, pt.migrated_from_title,
               COALESCE(pt.display_title, s.title) AS title, s.channel_title, s.thumbnail_url
        FROM playlist_tracks pt
        JOIN songs s ON s.video_id = pt.video_id
        WHERE pt.playlist_id = ?
        ORDER BY pt.position ASC, pt.id ASC
        """,
        (playlist_id,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    if not rows:
        return {
            "playlist_id": playlist_id,
            "total_tracks": 0,
            "embeddable_count": 0,
            "non_embeddable_count": 0,
            "tracks": [],
        }

    sem = asyncio.Semaphore(10)
    async with httpx.AsyncClient(timeout=6.5) as client:
        async def check_one(video_id: str) -> Dict[str, Any]:
            async with sem:
                return await check_video_embeddability(video_id, client=client)

        # Each distinct video is checked once, however many times it appears.
        unique_ids = list(dict.fromkeys(r["video_id"] for r in rows))
        infos = dict(zip(unique_ids, await asyncio.gather(*(check_one(v) for v in unique_ids))))

    audited = []
    for r in rows:
        info = infos.get(r["video_id"]) or {}
        base_title = r.get("migrated_from_title") or r["title"] or info.get("title") or r["video_id"]
        audited.append(
            {
                **r,
                "embeddable": bool(info.get("embeddable")),
                "status": info.get("status", "UNKNOWN"),
                "reason": info.get("reason"),
                "suggested_query": clean_migration_query(
                    base_title,
                    "" if r.get("migrated_from_title") else (r["channel_title"] or info.get("channel_title") or ""),
                ),
            }
        )

    embeddable_count = sum(1 for t in audited if t["embeddable"])
    return {
        "playlist_id": playlist_id,
        "total_tracks": len(audited),
        "embeddable_count": embeddable_count,
        "non_embeddable_count": len(audited) - embeddable_count,
        "tracks": list(audited),
    }


@app.delete("/api/playlists/{playlist_id}/tracks/{track_id}")
def delete_playlist_track(
    playlist_id: int,
    track_id: int,
    request: Request,
    user_id: Optional[int] = Query(None),
    conn: sqlite3.Connection = Depends(get_db),
):
    require_playlist_owner(conn, playlist_id, resolve_actor(request, user_id))
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM playlist_tracks WHERE id = ? AND playlist_id = ?",
        (track_id, playlist_id),
    )
    # Re-normalize positions
    cur.execute(
        "SELECT id FROM playlist_tracks WHERE playlist_id = ? ORDER BY position ASC, id ASC",
        (playlist_id,),
    )
    for pos, row in enumerate(cur.fetchall(), start=1):
        cur.execute("UPDATE playlist_tracks SET position = ? WHERE id = ?", (pos, row["id"]))
    conn.commit()
    return {"ok": True}


@app.put("/api/playlists/{playlist_id}/reorder")
def reorder_playlist_tracks(
    playlist_id: int, req: ReorderTracksRequest, request: Request, conn: sqlite3.Connection = Depends(get_db)
):
    req.user_id = resolve_actor(request, req.user_id)
    require_playlist_owner(conn, playlist_id, req.user_id)
    cur = conn.cursor()

    cur.execute(
        "SELECT id FROM playlist_tracks WHERE playlist_id = ? ORDER BY position ASC, id ASC",
        (playlist_id,),
    )
    existing_ids = [r["id"] for r in cur.fetchall()]
    existing_set = set(existing_ids)

    ordered_ids: List[int] = []
    seen = set()
    for tid in req.track_ids:
        if tid in existing_set and tid not in seen:
            ordered_ids.append(tid)
            seen.add(tid)
    for tid in existing_ids:
        if tid not in seen:
            ordered_ids.append(tid)

    for pos, tid in enumerate(ordered_ids, start=1):
        cur.execute(
            "UPDATE playlist_tracks SET position = ? WHERE id = ? AND playlist_id = ?",
            (pos, tid, playlist_id),
        )

    cur.execute("UPDATE playlists SET updated_at = ? WHERE id = ?", (now_str(), playlist_id))
    conn.commit()
    return {"ok": True, "ordered_track_ids": ordered_ids}


@app.put("/api/playlists/{playlist_id}/snapshot")
def restore_playlist_snapshot(
    playlist_id: int,
    req: RestorePlaylistSnapshotRequest,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
):
    """Undo stopgap (replaced by the op log in Phase 1).

    Only the owner's own content is taken from the client: order, notes, clips,
    migration notes and display titles. Attribution (added_by_user_id, source_*,
    created_at) is never trusted: a row whose id already belongs to this playlist
    keeps the attribution it has on the server; any other row is attributed to the
    actor, now, and only gets a source if that source playlist really contains it.
    """
    req.user_id = resolve_actor(request, req.user_id)
    require_playlist_owner(conn, playlist_id, req.user_id)
    cur = conn.cursor()

    if len(req.tracks) > MAX_TRACKS_PER_PLAYLIST:
        raise HTTPException(
            status_code=400,
            detail=f"Playlists can hold at most {MAX_TRACKS_PER_PLAYLIST} tracks.",
        )
    video_ids = {t.video_id for t in req.tracks}
    bad = [v for v in video_ids if not VIDEO_ID_PATTERN.match(v)]
    if bad:
        raise HTTPException(status_code=400, detail="Snapshot contains an invalid video id.")
    if video_ids:
        placeholders = ",".join("?" * len(video_ids))
        known = {
            r["video_id"]
            for r in cur.execute(
                f"SELECT video_id FROM songs WHERE video_id IN ({placeholders})", tuple(video_ids)
            ).fetchall()
        }
        if known != video_ids:
            raise HTTPException(status_code=400, detail="Snapshot contains a track that isn't on Webjam.")

    existing = {
        r["id"]: r
        for r in cur.execute("SELECT * FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,)).fetchall()
    }

    ts = now_str()
    cur.execute(
        "UPDATE playlists SET title = ?, description = ?, updated_at = ? WHERE id = ?",
        (req.title.strip() or "Untitled Playlist", (req.description or "").strip(), ts, playlist_id),
    )

    cur.execute("DELETE FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,))
    used_ids = set()
    for idx, t in enumerate(req.tracks, start=1):
        orig = existing.get(t.id) if t.id is not None and t.id not in used_ids else None
        row_id: Optional[int] = None
        if orig is not None:
            row_id = orig["id"]
            added_by = orig["added_by_user_id"]
            source_pid, source_uid = orig["source_playlist_id"], orig["source_user_id"]
            created_at = orig["created_at"]
            fallback_display = orig["display_title"] if orig["video_id"] == t.video_id else None
        else:
            # Reuse a client id only if no row anywhere holds it (e.g. undoing a delete).
            if t.id is not None and t.id > 0 and t.id not in used_ids:
                if not cur.execute("SELECT 1 FROM playlist_tracks WHERE id = ?", (t.id,)).fetchone():
                    row_id = t.id
            added_by = req.user_id
            src = verified_source(conn, t.source_playlist_id, t.video_id)
            source_pid, source_uid = (src[0], src[1]) if src else (None, None)
            created_at = ts
            fallback_display = None
        display_title = (
            ((t.display_title or "").strip()[:MAX_TITLE] or None)
            if "display_title" in t.model_fields_set
            else fallback_display
        )
        migrated_vid = (
            t.migrated_from_video_id
            if t.migrated_from_video_id and VIDEO_ID_PATTERN.match(t.migrated_from_video_id)
            else None
        )
        cur.execute(
            """
            INSERT INTO playlist_tracks (
                id, playlist_id, video_id, position, track_note,
                start_seconds, end_seconds,
                migrated_from_video_id, migrated_from_title, display_title,
                added_by_user_id, source_playlist_id, source_user_id, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row_id,
                playlist_id,
                t.video_id,
                idx,
                (t.track_note or "").strip(),
                t.start_seconds,
                t.end_seconds,
                migrated_vid,
                (t.migrated_from_title or "").strip() or None,
                display_title,
                added_by,
                source_pid,
                source_uid,
                created_at,
            ),
        )
        used_ids.add(cur.lastrowid)

    conn.commit()
    return {"ok": True}



@app.post("/api/playlists/{playlist_id}/like", dependencies=[rate_limit("like")])
def toggle_playlist_like(
    playlist_id: int, req: ToggleLikeRequest, request: Request, conn: sqlite3.Connection = Depends(get_db)
):
    req.user_id = resolve_actor(request, req.user_id)
    cur = conn.cursor()
    cur.execute("SELECT id, user_id, title FROM playlists WHERE id = ?", (playlist_id,))
    pl = cur.fetchone()
    if not pl:
        raise HTTPException(status_code=404, detail="Playlist not found")

    cur.execute(
        "SELECT count FROM playlist_likes WHERE playlist_id = ? AND user_id = ?",
        (playlist_id, req.user_id),
    )
    existing = cur.fetchone()
    prev_count = int(existing["count"]) if existing else 0
    ts = now_str()

    if req.reset or req.count <= 0:
        cur.execute(
            "DELETE FROM playlist_likes WHERE playlist_id = ? AND user_id = ?",
            (playlist_id, req.user_id),
        )
        my_likes = 0
    else:
        inc = max(1, min(req.count, 50))
        if existing:
            my_likes = prev_count + inc
            cur.execute(
                "UPDATE playlist_likes SET count = ?, created_at = ? WHERE playlist_id = ? AND user_id = ?",
                (my_likes, ts, playlist_id, req.user_id),
            )
        else:
            my_likes = inc
            cur.execute(
                "INSERT INTO playlist_likes (playlist_id, user_id, count, created_at) VALUES (?, ?, ?, ?)",
                (playlist_id, req.user_id, my_likes, ts),
            )

        if prev_count == 0:
            cur.execute(
                """
                INSERT INTO activity_log (actor_user_id, action_type, summary, link_type, link_id, created_at)
                VALUES (?, 'liked_playlist', ?, 'playlist', ?, ?)
                """,
                (req.user_id, f"co-signed playlist '{pl['title']}'", str(playlist_id), ts),
            )
        elif prev_count < 10 <= my_likes:
            cur.execute(
                """
                INSERT INTO activity_log (actor_user_id, action_type, summary, link_type, link_id, created_at)
                VALUES (?, 'superloved_playlist', ?, 'playlist', ?, ?)
                """,
                (req.user_id, f"bestowed Superlove ({my_likes}× ♥) on playlist '{pl['title']}'", str(playlist_id), ts),
            )

    conn.commit()
    actor_badges = evaluate_and_award_badges(conn, req.user_id)
    owner_badges = (
        evaluate_and_award_badges(conn, pl["user_id"])
        if req.user_id != pl["user_id"]
        else []
    )
    cur.execute(
        "SELECT COALESCE(SUM(count), 0) AS total_cnt, COUNT(*) AS lover_cnt FROM playlist_likes WHERE playlist_id = ?",
        (playlist_id,),
    )
    agg = cur.fetchone()
    like_count = int(agg["total_cnt"])
    lover_count = int(agg["lover_cnt"])
    cur.execute(
        """
        SELECT u.id, u.username, u.display_name, pl.count
        FROM playlist_likes pl
        JOIN users u ON u.id = pl.user_id
        WHERE pl.playlist_id = ?
        ORDER BY pl.count DESC, pl.created_at ASC
        """,
        (playlist_id,),
    )
    liked_by_users = [dict(r) for r in cur.fetchall()]

    return {
        "playlist_id": playlist_id,
        "liked": bool(my_likes > 0),
        "superloved": bool(my_likes >= 5),
        "my_likes": my_likes,
        "like_count": like_count,
        "lover_count": lover_count,
        "liked_by_users": liked_by_users,
        "new_badges": actor_badges,
        "owner_new_badges": owner_badges,
    }


@app.post("/api/songs/{video_id}/like", dependencies=[rate_limit("like")])
def toggle_song_like(
    video_id: str, req: ToggleLikeRequest, request: Request, conn: sqlite3.Connection = Depends(get_db)
):
    req.user_id = resolve_actor(request, req.user_id)
    if not VIDEO_ID_PATTERN.match(video_id):
        raise HTTPException(status_code=404, detail="Song not found in Webjam")
    cur = conn.cursor()
    cur.execute("SELECT video_id, title, first_added_by_user_id FROM songs WHERE video_id = ?", (video_id,))
    song = cur.fetchone()
    if not song:
        raise HTTPException(status_code=404, detail="Song not found in Webjam")

    cur.execute(
        "SELECT count FROM song_likes WHERE video_id = ? AND user_id = ?",
        (video_id, req.user_id),
    )
    existing = cur.fetchone()
    prev_count = int(existing["count"]) if existing else 0
    ts = now_str()

    if req.reset or req.count <= 0:
        cur.execute(
            "DELETE FROM song_likes WHERE video_id = ? AND user_id = ?",
            (video_id, req.user_id),
        )
        my_likes = 0
    else:
        inc = max(1, min(req.count, 50))
        if existing:
            my_likes = prev_count + inc
            cur.execute(
                "UPDATE song_likes SET count = ?, created_at = ? WHERE video_id = ? AND user_id = ?",
                (my_likes, ts, video_id, req.user_id),
            )
        else:
            my_likes = inc
            cur.execute(
                "INSERT INTO song_likes (video_id, user_id, count, created_at) VALUES (?, ?, ?, ?)",
                (video_id, req.user_id, my_likes, ts),
            )

        if prev_count == 0:
            cur.execute(
                """
                INSERT INTO activity_log (actor_user_id, action_type, summary, link_type, link_id, created_at)
                VALUES (?, 'liked_song', ?, 'song', ?, ?)
                """,
                (req.user_id, f"loved track '{song['title']}'", video_id, ts),
            )
        elif prev_count < 10 <= my_likes:
            cur.execute(
                """
                INSERT INTO activity_log (actor_user_id, action_type, summary, link_type, link_id, created_at)
                VALUES (?, 'superloved_song', ?, 'song', ?, ?)
                """,
                (req.user_id, f"bestowed Superlove ({my_likes}× ♥) on '{song['title']}'", video_id, ts),
            )

    conn.commit()
    actor_badges = evaluate_and_award_badges(conn, req.user_id)
    finder_badges = (
        evaluate_and_award_badges(conn, song["first_added_by_user_id"])
        if req.user_id != song["first_added_by_user_id"]
        else []
    )
    cur.execute(
        "SELECT COALESCE(SUM(count), 0) AS total_cnt, COUNT(*) AS lover_cnt FROM song_likes WHERE video_id = ?",
        (video_id,),
    )
    agg = cur.fetchone()
    like_count = int(agg["total_cnt"])
    lover_count = int(agg["lover_cnt"])
    cur.execute(
        """
        SELECT u.id, u.username, u.display_name, sl.count
        FROM song_likes sl
        JOIN users u ON u.id = sl.user_id
        WHERE sl.video_id = ?
        ORDER BY sl.count DESC, sl.created_at ASC
        """,
        (video_id,),
    )
    liked_by_users = [dict(r) for r in cur.fetchall()]

    return {
        "video_id": video_id,
        "title": song["title"],
        "liked": bool(my_likes > 0),
        "superloved": bool(my_likes >= 5),
        "my_likes": my_likes,
        "like_count": like_count,
        "lover_count": lover_count,
        "liked_by_users": liked_by_users,
        "new_badges": actor_badges,
        "owner_new_badges": finder_badges,
    }


_play_record_lock = threading.Lock()


def _play_badge_eval_due(owner_id: int) -> bool:
    """Play-driven badge evaluation runs at most once a minute per playlist owner."""
    now = time.monotonic()
    with _play_badge_eval_lock:
        last = _last_play_badge_eval.get(owner_id)
        if last is not None and now - last < PLAY_BADGE_EVAL_INTERVAL:
            return False
        _last_play_badge_eval[owner_id] = now
        return True


@app.post("/api/playlists/{playlist_id}/play", dependencies=[rate_limit("play")])
def record_playlist_play(
    playlist_id: int, req: RecordPlayRequest, request: Request, conn: sqlite3.Connection = Depends(get_db)
):
    """Count one play. req.count is ignored; the same listener (user, else IP) counts at
    most once per playlist+track every 30 minutes, and video_id must be in the playlist."""
    cur = conn.cursor()
    cur.execute("SELECT id, user_id, plays FROM playlists WHERE id = ?", (playlist_id,))
    pl = cur.fetchone()
    if not pl:
        raise HTTPException(status_code=404, detail="Playlist not found")

    video_id = (req.video_id or "").strip()
    if video_id:
        in_playlist = cur.execute(
            "SELECT 1 FROM playlist_tracks WHERE playlist_id = ? AND video_id = ?",
            (playlist_id, video_id),
        ).fetchone()
        if not in_playlist:
            raise HTTPException(status_code=400, detail="That track isn't in this playlist.")

    key = viewer_key(request)
    now = int(time.time())
    with _play_record_lock:
        cur.execute("DELETE FROM play_events WHERE at < ?", (now - PLAY_DEDUPE_SECONDS,))
        recent = cur.execute(
            "SELECT 1 FROM play_events WHERE viewer_key = ? AND playlist_id = ? AND video_id = ? AND at >= ?",
            (key, playlist_id, video_id, now - PLAY_DEDUPE_SECONDS),
        ).fetchone()
        counted = recent is None
        if counted:
            cur.execute(
                "INSERT INTO play_events (viewer_key, playlist_id, video_id, at) VALUES (?, ?, ?, ?)",
                (key, playlist_id, video_id, now),
            )
            cur.execute("UPDATE playlists SET plays = plays + 1 WHERE id = ?", (playlist_id,))
            if video_id:
                cur.execute("UPDATE songs SET total_plays = total_plays + 1 WHERE video_id = ?", (video_id,))
        conn.commit()

    owner_badges = (
        evaluate_and_award_badges(conn, pl["user_id"])
        if counted and _play_badge_eval_due(pl["user_id"])
        else []
    )
    cur.execute("SELECT plays FROM playlists WHERE id = ?", (playlist_id,))
    updated_plays = cur.fetchone()["plays"]

    return {
        "plays": updated_plays,
        "counted": counted,
        "owner_user_id": pl["user_id"],
        "new_badges": owner_badges,
    }


@app.get("/api/songs/{video_id}")
def get_song_page(
    video_id: str, request: Request, user_id: int = Query(1), conn: sqlite3.Connection = Depends(get_db)
):
    user_id = resolve_viewer(request, user_id)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT s.*,
               u.username AS finder_username, u.display_name AS finder_display_name, u.avatar_color AS finder_avatar_color,
               p.title AS debut_playlist_title,
               (SELECT COALESCE(SUM(sl.count), 0) FROM song_likes sl WHERE sl.video_id = s.video_id) AS like_count,
               (SELECT COALESCE(sl.count, 0) FROM song_likes sl WHERE sl.video_id = s.video_id AND sl.user_id = ?) AS my_likes
        FROM songs s
        JOIN users u ON u.id = s.first_added_by_user_id
        LEFT JOIN playlists p ON p.id = s.first_added_playlist_id
        WHERE s.video_id = ?
        """,
        (user_id, video_id),
    )
    song_row = cur.fetchone()
    if not song_row:
        raise HTTPException(status_code=404, detail="Song not found in Webjam")

    song = dict(song_row)
    song["my_likes"] = int(song.get("my_likes") or 0)
    song["liked_by_me"] = bool(song["my_likes"] > 0)

    cur.execute(
        """
        SELECT u.id, u.username, u.display_name, sl.count
        FROM song_likes sl
        JOIN users u ON u.id = sl.user_id
        WHERE sl.video_id = ?
        ORDER BY sl.count DESC, sl.created_at ASC
        """,
        (video_id,),
    )
    song["liked_by_users"] = [dict(r) for r in cur.fetchall()]

    # All playlist appearances ordered chronologically
    cur.execute(
        """
        SELECT pt.id AS track_entry_id, pt.playlist_id, pt.position, pt.track_note,
               pt.start_seconds, pt.end_seconds,
               pt.source_playlist_id, pt.source_user_id, pt.created_at,
               p.title AS playlist_title, p.plays AS playlist_plays,
               u.id AS user_id, u.username, u.display_name, u.avatar_color,
               su.username AS source_username, su.display_name AS source_display_name,
               sp.title AS source_playlist_title,
               (SELECT COALESCE(SUM(pl.count), 0) FROM playlist_likes pl WHERE pl.playlist_id = p.id) AS playlist_likes
        FROM playlist_tracks pt
        JOIN playlists p ON p.id = pt.playlist_id
        JOIN users u ON u.id = p.user_id
        LEFT JOIN users su ON su.id = pt.source_user_id
        LEFT JOIN playlists sp ON sp.id = pt.source_playlist_id
        WHERE pt.video_id = ?
        ORDER BY pt.created_at ASC, pt.id ASC
        """,
        (video_id,),
    )
    song["appearances"] = [dict(r) for r in cur.fetchall()]
    song["comments"], song["comments_has_more"] = fetch_comments_page(conn, "song", video_id)
    return song


@app.get("/api/users/{profile_user_id}")
def get_user_profile(
    profile_user_id: int, request: Request, viewer_id: int = Query(1), conn: sqlite3.Connection = Depends(get_db)
):
    viewer_id = resolve_viewer(request, viewer_id)
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE id = ?", (profile_user_id,))
    urow = cur.fetchone()
    if not urow:
        raise HTTPException(status_code=404, detail="User not found")

    user = dict(urow)
    user.pop("password_hash", None)
    user.pop("session_epoch", None)

    badge_data = get_user_badge_progress(conn, profile_user_id)

    # User's playlists
    cur.execute(
        """
        SELECT p.*,
               (SELECT COUNT(*) FROM playlist_tracks pt WHERE pt.playlist_id = p.id) AS track_count,
               (SELECT COALESCE(SUM(pl.count), 0) FROM playlist_likes pl WHERE pl.playlist_id = p.id) AS like_count,
               (SELECT COALESCE(pl.count, 0) FROM playlist_likes pl WHERE pl.playlist_id = p.id AND pl.user_id = ?) AS my_likes,
               (SELECT COUNT(*) FROM playlist_tracks pt WHERE pt.source_playlist_id = p.id AND pt.added_by_user_id != p.user_id) AS times_grabbed_from
        FROM playlists p
        WHERE p.user_id = ?
        ORDER BY p.updated_at DESC, p.id DESC
        """,
        (viewer_id, profile_user_id),
    )
    playlists = []
    for prow in cur.fetchall():
        pl = dict(prow)
        pl["my_likes"] = int(pl.get("my_likes") or 0)
        pl["liked_by_me"] = bool(pl["my_likes"] > 0)
        cur.execute(
            """
            SELECT pt.id, pt.video_id, pt.position, pt.track_note, pt.start_seconds, pt.end_seconds,
                   COALESCE(pt.display_title, s.title) AS title, s.title AS song_title, s.channel_title,
                   su.username AS source_username,
                   (SELECT COALESCE(SUM(sl.count), 0) FROM song_likes sl WHERE sl.video_id = pt.video_id) AS song_like_count,
                   (SELECT COALESCE(sl.count, 0) FROM song_likes sl WHERE sl.video_id = pt.video_id AND sl.user_id = ?) AS song_my_likes
            FROM playlist_tracks pt
            JOIN songs s ON s.video_id = pt.video_id
            LEFT JOIN users su ON su.id = pt.source_user_id
            WHERE pt.playlist_id = ?
            ORDER BY pt.position ASC
            """,
            (viewer_id, pl["id"]),
        )
        tracks = []
        for t in cur.fetchall():
            td = dict(t)
            td["song_my_likes"] = int(td.get("song_my_likes") or 0)
            td["song_liked_by_me"] = bool(td["song_my_likes"] > 0)
            tracks.append(td)
        pl["tracks"] = tracks
        playlists.append(pl)

    # Songs first discovered on Webjam by this user
    cur.execute(
        """
        SELECT s.*, p.title AS debut_playlist_title,
               (SELECT COUNT(DISTINCT pt.playlist_id) FROM playlist_tracks pt WHERE pt.video_id = s.video_id) AS playlist_count,
               (SELECT COALESCE(SUM(sl.count), 0) FROM song_likes sl WHERE sl.video_id = s.video_id) AS like_count,
               (SELECT COALESCE(sl.count, 0) FROM song_likes sl WHERE sl.video_id = s.video_id AND sl.user_id = ?) AS my_likes
        FROM songs s
        LEFT JOIN playlists p ON p.id = s.first_added_playlist_id
        WHERE s.first_added_by_user_id = ?
        ORDER BY s.first_added_at DESC
        """,
        (viewer_id, profile_user_id),
    )
    discovered_songs = []
    for r in cur.fetchall():
        sd = dict(r)
        sd["my_likes"] = int(sd.get("my_likes") or 0)
        sd["liked_by_me"] = bool(sd["my_likes"] > 0)
        sd["song_like_count"] = sd["like_count"]
        sd["song_my_likes"] = sd["my_likes"]
        sd["song_liked_by_me"] = sd["liked_by_me"]
        discovered_songs.append(sd)

    # Songs liked by this user
    cur.execute(
        """
        SELECT s.*, sl.created_at AS liked_at, sl.count AS profile_user_likes,
               fu.username AS finder_username,
               (SELECT COALESCE(SUM(sl2.count), 0) FROM song_likes sl2 WHERE sl2.video_id = s.video_id) AS like_count,
               (SELECT COALESCE(sl3.count, 0) FROM song_likes sl3 WHERE sl3.video_id = s.video_id AND sl3.user_id = ?) AS my_likes
        FROM song_likes sl
        JOIN songs s ON s.video_id = sl.video_id
        JOIN users fu ON fu.id = s.first_added_by_user_id
        WHERE sl.user_id = ?
        ORDER BY sl.count DESC, sl.created_at DESC
        """,
        (viewer_id, profile_user_id),
    )
    liked_songs = []
    for r in cur.fetchall():
        sd = dict(r)
        sd["my_likes"] = int(sd.get("my_likes") or 0)
        sd["liked_by_me"] = bool(sd["my_likes"] > 0)
        sd["song_like_count"] = sd["like_count"]
        sd["song_my_likes"] = sd["my_likes"]
        sd["song_liked_by_me"] = sd["liked_by_me"]
        liked_songs.append(sd)

    # Playlists liked by this user
    cur.execute(
        """
        SELECT p.id, p.user_id, p.title, p.description, p.plays, pl.created_at AS liked_at, pl.count AS profile_user_likes,
               u.username, u.display_name, u.avatar_color,
               (SELECT COUNT(*) FROM playlist_tracks pt WHERE pt.playlist_id = p.id) AS track_count,
               (SELECT COALESCE(SUM(pl2.count), 0) FROM playlist_likes pl2 WHERE pl2.playlist_id = p.id) AS like_count,
               (SELECT COALESCE(pl3.count, 0) FROM playlist_likes pl3 WHERE pl3.playlist_id = p.id AND pl3.user_id = ?) AS my_likes
        FROM playlist_likes pl
        JOIN playlists p ON p.id = pl.playlist_id
        JOIN users u ON u.id = p.user_id
        WHERE pl.user_id = ?
        ORDER BY pl.count DESC, pl.created_at DESC
        """,
        (viewer_id, profile_user_id),
    )
    liked_playlists = []
    for r in cur.fetchall():
        pd = dict(r)
        pd["my_likes"] = int(pd.get("my_likes") or 0)
        pd["liked_by_me"] = bool(pd["my_likes"] > 0)
        liked_playlists.append(pd)

    # Lineage: tracks others grabbed from this user
    cur.execute(
        """
        SELECT pt.created_at, pt.video_id, s.title AS song_title,
               u.id AS taker_user_id, u.username AS taker_username, u.display_name AS taker_display_name,
               tp.id AS target_playlist_id, tp.title AS target_playlist_title, tp.plays AS target_playlist_plays,
               sp.id AS source_playlist_id, sp.title AS source_playlist_title
        FROM playlist_tracks pt
        JOIN songs s ON s.video_id = pt.video_id
        JOIN users u ON u.id = pt.added_by_user_id
        JOIN playlists tp ON tp.id = pt.playlist_id
        LEFT JOIN playlists sp ON sp.id = pt.source_playlist_id
        WHERE pt.source_user_id = ? AND pt.added_by_user_id != ?
        ORDER BY pt.created_at DESC
        """,
        (profile_user_id, profile_user_id),
    )
    grabs_from_user = [dict(r) for r in cur.fetchall()]

    # Lineage: tracks this user grabbed from others
    cur.execute(
        """
        SELECT pt.created_at, pt.video_id, s.title AS song_title,
               su.id AS source_user_id, su.username AS source_username, su.display_name AS source_display_name,
               sp.id AS source_playlist_id, sp.title AS source_playlist_title,
               tp.id AS target_playlist_id, tp.title AS target_playlist_title, tp.plays AS target_playlist_plays
        FROM playlist_tracks pt
        JOIN songs s ON s.video_id = pt.video_id
        JOIN users su ON su.id = pt.source_user_id
        JOIN playlists tp ON tp.id = pt.playlist_id
        LEFT JOIN playlists sp ON sp.id = pt.source_playlist_id
        WHERE pt.added_by_user_id = ? AND pt.source_user_id IS NOT NULL AND pt.source_user_id != ?
        ORDER BY pt.created_at DESC
        """,
        (profile_user_id, profile_user_id),
    )
    grabs_by_user = [dict(r) for r in cur.fetchall()]
    profile_comments, profile_comments_more = fetch_comments_page(conn, "profile", str(profile_user_id))

    return {
        "user": user,
        "metrics": badge_data["metrics"],
        "earned_badges": badge_data["earned"],
        "badge_catalog": badge_data["catalog"],
        "playlists": playlists,
        "discovered_songs": discovered_songs,
        "liked_songs": liked_songs,
        "liked_playlists": liked_playlists,
        "grabs_from_user": grabs_from_user,
        "grabs_by_user": grabs_by_user,
        "comments": profile_comments,
        "comments_has_more": profile_comments_more,
    }


def resolve_comment_target(conn: sqlite3.Connection, target_type: str, target_id: str) -> Tuple[str, str]:
    """Validate a comment target. Returns (canonical target_id, activity label); 400/404 otherwise."""
    cur = conn.cursor()
    raw = (target_id or "").strip()
    if target_type in ("playlist", "profile"):
        if not raw.isdigit() or len(raw) > 18:
            raise HTTPException(status_code=400, detail=f"Invalid {target_type} id")
        num = int(raw)
        if target_type == "playlist":
            r = cur.execute("SELECT title FROM playlists WHERE id = ?", (num,)).fetchone()
            if not r:
                raise HTTPException(status_code=404, detail="Playlist not found")
            return str(num), f"playlist '{r['title']}'"
        r = cur.execute("SELECT username FROM users WHERE id = ?", (num,)).fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="User not found")
        return str(num), f"@{r['username']}'s profile"
    if target_type == "song":
        if not VIDEO_ID_PATTERN.match(raw):
            raise HTTPException(status_code=400, detail="Invalid song id")
        r = cur.execute("SELECT title FROM songs WHERE video_id = ?", (raw,)).fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="Song not found in Webjam")
        return raw, f"song '{r['title']}'"
    raise HTTPException(status_code=400, detail="Invalid comment target_type")


@app.get("/api/comments")
def list_comments(
    target_type: str = Query(..., max_length=16),
    target_id: str = Query(..., max_length=64),
    before_id: Optional[int] = Query(None),
    conn: sqlite3.Connection = Depends(get_db),
):
    """Older pages of a comment thread: the 50 comments before `before_id`, oldest first."""
    canonical, _ = resolve_comment_target(conn, target_type, target_id)
    comments, has_more = fetch_comments_page(conn, target_type, canonical, before_id=before_id)
    return {"comments": comments, "has_more": has_more}


@app.post("/api/comments", dependencies=[rate_limit("comment")])
def create_comment(req: CreateCommentRequest, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    req.author_user_id = resolve_actor(request, req.author_user_id)
    if req.target_type not in ("playlist", "song", "profile"):
        raise HTTPException(status_code=400, detail="Invalid comment target_type")
    body = req.body.strip()
    if not body:
        raise HTTPException(status_code=400, detail="Comment cannot be empty")
    target_id, label = resolve_comment_target(conn, req.target_type, req.target_id)

    ts = now_str()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO comments (author_user_id, target_type, target_id, body, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (req.author_user_id, req.target_type, target_id, body, ts),
    )
    cur.execute(
        """
        INSERT INTO activity_log (actor_user_id, action_type, summary, link_type, link_id, created_at)
        VALUES (?, 'commented', ?, ?, ?, ?)
        """,
        (req.author_user_id, f"commented on {label}", req.target_type, target_id, ts),
    )
    conn.commit()

    new_badges = evaluate_and_award_badges(conn, req.author_user_id)
    comments, has_more = fetch_comments_page(conn, req.target_type, target_id)
    return {
        "comments": comments,
        "comments_has_more": has_more,
        "new_badges": new_badges,
    }


@app.get("/api/youtube/search", dependencies=[rate_limit("search")])
async def search_youtube_embeddable(
    q: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(8, ge=1, le=15),
):
    results = await search_embeddable_youtube_songs(q, max_results=limit)
    return {"query": q, "results": results}


# GET /api/youtube/check/{video_id} was removed: the frontend never used it and it let
# anonymous callers send unvalidated ids to YouTube.


# Serve static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def serve_index():
    return FileResponse(str(STATIC_DIR / "index.html"))


import asyncio
import bisect
import ipaddress
import logging
import re
import threading
import time
import urllib.parse
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Annotated, Any, Dict, List, Literal, Optional, Tuple, Union
import httpx

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.sessions import SessionMiddleware

from app.auth import (
    LOOPBACK_HOSTS,
    SESSION_MAX_AGE,
    auth_enabled,
    auth_mode,
    check_demo_mode_allowed,
    configure_from_env,
    demo_allows_remote,
    get_config,
    handle_taken,
    has_youtube_access,
    migrate_legacy_google_accounts,
    release_handle,
    resolve_actor,
    resolve_viewer,
    router as auth_router,
    session_user_id,
    validate_handle,
)

from app import ops
from app.badges import BADGE_BY_KEY, BADGE_CATALOG, evaluate_and_award_badges, get_user_badge_progress
from app import discovery
from app.database import get_connection, get_db, init_db
from app.errors import ApiError
from app.invite import list_invites, seed_invites_from_env
from app.ops import (
    Actor,
    DbId,
    Op,
    OpIdStr,
    VideoLookupFailed,
    VideoLookupUnavailable,
    apply_ops,
    ensure_crate,
    new_ulid,
    parse_ops,
    require_playlist_role,
    undo_state,
)
from app.paste import resolve_paste
from app.visibility import (
    is_linkable,
    is_listed,
    linkable,
    listed,
    public_song_sql,
    require_visible_playlist,
    song_visible,
    song_visible_sql,
    visible,
)
from app.youtube import (
    MAX_CLIP_SECONDS,
    SearchBudgetExceeded,
    cached_search_results,
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
MAX_PASTE_HTML = 256 * 1024
COMMENTS_PAGE_SIZE = 50

# --- Rate limits: bucket -> (burst capacity, tokens refilled per second) ---
RATE_LIMITS: Dict[str, Tuple[float, float]] = {
    # Only searches that miss the cache are charged (auto-migrate repeats queries).
    "search": (30, 30 / 60),
    "inspect": (6, 6 / 60),
    "add": (30, 30 / 60),
    "comment": (10, 5 / 60),
    "like": (60, 1.0),
    "play": (60, 1.0),
    "audit": (5, 5 / 60),
    # /api/ops, undo and redo (the "add" bucket is also charged when a batch needs YouTube lookups).
    "ops": (120, 2.0),
    # /api/search and /api/discover (Phase 1b).
    "find": (60, 1.0),
}
PLAY_DEDUPE_SECONDS = 30 * 60
PLAY_BADGE_EVAL_INTERVAL = 60
TRUSTED_PROXIES = {"127.0.0.1", "::1"}


class TokenBucketLimiter:
    """Small in-process token bucket. Good enough for one uvicorn worker.

    Buckets live in an LRU of at most `max_buckets` keys, so memory is bounded and
    eviction is O(1). An evicted key just starts again with a full bucket.
    """

    def __init__(self, max_buckets: int = 50_000) -> None:
        self._buckets: "OrderedDict[Tuple[str, str], Tuple[float, float]]" = OrderedDict()
        self._lock = threading.Lock()
        self.max_buckets = max_buckets

    def allow(self, key: Tuple[str, str], capacity: float, refill_per_sec: float) -> Tuple[bool, float]:
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(key, (capacity, now))
            tokens = min(capacity, tokens + (now - last) * refill_per_sec)
            allowed = tokens >= 1
            self._buckets[key] = (tokens - 1 if allowed else tokens, now)
            self._buckets.move_to_end(key)
            while len(self._buckets) > self.max_buckets:
                self._buckets.popitem(last=False)
            if allowed:
                return True, 0.0
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


def ip_key(ip: str) -> str:
    """IPv4 addresses as-is; IPv6 by /64, since one client usually owns a whole /64."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if addr.version == 6:
        if addr.ipv4_mapped:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


def viewer_key(request: Request) -> str:
    """Who is calling, for rate limits and play dedupe: the signed-in user, else the client IP.

    In demo mode the client-chosen user_id is not trusted, so the IP is used.
    """
    if auth_enabled():
        uid = session_user_id(request)
        if uid:
            return f"u:{uid}"
    return f"ip:{ip_key(client_ip(request))}"


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


def _is_loopback_peer(host: str) -> bool:
    if host == "testclient":  # Starlette's TestClient
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class DemoLoopbackMiddleware:
    """Demo mode lets anyone act as anyone, so it only serves this machine.

    Refuses requests from a non-loopback peer, requests relayed by a proxy, and
    requests whose Host is not a localhost name (DNS rebinding).
    WEBJAM_DEMO_ALLOW_REMOTE=1 turns this off for a trusted LAN.
    """

    ALLOWED_HOSTS = set(LOOPBACK_HOSTS) | {"testserver"}

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and get_config().mode == "demo" and not demo_allows_remote():
            headers = Headers(scope=scope)
            peer = (scope.get("client") or ("", 0))[0] or ""
            host = urllib.parse.urlsplit("//" + headers.get("host", "")).hostname or ""
            if (
                not _is_loopback_peer(peer)
                or "x-forwarded-for" in headers
                or "forwarded" in headers
                or host not in self.ALLOWED_HOSTS
            ):
                return await _send_json(
                    scope, receive, send, 403,
                    "Demo mode only serves localhost. Use WEBJAM_AUTH_MODE=google to share Webjam "
                    "(or set WEBJAM_DEMO_ALLOW_REMOTE=1 on a trusted network).",
                )
        await self.app(scope, receive, send)


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
        limit = MAX_BODY_BYTES
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


CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self' https://www.youtube.com https://s.ytimg.com",
    "frame-src https://www.youtube.com https://www.youtube-nocookie.com",
    "img-src 'self' data: https://i.ytimg.com https://*.ytimg.com",
    "style-src 'self'",
    "font-src 'self'",
    "connect-src 'self'",
    "media-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])
# The old UI (moved under /legacy/ until cutover) still needs its inline handlers.
LEGACY_CONTENT_SECURITY_POLICY = "frame-ancestors 'none'"
LEGACY_PATH_PREFIXES = ("/legacy/", "/static/legacy/")


def _is_legacy_path(path: str) -> bool:
    return path == "/legacy" or path.startswith(LEGACY_PATH_PREFIXES)


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
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
                if _is_legacy_path(scope.get("path") or ""):
                    headers["Content-Security-Policy"] = LEGACY_CONTENT_SECURITY_POLICY
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
        migrate_legacy_google_accounts(conn)
        seed_invites_from_env(conn)
        invite_count = len(list_invites(conn))
    finally:
        conn.close()
    reset_rate_limits()
    if cfg.mode == "demo":
        logger.warning("Webjam auth mode: DEMO — anyone can act as anyone. Never expose this instance publicly.")
    else:
        logger.warning("Webjam auth mode: google (invite-only), base URL %s", cfg.base_url)
        if invite_count == 0:
            logger.warning(
                "No invites: nobody (including you) can sign in. Set WEBJAM_INVITE_EMAILS or run "
                "`python -m app.invite add you@example.com`."
            )
    yield


app = FastAPI(title="Webjam — Social YouTube Playlist Curation", lifespan=lifespan)
# Added innermost first: security headers wrap everything, then body cap, CSRF, sessions.
app.add_middleware(ConfiguredSessionMiddleware)
app.add_middleware(CSRFMiddleware)
app.add_middleware(BodySizeLimitMiddleware)
app.add_middleware(DemoLoopbackMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.include_router(auth_router)


@app.exception_handler(OverflowError)
async def _integer_out_of_range(request: Request, exc: OverflowError):
    # A huge id/number reached SQLite ("Python int too large to convert to SQLite INTEGER").
    return JSONResponse({"detail": "A number in the request is out of range."}, status_code=400)


@app.exception_handler(ApiError)
async def _api_error(request: Request, exc: ApiError):
    return JSONResponse(exc.body(), status_code=exc.status_code, headers=exc.headers)


@app.exception_handler(ValidationError)
async def _server_built_op_invalid(request: Request, exc: ValidationError):
    # A legacy endpoint built an op from out-of-range client values (e.g. a huge source_playlist_id).
    return JSONResponse({"detail": "A value in the request is out of range.", "code": "bad_op"}, status_code=400)


def now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def require_playlist_owner(conn: sqlite3.Connection, playlist_id: int, user_id: int) -> sqlite3.Row:
    """Owner-only checks that aren't ops (audit-embeds, import target): 404 if not visible, else 403."""
    return require_playlist_role(conn, playlist_id, Actor(user_id), "owner")


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
ClipSeconds = Optional[Annotated[int, Field(ge=0, le=MAX_CLIP_SECONDS)]]


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


Visibility = Literal["public", "unlisted", "private"]


class CreatePlaylistRequest(BaseModel):
    user_id: Optional[int] = None
    title: Title
    description: Optional[LongText] = ""
    initial_urls: Optional[UrlBlob] = ""
    visibility: Visibility = "public"


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
    # Optional undo group shared by several calls (auto-migrate: one Ctrl+Z reverts the whole run).
    group_id: Optional[OpIdStr] = None


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


class ToggleLikeRequest(BaseModel):
    user_id: Optional[int] = None
    count: int = 1
    reset: bool = False


class RecordPlayRequest(BaseModel):
    user_id: Optional[int] = None  # demo mode: who is listening (private playlists need the owner)
    video_id: Optional[Annotated[str, Field(max_length=32)]] = None
    count: int = 1  # ignored: every play counts once


class CreateCommentRequest(BaseModel):
    author_user_id: Optional[int] = None
    target_type: Annotated[str, Field(max_length=16)]  # 'playlist' | 'song' | 'profile'
    target_id: Annotated[str, Field(max_length=64)]
    body: Annotated[str, Field(max_length=MAX_COMMENT)]


class OpsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: Optional[int] = None  # demo mode only
    op_id: OpIdStr
    group_id: Optional[OpIdStr] = None
    expected_versions: Optional[Dict[DbId, Annotated[int, Field(ge=0, le=2**53)]]] = None
    ops: Annotated[List[Op], Field(min_length=1, max_length=200)]


class UndoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: Optional[int] = None
    group_id: Optional[Annotated[str, Field(max_length=64)]] = None
    skip: bool = False


class PromoteRequest(BaseModel):
    user_id: Optional[int] = None
    title: Annotated[str, Field(max_length=1000)]
    description: Optional[LongText] = ""
    visibility: Visibility = "public"
    track_ids: Optional[Annotated[List[DbId], Field(max_length=MAX_TRACKS_PER_PLAYLIST)]] = None


class DockRequest(BaseModel):
    user_id: Optional[int] = None
    pinned: Annotated[List[DbId], Field(max_length=100)]


class ResolvePasteRequest(BaseModel):
    user_id: Optional[int] = None
    text: UrlBlob = ""
    html: Optional[Annotated[str, Field(max_length=MAX_PASTE_HTML)]] = None


class ForkRequest(BaseModel):
    user_id: Optional[int] = None
    title: Optional[Annotated[str, Field(max_length=1000)]] = None
    visibility: Optional[Visibility] = None  # default: the source's visibility


# --- Helper query functions ---

def fetch_all_users_summary(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT u.id, u.username, u.display_name, u.bio, u.avatar_color, u.created_at,
               (SELECT COUNT(*) FROM playlists p WHERE p.user_id = u.id AND {listed('p')}) AS playlist_count,
               (SELECT COUNT(*) FROM songs s WHERE s.first_added_by_user_id = u.id) AS songs_discovered,
               (SELECT COUNT(*) FROM user_badges ub WHERE ub.user_id = u.id) AS badge_count,
               (SELECT COUNT(*) FROM playlist_tracks pt JOIN playlists tp ON tp.id = pt.playlist_id
                 WHERE pt.source_user_id = u.id AND pt.added_by_user_id != u.id AND {listed('tp')}) AS times_grabbed
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


NOT_FOUND_DETAIL = ops.NOT_FOUND_DETAIL
UNAVAILABLE_DETAIL = ops.UNAVAILABLE_DETAIL


def lookup_failure(exc: VideoLookupFailed) -> HTTPException:
    return ops.lookup_error(exc.reason)


def ensure_room(conn: sqlite3.Connection, playlist_id: int, adding: int = 1) -> None:
    """Pre-check the 1000-track cap before spending YouTube lookups (apply_ops checks it again)."""
    row = conn.execute("SELECT COUNT(*) AS cnt FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,)).fetchone()
    if int(row["cnt"]) + adding > MAX_TRACKS_PER_PLAYLIST:
        raise ApiError(400, "playlist_full", f"Playlists can hold at most {MAX_TRACKS_PER_PLAYLIST} tracks.")


def ensure_entry_count(count: int) -> None:
    if count > MAX_ENTRIES_PER_REQUEST:
        raise ApiError(400, "batch_too_large", f"Add at most {MAX_ENTRIES_PER_REQUEST} videos at a time.")


async def fetch_song_meta(conn: sqlite3.Connection, video_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """YouTube metadata for unknown videos, fetched before any write lock is taken.

    Goes through this module's fetch_youtube_metadata (looked up at call time) so tests can fake it.
    """
    return await ops.prefetch_song_meta(conn, video_ids, lambda vid: fetch_youtube_metadata(vid))


def server_batch_id() -> str:
    return f"srv-{new_ulid()}"


def legacy_added(conn: sqlite3.Connection, outcome: ops.OpsOutcome, playlist_id: int) -> List[Dict[str, Any]]:
    """Inserted rows in the old add/import response shape."""
    out = []
    claimed_seen = set()
    for res in outcome.results:
        if res.get("kind") != "insert":
            continue
        for tid in res.get("track_ids") or []:
            row = outcome.rows.get(tid)
            if not row:
                continue
            occ = conn.execute(
                "SELECT COUNT(*) FROM playlist_tracks WHERE playlist_id = ? AND video_id = ?",
                (playlist_id, row["video_id"]),
            ).fetchone()[0]
            new_disc = row["video_id"] in outcome.claimed and row["video_id"] not in claimed_seen
            if new_disc:
                claimed_seen.add(row["video_id"])
            out.append({
                "track_id": tid,
                "video_id": row["video_id"],
                "title": row["title"],
                "already_in_playlist": False,
                "occurrences_in_playlist": int(occ or 1),
                "is_new_discovery": new_disc,
                "start_seconds": row["start_seconds"],
                "end_seconds": row["end_seconds"],
            })
    return out


def legacy_skipped(skipped: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{"video_id": s["video_id"], "reason": s["reason"]} for s in skipped]


def playlist_version(conn: sqlite3.Connection, playlist_id: int) -> Optional[int]:
    row = conn.execute("SELECT version FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
    return int(row["version"]) if row else None


def legacy_ref(
    conn: sqlite3.Connection,
    source_playlist_id: Optional[int],
    claimed_source_user_id: Optional[int],
    video_id: str,
) -> Optional[Dict[str, int]]:
    """The provenance ref for an old-style grab; apply_ops verifies it (visible, contains the video).

    A client-sent source_user_id is never stored; on its own it only means "credit the
    song's first finder", which becomes a ref to the finder's debut playlist.
    """
    if source_playlist_id:
        return {"playlist_id": source_playlist_id}
    if claimed_source_user_id:
        song = conn.execute(
            "SELECT first_added_by_user_id, first_added_playlist_id FROM songs WHERE video_id = ?", (video_id,)
        ).fetchone()
        if song and song["first_added_by_user_id"] == claimed_source_user_id and song["first_added_playlist_id"]:
            return {"playlist_id": song["first_added_playlist_id"]}
    return None


def ordinal_sql(alias: str = "pt") -> str:
    """The 1-based position of a row in its playlist (positions are fractional sort keys)."""
    return (
        f"(SELECT COUNT(*) FROM playlist_tracks x WHERE x.playlist_id = {alias}.playlist_id AND "
        f"(x.position < {alias}.position OR (x.position = {alias}.position AND x.id <= {alias}.id)))"
    )


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

    crate_id = None
    if actual_uid:
        crate_id = ensure_crate(conn, actual_uid)
        conn.commit()

    badge_info = get_user_badge_progress(conn, actual_uid)
    cur = conn.cursor()
    # Owner view: all of my playlists, crate first, with kind/visibility/version.
    cur.execute(
        """
        SELECT p.id, p.kind, p.visibility, p.version, p.title, p.description, p.plays, p.created_at, p.updated_at,
               (SELECT COUNT(*) FROM playlist_tracks pt WHERE pt.playlist_id = p.id) AS track_count
        FROM playlists p
        WHERE p.user_id = ?
        ORDER BY (p.kind = 'crate') DESC, p.updated_at DESC, p.id DESC
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
            "crate_id": crate_id,
            "undo": undo_state(conn, actual_uid),
            "needs_handle": discovery.needs_handle(
                conn, actual_uid, auth_enabled() and bool(session_user_id(request))
            ),
        },
        "badge_catalog": BADGE_CATALOG,
        "auth": {
            # 'demo' means anyone can act as anyone; the UI should say so.
            "mode": auth_mode(),
            "signed_in": bool(auth_enabled() and current_user["id"]),
            # False until incremental YouTube consent ships (Phase 4): hide "My YouTube".
            "youtube_connected": bool(
                auth_enabled()
                and current_user["id"]
                and has_youtube_access(
                    cur.execute("SELECT * FROM google_accounts WHERE user_id = ?", (current_user["id"],)).fetchone()
                )
            ),
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
        INSERT INTO users (username, display_name, bio, avatar_color, password_hash, created_at, handle_chosen_at)
        VALUES (?, ?, ?, ?, '', ?, ?)
        """,
        (
            clean_uname,
            display,
            (req.bio or "New crate digger on Webjam.").strip(),
            color,
            ts,
            ts,  # the demo user typed this name: never prompt for a handle
        ),
    )
    new_uid = cur.lastrowid
    ensure_crate(conn, new_uid)
    conn.commit()
    return {"user_id": new_uid, "created": True, "username": clean_uname}


@app.put("/api/me/handle")
def update_my_handle(req: UpdateHandleRequest, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    """Choose your public @handle (never derived from your email address)."""
    uid = resolve_actor(request, req.user_id)
    handle = validate_handle(req.handle)
    cur = conn.cursor()
    me = cur.execute("SELECT username FROM users WHERE id = ?", (uid,)).fetchone()
    if not me:
        raise HTTPException(status_code=404, detail="User not found")
    if handle_taken(cur, handle, exclude_user_id=uid):
        raise HTTPException(status_code=409, detail="That handle is taken.")
    try:
        if (me["username"] or "").lower() != handle:
            release_handle(cur, me["username"], uid, now_str())
        # Saving the same handle is how "Keep @x" confirms it.
        cur.execute("UPDATE users SET username = ?, handle_chosen_at = ? WHERE id = ?", (handle, now_str(), uid))
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


def _song_visible_sql(video_expr: str, viewer: int) -> str:
    """song_visible() as SQL (see app.visibility.song_visible_sql)."""
    return song_visible_sql(video_expr, viewer)


def _public_song_sql(video_expr: str) -> str:
    return public_song_sql(video_expr)


@app.get("/api/feed")
def get_community_feed(request: Request, user_id: int = Query(1), conn: sqlite3.Connection = Depends(get_db)):
    user_id = resolve_viewer(request, user_id)
    cur = conn.cursor()

    # 1. Public playlists with curator info, track count, likes, grabs
    cur.execute(
        f"""
        SELECT p.id, p.user_id, p.kind, p.visibility, p.version, p.title, p.description, p.plays,
               p.created_at, p.updated_at,
               u.username, u.display_name, u.avatar_color,
               (SELECT COUNT(*) FROM playlist_tracks pt WHERE pt.playlist_id = p.id) AS track_count,
               (SELECT COALESCE(SUM(pl.count), 0) FROM playlist_likes pl WHERE pl.playlist_id = p.id) AS like_count,
               (SELECT COALESCE(pl.count, 0) FROM playlist_likes pl WHERE pl.playlist_id = p.id AND pl.user_id = ?) AS my_likes,
               (SELECT COUNT(*) FROM playlist_tracks pt JOIN playlists tp ON tp.id = pt.playlist_id
                 WHERE pt.source_playlist_id = p.id AND pt.added_by_user_id != p.user_id AND {listed('tp')}) AS times_grabbed_from,
               (SELECT COUNT(*) FROM comments c WHERE c.target_type = 'playlist' AND c.target_id = CAST(p.id AS TEXT)) AS comment_count
        FROM playlists p
        JOIN users u ON u.id = p.user_id
        WHERE {listed('p')}
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
            f"""
            WITH ordered AS (
                SELECT pt.*, ROW_NUMBER() OVER (ORDER BY pt.position ASC, pt.id ASC) AS ordinal
                FROM playlist_tracks pt WHERE pt.playlist_id = ?
            )
            SELECT o.id, o.video_id, o.ordinal AS position, o.track_note,
                   CAST(o.start_seconds AS INTEGER) AS start_seconds, CAST(o.end_seconds AS INTEGER) AS end_seconds,
                   o.migrated_from_video_id, o.migrated_from_title,
                   COALESCE(o.display_title, s.title) AS title, s.title AS song_title, o.display_title,
                   s.channel_title, s.thumbnail_url, s.first_added_by_user_id,
                   o.source_user_id, su.username AS source_username,
                   CASE WHEN sp.id IS NOT NULL AND {linkable('sp', user_id)} THEN sp.id END AS source_playlist_id,
                   CASE WHEN sp.id IS NOT NULL AND {linkable('sp', user_id)} THEN sp.title END AS source_playlist_title,
                   fu.username AS first_finder_username,
                   (SELECT COALESCE(SUM(sl.count), 0) FROM song_likes sl WHERE sl.video_id = o.video_id) AS song_like_count,
                   (SELECT COALESCE(sl.count, 0) FROM song_likes sl WHERE sl.video_id = o.video_id AND sl.user_id = ?) AS song_my_likes
            FROM ordered o
            JOIN songs s ON s.video_id = o.video_id
            LEFT JOIN users su ON su.id = o.source_user_id
            LEFT JOIN users fu ON fu.id = s.first_added_by_user_id
            LEFT JOIN playlists sp ON sp.id = o.source_playlist_id
            ORDER BY o.ordinal ASC
            """,
            (pl["id"], user_id),
        )
        tracks = []
        for t in cur.fetchall():
            td = dict(t)
            td["song_my_likes"] = int(td.get("song_my_likes") or 0)
            td["song_liked_by_me"] = bool(td["song_my_likes"] > 0)
            tracks.append(td)
        pl["tracks"] = tracks
        playlists.append(pl)

    # 2. Recently discovered songs on Webjam (only LISTED placements claim discovery)
    cur.execute(
        f"""
        SELECT s.video_id, s.title, s.channel_title, s.thumbnail_url,
               s.first_added_by_user_id,
               CASE WHEN p.id IS NOT NULL AND {linkable('p', user_id)} THEN s.first_added_playlist_id END AS first_added_playlist_id,
               s.first_added_at, s.total_plays,
               u.username AS finder_username, u.display_name AS finder_display_name, u.avatar_color AS finder_avatar_color,
               CASE WHEN p.id IS NOT NULL AND {linkable('p', user_id)} THEN p.title END AS debut_playlist_title,
               (SELECT COUNT(DISTINCT pt.playlist_id) FROM playlist_tracks pt JOIN playlists p2 ON p2.id = pt.playlist_id
                 WHERE pt.video_id = s.video_id AND {listed('p2')}) AS playlist_count,
               (SELECT COUNT(*) FROM comments c WHERE c.target_type = 'song' AND c.target_id = s.video_id) AS comment_count,
               (SELECT COALESCE(SUM(sl.count), 0) FROM song_likes sl WHERE sl.video_id = s.video_id) AS like_count,
               (SELECT COALESCE(sl.count, 0) FROM song_likes sl WHERE sl.video_id = s.video_id AND sl.user_id = ?) AS my_likes
        FROM songs s
        LEFT JOIN users u ON u.id = s.first_added_by_user_id
        LEFT JOIN playlists p ON p.id = s.first_added_playlist_id
        WHERE s.first_added_at IS NOT NULL AND {_song_visible_sql('s.video_id', user_id)}
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

    # 3. Community Activity Log: lines about playlists only while they are public
    cur.execute(
        f"""
        SELECT a.id, a.actor_user_id, a.action_type, a.summary, a.link_type, a.link_id, a.created_at,
               u.username, u.display_name, u.avatar_color
        FROM activity_log a
        JOIN users u ON u.id = a.actor_user_id
        WHERE (a.link_type != 'playlist'
               OR EXISTS (SELECT 1 FROM playlists p WHERE p.id = CAST(a.link_id AS INTEGER) AND {listed('p')}))
          AND (a.link_type != 'song' OR {_public_song_sql('a.link_id')})
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


def build_playlist_detail(conn: sqlite3.Connection, pl_row: sqlite3.Row, viewer: int) -> Dict[str, Any]:
    cur = conn.cursor()
    playlist_id = int(pl_row["id"])
    cur.execute(
        f"""
        SELECT p.*, u.username, u.display_name, u.avatar_color, u.bio AS user_bio,
               (SELECT COALESCE(SUM(pl.count), 0) FROM playlist_likes pl WHERE pl.playlist_id = p.id) AS like_count,
               (SELECT COALESCE(pl.count, 0) FROM playlist_likes pl WHERE pl.playlist_id = p.id AND pl.user_id = ?) AS my_likes,
               (SELECT COUNT(*) FROM playlist_tracks pt JOIN playlists tp ON tp.id = pt.playlist_id
                 WHERE pt.source_playlist_id = p.id AND pt.added_by_user_id != p.user_id AND {listed('tp')}) AS times_grabbed_from
        FROM playlists p
        JOIN users u ON u.id = p.user_id
        WHERE p.id = ?
        """,
        (viewer, playlist_id),
    )
    playlist = dict(cur.fetchone())
    playlist["my_likes"] = int(playlist.get("my_likes") or 0)
    playlist["liked_by_me"] = bool(playlist["my_likes"] > 0)

    forked_from = None
    if playlist.get("forked_from_playlist_id"):
        src = cur.execute(
            "SELECT p.*, u.username FROM playlists p JOIN users u ON u.id = p.user_id WHERE p.id = ?",
            (playlist["forked_from_playlist_id"],),
        ).fetchone()
        if src and is_linkable(src, viewer):
            forked_from = {"playlist_id": src["id"], "title": src["title"], "username": src["username"]}
    playlist["forked_from"] = forked_from
    if forked_from is None:
        playlist["forked_from_playlist_id"] = None

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
    playlist["tracks"] = ops.render_track_rows(conn, pl_row, viewer)
    playlist["related_count"] = discovery.related_count(conn, playlist_id)
    if pl_row["kind"] == "crate":
        playlist["comments"], playlist["comments_has_more"] = [], False
    else:
        playlist["comments"], playlist["comments_has_more"] = fetch_comments_page(conn, "playlist", str(playlist_id))
    return playlist


@app.get("/api/playlists/{playlist_id}")
def get_playlist_detail(
    playlist_id: int, request: Request, user_id: int = Query(1), conn: sqlite3.Connection = Depends(get_db)
):
    viewer = resolve_viewer(request, user_id)
    pl = require_visible_playlist(conn, playlist_id, viewer)
    return build_playlist_detail(conn, pl, viewer)


@app.post("/api/playlists")
async def create_playlist(req: CreatePlaylistRequest, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    uid = resolve_actor(request, req.user_id)
    actor = Actor(uid)
    title = req.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Playlist title is required")
    entries = extract_video_entries(req.initial_urls) if (req.initial_urls or "").strip() else []
    ensure_entry_count(len(entries))
    meta: Dict[str, Dict[str, Any]] = {}
    if entries:
        enforce_rate_limit(request, "add")
        meta = await fetch_song_meta(conn, [e["video_id"] for e in entries])

    playlist_id = ops.create_playlist(conn, actor, title, (req.description or "").strip(), req.visibility)
    added_tracks: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    new_badges: List[Dict[str, Any]] = []
    if entries:
        items = [{"video_id": e["video_id"], "start_seconds": e["start_seconds"], "end_seconds": e["end_seconds"]}
                 for e in entries]
        outcome = apply_ops(
            conn, actor, parse_ops([{"kind": "insert", "playlist_id": playlist_id, "items": items}]),
            batch_id=server_batch_id(), song_meta=meta, skip_unavailable=True,
        )
        added_tracks = legacy_added(conn, outcome, playlist_id)
        skipped = legacy_skipped(outcome.skipped)
        new_badges = outcome.new_badges
    else:
        conn.commit()
    if not added_tracks and req.visibility == "public":
        new_badges = evaluate_and_award_badges(conn, uid)
    return {
        "playlist_id": playlist_id,
        "added_tracks": added_tracks,
        "skipped": skipped,
        "new_badges": new_badges,
        "version": playlist_version(conn, playlist_id),
    }


@app.put("/api/playlists/{playlist_id}")
def update_playlist(
    playlist_id: int, req: UpdatePlaylistRequest, request: Request, conn: sqlite3.Connection = Depends(get_db)
):
    uid = resolve_actor(request, req.user_id)
    outcome = apply_ops(
        conn, Actor(uid),
        parse_ops([{"kind": "set_meta", "playlist_id": playlist_id,
                    "set": {"title": req.title, "description": req.description}}]),
        batch_id=server_batch_id(),
    )
    return {"ok": True, "new_badges": outcome.new_badges, "version": playlist_version(conn, playlist_id)}


@app.post("/api/playlists/{playlist_id}/tracks", dependencies=[rate_limit("add")])
async def add_tracks_to_playlist(
    playlist_id: int, req: AddTracksRequest, request: Request, conn: sqlite3.Connection = Depends(get_db)
):
    uid = resolve_actor(request, req.user_id)
    actor = Actor(uid)
    require_playlist_role(conn, playlist_id, actor, "editor")

    entries = extract_video_entries(req.urls_or_ids)
    if not entries:
        raise HTTPException(
            status_code=400,
            detail="Could not find a valid YouTube link or 11-character video ID. Try pasting any youtube.com/watch?v=... or youtu.be/... link!",
        )
    ensure_entry_count(len(entries))
    ensure_room(conn, playlist_id, len(entries))

    explicit_start = parse_timestamp_seconds(req.start_time)
    explicit_end = parse_timestamp_seconds(req.end_time)
    meta = await fetch_song_meta(conn, [e["video_id"] for e in entries])
    failures = [m["error"] for m in meta.values() if m.get("error")]
    if len(failures) == len(entries) and failures:
        raise lookup_failure(VideoLookupUnavailable() if VideoLookupUnavailable.reason in failures else VideoLookupFailed())

    items = []
    for entry in entries:
        vid = entry["video_id"]
        item: Dict[str, Any] = {
            "video_id": vid,
            "start_seconds": explicit_start if explicit_start is not None else entry["start_seconds"],
            "end_seconds": explicit_end if explicit_end is not None else entry["end_seconds"],
            "note": req.track_note or "",
            "display_title": req.custom_title if len(entries) == 1 else None,
        }
        ref = legacy_ref(conn, req.source_playlist_id, req.source_user_id, vid)
        if ref:
            item["ref"] = ref
        items.append(item)
    outcome = apply_ops(
        conn, actor, parse_ops([{"kind": "insert", "playlist_id": playlist_id, "items": items}]),
        batch_id=server_batch_id(), song_meta=meta, skip_unavailable=True,
    )
    return {
        "results": legacy_added(conn, outcome, playlist_id),
        "skipped": legacy_skipped(outcome.skipped),
        "new_badges": outcome.new_badges,
        "version": playlist_version(conn, playlist_id),
    }


@app.put("/api/playlists/{playlist_id}/tracks/{track_id}")
async def update_playlist_track(
    playlist_id: int,
    track_id: int,
    req: UpdateTrackRequest,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
):
    uid = resolve_actor(request, req.user_id)
    reject_reserved_ids(req.group_id)
    actor = Actor(uid)
    pl = require_playlist_role(conn, playlist_id, actor, "editor")
    swapping = bool(req.new_video_id and req.new_video_id.strip())
    if swapping:
        enforce_rate_limit(request, "add")  # a swap can cost a YouTube lookup, like an add
    order = [r["id"] for r in conn.execute(
        "SELECT id FROM playlist_tracks WHERE playlist_id = ? ORDER BY position ASC, id ASC", (playlist_id,))]
    if track_id not in order:
        raise ApiError(404, "track_not_found", "Track not found in playlist")

    new_set: Dict[str, Any] = {}
    meta: Dict[str, Dict[str, Any]] = {}
    if swapping:
        vids = extract_video_ids(req.new_video_id)
        if not vids:
            raise HTTPException(
                status_code=400,
                detail="Could not find a valid YouTube link or 11-character video ID for migration.",
            )
        new_vid = vids[0]
        meta = await fetch_song_meta(conn, [new_vid])
        if meta.get(new_vid, {}).get("error"):
            raise ops.lookup_error(meta[new_vid]["error"])
        new_set["video_id"] = new_vid
        # A swap gives the row the new song's title unless a custom title comes with it.
        new_set["display_title"] = req.custom_title or ""
    elif req.custom_title is not None:
        new_set["display_title"] = req.custom_title
    if req.track_note is not None:
        new_set["note"] = req.track_note
    if req.update_clip or req.start_time is not None or req.end_time is not None:
        new_set["start_seconds"] = parse_timestamp_seconds(req.start_time)
        new_set["end_seconds"] = parse_timestamp_seconds(req.end_time)

    raw_ops: List[Dict[str, Any]] = []
    if new_set:
        raw_ops.append({"kind": "update", "playlist_id": playlist_id, "track_id": track_id, "set": new_set})
    if req.direction in ("up", "down"):
        index = order.index(track_id) + 1
        target = index - 1 if req.direction == "up" else index + 1
        if 1 <= target <= len(order):
            raw_ops.append({"kind": "move", "playlist_id": playlist_id, "track_ids": [track_id], "position": target})
    new_badges: List[Dict[str, Any]] = []
    if raw_ops:
        outcome = apply_ops(conn, actor, parse_ops(raw_ops), batch_id=server_batch_id(), group_id=req.group_id,
                            song_meta=meta)
        new_badges = outcome.new_badges
    row = ops.render_track_rows(conn, pl, uid, [track_id])[0]
    return {
        "ok": True,
        "video_id": row["video_id"],
        "title": row["title"],
        "channel_title": row["channel_title"],
        "migrated_from_video_id": row["migrated_from_video_id"],
        "migrated_from_title": row["migrated_from_title"],
        "start_seconds": row["start_seconds"],
        "end_seconds": row["end_seconds"],
        "new_badges": new_badges,
        "version": playlist_version(conn, playlist_id),
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
    except httpx.HTTPError as exc:
        logger.warning("Playlist inspect failed talking to YouTube: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Couldn't reach YouTube. Please try again in a moment.")


@app.post("/api/playlists/import/commit", dependencies=[rate_limit("add")])
async def commit_playlist_import(
    req: CommitPlaylistImportRequest,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
):
    uid = resolve_actor(request, req.user_id)
    actor = Actor(uid)
    target = None
    if req.target_playlist_id:
        target = require_playlist_role(conn, req.target_playlist_id, actor, "editor")  # may be the crate
    if not req.tracks:
        raise HTTPException(status_code=400, detail="Select at least one track to import.")
    ensure_entry_count(len(req.tracks))
    if target is not None:
        ensure_room(conn, target["id"], len(req.tracks))

    items: List[Dict[str, Any]] = []
    migrated: List[Tuple[Optional[str], Optional[str]]] = []
    for item in req.tracks:
        vids = extract_video_ids(item.video_id)
        if not vids:
            continue
        items.append({
            "video_id": vids[0],
            "start_seconds": parse_timestamp_seconds(item.start_seconds),
            "end_seconds": parse_timestamp_seconds(item.end_seconds),
            "note": item.track_note or "",
            "display_title": item.custom_title,
        })
        migrated.append((item.migrated_from_video_id, item.migrated_from_title))
    meta = await fetch_song_meta(conn, [i["video_id"] for i in items])

    if target is not None:
        playlist_id, playlist_title = int(target["id"]), target["title"]
    else:
        playlist_title = (req.title or "").strip() or "Imported YouTube Playlist"
        playlist_id = ops.create_playlist(
            conn, actor, playlist_title, (req.description or "").strip(), "public",
            activity=f"imported YouTube playlist '{playlist_title}'",
        )

    added_tracks: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    new_badges: List[Dict[str, Any]] = []
    if items:
        parsed = parse_ops([{"kind": "insert", "playlist_id": playlist_id, "items": items}])
        # migrated_from_* is accepted only on this internal path (never through /api/ops).
        for it, (mv, mt) in zip(parsed[0].items, migrated):
            it._migrated_from_video_id = mv
            it._migrated_from_title = mt
        outcome = apply_ops(conn, actor, parsed, batch_id=server_batch_id(), song_meta=meta, skip_unavailable=True)
        added_tracks = legacy_added(conn, outcome, playlist_id)
        skipped = legacy_skipped(outcome.skipped)
        new_badges = outcome.new_badges
    elif conn.in_transaction:
        conn.commit()
    return {
        "playlist_id": playlist_id,
        "title": playlist_title,
        "added_tracks": added_tracks,
        "skipped": skipped,
        "new_badges": new_badges,
        "version": playlist_version(conn, playlist_id),
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
        WITH ordered AS (
            SELECT pt.*, ROW_NUMBER() OVER (ORDER BY pt.position ASC, pt.id ASC) AS ordinal
            FROM playlist_tracks pt WHERE pt.playlist_id = ?
        )
        SELECT o.id AS track_id, o.video_id, o.ordinal AS position, o.track_note,
               CAST(o.start_seconds AS INTEGER) AS start_seconds, CAST(o.end_seconds AS INTEGER) AS end_seconds,
               o.migrated_from_video_id, o.migrated_from_title,
               COALESCE(o.display_title, s.title) AS title, s.channel_title, s.thumbnail_url
        FROM ordered o
        JOIN songs s ON s.video_id = o.video_id
        ORDER BY o.ordinal ASC
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
    suggestions: Dict[Tuple[str, str], str] = {}
    for r in rows:
        info = infos.get(r["video_id"]) or {}
        base_title = r.get("migrated_from_title") or r["title"] or info.get("title") or r["video_id"]
        channel = "" if r.get("migrated_from_title") else (r["channel_title"] or info.get("channel_title") or "")
        if (base_title, channel) not in suggestions:
            suggestions[(base_title, channel)] = clean_migration_query(base_title, channel)
        audited.append(
            {
                **r,
                "embeddable": bool(info.get("embeddable")),
                "status": info.get("status", "UNKNOWN"),
                "reason": info.get("reason"),
                "suggested_query": suggestions[(base_title, channel)],
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
    uid = resolve_actor(request, user_id)
    apply_ops(conn, Actor(uid), parse_ops([{"kind": "delete", "playlist_id": playlist_id, "track_ids": [track_id]}]),
              batch_id=server_batch_id())
    return {"ok": True, "version": playlist_version(conn, playlist_id)}


def reorder_moves(current: List[int], wanted: List[int]) -> List[Dict[str, Any]]:
    """Minimal moves turning `current` into `wanted`: keep a longest increasing subsequence,
    move every other track right after its new predecessor (left to right)."""
    index = {t: i for i, t in enumerate(current)}
    seq = [index[t] for t in wanted]
    # Patience sorting with back-pointers: O(n log n) LIS.

    tails: List[int] = []
    tails_at: List[int] = []
    prev = [-1] * len(seq)
    for i, v in enumerate(seq):
        j = bisect.bisect_left(tails, v)
        if j == len(tails):
            tails.append(v)
            tails_at.append(i)
        else:
            tails[j] = v
            tails_at[j] = i
        prev[i] = tails_at[j - 1] if j > 0 else -1
    keep = set()
    k = tails_at[-1] if tails_at else -1
    while k != -1:
        keep.add(wanted[k])
        k = prev[k]
    moves = []
    for j, t in enumerate(wanted):
        if t in keep:
            continue
        move: Dict[str, Any] = {"kind": "move", "track_ids": [t]}
        if j == 0:
            move["position"] = 1
        else:
            move["after_track_id"] = wanted[j - 1]
        moves.append(move)
    return moves


@app.put("/api/playlists/{playlist_id}/reorder", dependencies=[rate_limit("ops")])
def reorder_playlist_tracks(
    playlist_id: int, req: ReorderTracksRequest, request: Request, conn: sqlite3.Connection = Depends(get_db)
):
    uid = resolve_actor(request, req.user_id)
    actor = Actor(uid)
    require_playlist_role(conn, playlist_id, actor, "editor")
    existing_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM playlist_tracks WHERE playlist_id = ? ORDER BY position ASC, id ASC", (playlist_id,))]
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

    moves = reorder_moves(existing_ids, ordered_ids)
    if len(moves) == 1:  # a single drag: one move op
        moves[0]["playlist_id"] = playlist_id
        apply_ops(conn, actor, parse_ops(moves), batch_id=server_batch_id())
    elif moves:
        # Anything bigger is ONE reorder effect (one op row, one O(n) pass), not one move per track.
        reorder = ops.ReorderOp(playlist_id=playlist_id, track_ids=ordered_ids)
        apply_ops(conn, actor, [reorder], batch_id=server_batch_id())
    final = [r["id"] for r in conn.execute(
        "SELECT id FROM playlist_tracks WHERE playlist_id = ? ORDER BY position ASC, id ASC", (playlist_id,))]
    return {"ok": True, "ordered_track_ids": final, "version": playlist_version(conn, playlist_id)}


# PUT /api/playlists/{id}/snapshot was deleted in Phase 1a: undo is POST /api/ops/undo now.


# --- Ops, undo/redo, crate, dock, paste, delete, fork (Phase 1a) -----------------------------

def reject_reserved_ids(*ids: Optional[str]) -> None:
    """srv-, fork- and promote- ids are minted by the server; a client may not borrow them."""
    for value in ids:
        if ops.is_reserved_id(value):
            raise ApiError(400, "bad_op", "That op_id / group_id prefix is reserved.")


@app.post("/api/ops", dependencies=[rate_limit("ops")])
async def post_ops(req: OpsRequest, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    uid = resolve_actor(request, req.user_id)
    reject_reserved_ids(req.op_id, req.group_id)
    # Caps and roles first (read-only), so a batch that will be refused costs no YouTube lookups.
    # The caps also bound the lookups: at most 50 items, so at most 50 unknown videos.
    ops.check_batch_shape(req.ops)
    ops.authorise_batch(conn, Actor(uid), req.ops)
    video_ids = [it.video_id for op in req.ops if op.kind == "insert" for it in op.items]
    video_ids += [op.set.video_id for op in req.ops if op.kind == "update" and op.set.video_id]
    meta: Dict[str, Dict[str, Any]] = {}
    unknown = ops.unknown_video_ids(conn, video_ids)
    if unknown:
        replay = conn.execute("SELECT 1 FROM playlist_ops WHERE batch_id = ? LIMIT 1", (req.op_id,)).fetchone()
        if not replay:
            enforce_rate_limit(request, "add")
            meta = await fetch_song_meta(conn, unknown)
    outcome = apply_ops(
        conn, Actor(uid), req.ops, batch_id=req.op_id, group_id=req.group_id,
        expected_versions=req.expected_versions, song_meta=meta,
    )
    return ops.outcome_json(conn, outcome, req.op_id, uid)


@app.post("/api/ops/undo", dependencies=[rate_limit("ops")])
def post_undo(req: UndoRequest, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    uid = resolve_actor(request, req.user_id)
    return ops.undo_group(conn, Actor(uid), expect_group_id=req.group_id, skip=req.skip)


@app.post("/api/ops/redo", dependencies=[rate_limit("ops")])
def post_redo(req: UndoRequest, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    uid = resolve_actor(request, req.user_id)
    return ops.redo_group(conn, Actor(uid), expect_group_id=req.group_id, skip=req.skip)


@app.get("/api/me/undo")
def get_my_undo(request: Request, user_id: Optional[int] = Query(None), conn: sqlite3.Connection = Depends(get_db)):
    return undo_state(conn, resolve_actor(request, user_id))


@app.get("/api/me/crate")
def get_my_crate(request: Request, user_id: Optional[int] = Query(None), conn: sqlite3.Connection = Depends(get_db)):
    uid = resolve_actor(request, user_id)
    crate_id = ensure_crate(conn, uid)
    conn.commit()
    pl = conn.execute("SELECT * FROM playlists WHERE id = ?", (crate_id,)).fetchone()
    detail = build_playlist_detail(conn, pl, uid)
    for t in detail["tracks"]:
        t["also_in"] = [
            dict(r)
            for r in conn.execute(
                """
                SELECT DISTINCT p.id AS playlist_id, p.title FROM playlist_tracks pt JOIN playlists p ON p.id = pt.playlist_id
                WHERE p.user_id = ? AND p.kind = 'playlist' AND pt.video_id = ?
                  AND pt.start_seconds IS ? AND pt.end_seconds IS ?
                ORDER BY p.id
                """,
                (uid, t["video_id"], t["start_seconds"], t["end_seconds"]),
            )
        ]
    return detail


@app.post("/api/me/crate/promote", status_code=201, dependencies=[rate_limit("ops")])
def promote_my_crate(req: PromoteRequest, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    uid = resolve_actor(request, req.user_id)
    return ops.promote_crate(conn, Actor(uid), req.title, req.description or "", req.visibility, req.track_ids)


@app.get("/api/me/dock")
def get_my_dock(request: Request, user_id: Optional[int] = Query(None), conn: sqlite3.Connection = Depends(get_db)):
    return ops.dock_view(conn, resolve_actor(request, user_id))


@app.put("/api/me/dock")
def put_my_dock(req: DockRequest, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    uid = resolve_actor(request, req.user_id)
    return ops.set_dock_pins(conn, uid, list(req.pinned))


@app.post("/api/resolve-paste")
async def post_resolve_paste(req: ResolvePasteRequest, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    uid = resolve_actor(request, req.user_id)
    return await resolve_paste(
        conn, req.text or "", req.html, uid,
        fetch=lambda vid: fetch_youtube_metadata(vid),
        charge=lambda: enforce_rate_limit(request, "add"),
        base_url=get_config().base_url,
        demo=not auth_enabled(),
    )


@app.delete("/api/playlists/{playlist_id}")
def delete_playlist(
    playlist_id: int, request: Request, user_id: Optional[int] = Query(None), conn: sqlite3.Connection = Depends(get_db)
):
    uid = resolve_actor(request, user_id)
    return ops.delete_playlist(conn, Actor(uid), playlist_id)


@app.post("/api/playlists/{playlist_id}/fork", status_code=201, dependencies=[rate_limit("ops")])
def fork_playlist(playlist_id: int, req: ForkRequest, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    uid = resolve_actor(request, req.user_id)
    return ops.fork_playlist(conn, Actor(uid), playlist_id, req.title, req.visibility)


# --- Likes, plays, songs, profiles, comments --------------------------------------------------

@app.post("/api/playlists/{playlist_id}/like", dependencies=[rate_limit("like")])
def toggle_playlist_like(
    playlist_id: int, req: ToggleLikeRequest, request: Request, conn: sqlite3.Connection = Depends(get_db)
):
    req.user_id = resolve_actor(request, req.user_id)
    cur = conn.cursor()
    pl = require_visible_playlist(conn, playlist_id, req.user_id)
    if pl["kind"] == "crate":
        raise ApiError(400, "not_likeable", "Crates can't be liked.")
    public = is_listed(pl)

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

        if public and prev_count == 0:
            cur.execute(
                """
                INSERT INTO activity_log (actor_user_id, action_type, summary, link_type, link_id, created_at)
                VALUES (?, 'liked_playlist', ?, 'playlist', ?, ?)
                """,
                (req.user_id, f"co-signed playlist '{pl['title']}'", str(playlist_id), ts),
            )
        elif public and prev_count < 10 <= my_likes:
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
    if not song or not song_visible(conn, video_id, req.user_id):
        raise HTTPException(status_code=404, detail="Song not found in Webjam")
    public = song_visible(conn, video_id, 0)

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

        if public and prev_count == 0:
            cur.execute(
                """
                INSERT INTO activity_log (actor_user_id, action_type, summary, link_type, link_id, created_at)
                VALUES (?, 'liked_song', ?, 'song', ?, ?)
                """,
                (req.user_id, f"loved track '{song['title']}'", video_id, ts),
            )
        elif public and prev_count < 10 <= my_likes:
            cur.execute(
                """
                INSERT INTO activity_log (actor_user_id, action_type, summary, link_type, link_id, created_at)
                VALUES (?, 'superloved_song', ?, 'song', ?, ?)
                """,
                (req.user_id, f"bestowed Superlove ({my_likes}× ♥) on '{song['title']}'", video_id, ts),
            )

    conn.commit()
    actor_badges = evaluate_and_award_badges(conn, req.user_id)
    finder = song["first_added_by_user_id"]
    finder_badges = evaluate_and_award_badges(conn, finder) if finder and req.user_id != finder else []
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
    """Count one play. req.count is ignored and video_id must be in the playlist.

    The playlist's play count goes up at most once per listener (user, else IP) per
    playlist every 30 minutes, and never for the owner's own listening (it feeds
    badges). A song's total_plays counts once per listener+track per 30 minutes.
    Crates and private playlists never count toward either."""
    cur = conn.cursor()
    pl = require_visible_playlist(conn, playlist_id, resolve_viewer(request, req.user_id))
    countable = pl["kind"] == "playlist" and pl["visibility"] != "private"

    video_id = (req.video_id or "").strip()
    if video_id:
        in_playlist = cur.execute(
            "SELECT 1 FROM playlist_tracks WHERE playlist_id = ? AND video_id = ?",
            (playlist_id, video_id),
        ).fetchone()
        if not in_playlist:
            raise HTTPException(status_code=400, detail="That track isn't in this playlist.")

    key = viewer_key(request)
    is_owner = auth_enabled() and session_user_id(request) == pl["user_id"]
    now = int(time.time())
    since = now - PLAY_DEDUPE_SECONDS
    with _play_record_lock:
        cur.execute("DELETE FROM play_events WHERE at < ?", (since,))
        playlist_recent = cur.execute(
            "SELECT 1 FROM play_events WHERE viewer_key = ? AND playlist_id = ? AND at >= ?",
            (key, playlist_id, since),
        ).fetchone()
        track_recent = playlist_recent and cur.execute(
            "SELECT 1 FROM play_events WHERE viewer_key = ? AND playlist_id = ? AND video_id = ? AND at >= ?",
            (key, playlist_id, video_id, since),
        ).fetchone()
        counted = playlist_recent is None and not is_owner and countable
        if not track_recent:
            cur.execute(
                "INSERT INTO play_events (viewer_key, playlist_id, video_id, at) VALUES (?, ?, ?, ?)",
                (key, playlist_id, video_id, now),
            )
            if video_id and countable:
                cur.execute("UPDATE songs SET total_plays = total_plays + 1 WHERE video_id = ?", (video_id,))
        if counted:
            cur.execute("UPDATE playlists SET plays = plays + 1 WHERE id = ?", (playlist_id,))
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
    if not VIDEO_ID_PATTERN.match(video_id) or not song_visible(conn, video_id, user_id):
        raise HTTPException(status_code=404, detail="Song not found in Webjam")
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT s.*,
               CASE WHEN p.id IS NOT NULL AND {linkable('p', user_id)} THEN s.first_added_playlist_id END AS first_added_playlist_id,
               u.username AS finder_username, u.display_name AS finder_display_name, u.avatar_color AS finder_avatar_color,
               CASE WHEN p.id IS NOT NULL AND {linkable('p', user_id)} THEN p.title END AS debut_playlist_title,
               (SELECT COALESCE(SUM(sl.count), 0) FROM song_likes sl WHERE sl.video_id = s.video_id) AS like_count,
               (SELECT COALESCE(sl.count, 0) FROM song_likes sl WHERE sl.video_id = s.video_id AND sl.user_id = ?) AS my_likes
        FROM songs s
        LEFT JOIN users u ON u.id = s.first_added_by_user_id
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

    # Playlist appearances the viewer may see, ordered chronologically
    cur.execute(
        f"""
        SELECT pt.id AS track_entry_id, pt.playlist_id, {ordinal_sql('pt')} AS position, pt.track_note,
               CAST(pt.start_seconds AS INTEGER) AS start_seconds, CAST(pt.end_seconds AS INTEGER) AS end_seconds,
               CASE WHEN sp.id IS NOT NULL AND {linkable('sp', user_id)} THEN sp.id END AS source_playlist_id,
               pt.source_user_id, pt.created_at,
               p.title AS playlist_title, p.plays AS playlist_plays, p.visibility AS playlist_visibility,
               u.id AS user_id, u.username, u.display_name, u.avatar_color,
               su.username AS source_username, su.display_name AS source_display_name,
               CASE WHEN sp.id IS NOT NULL AND {linkable('sp', user_id)} THEN sp.title END AS source_playlist_title,
               (SELECT COALESCE(SUM(pl.count), 0) FROM playlist_likes pl WHERE pl.playlist_id = p.id) AS playlist_likes
        FROM playlist_tracks pt
        JOIN playlists p ON p.id = pt.playlist_id
        JOIN users u ON u.id = p.user_id
        LEFT JOIN users su ON su.id = pt.source_user_id
        LEFT JOIN playlists sp ON sp.id = pt.source_playlist_id
        WHERE pt.video_id = ? AND ({listed('p')} OR (p.user_id = ? AND p.kind = 'playlist'))
        ORDER BY pt.created_at ASC, pt.id ASC
        """,
        (video_id, user_id),
    )
    song["appearances"] = [dict(r) for r in cur.fetchall()]
    song.update(discovery.song_extras(conn, video_id, user_id))
    song["comments"], song["comments_has_more"] = fetch_comments_page(conn, "song", video_id)
    return song


def _profile_viewer(request: Request, user_id: Optional[int], viewer_id: Optional[int]) -> int:
    # `user_id` is the new client's name for the viewer; `viewer_id` keeps working.
    claimed = user_id if user_id is not None else (viewer_id if viewer_id is not None else 1)
    return resolve_viewer(request, claimed)


@app.get("/api/users/by-handle/{handle}")
def get_user_profile_by_handle(
    handle: str,
    request: Request,
    user_id: Optional[int] = Query(None),
    viewer_id: Optional[int] = Query(None),
    conn: sqlite3.Connection = Depends(get_db),
):
    """The /api/users/{id} payload for a handle; a handle the user gave up resolves to them."""
    profile_user_id = discovery.user_id_for_handle(conn, handle)
    if profile_user_id is None:
        raise ApiError(404, "user_not_found", "User not found")
    return build_user_profile(conn, profile_user_id, _profile_viewer(request, user_id, viewer_id))


@app.get("/api/users/{profile_user_id}")
def get_user_profile(
    profile_user_id: int,
    request: Request,
    user_id: Optional[int] = Query(None),
    viewer_id: Optional[int] = Query(None),
    conn: sqlite3.Connection = Depends(get_db),
):
    return build_user_profile(conn, profile_user_id, _profile_viewer(request, user_id, viewer_id))


def build_user_profile(conn: sqlite3.Connection, profile_user_id: int, viewer_id: int) -> Dict[str, Any]:
    is_self = bool(viewer_id) and viewer_id == profile_user_id
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE id = ?", (profile_user_id,))
    urow = cur.fetchone()
    if not urow:
        raise HTTPException(status_code=404, detail="User not found")

    user = dict(urow)
    user.pop("password_hash", None)
    user.pop("session_epoch", None)
    user.pop("handle_chosen_at", None)

    badge_data = get_user_badge_progress(conn, profile_user_id)

    # User's playlists: all of them (not the crate) for the owner, public ones for everyone else.
    playlist_filter = "p.kind = 'playlist'" if is_self else listed("p")
    cur.execute(
        f"""
        SELECT p.*,
               (SELECT COUNT(*) FROM playlist_tracks pt WHERE pt.playlist_id = p.id) AS track_count,
               (SELECT COALESCE(SUM(pl.count), 0) FROM playlist_likes pl WHERE pl.playlist_id = p.id) AS like_count,
               (SELECT COALESCE(pl.count, 0) FROM playlist_likes pl WHERE pl.playlist_id = p.id AND pl.user_id = ?) AS my_likes,
               (SELECT COUNT(*) FROM playlist_tracks pt JOIN playlists tp ON tp.id = pt.playlist_id
                 WHERE pt.source_playlist_id = p.id AND pt.added_by_user_id != p.user_id AND {listed('tp')}) AS times_grabbed_from
        FROM playlists p
        WHERE p.user_id = ? AND {playlist_filter}
        ORDER BY p.updated_at DESC, p.id DESC
        """,
        (viewer_id, profile_user_id),
    )
    playlists = []
    for prow in cur.fetchall():
        pl = dict(prow)
        if pl.get("forked_from_playlist_id"):
            src = conn.execute("SELECT * FROM playlists WHERE id = ?", (pl["forked_from_playlist_id"],)).fetchone()
            if not is_linkable(src, viewer_id):  # same rule as the detail endpoint's forked_from
                pl["forked_from_playlist_id"] = None
        pl["my_likes"] = int(pl.get("my_likes") or 0)
        pl["liked_by_me"] = bool(pl["my_likes"] > 0)
        cur.execute(
            """
            WITH ordered AS (
                SELECT pt.*, ROW_NUMBER() OVER (ORDER BY pt.position ASC, pt.id ASC) AS ordinal
                FROM playlist_tracks pt WHERE pt.playlist_id = ?
            )
            SELECT o.id, o.video_id, o.ordinal AS position, o.track_note,
                   CAST(o.start_seconds AS INTEGER) AS start_seconds, CAST(o.end_seconds AS INTEGER) AS end_seconds,
                   COALESCE(o.display_title, s.title) AS title, s.title AS song_title, s.channel_title,
                   su.username AS source_username,
                   (SELECT COALESCE(SUM(sl.count), 0) FROM song_likes sl WHERE sl.video_id = o.video_id) AS song_like_count,
                   (SELECT COALESCE(sl.count, 0) FROM song_likes sl WHERE sl.video_id = o.video_id AND sl.user_id = ?) AS song_my_likes
            FROM ordered o
            JOIN songs s ON s.video_id = o.video_id
            LEFT JOIN users su ON su.id = o.source_user_id
            ORDER BY o.ordinal ASC
            """,
            (pl["id"], viewer_id),
        )
        tracks = []
        for t in cur.fetchall():
            td = dict(t)
            td["song_my_likes"] = int(td.get("song_my_likes") or 0)
            td["song_liked_by_me"] = bool(td["song_my_likes"] > 0)
            tracks.append(td)
        pl["tracks"] = tracks
        playlists.append(pl)
    card_fields = {c["id"]: c for c in discovery.playlist_cards(conn, [pl["id"] for pl in playlists])}
    for pl in playlists:
        pl["related_count"] = card_fields[pl["id"]]["related_count"]
        pl["preview"] = card_fields[pl["id"]]["preview"]

    # Songs first discovered on Webjam by this user (claims only come from public placements)
    cur.execute(
        f"""
        SELECT s.*,
               CASE WHEN p.id IS NOT NULL AND {linkable('p', viewer_id)} THEN s.first_added_playlist_id END AS first_added_playlist_id,
               CASE WHEN p.id IS NOT NULL AND {linkable('p', viewer_id)} THEN p.title END AS debut_playlist_title,
               (SELECT COUNT(DISTINCT pt.playlist_id) FROM playlist_tracks pt JOIN playlists p2 ON p2.id = pt.playlist_id
                 WHERE pt.video_id = s.video_id AND {listed('p2')}) AS playlist_count,
               (SELECT COALESCE(SUM(sl.count), 0) FROM song_likes sl WHERE sl.video_id = s.video_id) AS like_count,
               (SELECT COALESCE(sl.count, 0) FROM song_likes sl WHERE sl.video_id = s.video_id AND sl.user_id = ?) AS my_likes
        FROM songs s
        LEFT JOIN playlists p ON p.id = s.first_added_playlist_id
        WHERE s.first_added_by_user_id = ? AND {_song_visible_sql('s.video_id', viewer_id)}
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

    # Songs liked by this user (others only see songs they could open)
    liked_filter = "" if is_self else (
        f"AND EXISTS (SELECT 1 FROM playlist_tracks xpt JOIN playlists xp ON xp.id = xpt.playlist_id "
        f"WHERE xpt.video_id = s.video_id AND ({listed('xp')} OR xp.user_id = {int(viewer_id or 0)}))"
    )
    cur.execute(
        f"""
        SELECT s.*, sl.created_at AS liked_at, sl.count AS profile_user_likes,
               fu.username AS finder_username,
               (SELECT COALESCE(SUM(sl2.count), 0) FROM song_likes sl2 WHERE sl2.video_id = s.video_id) AS like_count,
               (SELECT COALESCE(sl3.count, 0) FROM song_likes sl3 WHERE sl3.video_id = s.video_id AND sl3.user_id = ?) AS my_likes
        FROM song_likes sl
        JOIN songs s ON s.video_id = sl.video_id
        LEFT JOIN users fu ON fu.id = s.first_added_by_user_id
        WHERE sl.user_id = ? {liked_filter}
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
    liked_pl_filter = visible("p", viewer_id) if is_self else listed("p")
    cur.execute(
        f"""
        SELECT p.id, p.user_id, p.title, p.description, p.plays, p.visibility, pl.created_at AS liked_at, pl.count AS profile_user_likes,
               u.username, u.display_name, u.avatar_color,
               (SELECT COUNT(*) FROM playlist_tracks pt WHERE pt.playlist_id = p.id) AS track_count,
               (SELECT COALESCE(SUM(pl2.count), 0) FROM playlist_likes pl2 WHERE pl2.playlist_id = p.id) AS like_count,
               (SELECT COALESCE(pl3.count, 0) FROM playlist_likes pl3 WHERE pl3.playlist_id = p.id AND pl3.user_id = ?) AS my_likes
        FROM playlist_likes pl
        JOIN playlists p ON p.id = pl.playlist_id
        JOIN users u ON u.id = p.user_id
        WHERE pl.user_id = ? AND {liked_pl_filter}
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

    # Lineage: tracks others grabbed from this user (only grabs that live in public playlists)
    cur.execute(
        f"""
        SELECT pt.created_at, pt.video_id, s.title AS song_title,
               u.id AS taker_user_id, u.username AS taker_username, u.display_name AS taker_display_name,
               tp.id AS target_playlist_id, tp.title AS target_playlist_title, tp.plays AS target_playlist_plays,
               CASE WHEN sp.id IS NOT NULL AND {linkable('sp', viewer_id)} THEN sp.id END AS source_playlist_id,
               CASE WHEN sp.id IS NOT NULL AND {linkable('sp', viewer_id)} THEN sp.title END AS source_playlist_title
        FROM playlist_tracks pt
        JOIN songs s ON s.video_id = pt.video_id
        JOIN users u ON u.id = pt.added_by_user_id
        JOIN playlists tp ON tp.id = pt.playlist_id
        LEFT JOIN playlists sp ON sp.id = pt.source_playlist_id
        WHERE pt.source_user_id = ? AND pt.added_by_user_id != ? AND {listed('tp')}
        ORDER BY pt.created_at DESC
        """,
        (profile_user_id, profile_user_id),
    )
    grabs_from_user = [dict(r) for r in cur.fetchall()]

    # Lineage: tracks this user grabbed from others
    target_filter = "tp.kind = 'playlist'" if is_self else listed("tp")
    cur.execute(
        f"""
        SELECT pt.created_at, pt.video_id, s.title AS song_title,
               su.id AS source_user_id, su.username AS source_username, su.display_name AS source_display_name,
               CASE WHEN sp.id IS NOT NULL AND {linkable('sp', viewer_id)} THEN sp.id END AS source_playlist_id,
               CASE WHEN sp.id IS NOT NULL AND {linkable('sp', viewer_id)} THEN sp.title END AS source_playlist_title,
               tp.id AS target_playlist_id, tp.title AS target_playlist_title, tp.plays AS target_playlist_plays
        FROM playlist_tracks pt
        JOIN songs s ON s.video_id = pt.video_id
        JOIN users su ON su.id = pt.source_user_id
        JOIN playlists tp ON tp.id = pt.playlist_id
        LEFT JOIN playlists sp ON sp.id = pt.source_playlist_id
        WHERE pt.added_by_user_id = ? AND pt.source_user_id IS NOT NULL AND pt.source_user_id != ? AND {target_filter}
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
        **discovery.profile_extras(conn, profile_user_id, viewer_id),
    }


def resolve_comment_target(
    conn: sqlite3.Connection, target_type: str, target_id: str, viewer: int
) -> Tuple[str, str, bool]:
    """Validate a comment target the viewer can see. Returns (canonical id, activity label, public)."""
    cur = conn.cursor()
    raw = (target_id or "").strip()
    if target_type in ("playlist", "profile"):
        if not (raw.isascii() and raw.isdigit()) or len(raw) > 18:
            raise HTTPException(status_code=400, detail=f"Invalid {target_type} id")
        num = int(raw)
        if target_type == "playlist":
            pl = require_visible_playlist(conn, num, viewer)
            if pl["kind"] == "crate":
                raise ApiError(400, "not_commentable", "Crates don't have comments.")
            return str(num), f"playlist '{pl['title']}'", is_listed(pl)
        r = cur.execute("SELECT username FROM users WHERE id = ?", (num,)).fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="User not found")
        return str(num), f"@{r['username']}'s profile", True
    if target_type == "song":
        if not VIDEO_ID_PATTERN.match(raw):
            raise HTTPException(status_code=400, detail="Invalid song id")
        r = cur.execute("SELECT title FROM songs WHERE video_id = ?", (raw,)).fetchone()
        if not r or not song_visible(conn, raw, viewer):
            raise HTTPException(status_code=404, detail="Song not found in Webjam")
        return raw, f"song '{r['title']}'", song_visible(conn, raw, 0)
    raise HTTPException(status_code=400, detail="Invalid comment target_type")


@app.get("/api/comments")
def list_comments(
    request: Request,
    target_type: str = Query(..., max_length=16),
    target_id: str = Query(..., max_length=64),
    before_id: Optional[int] = Query(None),
    user_id: int = Query(1),
    conn: sqlite3.Connection = Depends(get_db),
):
    """Older pages of a comment thread: the 50 comments before `before_id`, oldest first."""
    canonical, _, _ = resolve_comment_target(conn, target_type, target_id, resolve_viewer(request, user_id))
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
    target_id, label, public = resolve_comment_target(conn, req.target_type, req.target_id, req.author_user_id)

    ts = now_str()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO comments (author_user_id, target_type, target_id, body, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (req.author_user_id, req.target_type, target_id, body, ts),
    )
    if public:
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


@app.get("/api/youtube/search")
async def search_youtube_embeddable(
    request: Request,
    q: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(8, ge=1, le=15),
):
    if auth_enabled():
        # Guests can't add tracks, and every search spends shared YouTube quota.
        resolve_actor(request, None)
    results = cached_search_results(q, max_results=limit)
    if results is None:
        enforce_rate_limit(request, "search")  # only cache misses cost anything
        try:
            results = await search_embeddable_youtube_songs(q, max_results=limit)
        except SearchBudgetExceeded:
            raise HTTPException(
                status_code=503,
                detail="Webjam's YouTube search budget for today is used up. Paste a YouTube link instead, or try again tomorrow.",
            )
    return {"query": q, "results": results}


# GET /api/youtube/check/{video_id} was removed: the frontend never used it and it let
# anonymous callers send unvalidated ids to YouTube.


# --- Discovery (Phase 1b, docs/phase1b-shell.md §3; queries live in app/discovery.py) ---

@app.get("/api/me/home")
def get_my_home(request: Request, user_id: Optional[int] = Query(None), conn: sqlite3.Connection = Depends(get_db)):
    if auth_enabled():
        uid = session_user_id(request)
        if not uid:
            raise ApiError(401, "signed_out", "Sign in to see your page.")
    else:
        uid = resolve_actor(request, user_id)
    return discovery.home(conn, uid, google_mode=auth_enabled())


@app.get("/api/discover", dependencies=[rate_limit("find")])
def get_discover(
    request: Request,
    tab: Literal["playlists", "tracks", "people"] = Query("playlists"),
    sort: Literal["fresh", "popular"] = Query("fresh"),
    offset: int = Query(0, ge=0, le=1_000_000),
    limit: int = Query(20, ge=1, le=50),
    user_id: int = Query(1),
    conn: sqlite3.Connection = Depends(get_db),
):
    if tab == "playlists":
        return discovery.discover_playlists(conn, sort, offset, limit)
    if tab == "tracks":
        return discovery.discover_tracks(conn, resolve_viewer(request, user_id), offset, limit)
    return discovery.discover_people(conn, offset, limit)


@app.get("/api/search", dependencies=[rate_limit("find")])
def get_search(
    request: Request,
    q: str = Query(..., max_length=1000),
    type: Literal["all", "playlists", "tracks", "people"] = Query("all"),
    limit: int = Query(10, ge=1, le=25),
    user_id: int = Query(1),
    conn: sqlite3.Connection = Depends(get_db),
):
    query = discovery.normalize_query(q)
    if not 1 <= len(query) <= discovery.SEARCH_MAX_QUERY:
        raise ApiError(422, "bad_query", f"Search for 1-{discovery.SEARCH_MAX_QUERY} characters.")
    return discovery.search(conn, resolve_viewer(request, user_id), query, type, limit)


@app.get("/api/playlists/{playlist_id}/related")
def get_related_playlists(
    playlist_id: int,
    request: Request,
    limit: int = Query(12, ge=1, le=50),
    offset: int = Query(0, ge=0, le=1_000_000),
    user_id: int = Query(1),
    conn: sqlite3.Connection = Depends(get_db),
):
    # The source may be the viewer's private playlist or crate: the output only names LISTED playlists.
    pl = require_visible_playlist(conn, playlist_id, resolve_viewer(request, user_id))
    return discovery.related_playlists(conn, int(pl["id"]), limit, offset)


# Serve static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def serve_shell() -> FileResponse:
    """The SPA shell. no-cache so a deploy is picked up on the next navigation."""
    return FileResponse(str(STATIC_DIR / "index.html"), headers={"Cache-Control": "no-cache"})


@app.get("/")
def serve_index():
    return serve_shell()


@app.get("/discover")
def serve_discover_page():
    return serve_shell()


@app.get("/search")
def serve_search_page():
    return serve_shell()


@app.get("/crate")
def serve_crate_page():
    return serve_shell()


_HANDLE_ROUTE = re.compile(r"^[a-z0-9_]{3,20}$")


@app.get("/u/{handle}")
def serve_profile_page(handle: str):
    # Case is folded like the API does; the router replaces the URL with the current handle.
    if not _HANDLE_ROUTE.match(handle.lower()):
        raise HTTPException(status_code=404, detail="Not found")
    return serve_shell()


@app.get("/legacy/")
def serve_legacy_ui():
    """The old UI, kept for comparison until the Phase 1b cutover (served with the legacy CSP)."""
    legacy = STATIC_DIR / "legacy" / "index.html"
    if not legacy.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(str(legacy), headers={"Cache-Control": "no-cache"})


# Page routes for the plain-text clipboard links (§5.2): the SPA maps the path onto its hash routes.
_P_ROUTE = re.compile(r"^\d{1,18}(?:-[A-Za-z0-9_-]{0,200})?$")


@app.get("/p/{ref}")
def serve_playlist_page(ref: str):
    if not _P_ROUTE.match(ref):
        raise HTTPException(status_code=404, detail="Not found")
    return serve_shell()


@app.get("/t/{video_id}")
def serve_track_page(video_id: str):
    if not VIDEO_ID_PATTERN.match(video_id):
        raise HTTPException(status_code=404, detail="Not found")
    return serve_shell()

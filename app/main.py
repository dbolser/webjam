import asyncio
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any, Dict, List, Optional
import httpx

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from app.auth import auth_enabled, base_url, resolve_actor, resolve_viewer, router as auth_router

from app.badges import BADGE_BY_KEY, BADGE_CATALOG, evaluate_and_award_badges, get_user_badge_progress
from app.database import get_db, hash_password, init_db
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Webjam — Social YouTube Playlist Curation", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("WEBJAM_SESSION_SECRET") or secrets.token_urlsafe(32),
    session_cookie="webjam_session",
    max_age=60 * 60 * 24 * 30,
    same_site="lax",
    https_only=os.environ.get("WEBJAM_BASE_URL", "").startswith("https://"),
)
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

class LoginRequest(BaseModel):
    username: str
    password: Optional[str] = "webjam"
    display_name: Optional[str] = None
    bio: Optional[str] = ""


class UpdateProfileRequest(BaseModel):
    display_name: str
    bio: str


class CreatePlaylistRequest(BaseModel):
    user_id: Optional[int] = None
    title: str
    description: Optional[str] = ""
    initial_urls: Optional[str] = ""


class UpdatePlaylistRequest(BaseModel):
    user_id: Optional[int] = None
    title: str
    description: str


class AddTracksRequest(BaseModel):
    user_id: Optional[int] = None
    urls_or_ids: str
    track_note: Optional[str] = ""
    custom_title: Optional[str] = None
    start_time: Optional[Any] = None
    end_time: Optional[Any] = None
    source_playlist_id: Optional[int] = None
    source_user_id: Optional[int] = None


class UpdateTrackRequest(BaseModel):
    user_id: Optional[int] = None
    track_note: Optional[str] = None
    start_time: Optional[Any] = None
    end_time: Optional[Any] = None
    update_clip: Optional[bool] = False
    direction: Optional[str] = None  # 'up' or 'down'
    new_video_id: Optional[str] = None
    custom_title: Optional[str] = None


class InspectPlaylistImportRequest(BaseModel):
    source_input: str
    max_tracks: int = 30


class CommitImportedTrackItem(BaseModel):
    video_id: str
    custom_title: Optional[str] = None
    track_note: Optional[str] = ""
    start_seconds: Optional[Any] = None
    end_seconds: Optional[Any] = None
    migrated_from_video_id: Optional[str] = None
    migrated_from_title: Optional[str] = None


class CommitPlaylistImportRequest(BaseModel):
    user_id: Optional[int] = None
    title: str
    description: Optional[str] = ""
    target_playlist_id: Optional[int] = None
    tracks: List[CommitImportedTrackItem]


class ReorderTracksRequest(BaseModel):
    user_id: Optional[int] = None
    track_ids: List[int]


class SnapshotTrackItem(BaseModel):
    id: Optional[int] = None
    video_id: str
    position: int
    track_note: Optional[str] = ""
    start_seconds: Optional[int] = None
    end_seconds: Optional[int] = None
    migrated_from_video_id: Optional[str] = None
    migrated_from_title: Optional[str] = None
    added_by_user_id: Optional[int] = None
    source_playlist_id: Optional[int] = None
    source_user_id: Optional[int] = None
    created_at: Optional[str] = None


class RestorePlaylistSnapshotRequest(BaseModel):
    user_id: Optional[int] = None
    title: str
    description: Optional[str] = ""
    tracks: List[SnapshotTrackItem]



class ToggleLikeRequest(BaseModel):
    user_id: Optional[int] = None
    count: int = 1
    reset: bool = False


class RecordPlayRequest(BaseModel):
    video_id: Optional[str] = None
    count: int = 1


class CreateCommentRequest(BaseModel):
    author_user_id: Optional[int] = None
    target_type: str  # 'playlist' | 'song' | 'profile'
    target_id: str
    body: str


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


def fetch_comments(conn: sqlite3.Connection, target_type: str, target_id: str) -> List[Dict[str, Any]]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT c.id, c.author_user_id, c.target_type, c.target_id, c.body, c.created_at,
               u.username AS author_username, u.display_name AS author_display_name, u.avatar_color AS author_avatar_color
        FROM comments c
        JOIN users u ON u.id = c.author_user_id
        WHERE c.target_type = ? AND c.target_id = ?
        ORDER BY c.created_at ASC, c.id ASC
        """,
        (target_type, str(target_id)),
    )
    return [dict(r) for r in cur.fetchall()]


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

    # Check if song already exists in Webjam
    cur.execute("SELECT * FROM songs WHERE video_id = ?", (video_id,))
    song_row = cur.fetchone()
    is_new_discovery = False

    if not song_row:
        meta = await fetch_youtube_metadata(video_id, custom_title=custom_title)
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

    # Determine next position (songs can be added to a playlist as many times as desired!)
    cur.execute(
        "SELECT COALESCE(MAX(position), 0) + 1 AS next_pos FROM playlist_tracks WHERE playlist_id = ?",
        (playlist_id,),
    )
    next_pos = cur.fetchone()["next_pos"]

    cur.execute(
        """
        INSERT INTO playlist_tracks
        (playlist_id, video_id, position, added_by_user_id, track_note, start_seconds, end_seconds, migrated_from_video_id, migrated_from_title, source_playlist_id, source_user_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            playlist_id,
            video_id,
            next_pos,
            user_id,
            (track_note or "").strip(),
            start_seconds,
            end_seconds,
            migrated_from_video_id,
            migrated_from_title,
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
        "title": song_title,
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
            "mode": "google" if auth_enabled() else "demo",
            "signed_in": bool(auth_enabled() and current_user["id"]),
        },
    }


@app.post("/api/auth/login")
def login_or_register(req: LoginRequest, conn: sqlite3.Connection = Depends(get_db)):
    if auth_enabled():
        raise HTTPException(status_code=404, detail="Use Google sign-in")
    clean_uname = req.username.strip().lower().lstrip("@")
    if not clean_uname:
        raise HTTPException(status_code=400, detail="Username cannot be empty")

    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE LOWER(username) = ?", (clean_uname,))
    existing = cur.fetchone()
    if existing:
        return {"user_id": existing["id"], "created": False}

    # Register new user
    palette = ["#1f5f5b", "#8c3b2b", "#3d4e81", "#6b4226", "#4a6b2c", "#7a3b69"]
    cur.execute("SELECT COUNT(*) AS cnt FROM users")
    color = palette[cur.fetchone()["cnt"] % len(palette)]
    display = (req.display_name or req.username).strip()
    ts = now_str()
    cur.execute(
        """
        INSERT INTO users (username, display_name, bio, avatar_color, password_hash, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            clean_uname,
            display,
            (req.bio or "New crate digger on Webjam.").strip(),
            color,
            hash_password(req.password or "webjam"),
            ts,
        ),
    )
    new_uid = cur.lastrowid
    conn.commit()
    return {"user_id": new_uid, "created": True}


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
                   s.title, s.channel_title, s.thumbnail_url, s.first_added_by_user_id,
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
               s.title, s.channel_title, s.thumbnail_url, s.total_plays,
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
    playlist["comments"] = fetch_comments(conn, "playlist", str(playlist_id))
    return playlist


@app.post("/api/playlists")
async def create_playlist(req: CreatePlaylistRequest, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    req.user_id = resolve_actor(request, req.user_id)
    title = req.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Playlist title is required")

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
    if req.initial_urls and req.initial_urls.strip():
        entries = extract_video_entries(req.initial_urls)
        for entry in entries:
            res = await add_single_video_to_playlist(
                conn=conn,
                playlist_id=playlist_id,
                user_id=req.user_id,
                video_id=entry["video_id"],
                start_seconds=entry["start_seconds"],
                end_seconds=entry["end_seconds"],
            )
            added_tracks.append(res)

    new_badges = evaluate_and_award_badges(conn, req.user_id)
    return {
        "playlist_id": playlist_id,
        "added_tracks": added_tracks,
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


@app.post("/api/playlists/{playlist_id}/tracks")
async def add_tracks_to_playlist(
    playlist_id: int, req: AddTracksRequest, request: Request, conn: sqlite3.Connection = Depends(get_db)
):
    req.user_id = resolve_actor(request, req.user_id)
    cur = conn.cursor()
    pl = require_playlist_owner(conn, playlist_id, req.user_id)

    entries = extract_video_entries(req.urls_or_ids)
    if not entries:
        raise HTTPException(
            status_code=400,
            detail="Could not find a valid YouTube link or 11-character video ID. Try pasting any youtube.com/watch?v=... or youtu.be/... link!",
        )

    explicit_start = parse_timestamp_seconds(req.start_time)
    explicit_end = parse_timestamp_seconds(req.end_time)

    results = []
    for entry in entries:
        vid = entry["video_id"]
        start_sec = explicit_start if explicit_start is not None else entry["start_seconds"]
        end_sec = explicit_end if explicit_end is not None else entry["end_seconds"]
        res = await add_single_video_to_playlist(
            conn=conn,
            playlist_id=playlist_id,
            user_id=req.user_id,
            video_id=vid,
            track_note=req.track_note or "",
            custom_title=req.custom_title if len(entries) == 1 else None,
            start_seconds=start_sec,
            end_seconds=end_sec,
            source_playlist_id=req.source_playlist_id,
            source_user_id=req.source_user_id,
        )
        results.append(res)

    # Evaluate badges for the user who added/grabbed the track
    new_badges = evaluate_and_award_badges(conn, req.user_id)

    # Also evaluate badges for the source curator (if grabbed from someone else) and original song discoverers
    if req.source_user_id and req.source_user_id != req.user_id:
        evaluate_and_award_badges(conn, req.source_user_id)

    for entry in entries:
        vid = entry["video_id"]
        cur.execute("SELECT first_added_by_user_id FROM songs WHERE video_id = ?", (vid,))
        srow = cur.fetchone()
        if srow and srow["first_added_by_user_id"] != req.user_id:
            evaluate_and_award_badges(conn, srow["first_added_by_user_id"])

    return {
        "results": results,
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
    migrated_title = track["current_song_title"]
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
            meta = await fetch_youtube_metadata(new_vid, custom_title=req.custom_title)
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
            migrated_title = req.custom_title.strip() if (req.custom_title and req.custom_title.strip()) else srow["title"]
            migrated_channel = srow["channel_title"]

        if new_vid != track["video_id"]:
            if orig_from_vid and new_vid == orig_from_vid:
                # Reverted back to the original un-migrated track
                orig_from_vid = None
                orig_from_title = None
            elif not orig_from_vid:
                orig_from_vid = track["video_id"]
                orig_from_title = f"{track['current_song_title']}" + (
                    f" ({track['current_channel_title']})"
                    if track["current_channel_title"] and track["current_channel_title"] not in ("YouTube", "YouTube Artist")
                    else ""
                )

        cur.execute(
            """
            UPDATE playlist_tracks
            SET video_id = ?, migrated_from_video_id = ?, migrated_from_title = ?
            WHERE id = ? AND playlist_id = ?
            """,
            (new_vid, orig_from_vid, orig_from_title, track_id, playlist_id),
        )
        migrated_video_id = new_vid

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


@app.post("/api/playlists/import/inspect")
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


@app.post("/api/playlists/import/commit")
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
    for item in req.tracks:
        vids = extract_video_ids(item.video_id)
        if not vids:
            continue
        vid = vids[0]
        start_sec = parse_timestamp_seconds(item.start_seconds)
        end_sec = parse_timestamp_seconds(item.end_seconds)
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
        added_tracks.append(res)

    new_badges = evaluate_and_award_badges(conn, req.user_id)
    return {
        "playlist_id": playlist_id,
        "title": playlist_title,
        "added_tracks": added_tracks,
        "new_badges": new_badges,
    }


@app.get("/api/playlists/{playlist_id}/audit-embeds")
async def audit_playlist_embeds(
    playlist_id: int,
    conn: sqlite3.Connection = Depends(get_db),
):
    cur = conn.cursor()
    cur.execute("SELECT id, title FROM playlists WHERE id = ?", (playlist_id,))
    pl = cur.fetchone()
    if not pl:
        raise HTTPException(status_code=404, detail="Playlist not found")

    cur.execute(
        """
        SELECT pt.id AS track_id, pt.video_id, pt.position, pt.track_note,
               pt.start_seconds, pt.end_seconds,
               pt.migrated_from_video_id, pt.migrated_from_title,
               s.title, s.channel_title, s.thumbnail_url
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
        async def check_row(r: Dict[str, Any]) -> Dict[str, Any]:
            async with sem:
                info = await check_video_embeddability(r["video_id"], client=client)
                base_title = r.get("migrated_from_title") or r["title"] or info.get("title") or r["video_id"]
                return {
                    **r,
                    "embeddable": bool(info.get("embeddable")),
                    "status": info.get("status", "UNKNOWN"),
                    "reason": info.get("reason"),
                    "suggested_query": clean_migration_query(
                        base_title,
                        "" if r.get("migrated_from_title") else (r["channel_title"] or info.get("channel_title") or ""),
                    ),
                }

        audited = await asyncio.gather(*(check_row(r) for r in rows))

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
    req.user_id = resolve_actor(request, req.user_id)
    pl = require_playlist_owner(conn, playlist_id, req.user_id)
    cur = conn.cursor()

    ts = now_str()
    cur.execute(
        "UPDATE playlists SET title = ?, description = ?, updated_at = ? WHERE id = ?",
        (req.title.strip() or "Untitled Playlist", (req.description or "").strip(), ts, playlist_id),
    )

    cur.execute("DELETE FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,))
    for idx, t in enumerate(req.tracks, start=1):
        if t.id is not None:
            cur.execute(
                """
                INSERT INTO playlist_tracks (
                    id, playlist_id, video_id, position, track_note,
                    start_seconds, end_seconds,
                    migrated_from_video_id, migrated_from_title,
                    added_by_user_id, source_playlist_id, source_user_id, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    t.id,
                    playlist_id,
                    t.video_id,
                    idx,
                    (t.track_note or "").strip(),
                    t.start_seconds,
                    t.end_seconds,
                    t.migrated_from_video_id,
                    t.migrated_from_title,
                    t.added_by_user_id or pl["user_id"],
                    t.source_playlist_id,
                    t.source_user_id,
                    t.created_at or ts,
                ),
            )
        else:
            cur.execute(
                """
                INSERT INTO playlist_tracks (
                    playlist_id, video_id, position, track_note,
                    start_seconds, end_seconds,
                    migrated_from_video_id, migrated_from_title,
                    added_by_user_id, source_playlist_id, source_user_id, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    playlist_id,
                    t.video_id,
                    idx,
                    (t.track_note or "").strip(),
                    t.start_seconds,
                    t.end_seconds,
                    t.migrated_from_video_id,
                    t.migrated_from_title,
                    t.added_by_user_id or pl["user_id"],
                    t.source_playlist_id,
                    t.source_user_id,
                    t.created_at or ts,
                ),
            )

    conn.commit()
    return {"ok": True}



@app.post("/api/playlists/{playlist_id}/like")
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


@app.post("/api/songs/{video_id}/like")
def toggle_song_like(
    video_id: str, req: ToggleLikeRequest, request: Request, conn: sqlite3.Connection = Depends(get_db)
):
    req.user_id = resolve_actor(request, req.user_id)
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


@app.post("/api/playlists/{playlist_id}/play")
def record_playlist_play(
    playlist_id: int, req: RecordPlayRequest, conn: sqlite3.Connection = Depends(get_db)
):
    cur = conn.cursor()
    cur.execute("SELECT id, user_id, plays FROM playlists WHERE id = ?", (playlist_id,))
    pl = cur.fetchone()
    if not pl:
        raise HTTPException(status_code=404, detail="Playlist not found")

    inc = max(1, min(req.count, 20))
    cur.execute("UPDATE playlists SET plays = plays + ? WHERE id = ?", (inc, playlist_id))
    if req.video_id:
        cur.execute(
            "UPDATE songs SET total_plays = total_plays + ? WHERE video_id = ?",
            (inc, req.video_id),
        )
    conn.commit()

    owner_badges = evaluate_and_award_badges(conn, pl["user_id"])
    cur.execute("SELECT plays FROM playlists WHERE id = ?", (playlist_id,))
    updated_plays = cur.fetchone()["plays"]

    return {
        "plays": updated_plays,
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
    song["comments"] = fetch_comments(conn, "song", video_id)
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
                   s.title, s.channel_title,
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
        "comments": fetch_comments(conn, "profile", str(profile_user_id)),
    }


@app.post("/api/comments")
def create_comment(req: CreateCommentRequest, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    req.author_user_id = resolve_actor(request, req.author_user_id)
    if req.target_type not in ("playlist", "song", "profile"):
        raise HTTPException(status_code=400, detail="Invalid comment target_type")
    body = req.body.strip()
    if not body:
        raise HTTPException(status_code=400, detail="Comment cannot be empty")

    ts = now_str()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO comments (author_user_id, target_type, target_id, body, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (req.author_user_id, req.target_type, str(req.target_id), body, ts),
    )

    # Record activity
    label = req.target_type
    if req.target_type == "playlist":
        cur.execute("SELECT title FROM playlists WHERE id = ?", (int(req.target_id),))
        r = cur.fetchone()
        label = f"playlist '{r['title']}'" if r else "a playlist"
    elif req.target_type == "song":
        cur.execute("SELECT title FROM songs WHERE video_id = ?", (req.target_id,))
        r = cur.fetchone()
        label = f"song '{r['title']}'" if r else "a song"
    elif req.target_type == "profile":
        cur.execute("SELECT username FROM users WHERE id = ?", (int(req.target_id),))
        r = cur.fetchone()
        label = f"@{r['username']}'s profile" if r else "a profile"

    cur.execute(
        """
        INSERT INTO activity_log (actor_user_id, action_type, summary, link_type, link_id, created_at)
        VALUES (?, 'commented', ?, ?, ?, ?)
        """,
        (req.author_user_id, f"commented on {label}", req.target_type, str(req.target_id), ts),
    )
    conn.commit()

    new_badges = evaluate_and_award_badges(conn, req.author_user_id)
    return {
        "comments": fetch_comments(conn, req.target_type, str(req.target_id)),
        "new_badges": new_badges,
    }


@app.get("/api/youtube/search")
async def search_youtube_embeddable(
    q: str = Query(..., min_length=1),
    limit: int = Query(8, ge=1, le=15),
):
    results = await search_embeddable_youtube_songs(q, max_results=limit)
    return {"query": q, "results": results}


@app.get("/api/youtube/check/{video_id}")
async def check_youtube_embeddable(video_id: str):
    return await check_video_embeddability(video_id)


# Serve static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def serve_index():
    return FileResponse(str(STATIC_DIR / "index.html"))


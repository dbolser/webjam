"""The op log: the only write path for playlist tracks and playlist meta (Phase 1a).

`apply_ops` applies a batch of ops in one transaction, writes one `playlist_ops` row
per per-playlist effect (with a server-computed inverse), and keeps the per-user undo
stack on the server. `undo_group` / `redo_group` pop and push whole groups.

See docs/phase1-ops-api.md for the contract.
"""

import asyncio
import hashlib
import json
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Awaitable, Callable, Dict, Iterable, List, Literal, Optional, Set, Tuple, Union

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, TypeAdapter

from app.badges import evaluate_and_award_badges
from app.errors import ApiError
from app.visibility import is_listed, is_visible, linkable, listed
from app.youtube import MAX_CLIP_SECONDS, parse_timestamp_seconds

# --- Caps ------------------------------------------------------------------------
MAX_TITLE = 200
MAX_TEXT = 5000
MAX_TRACKS_PER_PLAYLIST = 1000
MAX_ENTRIES_PER_REQUEST = 50
MAX_OPS_PER_BATCH = 20
MAX_PLAYLISTS_PER_BATCH = 10
SERVER_MAX_ITEMS = 1000
UNDO_GROUP_LIMIT = 500
OP_RETENTION_DAYS = 90
DOCK_RECENT_KEEP = 20
DOCK_RECENT_SHOWN = 5
DOCK_MAX_PINS = 12
DOCK_MAX_SLOT = 9
RENORM_MIN_GAP = 1e-6
RENORM_MAX_ABS = 1e9

ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
VIDEO_ID_REGEX = r"^[A-Za-z0-9_-]{11}$"

ROLE_RANK = {"viewer": 1, "contributor": 2, "editor": 3, "owner": 4}
OP_MIN_ROLE = {"insert": "editor", "delete": "editor", "move": "editor", "update": "editor", "set_meta": "owner",
               "reorder": "editor"}
# Batch and group ids the server mints. A client op_id / group_id may not start with one.
RESERVED_ID_PREFIXES = ("srv-", "fork-", "promote-")

TRACK_COLS = (
    "id", "playlist_id", "video_id", "position", "added_by_user_id", "track_note",
    "start_seconds", "end_seconds", "migrated_from_video_id", "migrated_from_title",
    "display_title", "source_playlist_id", "source_user_id", "source_track_id", "created_at",
)
SNAPSHOT_COLS = tuple(c for c in TRACK_COLS if c != "position")


def now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid() -> str:
    """A ULID (48-bit ms timestamp + 80 random bits, Crockford base32): sortable and unique."""
    value = (int(time.time() * 1000) << 80) | secrets.randbits(80)
    out = []
    for _ in range(26):
        out.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(out))


# --- Lookup failures (raised by the metadata prefetch) -------------------------------

class VideoLookupFailed(Exception):
    """YouTube had no metadata for this id (deleted, private or never existed)."""

    reason = "not_found"


class VideoLookupUnavailable(VideoLookupFailed):
    """YouTube could not be asked right now (network error, 429/5xx). Worth retrying."""

    reason = "youtube_unavailable"


NOT_FOUND_DETAIL = "YouTube couldn't find that video (it may be private, deleted or mistyped)."
UNAVAILABLE_DETAIL = "YouTube isn't answering right now. Please try again in a moment."


def lookup_error(reason: str, **extra: Any) -> ApiError:
    if reason == VideoLookupUnavailable.reason:
        return ApiError(503, "youtube_unavailable", UNAVAILABLE_DETAIL, headers={"Retry-After": "30"}, **extra)
    return ApiError(400, "video_not_found", NOT_FOUND_DETAIL, **extra)


# --- Pydantic op models -----------------------------------------------------------------

DbId = Annotated[int, Field(ge=1, le=2**53)]
TimeValue = Optional[Union[int, float, Annotated[str, Field(max_length=32)]]]
OpIdStr = Annotated[str, Field(pattern=ID_PATTERN)]
VideoIdStr = Annotated[str, Field(pattern=VIDEO_ID_REGEX)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ItemRef(_Strict):
    track_id: Optional[DbId] = None
    playlist_id: Optional[DbId] = None
    # Display hints carried by the clipboard / resolve-paste. Never trusted or stored.
    user_id: Optional[int] = None
    username: Optional[Annotated[str, Field(max_length=64)]] = None


class InsertItem(_Strict):
    video_id: VideoIdStr
    start_seconds: TimeValue = None
    end_seconds: TimeValue = None
    note: Optional[Annotated[str, Field(max_length=MAX_TEXT)]] = ""
    display_title: Optional[Annotated[str, Field(max_length=MAX_TITLE)]] = None
    ref: Optional[ItemRef] = None
    # Display hints from /api/resolve-paste and the clipboard payload: accepted, ignored.
    title: Optional[Annotated[str, Field(max_length=500)]] = None
    channel_title: Optional[Annotated[str, Field(max_length=500)]] = None
    thumbnail_url: Optional[Annotated[str, Field(max_length=2048)]] = None
    known: Optional[bool] = None
    embeddable: Optional[bool] = None
    from_: Optional[Annotated[str, Field(max_length=32)]] = Field(default=None, alias="from")
    # Server-only (the import path); a client body can never set these.
    _migrated_from_video_id: Optional[str] = PrivateAttr(default=None)
    _migrated_from_title: Optional[str] = PrivateAttr(default=None)


class _Placement(_Strict):
    after_track_id: Optional[DbId] = None
    position: Optional[Annotated[int, Field(ge=1, le=10**6)]] = None


class InsertOp(_Placement):
    kind: Literal["insert"]
    playlist_id: DbId
    items: Annotated[List[InsertItem], Field(min_length=1, max_length=SERVER_MAX_ITEMS)]


class DeleteOp(_Strict):
    kind: Literal["delete"]
    playlist_id: DbId
    track_ids: Annotated[List[DbId], Field(min_length=1, max_length=SERVER_MAX_ITEMS)]


class MoveOp(_Placement):
    kind: Literal["move"]
    playlist_id: DbId
    track_ids: Annotated[List[DbId], Field(min_length=1, max_length=SERVER_MAX_ITEMS)]
    to_playlist_id: Optional[DbId] = None


class TrackSet(_Strict):
    note: Optional[Annotated[str, Field(max_length=MAX_TEXT)]] = None
    start_seconds: TimeValue = None
    end_seconds: TimeValue = None
    display_title: Optional[Annotated[str, Field(max_length=MAX_TITLE)]] = None
    video_id: Optional[VideoIdStr] = None


class UpdateOp(_Strict):
    kind: Literal["update"]
    playlist_id: DbId
    track_id: DbId
    set: TrackSet


class MetaSet(_Strict):
    title: Optional[Annotated[str, Field(max_length=MAX_TITLE)]] = None
    description: Optional[Annotated[str, Field(max_length=MAX_TEXT)]] = None
    visibility: Optional[Literal["public", "unlisted", "private"]] = None


class SetMetaOp(_Strict):
    kind: Literal["set_meta"]
    playlist_id: DbId
    set: MetaSet


Op = Annotated[Union[InsertOp, DeleteOp, MoveOp, UpdateOp, SetMetaOp], Field(discriminator="kind")]
OPS_ADAPTER = TypeAdapter(List[Op])


class ReorderOp(_Strict):
    """Server-only (PUT /api/playlists/{id}/reorder): the playlist's whole new order as ONE effect.

    Not part of the client Op union. Its inverse is the previous order, so a reorder of n tracks
    costs one op row and one O(n) pass instead of up to n single-track moves.
    """
    kind: Literal["reorder"] = "reorder"
    playlist_id: DbId
    track_ids: Annotated[List[DbId], Field(max_length=MAX_TRACKS_PER_PLAYLIST)]


def is_reserved_id(value: Optional[str]) -> bool:
    return bool(value) and value.lower().startswith(RESERVED_ID_PREFIXES)


def parse_ops(raw: List[Dict[str, Any]]) -> List[Any]:
    """Validate server-composed ops (legacy endpoints, promote, fork)."""
    return OPS_ADAPTER.validate_python(raw)


@dataclass(frozen=True)
class Actor:
    user_id: int
    # Phase 5: memberships are looked up per playlist; nothing else changes here.


@dataclass
class OpsOutcome:
    group_id: str
    replayed: bool
    results: List[Dict[str, Any]]
    versions: Dict[int, int]
    order: Dict[int, List[int]]
    rows: Dict[int, Dict[str, Any]]
    skipped: List[Dict[str, Any]]
    new_badges: List[Dict[str, Any]]
    claimed: Set[str] = field(default_factory=set)  # video ids whose discovery this batch claimed
    label: Optional[str] = None


# --- Roles ------------------------------------------------------------------------------

def playlist_role(conn: sqlite3.Connection, pl: sqlite3.Row, user_id: int) -> Optional[str]:
    if user_id and pl["user_id"] == user_id:
        return "owner"
    # Phase 5: SELECT role FROM playlist_members WHERE playlist_id = ? AND user_id = ?
    if pl["kind"] == "playlist" and pl["visibility"] in ("public", "unlisted"):
        return "viewer"
    return None


def require_playlist_role(
    conn: sqlite3.Connection, playlist_id: int, actor: Actor, min_role: str, **extra: Any
) -> sqlite3.Row:
    pl = conn.execute("SELECT * FROM playlists WHERE id = ?", (int(playlist_id),)).fetchone()
    role = playlist_role(conn, pl, actor.user_id) if pl else None
    if role is None:
        raise ApiError(404, "playlist_not_found", "Playlist not found", **extra)
    if ROLE_RANK[role] < ROLE_RANK[min_role]:
        raise ApiError(403, "not_allowed", "Only the playlist creator can change this playlist", **extra)
    return pl


# --- Crate ------------------------------------------------------------------------------

def ensure_crate(conn: sqlite3.Connection, user_id: int) -> int:
    """The user's crate id, creating it if needed. Does not commit."""
    ts = now_str()
    conn.execute(
        """
        INSERT OR IGNORE INTO playlists (user_id, kind, visibility, version, title, description, plays, created_at, updated_at)
        VALUES (?, 'crate', 'private', 0, 'Crate', '', 0, ?, ?)
        """,
        (int(user_id), ts, ts),
    )
    return int(
        conn.execute("SELECT id FROM playlists WHERE user_id = ? AND kind = 'crate'", (int(user_id),)).fetchone()["id"]
    )


def crate_id_for(conn: sqlite3.Connection, user_id: int) -> Optional[int]:
    row = conn.execute("SELECT id FROM playlists WHERE user_id = ? AND kind = 'crate'", (int(user_id),)).fetchone()
    return int(row["id"]) if row else None


# --- Song metadata prefetch (runs before any write lock is taken) -------------------------

def unknown_video_ids(conn: sqlite3.Connection, video_ids: Iterable[str]) -> List[str]:
    out = []
    for vid in dict.fromkeys(v for v in video_ids if v):
        if not conn.execute("SELECT 1 FROM songs WHERE video_id = ?", (vid,)).fetchone():
            out.append(vid)
    return out


async def lookup_song_metadata(fetch: Callable[[str], Awaitable[Dict[str, Any]]], video_id: str) -> Dict[str, Any]:
    meta = await fetch(video_id)
    if meta and meta.get("status") == "UNAVAILABLE":
        raise VideoLookupUnavailable(video_id)
    if not meta or meta.get("status") == "NOT_FOUND" or not meta.get("title"):
        raise VideoLookupFailed(video_id)
    return meta


async def prefetch_song_meta(
    conn: sqlite3.Connection, video_ids: Iterable[str], fetch: Callable[[str], Awaitable[Dict[str, Any]]]
) -> Dict[str, Dict[str, Any]]:
    """YouTube metadata for every id with no songs row: {video_id: meta | {"error": reason}}."""
    unknown = unknown_video_ids(conn, video_ids)

    async def one(vid: str) -> Tuple[str, Dict[str, Any]]:
        try:
            return vid, await lookup_song_metadata(fetch, vid)
        except VideoLookupFailed as exc:
            return vid, {"error": exc.reason}

    return dict(await asyncio.gather(*(one(v) for v in unknown)))


# --- Small helpers --------------------------------------------------------------------------

def clean_display_title(custom_title: Optional[str], song_title: Optional[str]) -> Optional[str]:
    """A per-row title override, or None when it is empty or the same as the song's real title."""
    title = (custom_title or "").strip()[:MAX_TITLE]
    return title if title and title != (song_title or "") else None


def _parse_clip_value(raw: Any) -> Tuple[bool, Optional[int]]:
    """(ok, seconds). None stays None; anything else must parse within 0..MAX_CLIP_SECONDS."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return True, None
    sec = parse_timestamp_seconds(raw)
    return (sec is not None), sec


def _clip(start_raw: Any, end_raw: Any, **where: Any) -> Tuple[Optional[int], Optional[int]]:
    ok_s, start = _parse_clip_value(start_raw)
    ok_e, end = _parse_clip_value(end_raw)
    if not ok_s or not ok_e:
        raise ApiError(400, "bad_clip", f"Clip times must be between 0 and {MAX_CLIP_SECONDS} seconds.", **where)
    if start is not None and end is not None and end <= start:
        raise ApiError(400, "bad_clip", "End time must be greater than start time.", **where)
    return start, end


def _num(v: Any) -> Any:
    """Clip seconds are stored REAL; the API keeps whole seconds until Phase 2."""
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return float(a) == float(b)
    return a == b


def _plural(n: int, word: str = "track") -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _ordered(conn: sqlite3.Connection, playlist_id: int) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM playlist_tracks WHERE playlist_id = ? ORDER BY position ASC, id ASC", (playlist_id,)
    ).fetchall()


def _snapshot(row: sqlite3.Row) -> Dict[str, Any]:
    return {c: row[c] for c in SNAPSHOT_COLS}


def _renormalise(conn: sqlite3.Connection, playlist_id: int) -> None:
    for i, r in enumerate(_ordered(conn, playlist_id), start=1):
        conn.execute("UPDATE playlist_tracks SET position = ? WHERE id = ?", (float(i), r["id"]))


def _keys_for(conn: sqlite3.Connection, playlist_id: int, remaining_ids: List[int], k: int, n: int) -> List[float]:
    """Sort keys for n rows placed before remaining_ids[k] (after remaining_ids[k-1])."""

    def bounds() -> Tuple[Optional[float], Optional[float], bool]:
        pos = {r["id"]: r["position"] for r in conn.execute(
            "SELECT id, position FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,))}
        a = pos[remaining_ids[k - 1]] if k > 0 else None
        b = pos[remaining_ids[k]] if k < len(remaining_ids) else None
        too_big = any(abs(p) > RENORM_MAX_ABS for p in pos.values())
        too_tight = a is not None and b is not None and (b - a) / (n + 1) < RENORM_MIN_GAP
        return a, b, too_big or too_tight

    a, b, renorm = bounds()
    if renorm:
        _renormalise(conn, playlist_id)
        a, b, _ = bounds()
    if a is None and b is None:
        return [float(i) for i in range(1, n + 1)]
    if b is None:
        return [a + i for i in range(1, n + 1)]
    if a is None:
        return [b - (n + 1) + i for i in range(1, n + 1)]
    return [a + (b - a) * i / (n + 1) for i in range(1, n + 1)]


def _insert_track(conn: sqlite3.Connection, row: Dict[str, Any], position: float) -> int:
    cols = [c for c in TRACK_COLS if c != "position" and (c != "id" or row.get("id") is not None)]
    values = [row.get(c) for c in cols]
    cols.append("position")
    values.append(position)
    cur = conn.execute(
        f"INSERT INTO playlist_tracks ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", values
    )
    return int(row["id"]) if row.get("id") is not None else int(cur.lastrowid)


def _sanitize_refs(conn: sqlite3.Connection, row: Dict[str, Any]) -> Dict[str, Any]:
    """Null out FK references to rows that were deleted since a snapshot was taken.

    source_track_id has no FK (ids are AUTOINCREMENT, never reused): it is kept as is, so a
    restored row links again as soon as its source row is back, and readers ignore dangling ids.
    """
    out = dict(row)
    checks = (
        ("source_playlist_id", "SELECT 1 FROM playlists WHERE id = ?"),
        ("source_user_id", "SELECT 1 FROM users WHERE id = ?"),
    )
    for col, sql in checks:
        if out.get(col) is not None and not conn.execute(sql, (out[col],)).fetchone():
            out[col] = None
    return out


def _resolve_index(
    remaining_ids: List[int],
    after_track_id: Optional[int],
    position: Optional[int],
    block_ids: Iterable[int] = (),
    **where: Any,
) -> int:
    if after_track_id is not None and position is not None:
        raise ApiError(400, "bad_placement", "Give after_track_id or position, not both.", **where)
    if after_track_id is not None:
        if after_track_id in set(block_ids):
            raise ApiError(400, "bad_placement", "Can't place tracks after one of the tracks being moved.", **where)
        if after_track_id not in remaining_ids:
            raise ApiError(409, "anchor_missing", "The track to place after is gone. Reload and try again.",
                           after_track_id=after_track_id, **where)
        return remaining_ids.index(after_track_id) + 1
    if position is not None:
        return min(max(int(position), 1), len(remaining_ids) + 1) - 1
    return len(remaining_ids)


def _playlist_title(conn: sqlite3.Connection, pid: int) -> str:
    row = conn.execute("SELECT title FROM playlists WHERE id = ?", (pid,)).fetchone()
    return row["title"] if row else "playlist"


def _log_activity(
    conn: sqlite3.Connection, actor_id: int, action: str, summary: str, link_type: str, link_id: Any,
    group_id: Optional[str], ts: str,
) -> None:
    conn.execute(
        """
        INSERT INTO activity_log (actor_user_id, action_type, summary, link_type, link_id, created_at, op_group_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (actor_id, action, summary, link_type, str(link_id), ts, group_id),
    )


# --- Discovery ("first found by") --------------------------------------------------------------

def claim_discovery(
    conn: sqlite3.Connection, playlist_row: sqlite3.Row, video_ids: Iterable[str], group_id: Optional[str], ts: str
) -> List[str]:
    """Claim first-finder credit for unclaimed songs placed in a LISTED playlist. Returns claimed ids."""
    if not is_listed(playlist_row):
        return []
    claimed = []
    for vid in dict.fromkeys(video_ids):
        cur = conn.execute(
            """
            UPDATE songs SET first_added_by_user_id = ?, first_added_playlist_id = ?, first_added_at = ?
            WHERE video_id = ? AND first_added_by_user_id IS NULL
            """,
            (playlist_row["user_id"], playlist_row["id"], ts, vid),
        )
        if cur.rowcount:
            title = conn.execute("SELECT title FROM songs WHERE video_id = ?", (vid,)).fetchone()["title"]
            _log_activity(conn, playlist_row["user_id"], "discovered_song",
                          f"was the first on Webjam to add '{title}'", "song", vid, group_id, ts)
            claimed.append(vid)
    return claimed


def revert_claims(conn: sqlite3.Connection, claims: Iterable[Dict[str, Any]]) -> None:
    """Undo of a claiming group: reset the song, then re-claim from the earliest remaining LISTED row."""
    for c in claims:
        vid, uid = c.get("video_id"), c.get("user_id")
        song = conn.execute("SELECT first_added_by_user_id FROM songs WHERE video_id = ?", (vid,)).fetchone()
        if not song or song["first_added_by_user_id"] != uid:
            continue
        conn.execute(
            "UPDATE songs SET first_added_by_user_id = NULL, first_added_playlist_id = NULL, first_added_at = NULL WHERE video_id = ?",
            (vid,),
        )
        earliest = conn.execute(
            f"""
            SELECT pt.created_at, p.id AS playlist_id, p.user_id FROM playlist_tracks pt
            JOIN playlists p ON p.id = pt.playlist_id
            WHERE pt.video_id = ? AND {listed('p')}
            ORDER BY pt.created_at ASC, pt.id ASC LIMIT 1
            """,
            (vid,),
        ).fetchone()
        if earliest:
            conn.execute(
                "UPDATE songs SET first_added_by_user_id = ?, first_added_playlist_id = ?, first_added_at = ? WHERE video_id = ?",
                (earliest["user_id"], earliest["playlist_id"], earliest["created_at"], vid),
            )


# --- Rendering ------------------------------------------------------------------------------

def render_track_rows(
    conn: sqlite3.Connection, playlist_row: sqlite3.Row, viewer: int, track_ids: Optional[Iterable[int]] = None
) -> List[Dict[str, Any]]:
    """Track rows in the shape of GET /api/playlists/{id} (position = 1-based ordinal)."""
    viewer = int(viewer or 0)
    pid = int(playlist_row["id"])
    rows = conn.execute(
        f"""
        WITH ordered AS (
            SELECT pt.*, ROW_NUMBER() OVER (ORDER BY pt.position ASC, pt.id ASC) AS ordinal
            FROM playlist_tracks pt WHERE pt.playlist_id = :pid
        )
        SELECT o.id, o.playlist_id, o.video_id, o.ordinal AS position, o.added_by_user_id,
               o.track_note, CAST(o.start_seconds AS INTEGER) AS start_seconds,
               CAST(o.end_seconds AS INTEGER) AS end_seconds,
               o.migrated_from_video_id, o.migrated_from_title,
               CASE WHEN sp.id IS NOT NULL AND {linkable('sp')} THEN sp.id END AS source_playlist_id,
               o.source_user_id,
               CASE WHEN sp.id IS NOT NULL AND {linkable('sp')} AND EXISTS (
                   SELECT 1 FROM playlist_tracks st WHERE st.id = o.source_track_id AND st.playlist_id = sp.id)
                 THEN o.source_track_id END AS source_track_id,
               o.created_at,
               COALESCE(o.display_title, s.title) AS title, s.title AS song_title, o.display_title,
               s.channel_title, s.thumbnail_url, s.total_plays,
               s.first_added_by_user_id,
               CASE WHEN dp.id IS NOT NULL AND {linkable('dp')} THEN s.first_added_playlist_id END AS first_added_playlist_id,
               s.first_added_at,
               fu.username AS first_finder_username, fu.display_name AS first_finder_display_name,
               su.username AS source_username, su.display_name AS source_display_name,
               CASE WHEN sp.id IS NOT NULL AND {linkable('sp')} THEN sp.title END AS source_playlist_title,
               (SELECT COUNT(DISTINCT pt2.playlist_id) FROM playlist_tracks pt2 JOIN playlists p2 ON p2.id = pt2.playlist_id
                 WHERE pt2.video_id = o.video_id AND {listed('p2')}) AS playlist_count,
               (SELECT COUNT(*) FROM comments c WHERE c.target_type = 'song' AND c.target_id = o.video_id) AS song_comment_count,
               (SELECT COALESCE(SUM(sl.count), 0) FROM song_likes sl WHERE sl.video_id = o.video_id) AS song_like_count,
               (SELECT COALESCE(sl.count, 0) FROM song_likes sl WHERE sl.video_id = o.video_id AND sl.user_id = :viewer) AS song_my_likes
        FROM ordered o
        JOIN songs s ON s.video_id = o.video_id
        LEFT JOIN users fu ON fu.id = s.first_added_by_user_id
        LEFT JOIN playlists dp ON dp.id = s.first_added_playlist_id
        LEFT JOIN users su ON su.id = o.source_user_id
        LEFT JOIN playlists sp ON sp.id = o.source_playlist_id
        ORDER BY o.ordinal ASC
        """,
        {"pid": pid, "viewer": viewer},
    ).fetchall()
    wanted = set(int(t) for t in track_ids) if track_ids is not None else None
    out = []
    for r in rows:
        if wanted is not None and r["id"] not in wanted:
            continue
        t = dict(r)
        t["song_my_likes"] = int(t.get("song_my_likes") or 0)
        t["song_liked_by_me"] = bool(t["song_my_likes"] > 0)
        t["is_first_discovery_here"] = (
            t["first_added_by_user_id"] == playlist_row["user_id"] and s_eq(t["first_added_playlist_id"], pid)
        )
        t["other_playlists"] = [
            dict(o)
            for o in conn.execute(
                f"""
                SELECT p2.id AS playlist_id, p2.title AS playlist_title, u2.id AS user_id, u2.username
                FROM playlist_tracks pt2
                JOIN playlists p2 ON p2.id = pt2.playlist_id
                JOIN users u2 ON u2.id = p2.user_id
                WHERE pt2.video_id = ? AND pt2.playlist_id != ?
                  AND ({listed('p2')} OR (p2.user_id = ? AND p2.kind = 'playlist'))
                ORDER BY pt2.created_at ASC
                """,
                (t["video_id"], pid, viewer),
            ).fetchall()
        ]
        out.append(t)
    return out


def s_eq(a: Any, b: Any) -> bool:
    return a is not None and b is not None and int(a) == int(b)


def _order_ids(conn: sqlite3.Connection, pid: int) -> List[int]:
    return [r["id"] for r in _ordered(conn, pid)]


def _versions(conn: sqlite3.Connection, pids: Iterable[int]) -> Dict[int, int]:
    out = {}
    for pid in pids:
        row = conn.execute("SELECT version FROM playlists WHERE id = ?", (pid,)).fetchone()
        if row:
            out[int(pid)] = int(row["version"])
    return out


def _render_rows(conn: sqlite3.Connection, viewer: int, track_ids: Iterable[int]) -> Dict[int, Dict[str, Any]]:
    by_pl: Dict[int, List[int]] = {}
    for tid in dict.fromkeys(track_ids):
        r = conn.execute("SELECT playlist_id FROM playlist_tracks WHERE id = ?", (tid,)).fetchone()
        if r:
            by_pl.setdefault(int(r["playlist_id"]), []).append(int(tid))
    out: Dict[int, Dict[str, Any]] = {}
    for pid, tids in by_pl.items():
        pl = conn.execute("SELECT * FROM playlists WHERE id = ?", (pid,)).fetchone()
        for row in render_track_rows(conn, pl, viewer, tids):
            out[int(row["id"])] = row
    return out


# --- Undo stack state -----------------------------------------------------------------------

def _group_label(conn: sqlite3.Connection, user_id: int, group_id: str) -> Optional[str]:
    row = conn.execute(
        "SELECT forward_json FROM playlist_ops WHERE user_id = ? AND group_id = ? ORDER BY id ASC LIMIT 1",
        (user_id, group_id),
    ).fetchone()
    return json.loads(row["forward_json"]).get("label") if row else None


def _top_undo_group(conn: sqlite3.Connection, user_id: int) -> Optional[str]:
    row = conn.execute(
        """
        SELECT group_id FROM playlist_ops
        WHERE user_id = ? AND undone_at IS NULL AND discarded_at IS NULL
        ORDER BY id DESC LIMIT 1
        """,
        (user_id,),
    ).fetchone()
    return row["group_id"] if row else None


def _next_redo_group(conn: sqlite3.Connection, user_id: int) -> Optional[str]:
    row = conn.execute(
        """
        SELECT group_id FROM playlist_ops
        WHERE user_id = ? AND undone_at IS NOT NULL AND discarded_at IS NULL
        ORDER BY id ASC LIMIT 1
        """,
        (user_id,),
    ).fetchone()
    return row["group_id"] if row else None


def undo_state(conn: sqlite3.Connection, user_id: int) -> Dict[str, Any]:
    if not user_id:
        return {"can_undo": False, "undo_label": None, "undo_group_id": None,
                "can_redo": False, "redo_label": None, "redo_group_id": None}
    top = _top_undo_group(conn, user_id)
    nxt = _next_redo_group(conn, user_id)
    # The group ids let a client assert which group it means (409 stack_moved otherwise).
    return {
        "can_undo": top is not None,
        "undo_label": _group_label(conn, user_id, top) if top else None,
        "undo_group_id": top,
        "can_redo": nxt is not None,
        "redo_label": _group_label(conn, user_id, nxt) if nxt else None,
        "redo_group_id": nxt,
    }


# --- Transactions -----------------------------------------------------------------------------

def _begin(conn: sqlite3.Connection) -> None:
    """Take the write lock. If the caller already opened a transaction, join it."""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")


# --- The batch context ---------------------------------------------------------------------------

@dataclass
class _Effect:
    playlist_id: int
    kind: str  # insert | delete | move | update | set_meta
    forward: Dict[str, Any]
    inverse: Dict[str, Any]


class _Batch:
    def __init__(self, conn: sqlite3.Connection, actor: Actor, group_id: str, ts: str,
                 origin: Optional[str] = None) -> None:
        self.conn = conn
        self.actor = actor
        self.group_id = group_id
        self.ts = ts
        # Who composed the batch: None (client or legacy endpoint), "fork" or "promote". Passed
        # explicitly by the server, never inferred from a (client-choosable) id.
        self.origin = origin
        self.effects: List[_Effect] = []
        self.provenance: Dict[int, str] = {}
        self.rendered_ids: List[int] = []
        self.claimed: Set[str] = set()
        self.credited_users: Set[int] = set()
        self.finders: Set[int] = set()

    def playlist(self, pid: int) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM playlists WHERE id = ?", (pid,)).fetchone()


def _post_effect(ctx: _Batch, eff: _Effect) -> None:
    """Discovery claims and activity for one effect; LISTED targets only. Shared by apply and redo."""
    conn, pl = ctx.conn, ctx.playlist(eff.playlist_id)
    fwd = eff.forward
    claims: List[Dict[str, Any]] = []
    if eff.kind == "insert":
        tids = fwd.get("track_ids") or []
        # A fork logs one "forked" line instead of a grab line for its copy-all insert.
        log_line = ctx.origin != "fork"
        if is_listed(pl) and tids:
            rows = conn.execute(
                f"""
                SELECT pt.id, pt.video_id, pt.source_user_id, COALESCE(pt.display_title, s.title) AS title
                FROM playlist_tracks pt JOIN songs s ON s.video_id = pt.video_id
                WHERE pt.id IN ({','.join('?' * len(tids))})
                """,
                tids,
            ).fetchall()
            by_id = {r["id"]: r for r in rows}
            rows = [by_id[t] for t in tids if t in by_id]
            if rows:
                for vid in claim_discovery(conn, pl, [r["video_id"] for r in rows], ctx.group_id, ctx.ts):
                    claims.append({"video_id": vid, "user_id": pl["user_id"], "playlist_id": pl["id"]})
                sources = {r["source_user_id"] for r in rows}
                more = f" and {len(rows) - 1} more" if len(rows) > 1 else ""
                first = rows[0]["title"]
                src = next(iter(sources)) if len(sources) == 1 else None
                if not log_line:
                    pass
                elif src is not None and src != ctx.actor.user_id:
                    uname = conn.execute("SELECT username FROM users WHERE id = ?", (src,)).fetchone()
                    _log_activity(conn, ctx.actor.user_id, "grabbed_track",
                                  f"grabbed '{first}'{more} from @{uname['username'] if uname else 'someone'} into '{pl['title']}'",
                                  "playlist", pl["id"], ctx.group_id, ctx.ts)
                else:
                    _log_activity(conn, ctx.actor.user_id, "added_track", f"added '{first}'{more} to '{pl['title']}'",
                                  "playlist", pl["id"], ctx.group_id, ctx.ts)
                for r in rows:
                    if r["source_user_id"]:
                        ctx.credited_users.add(int(r["source_user_id"]))
                for r in conn.execute(
                    f"SELECT DISTINCT s.first_added_by_user_id FROM songs s WHERE s.video_id IN ({','.join('?' * len(rows))})",
                    [r["video_id"] for r in rows],
                ):
                    if r[0]:
                        ctx.finders.add(int(r[0]))
    elif eff.kind == "update" and "video_id" in (fwd.get("set") or {}):
        for vid in claim_discovery(conn, pl, [fwd["set"]["video_id"]], ctx.group_id, ctx.ts):
            claims.append({"video_id": vid, "user_id": pl["user_id"], "playlist_id": pl["id"]})
    elif eff.kind == "set_meta":
        old_vis = (fwd.get("old") or {}).get("visibility")
        new_vis = (fwd.get("set") or {}).get("visibility")
        if new_vis is not None and old_vis != "public" and is_listed(pl):
            vids = [r["video_id"] for r in _ordered(conn, pl["id"])]
            for vid in claim_discovery(conn, pl, vids, ctx.group_id, ctx.ts):
                claims.append({"video_id": vid, "user_id": pl["user_id"], "playlist_id": pl["id"]})
            _log_activity(conn, ctx.actor.user_id, "published_playlist", f"published playlist '{pl['title']}'",
                          "playlist", pl["id"], ctx.group_id, ctx.ts)
    fwd["claims"] = claims
    ctx.claimed.update(c["video_id"] for c in claims)


# --- Applying request ops -----------------------------------------------------------------------

def _song_row(conn: sqlite3.Connection, vid: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM songs WHERE video_id = ?", (vid,)).fetchone()


def _ensure_song(conn: sqlite3.Connection, vid: str, meta: Dict[str, Any], ts: str) -> sqlite3.Row:
    conn.execute(
        """
        INSERT INTO songs (video_id, title, channel_title, thumbnail_url, first_added_by_user_id,
                           first_added_playlist_id, first_added_at, total_plays, created_at)
        VALUES (?, ?, ?, ?, NULL, NULL, NULL, 0, ?)
        ON CONFLICT(video_id) DO NOTHING
        """,
        (vid, meta["title"], meta.get("channel_title") or "", meta.get("thumbnail_url") or "", ts),
    )
    return _song_row(conn, vid)


def verified_source(
    conn: sqlite3.Connection, source_playlist_id: Optional[int], video_id: str, viewer: int
) -> Optional[Tuple[int, int]]:
    """(playlist_id, owner_id) if that playlist is VISIBLE to the viewer and contains the video."""
    if not source_playlist_id:
        return None
    pl = conn.execute("SELECT * FROM playlists WHERE id = ?", (int(source_playlist_id),)).fetchone()
    if not is_visible(pl, viewer):
        return None
    has = conn.execute(
        "SELECT 1 FROM playlist_tracks WHERE playlist_id = ? AND video_id = ? LIMIT 1", (pl["id"], video_id)
    ).fetchone()
    return (int(pl["id"]), int(pl["user_id"])) if has else None


def _provenance(conn: sqlite3.Connection, actor: Actor, item: InsertItem) -> Tuple[Dict[str, Any], str]:
    none = {"source_playlist_id": None, "source_user_id": None, "source_track_id": None,
            "migrated_from_video_id": None, "migrated_from_title": None}
    ref = item.ref
    if ref is None or (ref.track_id is None and ref.playlist_id is None):
        return none, "none"
    if ref.track_id is not None:
        r = conn.execute("SELECT * FROM playlist_tracks WHERE id = ?", (ref.track_id,)).fetchone()
        p = conn.execute("SELECT * FROM playlists WHERE id = ?", (r["playlist_id"],)).fetchone() if r else None
        if not r or not is_visible(p, actor.user_id) or r["video_id"] != item.video_id:
            return none, "dropped"
        migrated = {"migrated_from_video_id": r["migrated_from_video_id"], "migrated_from_title": r["migrated_from_title"]}
        if p["user_id"] != actor.user_id:
            return {"source_playlist_id": p["id"], "source_user_id": p["user_id"], "source_track_id": r["id"],
                    **migrated}, "credited"
        return {"source_playlist_id": r["source_playlist_id"], "source_user_id": r["source_user_id"],
                "source_track_id": r["source_track_id"], **migrated}, "kept"
    src = verified_source(conn, ref.playlist_id, item.video_id, actor.user_id)
    if not src:
        return none, "dropped"
    if src[1] == actor.user_id:
        return none, "none"
    return {**none, "source_playlist_id": src[0], "source_user_id": src[1]}, "credited"


def _apply_insert(ctx: _Batch, op: InsertOp, op_index: int, song_meta: Dict[str, Dict[str, Any]],
                  skip_unavailable: bool, skipped: List[Dict[str, Any]]) -> Dict[str, Any]:
    conn, pid = ctx.conn, op.playlist_id
    prepared = []
    my_skipped = []
    for item_index, item in enumerate(op.items):
        vid = item.video_id
        song = _song_row(conn, vid)
        if song is None:
            meta = song_meta.get(vid)
            if not meta or meta.get("error") or not meta.get("title"):
                reason = (meta or {}).get("error") or "not_found"
                if skip_unavailable:
                    entry = {"op_index": op_index, "item_index": item_index, "video_id": vid, "reason": reason}
                    skipped.append(entry)
                    my_skipped.append(entry)
                    continue
                raise lookup_error(reason, op_index=op_index, item_index=item_index, video_id=vid)
            song_title = meta["title"]
        else:
            meta = None
            song_title = song["title"]
        start, end = _clip(item.start_seconds, item.end_seconds, op_index=op_index, item_index=item_index)
        prov, prov_kind = _provenance(conn, ctx.actor, item)
        if prov_kind in ("none", "dropped") and item._migrated_from_video_id:
            prov["migrated_from_video_id"] = item._migrated_from_video_id
            prov["migrated_from_title"] = item._migrated_from_title
        prepared.append((item, meta, prov, prov_kind, start, end, clean_display_title(item.display_title, song_title)))

    result = {"kind": "insert", "playlist_id": pid, "track_ids": [], "skipped": my_skipped}
    if not prepared:
        return result

    count = conn.execute("SELECT COUNT(*) FROM playlist_tracks WHERE playlist_id = ?", (pid,)).fetchone()[0]
    if count + len(prepared) > MAX_TRACKS_PER_PLAYLIST:
        raise ApiError(400, "playlist_full", f"Playlists can hold at most {MAX_TRACKS_PER_PLAYLIST} tracks.",
                       op_index=op_index, playlist_id=pid)
    for item, meta, *_ in prepared:
        if meta is not None:
            _ensure_song(conn, item.video_id, meta, ctx.ts)

    remaining = _order_ids(conn, pid)
    k = _resolve_index(remaining, op.after_track_id, op.position, op_index=op_index)
    keys = _keys_for(conn, pid, remaining, k, len(prepared))
    rows, ids = [], []
    for (item, _meta, prov, prov_kind, start, end, display), key in zip(prepared, keys):
        row = {
            "id": None, "playlist_id": pid, "video_id": item.video_id, "added_by_user_id": ctx.actor.user_id,
            "track_note": (item.note or "").strip(), "start_seconds": start, "end_seconds": end,
            "display_title": display, "created_at": ctx.ts, **prov,
        }
        if row["migrated_from_video_id"] and not _valid_vid(row["migrated_from_video_id"]):
            row["migrated_from_video_id"] = None
        if row["migrated_from_title"]:
            row["migrated_from_title"] = str(row["migrated_from_title"]).strip()[:500] or None
        tid = _insert_track(conn, row, key)
        row["id"] = tid
        rows.append(row)
        ids.append(tid)
        ctx.provenance[tid] = prov_kind
    ctx.rendered_ids.extend(ids)
    title = _playlist_title(conn, pid)
    eff = _Effect(pid, "insert",
                  {"kind": "insert", "playlist_id": pid, "track_ids": ids, "rows": rows,
                   "after_track_id": remaining[k - 1] if k > 0 else None, "index": k,
                   "label": f"Added {_plural(len(ids))} to '{title}'"},
                  {"kind": "delete", "playlist_id": pid, "track_ids": list(reversed(ids)),
                   "expect": {str(r["id"]): r["video_id"] for r in rows}})
    ctx.effects.append(eff)
    _post_effect(ctx, eff)
    result["track_ids"] = ids
    return result


def _valid_vid(v: Any) -> bool:
    return isinstance(v, str) and re.fullmatch(VIDEO_ID_REGEX[1:-1], v) is not None


def _rows_in_playlist(conn: sqlite3.Connection, pid: int, track_ids: List[int], op_index: int) -> List[sqlite3.Row]:
    if len(set(track_ids)) != len(track_ids):
        raise ApiError(400, "bad_op", "The same track is listed twice.", op_index=op_index)
    ordered = _ordered(conn, pid)
    by_id = {r["id"]: r for r in ordered}
    for tid in track_ids:
        if tid not in by_id:
            raise ApiError(404, "track_not_found", "Track not found in playlist", op_index=op_index, track_id=tid)
    return ordered


def _remove_sequence(ordered_ids: List[int], track_ids: List[int]) -> List[Tuple[int, Optional[int], int]]:
    """Remove ids one at a time; record (id, prev at that moment, index at that moment)."""
    current = list(ordered_ids)
    records = []
    for tid in track_ids:
        idx = current.index(tid)
        records.append((tid, current[idx - 1] if idx > 0 else None, idx))
        current.pop(idx)
    return records


def _delete_effect(ctx: _Batch, pid: int, track_ids: List[int], ordered: List[sqlite3.Row], via: Optional[str],
                   label: str) -> _Effect:
    conn = ctx.conn
    by_id = {r["id"]: r for r in ordered}
    records = _remove_sequence([r["id"] for r in ordered], track_ids)
    restore = [{**_snapshot(by_id[tid]), "after_track_id": prev, "index": idx} for tid, prev, idx in records]
    conn.execute(f"DELETE FROM playlist_tracks WHERE id IN ({','.join('?' * len(track_ids))})", track_ids)
    fwd = {"kind": "delete", "playlist_id": pid, "track_ids": list(track_ids),
           "expect": {str(t): by_id[t]["video_id"] for t in track_ids}, "label": label}
    if via:
        fwd["via"] = via
    eff = _Effect(pid, "delete", fwd, {"kind": "restore", "playlist_id": pid, "rows": list(reversed(restore))})
    ctx.effects.append(eff)
    _post_effect(ctx, eff)
    return eff


def _apply_delete(ctx: _Batch, op: DeleteOp, op_index: int) -> Dict[str, Any]:
    ordered = _rows_in_playlist(ctx.conn, op.playlist_id, op.track_ids, op_index)
    title = _playlist_title(ctx.conn, op.playlist_id)
    _delete_effect(ctx, op.playlist_id, list(op.track_ids), ordered, None,
                   f"Removed {_plural(len(op.track_ids))} from '{title}'")
    return {"kind": "delete", "playlist_id": op.playlist_id, "track_ids": list(op.track_ids)}


def _place_block(conn: sqlite3.Connection, pid: int, block: List[int], remaining: List[int], k: int) -> None:
    keys = _keys_for(conn, pid, remaining, k, len(block))
    for tid, key in zip(block, keys):
        conn.execute("UPDATE playlist_tracks SET position = ? WHERE id = ?", (key, tid))


def _apply_move(ctx: _Batch, op: MoveOp, op_index: int) -> Dict[str, Any]:
    conn = ctx.conn
    src, dst = op.playlist_id, op.to_playlist_id or op.playlist_id
    ordered = _rows_in_playlist(conn, src, op.track_ids, op_index)
    wanted = set(op.track_ids)
    block = [r["id"] for r in ordered if r["id"] in wanted]  # keeps current relative order

    if src == dst:
        order = [r["id"] for r in ordered]
        remaining = [t for t in order if t not in wanted]
        k = _resolve_index(remaining, op.after_track_id, op.position, block, op_index=op_index)
        result = {"kind": "move", "playlist_id": src, "to_playlist_id": dst,
                  "moved": [{"from_track_id": t, "track_id": t} for t in block]}
        if remaining[:k] + block + remaining[k:] == order:
            return result  # already there: no effect, no undo entry
        records = _remove_sequence(order, block)
        _place_block(conn, src, block, remaining, k)
        title = _playlist_title(conn, src)
        eff = _Effect(src, "move",
                      {"kind": "move", "playlist_id": src, "track_ids": block,
                       "after_track_id": remaining[k - 1] if k > 0 else None, "index": k,
                       "label": f"Moved {_plural(len(block))} in '{title}'"},
                      {"kind": "move_each", "playlist_id": src,
                       "moves": [{"track_id": t, "after_track_id": prev, "index": idx}
                                 for t, prev, idx in reversed(records)]})
        ctx.effects.append(eff)
        _post_effect(ctx, eff)
        return result

    # Across playlists: a delete on the source and an insert on the destination.
    dst_count = conn.execute("SELECT COUNT(*) FROM playlist_tracks WHERE playlist_id = ?", (dst,)).fetchone()[0]
    if dst_count + len(block) > MAX_TRACKS_PER_PLAYLIST:
        raise ApiError(400, "playlist_full", f"Playlists can hold at most {MAX_TRACKS_PER_PLAYLIST} tracks.",
                       op_index=op_index, playlist_id=dst)
    dst_remaining = _order_ids(conn, dst)
    k = _resolve_index(dst_remaining, op.after_track_id, op.position, block, op_index=op_index)
    by_id = {r["id"]: r for r in ordered}
    label = f"Moved {_plural(len(block))} to '{_playlist_title(conn, dst)}'"
    copies = []
    for tid in block:
        snap = _snapshot(by_id[tid])
        snap.update({"id": None, "playlist_id": dst, "created_at": ctx.ts})
        copies.append(snap)
    _delete_effect(ctx, src, block, ordered, "move", label)
    copies = [_sanitize_refs(conn, c) for c in copies]
    keys = _keys_for(conn, dst, dst_remaining, k, len(copies))
    new_ids = []
    for c, key in zip(copies, keys):
        c["id"] = _insert_track(conn, c, key)
        new_ids.append(c["id"])
        ctx.provenance[c["id"]] = "kept"
    ctx.rendered_ids.extend(new_ids)
    eff = _Effect(dst, "insert",
                  {"kind": "insert", "playlist_id": dst, "track_ids": new_ids, "rows": copies, "via": "move",
                   "from_track_ids": block, "after_track_id": dst_remaining[k - 1] if k > 0 else None,
                   "index": k, "label": label},
                  {"kind": "delete", "playlist_id": dst, "track_ids": list(reversed(new_ids)),
                   "expect": {str(c["id"]): c["video_id"] for c in copies}})
    ctx.effects.append(eff)
    _post_effect(ctx, eff)
    return {"kind": "move", "playlist_id": src, "to_playlist_id": dst,
            "moved": [{"from_track_id": a, "track_id": b} for a, b in zip(block, new_ids)]}


def _set_order(conn: sqlite3.Connection, pid: int, ids: List[int]) -> None:
    for i, tid in enumerate(ids, start=1):
        conn.execute("UPDATE playlist_tracks SET position = ? WHERE id = ?", (float(i), tid))


def _apply_reorder(ctx: _Batch, op: ReorderOp, op_index: int) -> Dict[str, Any]:
    conn, pid = ctx.conn, op.playlist_id
    current = _order_ids(conn, pid)
    wanted = [int(t) for t in op.track_ids]
    if len(set(wanted)) != len(wanted) or set(wanted) != set(current):
        raise ApiError(400, "bad_op", "A reorder must list every track of the playlist exactly once.",
                       op_index=op_index)
    result = {"kind": "reorder", "playlist_id": pid, "track_ids": wanted}
    if wanted == current:
        return result  # no effect, no undo entry
    _set_order(conn, pid, wanted)
    eff = _Effect(pid, "reorder",
                  {"kind": "reorder", "playlist_id": pid, "track_ids": wanted,
                   "label": f"Reordered '{_playlist_title(conn, pid)}'"},
                  {"kind": "reorder", "playlist_id": pid, "track_ids": current})
    ctx.effects.append(eff)
    _post_effect(ctx, eff)
    return result


_TRACK_SET_COLUMNS = {"note": "track_note", "start_seconds": "start_seconds", "end_seconds": "end_seconds",
                      "display_title": "display_title", "video_id": "video_id"}


def _apply_update(ctx: _Batch, op: UpdateOp, op_index: int, song_meta: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    conn, pid, tid = ctx.conn, op.playlist_id, op.track_id
    row = conn.execute("SELECT * FROM playlist_tracks WHERE id = ? AND playlist_id = ?", (tid, pid)).fetchone()
    if not row:
        raise ApiError(404, "track_not_found", "Track not found in playlist", op_index=op_index, track_id=tid)
    given = op.set.model_fields_set
    new: Dict[str, Any] = {}
    if "note" in given:
        new["track_note"] = (op.set.note or "").strip()
    if "start_seconds" in given or "end_seconds" in given:
        start, end = _clip(
            op.set.start_seconds if "start_seconds" in given else _num(row["start_seconds"]),
            op.set.end_seconds if "end_seconds" in given else _num(row["end_seconds"]),
            op_index=op_index,
        )
        new["start_seconds"], new["end_seconds"] = start, end

    cur_song = _song_row(conn, row["video_id"])
    final_title = cur_song["title"] if cur_song else ""
    if "video_id" in given and op.set.video_id and op.set.video_id != row["video_id"]:
        new_vid = op.set.video_id
        song = _song_row(conn, new_vid)
        if song is None:
            meta = song_meta.get(new_vid)
            if not meta or meta.get("error") or not meta.get("title"):
                raise lookup_error((meta or {}).get("error") or "not_found", op_index=op_index, video_id=new_vid)
            song = _ensure_song(conn, new_vid, meta, ctx.ts)
        final_title = song["title"]
        current_row_title = row["display_title"] or (cur_song["title"] if cur_song else "")
        cur_channel = cur_song["channel_title"] if cur_song else ""
        orig_vid, orig_title = row["migrated_from_video_id"], row["migrated_from_title"]
        if orig_vid and new_vid == orig_vid:
            orig_vid, orig_title = None, None
        elif not orig_vid:
            orig_vid = row["video_id"]
            orig_title = current_row_title + (
                f" ({cur_channel})" if cur_channel and cur_channel not in ("YouTube", "YouTube Artist") else ""
            )
        # Grab credit only survives if the source playlist (visible to the actor) has the new video.
        src_pid, src_uid = None, None
        if row["source_playlist_id"]:
            src = verified_source(conn, row["source_playlist_id"], new_vid, ctx.actor.user_id)
            if src:
                src_pid, src_uid = src
        new.update({"video_id": new_vid, "migrated_from_video_id": orig_vid, "migrated_from_title": orig_title,
                    "source_playlist_id": src_pid, "source_user_id": src_uid, "source_track_id": None})
    if "display_title" in given:
        new["display_title"] = clean_display_title(op.set.display_title, final_title)

    changed_cols = {c: v for c, v in new.items() if not _same(row[c], v)}
    changed = [k for k, c in _TRACK_SET_COLUMNS.items() if c in changed_cols]
    result = {"kind": "update", "playlist_id": pid, "track_id": tid, "changed": changed}
    ctx.rendered_ids.append(tid)
    if not changed_cols:
        return result
    old = {c: row[c] for c in changed_cols}
    conn.execute(
        f"UPDATE playlist_tracks SET {', '.join(f'{c} = ?' for c in changed_cols)} WHERE id = ?",
        [*changed_cols.values(), tid],
    )
    eff = _Effect(pid, "update",
                  {"kind": "update", "playlist_id": pid, "track_id": tid, "set": changed_cols, "old": old,
                   "label": ("Swapped a track" if "video_id" in changed_cols else "Edited a track")
                   + f" in '{_playlist_title(conn, pid)}'"},
                  {"kind": "update", "playlist_id": pid, "track_id": tid, "set": old, "expect": changed_cols})
    ctx.effects.append(eff)
    _post_effect(ctx, eff)
    return result


def _apply_set_meta(ctx: _Batch, op: SetMetaOp, op_index: int) -> Dict[str, Any]:
    conn, pid = ctx.conn, op.playlist_id
    pl = ctx.playlist(pid)
    given = op.set.model_fields_set
    new: Dict[str, Any] = {}
    if "title" in given:
        title = (op.set.title or "").strip()
        if not title:
            raise ApiError(400, "bad_op", "Playlist title is required", op_index=op_index)
        new["title"] = title
    if "description" in given:
        new["description"] = (op.set.description or "").strip()
    if "visibility" in given and op.set.visibility is not None:
        if pl["kind"] == "crate":
            raise ApiError(400, "crate_visibility_fixed", "Your crate is always private.", op_index=op_index)
        new["visibility"] = op.set.visibility
    changed = {c: v for c, v in new.items() if pl[c] != v}
    result = {"kind": "set_meta", "playlist_id": pid, "changed": list(changed)}
    if not changed:
        return result
    old = {c: pl[c] for c in changed}
    conn.execute(f"UPDATE playlists SET {', '.join(f'{c} = ?' for c in changed)} WHERE id = ?", [*changed.values(), pid])
    label = f"Made '{changed.get('title', pl['title'])}' {changed['visibility']}" if "visibility" in changed else \
        f"Edited '{changed.get('title', pl['title'])}'"
    eff = _Effect(pid, "set_meta", {"kind": "set_meta", "playlist_id": pid, "set": changed, "old": old, "label": label},
                  {"kind": "set_meta", "playlist_id": pid, "set": old, "expect": changed})
    ctx.effects.append(eff)
    _post_effect(ctx, eff)
    return result


def _effect_summary(fwd: Dict[str, Any]) -> Optional[Tuple[str, int, int]]:
    """(verb, playlist id, track count) for the group label; None for the source half of a move."""
    kind, pid = fwd["kind"], int(fwd["playlist_id"])
    n = len(fwd.get("track_ids") or [])
    if kind == "insert":
        return ("moved_to" if fwd.get("via") == "move" else "added", pid, n)
    if kind == "delete":
        return None if fwd.get("via") == "move" else ("removed", pid, n)
    if kind == "move":
        return ("moved_in", pid, n)
    if kind == "update":
        return ("swapped" if "video_id" in (fwd.get("set") or {}) else "edited", pid, 1)
    return (kind, pid, 1)  # set_meta, reorder


_LABEL_TEMPLATES = {
    "added": "Added {n} to {where}", "removed": "Removed {n} from {where}", "moved_to": "Moved {n} to {where}",
    "moved_in": "Moved {n} in {where}", "swapped": "Swapped {n} in {where}", "edited": "Edited {n} in {where}",
}


def _compose_label(conn: sqlite3.Connection, fwds: List[Dict[str, Any]]) -> Optional[str]:
    """One undo label for a whole group: it must describe everything one undo reverts."""
    if not fwds:
        return None
    own = [f.get("effect_label") or f.get("label") for f in fwds]
    parts = [(s, lbl) for s, lbl in ((_effect_summary(f), lbl) for f, lbl in zip(fwds, own)) if s]
    if not parts:
        return own[0]
    if len(parts) == 1:
        return parts[0][1]
    verbs = {s[0] for s, _ in parts}
    pids = list(dict.fromkeys(s[1] for s, _ in parts))
    verb = next(iter(verbs))
    if len(verbs) == 1 and verb in _LABEL_TEMPLATES:
        n = sum(s[2] for s, _ in parts)
        count = "a track" if n == 1 else _plural(n)
        where = f"'{_playlist_title(conn, pids[0])}'" if len(pids) == 1 else f"{len(pids)} playlists"
        return _LABEL_TEMPLATES[verb].format(n=count, where=where)
    if len(pids) == 1:
        return f"Changed '{_playlist_title(conn, pids[0])}'"
    return f"Changed {len(pids)} playlists"


def _request_hash(group_id: Optional[str], ops: List[Any], expected_versions: Optional[Dict[int, int]]) -> str:
    payload = {
        "group_id": group_id,
        "ops": [o.model_dump(mode="json", by_alias=True, exclude_unset=True) for o in ops],
        "expected_versions": {str(k): v for k, v in sorted((expected_versions or {}).items())},
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _count_items(ops: List[Any]) -> int:
    n = 0
    for op in ops:
        if op.kind == "insert":
            n += len(op.items)
        elif op.kind in ("delete", "move"):
            n += len(op.track_ids)
        else:
            n += 1
    return n


def _touched_ids(ops: List[Any]) -> List[int]:
    pids: List[int] = []
    for op in ops:
        pids.append(op.playlist_id)
        if op.kind == "move" and op.to_playlist_id:
            pids.append(op.to_playlist_id)
    return list(dict.fromkeys(pids))


def _upsert_dock(conn: sqlite3.Connection, user_id: int, pids: Iterable[int], ts: str) -> None:
    for pid in pids:
        pl = conn.execute("SELECT user_id FROM playlists WHERE id = ?", (pid,)).fetchone()
        if not pl or pl["user_id"] != user_id:
            continue
        conn.execute(
            """
            INSERT INTO user_dock (user_id, playlist_id, pinned, sort, last_used_at) VALUES (?, ?, 0, NULL, ?)
            ON CONFLICT(user_id, playlist_id) DO UPDATE SET last_used_at = excluded.last_used_at
            """,
            (user_id, pid, ts),
        )
    conn.execute(
        """
        DELETE FROM user_dock WHERE user_id = ? AND pinned = 0 AND playlist_id NOT IN (
            SELECT playlist_id FROM user_dock WHERE user_id = ? AND pinned = 0
            ORDER BY last_used_at DESC LIMIT ?
        )
        """,
        (user_id, user_id, DOCK_RECENT_KEEP),
    )


def touch_dock(conn: sqlite3.Connection, user_id: int, pids: Iterable[int]) -> None:
    _upsert_dock(conn, user_id, pids, now_str())


def _retention(conn: sqlite3.Connection, user_id: int, ts: str) -> None:
    groups = conn.execute(
        """
        SELECT group_id, MIN(id) AS first_id FROM playlist_ops
        WHERE user_id = ? AND discarded_at IS NULL GROUP BY group_id ORDER BY first_id DESC
        """,
        (user_id,),
    ).fetchall()
    for g in groups[UNDO_GROUP_LIMIT:]:
        conn.execute("UPDATE playlist_ops SET discarded_at = ? WHERE user_id = ? AND group_id = ? AND discarded_at IS NULL",
                     (ts, user_id, g["group_id"]))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=OP_RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("DELETE FROM playlist_ops WHERE user_id = ? AND discarded_at IS NOT NULL AND created_at < ?",
                 (user_id, cutoff))


def badge_group_key(user_id: int, group_id: str) -> str:
    """user_badges.op_group_key / activity_log.op_group_id of badges an undo group earned.

    ':' can't occur in an op id, so these never collide with plain group ids.
    """
    return f"{int(user_id)}:{group_id}"


def _evaluate_badges(conn: sqlite3.Connection, actor: Actor, credited: Set[int], finders: Set[int],
                     group_key: Optional[str] = None) -> List[Dict[str, Any]]:
    new_badges = evaluate_and_award_badges(conn, actor.user_id, op_group_key=group_key)
    for uid in (credited | finders) - {actor.user_id}:
        if conn.execute("SELECT 1 FROM users WHERE id = ?", (uid,)).fetchone():
            evaluate_and_award_badges(conn, uid, op_group_key=group_key)
    return new_badges


def _revoke_group_badges(conn: sqlite3.Connection, group_key: str) -> List[int]:
    """Inside the undo transaction: drop the badges (and their feed lines) a group earned."""
    users = [int(r[0]) for r in conn.execute(
        "SELECT DISTINCT user_id FROM user_badges WHERE op_group_key = ?", (group_key,))]
    conn.execute("DELETE FROM user_badges WHERE op_group_key = ?", (group_key,))
    conn.execute("DELETE FROM activity_log WHERE action_type = 'earned_badge' AND op_group_id = ?", (group_key,))
    return users


def _replay(conn: sqlite3.Connection, actor: Actor, rows: List[sqlite3.Row]) -> OpsOutcome:
    first = json.loads(rows[0]["forward_json"])
    batch = first.get("batch") or {}
    results = batch.get("results") or []
    pids = batch.get("playlists") or list(dict.fromkeys(r["playlist_id"] for r in rows))
    tids = [int(t) for t in (batch.get("provenance") or {})] + list(batch.get("updated") or [])
    rendered = _render_rows(conn, actor.user_id, tids)
    for tid, prov in (batch.get("provenance") or {}).items():
        if int(tid) in rendered:
            rendered[int(tid)]["provenance"] = prov
    return OpsOutcome(
        group_id=rows[0]["group_id"], replayed=True, results=results, versions=_versions(conn, pids),
        order={int(p): _order_ids(conn, int(p)) for p in pids if _versions(conn, [p])},
        rows=rendered, skipped=batch.get("skipped") or [], new_badges=[],
        claimed=set(batch.get("claimed") or []), label=first.get("label"),
    )


def check_batch_shape(ops: List[Any], max_items: int = MAX_ENTRIES_PER_REQUEST) -> List[int]:
    """The request caps, before any DB or network work. Returns the touched playlist ids.

    Server-composed batches (max_items > 50: promote, fork, import) may use one op per item.
    """
    max_ops = MAX_OPS_PER_BATCH if max_items <= MAX_ENTRIES_PER_REQUEST else max_items
    if not ops:
        raise ApiError(400, "bad_op", "No ops to apply.")
    if len(ops) > max_ops:
        raise ApiError(400, "batch_too_large", f"Send at most {max_ops} ops at a time.")
    if _count_items(ops) > max_items:
        raise ApiError(400, "batch_too_large", f"Change at most {max_items} items at a time.")
    touched = _touched_ids(ops)
    if len(touched) > MAX_PLAYLISTS_PER_BATCH:
        raise ApiError(400, "batch_too_large", f"Touch at most {MAX_PLAYLISTS_PER_BATCH} playlists at a time.")
    return touched


def authorise_batch(conn: sqlite3.Connection, actor: Actor, ops: List[Any]) -> None:
    """Role check for every op (read-only; also run before any YouTube lookups)."""
    for i, op in enumerate(ops):
        require_playlist_role(conn, op.playlist_id, actor, OP_MIN_ROLE[op.kind], op_index=i)
        if op.kind == "move" and op.to_playlist_id and op.to_playlist_id != op.playlist_id:
            require_playlist_role(conn, op.to_playlist_id, actor, OP_MIN_ROLE["move"], op_index=i)


def apply_ops(
    conn: sqlite3.Connection,
    actor: Actor,
    ops: List[Any],
    *,
    batch_id: str,
    group_id: Optional[str] = None,
    expected_versions: Optional[Dict[int, int]] = None,
    song_meta: Optional[Dict[str, Dict[str, Any]]] = None,
    skip_unavailable: bool = False,
    max_items: int = MAX_ENTRIES_PER_REQUEST,
    undoable: bool = True,
    origin: Optional[str] = None,
) -> OpsOutcome:
    """Apply a batch in one transaction. Joins the caller's transaction if one is open.

    Commits on success (badges are evaluated after the commit) and rolls back on any error.
    `origin` ("fork" / "promote") is set only by the server-side composers, never from a request.
    """
    explicit_group = group_id is not None
    group_id = group_id or batch_id
    song_meta = song_meta or {}
    # 1. Shape caps (Pydantic already checked kinds and fields).
    touched = check_batch_shape(ops, max_items)
    req_hash = _request_hash(group_id if explicit_group else None, ops, expected_versions)
    ts = now_str()

    _begin(conn)
    try:
        # 2. Idempotency (inside the write lock, so a racing retry can't double-apply).
        prior = conn.execute("SELECT * FROM playlist_ops WHERE batch_id = ? ORDER BY id", (batch_id,)).fetchall()
        if prior:
            if any(r["user_id"] != actor.user_id for r in prior) or prior[0]["request_hash"] != req_hash:
                raise ApiError(409, "op_id_conflict", "That op_id was already used for a different change.")
            outcome = _replay(conn, actor, prior)
            conn.rollback()
            return outcome

        # 4. Load and authorise.
        authorise_batch(conn, actor, ops)

        # 5. Versions.
        if expected_versions:
            conflicts, current = [], {}
            for pid, v in expected_versions.items():
                pl = conn.execute("SELECT * FROM playlists WHERE id = ?", (int(pid),)).fetchone()
                if not is_visible(pl, actor.user_id):
                    raise ApiError(404, "playlist_not_found", "Playlist not found")
                current[int(pid)] = int(pl["version"])
                if int(pl["version"]) != int(v):
                    conflicts.append({"playlist_id": int(pid), "expected": int(v), "current": int(pl["version"])})
            if conflicts:
                first_pid = conflicts[0]["playlist_id"]
                raise ApiError(409, "stale_version", f"Playlist {first_pid} changed since you loaded it.",
                               conflicts=conflicts, versions={str(k): v for k, v in current.items()})

        # 6. Group check: a reused group must still be the top of the user's live stack.
        joined: List[sqlite3.Row] = []
        if explicit_group:
            exists = conn.execute("SELECT 1 FROM playlist_ops WHERE user_id = ? AND group_id = ? LIMIT 1",
                                  (actor.user_id, group_id)).fetchone()
            if exists and _top_undo_group(conn, actor.user_id) != group_id:
                raise ApiError(409, "group_closed", "That undo group is closed; start a new one.")
            if exists:
                joined = _group_rows(conn, actor.user_id, group_id, undone=False)

        # 7. Apply in order.
        ctx = _Batch(conn, actor, group_id, ts, origin)
        results: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []
        for i, op in enumerate(ops):
            if op.kind == "insert":
                results.append(_apply_insert(ctx, op, i, song_meta, skip_unavailable, skipped))
            elif op.kind == "delete":
                results.append(_apply_delete(ctx, op, i))
            elif op.kind == "move":
                results.append(_apply_move(ctx, op, i))
            elif op.kind == "update":
                results.append(_apply_update(ctx, op, i, song_meta))
            elif op.kind == "reorder":
                results.append(_apply_reorder(ctx, op, i))
            else:
                results.append(_apply_set_meta(ctx, op, i))

        # 8. Op rows. The label describes the whole group (this batch plus any batch it joins).
        effect_pids = list(dict.fromkeys(e.playlist_id for e in ctx.effects))
        for eff in ctx.effects:
            eff.forward["effect_label"] = eff.forward.get("label")
            if origin:
                eff.forward["origin"] = origin
        label = None
        if ctx.effects:
            joined_fwds = [json.loads(r["forward_json"]) for r in joined]
            label = _compose_label(conn, joined_fwds + [e.forward for e in ctx.effects])
            for r, f in zip(joined, joined_fwds):
                if f.get("label") != label:
                    f["label"] = label
                    conn.execute("UPDATE playlist_ops SET forward_json = ? WHERE id = ?", (json.dumps(f), r["id"]))
        for n, eff in enumerate(ctx.effects):
            fwd = dict(eff.forward)
            fwd["label"] = label
            if n == 0:
                fwd["batch"] = {
                    "results": results, "skipped": skipped, "playlists": touched,
                    "provenance": {str(k): v for k, v in ctx.provenance.items()},
                    "updated": [r["track_id"] for r in results if r["kind"] == "update"],
                    "claimed": sorted(ctx.claimed),
                }
            conn.execute(
                """
                INSERT INTO playlist_ops (op_id, batch_id, playlist_id, user_id, group_id, kind, forward_json,
                                          inverse_json, request_hash, created_at, undone_at, discarded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (f"{batch_id}#{n}", batch_id, eff.playlist_id, actor.user_id, group_id, eff.kind,
                 json.dumps(fwd), json.dumps(eff.inverse), req_hash, ts, None if undoable else ts),
            )
        # 9. Versions.
        for pid in effect_pids:
            conn.execute("UPDATE playlists SET version = version + 1, updated_at = ? WHERE id = ?", (ts, pid))
        # 10. A new action drops the redo branch.
        if ctx.effects and undoable:
            conn.execute(
                "UPDATE playlist_ops SET discarded_at = ? WHERE user_id = ? AND undone_at IS NOT NULL AND discarded_at IS NULL",
                (ts, actor.user_id),
            )
            _retention(conn, actor.user_id, ts)
        # 12. Dock.
        if ctx.effects:
            _upsert_dock(conn, actor.user_id, effect_pids, ts)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise

    # 14. Badges, only when a LISTED playlist was involved. Badges an undoable group earns are
    # tagged with it, so undoing the group revokes them (paste-then-undo can't farm badges).
    new_badges: List[Dict[str, Any]] = []
    if ctx.effects and any(is_listed(ctx.playlist(p)) for p in effect_pids):
        new_badges = _evaluate_badges(conn, actor, ctx.credited_users, ctx.finders,
                                      badge_group_key(actor.user_id, group_id) if undoable else None)

    rendered = _render_rows(conn, actor.user_id, ctx.rendered_ids)
    for tid, prov in ctx.provenance.items():
        if tid in rendered:
            rendered[tid]["provenance"] = prov
    return OpsOutcome(
        group_id=group_id, replayed=False, results=results, versions=_versions(conn, touched),
        order={pid: _order_ids(conn, pid) for pid in touched}, rows=rendered, skipped=skipped,
        new_badges=new_badges, claimed=ctx.claimed, label=label,
    )


# --- Undo / redo: internal op forms --------------------------------------------------------------

class _Conflict(Exception):
    def __init__(self, playlist_id: int, track_id: Optional[int] = None, reason: str = "") -> None:
        super().__init__(reason)
        self.playlist_id = playlist_id
        self.track_id = track_id


def _place_rows(conn: sqlite3.Connection, pid: int, rows: List[Dict[str, Any]], after: Optional[int], index: int) -> List[int]:
    """Insert rows (with their original ids) as a block after `after`, else at `index` (clamped)."""
    for r in rows:
        if conn.execute("SELECT 1 FROM playlist_tracks WHERE id = ?", (r["id"],)).fetchone():
            raise _Conflict(pid, r["id"], "row exists")
    count = conn.execute("SELECT COUNT(*) FROM playlist_tracks WHERE playlist_id = ?", (pid,)).fetchone()[0]
    if count + len(rows) > MAX_TRACKS_PER_PLAYLIST:
        raise _Conflict(pid, None, "playlist full")
    remaining = _order_ids(conn, pid)
    if after is not None and after in remaining:
        k = remaining.index(after) + 1
    elif after is None:
        k = 0
    else:
        k = min(max(int(index or 0), 0), len(remaining))
    keys = _keys_for(conn, pid, remaining, k, len(rows))
    ids = []
    for r, key in zip(rows, keys):
        clean = _sanitize_refs(conn, {c: r.get(c) for c in SNAPSHOT_COLS})
        clean["playlist_id"] = pid
        ids.append(_insert_track(conn, clean, key))
    return ids


def _move_block(conn: sqlite3.Connection, pid: int, block: List[int], after: Optional[int], index: int) -> None:
    order = _order_ids(conn, pid)
    for t in block:
        if t not in order:
            raise _Conflict(pid, t, "track gone")
    remaining = [t for t in order if t not in set(block)]
    if after is not None and after in remaining:
        k = remaining.index(after) + 1
    elif after is None:
        k = 0
    else:
        k = min(max(int(index or 0), 0), len(remaining))
    _place_block(conn, pid, block, remaining, k)


def _exec_internal(conn: sqlite3.Connection, op: Dict[str, Any], redo_rows: bool = False) -> Dict[str, Any]:
    kind, pid = op["kind"], int(op["playlist_id"])
    if kind == "delete":
        ids = [int(t) for t in op["track_ids"]]
        for t in ids:
            r = conn.execute("SELECT video_id FROM playlist_tracks WHERE id = ? AND playlist_id = ?", (t, pid)).fetchone()
            if not r or (op.get("expect") and op["expect"].get(str(t)) not in (None, r["video_id"])):
                raise _Conflict(pid, t, "row changed")
        conn.execute(f"DELETE FROM playlist_tracks WHERE id IN ({','.join('?' * len(ids))})", ids)
        return {"kind": "delete", "playlist_id": pid, "track_ids": ids}
    if kind == "restore":
        ids = []
        for r in op["rows"]:
            ids.extend(_place_rows(conn, pid, [r], r.get("after_track_id"), r.get("index", 0)))
        return {"kind": "restore", "playlist_id": pid, "track_ids": ids}
    if kind == "insert":  # redo of an insert: same ids, same block placement
        ids = _place_rows(conn, pid, op["rows"], op.get("after_track_id"), op.get("index", 0))
        return {"kind": "insert", "playlist_id": pid, "track_ids": ids}
    if kind == "move":  # redo of a move within a playlist
        _move_block(conn, pid, [int(t) for t in op["track_ids"]], op.get("after_track_id"), op.get("index", 0))
        return {"kind": "move", "playlist_id": pid, "track_ids": [int(t) for t in op["track_ids"]]}
    if kind == "move_each":
        ids = []
        for m in op["moves"]:
            _move_block(conn, pid, [int(m["track_id"])], m.get("after_track_id"), m.get("index", 0))
            ids.append(int(m["track_id"]))
        return {"kind": "move_each", "playlist_id": pid, "track_ids": ids}
    if kind == "reorder":
        ids = [int(t) for t in op["track_ids"]]
        current = _order_ids(conn, pid)
        present = set(current)
        for t in ids:
            if t not in present:
                raise _Conflict(pid, t, "track gone")
        listed_ids = set(ids)
        _set_order(conn, pid, ids + [t for t in current if t not in listed_ids])
        return {"kind": "reorder", "playlist_id": pid, "track_ids": ids}
    if kind == "update":
        tid = int(op["track_id"])
        row = conn.execute("SELECT * FROM playlist_tracks WHERE id = ? AND playlist_id = ?", (tid, pid)).fetchone()
        if not row:
            raise _Conflict(pid, tid, "track gone")
        # An FK may have nulled an expected source_* id since (a deleted playlist): compare like for like.
        for c, v in _sanitize_refs(conn, dict(op.get("expect") or {})).items():
            if not _same(row[c], v):
                raise _Conflict(pid, tid, f"{c} changed")
        values = _sanitize_refs(conn, dict(op["set"]))
        if values.get("video_id") and not _song_row(conn, values["video_id"]):
            raise _Conflict(pid, tid, "song gone")
        conn.execute(f"UPDATE playlist_tracks SET {', '.join(f'{c} = ?' for c in values)} WHERE id = ?",
                     [*values.values(), tid])
        return {"kind": "update", "playlist_id": pid, "track_ids": [tid]}
    if kind == "set_meta":
        pl = conn.execute("SELECT * FROM playlists WHERE id = ?", (pid,)).fetchone()
        if not pl:
            raise _Conflict(pid, None, "playlist gone")
        for c, v in (op.get("expect") or {}).items():
            if pl[c] != v:
                raise _Conflict(pid, None, f"{c} changed")
        conn.execute(f"UPDATE playlists SET {', '.join(f'{c} = ?' for c in op['set'])} WHERE id = ?",
                     [*op["set"].values(), pid])
        return {"kind": "set_meta", "playlist_id": pid, "track_ids": []}
    raise ValueError(f"unknown internal op {kind}")


def _forward_for_redo(fwd: Dict[str, Any]) -> Dict[str, Any]:
    kind = fwd["kind"]
    if kind == "update":
        return {"kind": "update", "playlist_id": fwd["playlist_id"], "track_id": fwd["track_id"],
                "set": fwd["set"], "expect": fwd["old"]}
    if kind == "set_meta":
        return {"kind": "set_meta", "playlist_id": fwd["playlist_id"], "set": fwd["set"], "expect": fwd["old"]}
    return fwd


def _touched_keys(fwd: Dict[str, Any]) -> Set[str]:
    if fwd["kind"] == "set_meta":
        return {f"meta:{k}" for k in (fwd.get("set") or {})}
    if fwd["kind"] == "update":
        return {f"t:{fwd['track_id']}"}
    return {f"t:{t}" for t in fwd.get("track_ids") or []}


def _foreign_conflicts(conn: sqlite3.Connection, user_id: int, rows: List[sqlite3.Row]) -> List[Dict[str, Any]]:
    """A later live op by another user that touches the same tracks / meta fields (Phase 5 members)."""
    out = []
    for r in rows:
        mine = _touched_keys(json.loads(r["forward_json"]))
        for later in conn.execute(
            """
            SELECT * FROM playlist_ops WHERE id > ? AND playlist_id = ? AND user_id != ?
              AND undone_at IS NULL AND discarded_at IS NULL
            """,
            (r["id"], r["playlist_id"], user_id),
        ):
            hit = mine & _touched_keys(json.loads(later["forward_json"]))
            if hit:
                key = sorted(hit)[0]
                out.append({"playlist_id": r["playlist_id"],
                            "track_id": int(key[2:]) if key.startswith("t:") else None,
                            "by_user_id": later["user_id"]})
    return out


def _stack_response(conn, user_id, group_id, label, results, touched, rendered_ids, notes, extra=None) -> Dict[str, Any]:
    live = [p for p in touched if _versions(conn, [p])]
    return {
        "group_id": group_id,
        "label": label,
        "results": results,
        "versions": {str(k): v for k, v in _versions(conn, live).items()},
        "order": {str(p): _order_ids(conn, p) for p in live},
        "rows": {str(k): v for k, v in _render_rows(conn, user_id, rendered_ids).items()},
        "notes": notes,
        **(extra or {}),
        "undo": undo_state(conn, user_id),
    }


def _group_rows(conn: sqlite3.Connection, user_id: int, group_id: str, undone: bool) -> List[sqlite3.Row]:
    return conn.execute(
        f"""
        SELECT * FROM playlist_ops WHERE user_id = ? AND group_id = ? AND discarded_at IS NULL
          AND undone_at IS {'NOT ' if undone else ''}NULL
        ORDER BY id ASC
        """,
        (user_id, group_id),
    ).fetchall()


def _require_editor(conn: sqlite3.Connection, actor: Actor, rows: List[sqlite3.Row]) -> None:
    for r in rows:
        pl = conn.execute("SELECT * FROM playlists WHERE id = ?", (r["playlist_id"],)).fetchone()
        role = playlist_role(conn, pl, actor.user_id) if pl else None
        if role is None or ROLE_RANK[role] < ROLE_RANK["editor"]:
            raise _Conflict(r["playlist_id"], None, "no longer allowed")


def _conflict_error(group_id: str, conflicts: List[Dict[str, Any]], what: str = "undo") -> ApiError:
    return ApiError(409, f"{what}_conflict", f"Can't {what}: that playlist was changed afterwards.",
                    group_id=group_id, conflicts=conflicts)


def undo_group(conn: sqlite3.Connection, actor: Actor, expect_group_id: Optional[str] = None,
               skip: bool = False) -> Dict[str, Any]:
    ts = now_str()
    _begin(conn)
    try:
        gid = _top_undo_group(conn, actor.user_id)
        if gid is None:
            conn.rollback()
            return {"group_id": None, "reason": "nothing_to_undo", "undo": undo_state(conn, actor.user_id)}
        if expect_group_id is not None and expect_group_id != gid:
            raise ApiError(409, "stack_moved", "The undo stack changed; nothing was undone.", group_id=gid,
                           label=_group_label(conn, actor.user_id, gid))
        rows = _group_rows(conn, actor.user_id, gid, undone=False)
        label = json.loads(rows[0]["forward_json"]).get("label")
        if skip:
            conn.execute("UPDATE playlist_ops SET discarded_at = ? WHERE user_id = ? AND group_id = ? AND discarded_at IS NULL",
                         (ts, actor.user_id, gid))
            conn.commit()
            return {"group_id": gid, "label": label, "skipped": True, "results": [], "notes": [],
                    "undo": undo_state(conn, actor.user_id)}
        foreign = _foreign_conflicts(conn, actor.user_id, rows)
        if foreign:
            raise _conflict_error(gid, foreign)
        results, rendered = [], []
        try:
            _require_editor(conn, actor, rows)
            for r in reversed(rows):
                res = _exec_internal(conn, json.loads(r["inverse_json"]))
                results.append(res)
                if res["kind"] in ("restore", "update"):
                    rendered.extend(res["track_ids"])
        except _Conflict as c:
            raise _conflict_error(gid, [{"playlist_id": c.playlist_id, "track_id": c.track_id, "by_user_id": None}])
        conn.execute("UPDATE playlist_ops SET undone_at = ? WHERE user_id = ? AND group_id = ? AND discarded_at IS NULL",
                     (ts, actor.user_id, gid))
        touched = list(dict.fromkeys(int(r["playlist_id"]) for r in rows))
        for pid in touched:
            conn.execute("UPDATE playlists SET version = version + 1, updated_at = ? WHERE id = ?", (ts, pid))
        conn.execute("DELETE FROM activity_log WHERE actor_user_id = ? AND op_group_id = ?", (actor.user_id, gid))
        for r in reversed(rows):
            revert_claims(conn, json.loads(r["forward_json"]).get("claims") or [])
        revoked = _revoke_group_badges(conn, badge_group_key(actor.user_id, gid))
        _upsert_dock(conn, actor.user_id, touched, ts)
        notes = []
        if json.loads(rows[0]["forward_json"]).get("origin") == "promote":
            for r in rows:
                fwd = json.loads(r["forward_json"])
                if fwd["kind"] == "insert" and fwd.get("via") == "move" and not _order_ids(conn, r["playlist_id"]):
                    notes.append({"code": "playlist_left_empty", "playlist_id": int(r["playlist_id"])})
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    # A revoked badge whose criteria still hold without the group comes back, silently.
    for uid in revoked:
        evaluate_and_award_badges(conn, uid, record_activity=False)
    return _stack_response(conn, actor.user_id, gid, label, results, touched, rendered, notes)


def redo_group(conn: sqlite3.Connection, actor: Actor, expect_group_id: Optional[str] = None,
               skip: bool = False) -> Dict[str, Any]:
    ts = now_str()
    _begin(conn)
    try:
        gid = _next_redo_group(conn, actor.user_id)
        if gid is None:
            conn.rollback()
            return {"group_id": None, "reason": "nothing_to_redo", "undo": undo_state(conn, actor.user_id)}
        if expect_group_id is not None and expect_group_id != gid:
            raise ApiError(409, "stack_moved", "The redo stack changed; nothing was redone.", group_id=gid,
                           label=_group_label(conn, actor.user_id, gid))
        rows = _group_rows(conn, actor.user_id, gid, undone=True)
        label = json.loads(rows[0]["forward_json"]).get("label")
        if skip:
            conn.execute("UPDATE playlist_ops SET discarded_at = ? WHERE user_id = ? AND group_id = ? AND discarded_at IS NULL",
                         (ts, actor.user_id, gid))
            conn.commit()
            return {"group_id": gid, "label": label, "skipped": True, "results": [], "notes": [],
                    "undo": undo_state(conn, actor.user_id)}
        ctx = _Batch(conn, actor, gid, ts, json.loads(rows[0]["forward_json"]).get("origin"))
        results, rendered = [], []
        try:
            _require_editor(conn, actor, rows)
            for r in rows:
                fwd = json.loads(r["forward_json"])
                res = _exec_internal(conn, _forward_for_redo(fwd))
                results.append(res)
                if res["kind"] in ("insert", "update"):
                    rendered.extend(res["track_ids"])
                eff = _Effect(int(r["playlist_id"]), fwd["kind"], fwd, {})
                _post_effect(ctx, eff)
                conn.execute("UPDATE playlist_ops SET forward_json = ?, undone_at = NULL WHERE id = ?",
                             (json.dumps(fwd), r["id"]))
        except _Conflict as c:
            raise _conflict_error(gid, [{"playlist_id": c.playlist_id, "track_id": c.track_id, "by_user_id": None}],
                                  "redo")
        touched = list(dict.fromkeys(int(r["playlist_id"]) for r in rows))
        for pid in touched:
            conn.execute("UPDATE playlists SET version = version + 1, updated_at = ? WHERE id = ?", (ts, pid))
        _upsert_dock(conn, actor.user_id, touched, ts)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    new_badges: List[Dict[str, Any]] = []
    if any(is_listed(ctx.playlist(p)) for p in touched if ctx.playlist(p)):
        new_badges = _evaluate_badges(conn, actor, ctx.credited_users, ctx.finders, badge_group_key(actor.user_id, gid))
    return _stack_response(conn, actor.user_id, gid, label, results, touched, rendered, [],
                           {"new_badges": new_badges})


# --- Playlist delete, fork, promote ---------------------------------------------------------------

def delete_playlist(conn: sqlite3.Connection, actor: Actor, playlist_id: int) -> Dict[str, Any]:
    ts = now_str()
    _begin(conn)
    try:
        pl = require_playlist_role(conn, playlist_id, actor, "owner")
        if pl["kind"] == "crate":
            raise ApiError(400, "cannot_delete_crate", "Your crate can't be deleted; empty it instead.")
        groups = {(r["user_id"], r["group_id"]) for r in conn.execute(
            "SELECT DISTINCT user_id, group_id FROM playlist_ops WHERE playlist_id = ? AND discarded_at IS NULL",
            (playlist_id,))}
        group_pairs = conn.execute(
            "SELECT DISTINCT user_id, group_id FROM playlist_ops WHERE playlist_id = ?", (playlist_id,)
        ).fetchall()
        for g in group_pairs:
            conn.execute("UPDATE playlist_ops SET discarded_at = ? WHERE user_id = ? AND group_id = ? AND discarded_at IS NULL",
                         (ts, g["user_id"], g["group_id"]))
        conn.execute("DELETE FROM comments WHERE target_type = 'playlist' AND target_id = ?", (str(playlist_id),))
        conn.execute("DELETE FROM activity_log WHERE link_type = 'playlist' AND link_id = ?", (str(playlist_id),))
        conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return {"ok": True, "playlist_id": int(playlist_id), "discarded_undo_groups": len(groups)}


def _create_playlist_row(conn: sqlite3.Connection, user_id: int, title: str, description: str, visibility: str,
                         ts: str, forked_from: Optional[int] = None) -> int:
    cur = conn.execute(
        """
        INSERT INTO playlists (user_id, kind, visibility, version, title, description, plays,
                               forked_from_playlist_id, created_at, updated_at)
        VALUES (?, 'playlist', ?, 0, ?, ?, 0, ?, ?, ?)
        """,
        (user_id, visibility, title, description, forked_from, ts, ts),
    )
    return int(cur.lastrowid)


def create_playlist(conn: sqlite3.Connection, actor: Actor, title: str, description: str = "",
                    visibility: str = "public", activity: Optional[str] = None) -> int:
    """Insert a playlist row (inside the caller's transaction; does not commit) and log its creation if LISTED."""
    ts = now_str()
    _begin(conn)
    pid = _create_playlist_row(conn, actor.user_id, title, description, visibility, ts)
    if visibility == "public":
        _log_activity(conn, actor.user_id, "created_playlist", activity or f"published playlist '{title}'",
                      "playlist", pid, None, ts)
    _upsert_dock(conn, actor.user_id, [pid], ts)
    return pid


def fork_playlist(conn: sqlite3.Connection, actor: Actor, source_id: int, title: Optional[str],
                  visibility: Optional[str] = None) -> Dict[str, Any]:
    src = conn.execute("SELECT * FROM playlists WHERE id = ?", (int(source_id),)).fetchone()
    if not is_visible(src, actor.user_id):
        raise ApiError(404, "playlist_not_found", "Playlist not found")
    if src["kind"] == "crate":
        raise ApiError(400, "use_promote", "Promote your crate instead of forking it.")
    # By default a fork keeps its source's audience: an unlisted or private source (whose title
    # the fork copies) is not published unless the forker asks for public explicitly.
    visibility = visibility or src["visibility"]
    new_title = (title if title is not None else src["title"]).strip()[:MAX_TITLE]
    if not new_title:
        raise ApiError(400, "bad_title", "Playlist title is required")
    owner = conn.execute("SELECT username FROM users WHERE id = ?", (src["user_id"],)).fetchone()
    ts = now_str()
    rows = _ordered(conn, src["id"])
    if len(rows) > MAX_TRACKS_PER_PLAYLIST:
        raise ApiError(400, "playlist_full", f"Playlists can hold at most {MAX_TRACKS_PER_PLAYLIST} tracks.")
    _begin(conn)
    try:
        fid = _create_playlist_row(conn, actor.user_id, new_title, src["description"] or "", visibility, ts,
                                   forked_from=src["id"])
        if visibility == "public":
            if is_listed(src):
                _log_activity(conn, actor.user_id, "forked_playlist",
                              f"forked '{src['title']}' from @{owner['username'] if owner else 'someone'}",
                              "playlist", fid, None, ts)
            else:
                # An unlisted or private source may never be named or linked from a public page (§4.1).
                _log_activity(conn, actor.user_id, "created_playlist", f"published playlist '{new_title}'",
                              "playlist", fid, None, ts)
        _upsert_dock(conn, actor.user_id, [fid], ts)
        new_badges: List[Dict[str, Any]] = []
        if rows:
            items = [{"video_id": r["video_id"], "start_seconds": _num(r["start_seconds"]),
                      "end_seconds": _num(r["end_seconds"]), "note": r["track_note"] or "",
                      "display_title": r["display_title"], "ref": {"track_id": r["id"]}} for r in rows]
            ops = parse_ops([{"kind": "insert", "playlist_id": fid, "items": items}])
            outcome = apply_ops(conn, actor, ops, batch_id=f"fork-{new_ulid()}", max_items=SERVER_MAX_ITEMS,
                                undoable=False, origin="fork")
            new_badges = outcome.new_badges
        else:
            conn.commit()
            if visibility == "public":
                new_badges = evaluate_and_award_badges(conn, actor.user_id)
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    pl = conn.execute("SELECT version FROM playlists WHERE id = ?", (fid,)).fetchone()
    return {
        "playlist_id": fid, "version": int(pl["version"]), "track_count": len(rows),
        "forked_from": {"playlist_id": src["id"], "title": src["title"], "username": owner["username"] if owner else None},
        "new_badges": new_badges,
    }


def promote_crate(conn: sqlite3.Connection, actor: Actor, title: str, description: str, visibility: str,
                  track_ids: Optional[List[int]]) -> Dict[str, Any]:
    crate_id = ensure_crate(conn, actor.user_id)
    conn.commit()
    new_title = (title or "").strip()
    if not new_title or len(new_title) > MAX_TITLE:
        raise ApiError(400, "bad_title", "Give the new playlist a title (up to 200 characters).")
    crate_rows = _ordered(conn, crate_id)
    if not crate_rows:
        raise ApiError(400, "crate_empty", "Your crate is empty.")
    if track_ids is None:
        block = [r["id"] for r in crate_rows]
    else:
        in_crate = {r["id"] for r in crate_rows}
        for t in track_ids:
            if t not in in_crate:
                raise ApiError(404, "track_not_found", "Track not found in your crate", track_id=t)
        wanted = set(track_ids)
        block = [r["id"] for r in crate_rows if r["id"] in wanted]
        if not block:
            raise ApiError(400, "crate_empty", "Pick at least one track to promote.")
    gid = f"promote-{new_ulid()}"
    ts = now_str()
    _begin(conn)
    try:
        pid = _create_playlist_row(conn, actor.user_id, new_title, (description or "").strip(), visibility, ts)
        if visibility == "public":
            _log_activity(conn, actor.user_id, "created_playlist", f"published playlist '{new_title}'",
                          "playlist", pid, None, ts)
        ops = parse_ops([{"kind": "move", "playlist_id": crate_id, "track_ids": block, "to_playlist_id": pid}])
        outcome = apply_ops(conn, actor, ops, batch_id=gid, group_id=gid, max_items=SERVER_MAX_ITEMS,
                            origin="promote")
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    crate = conn.execute("SELECT version FROM playlists WHERE id = ?", (crate_id,)).fetchone()
    count = conn.execute("SELECT COUNT(*) FROM playlist_tracks WHERE playlist_id = ?", (crate_id,)).fetchone()[0]
    return {
        "playlist_id": pid, "group_id": gid, "moved": len(block),
        "versions": {str(k): v for k, v in outcome.versions.items()},
        "crate": {"id": crate_id, "version": int(crate["version"]), "track_count": int(count)},
        "new_badges": outcome.new_badges,
    }


# --- Dock -----------------------------------------------------------------------------------

def _chip(conn: sqlite3.Connection, pl: sqlite3.Row, slot: Optional[int], last_used_at: Optional[str]) -> Dict[str, Any]:
    thumbs = [r["thumbnail_url"] for r in conn.execute(
        """
        SELECT s.thumbnail_url FROM playlist_tracks pt JOIN songs s ON s.video_id = pt.video_id
        WHERE pt.playlist_id = ? ORDER BY pt.position ASC, pt.id ASC LIMIT 4
        """,
        (pl["id"],),
    )]
    count = conn.execute("SELECT COUNT(*) FROM playlist_tracks WHERE playlist_id = ?", (pl["id"],)).fetchone()[0]
    chip = {"slot": slot, "playlist_id": pl["id"], "kind": pl["kind"], "title": pl["title"],
            "visibility": pl["visibility"], "track_count": int(count), "version": int(pl["version"]), "thumbs": thumbs}
    if pl["kind"] != "crate":
        chip["last_used_at"] = last_used_at
    return chip


def dock_view(conn: sqlite3.Connection, user_id: int) -> Dict[str, Any]:
    crate_id = ensure_crate(conn, user_id)
    conn.commit()
    crate = conn.execute("SELECT * FROM playlists WHERE id = ?", (crate_id,)).fetchone()
    pinned_rows = conn.execute(
        """
        SELECT p.*, d.last_used_at AS dock_last_used FROM user_dock d JOIN playlists p ON p.id = d.playlist_id
        WHERE d.user_id = ? AND d.pinned = 1 AND p.user_id = ? AND p.kind = 'playlist'
        ORDER BY d.sort ASC, p.id ASC
        """,
        (user_id, user_id),
    ).fetchall()
    recent_rows = conn.execute(
        """
        SELECT p.*, d.last_used_at AS dock_last_used FROM user_dock d JOIN playlists p ON p.id = d.playlist_id
        WHERE d.user_id = ? AND d.pinned = 0 AND p.user_id = ? AND p.kind = 'playlist' AND d.last_used_at IS NOT NULL
        ORDER BY d.last_used_at DESC, p.id DESC LIMIT ?
        """,
        (user_id, user_id, DOCK_RECENT_SHOWN),
    ).fetchall()
    slot = 1
    pinned, recent = [], []
    for r in pinned_rows:
        pinned.append(_chip(conn, r, slot if slot <= DOCK_MAX_SLOT else None, r["dock_last_used"]))
        slot += 1
    for r in recent_rows:
        recent.append(_chip(conn, r, slot if slot <= DOCK_MAX_SLOT else None, r["dock_last_used"]))
        slot += 1
    return {"crate": _chip(conn, crate, 0, None), "pinned": pinned, "recent": recent}


def set_dock_pins(conn: sqlite3.Connection, user_id: int, pins: List[int]) -> Dict[str, Any]:
    crate_id = ensure_crate(conn, user_id)
    if len(pins) != len(set(pins)):
        raise ApiError(400, "bad_dock", "A playlist is pinned twice.")
    if len(pins) > DOCK_MAX_PINS:
        raise ApiError(400, "bad_dock", f"Pin at most {DOCK_MAX_PINS} playlists.")
    if crate_id in pins:
        raise ApiError(400, "bad_dock", "The crate is always in the dock; don't pin it.")
    for pid in pins:
        pl = conn.execute("SELECT user_id, kind FROM playlists WHERE id = ?", (pid,)).fetchone()
        if not pl or pl["user_id"] != user_id or pl["kind"] != "playlist":
            raise ApiError(404, "playlist_not_found", "Playlist not found", playlist_id=pid)
    _begin(conn)
    try:
        conn.execute("UPDATE user_dock SET pinned = 0, sort = NULL WHERE user_id = ?", (user_id,))
        for i, pid in enumerate(pins, start=1):
            conn.execute(
                """
                INSERT INTO user_dock (user_id, playlist_id, pinned, sort, last_used_at) VALUES (?, ?, 1, ?, NULL)
                ON CONFLICT(user_id, playlist_id) DO UPDATE SET pinned = 1, sort = excluded.sort
                """,
                (user_id, pid, float(i)),
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return dock_view(conn, user_id)


# --- Response helpers ---------------------------------------------------------------------------

def outcome_json(conn: sqlite3.Connection, outcome: OpsOutcome, op_id: str, user_id: int) -> Dict[str, Any]:
    return {
        "op_id": op_id,
        "group_id": outcome.group_id,
        "replayed": outcome.replayed,
        "results": outcome.results,
        "versions": {str(k): v for k, v in outcome.versions.items()},
        "order": {str(k): v for k, v in outcome.order.items()},
        "rows": {str(k): v for k, v in outcome.rows.items()},
        "skipped": outcome.skipped,
        "undo": undo_state(conn, user_id),
        "new_badges": outcome.new_badges,
    }

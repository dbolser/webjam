"""Discovery queries for the Phase 1b shell (docs/phase1b-shell.md §3).

Every function takes `conn` and the viewer (0 = anonymous) and returns plain dicts.
Privacy is spelled only with the app.visibility predicates:

- lists, counts, neighbours, curators, "alongside" and search results name LISTED playlists only;
- the viewer's own playlists (kind = 'playlist', any visibility) may shape results that go to
  that viewer alone (home, "shared with you", the song page's playlist list);
- crates never appear in any of this, except the owner's own `crate` block on /api/me/home
  and the owner's own crate rows making a song searchable for them (song_visible).
"""

import re
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Sequence

from app.ops import crate_id_for, dock_view, undo_state
from app.visibility import linkable, listed, public_song_sql, song_visible_sql

PREVIEW_SIZE = 3
RELATED_SHARED_SHOWN = 4
HOME_NEIGHBOURS = 6
PROFILE_NEIGHBOURS = 8
NEIGHBOUR_POOL = 20  # "my top 20 neighbours" for fresh tracks and activity
NEIGHBOUR_SAMPLE = 3
SHARED_SAMPLE = 5
HOME_FRESH = 10
HOME_ABOUT = 10
HOME_ACTIVITY = 20
HOME_CRATE_RECENT = 5
DISCOVER_ACTIVITY = 15
ALONGSIDE = 10
SEARCH_MAX_QUERY = 100


def _marks(n: int) -> str:
    return ",".join("?" * n)


def user_mini(row: Any, prefix: str = "") -> Optional[Dict[str, Any]]:
    """UserMini from a row holding {prefix}id/username/display_name/avatar_color."""
    uid = row[f"{prefix}id"]
    if uid is None:
        return None
    return {
        "id": int(uid),
        "username": row[f"{prefix}username"],
        "display_name": row[f"{prefix}display_name"],
        "avatar_color": row[f"{prefix}avatar_color"],
    }


def user_minis(conn: sqlite3.Connection, user_ids: Iterable[int]) -> Dict[int, Dict[str, Any]]:
    ids = sorted({int(u) for u in user_ids})
    if not ids:
        return {}
    rows = conn.execute(
        f"SELECT id, username, display_name, avatar_color FROM users WHERE id IN ({_marks(len(ids))})", ids
    ).fetchall()
    return {int(r["id"]): user_mini(r) for r in rows}


def _track_mini(r: Any) -> Dict[str, Any]:
    return {
        "id": int(r["id"]),
        "video_id": r["video_id"],
        "position": int(r["ordinal"]),
        "title": r["title"],
        "channel_title": r["channel_title"],
        "thumbnail_url": r["thumbnail_url"],
        "start_seconds": None if r["start_seconds"] is None else int(r["start_seconds"]),
        "end_seconds": None if r["end_seconds"] is None else int(r["end_seconds"]),
    }


def _ordered_rows_sql(pid_marks: str) -> str:
    """playlist_tracks of the given playlists with their 1-based ordinal (positions are sort keys)."""
    return f"""
        SELECT pt.*, ROW_NUMBER() OVER (PARTITION BY pt.playlist_id ORDER BY pt.position ASC, pt.id ASC) AS ordinal
        FROM playlist_tracks pt WHERE pt.playlist_id IN ({pid_marks})
    """


_TRACK_MINI_COLS = """o.id, o.playlist_id, o.video_id, o.ordinal, o.created_at,
       COALESCE(o.display_title, s.title) AS title, s.channel_title, s.thumbnail_url,
       CAST(o.start_seconds AS INTEGER) AS start_seconds, CAST(o.end_seconds AS INTEGER) AS end_seconds"""


# --- Playlist cards ----------------------------------------------------------------

def related_counts(conn: sqlite3.Connection, playlist_ids: Sequence[int]) -> Dict[int, int]:
    """Number of OTHER listed playlists sharing at least one video_id, per playlist."""
    ids = [int(i) for i in playlist_ids]
    if not ids:
        return {}
    rows = conn.execute(
        f"""
        SELECT a.playlist_id AS pid, COUNT(DISTINCT p2.id) AS n
        FROM playlist_tracks a
        JOIN playlist_tracks b ON b.video_id = a.video_id
        JOIN playlists p2 ON p2.id = b.playlist_id
        WHERE a.playlist_id IN ({_marks(len(ids))}) AND p2.id != a.playlist_id AND {listed('p2')}
        GROUP BY a.playlist_id
        """,
        ids,
    ).fetchall()
    out = {i: 0 for i in ids}
    out.update({int(r["pid"]): int(r["n"]) for r in rows})
    return out


def related_count(conn: sqlite3.Connection, playlist_id: int) -> int:
    return related_counts(conn, [playlist_id])[int(playlist_id)]


def previews(conn: sqlite3.Connection, playlist_ids: Sequence[int]) -> Dict[int, List[Dict[str, Any]]]:
    """Each playlist's 3 best tracks (song likes, then plays, then position), in playlist order."""
    ids = [int(i) for i in playlist_ids]
    if not ids:
        return {}
    rows = conn.execute(
        f"""
        WITH o AS ({_ordered_rows_sql(_marks(len(ids)))}),
        likes AS (SELECT video_id, SUM(count) AS n FROM song_likes
                  WHERE video_id IN (SELECT video_id FROM o) GROUP BY video_id),
        ranked AS (
            SELECT {_TRACK_MINI_COLS},
                   ROW_NUMBER() OVER (PARTITION BY o.playlist_id
                                      ORDER BY COALESCE(l.n, 0) DESC, COALESCE(s.total_plays, 0) DESC, o.ordinal ASC) AS rk
            FROM o JOIN songs s ON s.video_id = o.video_id
            LEFT JOIN likes l ON l.video_id = o.video_id
        )
        SELECT * FROM ranked WHERE rk <= {PREVIEW_SIZE} ORDER BY playlist_id, ordinal
        """,
        ids,
    ).fetchall()
    out: Dict[int, List[Dict[str, Any]]] = {i: [] for i in ids}
    for r in rows:
        out[int(r["playlist_id"])].append(_track_mini(r))
    return out


def playlist_cards(
    conn: sqlite3.Connection, playlist_ids: Sequence[int], preview: bool = True
) -> List[Dict[str, Any]]:
    """PlaylistCards for the given ids, in the given order. Callers decide visibility."""
    ids = [int(i) for i in playlist_ids]
    if not ids:
        return []
    rows = conn.execute(
        f"""
        SELECT p.id, p.kind, p.visibility, p.version, p.title, p.description, p.plays, p.updated_at,
               u.id AS u_id, u.username AS u_username, u.display_name AS u_display_name, u.avatar_color AS u_avatar_color,
               (SELECT COUNT(*) FROM playlist_tracks pt WHERE pt.playlist_id = p.id) AS track_count,
               (SELECT COALESCE(SUM(pl.count), 0) FROM playlist_likes pl WHERE pl.playlist_id = p.id) AS like_count
        FROM playlists p JOIN users u ON u.id = p.user_id
        WHERE p.id IN ({_marks(len(ids))})
        """,
        ids,
    ).fetchall()
    by_id = {int(r["id"]): r for r in rows}
    rel = related_counts(conn, list(by_id))
    prev = previews(conn, list(by_id)) if preview else {}
    cards = []
    for pid in ids:
        r = by_id.get(pid)
        if r is None:
            continue
        card = {
            "id": pid,
            "kind": r["kind"],
            "visibility": r["visibility"],
            "version": int(r["version"]),
            "title": r["title"],
            "description": r["description"] or "",
            "track_count": int(r["track_count"]),
            "like_count": int(r["like_count"]),
            "plays": int(r["plays"] or 0),
            "related_count": rel.get(pid, 0),
            "updated_at": r["updated_at"],
            "user": user_mini(r, "u_"),
        }
        if preview:
            card["preview"] = prev.get(pid, [])
        cards.append(card)
    return cards


# --- Related playlists (§3.5) --------------------------------------------------------

def related_playlists(conn: sqlite3.Connection, playlist_id: int, limit: int, offset: int) -> Dict[str, Any]:
    """Listed playlists sharing tracks with `playlist_id` (the caller has already checked it is visible)."""
    pid = int(playlist_id)
    rows = conn.execute(
        f"""
        WITH mine AS (SELECT DISTINCT video_id FROM playlist_tracks WHERE playlist_id = ?)
        SELECT p2.id, COUNT(DISTINCT pt.video_id) AS shared_count
        FROM playlist_tracks pt JOIN mine m ON m.video_id = pt.video_id
        JOIN playlists p2 ON p2.id = pt.playlist_id
        WHERE p2.id != ? AND {listed('p2')}
        GROUP BY p2.id
        ORDER BY shared_count DESC, p2.updated_at DESC, p2.id DESC
        LIMIT ? OFFSET ?
        """,
        (pid, pid, limit + 1, offset),
    ).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    ids = [int(r["id"]) for r in rows]
    shared: Dict[int, List[Dict[str, Any]]] = {i: [] for i in ids}
    if ids:
        for r in conn.execute(
            f"""
            WITH o AS ({_ordered_rows_sql(_marks(len(ids)))}),
            hits AS (
                SELECT o.playlist_id, o.video_id, o.ordinal,
                       ROW_NUMBER() OVER (PARTITION BY o.playlist_id, o.video_id ORDER BY o.ordinal) AS dup
                FROM o WHERE o.video_id IN (SELECT video_id FROM playlist_tracks WHERE playlist_id = ?)
            ),
            firsts AS (
                SELECT h.*, ROW_NUMBER() OVER (PARTITION BY h.playlist_id ORDER BY h.ordinal) AS n
                FROM hits h WHERE h.dup = 1
            )
            SELECT f.playlist_id, f.video_id, f.ordinal, s.title, s.thumbnail_url
            FROM firsts f JOIN songs s ON s.video_id = f.video_id
            WHERE f.n <= {RELATED_SHARED_SHOWN}
            ORDER BY f.playlist_id, f.ordinal
            """,
            [*ids, pid],
        ):
            shared[int(r["playlist_id"])].append(
                {"video_id": r["video_id"], "title": r["title"], "thumbnail_url": r["thumbnail_url"],
                 "position": int(r["ordinal"])}
            )
    cards = {c["id"]: c for c in playlist_cards(conn, ids, preview=False)}
    return {
        "playlist_id": pid,
        "total": related_count(conn, pid),
        "next_offset": offset + limit if more else None,
        "related": [
            {"playlist": cards[int(r["id"])], "shared_count": int(r["shared_count"]), "shared": shared[int(r["id"])]}
            for r in rows
        ],
    }


# --- Song page extras (§3.6) ----------------------------------------------------------

def listed_playlist_count(conn: sqlite3.Connection, video_id: str) -> int:
    return int(conn.execute(
        f"""SELECT COUNT(DISTINCT pt.playlist_id) FROM playlist_tracks pt JOIN playlists p ON p.id = pt.playlist_id
            WHERE pt.video_id = ? AND {listed('p')}""",
        (video_id,),
    ).fetchone()[0])


def song_extras(conn: sqlite3.Connection, video_id: str, viewer: int) -> Dict[str, Any]:
    viewer = int(viewer or 0)
    # playlists: listed ones, plus the viewer's own kind='playlist' ones (never a crate)
    hit_ids = [int(r[0]) for r in conn.execute(
        f"""SELECT DISTINCT p.id FROM playlist_tracks pt JOIN playlists p ON p.id = pt.playlist_id
            WHERE pt.video_id = ? AND ({listed('p')} OR (p.user_id = ? AND p.kind = 'playlist'))""",
        (video_id, viewer),
    )]
    groups: Dict[int, Dict[str, Any]] = {}
    if hit_ids:
        for r in conn.execute(
            f"""
            WITH o AS ({_ordered_rows_sql(_marks(len(hit_ids)))})
            SELECT o.playlist_id, o.ordinal, o.track_note FROM o WHERE o.video_id = ? ORDER BY o.playlist_id, o.ordinal
            """,
            [*hit_ids, video_id],
        ):
            g = groups.setdefault(int(r["playlist_id"]), {"occurrences": 0, "first_position": int(r["ordinal"]), "note": ""})
            g["occurrences"] += 1
            if not g["note"] and (r["track_note"] or "").strip():
                g["note"] = r["track_note"]
    cards = playlist_cards(conn, hit_ids, preview=False)
    # listed first, then most recently updated (sorts are stable)
    cards.sort(key=lambda c: (c["updated_at"] or "", c["id"]), reverse=True)
    cards.sort(key=lambda c: 0 if (c["kind"] == "playlist" and c["visibility"] == "public") else 1)
    playlists = [{"playlist": c, **groups[c["id"]]} for c in cards]

    # curators: owners of LISTED playlists only
    cur_rows = conn.execute(
        f"""
        SELECT u.id, u.username, u.display_name, u.avatar_color, COUNT(DISTINCT p.id) AS n, MIN(pt.created_at) AS first_at
        FROM playlist_tracks pt JOIN playlists p ON p.id = pt.playlist_id JOIN users u ON u.id = p.user_id
        WHERE pt.video_id = ? AND {listed('p')}
        GROUP BY u.id ORDER BY n DESC, first_at ASC, u.id ASC
        """,
        (video_id,),
    ).fetchall()
    curators = [{"user": user_mini(r), "playlist_count": int(r["n"])} for r in cur_rows]

    # alongside: co-occurring songs in LISTED playlists that contain this one
    along = conn.execute(
        f"""
        WITH lp AS (SELECT DISTINCT p.id FROM playlist_tracks pt JOIN playlists p ON p.id = pt.playlist_id
                    WHERE pt.video_id = ? AND {listed('p')})
        SELECT b.video_id, s.title, s.channel_title, s.thumbnail_url, COUNT(DISTINCT b.playlist_id) AS together_count
        FROM playlist_tracks b JOIN lp ON lp.id = b.playlist_id JOIN songs s ON s.video_id = b.video_id
        WHERE b.video_id != ?
        GROUP BY b.video_id
        ORDER BY together_count DESC, s.title ASC, b.video_id ASC
        LIMIT ?
        """,
        (video_id, video_id, ALONGSIDE),
    ).fetchall()
    alongside = [
        {"video_id": r["video_id"], "title": r["title"], "channel_title": r["channel_title"],
         "thumbnail_url": r["thumbnail_url"], "together_count": int(r["together_count"])}
        for r in along
    ]
    return {
        "playlist_count": listed_playlist_count(conn, video_id),
        "playlists": playlists,
        "curators": curators,
        "alongside": alongside,
    }


# --- Taste neighbours and overlap ---------------------------------------------------------

def _my_videos_sql(listed_only: bool) -> str:
    cond = listed("mp") if listed_only else "mp.kind = 'playlist'"
    return (
        "SELECT DISTINCT mpt.video_id FROM playlist_tracks mpt JOIN playlists mp ON mp.id = mpt.playlist_id "
        f"WHERE mp.user_id = :me AND {cond}"
    )


def _shared_sample(conn: sqlite3.Connection, me: int, other: int, listed_only_me: bool, n: int) -> List[Dict[str, Any]]:
    rows = conn.execute(
        f"""
        WITH mine AS ({_my_videos_sql(listed_only_me)})
        SELECT pt.video_id, s.title, s.thumbnail_url, MAX(pt.created_at) AS last_at
        FROM playlist_tracks pt JOIN playlists p ON p.id = pt.playlist_id
        JOIN mine m ON m.video_id = pt.video_id JOIN songs s ON s.video_id = pt.video_id
        WHERE p.user_id = :other AND {listed('p')}
        GROUP BY pt.video_id ORDER BY last_at DESC, pt.video_id ASC LIMIT :n
        """,
        {"me": me, "other": other, "n": n},
    ).fetchall()
    return [{"video_id": r["video_id"], "title": r["title"], "thumbnail_url": r["thumbnail_url"]} for r in rows]


def neighbour_ids(conn: sqlite3.Connection, me: int, listed_only_me: bool, limit: int) -> List[Dict[str, Any]]:
    """[{user_id, shared_count}] ranked by distinct shared videos, ties by the most recent shared placement."""
    if not me:
        return []
    rows = conn.execute(
        f"""
        WITH mine AS ({_my_videos_sql(listed_only_me)})
        SELECT p.user_id, COUNT(DISTINCT pt.video_id) AS shared_count, MAX(pt.created_at) AS last_at
        FROM playlist_tracks pt JOIN playlists p ON p.id = pt.playlist_id JOIN mine m ON m.video_id = pt.video_id
        WHERE p.user_id != :me AND {listed('p')}
        GROUP BY p.user_id ORDER BY shared_count DESC, last_at DESC, p.user_id ASC LIMIT :n
        """,
        {"me": int(me), "n": limit},
    ).fetchall()
    return [{"user_id": int(r["user_id"]), "shared_count": int(r["shared_count"])} for r in rows]


def taste_neighbours(conn: sqlite3.Connection, me: int, listed_only_me: bool, limit: int) -> List[Dict[str, Any]]:
    ranked = neighbour_ids(conn, me, listed_only_me, limit)
    minis = user_minis(conn, [r["user_id"] for r in ranked])
    return [
        {"user": minis[r["user_id"]], "shared_count": r["shared_count"],
         "sample": _shared_sample(conn, me, r["user_id"], listed_only_me, NEIGHBOUR_SAMPLE)}
        for r in ranked
    ]


def shared_with_viewer(conn: sqlite3.Connection, profile_user_id: int, viewer: int) -> Optional[Dict[str, Any]]:
    """Overlap between the viewer's own playlists (any visibility, result goes to the viewer only)
    and the profile user's LISTED playlists. None when anonymous or on your own profile."""
    viewer = int(viewer or 0)
    if not viewer or viewer == int(profile_user_id):
        return None
    n = conn.execute(
        f"""
        WITH mine AS ({_my_videos_sql(False)})
        SELECT COUNT(DISTINCT pt.video_id) FROM playlist_tracks pt JOIN playlists p ON p.id = pt.playlist_id
        JOIN mine m ON m.video_id = pt.video_id
        WHERE p.user_id = :other AND {listed('p')}
        """,
        {"me": viewer, "other": int(profile_user_id)},
    ).fetchone()[0]
    return {"shared_count": int(n), "sample": _shared_sample(conn, viewer, int(profile_user_id), False, SHARED_SAMPLE)}


def profile_extras(conn: sqlite3.Connection, profile_user_id: int, viewer: int) -> Dict[str, Any]:
    return {
        # Both sides listed-only: everyone sees the same neighbours on a profile.
        "neighbours": taste_neighbours(conn, int(profile_user_id), True, PROFILE_NEIGHBOURS),
        "shared_with_viewer": shared_with_viewer(conn, profile_user_id, viewer),
    }


# --- Activity ------------------------------------------------------------------------

def activity_lines(conn: sqlite3.Connection, limit: int, actor_ids: Optional[Sequence[int]] = None) -> List[Dict[str, Any]]:
    """The /api/feed activity query (same visibility filters) without earned_badge lines."""
    params: List[Any] = []
    actor_filter = ""
    if actor_ids is not None:
        ids = [int(a) for a in actor_ids]
        if not ids:
            return []
        actor_filter = f"AND a.actor_user_id IN ({_marks(len(ids))})"
        params.extend(ids)
    params.append(limit)
    return [dict(r) for r in conn.execute(
        f"""
        SELECT a.id, a.actor_user_id, a.action_type, a.summary, a.link_type, a.link_id, a.created_at,
               u.username, u.display_name, u.avatar_color
        FROM activity_log a
        JOIN users u ON u.id = a.actor_user_id
        WHERE (a.link_type != 'playlist'
               OR EXISTS (SELECT 1 FROM playlists p WHERE p.id = CAST(a.link_id AS INTEGER) AND {listed('p')}))
          AND (a.link_type != 'song' OR {public_song_sql('a.link_id')})
          AND a.action_type != 'earned_badge'
          {actor_filter}
        ORDER BY a.created_at DESC, a.id DESC
        LIMIT ?
        """,
        params,
    )]


# --- Home (§3.2) ------------------------------------------------------------------------

def needs_handle(conn: sqlite3.Connection, user_id: int, google_mode: bool) -> bool:
    if not google_mode or not user_id:
        return False
    row = conn.execute("SELECT handle_chosen_at FROM users WHERE id = ?", (int(user_id),)).fetchone()
    return row is not None and row["handle_chosen_at"] is None


def _crate_block(conn: sqlite3.Connection, me: int) -> Optional[Dict[str, Any]]:
    cid = crate_id_for(conn, me)
    if cid is None:
        return None
    pl = conn.execute("SELECT id, version FROM playlists WHERE id = ?", (cid,)).fetchone()
    count = conn.execute("SELECT COUNT(*) FROM playlist_tracks WHERE playlist_id = ?", (cid,)).fetchone()[0]
    rows = conn.execute(
        f"""
        WITH o AS ({_ordered_rows_sql('?')})
        SELECT {_TRACK_MINI_COLS}, su.username AS source_username
        FROM o JOIN songs s ON s.video_id = o.video_id LEFT JOIN users su ON su.id = o.source_user_id
        ORDER BY o.created_at DESC, o.id DESC LIMIT ?
        """,
        (cid, HOME_CRATE_RECENT),
    ).fetchall()
    return {
        "id": int(pl["id"]),
        "version": int(pl["version"]),
        "track_count": int(count),
        "recent": [{**_track_mini(r), "source_username": r["source_username"]} for r in rows],
    }


def _fresh(conn: sqlite3.Connection, me: int, neighbour_user_ids: List[int]) -> List[Dict[str, Any]]:
    if not neighbour_user_ids:
        return []
    marks = _marks(len(neighbour_user_ids))
    rows = conn.execute(
        f"""
        WITH theirs AS (SELECT p.id FROM playlists p WHERE p.user_id IN ({marks}) AND {listed('p')}),
        o AS (
            SELECT pt.*, ROW_NUMBER() OVER (PARTITION BY pt.playlist_id ORDER BY pt.position ASC, pt.id ASC) AS ordinal
            FROM playlist_tracks pt WHERE pt.playlist_id IN (SELECT id FROM theirs)
        ),
        cand AS (
            SELECT o.*, ROW_NUMBER() OVER (PARTITION BY o.video_id ORDER BY o.created_at DESC, o.id DESC) AS rn
            FROM o
            -- not in any playlist I own: my playlists of any visibility AND my crate
            WHERE o.video_id NOT IN (SELECT mpt.video_id FROM playlist_tracks mpt
                                     JOIN playlists mp ON mp.id = mpt.playlist_id WHERE mp.user_id = ?)
        )
        SELECT {_TRACK_MINI_COLS.replace('o.', 'c.')},
               p.title AS playlist_title,
               u.id AS u_id, u.username AS u_username, u.display_name AS u_display_name, u.avatar_color AS u_avatar_color,
               (SELECT COUNT(DISTINCT pt2.playlist_id) FROM playlist_tracks pt2 JOIN playlists p2 ON p2.id = pt2.playlist_id
                 WHERE pt2.video_id = c.video_id AND {listed('p2')}) AS playlist_count
        FROM cand c JOIN songs s ON s.video_id = c.video_id
        JOIN playlists p ON p.id = c.playlist_id JOIN users u ON u.id = p.user_id
        WHERE c.rn = 1
        ORDER BY c.created_at DESC, c.id DESC
        LIMIT ?
        """,
        [*neighbour_user_ids, int(me), HOME_FRESH],
    ).fetchall()
    return [
        {"track": _track_mini(r), "playlist": {"id": int(r["playlist_id"]), "title": r["playlist_title"]},
         "user": user_mini(r, "u_"), "added_at": r["created_at"], "playlist_count": int(r["playlist_count"])}
        for r in rows
    ]


def _about_you(conn: sqlite3.Connection, me: int) -> List[Dict[str, Any]]:
    grabs = conn.execute(
        f"""
        SELECT pt.created_at AS at, pt.video_id, s.title, tp.id AS playlist_id, tp.title AS playlist_title,
               u.id AS u_id, u.username AS u_username, u.display_name AS u_display_name, u.avatar_color AS u_avatar_color
        FROM playlist_tracks pt
        JOIN songs s ON s.video_id = pt.video_id
        JOIN users u ON u.id = pt.added_by_user_id
        JOIN playlists tp ON tp.id = pt.playlist_id
        WHERE pt.source_user_id = ? AND pt.added_by_user_id != ? AND {listed('tp')}
        ORDER BY pt.created_at DESC, pt.id DESC LIMIT ?
        """,
        (me, me, HOME_ABOUT),
    ).fetchall()
    comments = conn.execute(
        """
        SELECT c.id, c.created_at AS at, c.body, p.id AS playlist_id, p.title AS playlist_title,
               u.id AS u_id, u.username AS u_username, u.display_name AS u_display_name, u.avatar_color AS u_avatar_color
        FROM playlists p
        JOIN comments c ON c.target_type = 'playlist' AND c.target_id = CAST(p.id AS TEXT)
        JOIN users u ON u.id = c.author_user_id
        WHERE p.user_id = ? AND p.kind = 'playlist' AND c.author_user_id != ?
        ORDER BY c.created_at DESC, c.id DESC LIMIT ?
        """,
        (me, me, HOME_ABOUT),
    ).fetchall()
    items = [
        {"type": "grab", "at": r["at"], "user": user_mini(r, "u_"), "video_id": r["video_id"], "title": r["title"],
         "playlist": {"id": int(r["playlist_id"]), "title": r["playlist_title"]}}
        for r in grabs
    ] + [
        {"type": "comment", "at": r["at"], "user": user_mini(r, "u_"), "body": r["body"],
         "playlist": {"id": int(r["playlist_id"]), "title": r["playlist_title"]}}
        for r in comments
    ]
    items.sort(key=lambda i: i["at"] or "", reverse=True)
    return items[:HOME_ABOUT]


def home(conn: sqlite3.Connection, me: int, google_mode: bool) -> Dict[str, Any]:
    me = int(me)
    u = conn.execute("SELECT id, username, display_name, avatar_color, bio FROM users WHERE id = ?", (me,)).fetchone()
    my_ids = [int(r[0]) for r in conn.execute(
        "SELECT id FROM playlists WHERE user_id = ? AND kind = 'playlist' ORDER BY updated_at DESC, id DESC", (me,)
    )]
    dock = dock_view(conn, me)  # creates the crate if it is missing (and commits)
    pool = neighbour_ids(conn, me, False, NEIGHBOUR_POOL)
    pool_ids = [r["user_id"] for r in pool]
    minis = user_minis(conn, pool_ids[:HOME_NEIGHBOURS])
    neighbours = [
        {"user": minis[r["user_id"]], "shared_count": r["shared_count"],
         "sample": _shared_sample(conn, me, r["user_id"], False, NEIGHBOUR_SAMPLE)}
        for r in pool[:HOME_NEIGHBOURS]
    ]
    activity = activity_lines(conn, HOME_ACTIVITY, pool_ids if pool_ids else None)
    return {
        "user": {**user_mini(u), "bio": u["bio"] or "", "needs_handle": needs_handle(conn, me, google_mode)},
        "playlists": playlist_cards(conn, my_ids),
        "crate": _crate_block(conn, me),
        "dock": dock,
        "undo": undo_state(conn, me),
        "neighbours": neighbours,
        "fresh": _fresh(conn, me, pool_ids),
        "about_you": _about_you(conn, me),
        "activity": activity,
    }


# --- Discover (§3.3) --------------------------------------------------------------------

def _page(rows: List[Any], offset: int, limit: int):
    more = len(rows) > limit
    return rows[:limit], (offset + limit if more else None)


def discover_playlists(conn: sqlite3.Connection, sort: str, offset: int, limit: int) -> Dict[str, Any]:
    order = (
        "(p.plays + 3 * (SELECT COALESCE(SUM(pl.count), 0) FROM playlist_likes pl WHERE pl.playlist_id = p.id)) DESC, "
        "p.updated_at DESC, p.id DESC"
        if sort == "popular"
        else "p.updated_at DESC, p.id DESC"
    )
    rows = conn.execute(
        f"""
        SELECT p.id FROM playlists p
        WHERE {listed('p')} AND EXISTS (SELECT 1 FROM playlist_tracks pt WHERE pt.playlist_id = p.id)
        ORDER BY {order} LIMIT ? OFFSET ?
        """,
        (limit + 1, offset),
    ).fetchall()
    rows, nxt = _page(rows, offset, limit)
    out = {"tab": "playlists", "sort": sort, "offset": offset, "next_offset": nxt,
           "playlists": playlist_cards(conn, [int(r["id"]) for r in rows])}
    if offset == 0:
        out["activity"] = activity_lines(conn, DISCOVER_ACTIVITY)
    return out


def discover_tracks(conn: sqlite3.Connection, viewer: int, offset: int, limit: int) -> Dict[str, Any]:
    viewer = int(viewer or 0)
    rows = conn.execute(
        f"""
        SELECT s.video_id, s.title, s.channel_title, s.thumbnail_url, s.first_added_at,
               u.id AS u_id, u.username AS u_username, u.display_name AS u_display_name, u.avatar_color AS u_avatar_color,
               CASE WHEN p.id IS NOT NULL AND {linkable('p', viewer)} THEN p.id END AS debut_id,
               CASE WHEN p.id IS NOT NULL AND {linkable('p', viewer)} THEN p.title END AS debut_title,
               (SELECT COUNT(DISTINCT pt.playlist_id) FROM playlist_tracks pt JOIN playlists p2 ON p2.id = pt.playlist_id
                 WHERE pt.video_id = s.video_id AND {listed('p2')}) AS playlist_count,
               (SELECT COALESCE(SUM(sl.count), 0) FROM song_likes sl WHERE sl.video_id = s.video_id) AS like_count
        FROM songs s
        LEFT JOIN users u ON u.id = s.first_added_by_user_id
        LEFT JOIN playlists p ON p.id = s.first_added_playlist_id
        WHERE s.first_added_at IS NOT NULL AND {song_visible_sql('s.video_id', viewer)}
        ORDER BY s.first_added_at DESC, s.video_id ASC
        LIMIT ? OFFSET ?
        """,
        (limit + 1, offset),
    ).fetchall()
    rows, nxt = _page(rows, offset, limit)
    return {
        "tab": "tracks", "offset": offset, "next_offset": nxt,
        "tracks": [
            {"video_id": r["video_id"], "title": r["title"], "channel_title": r["channel_title"],
             "thumbnail_url": r["thumbnail_url"], "first_added_at": r["first_added_at"],
             "finder": user_mini(r, "u_"),
             "debut_playlist": {"id": int(r["debut_id"]), "title": r["debut_title"]} if r["debut_id"] is not None else None,
             "playlist_count": int(r["playlist_count"]), "like_count": int(r["like_count"])}
            for r in rows
        ],
    }


def discover_people(conn: sqlite3.Connection, offset: int, limit: int) -> Dict[str, Any]:
    rows = conn.execute(
        f"""
        SELECT u.id, u.username, u.display_name, u.avatar_color, u.bio,
               COUNT(DISTINCT p.id) AS playlist_count,
               (SELECT COUNT(*) FROM playlist_tracks pt JOIN playlists tp ON tp.id = pt.playlist_id
                 WHERE tp.user_id = u.id AND {listed('tp')}) AS track_count,
               MAX(p.updated_at) AS last_active_at
        FROM users u JOIN playlists p ON p.user_id = u.id AND {listed('p')}
        GROUP BY u.id
        ORDER BY last_active_at DESC, u.id ASC
        LIMIT ? OFFSET ?
        """,
        (limit + 1, offset),
    ).fetchall()
    rows, nxt = _page(rows, offset, limit)
    return {
        "tab": "people", "offset": offset, "next_offset": nxt,
        "people": [
            {"user": user_mini(r), "bio": r["bio"] or "", "playlist_count": int(r["playlist_count"]),
             "track_count": int(r["track_count"]), "last_active_at": r["last_active_at"]}
            for r in rows
        ],
    }


# --- Search (§3.4) ----------------------------------------------------------------------

def normalize_query(raw: str) -> str:
    return re.sub(r"\s+", " ", raw or "").strip()


def like_pattern(q: str) -> str:
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def search(conn: sqlite3.Connection, viewer: int, q: str, kind: str, limit: int) -> Dict[str, Any]:
    viewer = int(viewer or 0)
    pat = like_pattern(q)
    out: Dict[str, Any] = {"query": q, "playlists": [], "tracks": [], "people": []}
    if kind in ("all", "playlists"):
        ids = [int(r[0]) for r in conn.execute(
            f"""
            SELECT p.id FROM playlists p
            WHERE {listed('p')} AND (p.title LIKE :pat ESCAPE '\\' OR p.description LIKE :pat ESCAPE '\\')
            ORDER BY p.updated_at DESC, p.id DESC LIMIT :n
            """,
            {"pat": pat, "n": limit},
        )]
        out["playlists"] = playlist_cards(conn, ids)
    if kind in ("all", "tracks"):
        rows = conn.execute(
            f"""
            WITH hits AS (
                SELECT s.video_id FROM songs s
                WHERE (s.title LIKE :pat ESCAPE '\\' OR s.channel_title LIKE :pat ESCAPE '\\')
                  AND {song_visible_sql('s.video_id', viewer)}
                UNION
                SELECT pt.video_id FROM playlist_tracks pt JOIN playlists p ON p.id = pt.playlist_id
                WHERE pt.display_title LIKE :pat ESCAPE '\\' AND {listed('p')}
            )
            SELECT s.video_id, s.title, s.channel_title, s.thumbnail_url,
                   (SELECT COUNT(DISTINCT pt.playlist_id) FROM playlist_tracks pt JOIN playlists p2 ON p2.id = pt.playlist_id
                     WHERE pt.video_id = s.video_id AND {listed('p2')}) AS playlist_count
            FROM hits h JOIN songs s ON s.video_id = h.video_id
            ORDER BY playlist_count DESC, s.title ASC, s.video_id ASC LIMIT :n
            """,
            {"pat": pat, "n": limit},
        ).fetchall()
        out["tracks"] = [
            {"video_id": r["video_id"], "title": r["title"], "channel_title": r["channel_title"],
             "thumbnail_url": r["thumbnail_url"], "playlist_count": int(r["playlist_count"])}
            for r in rows
        ]
    if kind in ("all", "people"):
        rows = conn.execute(
            f"""
            SELECT u.id, u.username, u.display_name, u.avatar_color,
                   (SELECT COUNT(*) FROM playlists p WHERE p.user_id = u.id AND {listed('p')}) AS playlist_count
            FROM users u
            WHERE (u.username LIKE :pat ESCAPE '\\' OR u.display_name LIKE :pat ESCAPE '\\')
              AND (u.id = :viewer OR EXISTS (SELECT 1 FROM playlists p WHERE p.user_id = u.id AND {listed('p')}))
            ORDER BY playlist_count DESC, u.username ASC LIMIT :n
            """,
            {"pat": pat, "viewer": viewer, "n": limit},
        ).fetchall()
        out["people"] = [{"user": user_mini(r), "playlist_count": int(r["playlist_count"])} for r in rows]
    return out


# --- Profiles by handle (§3.7) ---------------------------------------------------------------

HANDLE_SHAPE = re.compile(r"^[a-z0-9_]{3,20}$")


def user_id_for_handle(conn: sqlite3.Connection, handle: str) -> Optional[int]:
    h = (handle or "").lower()
    if not HANDLE_SHAPE.match(h):
        return None
    row = conn.execute("SELECT id FROM users WHERE LOWER(username) = ?", (h,)).fetchone()
    if row:
        return int(row["id"])
    row = conn.execute("SELECT user_id FROM handle_history WHERE handle = ?", (h,)).fetchone()
    return int(row["user_id"]) if row else None

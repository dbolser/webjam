"""Who can see which playlist: the only allowed spellings of the privacy predicates.

LISTED(p)   public playlist: feeds, lists, counts, badges, discovery.
VISIBLE(p)  can be opened by id: public/unlisted playlists, or anything the viewer owns.
LINKABLE(p) may be named or linked from another page: LISTED, or the viewer's own.

"Not visible" is always a 404 so private playlists can't be probed.

The SQL helpers take the table alias and the viewer. A viewer given as an int is
inlined (after int()), so the fragments work in queries with positional or named
parameters alike; pass a string such as ":viewer" or "?" to bind it instead.
"""

import sqlite3
from typing import Union

from app.errors import ApiError

Viewer = Union[int, str]

LISTED = "({a}.kind = 'playlist' AND {a}.visibility = 'public')"
VISIBLE = "(({a}.kind = 'playlist' AND {a}.visibility IN ('public','unlisted')) OR {a}.user_id = {viewer})"
LINKABLE = "(({a}.kind = 'playlist' AND {a}.visibility = 'public') OR {a}.user_id = {viewer})"


def _viewer_sql(viewer: Viewer) -> str:
    if isinstance(viewer, str):
        return viewer
    return str(int(viewer or 0))


def listed(a: str = "p") -> str:
    return LISTED.format(a=a)


def visible(a: str = "p", viewer: Viewer = ":viewer") -> str:
    return VISIBLE.format(a=a, viewer=_viewer_sql(viewer))


def linkable(a: str = "p", viewer: Viewer = ":viewer") -> str:
    return LINKABLE.format(a=a, viewer=_viewer_sql(viewer))


def is_listed(pl) -> bool:
    return pl is not None and pl["kind"] == "playlist" and pl["visibility"] == "public"


def is_visible(pl, viewer: int) -> bool:
    if pl is None:
        return False
    return (pl["kind"] == "playlist" and pl["visibility"] in ("public", "unlisted")) or (
        bool(viewer) and pl["user_id"] == viewer
    )


def is_linkable(pl, viewer: int) -> bool:
    if pl is None:
        return False
    return is_listed(pl) or (bool(viewer) and pl["user_id"] == viewer)


def require_visible_playlist(conn: sqlite3.Connection, playlist_id: int, viewer: int) -> sqlite3.Row:
    """The playlist row if the viewer may open it, else 404 (missing and private look the same)."""
    pl = conn.execute("SELECT * FROM playlists WHERE id = ?", (int(playlist_id),)).fetchone()
    if not is_visible(pl, viewer):
        raise ApiError(404, "playlist_not_found", "Playlist not found")
    return pl


def song_visible(conn: sqlite3.Connection, video_id: str, viewer: int) -> bool:
    """A song page exists for the viewer if it is in a public playlist, or in one of their own."""
    row = conn.execute(
        f"""
        SELECT 1 FROM playlist_tracks pt JOIN playlists p ON p.id = pt.playlist_id
        WHERE pt.video_id = ? AND ({listed('p')} OR p.user_id = ?)
        LIMIT 1
        """,
        (video_id, int(viewer or 0)),
    ).fetchone()
    return row is not None


def song_visible_sql(video_expr: str, viewer: int) -> str:
    """song_visible() as SQL: in a LISTED playlist, or in one of the viewer's own (crate included)."""
    return (
        f"EXISTS (SELECT 1 FROM playlist_tracks vpt JOIN playlists vp ON vp.id = vpt.playlist_id "
        f"WHERE vpt.video_id = {video_expr} AND ({listed('vp')} OR vp.user_id = {int(viewer or 0)}))"
    )


def public_song_sql(video_expr: str) -> str:
    """EXISTS a LISTED playlist containing the video (a song anyone may see named)."""
    return (
        f"EXISTS (SELECT 1 FROM playlist_tracks xpt JOIN playlists xp ON xp.id = xpt.playlist_id "
        f"WHERE xpt.video_id = {video_expr} AND {listed('xp')})"
    )

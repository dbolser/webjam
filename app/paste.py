"""Turn pasted text (or clipboard HTML) into insert-ready items. Never writes anything.

Recognition order (docs/phase1-ops-api.md §3.7):
1. a Webjam JSON clipboard payload ({"webjam": 1, ...}), in the text or in a data-webjam HTML attribute;
2. Webjam URLs (/p/{id}[-slug][?track=tid], /#playlist/{id}, /t/{video_id}?s=&e=, /#song/{video_id});
3. YouTube video URLs (with t=/start=/end= clips; watch?v=X&list=PL... also yields a playlist pointer);
4. YouTube playlist URLs and bare playlist ids: a pointer only, never expanded;
5. bare 11-character video ids, only when nothing above matched.
Results keep text order and keep duplicates.
"""

import html as html_lib
import json
import re
import sqlite3
import urllib.parse
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from app import youtube
from app.ops import VideoLookupFailed, lookup_song_metadata
from app.visibility import is_visible, song_visible
from app.youtube import (
    MAX_PARSED_ENTRIES,
    PLAYLIST_URL_PARAM_PATTERN,
    RAW_PLAYLIST_ID_PATTERN,
    _parse_youtube_url,
    extract_playlist_id,
    is_valid_video_id,
    parse_timestamp_seconds,
)

MAX_PASTE_ITEMS = 1000
MAX_UNKNOWN_LOOKUPS = 50
MAX_NOTE = 5000
MAX_TITLE = 200
LOOPBACK = {"localhost", "127.0.0.1", "::1"}
IMPORT_ENDPOINT = "/api/playlists/import/inspect"

_DATA_WEBJAM = re.compile(r"""data-webjam\s*=\s*(?:'([^']*)'|"([^"]*)")""", re.I)
_P_PATH = re.compile(r"/p/(\d{1,18})(?:-[A-Za-z0-9_-]{0,200})?/?")
_T_PATH = re.compile(r"/t/([A-Za-z0-9_-]{11})/?")
_P_FRAG = re.compile(r"playlist/(\d{1,18})")
_T_FRAG = re.compile(r"song/([A-Za-z0-9_-]{11})")
_TRIM = "<>()[]{}\"'.,;!"


def _payload_from(text: str, html: Optional[str]) -> Optional[Dict[str, Any]]:
    candidates = []
    if html:
        m = _DATA_WEBJAM.search(html)
        if m:
            candidates.append(html_lib.unescape(m.group(1) if m.group(1) is not None else m.group(2)))
    stripped = (text or "").strip()
    if stripped.startswith("{"):
        candidates.append(stripped)
    for raw in candidates:
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError):
            continue
        if isinstance(data, dict) and data.get("webjam") == 1 and isinstance(data.get("items"), list):
            return data
    return None


def _clip_param(qs: Dict[str, List[str]], *names: str) -> Optional[int]:
    for n in names:
        if qs.get(n):
            return parse_timestamp_seconds(qs[n][0])
    return None


def _webjam_target(token: str, base_host: str, demo: bool) -> Optional[Tuple[str, Any, Dict[str, List[str]]]]:
    try:
        parsed = urllib.parse.urlsplit(token)
        qs = urllib.parse.parse_qs(parsed.query, max_num_fields=20)
    except ValueError:
        return None
    if token.startswith("/"):
        if token.startswith("//"):
            return None
    else:
        if parsed.scheme.lower() not in ("http", "https"):
            return None
        host = (parsed.hostname or "").lower()
        if not ((base_host and host == base_host) or (demo and host in LOOPBACK)):
            return None
    path, frag = parsed.path, parsed.fragment
    m = _P_PATH.fullmatch(path)
    if m:
        return "playlist", int(m.group(1)), qs
    m = _T_PATH.fullmatch(path)
    if m:
        return "song", m.group(1), qs
    if path in ("", "/") and frag:
        m = _P_FRAG.fullmatch(frag)
        if m:
            return "playlist", int(m.group(1)), {}
        m = _T_FRAG.fullmatch(frag)
        if m:
            return "song", m.group(1), {}
    return None


def _is_urlish(token: str) -> bool:
    low = token.lower()
    return "://" in low or low.startswith(("youtube.com/", "www.youtube.com/", "m.youtube.com/", "music.youtube.com/",
                                           "youtu.be/", "/p/", "/t/", "/#"))


def _playlist_pointer(list_id: str, source_input: str) -> Dict[str, Any]:
    return {"type": "youtube_playlist", "list_id": list_id, "source_input": source_input,
            "import_endpoint": IMPORT_ENDPOINT}


def _row_ref(conn: sqlite3.Connection, track_id: Any, video_id: str, viewer: int) -> Optional[Dict[str, Any]]:
    try:
        tid = int(track_id)
    except (TypeError, ValueError, OverflowError):
        return None
    if tid < 1 or tid > 2**53:
        return None
    row = conn.execute(
        """
        SELECT pt.id, pt.video_id, p.id AS playlist_id, p.user_id, p.kind, p.visibility, u.username
        FROM playlist_tracks pt JOIN playlists p ON p.id = pt.playlist_id JOIN users u ON u.id = p.user_id
        WHERE pt.id = ?
        """,
        (tid,),
    ).fetchone()
    if not row or row["video_id"] != video_id or not is_visible(row, viewer):
        return None
    return {"track_id": row["id"], "playlist_id": row["playlist_id"], "username": row["username"]}


def _item(video_id: str, start: Any = None, end: Any = None, note: str = "", display_title: Optional[str] = None,
          ref: Optional[Dict[str, Any]] = None, source: str = "") -> Dict[str, Any]:
    return {"video_id": video_id, "start_seconds": start, "end_seconds": end, "note": note or "",
            "display_title": display_title, "ref": ref, "from": source}


def _clip_ok(start: Optional[int], end: Optional[int]) -> Tuple[Optional[int], Optional[int]]:
    if start is not None and end is not None and end <= start:
        return start, None
    return start, end


def _rows_as_items(conn: sqlite3.Connection, pl: sqlite3.Row, track_id: Optional[int]) -> List[Dict[str, Any]]:
    owner = conn.execute("SELECT username FROM users WHERE id = ?", (pl["user_id"],)).fetchone()
    sql = "SELECT * FROM playlist_tracks WHERE playlist_id = ?"
    args: List[Any] = [pl["id"]]
    if track_id is not None:
        sql += " AND id = ?"
        args.append(track_id)
    rows = conn.execute(sql + " ORDER BY position ASC, id ASC", args).fetchall()
    out = []
    for r in rows:
        start = int(r["start_seconds"]) if r["start_seconds"] is not None else None
        end = int(r["end_seconds"]) if r["end_seconds"] is not None else None
        out.append(_item(r["video_id"], start, end, r["track_note"] or "", r["display_title"],
                         {"track_id": r["id"], "playlist_id": pl["id"], "username": owner["username"] if owner else None},
                         "webjam_url"))
    return out


_WORDLIKE = re.compile(r"^(?:[A-Za-z][a-z]{10}|[A-Z]{11})$")


def _looks_like_video_id(token: str) -> bool:
    """An 11-character id that isn't shaped like an English word (all lower case, Capitalised or ALL CAPS).

    A real id is all letters in one of those shapes with odds of roughly 1 in 10,000; those can
    still be pasted as a youtu.be / watch URL.
    """
    return is_valid_video_id(token) and not _WORDLIKE.match(token)


async def resolve_paste(
    conn: sqlite3.Connection,
    text: str,
    html: Optional[str],
    viewer: int,
    *,
    fetch: Callable[[str], Awaitable[Dict[str, Any]]],
    charge: Callable[[], None],
    base_url: str = "",
    demo: bool = False,
) -> Dict[str, Any]:
    base_host = (urllib.parse.urlsplit(base_url).hostname or "").lower() if base_url else ""
    sources: List[str] = []
    items: List[Dict[str, Any]] = []
    pointers: List[Dict[str, Any]] = []
    unresolved: List[Dict[str, Any]] = []

    def source(s: str) -> None:
        if s not in sources:
            sources.append(s)

    def add(new_items: List[Dict[str, Any]], token: str) -> None:
        for it in new_items:
            if len(items) >= MAX_PASTE_ITEMS:
                unresolved.append({"token": token, "reason": "too_many"})
                return
            items.append(it)

    payload = _payload_from(text, html)
    if payload is not None:
        source("webjam_json")
        for raw in payload["items"][:MAX_PASTE_ITEMS]:
            if not isinstance(raw, dict) or not is_valid_video_id(raw.get("video_id")):
                continue
            vid = raw["video_id"]
            start, end = _clip_ok(parse_timestamp_seconds(raw.get("start_seconds")),
                                  parse_timestamp_seconds(raw.get("end_seconds")))
            note = raw.get("note") if isinstance(raw.get("note"), str) else ""
            dt = raw.get("display_title") if isinstance(raw.get("display_title"), str) else None
            ref_in = raw.get("ref") if isinstance(raw.get("ref"), dict) else {}
            ref = _row_ref(conn, ref_in.get("track_id"), vid, viewer) if ref_in.get("track_id") is not None else None
            items.append(_item(vid, start, end, note[:MAX_NOTE], (dt or "")[:MAX_TITLE] or None, ref, "webjam_json"))
        if len(payload["items"]) > MAX_PASTE_ITEMS:
            unresolved.append({"token": "webjam_json", "reason": "too_many"})
    else:
        url_count = 0
        bare_tokens: List[str] = []
        for raw_token in (text or "").split():
            token = raw_token.strip(_TRIM)
            if not token:
                continue
            if not _is_urlish(token):
                bare_tokens.append(token)
                if len(token) != 11 and RAW_PLAYLIST_ID_PATTERN.match(token):
                    source("youtube_playlist")
                    pointers.append(_playlist_pointer(token, token))
                continue
            url_count += 1
            if url_count > MAX_PARSED_ENTRIES:
                unresolved.append({"token": token[:200], "reason": "too_many"})
                continue
            target = _webjam_target(token, base_host, demo)
            if target is not None:
                kind, ident, qs = target
                if kind == "playlist":
                    pl = conn.execute("SELECT * FROM playlists WHERE id = ?", (ident,)).fetchone()
                    if not is_visible(pl, viewer):
                        unresolved.append({"token": token[:200], "reason": "not_found"})
                        continue
                    track = (qs.get("track") or [None])[0]
                    if track is not None:
                        if not (track.isascii() and track.isdigit() and len(track) <= 18):
                            unresolved.append({"token": token[:200], "reason": "not_found"})
                            continue
                        found = _rows_as_items(conn, pl, int(track))
                        if not found:
                            unresolved.append({"token": token[:200], "reason": "not_found"})
                            continue
                        source("webjam_url")
                        add(found, token)
                    else:
                        source("webjam_url")
                        found = _rows_as_items(conn, pl, None)
                        add(found, token)
                        pointers.append({"type": "webjam_playlist", "playlist_id": pl["id"], "title": pl["title"],
                                         "kind": pl["kind"], "track_count": len(found)})
                else:
                    start, end = _clip_ok(_clip_param(qs, "s", "start", "t"), _clip_param(qs, "e", "end"))
                    source("webjam_url")
                    add([_item(ident, start, end, source="webjam_url")], token)
                continue
            entry = _parse_youtube_url(token)
            if entry:
                source("youtube_url")
                start, end = _clip_ok(entry["start_seconds"], entry["end_seconds"])
                add([_item(entry["video_id"], start, end, source="youtube_url")], token)
                m = PLAYLIST_URL_PARAM_PATTERN.search(token)
                if m:
                    source("youtube_playlist")
                    pointers.append(_playlist_pointer(m.group(1), token))
                continue
            list_id = extract_playlist_id(token)
            if list_id:
                source("youtube_playlist")
                pointers.append(_playlist_pointer(list_id, token))
        # Bare ids only when the whole paste is ids (separated by spaces / commas), and none of
        # them looks like a word: free text ("really interesting information") stays empty and
        # costs no lookups.
        parts = [p for token in bare_tokens for p in re.split(r"[\s,]+", token) if p]
        if not items and not pointers and not unresolved and parts and all(_looks_like_video_id(p) for p in parts):
            for part in parts:
                source("video_id")
                add([_item(part, source="video_id")], part)

    # Metadata: Webjam's cached row when the song is on Webjam, else a (TTL-cached) YouTube lookup.
    meta: Dict[str, Dict[str, Any]] = {}
    lookups = 0
    charged = False
    drop: Dict[str, str] = {}
    for vid in dict.fromkeys(it["video_id"] for it in items):
        row = conn.execute("SELECT title, channel_title, thumbnail_url FROM songs WHERE video_id = ?", (vid,)).fetchone()
        if row is not None:
            meta[vid] = {"title": row["title"], "channel_title": row["channel_title"],
                         "thumbnail_url": row["thumbnail_url"], "known": song_visible(conn, vid, viewer)}
            continue
        if lookups >= MAX_UNKNOWN_LOOKUPS:
            drop[vid] = "too_many"
            continue
        if not charged:
            charge()
            charged = True
        lookups += 1
        try:
            m = await lookup_song_metadata(fetch, vid)
        except VideoLookupFailed as exc:
            drop[vid] = exc.reason
            continue
        meta[vid] = {"title": m["title"], "channel_title": m.get("channel_title") or "",
                     "thumbnail_url": m.get("thumbnail_url") or "", "known": False}

    out_items = []
    reported = set()
    for it in items:
        vid = it["video_id"]
        if vid in drop:
            if vid not in reported:
                unresolved.append({"token": vid, "reason": drop[vid]})
                reported.add(vid)
            continue
        cached = youtube._EMBED_CACHE.get(vid)
        out_items.append({**it, **meta[vid],
                          "embeddable": (bool(cached.get("embeddable")) if isinstance(cached, dict) else None)})
    return {"sources": sources, "items": out_items, "pointers": pointers, "unresolved": unresolved}

"""Lane B (Discovery) views: every request the track, profile, discover and search views, the
related panel, likes and comments send, with the exact parameters the frontend uses
(static/js/views/{track,profile,discover,search}.js, static/js/discovery/*.js).

Privacy first: with Dan's crate canary (seeded crate 5) plus a private playlist and a crate row
holding a video nobody else has, nothing any Lane B view fetches for another viewer mentions it.
"""

import json

import pytest

from test_security_backend import db, db_path, demo, fakes  # noqa: F401  (fixtures are used by name)
from test_ops import ok, post_ops

CANARY_TITLE = "LANE B CANARY TITLE"
CANARY_VIDEO = "LANEBCANARY"
CANARY_NOTE = "lane b canary note"
TYCHO = "c_56qQqxvm4"


def _canary(client):
    """Dan (1): a private playlist and his crate (5) both hold CANARY_VIDEO."""
    pid = ok(client.post("/api/playlists", json={"user_id": 1, "title": CANARY_TITLE, "visibility": "private"}))["playlist_id"]
    ok(post_ops(client, 1, [{"kind": "insert", "playlist_id": pid, "items": [
        {"video_id": CANARY_VIDEO, "note": CANARY_NOTE}, {"video_id": TYCHO, "note": CANARY_NOTE}]}]))
    ok(post_ops(client, 1, [{"kind": "insert", "playlist_id": 5, "items": [{"video_id": CANARY_VIDEO, "note": CANARY_NOTE}]}]))
    return pid


def lane_b_views(client, viewer):
    """What Lane B's views fetch, as `viewer` (demo mode adds user_id to every GET)."""
    v = {"user_id": viewer}
    out = {}
    for vid in (TYCHO, "0PsOJ_fRckg", "6647smeduGI"):
        out[f"track.{vid}"] = ok(client.get(f"/api/songs/{vid}", params=v))
    for handle in ("dan", "maya", "lucas", "julian"):
        out[f"profile.{handle}"] = ok(client.get(f"/api/users/by-handle/{handle}", params=v))
    for tab in ("playlists", "tracks", "people"):
        out[f"discover.{tab}"] = ok(client.get("/api/discover", params={**v, "tab": tab, "offset": 0, "limit": 20}))
    out["discover.popular"] = ok(client.get("/api/discover", params={**v, "tab": "playlists", "sort": "popular", "offset": 0, "limit": 20}))
    for q in ("canary", "lane", "kexp", "a"):
        out[f"search.{q}"] = ok(client.get("/api/search", params={**v, "q": q, "type": "all", "limit": 10}))
        for group in ("playlists", "tracks", "people"):
            out[f"search.{q}.{group}"] = ok(client.get("/api/search", params={**v, "q": q, "type": group, "limit": 25}))
    for pid in (1, 2, 3, 4):
        out[f"related.{pid}"] = ok(client.get(f"/api/playlists/{pid}/related", params={**v, "limit": 12, "offset": 0}))
    for target_type, target_id in (("profile", "1"), ("song", TYCHO), ("playlist", "4")):
        out[f"comments.{target_type}"] = ok(client.get("/api/comments", params={**v, "target_type": target_type, "target_id": target_id}))
    return out


@pytest.mark.parametrize("viewer", [2, 3])
def test_lane_b_views_never_show_a_crate_or_private_canary(demo, viewer, monkeypatch):
    import app.main

    monkeypatch.setitem(app.main.RATE_LIMITS, "find", (1000, 1000.0))
    _canary(demo)
    blob = json.dumps(lane_b_views(demo, viewer))
    assert CANARY_VIDEO not in blob
    assert CANARY_TITLE not in blob
    assert CANARY_NOTE not in blob


def test_the_owner_still_sees_their_own_private_placement_on_the_track_page(demo):
    pid = _canary(demo)
    song = ok(demo.get(f"/api/songs/{CANARY_VIDEO}", params={"user_id": 1}))
    assert [p["playlist"]["id"] for p in song["playlists"]] == [pid]
    assert song["curators"] == []  # own private playlists never make you a curator
    assert demo.get(f"/api/songs/{CANARY_VIDEO}", params={"user_id": 3}).status_code == 404


def test_track_page_fields_the_view_reads(demo):
    song = ok(demo.get(f"/api/songs/{TYCHO}", params={"user_id": 3}))
    for key in ("title", "channel_title", "thumbnail_url", "my_likes", "like_count", "liked_by_users",
                "finder_username", "appearances", "playlist_count", "playlists", "curators", "alongside",
                "comments", "comments_has_more"):
        assert key in song, key
    by_pid = {a["playlist_id"]: a for a in song["appearances"]}
    for entry in song["playlists"]:
        # The view links each playlist to ?track=<first placement> and plays from first_position.
        a = by_pid[entry["playlist"]["id"]]
        assert entry["first_position"] >= 1 and a["track_entry_id"] > 0
        assert entry["playlist"]["user"]["username"]
    assert all("together_count" in a for a in song["alongside"])


def test_like_control_round_trip(demo):
    """Plain click likes once, Shift-click adds one, a plain click on a liked item resets."""
    url = "/api/playlists/2/like"
    base = ok(demo.post(url, json={"user_id": 4, "reset": True, "count": 0}))  # julian's seeded like, cleared
    assert base["my_likes"] == 0
    first = ok(demo.post(url, json={"user_id": 4, "count": 1}))
    assert first["my_likes"] == 1
    more = ok(demo.post(url, json={"user_id": 4, "count": 1}))
    assert more["my_likes"] == 2 and more["like_count"] == first["like_count"] + 1
    reset = ok(demo.post(url, json={"user_id": 4, "reset": True, "count": 0}))
    assert reset["my_likes"] == 0 and reset["like_count"] == base["like_count"]
    # Crates are never likeable, so the ♡ is never offered there.
    assert demo.post("/api/playlists/5/like", json={"user_id": 1, "count": 1}).status_code == 400


def test_comment_post_and_older_paging(demo, monkeypatch):
    import app.main

    monkeypatch.setitem(app.main.RATE_LIMITS, "comment", (1000, 1000.0))
    for i in range(53):
        res = ok(demo.post("/api/comments", json={"author_user_id": 2, "target_type": "profile",
                                                 "target_id": "3", "body": f"note {i}"}))
    assert res["comments_has_more"] is True and len(res["comments"]) == 50
    assert res["comments"][-1]["body"] == "note 52" and res["comments"][-1]["author_username"] == "lucas"
    older = ok(demo.get("/api/comments", params={"target_type": "profile", "target_id": "3",
                                                 "before_id": res["comments"][0]["id"], "user_id": 3}))
    assert older["has_more"] is False
    assert [c["body"] for c in older["comments"]] == ["note 0", "note 1", "note 2"]
    profile = ok(demo.get("/api/users/by-handle/maya", params={"user_id": 1}))
    assert profile["comments"][-1]["body"] == "note 52"


def test_profile_edit_is_own_only(demo):
    assert demo.put("/api/users/3", json={"user_id": 3, "display_name": "Maya L", "bio": "dub"}).status_code == 200
    me = ok(demo.get("/api/users/by-handle/maya", params={"user_id": 3}))["user"]
    assert me["display_name"] == "Maya L" and me["bio"] == "dub"


def test_profile_payload_carries_what_the_badge_section_needs_and_nothing_private(demo):
    prof = ok(demo.get("/api/users/by-handle/dan", params={"user_id": 3}))
    assert isinstance(prof["earned_badges"], list) and isinstance(prof["badge_catalog"], list)
    for b in prof["badge_catalog"]:
        assert {"key", "name", "icon", "description"} <= set(b)
    for key in ("password_hash", "session_epoch", "handle_chosen_at"):
        assert key not in prof["user"]
    assert all(p["kind"] != "crate" for p in prof["playlists"])

"""Crates, private and unlisted playlists never leak into public views, counts or badges.

Dan's seeded crate (playlist 5) holds two rows copied from Maya and Julian. They are the
canary: every public view must look exactly the same as if they did not exist.
"""

import json

import pytest

from test_security_backend import db, db_path, demo, fakes  # noqa: F401  (fixtures are used by name)
from test_ops import ok, order, post_ops, q

SEED_VIDEOS = ["0PsOJ_fRckg", "TNsSBhl_2LI", "c_56qQqxvm4", "4fHw4GeW3EU", "w2oGFEsaG5c",
               "-y9rkNXBuhg", "6647smeduGI", "BI2Et19vDCM", "S8fFo4REHOE"]
CANARY_TITLE = "CANARY PRIVATE TITLE"
CANARY_VIDEO = "CANARYVID01"


def public_views(client, viewer):
    """Every read endpoint that shows other people's stuff, as `viewer` sees it."""
    out = {
        "feed": client.get(f"/api/feed?user_id={viewer}").json(),
        "bootstrap.users": client.get(f"/api/bootstrap?user_id={viewer}").json()["users"],
    }
    for vid in SEED_VIDEOS:
        out[f"song.{vid}"] = client.get(f"/api/songs/{vid}?user_id={viewer}").json()
    for uid in (1, 2, 3, 4):
        if uid != viewer:
            out[f"profile.{uid}"] = client.get(f"/api/users/{uid}?viewer_id={viewer}").json()
    for pid in (1, 2, 3, 4):
        detail = client.get(f"/api/playlists/{pid}?user_id={viewer}").json()
        detail.pop("version", None)
        out[f"playlist.{pid}"] = detail
    out["comments"] = client.get(f"/api/comments?target_type=playlist&target_id=1&user_id={viewer}").json()
    return out


def badge_state():
    from app.badges import compute_user_metrics

    conn = db()
    try:
        badges = sorted(tuple(r) for r in conn.execute("SELECT user_id, badge_key, context_note FROM user_badges"))
        metrics = {u: {k: v for k, v in compute_user_metrics(conn, u).items()} for u in (1, 2, 3, 4)}
    finally:
        conn.close()
    return badges, metrics


def drop_crate_rows():
    conn = db()
    conn.execute("DELETE FROM playlist_tracks WHERE playlist_id IN (SELECT id FROM playlists WHERE kind = 'crate')")
    conn.commit()
    conn.close()


@pytest.mark.parametrize("viewer", [1, 2, 3])
def test_seeded_crate_rows_change_no_public_view(demo, viewer):
    assert order(5) == [16, 17]
    with_crate = public_views(demo, viewer)
    badges_with = badge_state()
    drop_crate_rows()
    assert public_views(demo, viewer) == with_crate
    assert badge_state() == badges_with


def test_unfiltered_counts_would_have_seen_the_canary(demo):
    # Guard against a vacuous privacy test: the raw tables do contain the credited crate rows.
    raw = q("SELECT COUNT(*) FROM playlist_tracks WHERE source_user_id = 3 AND added_by_user_id != 3")[0][0]
    listed = q("""SELECT COUNT(*) FROM playlist_tracks pt JOIN playlists p ON p.id = pt.playlist_id
                  WHERE pt.source_user_id = 3 AND pt.added_by_user_id != 3
                    AND p.kind = 'playlist' AND p.visibility = 'public'""")[0][0]
    assert raw == listed + 1
    maya = next(u for u in ok(demo.get("/api/bootstrap?user_id=2"))["users"] if u["id"] == 3)
    assert maya["times_grabbed"] == listed


def test_crate_is_owner_only(demo):
    assert demo.get("/api/playlists/5?user_id=3").status_code == 404
    mine = ok(demo.get("/api/playlists/5?user_id=1"))
    assert mine["kind"] == "crate" and [t["id"] for t in mine["tracks"]] == [16, 17] and mine["comments"] == []
    assert demo.post("/api/playlists/5/like", json={"user_id": 1}).json()["code"] == "not_likeable"
    assert demo.post("/api/playlists/5/like", json={"user_id": 3}).status_code == 404
    assert demo.post("/api/comments", json={"author_user_id": 1, "target_type": "playlist", "target_id": "5",
                                            "body": "hi"}).status_code == 400
    assert demo.post("/api/comments", json={"author_user_id": 3, "target_type": "playlist", "target_id": "5",
                                            "body": "hi"}).status_code == 404
    assert demo.get("/api/comments?target_type=playlist&target_id=5&user_id=3").status_code == 404
    assert demo.post("/api/playlists/5/play", json={"user_id": 3}).status_code == 404
    plays = q("SELECT total_plays FROM songs WHERE video_id = 'c_56qQqxvm4'")[0][0]
    res = ok(demo.post("/api/playlists/5/play", json={"user_id": 1, "video_id": "c_56qQqxvm4"}))
    assert res["counted"] is False
    assert q("SELECT total_plays FROM songs WHERE video_id = 'c_56qQqxvm4'")[0][0] == plays
    # The crate is in the owner's bootstrap (first) and never in a profile's playlists.
    boot = ok(demo.get("/api/bootstrap?user_id=1"))["current_user"]
    assert boot["crate_id"] == 5 and boot["playlists"][0]["id"] == 5
    assert 5 not in [p["id"] for p in ok(demo.get("/api/users/1?viewer_id=1"))["playlists"]]
    assert ok(demo.get("/api/bootstrap?user_id=3"))["current_user"]["crate_id"] == 7


def _make_private_canary(client):
    pid = ok(client.post("/api/playlists", json={"user_id": 1, "title": CANARY_TITLE, "visibility": "private"}))["playlist_id"]
    ok(post_ops(client, 1, [{"kind": "insert", "playlist_id": pid, "items": [
        {"video_id": CANARY_VIDEO, "note": "canary note"},
        {"video_id": "c_56qQqxvm4", "ref": {"track_id": 4}},  # a grab from Maya, credited privately
    ]}]))
    ok(post_ops(client, 1, [{"kind": "set_meta", "playlist_id": 5, "set": {"title": CANARY_TITLE + " CRATE"}}]))
    ok(post_ops(client, 1, [{"kind": "insert", "playlist_id": 5, "items": [{"video_id": CANARY_VIDEO}]}]))
    return pid


def test_private_playlist_and_crate_never_appear_in_any_public_endpoint(demo):
    before = {v: public_views(demo, v) for v in (2, 3)}
    badges_before = badge_state()
    pid = _make_private_canary(demo)
    for viewer in (2, 3):
        views = public_views(demo, viewer)
        blob = json.dumps(views)
        assert CANARY_TITLE not in blob and CANARY_VIDEO not in blob and "canary note" not in blob
        # Grab counts and everything else are unchanged.
        assert views == before[viewer]
        assert demo.get(f"/api/playlists/{pid}?user_id={viewer}").status_code == 404
        assert demo.get(f"/api/songs/{CANARY_VIDEO}?user_id={viewer}").status_code == 404
        assert demo.post(f"/api/songs/{CANARY_VIDEO}/like", json={"user_id": viewer}).status_code == 404
        hidden = demo.post("/api/resolve-paste", json={"user_id": viewer, "text": f"http://localhost/p/{pid}"}).json()
        assert hidden["items"] == [] and hidden["unresolved"][0]["reason"] == "not_found"
    assert badge_state() == badges_before
    # Nothing about it in the activity log, and the canary song has no finder.
    assert not [r for r in q("SELECT summary FROM activity_log") if CANARY_TITLE in r[0] or "CANARY" in r[0]]
    assert q("SELECT first_added_by_user_id FROM songs WHERE video_id = ?", CANARY_VIDEO)[0][0] is None
    # The owner sees it: detail, song page and own profile (with its visibility).
    assert ok(demo.get(f"/api/playlists/{pid}?user_id=1"))["visibility"] == "private"
    assert ok(demo.get(f"/api/songs/{CANARY_VIDEO}?user_id=1"))["finder_username"] is None
    own = ok(demo.get("/api/users/1?viewer_id=1"))
    assert [p["visibility"] for p in own["playlists"] if p["id"] == pid] == ["private"]
    assert CANARY_VIDEO in json.dumps(own) or CANARY_TITLE in json.dumps(own)


def test_badges_wait_for_the_public_placement(demo):
    uid = ok(demo.post("/api/auth/login", json={"username": "elena"}))["user_id"]
    crate = ok(demo.get(f"/api/bootstrap?user_id={uid}"))["current_user"]["crate_id"]
    maya_metrics = badge_state()[1][3]
    res = ok(post_ops(demo, uid, [{"kind": "insert", "playlist_id": crate, "items": [
        {"video_id": "c_56qQqxvm4", "ref": {"track_id": 4}}, {"video_id": "4fHw4GeW3EU", "ref": {"track_id": 5}},
        {"video_id": "w2oGFEsaG5c", "ref": {"track_id": 6}}, {"video_id": "NEWVIDEO099"}]}]))
    assert res["new_badges"] == []
    assert q("SELECT COUNT(*) FROM user_badges WHERE user_id = ?", uid)[0][0] == 0
    assert badge_state()[1][3] == maya_metrics
    promoted = ok(demo.post("/api/me/crate/promote", json={"user_id": uid, "title": "Elena's picks"}), 201)
    keys = {b["key"] for b in promoted["new_badges"]}
    assert {"crate_borrower_1", "cross_pollinator_3", "first_finder_1"} <= keys
    assert badge_state()[1][3]["tracks_taken_from_me"] == maya_metrics["tracks_taken_from_me"] + 3


def test_unlisted_opens_by_id_but_is_never_listed_or_counted(demo):
    before = {v: public_views(demo, v) for v in (2, 3)}
    badges_before = badge_state()
    pid = ok(demo.post("/api/playlists", json={"user_id": 1, "title": "UNLISTED MIX", "visibility": "unlisted"}))["playlist_id"]
    ok(post_ops(demo, 1, [{"kind": "insert", "playlist_id": pid, "items": [
        {"video_id": "4fHw4GeW3EU", "ref": {"track_id": 5}}, {"video_id": "NEWVIDEO050"}]}]))
    for viewer in (2, 3):
        assert public_views(demo, viewer) == before[viewer]
        detail = ok(demo.get(f"/api/playlists/{pid}?user_id={viewer}"))
        assert detail["visibility"] == "unlisted" and len(detail["tracks"]) == 2
        assert "UNLISTED MIX" not in json.dumps(public_views(demo, viewer))
    assert badge_state() == badges_before
    # Someone grabs from the unlisted playlist: their row credits dan, but doesn't name or link it.
    ok(post_ops(demo, 3, [{"kind": "insert", "playlist_id": 2, "items": [
        {"video_id": "4fHw4GeW3EU", "ref": {"track_id": order(pid)[0]}}]}]))
    maya_pl = ok(demo.get("/api/playlists/2?user_id=4"))
    grabbed = maya_pl["tracks"][-1]
    assert grabbed["source_username"] == "dan"
    assert grabbed["source_playlist_id"] is None and grabbed["source_playlist_title"] is None
    # The unlisted playlist's own owner sees the link.
    assert ok(demo.get("/api/playlists/2?user_id=1"))["tracks"][-1]["source_playlist_title"] == "UNLISTED MIX"


def test_going_private_hides_old_activity_and_listings(demo):
    feed = ok(demo.get("/api/feed?user_id=2"))
    assert any(a["link_type"] == "playlist" and a["link_id"] == "3" for a in feed["activity"])
    ok(post_ops(demo, 4, [{"kind": "set_meta", "playlist_id": 3, "set": {"visibility": "private"}}]))
    feed = ok(demo.get("/api/feed?user_id=2"))
    assert 3 not in [p["id"] for p in feed["playlists"]]
    assert not any(a["link_type"] == "playlist" and a["link_id"] == "3" for a in feed["activity"])
    julian = next(c for c in feed["curators"] if c["id"] == 4)
    assert julian["playlist_count"] == 0
    assert demo.get("/api/playlists/3?user_id=2").status_code == 404
    # Song page: the private appearance is gone for others but still there for the owner.
    assert demo.get("/api/songs/-y9rkNXBuhg?user_id=2").status_code == 404  # only in julian's (now private) playlist
    assert ok(demo.get("/api/songs/-y9rkNXBuhg?user_id=4"))["appearances"][0]["playlist_id"] == 3
    # Rows grabbed from it elsewhere keep "via @julian" but drop the playlist link.
    lucas_view = ok(demo.get("/api/playlists/2?user_id=2"))
    assert all(t["source_playlist_id"] != 3 for t in lucas_view["tracks"])

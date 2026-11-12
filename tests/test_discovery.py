"""Phase 1b discovery backend (docs/phase1b-shell.md §3): related playlists, song hops,
taste neighbours, home, discover and search. Privacy first: crates, private and unlisted
playlists never shape what other people see.

Seed recap (tests rely on it):
  playlist 1 lucas  [0PsOJ, TNsSB, c_56q]          updated 10-28
  playlist 2 maya   [c_56q, 4fHw4, w2oGF, 0PsOJ]   updated 10-30
  playlist 3 julian [-y9rk, 6647s, TNsSB, 4fHw4]   updated 10-31
  playlist 4 dan    [BI2Et, S8fFo, 0PsOJ, w2oGF]   updated 11-01
  crate 5 dan       [c_56q (from maya), 6647s (from julian)]  <- the privacy canary
"""

import pytest

from test_security_backend import db, db_path, demo, fakes, google, google_sign_in  # noqa: F401
from test_ops import insert, ok, post_ops, q

KHRUANGBIN = "0PsOJ_fRckg"
MEN_I_TRUST = "TNsSBhl_2LI"
TYCHO = "c_56qQqxvm4"
KIASMOS = "4fHw4GeW3EU"
BICEP = "w2oGFEsaG5c"
PARCELS = "-y9rkNXBuhg"
ALVVAYS = "6647smeduGI"
CARIBOU = "BI2Et19vDCM"
FLOATING = "S8fFo4REHOE"


def new_playlist(client, uid, title, visibility="public", videos=()):
    res = client.post("/api/playlists", json={"user_id": uid, "title": title, "visibility": visibility})
    assert res.status_code in (200, 201), res.text
    pid = res.json()["playlist_id"]
    if videos:
        ok(post_ops(client, uid, [insert(pid, *videos)]))
    return pid


def set_visibility(client, uid, pid, visibility):
    ok(post_ops(client, uid, [{"kind": "set_meta", "playlist_id": pid, "set": {"visibility": visibility}}]))


def drop_crate_rows():
    conn = db()
    conn.execute("DELETE FROM playlist_tracks WHERE playlist_id IN (SELECT id FROM playlists WHERE kind = 'crate')")
    conn.commit()
    conn.close()


def crate_ids():
    return {r[0] for r in q("SELECT id FROM playlists WHERE kind = 'crate'")}


def related(client, pid, viewer=1, **params):
    return client.get(f"/api/playlists/{pid}/related", params={"user_id": viewer, **params})


def ids_of(cards):
    return [c["id"] for c in cards]


# --- shared shapes ----------------------------------------------------------------------

USER_MINI_KEYS = {"id", "username", "display_name", "avatar_color"}
TRACK_MINI_KEYS = {"id", "video_id", "position", "title", "channel_title", "thumbnail_url",
                   "start_seconds", "end_seconds"}
CARD_KEYS = {"id", "kind", "visibility", "version", "title", "description", "track_count", "like_count",
             "plays", "related_count", "updated_at", "user", "preview"}


def test_playlist_card_shape_and_preview_is_best_three_in_playlist_order(demo):
    cards = ok(demo.get("/api/discover", params={"user_id": 2}))["playlists"]
    card = next(c for c in cards if c["id"] == 4)
    assert set(card) == CARD_KEYS
    assert set(card["user"]) == USER_MINI_KEYS and card["user"]["username"] == "dan"
    assert all(set(t) == TRACK_MINI_KEYS for t in card["preview"])
    # Song likes: Khruangbin 15, Bicep 9, Caribou 8, Floating Points 0 -> those three, in playlist order.
    assert [t["video_id"] for t in card["preview"]] == [CARIBOU, KHRUANGBIN, BICEP]
    assert [t["position"] for t in card["preview"]] == [1, 3, 4]
    # A playlist with <= 3 tracks returns all of them.
    small = next(c for c in cards if c["id"] == 1)
    assert [t["position"] for t in small["preview"]] == [1, 2, 3]


def test_preview_tiebreak_is_plays_then_position(demo):
    conn = db()
    conn.execute("DELETE FROM song_likes")
    conn.execute("UPDATE songs SET total_plays = 0")
    conn.execute("UPDATE songs SET total_plays = 5 WHERE video_id = ?", (BICEP,))
    conn.commit()
    conn.close()
    card = next(c for c in ok(demo.get("/api/discover"))["playlists"] if c["id"] == 4)
    # Bicep by plays, then the first two by position, returned in playlist order.
    assert [t["video_id"] for t in card["preview"]] == [CARIBOU, FLOATING, BICEP]


# --- related playlists (§3.5) ---------------------------------------------------------------

def test_related_ranks_by_shared_count_with_shared_tracks_in_their_order(demo):
    body = ok(related(demo, 2))
    assert body["playlist_id"] == 2 and body["next_offset"] is None
    # 4 shares Khruangbin + Bicep, 1 shares Tycho + Khruangbin, 3 shares Kiasmos.
    # Ties by updated_at DESC: 4 (11-01) before 1 (10-28).
    assert [(r["playlist"]["id"], r["shared_count"]) for r in body["related"]] == [(4, 2), (1, 2), (3, 1)]
    r4 = body["related"][0]
    assert [(s["video_id"], s["position"]) for s in r4["shared"]] == [(KHRUANGBIN, 3), (BICEP, 4)]
    r1 = body["related"][1]
    assert [(s["video_id"], s["position"]) for s in r1["shared"]] == [(KHRUANGBIN, 1), (TYCHO, 3)]
    assert "preview" not in r4["playlist"] and set(r4["playlist"]) == CARD_KEYS - {"preview"}
    assert body["total"] == 3 == ok(demo.get("/api/playlists/2?user_id=1"))["related_count"]


def test_related_shared_list_caps_at_four_and_dedupes_repeats(demo):
    pid = new_playlist(demo, 2, "Mirror", videos=[BICEP, BICEP, KHRUANGBIN, KIASMOS, TYCHO, PARCELS])
    body = ok(related(demo, 2))
    row = next(r for r in body["related"] if r["playlist"]["id"] == pid)
    assert row["shared_count"] == 4 and body["related"][0]["playlist"]["id"] == pid
    assert [(s["video_id"], s["position"]) for s in row["shared"]] == [
        (BICEP, 1), (KHRUANGBIN, 3), (KIASMOS, 4), (TYCHO, 5)]


def test_related_paging(demo):
    first = ok(related(demo, 2, limit=2))
    assert len(first["related"]) == 2 and first["next_offset"] == 2 and first["total"] == 3
    second = ok(related(demo, 2, limit=2, offset=2))
    assert [r["playlist"]["id"] for r in second["related"]] == [3] and second["next_offset"] is None
    assert related(demo, 2, limit=0).status_code == 422
    assert related(demo, 2, limit=51).status_code == 422
    assert related(demo, 2, offset=-1).status_code == 422


@pytest.mark.parametrize("visibility", ["private", "unlisted"])
def test_hidden_playlists_leave_related_and_related_count(demo, visibility):
    before = ok(demo.get("/api/playlists/2?user_id=1"))["related_count"]
    set_visibility(demo, 2, 1, visibility)
    body = ok(related(demo, 2))
    assert 1 not in [r["playlist"]["id"] for r in body["related"]]
    assert body["total"] == before - 1 == ok(demo.get("/api/playlists/2?user_id=1"))["related_count"]
    # The owner sees it gone too: the list only ever names listed playlists.
    assert 1 not in [r["playlist"]["id"] for r in ok(related(demo, 2, viewer=2))["related"]]
    card4 = next(c for c in ok(demo.get("/api/discover"))["playlists"] if c["id"] == 4)
    assert card4["related_count"] == 1  # only maya's playlist 2 still shares with dan's 4


def test_crates_never_appear_in_related_and_do_not_change_counts(demo):
    def snapshot():
        return {pid: ok(related(demo, pid, viewer=v)) for pid in (1, 2, 3, 4) for v in (2,)}

    with_crate = snapshot()
    for body in with_crate.values():
        assert not crate_ids() & {r["playlist"]["id"] for r in body["related"]}
    drop_crate_rows()
    assert snapshot() == with_crate


def test_related_from_a_crate_or_private_playlist_names_only_listed_playlists(demo):
    # Dan's crate (5) holds Tycho and Alvvays: it is related to 1, 2 (Tycho) and 3 (Alvvays).
    body = ok(related(demo, 5, viewer=1))
    assert sorted(r["playlist"]["id"] for r in body["related"]) == [1, 2, 3]
    assert related(demo, 5, viewer=3).status_code == 404

    secret = new_playlist(demo, 3, "Secret", "private", videos=[PARCELS])
    other = new_playlist(demo, 4, "Julian private", "private", videos=[PARCELS])
    assert related(demo, secret, viewer=1).status_code == 404
    mine = ok(related(demo, secret, viewer=3))
    assert [r["playlist"]["id"] for r in mine["related"]] == [3]  # not julian's private one
    assert other not in [r["playlist"]["id"] for r in mine["related"]]


def test_related_unknown_and_unlisted(demo):
    assert related(demo, 999).status_code == 404
    assert related(demo, 999).json()["code"] == "playlist_not_found"
    set_visibility(demo, 2, 1, "unlisted")
    assert related(demo, 1, viewer=3).status_code == 200  # openable by link, so its related list is too


# --- song page (§3.6) ---------------------------------------------------------------------------

def test_song_page_hops(demo):
    song = ok(demo.get(f"/api/songs/{KHRUANGBIN}?user_id=2"))
    assert "appearances" in song  # back-compat
    assert song["playlist_count"] == 3
    assert [(p["playlist"]["id"], p["first_position"], p["occurrences"]) for p in song["playlists"]] == [
        (4, 3, 1), (2, 4, 1), (1, 1, 1)]  # listed, then updated_at DESC
    assert song["playlists"][0]["note"] == "Snagged from @lucas's Sunday Coffee mix!"
    assert "preview" not in song["playlists"][0]["playlist"]
    assert [(c["user"]["username"], c["playlist_count"]) for c in song["curators"]] == [
        ("lucas", 1), ("maya", 1), ("dan", 1)]
    along = {a["video_id"]: a["together_count"] for a in song["alongside"]}
    assert KHRUANGBIN not in along
    assert along[BICEP] == 2 and along[TYCHO] == 2 and along[CARIBOU] == 1
    assert [a["together_count"] for a in song["alongside"]] == sorted(along.values(), reverse=True)


def test_song_occurrences_and_first_note(demo):
    pid = new_playlist(demo, 2, "Twice", videos=[KIASMOS, KHRUANGBIN, KHRUANGBIN])
    ok(post_ops(demo, 2, [{"kind": "update", "playlist_id": pid,
                           "track_id": q("SELECT MAX(id) FROM playlist_tracks")[0][0], "set": {"note": "second copy"}}]))
    song = ok(demo.get(f"/api/songs/{KHRUANGBIN}?user_id=1"))
    entry = next(p for p in song["playlists"] if p["playlist"]["id"] == pid)
    assert (entry["occurrences"], entry["first_position"], entry["note"]) == (2, 2, "second copy")
    assert next(c for c in song["curators"] if c["user"]["username"] == "lucas")["playlist_count"] == 2


def test_song_page_ignores_crates_and_other_peoples_private_playlists(demo):
    secret = new_playlist(demo, 3, "Maya private", "private", videos=[CARIBOU, PARCELS])
    for viewer in (1, 2, 4):
        song = ok(demo.get(f"/api/songs/{CARIBOU}?user_id={viewer}"))
        assert secret not in [p["playlist"]["id"] for p in song["playlists"]]
        assert "maya" not in [c["user"]["username"] for c in song["curators"]]
        assert PARCELS not in [a["video_id"] for a in song["alongside"]]
        assert song["playlist_count"] == 1
    mine = ok(demo.get(f"/api/songs/{CARIBOU}?user_id=3"))
    assert [p["playlist"]["id"] for p in mine["playlists"]] == [4, secret]  # listed first
    assert mine["playlists"][1]["playlist"]["visibility"] == "private"
    assert "maya" not in [c["user"]["username"] for c in mine["curators"]]
    assert mine["playlist_count"] == 1
    assert PARCELS not in [a["video_id"] for a in mine["alongside"]]

    # Dan's crate holds Tycho: never in Tycho's playlists, curators or alongside, even for Dan.
    for viewer in (1, 2, 3):
        tycho = ok(demo.get(f"/api/songs/{TYCHO}?user_id={viewer}"))
        assert not crate_ids() & {p["playlist"]["id"] for p in tycho["playlists"]}
        assert "dan" not in [c["user"]["username"] for c in tycho["curators"]]
        assert ALVVAYS not in [a["video_id"] for a in tycho["alongside"]]
        assert tycho["playlist_count"] == 2


def test_song_only_in_a_crate_is_404_for_others(demo):
    ok(post_ops(demo, 1, [insert(5, "CRATEONLY01")]))
    assert demo.get("/api/songs/CRATEONLY01?user_id=2").status_code == 404
    mine = ok(demo.get("/api/songs/CRATEONLY01?user_id=1"))
    assert mine["playlists"] == [] and mine["curators"] == [] and mine["playlist_count"] == 0


# --- profiles (§3.7) ----------------------------------------------------------------------------

def test_profile_by_handle_matches_by_id(demo):
    by_id = ok(demo.get("/api/users/3?user_id=1"))
    assert ok(demo.get("/api/users/by-handle/maya?user_id=1")) == by_id
    assert ok(demo.get("/api/users/by-handle/MAYA?user_id=1")) == by_id
    assert ok(demo.get("/api/users/3?viewer_id=1")) == by_id  # the old parameter name still works
    for p in by_id["playlists"]:
        assert {"related_count", "preview", "tracks"} <= set(p)
    assert "handle_chosen_at" not in by_id["user"]


@pytest.mark.parametrize("bad", ["Bad-Handle!", "ab", "x" * 21, "nobody_here", "dan%20x"])
def test_profile_by_handle_bad_shapes_404(demo, bad):
    assert demo.get(f"/api/users/by-handle/{bad}").status_code == 404


def test_old_handle_resolves_to_the_current_user(demo):
    ok(demo.put("/api/me/handle", json={"user_id": 3, "handle": "maya_k"}))
    prof = ok(demo.get("/api/users/by-handle/maya?user_id=1"))
    assert prof["user"]["id"] == 3 and prof["user"]["username"] == "maya_k"
    assert ok(demo.get("/api/users/by-handle/maya_k?user_id=1"))["user"]["id"] == 3


def test_profile_neighbours_are_listed_only_and_viewer_independent(demo):
    # A private playlist of Maya's overlapping Julian must not change her public neighbours.
    new_playlist(demo, 3, "Maya private", "private", videos=[PARCELS, ALVVAYS])
    views = [ok(demo.get(f"/api/users/3?user_id={v}"))["neighbours"] for v in (1, 2, 3, 4)]
    assert all(v == views[0] for v in views)
    assert [(n["user"]["username"], n["shared_count"]) for n in views[0]] == [
        ("dan", 2), ("lucas", 2), ("julian", 1)]
    assert all(set(n["user"]) == USER_MINI_KEYS for n in views[0])
    assert all(len(n["sample"]) <= 3 for n in views[0])
    # Her own home counts her private playlist: Julian now shares 3.
    home = ok(demo.get("/api/me/home?user_id=3"))
    assert ("julian", 3) in [(n["user"]["username"], n["shared_count"]) for n in home["neighbours"]]


def test_profile_neighbours_rank_ties_by_recent_shared_placement(demo):
    # Dan and Lucas both share 2 with Maya; Dan's shared placements are newer (10-29 vs 10-28).
    names = [n["user"]["username"] for n in ok(demo.get("/api/users/3?user_id=2"))["neighbours"]]
    assert names.index("dan") < names.index("lucas")
    # Give Lucas a third shared track: he moves ahead.
    ok(post_ops(demo, 2, [insert(1, KIASMOS)]))
    names = [n["user"]["username"] for n in ok(demo.get("/api/users/3?user_id=2"))["neighbours"]]
    assert names[0] == "lucas"


def test_shared_with_viewer(demo):
    # Dan's crate holds Alvvays (Julian's): the crate must not count.
    assert ok(demo.get("/api/users/4?user_id=1"))["shared_with_viewer"] == {"shared_count": 0, "sample": []}
    secret = new_playlist(demo, 1, "Dan private", "private", videos=[ALVVAYS, PARCELS])
    mine = ok(demo.get("/api/users/4?user_id=1"))["shared_with_viewer"]
    assert mine["shared_count"] == 2 and {s["video_id"] for s in mine["sample"]} == {ALVVAYS, PARCELS}
    # Nobody else's view of Julian changes, and Dan's own public profile doesn't count it either.
    assert ok(demo.get("/api/users/4?user_id=2"))["shared_with_viewer"]["shared_count"] == 1  # Men I Trust
    assert ok(demo.get("/api/users/1?user_id=4"))["shared_with_viewer"]["shared_count"] == 0
    assert ok(demo.get("/api/users/4?user_id=4"))["shared_with_viewer"] is None
    assert secret


def test_shared_with_viewer_is_null_when_anonymous(google):
    assert ok(google.get("/api/users/3"))["shared_with_viewer"] is None
    assert ok(google.get("/api/users/by-handle/maya"))["shared_with_viewer"] is None


# --- home (§3.2) ---------------------------------------------------------------------------------

def test_home_requires_sign_in_in_google_mode(google):
    res = google.get("/api/me/home")
    assert res.status_code == 401 and res.json()["code"] == "signed_out"
    google_sign_in(google)
    home = ok(google.get("/api/me/home"))
    assert home["user"]["needs_handle"] is True and home["playlists"] == []
    assert home["crate"]["track_count"] == 0 and home["neighbours"] == []


def test_home_shape_and_my_playlists_first(demo):
    newer = new_playlist(demo, 1, "Fresh one", "unlisted", videos=[KIASMOS])
    home = ok(demo.get("/api/me/home?user_id=1"))
    assert set(home) == {"user", "playlists", "crate", "dock", "undo", "neighbours", "fresh", "about_you", "activity"}
    assert home["user"]["username"] == "dan" and home["user"]["needs_handle"] is False
    assert ids_of(home["playlists"]) == [newer, 4]
    assert home["playlists"][0]["visibility"] == "unlisted"
    assert all(set(c) == CARD_KEYS for c in home["playlists"])
    p4 = home["playlists"][1]
    assert len(p4["preview"]) == 3
    assert [t["position"] for t in p4["preview"]] == sorted(t["position"] for t in p4["preview"])
    assert home["dock"] == ok(demo.get("/api/me/dock?user_id=1"))
    assert home["undo"] == ok(demo.get("/api/me/undo?user_id=1"))


def test_home_crate_block_holds_the_canary_rows(demo):
    crate = ok(demo.get("/api/me/home?user_id=1"))["crate"]
    assert crate["id"] == 5 and crate["track_count"] == 2
    assert [(t["id"], t["position"], t["source_username"]) for t in crate["recent"]] == [
        (17, 2, "julian"), (16, 1, "maya")]
    assert all(TRACK_MINI_KEYS <= set(t) for t in crate["recent"])
    # Nobody else's home mentions Dan's crate.
    for uid in (2, 3, 4):
        other = ok(demo.get(f"/api/me/home?user_id={uid}"))
        assert other["crate"]["id"] != 5


def test_home_fresh_excludes_what_i_already_have_including_my_crate(demo):
    home = ok(demo.get("/api/me/home?user_id=1"))
    mine = {r[0] for r in q("""SELECT pt.video_id FROM playlist_tracks pt JOIN playlists p ON p.id = pt.playlist_id
                               WHERE p.user_id = 1""")}
    assert TYCHO in mine  # only via the crate: the exclusion is not vacuous
    vids = [f["track"]["video_id"] for f in home["fresh"]]
    assert vids and not set(vids) & mine
    assert len(vids) == len(set(vids)) <= 10
    assert set(vids) == {KIASMOS, MEN_I_TRUST}  # from maya and lucas, Dan's neighbours
    assert [f["added_at"] for f in home["fresh"]] == sorted((f["added_at"] for f in home["fresh"]), reverse=True)
    first = home["fresh"][0]
    assert set(first) == {"track", "playlist", "user", "added_at", "playlist_count"}
    assert first["playlist"] == {"id": 2, "title": "2AM Terminal Glow (Coding Mix)"} and first["user"]["username"] == "maya"


def test_home_fresh_ignores_neighbours_private_playlists(demo):
    new_playlist(demo, 3, "Maya private", "private", videos=["PRIVATEVID1"])
    vids = [f["track"]["video_id"] for f in ok(demo.get("/api/me/home?user_id=1"))["fresh"]]
    assert "PRIVATEVID1" not in vids


def test_home_activity_has_no_badge_lines_and_comes_from_neighbours(demo):
    conn = db()
    conn.execute("""INSERT INTO activity_log (actor_user_id, action_type, summary, link_type, link_id, created_at)
                    VALUES (3, 'earned_badge', 'earned a badge', '', '', '2030-01-01 00:00:00')""")
    conn.execute("""INSERT INTO activity_log (actor_user_id, action_type, summary, link_type, link_id, created_at)
                    VALUES (4, 'commented', 'julian said hi', 'profile', '4', '2030-01-01 00:00:01')""")
    conn.commit()
    conn.close()
    home = ok(demo.get("/api/me/home?user_id=1"))
    assert home["activity"] and len(home["activity"]) <= 20
    assert all(a["action_type"] != "earned_badge" for a in home["activity"])
    assert {a["actor_user_id"] for a in home["activity"]} <= {3, 2}  # Dan's neighbours: maya, lucas
    # A new user with no neighbours gets the global feed, still without badge lines.
    uid = ok(demo.post("/api/auth/login", json={"username": "newbie"}))["user_id"]
    fallback = ok(demo.get(f"/api/me/home?user_id={uid}"))
    assert fallback["neighbours"] == [] and fallback["fresh"] == []
    assert "julian said hi" in [a["summary"] for a in fallback["activity"]]
    assert all(a["action_type"] != "earned_badge" for a in fallback["activity"])


def test_home_about_you_lists_public_grabs_and_comments_only(demo):
    # Lucas grabs Dan's Caribou into his public playlist, Floating Points into a private one.
    q_row = q("SELECT id FROM playlist_tracks WHERE playlist_id = 4 AND video_id = ?", FLOATING)[0][0]
    grab = {"kind": "insert", "playlist_id": 1, "items": [{"video_id": CARIBOU, "ref": {"track_id": 12}}]}
    ok(post_ops(demo, 2, [grab]))
    secret = new_playlist(demo, 2, "Lucas private", "private")
    ok(post_ops(demo, 2, [{"kind": "insert", "playlist_id": secret,
                           "items": [{"video_id": FLOATING, "ref": {"track_id": q_row}}]}]))
    about = ok(demo.get("/api/me/home?user_id=1"))["about_you"]
    grabs = [a for a in about if a["type"] == "grab"]
    assert [(g["user"]["username"], g["video_id"], g["playlist"]["id"]) for g in grabs] == [("lucas", CARIBOU, 1)]
    comments = [a for a in about if a["type"] == "comment"]
    assert [(c["user"]["username"], c["playlist"]["id"]) for c in comments] == [("lucas", 4)]
    assert [a["at"] for a in about] == sorted((a["at"] for a in about), reverse=True)
    assert len(about) <= 10


def test_home_comments_by_me_are_not_about_me(demo):
    ok(demo.post("/api/comments", json={"author_user_id": 1, "target_type": "playlist", "target_id": "4", "body": "me"}))
    about = ok(demo.get("/api/me/home?user_id=1"))["about_you"]
    assert "me" not in [a.get("body") for a in about]


def test_needs_handle_flow(google):
    google_sign_in(google)
    boot = ok(google.get("/api/bootstrap"))
    assert boot["current_user"]["needs_handle"] is True
    handle = boot["current_user"]["username"]
    ok(google.put("/api/me/handle", json={"handle": handle}))  # "Keep @x"
    assert ok(google.get("/api/bootstrap"))["current_user"]["needs_handle"] is False
    assert ok(google.get("/api/me/home"))["user"]["needs_handle"] is False


def test_demo_users_are_never_prompted_for_a_handle(demo):
    assert ok(demo.get("/api/bootstrap?user_id=1"))["current_user"]["needs_handle"] is False
    uid = ok(demo.post("/api/auth/login", json={"username": "fresh_face"}))["user_id"]
    assert ok(demo.get(f"/api/bootstrap?user_id={uid}"))["current_user"]["needs_handle"] is False
    assert q("SELECT COUNT(*) FROM users WHERE handle_chosen_at IS NULL")[0][0] == 0


# --- discover (§3.3) -----------------------------------------------------------------------------

def _hidden_playlists(client):
    return {
        new_playlist(client, 3, "Private mix", "private", videos=[KIASMOS]),
        new_playlist(client, 3, "Unlisted mix", "unlisted", videos=[KIASMOS]),
        new_playlist(client, 3, "Empty mix"),
        *crate_ids(),
    }


@pytest.mark.parametrize("sort", ["fresh", "popular"])
def test_discover_playlists_never_lists_hidden_or_empty_playlists(demo, sort):
    hidden = _hidden_playlists(demo)
    seen, offset = [], 0
    while offset is not None:
        body = ok(demo.get("/api/discover", params={"sort": sort, "offset": offset, "limit": 2, "user_id": 3}))
        seen += ids_of(body["playlists"])
        assert ("activity" in body) == (offset == 0)
        offset = body["next_offset"]
    assert sorted(seen) == [1, 2, 3, 4] and not hidden & set(seen)


def test_discover_orders(demo):
    fresh = ok(demo.get("/api/discover"))
    assert fresh["tab"] == "playlists" and fresh["sort"] == "fresh" and fresh["offset"] == 0
    assert ids_of(fresh["playlists"]) == [4, 3, 2, 1]
    assert fresh["next_offset"] is None
    # popular = plays + 3 * likes: 2: 19+9=28, 1: 14+9=23, 4: 8+6=14, 3: 11+3=14 (tie -> updated_at DESC).
    rows = q("""SELECT p.id, p.plays + 3 * (SELECT COALESCE(SUM(count), 0) FROM playlist_likes WHERE playlist_id = p.id)
                FROM playlists p WHERE p.kind = 'playlist' AND p.visibility = 'public'""")
    score = {r[0]: r[1] for r in rows}
    popular = ids_of(ok(demo.get("/api/discover?sort=popular"))["playlists"])
    assert [score[i] for i in popular] == sorted(score.values(), reverse=True)
    assert popular[0] == max(score, key=score.get)


def test_discover_activity_has_no_badges(demo):
    conn = db()
    conn.execute("""INSERT INTO activity_log (actor_user_id, action_type, summary, link_type, link_id, created_at)
                    VALUES (3, 'earned_badge', 'earned a badge', '', '', '2030-01-01 00:00:00')""")
    conn.commit()
    conn.close()
    act = ok(demo.get("/api/discover"))["activity"]
    assert act and len(act) <= 15 and all(a["action_type"] != "earned_badge" for a in act)


def test_discover_tracks(demo):
    ok(post_ops(demo, 1, [insert(5, "CRATEONLY01")]))
    body = ok(demo.get("/api/discover?tab=tracks&limit=50&user_id=2"))
    vids = [t["video_id"] for t in body["tracks"]]
    assert "CRATEONLY01" not in vids and body["next_offset"] is None
    first = body["tracks"][0]
    assert set(first) == {"video_id", "title", "channel_title", "thumbnail_url", "first_added_at", "finder",
                          "debut_playlist", "playlist_count", "like_count"}
    assert [t["first_added_at"] for t in body["tracks"]] == sorted((t["first_added_at"] for t in body["tracks"]),
                                                                    reverse=True)
    paged = ok(demo.get("/api/discover?tab=tracks&limit=3&offset=3&user_id=2"))
    assert [t["video_id"] for t in paged["tracks"]] == vids[3:6] and paged["next_offset"] == 6


def test_discover_tracks_hides_a_private_debut(demo):
    set_visibility(demo, 1, 4, "private")
    tracks = ok(demo.get("/api/discover?tab=tracks&limit=50&user_id=2"))["tracks"]
    kh = next(t for t in tracks if t["video_id"] == KHRUANGBIN)
    assert kh["debut_playlist"]["id"] == 1
    assert CARIBOU not in [t["video_id"] for t in tracks]  # only in a private playlist now


def test_discover_people(demo):
    hidden_owner = ok(demo.post("/api/auth/login", json={"username": "lurker"}))["user_id"]
    new_playlist(demo, hidden_owner, "Only private", "private", videos=[KIASMOS])
    body = ok(demo.get("/api/discover?tab=people"))
    people = body["people"]
    assert [p["user"]["username"] for p in people] == ["dan", "julian", "maya", "lucas"]  # last active first
    assert all("badges" not in p and "badges" not in p["user"] for p in people)
    assert set(people[0]) == {"user", "bio", "playlist_count", "track_count", "last_active_at"}
    # Counts are listed-only: Dan's crate doesn't count.
    assert (people[0]["playlist_count"], people[0]["track_count"]) == (1, 4)
    assert body["next_offset"] is None


def test_discover_validation_and_rate_limit(demo, monkeypatch):
    import app.main

    assert demo.get("/api/discover?tab=badges").status_code == 422
    assert demo.get("/api/discover?sort=random").status_code == 422
    assert demo.get("/api/discover?limit=51").status_code == 422
    assert demo.get("/api/discover?offset=-1").status_code == 422
    monkeypatch.setitem(app.main.RATE_LIMITS, "find", (3, 0.0001))
    app.main.reset_rate_limits()
    codes = [demo.get("/api/discover").status_code for _ in range(3)] + [demo.get("/api/search?q=x").status_code]
    assert codes == [200, 200, 200, 429]


# --- search (§3.4) -------------------------------------------------------------------------------

def search(client, query, viewer=1, **params):
    return client.get("/api/search", params={"q": query, "user_id": viewer, **params})


def test_search_finds_listed_playlists_by_title_and_description(demo):
    body = ok(search(demo, "terminal"))
    assert body["query"] == "terminal" and ids_of(body["playlists"]) == [2]
    assert set(body["playlists"][0]) == CARD_KEYS
    assert ids_of(ok(search(demo, "ESPRESSO"))["playlists"]) == [1]  # description, any case
    assert ok(search(demo, "  Basement \n  Velvet "))["query"] == "Basement Velvet"


def test_search_never_returns_hidden_playlists(demo):
    new_playlist(demo, 3, "Zebra private", "private", videos=[KIASMOS])
    new_playlist(demo, 3, "Zebra unlisted", "unlisted", videos=[KIASMOS])
    conn = db()
    conn.execute("UPDATE playlists SET title = 'Zebra crate' WHERE id = 5")
    conn.commit()
    conn.close()
    for viewer in (1, 3):
        assert ok(search(demo, "zebra", viewer))["playlists"] == []


def test_search_tracks_respect_song_visibility(demo):
    ok(post_ops(demo, 1, [insert(5, "CRATEONLY01")]))
    assert ok(search(demo, "CRATEONLY01", 2))["tracks"] == []
    found = ok(search(demo, "CRATEONLY01", 1))["tracks"]
    assert [t["video_id"] for t in found] == ["CRATEONLY01"] and found[0]["playlist_count"] == 0


def test_search_tracks_order_and_display_titles(demo):
    tracks = ok(search(demo, "kexp"))["tracks"]
    assert [t["video_id"] for t in tracks][0] == KHRUANGBIN
    assert [t["playlist_count"] for t in tracks] == sorted((t["playlist_count"] for t in tracks), reverse=True)
    assert set(tracks[0]) == {"video_id", "title", "channel_title", "thumbnail_url", "playlist_count"}
    # A per-row title in a listed playlist is searchable; in a private one or a crate it is not.
    tid = q("SELECT id FROM playlist_tracks WHERE playlist_id = 2 AND video_id = ?", KIASMOS)[0][0]
    ok(post_ops(demo, 3, [{"kind": "update", "playlist_id": 2, "track_id": tid, "set": {"display_title": "Glacier set"}}]))
    assert [t["video_id"] for t in ok(search(demo, "glacier", 2))["tracks"]] == [KIASMOS]
    ok(post_ops(demo, 1, [{"kind": "update", "playlist_id": 5, "track_id": 16, "set": {"display_title": "Hush note"}}]))
    assert ok(search(demo, "hush", 2))["tracks"] == []


def test_search_people(demo):
    lurker = ok(demo.post("/api/auth/login", json={"username": "maya_lurker"}))["user_id"]
    people = ok(search(demo, "maya", 1))["people"]
    assert [p["user"]["username"] for p in people] == ["maya"] and people[0]["playlist_count"] == 1
    assert "maya_lurker" in [p["user"]["username"] for p in ok(search(demo, "maya", lurker))["people"]]
    assert [p["user"]["username"] for p in ok(search(demo, "Lin-Kratz"))["people"]] == ["maya"]


def test_search_like_wildcards_are_literal(demo):
    pid = new_playlist(demo, 2, "100% Pure_Fun", videos=[KIASMOS])
    assert ids_of(ok(search(demo, "%"))["playlists"]) == [pid]
    assert ids_of(ok(search(demo, "e_F"))["playlists"]) == [pid]
    assert ok(search(demo, "Pur__Fun"))["playlists"] == []
    assert ok(search(demo, "_"))["people"] == []
    assert ok(search(demo, "\\"))["playlists"] == []


def test_search_validation_and_type_filter(demo):
    assert search(demo, "x" * 101).status_code == 422
    assert search(demo, "   ").status_code == 422
    assert demo.get("/api/search").status_code == 422
    assert search(demo, "x" * 100).status_code == 200
    assert search(demo, "kexp", limit=26).status_code == 422
    only = ok(search(demo, "a", type="people"))
    assert only["playlists"] == [] and only["tracks"] == [] and only["people"]
    assert len(ok(search(demo, "a", type="tracks", limit=2))["tracks"]) == 2
    assert search(demo, "a", type="songs").status_code == 422


# --- the canary: Dan's crate rows change nothing anybody else sees ----------------------------------

@pytest.mark.parametrize("viewer", [2, 3])
def test_crate_canary_changes_no_discovery_view(demo, viewer):
    def views():
        out = {
            "discover": ok(demo.get(f"/api/discover?user_id={viewer}")),
            "discover.tracks": ok(demo.get(f"/api/discover?tab=tracks&user_id={viewer}")),
            "discover.people": ok(demo.get(f"/api/discover?tab=people&user_id={viewer}")),
            "search": ok(demo.get(f"/api/search?q=e&user_id={viewer}")),
            "home": {k: v for k, v in ok(demo.get(f"/api/me/home?user_id={viewer}")).items() if k != "dock"},
        }
        for pid in (1, 2, 3, 4):
            out[f"related.{pid}"] = ok(related(demo, pid, viewer))
        for uid in (1, 2, 3, 4):
            out[f"profile.{uid}"] = {k: v for k, v in ok(demo.get(f"/api/users/{uid}?user_id={viewer}")).items()
                                     if k in ("neighbours", "shared_with_viewer", "playlists")}
        return out

    before = views()
    drop_crate_rows()
    assert views() == before

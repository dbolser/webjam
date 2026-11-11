"""Regression tests for the Phase 1a review findings (one or more per finding).

Seed facts: see the docstring of test_ops.py (users 1 dan, 2 lucas, 3 maya, 4 julian;
playlists 1-4 public, crates 5 dan / 6 lucas / 7 maya / 8 julian; Dan's crate row 16 was
grabbed from Maya's track 4).
"""

import re
from pathlib import Path

import pytest

import test_security_backend as sec
from test_security_backend import db, db_path, demo, fakes  # noqa: F401  (fixtures are used by name)
from test_ops import ok, order, post_ops, q, redo, row, undo

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")


def feed_text(client, viewer):
    return " | ".join(a["summary"] for a in ok(client.get(f"/api/feed?user_id={viewer}"))["activity"])


def new_playlist(client, uid, title, visibility="public", urls=""):
    return ok(client.post("/api/playlists", json={"user_id": uid, "title": title, "visibility": visibility,
                                                  "initial_urls": urls}))["playlist_id"]


# --- 1. Superlove note must not name a crate-only song ------------------------------------------

def test_superlove_on_a_crate_only_song_does_not_name_it(demo):
    ok(post_ops(demo, 1, [{"kind": "insert", "playlist_id": 5, "items": [{"video_id": "SECRETVID01"}]}]))
    res = ok(demo.post("/api/songs/SECRETVID01/like", json={"user_id": 1, "count": 10}))
    assert demo.get("/api/songs/SECRETVID01?user_id=3").status_code == 404
    assert "SECRETVID01" not in feed_text(demo, 3)
    notes = [b["context_note"] or "" for b in ok(demo.get("/api/users/1?viewer_id=3"))["earned_badges"]]
    assert not [n for n in notes if "SECRETVID01" in n]
    superlover = [b for b in res.get("new_badges") or [] if b["key"] == "superlover"]
    if superlover:
        assert superlover[0]["context_note"] == "Bestowed 10× Superlove"


def test_superlove_on_a_public_song_still_names_it(demo):
    from app.badges import compute_user_metrics

    ok(demo.post("/api/songs/BI2Et19vDCM/like", json={"user_id": 4, "count": 10}))
    conn = db()
    try:
        ctx = compute_user_metrics(conn, 4)["_contexts"]["superlover"]
    finally:
        conn.close()
    assert ctx.startswith("Bestowed 10× Superlove on '")


# --- 2. Paste then undo can't farm badges --------------------------------------------------------

def test_undo_revokes_the_badges_its_group_earned_and_redo_restores_them(demo):
    vids = [f"FARMVID{i:04d}" for i in range(12)]
    res = ok(post_ops(demo, 1, [{"kind": "insert", "playlist_id": 4, "items": [{"video_id": v} for v in vids]}]))
    earned = {b["key"] for b in res["new_badges"]}
    assert {"first_finder_5", "first_finder_12"} <= earned
    ok(undo(demo, 1))
    keys = {r[0] for r in q("SELECT badge_key FROM user_badges WHERE user_id = 1")}
    assert not ({"first_finder_5", "first_finder_12"} & keys)
    assert "First to discover 14" not in feed_text(demo, 3)
    assert not q("SELECT 1 FROM activity_log WHERE action_type = 'earned_badge' AND summary LIKE '%14 tracks%'")
    red = ok(redo(demo, 1))
    assert {"first_finder_5", "first_finder_12"} <= {b["key"] for b in red["new_badges"]}
    assert {"first_finder_5", "first_finder_12"} <= {r[0] for r in q("SELECT badge_key FROM user_badges WHERE user_id = 1")}


def test_undo_keeps_badges_earned_outside_the_group(demo):
    before = {tuple(r) for r in q("SELECT user_id, badge_key FROM user_badges")}
    ok(post_ops(demo, 1, [{"kind": "insert", "playlist_id": 4, "items": [{"video_id": "KEEPVID0001"}]}]))
    ok(undo(demo, 1))
    assert before <= {tuple(r) for r in q("SELECT user_id, badge_key FROM user_badges")}


# --- 3. Legacy reorder: one op row, rate limited (fork and promote too) --------------------------

def test_big_reorder_is_one_reorder_row_and_undoes_exactly(demo):
    start = order(4)
    ok(demo.put("/api/playlists/4/reorder", json={"user_id": 1, "track_ids": list(reversed(start))}))
    assert order(4) == list(reversed(start))
    assert [r[0] for r in q("SELECT kind FROM playlist_ops")] == ["reorder"]
    assert ok(demo.get("/api/me/undo?user_id=1"))["undo_label"] == "Reordered 'Webjam Inaugural Transmission'"
    ok(undo(demo, 1))
    assert order(4) == start
    ok(redo(demo, 1))
    assert order(4) == list(reversed(start))


def test_reorder_of_many_tracks_costs_one_row(demo):
    for b in range(3):
        ok(post_ops(demo, 1, [{"kind": "insert", "playlist_id": 4,
                               "items": [{"video_id": f"R{b}x{i:08d}"} for i in range(50)]}]))
    ids = order(4)
    rows_before = q("SELECT COUNT(*) FROM playlist_ops")[0][0]
    ok(demo.put("/api/playlists/4/reorder", json={"user_id": 1, "track_ids": list(reversed(ids))}))
    assert q("SELECT COUNT(*) FROM playlist_ops")[0][0] == rows_before + 1
    assert order(4) == list(reversed(ids))


def test_reorder_fork_and_promote_are_rate_limited(demo, monkeypatch):
    import app.main

    monkeypatch.setitem(app.main.RATE_LIMITS, "ops", (1, 0.0001))
    assert demo.put("/api/playlists/4/reorder", json={"user_id": 1, "track_ids": [15, 14, 13, 12]}).status_code == 200
    assert demo.put("/api/playlists/4/reorder", json={"user_id": 1, "track_ids": [12, 13, 14, 15]}).status_code == 429
    assert demo.post("/api/playlists/1/fork", json={"user_id": 1}).status_code == 429
    assert demo.post("/api/me/crate/promote", json={"user_id": 1, "title": "x"}).status_code == 429


# --- 4 / 11. Forking an unlisted or private playlist never names it in public ---------------------

def test_public_fork_of_unlisted_source_does_not_name_it(demo):
    ok(post_ops(demo, 3, [{"kind": "set_meta", "playlist_id": 2,
                           "set": {"visibility": "unlisted", "title": "Maya Unlisted Secret"}}]))
    fork = ok(demo.post("/api/playlists/2/fork", json={"user_id": 2, "visibility": "public", "title": "lucas fork"}), 201)
    text = feed_text(demo, 4)
    assert "Maya Unlisted Secret" not in text
    assert "published playlist 'lucas fork'" in text
    assert ok(demo.get(f"/api/playlists/{fork['playlist_id']}?user_id=4"))["forked_from"] is None


def test_public_fork_of_own_private_source_does_not_name_it(demo):
    pid = new_playlist(demo, 1, "DAN PRIVATE SECRET", "private", "TNsSBhl_2LI")
    ok(demo.post(f"/api/playlists/{pid}/fork", json={"user_id": 1, "visibility": "public", "title": "dan open"}), 201)
    assert "DAN PRIVATE SECRET" not in feed_text(demo, 3)


def test_public_fork_of_public_source_still_credits_it(demo):
    ok(demo.post("/api/playlists/2/fork", json={"user_id": 2, "visibility": "public"}), 201)
    assert "forked '2AM Terminal Glow (Coding Mix)' from @maya" in feed_text(demo, 4)


# --- 5 / 14. Reserved op_id / group_id prefixes ---------------------------------------------------

@pytest.mark.parametrize("field,value", [("op_id", "fork-sneaky"), ("op_id", "srv-x"), ("op_id", "PROMOTE-x"),
                                         ("group_id", "promote-x"), ("group_id", "fork-y")])
def test_client_ids_may_not_use_reserved_prefixes(demo, field, value):
    before = q("SELECT COUNT(*) FROM playlist_tracks")[0][0]
    body = {"user_id": 1, "op_id": "client-1",
            "ops": [{"kind": "insert", "playlist_id": 4, "items": [{"video_id": "c_56qQqxvm4", "ref": {"track_id": 4}}]}]}
    body[field] = value
    res = demo.post("/api/ops", json=body)
    assert res.status_code == 400 and res.json()["code"] == "bad_op"
    assert q("SELECT COUNT(*) FROM playlist_tracks")[0][0] == before


def test_grab_into_public_playlist_always_logs_a_line(demo):
    ok(post_ops(demo, 1, [{"kind": "insert", "playlist_id": 4, "items": [{"video_id": "c_56qQqxvm4", "ref": {"track_id": 4}}]}],
                op_id="forkish-1"))
    assert q("SELECT COUNT(*) FROM activity_log WHERE action_type = 'grabbed_track'")[0][0] >= 1
    assert "from @maya into 'Webjam Inaugural Transmission'" in feed_text(demo, 3)


def test_fork_still_logs_no_grab_line(demo):
    before = q("SELECT COUNT(*) FROM activity_log WHERE action_type IN ('grabbed_track', 'added_track')")[0][0]
    ok(demo.post("/api/playlists/2/fork", json={"user_id": 1, "visibility": "public"}), 201)
    assert q("SELECT COUNT(*) FROM activity_log WHERE action_type IN ('grabbed_track', 'added_track')")[0][0] == before


def test_promote_undo_note_comes_from_the_server_flag(demo):
    ok(post_ops(demo, 1, [{"kind": "insert", "playlist_id": 5, "items": [{"video_id": "NEWVIDEO020"}]}]))
    promo = ok(demo.post("/api/me/crate/promote", json={"user_id": 1, "title": "Promoted", "visibility": "private"}), 201)
    res = ok(undo(demo, 1))
    assert res["group_id"] == promo["group_id"]
    assert {"code": "playlist_left_empty", "playlist_id": promo["playlist_id"]} in res["notes"]


# --- 6. Songs whose only placements went private disappear from lists -----------------------------

def test_unpublished_song_leaves_recent_songs_and_discovered_songs(demo):
    pid = new_playlist(demo, 1, "oops", "public", "HIDDENVID01")
    ok(post_ops(demo, 1, [{"kind": "set_meta", "playlist_id": pid, "set": {"visibility": "private"}}]))
    assert demo.get("/api/songs/HIDDENVID01?user_id=3").status_code == 404
    feed = ok(demo.get("/api/feed?user_id=3"))
    assert "HIDDENVID01" not in [s["video_id"] for s in feed["recent_songs"]]
    prof = ok(demo.get("/api/users/1?viewer_id=3"))
    assert "HIDDENVID01" not in [s["video_id"] for s in prof["discovered_songs"]]
    # The owner still sees it (the song page exists for them), and the claim stays sticky.
    own = ok(demo.get("/api/users/1?viewer_id=1"))
    assert "HIDDENVID01" in [s["video_id"] for s in own["discovered_songs"]]
    assert q("SELECT first_added_by_user_id FROM songs WHERE video_id = 'HIDDENVID01'")[0][0] == 1


# --- 7 / 13. source_track_id survives delete / move and their undo ------------------------------

def crate_row_16(client):
    return next(t for t in ok(client.get("/api/me/crate?user_id=1"))["tracks"] if t["id"] == 16)


def test_delete_then_undo_keeps_other_rows_source_track_links(demo):
    assert row(16)["source_track_id"] == 4
    ok(post_ops(demo, 3, [{"kind": "delete", "playlist_id": 2, "track_ids": [4]}]))
    assert row(16)["source_track_id"] == 4  # no FK SET NULL any more
    assert crate_row_16(demo)["source_track_id"] is None  # a dangling id is not rendered as a link
    ok(undo(demo, 3))
    assert row(4) is not None and row(16)["source_track_id"] == 4
    assert crate_row_16(demo)["source_track_id"] == 4


def test_cross_playlist_move_then_undo_keeps_source_track_links(demo):
    maya_crate = q("SELECT id FROM playlists WHERE user_id = 3 AND kind = 'crate'")[0][0]
    ok(post_ops(demo, 3, [{"kind": "move", "playlist_id": 2, "track_ids": [4], "to_playlist_id": maya_crate}]))
    assert crate_row_16(demo)["source_track_id"] is None  # the source row left playlist 2
    ok(undo(demo, 3))
    assert row(16)["source_track_id"] == 4 and crate_row_16(demo)["source_track_id"] == 4


def test_redo_of_swap_still_works_after_fk_nulled_its_expected_source(demo):
    # Dan swaps his crate row 16 (grabbed from Maya's playlist 2) to a song NOT in playlist 2, so the
    # swap drops the source; undo puts source_playlist_id=2 back, and redo expects to find it.
    ok(post_ops(demo, 1, [{"kind": "update", "playlist_id": 5, "track_id": 16, "set": {"video_id": "BI2Et19vDCM"}}]))
    assert row(16)["source_playlist_id"] is None
    ok(undo(demo, 1))
    assert row(16)["source_playlist_id"] == 2
    ok(demo.delete("/api/playlists/2?user_id=3"))  # the FK nulls row 16's source_playlist_id
    assert row(16)["source_playlist_id"] is None
    ok(redo(demo, 1))
    assert row(16)["video_id"] == "BI2Et19vDCM"


# --- 8 / 9. Undo names its group; the UI sends it, ignores key repeat, and skip re-undoes --------

def test_undo_state_exposes_group_ids_and_stale_undo_is_refused(demo):
    a = ok(post_ops(demo, 4, [{"kind": "insert", "playlist_id": 3, "items": [{"video_id": "TNsSBhl_2LI"}]}]))
    b = ok(post_ops(demo, 4, [{"kind": "insert", "playlist_id": 3, "items": [{"video_id": "c_56qQqxvm4"}]}]))
    state = ok(demo.get("/api/me/undo?user_id=4"))
    assert state["undo_group_id"] == b["group_id"] and state["redo_group_id"] is None
    assert ok(demo.get("/api/bootstrap?user_id=4"))["current_user"]["undo"]["undo_group_id"] == b["group_id"]
    first = ok(undo(demo, 4, group_id=state["undo_group_id"]))
    assert first["group_id"] == b["group_id"] and first["undo"]["undo_group_id"] == a["group_id"]
    assert first["undo"]["redo_group_id"] == b["group_id"]
    # A second press that still names b (a repeat / double click) undoes nothing.
    again = undo(demo, 4, group_id=b["group_id"])
    assert again.status_code == 409 and again.json()["code"] == "stack_moved"
    assert ok(demo.get("/api/me/undo?user_id=4"))["undo_group_id"] == a["group_id"]


def test_frontend_undo_sends_group_id_and_guards_repeats():
    assert "if (e.repeat) return;" in APP_JS
    assert "if (undoInFlight) return;" in APP_JS
    assert "group_id: expect || null" in APP_JS
    assert ".undo_group_id" in APP_JS


def test_frontend_skip_then_undoes_the_step_before():
    body = APP_JS[APP_JS.index("async function runUndo"):]
    body = body[:body.index("\n}\n")]
    assert "skip: true, group_id: conflictGroup" in body
    assert "await runUndo(false, next)" in body
    assert "err.data = errData" in APP_JS


# --- 10 / 15. Auto-migrate is one undo step --------------------------------------------------------

def test_legacy_swaps_sharing_a_group_undo_together(demo):
    for tid, vid in ((12, "SWAPVID0001"), (13, "SWAPVID0002")):
        ok(demo.put(f"/api/playlists/4/tracks/{tid}", json={"user_id": 1, "new_video_id": vid,
                                                             "custom_title": "x", "group_id": "mig-1"}))
    state = ok(demo.get("/api/me/undo?user_id=1"))
    assert state["undo_group_id"] == "mig-1"
    assert state["undo_label"] == "Swapped 2 tracks in 'Webjam Inaugural Transmission'"
    ok(undo(demo, 1))
    assert (row(12)["video_id"], row(13)["video_id"]) == ("BI2Et19vDCM", "S8fFo4REHOE")
    assert not ok(demo.get("/api/me/undo?user_id=1"))["can_undo"]


def test_legacy_swap_rejects_reserved_group_id(demo):
    res = demo.put("/api/playlists/4/tracks/12", json={"user_id": 1, "track_note": "n", "group_id": "srv-x"})
    assert res.status_code == 400


def test_frontend_auto_migrate_shares_one_group():
    body = APP_JS[APP_JS.index("async function autoMigrateAuditedPlaylist"):]
    body = body[:body.index("\n}\n")]
    assert "const groupId = `mig-" in body and "group_id: groupId" in body


# --- 12. /api/ops checks caps and roles before any YouTube lookup ---------------------------------

def test_oversized_batch_makes_no_metadata_calls(demo):
    sec.METADATA_CALLS.clear()
    items = [{"video_id": f"X{i:010d}"} for i in range(300)]
    res = post_ops(demo, 1, [{"kind": "insert", "playlist_id": 4, "items": items}])
    assert res.status_code == 400 and res.json()["code"] == "batch_too_large"
    assert sec.METADATA_CALLS == []


def test_unauthorised_batch_makes_no_metadata_calls(demo):
    sec.METADATA_CALLS.clear()
    res = post_ops(demo, 1, [{"kind": "insert", "playlist_id": 2, "items": [{"video_id": "Y0000000001"}]}])
    assert res.status_code in (403, 404)
    res = post_ops(demo, 1, [{"kind": "insert", "playlist_id": 7, "items": [{"video_id": "Y0000000002"}]}])
    assert res.status_code == 404
    assert sec.METADATA_CALLS == []


# --- Paste: free text with 11-letter words stays empty ----------------------------------------------

def test_free_text_with_eleven_letter_words_is_empty_and_free(demo):
    sec.METADATA_CALLS.clear()
    for text in ("really interesting information about programming", "programming", "Interesting",
                 "INFORMATION, programming", "TNsSBhl_2LI and more"):
        res = ok(demo.post("/api/resolve-paste", json={"user_id": 1, "text": text}))
        assert res == {"sources": [], "items": [], "pointers": [], "unresolved": []}, text
    assert sec.METADATA_CALLS == []
    ids = ok(demo.post("/api/resolve-paste", json={"user_id": 1, "text": "TNsSBhl_2LI\nNEWVIDEO011"}))
    assert [i["video_id"] for i in ids["items"]] == ["TNsSBhl_2LI", "NEWVIDEO011"]


# --- 16. Undo labels describe the whole group -------------------------------------------------------

def test_label_of_a_multi_playlist_batch_names_all_of_it(demo):
    a = new_playlist(demo, 1, "A side")
    b = new_playlist(demo, 1, "B side")
    res = ok(post_ops(demo, 1, [{"kind": "insert", "playlist_id": pid, "items": [{"video_id": "TNsSBhl_2LI"}]}
                                for pid in (4, a, b)]))
    assert res["undo"]["undo_label"] == "Added 3 tracks to 3 playlists"


def test_label_of_a_chunked_group_counts_every_chunk(demo):
    ok(post_ops(demo, 1, [{"kind": "insert", "playlist_id": 4, "items": [{"video_id": "TNsSBhl_2LI"}] * 3}],
                group_id="paste-1"))
    res = ok(post_ops(demo, 1, [{"kind": "insert", "playlist_id": 4, "items": [{"video_id": "c_56qQqxvm4"}] * 2}],
                      group_id="paste-1"))
    assert res["undo"]["undo_label"] == "Added 5 tracks to 'Webjam Inaugural Transmission'"
    assert ok(undo(demo, 1))["label"] == "Added 5 tracks to 'Webjam Inaugural Transmission'"
    assert order(4) == [12, 13, 14, 15]


def test_single_effect_label_unchanged(demo):
    res = ok(post_ops(demo, 1, [{"kind": "insert", "playlist_id": 4, "items": [{"video_id": "TNsSBhl_2LI"}]}]))
    assert res["undo"]["undo_label"] == "Added 1 track to 'Webjam Inaugural Transmission'"
    crate = ok(post_ops(demo, 1, [{"kind": "move", "playlist_id": 4, "track_ids": [12], "to_playlist_id": 5}]))
    assert crate["undo"]["undo_label"] == "Moved 1 track to 'Crate'"


# --- 17. Profile playlists blank forked_from_playlist_id of non-linkable sources -------------------

def test_profile_hides_private_fork_source_id(demo):
    src = new_playlist(demo, 1, "dan private src", "private", "TNsSBhl_2LI")
    fork = ok(demo.post(f"/api/playlists/{src}/fork", json={"user_id": 1, "visibility": "public", "title": "f"}), 201)
    others = {p["id"]: p for p in ok(demo.get("/api/users/1?viewer_id=3"))["playlists"]}
    assert others[fork["playlist_id"]]["forked_from_playlist_id"] is None
    mine = {p["id"]: p for p in ok(demo.get("/api/users/1?viewer_id=1"))["playlists"]}
    assert mine[fork["playlist_id"]]["forked_from_playlist_id"] == src


# --- 18. The player deck can't like a crate ------------------------------------------------------------

def test_frontend_deck_love_mix_is_off_for_crates():
    assert "state.queuePlaylistId && state.queuePlaylistKind !== 'crate'" in APP_JS
    assert re.search(r"likeActiveQueuePlaylist\(event = null\) \{\n  if \(!state\.queuePlaylistId \|\| state\.queuePlaylistKind === 'crate'\) return;", APP_JS)
    assert "state.queuePlaylistKind = playlist.kind || 'playlist';" in APP_JS


def test_fork_defaults_to_the_source_visibility(demo):
    ok(post_ops(demo, 3, [{"kind": "set_meta", "playlist_id": 2, "set": {"visibility": "unlisted"}}]))
    unlisted = ok(demo.post("/api/playlists/2/fork", json={"user_id": 2}), 201)
    public = ok(demo.post("/api/playlists/1/fork", json={"user_id": 3}), 201)
    vis = dict(q("SELECT id, visibility FROM playlists WHERE id IN (?, ?)", unlisted["playlist_id"], public["playlist_id"]))
    assert (vis[unlisted["playlist_id"]], vis[public["playlist_id"]]) == ("unlisted", "public")

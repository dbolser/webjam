"""Phase 1b Lane A (editing): the exact op shapes the editing UI sends, plus static guards on the
editing modules that the cutover (docs/phase1b-shell.md §7.3) will lean on.

Seed facts (fresh DB): playlist 4 is dan(1)'s with tracks 12, 13, 14, 15; dan's crate is 5 with rows
16 (c_56qQqxvm4 via maya) and 17 (6647smeduGI via julian).
"""

import re
from pathlib import Path

from test_ops import ok, order, post_ops, row, undo  # noqa: F401
from test_security_backend import (  # noqa: F401  (fixtures are used by name)
    OK_HEADERS,
    db,
    db_path,
    demo,
    fakes,
    google,
)

ROOT = Path(__file__).resolve().parent.parent
EDITING = ROOT / "static" / "js" / "editing"


def _version(client, pid):
    return ok(client.get(f"/api/playlists/{pid}?user_id=1"))["version"]


# --- the ops the UI sends --------------------------------------------------------------------

def test_move_to_position_and_nudges_chain_with_fresh_versions(demo):
    """G 1 Enter, then Alt-↓ twice: each op carries the version the previous response returned."""
    v = _version(demo, 4)
    res = ok(post_ops(demo, 1, [{"kind": "move", "playlist_id": 4, "track_ids": [15], "position": 1}],
                      expected_versions={"4": v}))
    assert order(4) == [15, 12, 13, 14]
    v = res["versions"]["4"]
    for expect in ([12, 15, 13, 14], [12, 13, 15, 14]):
        idx = order(4).index(15)
        res = ok(post_ops(demo, 1, [{"kind": "move", "playlist_id": 4, "track_ids": [15], "position": idx + 2}],
                          expected_versions={"4": v}))
        v = res["versions"]["4"]
        assert order(4) == expect
    # Shift-Alt-↓: to the bottom (n - k + 1).
    ok(post_ops(demo, 1, [{"kind": "move", "playlist_id": 4, "track_ids": [15], "position": 4}],
                expected_versions={"4": v}))
    assert order(4) == [12, 13, 14, 15]
    # Each keystroke is its own undo group.
    for _ in range(4):
        ok(undo(demo, 1))
    assert order(4) == [12, 13, 14, 15]


def test_multi_select_move_to_position_keeps_relative_order(demo):
    ok(post_ops(demo, 1, [{"kind": "move", "playlist_id": 4, "track_ids": [15, 12], "position": 2}]))
    assert order(4) == [13, 12, 15, 14]


def test_cut_paste_from_crate_is_a_move_and_one_undo_restores_both(demo):
    res = ok(post_ops(demo, 1, [{"kind": "move", "playlist_id": 5, "track_ids": [16, 17], "to_playlist_id": 4,
                                 "after_track_id": 13}],
                      expected_versions={"4": _version(demo, 4), "5": _version(demo, 5)}))
    moved = [m["track_id"] for m in res["results"][0]["moved"]]
    assert order(4) == [12, 13, *moved, 14, 15]
    assert order(5) == []
    # Provenance survives the unload (the destination credits the original source).
    assert row(moved[0])["source_user_id"] == 3
    ok(undo(demo, 1))
    assert order(5) == [16, 17] and order(4) == [12, 13, 14, 15]


def test_send_to_dock_slot_appends_a_credited_copy(demo):
    """Digit keys from someone else's playlist: an insert with ref.track_id and no placement."""
    res = ok(post_ops(demo, 1, [{"kind": "insert", "playlist_id": 4,
                                 "items": [{"video_id": "c_56qQqxvm4", "ref": {"track_id": 4}}]}]))
    tid = res["results"][0]["track_ids"][0]
    assert order(4)[-1] == tid
    assert res["rows"][str(tid)]["provenance"] == "credited"


def test_note_and_clip_update_then_undo(demo):
    ok(post_ops(demo, 1, [{"kind": "update", "playlist_id": 4, "track_id": 12,
                           "set": {"note": "listen for the bass", "start_seconds": 65, "end_seconds": None}}]))
    r = row(12)
    assert r["track_note"] == "listen for the bass" and r["start_seconds"] == 65
    res = ok(undo(demo, 1))
    assert str(12) in res["rows"]  # the undo response carries the row, so the UI needs no refetch
    assert row(12)["start_seconds"] is None


def test_set_meta_title_and_visibility(demo):
    ok(post_ops(demo, 1, [{"kind": "set_meta", "playlist_id": 4, "set": {"title": "Transmission II", "visibility": "unlisted"}}]))
    d = ok(demo.get("/api/playlists/4?user_id=1"))
    assert d["title"] == "Transmission II" and d["visibility"] == "unlisted"
    ok(undo(demo, 1))
    d = ok(demo.get("/api/playlists/4?user_id=1"))
    assert d["title"] != "Transmission II" and d["visibility"] == "public"


def test_insert_items_reject_unknown_fields(demo):
    """Why editing/ops.js cleanItem() strips display hints that the op model doesn't accept."""
    res = post_ops(demo, 1, [{"kind": "insert", "playlist_id": 4,
                              "items": [{"video_id": "c_56qQqxvm4", "row": {"title": "x"}}]}])
    assert res.status_code == 422


# --- static guards on the editing modules -------------------------------------------------------

def _src(name):
    return (EDITING / name).read_text(encoding="utf-8")


def test_editing_lane_is_wired_in():
    main = (ROOT / "static" / "js" / "main.js").read_text(encoding="utf-8")
    assert re.search(r"^import '\./editing/index\.js';", main, re.M)
    for name in ("index.js", "selection.js", "clipboard.js", "dnd.js", "move-to.js", "undo.js",
                 "inline-edit.js", "cheatsheet.js", "ops.js", "send.js"):
        assert (EDITING / name).is_file(), name


def test_undo_sends_the_expected_group_and_keeps_one_request_in_flight():
    undo_js = _src("undo.js")
    api_js = (ROOT / "static" / "js" / "api.js").read_text(encoding="utf-8")
    assert "if (inFlight) return;" in undo_js
    assert "undo_conflict" in undo_js and "skip: true" in undo_js
    assert "stack_moved" in undo_js
    assert "body.group_id = gid" in api_js
    keys_js = (ROOT / "static" / "js" / "keys.js").read_text(encoding="utf-8")
    assert "if (e.repeat && !arrow) return;" in keys_js


def test_edits_go_through_the_ops_pipeline_with_expected_versions():
    ops_js = _src("ops.js")
    assert "api.ops.send([sendOp], { token, groupId, expectedVersions })" in ops_js
    for name in ("clipboard.js", "move-to.js", "inline-edit.js", "send.js", "dnd.js"):
        assert "api.ops.send" not in _src(name), f"{name} must use editing/ops.js submit()"


def test_crate_and_private_rows_copy_as_track_links():
    clip = _src("clipboard.js")
    assert "p.kind === 'crate'" in clip and "visibility === 'private'" in clip
    assert "trackUrl(row.video_id, row.start_seconds, row.end_seconds)" in clip

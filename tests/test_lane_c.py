"""Phase 1b Lane C (import / share / crate / account): the server contracts the new client relies on,
plus a node smoke test of the lane's modules.

The client flows checked here, step by step as static/js performs them:
  - paste anywhere: /api/resolve-paste, then /api/ops inserts into my crate in batches of 50 that
    share one group_id, so one Undo removes the whole paste (import/paste-to-crate.js,
    crate/crate-actions.js insertIntoCrate);
  - crate "Make it a playlist" for a selection, then Undo with the response's group_id after
    reading /api/me/undo (crate/crate-actions.js submitPromote);
  - "Clear unloaded": /api/me/crate also_in, then one delete op (crate/crate-actions.js);
  - playlist details: a set_meta op, undone by the toast's Undo (editing/inline-edit.js after the
    cutover; Lane C's settings sheet was folded into it);
  - "Copy all": the fork response carries what the toast says (share/fork.js).
Seed facts: see tests/test_ops.py (dan=1 owns playlist 4 and crate 5 = rows 16, 17).
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from test_security_backend import db, db_path, demo, fakes, google  # noqa: F401
from test_ops import ok, op_id, order, post_ops, q, row, undo

ROOT = Path(__file__).resolve().parent.parent


def resolve(client, text, uid=1):
    return ok(client.post("/api/resolve-paste", json={"user_id": uid, "text": text, "html": None}))


def client_items(items):
    """What insertIntoCrate sends: the insert shape only, refs reduced to track_id / playlist_id."""
    out = []
    for it in items:
        o = {"video_id": it["video_id"]}
        for k in ("start_seconds", "end_seconds"):
            if it.get(k) is not None:
                o[k] = it[k]
        if it.get("note"):
            o["note"] = it["note"]
        ref = it.get("ref") or {}
        if ref.get("track_id"):
            o["ref"] = {"track_id": ref["track_id"]}
        elif ref.get("playlist_id"):
            o["ref"] = {"playlist_id": ref["playlist_id"]}
        out.append(o)
    return out


def test_paste_into_crate_in_batches_is_one_undo_group(demo):
    before = order(5)
    pasted = resolve(demo, "/p/2?track=5\nhttps://youtu.be/NEWVIDEO020?t=30")
    assert [i["video_id"] for i in pasted["items"]] == ["4fHw4GeW3EU", "NEWVIDEO020"]
    assert pasted["items"][0]["ref"]["track_id"] == 5
    items = client_items(pasted["items"])

    group = op_id()
    first = ok(post_ops(demo, 1, [{"kind": "insert", "playlist_id": 5, "items": items[:1]}], group_id=group))
    second = ok(post_ops(demo, 1, [{"kind": "insert", "playlist_id": 5, "items": items[1:]}], group_id=group))
    new_ids = first["results"][0]["track_ids"] + second["results"][0]["track_ids"]
    assert order(5) == before + new_ids
    # Credit survives the round trip through plain text: maya's row is credited, the clip is kept.
    assert first["rows"][str(new_ids[0])]["provenance"] == "credited"
    assert row(new_ids[0])["source_user_id"] == 3
    assert row(new_ids[1])["start_seconds"] == 30
    # The crate stays private.
    assert demo.get("/api/playlists/5", params={"user_id": 2}).status_code == 404

    # The toast's Undo: one request removes both batches.
    assert ok(demo.get("/api/me/undo", params={"user_id": 1}))["undo_group_id"] == group
    res = ok(undo(demo, 1, group_id=group))
    assert res["group_id"] == group
    assert order(5) == before
    assert [int(t) for t in res["order"]["5"]] == before


def test_free_text_paste_resolves_to_nothing(demo):
    res = resolve(demo, "really interesting information about programming")
    assert res == {"sources": [], "items": [], "pointers": [], "unresolved": []}


def test_youtube_playlist_paste_is_a_pointer_for_the_import_sheet(demo):
    res = resolve(demo, "https://www.youtube.com/playlist?list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG")
    assert res["items"] == []
    ptr = res["pointers"][0]
    assert ptr["type"] == "youtube_playlist" and ptr["source_input"].endswith("PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG")


def test_promote_selection_then_undo_with_the_response_group(demo):
    assert order(5) == [16, 17]
    res = ok(demo.post("/api/me/crate/promote", json={
        "user_id": 1, "title": "Late Shift", "description": "", "visibility": "unlisted", "track_ids": [17]}), 201)
    assert res["moved"] == 1 and res["crate"]["track_count"] == 1
    assert order(5) == [16]  # the client drops exactly the promoted ids from the crate
    pid = res["playlist_id"]
    assert q("SELECT visibility FROM playlists WHERE id = ?", pid)[0][0] == "unlisted"
    # The promote response has no undo state; the client reads it, and it names this group.
    u = ok(demo.get("/api/me/undo", params={"user_id": 1}))
    assert u["can_undo"] and u["undo_group_id"] == res["group_id"]
    back = ok(undo(demo, 1, group_id=res["group_id"]))
    assert len(order(5)) == 2 and order(pid) == []
    assert {"code": "playlist_left_empty", "playlist_id": pid} in back["notes"]
    # "Delete it" from the toast.
    ok(demo.delete(f"/api/playlists/{pid}", params={"user_id": 1}))


def test_clear_unloaded_deletes_only_rows_already_in_my_playlists(demo):
    # Unload row 16 with "keep a copy": an insert into playlist 4 with ref.track_id.
    ok(post_ops(demo, 1, [{"kind": "insert", "playlist_id": 4, "items": [
        {"video_id": "c_56qQqxvm4", "ref": {"track_id": 16}}]}]))
    crate = ok(demo.get("/api/me/crate", params={"user_id": 1}))
    also = {t["id"]: [a["playlist_id"] for a in t["also_in"]] for t in crate["tracks"]}
    assert also == {16: [4], 17: []}
    ids = [t["id"] for t in crate["tracks"] if t["also_in"]]
    cleared = ok(post_ops(demo, 1, [{"kind": "delete", "playlist_id": crate["id"], "track_ids": ids}]))
    assert [int(t) for t in cleared["order"]["5"]] == [17]
    # Unload (move) the rest: the row leaves the crate and lands in playlist 4 with its credit.
    moved = ok(post_ops(demo, 1, [{"kind": "move", "playlist_id": 5, "track_ids": [17], "to_playlist_id": 4}]))
    new_id = moved["results"][0]["moved"][0]["track_id"]
    assert order(5) == [] and order(4)[-1] == new_id
    assert row(new_id)["source_user_id"] == 4


def test_settings_set_meta_is_undoable(demo):
    res = ok(post_ops(demo, 1, [{"kind": "set_meta", "playlist_id": 4,
                                 "set": {"title": "Renamed", "description": "d", "visibility": "private"}}]))
    assert res["results"][0]["changed"]
    detail = ok(demo.get("/api/playlists/4", params={"user_id": 1}))
    assert (detail["title"], detail["visibility"]) == ("Renamed", "private")
    assert demo.get("/api/playlists/4", params={"user_id": 2}).status_code == 404
    ok(undo(demo, 1, group_id=res["group_id"]))
    detail = ok(demo.get("/api/playlists/4", params={"user_id": 1}))
    assert detail["title"] != "Renamed" and detail["visibility"] == "public"


def test_settings_cannot_touch_someone_elses_playlist(demo):
    res = post_ops(demo, 1, [{"kind": "set_meta", "playlist_id": 2, "set": {"title": "Mine now"}}])
    assert res.status_code == 403


def test_copy_all_response_feeds_the_toast(demo):
    res = ok(demo.post("/api/playlists/2/fork", json={"user_id": 1, "title": None, "visibility": None}), 201)
    assert res["track_count"] == 4
    assert res["forked_from"]["username"] == "maya"
    copy = ok(demo.get(f"/api/playlists/{res['playlist_id']}", params={"user_id": 1}))
    assert copy["title"] == ok(demo.get("/api/playlists/2", params={"user_id": 1}))["title"]
    assert all(t["source_username"] == "maya" for t in copy["tracks"])


def test_import_into_crate_keeps_it_private(demo):
    res = ok(demo.post("/api/playlists/import/commit", json={
        "user_id": 1, "title": "Crate", "description": "", "target_playlist_id": 5,
        "tracks": [{"video_id": "NEWVIDEO021", "start_seconds": None, "end_seconds": None}]}))
    assert res["playlist_id"] == 5 and len(res["added_tracks"]) == 1
    # An import into the crate claims no discovery and is invisible to others.
    assert q("SELECT first_added_by_user_id FROM songs WHERE video_id = 'NEWVIDEO021'")[0][0] is None
    found = ok(demo.get("/api/search", params={"q": "NEWVIDEO021", "user_id": 2}))
    assert found["tracks"] == []


def test_import_into_someone_elses_crate_is_refused(demo):
    res = demo.post("/api/playlists/import/commit", json={
        "user_id": 2, "title": "x", "target_playlist_id": 5, "tracks": [{"video_id": "NEWVIDEO022"}]})
    assert res.status_code == 404


def test_handle_keep_clears_the_prompt(google):
    from test_shell_routes import _sign_in_with_next

    _sign_in_with_next(google, None)
    me = ok(google.get("/api/bootstrap"))["current_user"]
    assert me["needs_handle"] is True
    ok(google.put("/api/me/handle", json={"handle": me["username"]}))
    assert ok(google.get("/api/bootstrap"))["current_user"]["needs_handle"] is False
    assert ok(google.get("/api/me/home"))["user"]["needs_handle"] is False


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")
def test_lane_c_modules_load_and_register():
    res = subprocess.run(
        ["node", str(ROOT / "tests" / "js" / "lane_c_smoke.mjs"), str(ROOT / "static" / "js")],
        capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "lane C smoke ok" in res.stdout

"""Phase 1a: the op log (apply_ops, undo/redo), crate, promote/fork, dock, paste resolver.

Seed facts used below (ids are stable on a fresh DB):
  playlist 1 lucas(2): tracks 1 0PsOJ_fRckg, 2 TNsSBhl_2LI, 3 c_56qQqxvm4
  playlist 2 maya(3):  tracks 4 c_56qQqxvm4, 5 4fHw4GeW3EU, 6 w2oGFEsaG5c, 7 0PsOJ_fRckg
  playlist 3 julian(4): tracks 8 -y9rkNXBuhg, 9 6647smeduGI, 10 TNsSBhl_2LI, 11 4fHw4GeW3EU
  playlist 4 dan(1):   tracks 12 BI2Et19vDCM, 13 S8fFo4REHOE, 14 0PsOJ_fRckg, 15 w2oGFEsaG5c
  crates 5 (dan, rows 16 c_56qQqxvm4 via maya/2/4 and 17 6647smeduGI via julian/3/9), 6 lucas, 7 maya, 8 julian
"""

import itertools
import re
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from test_security_backend import (  # noqa: F401  (fixtures are used by name)
    OK_HEADERS,
    db,
    db_path,
    demo,
    fakes,
    google,
)

ROOT = Path(__file__).resolve().parent.parent
_seq = itertools.count(1)


def op_id():
    return f"t{next(_seq):06d}"


def post_ops(client, uid, ops, **kw):
    body = {"user_id": uid, "op_id": kw.pop("op_id", None) or op_id(), "ops": ops, **kw}
    return client.post("/api/ops", json=body)


def ok(res, status=200):
    assert res.status_code == status, res.text
    return res.json()


def order(pid):
    conn = db()
    try:
        return [r[0] for r in conn.execute(
            "SELECT id FROM playlist_tracks WHERE playlist_id = ? ORDER BY position, id", (pid,))]
    finally:
        conn.close()


def row(tid):
    conn = db()
    try:
        r = conn.execute("SELECT * FROM playlist_tracks WHERE id = ?", (tid,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def q(sql, *args):
    conn = db()
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def undo(client, uid, **kw):
    return client.post("/api/ops/undo", json={"user_id": uid, **kw})


def redo(client, uid, **kw):
    return client.post("/api/ops/redo", json={"user_id": uid, **kw})


def insert(pid, *vids, **placement):
    return {"kind": "insert", "playlist_id": pid, "items": [{"video_id": v} for v in vids], **placement}


# --- schema ------------------------------------------------------------------------

def test_fresh_db_has_one_crate_per_user(demo):
    from app.ops import ensure_crate

    crates = q("SELECT id, user_id, visibility, title FROM playlists WHERE kind = 'crate' ORDER BY id")
    assert [(r["id"], r["user_id"], r["visibility"], r["title"]) for r in crates] == [
        (5, 1, "private", "Crate"), (6, 2, "private", "Crate"), (7, 3, "private", "Crate"), (8, 4, "private", "Crate")]
    conn = db()
    assert ensure_crate(conn, 1) == 5
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO playlists (user_id, kind, visibility, title, created_at, updated_at) "
                     "VALUES (1, 'crate', 'private', 'Crate 2', 'now', 'now')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO playlists (user_id, kind, visibility, title, created_at, updated_at) "
                     "VALUES (2, 'crate', 'public', 'x', 'now', 'now')")
    conn.close()
    assert len(q("SELECT 1 FROM playlists WHERE kind = 'crate'")) == 4
    assert q("SELECT value FROM meta WHERE key = 'schema_version'")[0][0] == "2"
    # Seeded dock: dan pins playlist 4.
    assert [tuple(r) for r in q("SELECT user_id, playlist_id, pinned FROM user_dock")] == [(1, 4, 1)]


def test_new_users_get_a_crate(demo):
    uid = ok(demo.post("/api/auth/login", json={"username": "elena"}))["user_id"]
    assert len(q("SELECT 1 FROM playlists WHERE user_id = ? AND kind = 'crate'", uid)) == 1
    boot = ok(demo.get(f"/api/bootstrap?user_id={uid}"))["current_user"]
    assert boot["crate_id"] and boot["playlists"][0]["kind"] == "crate"
    assert boot["undo"] == {"can_undo": False, "undo_label": None, "can_redo": False, "redo_label": None}


def test_pre_phase1_database_is_refused(tmp_path, monkeypatch):
    import app.database

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, display_name TEXT, created_at TEXT);
        INSERT INTO users VALUES (1, 'dan', 'Dan', 'now');
        CREATE TABLE playlists (id INTEGER PRIMARY KEY, user_id INTEGER, title TEXT, created_at TEXT, updated_at TEXT);
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(app.database, "DB_PATH", path)
    with pytest.raises(RuntimeError, match="Delete the database file"):
        app.database.init_db()


def test_signed_out_google_user_cannot_use_ops(google):
    assert google.post("/api/ops", json={"op_id": "x1", "ops": [insert(4, "TNsSBhl_2LI")]}).status_code == 401
    assert google.post("/api/ops/undo", json={}).status_code == 401
    assert google.get("/api/me/crate").status_code == 401
    assert google.post("/api/resolve-paste", json={"text": "TNsSBhl_2LI"}).status_code == 401


# --- the single write path ---------------------------------------------------------------

def test_playlist_tracks_are_only_written_by_ops_seed_and_delete():
    pattern = re.compile(r"(INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM)\s+playlist_tracks\b", re.I)
    allowed = {"ops.py", "database.py"}  # database.py: the seed only
    hits = []
    for path in (ROOT / "app").glob("*.py"):
        if path.name in allowed:
            continue
        for n, line in enumerate(path.read_text().splitlines(), start=1):
            if pattern.search(line):
                hits.append(f"{path.name}:{n}: {line.strip()}")
    assert hits == []
    seed = (ROOT / "app" / "database.py").read_text()
    for m in pattern.finditer(seed):
        # every playlist_tracks write in database.py lives inside seed_initial_data
        assert seed.rfind("def seed_initial_data", 0, m.start()) > seed.rfind("\ndef ", 0, m.start()) - 1


# --- idempotency and concurrency ------------------------------------------------------------

def test_same_op_id_and_body_is_a_replay(demo):
    body = {"user_id": 1, "op_id": "replay-1", "ops": [insert(4, "TNsSBhl_2LI")]}
    first = ok(demo.post("/api/ops", json=body))
    again = ok(demo.post("/api/ops", json=body))
    assert first["replayed"] is False and again["replayed"] is True
    assert again["results"] == first["results"]
    assert len(order(4)) == 5
    tid = first["results"][0]["track_ids"][0]
    assert again["rows"][str(tid)]["provenance"] == "none"
    assert again["new_badges"] == []


def test_op_id_reuse_with_other_body_or_user_conflicts(demo):
    ok(post_ops(demo, 1, [insert(4, "TNsSBhl_2LI")], op_id="reuse-1"))
    diff = post_ops(demo, 1, [insert(4, "6647smeduGI")], op_id="reuse-1")
    assert diff.status_code == 409 and diff.json()["code"] == "op_id_conflict"
    other = post_ops(demo, 3, [insert(2, "TNsSBhl_2LI")], op_id="reuse-1")
    assert other.status_code == 409 and other.json()["code"] == "op_id_conflict"
    assert len(order(4)) == 5 and len(order(2)) == 4


def test_stale_version_applies_nothing_and_keeps_the_op_id(demo):
    v = ok(demo.get("/api/playlists/4?user_id=1"))["version"]
    assert v == 0
    ok(post_ops(demo, 1, [insert(4, "TNsSBhl_2LI")]))
    stale = post_ops(demo, 1, [insert(4, "6647smeduGI")], op_id="stale-1", expected_versions={"4": 0})
    assert stale.status_code == 409
    body = stale.json()
    assert body["code"] == "stale_version" and isinstance(body["detail"], str)
    assert body["conflicts"] == [{"playlist_id": 4, "expected": 0, "current": 1}] and body["versions"] == {"4": 1}
    assert len(order(4)) == 5
    res = ok(post_ops(demo, 1, [insert(4, "6647smeduGI")], op_id="stale-1", expected_versions={"4": 1}))
    assert res["versions"] == {"4": 2}


def test_group_must_be_the_latest_live_group(demo):
    a = ok(post_ops(demo, 1, [insert(4, "TNsSBhl_2LI")], op_id="grp-a"))
    ok(post_ops(demo, 1, [insert(4, "6647smeduGI")], op_id="grp-b"))
    closed = post_ops(demo, 1, [insert(4, "4fHw4GeW3EU")], group_id=a["group_id"])
    assert closed.status_code == 409 and closed.json()["code"] == "group_closed"
    # Joining the latest group makes one undo unit (a >50-item paste sent in chunks).
    ok(post_ops(demo, 1, [insert(4, "4fHw4GeW3EU")], group_id="grp-b"))
    before = len(order(4))
    ok(undo(demo, 1))
    assert len(order(4)) == before - 2


# --- undo / redo -----------------------------------------------------------------------------

def test_paste_three_times_gives_three_groups(demo):
    after = 13
    made = []
    for _ in range(3):
        res = ok(post_ops(demo, 1, [{"kind": "insert", "playlist_id": 4, "after_track_id": after,
                                     "items": [{"video_id": "c_56qQqxvm4", "ref": {"track_id": 4}}]}]))
        after = res["results"][0]["track_ids"][-1]
        made.append(after)
        assert res["undo"]["can_undo"] is True
    assert order(4) == [12, 13, *made, 14, 15]
    for i in range(3):
        res = ok(undo(demo, 1))
        assert res["results"][0]["kind"] == "delete" and res["results"][0]["track_ids"] == [made[2 - i]]
    assert order(4) == [12, 13, 14, 15]
    assert ok(demo.get("/api/me/undo?user_id=1"))["can_redo"] is True


def test_cross_playlist_move_undoes_atomically_with_original_ids(demo):
    res = ok(post_ops(demo, 1, [{"kind": "move", "playlist_id": 5, "track_ids": [16], "to_playlist_id": 4,
                                 "after_track_id": 13}], expected_versions={"4": 0, "5": 0}))
    new_id = res["results"][0]["moved"][0]["track_id"]
    assert res["results"][0]["moved"] == [{"from_track_id": 16, "track_id": new_id}]
    assert res["order"] == {"4": [12, 13, new_id, 14, 15], "5": [17]}
    assert res["versions"] == {"4": 1, "5": 1}
    moved = row(new_id)
    assert (moved["source_playlist_id"], moved["source_user_id"], moved["source_track_id"]) == (2, 3, 4)
    assert res["rows"][str(new_id)]["provenance"] == "kept" and res["rows"][str(new_id)]["position"] == 3
    assert len(q("SELECT 1 FROM playlist_ops WHERE batch_id = ?", res["op_id"])) == 2

    back = ok(undo(demo, 1, group_id=res["group_id"]))
    assert [r["kind"] for r in back["results"]] == ["delete", "restore"]
    assert order(4) == [12, 13, 14, 15] and order(5) == [16, 17]
    assert row(16)["source_track_id"] == 4
    assert back["versions"] == {"4": 2, "5": 2}
    assert back["undo"]["can_redo"] is True

    again = ok(redo(demo, 1))
    assert order(4) == [12, 13, new_id, 14, 15] and order(5) == [17]
    assert again["undo"]["can_redo"] is False


def test_new_action_after_undo_discards_redo(demo):
    ok(post_ops(demo, 1, [insert(4, "TNsSBhl_2LI")]))
    ok(undo(demo, 1))
    assert ok(demo.get("/api/me/undo?user_id=1"))["can_redo"] is True
    res = ok(post_ops(demo, 1, [insert(4, "6647smeduGI")]))
    assert res["undo"]["can_redo"] is False
    assert ok(redo(demo, 1))["reason"] == "nothing_to_redo"


def test_undo_stack_survives_a_reload(demo):
    from app.main import app

    ok(post_ops(demo, 1, [{"kind": "delete", "playlist_id": 4, "track_ids": [13, 14]}]))
    assert order(4) == [12, 15]
    with TestClient(app, headers=OK_HEADERS) as fresh:
        state = ok(fresh.get("/api/me/undo?user_id=1"))
        assert state["can_undo"] and "Removed 2 tracks" in state["undo_label"]
        ok(undo(fresh, 1))
    assert order(4) == [12, 13, 14, 15]


def test_undo_of_moves_restores_exact_order(demo):
    ok(post_ops(demo, 1, [{"kind": "move", "playlist_id": 4, "track_ids": [13, 12]}]))  # block to the end
    assert order(4) == [14, 15, 12, 13]
    ok(post_ops(demo, 1, [{"kind": "move", "playlist_id": 4, "track_ids": [15], "position": 1}]))
    assert order(4) == [15, 14, 12, 13]
    ok(undo(demo, 1))
    assert order(4) == [14, 15, 12, 13]
    ok(undo(demo, 1))
    assert order(4) == [12, 13, 14, 15]
    ok(redo(demo, 1))
    assert order(4) == [14, 15, 12, 13]


def test_undo_update_and_set_meta(demo):
    ok(post_ops(demo, 1, [
        {"kind": "update", "playlist_id": 4, "track_id": 12,
         "set": {"note": "new note", "start_seconds": "1:00", "end_seconds": 90, "display_title": "Mine"}},
        {"kind": "set_meta", "playlist_id": 4, "set": {"title": "Renamed", "description": "d"}},
    ]))
    r = row(12)
    assert (r["track_note"], r["start_seconds"], r["end_seconds"], r["display_title"]) == ("new note", 60, 90, "Mine")
    detail = ok(demo.get("/api/playlists/4?user_id=1"))
    assert detail["title"] == "Renamed" and detail["version"] == 1
    assert detail["tracks"][0]["start_seconds"] == 60 and isinstance(detail["tracks"][0]["start_seconds"], int)
    ok(undo(demo, 1))
    r = row(12)
    assert (r["track_note"], r["start_seconds"], r["display_title"]) == ("The slow build at 3:15 is why we make playlists.", None, None)
    assert ok(demo.get("/api/playlists/4?user_id=1"))["title"] == "Webjam Inaugural Transmission"
    ok(redo(demo, 1))
    assert row(12)["track_note"] == "new note"


def test_undo_conflict_and_skip(demo):
    res = ok(post_ops(demo, 1, [insert(4, "TNsSBhl_2LI")]))
    tid = res["results"][0]["track_ids"][0]
    conn = db()  # someone else changed the row behind our back
    conn.execute("UPDATE playlist_tracks SET video_id = '6647smeduGI' WHERE id = ?", (tid,))
    conn.commit()
    conn.close()
    conflict = undo(demo, 1)
    assert conflict.status_code == 409
    body = conflict.json()
    assert body["code"] == "undo_conflict" and body["conflicts"][0]["track_id"] == tid
    assert row(tid) is not None
    skipped = ok(undo(demo, 1, skip=True))
    assert skipped["skipped"] is True and row(tid) is not None
    assert ok(demo.get("/api/me/undo?user_id=1"))["can_undo"] is False


def test_undo_asserts_the_expected_group(demo):
    ok(post_ops(demo, 1, [insert(4, "TNsSBhl_2LI")], op_id="expect-1"))
    moved = undo(demo, 1, group_id="not-the-top")
    assert moved.status_code == 409 and moved.json()["code"] == "stack_moved"
    assert moved.json()["group_id"] == "expect-1"
    assert ok(undo(demo, 1, group_id="expect-1"))["group_id"] == "expect-1"
    assert ok(undo(demo, 1))["reason"] == "nothing_to_undo"


def test_undoing_a_claiming_insert_reverts_the_discovery(demo):
    res = ok(post_ops(demo, 1, [insert(4, "NEWVIDEO001")]))
    assert q("SELECT first_added_by_user_id FROM songs WHERE video_id = 'NEWVIDEO001'")[0][0] == 1
    assert q("SELECT COUNT(*) FROM activity_log WHERE action_type = 'discovered_song' AND link_id = 'NEWVIDEO001'")[0][0] == 1
    # Maya adds it too, later: no claim (already claimed by dan).
    ok(post_ops(demo, 3, [insert(2, "NEWVIDEO001")]))
    ok(undo(demo, 1, group_id=res["group_id"]))
    song = q("SELECT first_added_by_user_id, first_added_playlist_id FROM songs WHERE video_id = 'NEWVIDEO001'")[0]
    assert tuple(song) == (3, 2)  # re-claimed by the earliest remaining public placement
    assert q("SELECT COUNT(*) FROM activity_log WHERE actor_user_id = 1 AND link_id = 'NEWVIDEO001'")[0][0] == 0
    # A crate-only song has no finder at all.
    ok(post_ops(demo, 1, [insert(5, "NEWVIDEO002")]))
    assert q("SELECT first_added_by_user_id FROM songs WHERE video_id = 'NEWVIDEO002'")[0][0] is None


def test_deleting_a_playlist_discards_its_undo_groups(demo):
    pid = ok(demo.post("/api/playlists", json={"user_id": 1, "title": "Temp"}))["playlist_id"]
    ok(post_ops(demo, 1, [{"kind": "move", "playlist_id": 5, "track_ids": [16], "to_playlist_id": pid}]))
    ok(post_ops(demo, 1, [insert(pid, "TNsSBhl_2LI")]))
    ok(post_ops(demo, 1, [insert(4, "6647smeduGI")]))
    gone = ok(demo.delete(f"/api/playlists/{pid}?user_id=1"))
    assert gone == {"ok": True, "playlist_id": pid, "discarded_undo_groups": 2}
    ok(undo(demo, 1))  # the playlist-4 insert
    assert ok(undo(demo, 1))["reason"] == "nothing_to_undo"  # the crate->Temp move can't come back
    assert order(5) == [17]


# --- versions, positions --------------------------------------------------------------------

def test_versions_bump_once_per_batch(demo):
    res = ok(post_ops(demo, 1, [insert(4, "TNsSBhl_2LI"), insert(4, "6647smeduGI"),
                                {"kind": "set_meta", "playlist_id": 4, "set": {"description": "x"}}]))
    assert res["versions"] == {"4": 1}
    # Likes and plays don't change it.
    demo.post("/api/playlists/4/like", json={"user_id": 2})
    demo.post("/api/playlists/4/play", json={"video_id": "BI2Et19vDCM"})
    assert ok(demo.get("/api/playlists/4?user_id=1"))["version"] == 1


def test_sixty_inserts_at_one_spot_keep_order_and_renormalise(demo):
    made = []
    for chunk in range(3):
        res = ok(post_ops(demo, 1, [insert(4, "TNsSBhl_2LI", after_track_id=12) for _ in range(20)]))
        made += [r["track_ids"][0] for r in res["results"]]
    assert order(4) == [12, *reversed(made), 13, 14, 15]
    positions = [r[0] for r in q("SELECT position FROM playlist_tracks WHERE playlist_id = 4 ORDER BY position, id")]
    gaps = [b - a for a, b in zip(positions, positions[1:])]
    assert min(gaps) > 1e-7  # halving the gap 60 times would leave ~1e-18 without renormalisation
    detail = ok(demo.get("/api/playlists/4?user_id=1"))
    assert [t["position"] for t in detail["tracks"]] == list(range(1, 65))


def test_position_and_block_moves(demo):
    res = ok(post_ops(demo, 1, [insert(4, "TNsSBhl_2LI", "6647smeduGI", position=2)]))
    a, b = res["results"][0]["track_ids"]
    assert order(4) == [12, a, b, 13, 14, 15]
    assert res["rows"][str(a)]["position"] == 2
    ok(post_ops(demo, 1, [insert(4, "4fHw4GeW3EU", position=99)]))  # clamped: appended
    assert order(4)[-1] not in (12, 13, 14, 15)
    # The moved block keeps its current relative order whatever order the ids are given in.
    ok(post_ops(demo, 1, [{"kind": "move", "playlist_id": 4, "track_ids": [15, 12], "after_track_id": 13}]))
    assert order(4)[:5] == [a, b, 13, 12, 15]


def test_placement_errors(demo):
    both = post_ops(demo, 1, [insert(4, "TNsSBhl_2LI", after_track_id=12, position=1)])
    assert both.status_code == 400 and both.json()["code"] == "bad_placement"
    gone = post_ops(demo, 1, [insert(4, "TNsSBhl_2LI", after_track_id=1)])  # track 1 is in playlist 1
    assert gone.status_code == 409 and gone.json()["code"] == "anchor_missing"
    self_anchor = post_ops(demo, 1, [{"kind": "move", "playlist_id": 4, "track_ids": [12, 13], "after_track_id": 13}])
    assert self_anchor.status_code == 400 and self_anchor.json()["code"] == "bad_placement"
    missing = post_ops(demo, 1, [insert(4, "TNsSBhl_2LI"), {"kind": "delete", "playlist_id": 4, "track_ids": [1]}])
    assert missing.status_code == 404 and missing.json() == {
        "detail": "Track not found in playlist", "code": "track_not_found", "op_index": 1, "track_id": 1}
    assert len(order(4)) == 4  # the whole batch rolled back
    assert post_ops(demo, 1, [{"kind": "restore", "playlist_id": 4, "rows": []}]).status_code == 422


def test_roles(demo):
    others = post_ops(demo, 1, [insert(2, "TNsSBhl_2LI")])
    assert others.status_code == 403 and others.json()["code"] == "not_allowed"
    crate = post_ops(demo, 1, [insert(6, "TNsSBhl_2LI")])  # lucas's crate: invisible, so 404
    assert crate.status_code == 404 and crate.json()["code"] == "playlist_not_found"
    into_other = post_ops(demo, 1, [{"kind": "move", "playlist_id": 5, "track_ids": [16], "to_playlist_id": 2}])
    assert into_other.status_code == 403
    fixed = post_ops(demo, 1, [{"kind": "set_meta", "playlist_id": 5, "set": {"visibility": "public"}}])
    assert fixed.status_code == 400 and fixed.json()["code"] == "crate_visibility_fixed"
    ok(post_ops(demo, 1, [{"kind": "set_meta", "playlist_id": 5, "set": {"title": "Dig bin"}}]))


# --- provenance -------------------------------------------------------------------------------

def _inserted(res):
    tid = res["results"][0]["track_ids"][0]
    return tid, row(tid), res["rows"][str(tid)]["provenance"]


def test_copy_from_someone_else_credits_that_owner(demo):
    tid, r, prov = _inserted(ok(post_ops(demo, 1, [
        {"kind": "insert", "playlist_id": 4, "items": [{"video_id": "4fHw4GeW3EU", "ref": {"track_id": 5}}]}])))
    assert prov == "credited"
    assert (r["added_by_user_id"], r["source_playlist_id"], r["source_user_id"], r["source_track_id"]) == (1, 2, 3, 5)
    assert q("SELECT summary FROM activity_log WHERE action_type = 'grabbed_track' ORDER BY id DESC LIMIT 1")[0][0] == \
        "grabbed 'Kiasmos - Full Performance (Live on KEXP)' from @maya into 'Webjam Inaugural Transmission'"


def test_copy_from_your_own_crate_keeps_the_original_credit(demo):
    tid, r, prov = _inserted(ok(post_ops(demo, 1, [
        {"kind": "insert", "playlist_id": 4, "items": [{"video_id": "c_56qQqxvm4", "ref": {"track_id": 16}}]}])))
    assert prov == "kept"
    assert (r["source_playlist_id"], r["source_user_id"], r["source_track_id"]) == (2, 3, 4)
    assert order(5) == [16, 17]  # a copy, the crate row stays


def test_bad_refs_are_dropped(demo):
    ok(post_ops(demo, 3, [{"kind": "set_meta", "playlist_id": 2, "set": {"visibility": "private"}}]))
    _, r, prov = _inserted(ok(post_ops(demo, 1, [
        {"kind": "insert", "playlist_id": 4, "items": [{"video_id": "c_56qQqxvm4", "ref": {"track_id": 4}}]}])))
    assert prov == "dropped" and r["source_user_id"] is None and r["source_playlist_id"] is None
    _, r, prov = _inserted(ok(post_ops(demo, 1, [
        {"kind": "insert", "playlist_id": 4, "items": [{"video_id": "TNsSBhl_2LI", "ref": {"track_id": 9}}]}])))
    assert prov == "dropped" and r["source_user_id"] is None  # row 9 is a different song
    _, r, prov = _inserted(ok(post_ops(demo, 1, [
        {"kind": "insert", "playlist_id": 4, "items": [{"video_id": "4fHw4GeW3EU", "ref": {"playlist_id": 2}}]}])))
    assert prov == "dropped"  # playlist 2 is private now: no "does it contain X" oracle
    _, r, prov = _inserted(ok(post_ops(demo, 1, [
        {"kind": "insert", "playlist_id": 4, "items": [{"video_id": "TNsSBhl_2LI", "ref": {"playlist_id": 3}}]}])))
    assert prov == "credited" and (r["source_playlist_id"], r["source_user_id"], r["source_track_id"]) == (3, 4, None)


def test_legacy_grab_from_private_playlist_is_not_credited(demo):
    ok(post_ops(demo, 4, [{"kind": "set_meta", "playlist_id": 3, "set": {"visibility": "private"}}]))
    res = ok(demo.post("/api/playlists/4/tracks", json={"user_id": 1, "urls_or_ids": "-y9rkNXBuhg", "source_playlist_id": 3}))
    r = row(res["results"][0]["track_id"])
    assert r["source_user_id"] is None and r["source_playlist_id"] is None


# --- caps ---------------------------------------------------------------------------------------

def test_caps(demo):
    big = post_ops(demo, 1, [insert(4, *(["TNsSBhl_2LI"] * 51))])
    assert big.status_code == 400 and big.json()["code"] == "batch_too_large"
    many_ops = post_ops(demo, 1, [insert(4, "TNsSBhl_2LI")] * 21)
    assert many_ops.status_code == 400 and many_ops.json()["code"] == "batch_too_large"
    pids = [ok(demo.post("/api/playlists", json={"user_id": 1, "title": f"p{i}"}))["playlist_id"] for i in range(11)]
    wide = post_ops(demo, 1, [insert(p, "TNsSBhl_2LI") for p in pids])
    assert wide.status_code == 400 and wide.json()["code"] == "batch_too_large"
    conn = db()
    conn.executemany(
        "INSERT INTO playlist_tracks (playlist_id, video_id, position, added_by_user_id, created_at) VALUES (4, 'TNsSBhl_2LI', ?, 1, 'now')",
        [(100.0 + i,) for i in range(996)],
    )
    conn.commit()
    conn.close()
    full = post_ops(demo, 1, [insert(4, "TNsSBhl_2LI")])
    assert full.status_code == 400 and full.json()["code"] == "playlist_full"
    assert len(order(4)) == 1000


def test_promote_and_fork_handle_1000_tracks(demo):
    conn = db()
    conn.execute("DELETE FROM playlist_tracks WHERE playlist_id = 5")
    conn.executemany(
        "INSERT INTO playlist_tracks (playlist_id, video_id, position, added_by_user_id, created_at) VALUES (5, 'TNsSBhl_2LI', ?, 1, 'now')",
        [(float(i),) for i in range(1, 1001)],
    )
    conn.commit()
    conn.close()
    crate_ids = order(5)
    res = ok(demo.post("/api/me/crate/promote", json={"user_id": 1, "title": "Big"}), 201)
    assert res["moved"] == 1000 and res["crate"]["track_count"] == 0
    assert len(order(res["playlist_id"])) == 1000
    fork = ok(demo.post(f"/api/playlists/{res['playlist_id']}/fork", json={"user_id": 2}), 201)
    assert fork["track_count"] == 1000 and len(order(fork["playlist_id"])) == 1000
    assert crate_ids  # (ids were generated)


# --- promote, unload, fork ---------------------------------------------------------------------

def _metric(client, uid, key):
    return ok(client.get(f"/api/users/{uid}?viewer_id=2"))["metrics"][key]


def test_promote_credits_maya_once_public_and_undo_returns_rows(demo):
    maya_before = _metric(demo, 3, "tracks_taken_from_me")
    res = ok(demo.post("/api/me/crate/promote", json={"user_id": 1, "title": "Late Shift"}), 201)
    pid = res["playlist_id"]
    assert res["moved"] == 2 and res["crate"] == {"id": 5, "version": 1, "track_count": 0}
    assert res["versions"] == {"5": 1, str(pid): 1}
    rows = [row(t) for t in order(pid)]
    assert [r["video_id"] for r in rows] == ["c_56qQqxvm4", "6647smeduGI"]  # crate order
    assert [(r["source_user_id"], r["source_playlist_id"]) for r in rows] == [(3, 2), (4, 3)]
    assert _metric(demo, 3, "tracks_taken_from_me") == maya_before + 1
    feed = ok(demo.get("/api/feed?user_id=2"))
    assert pid in [p["id"] for p in feed["playlists"]]
    assert any("Late Shift" in a["summary"] and a["action_type"] == "created_playlist" for a in feed["activity"])

    back = ok(undo(demo, 1))
    assert back["notes"] == [{"code": "playlist_left_empty", "playlist_id": pid}]
    assert order(5) == [16, 17] and order(pid) == []
    assert _metric(demo, 3, "tracks_taken_from_me") == maya_before
    ok(redo(demo, 1))
    assert len(order(pid)) == 2 and order(5) == []


def test_promote_errors_and_partial_promote(demo):
    assert demo.post("/api/me/crate/promote", json={"user_id": 1, "title": "  "}).json()["code"] == "bad_title"
    nf = demo.post("/api/me/crate/promote", json={"user_id": 1, "title": "x", "track_ids": [12]})
    assert nf.status_code == 404 and nf.json()["code"] == "track_not_found"
    res = ok(demo.post("/api/me/crate/promote", json={"user_id": 1, "title": "One", "track_ids": [17],
                                                     "visibility": "private"}), 201)
    assert order(5) == [16] and len(order(res["playlist_id"])) == 1
    empty = demo.post("/api/me/crate/promote", json={"user_id": 2, "title": "x"})
    assert empty.status_code == 400 and empty.json()["code"] == "crate_empty"


def test_unload_moves_and_keep_a_copy(demo):
    grabs_before = len(q("SELECT 1 FROM activity_log WHERE action_type = 'grabbed_track'"))
    ok(post_ops(demo, 1, [{"kind": "move", "playlist_id": 5, "track_ids": [17], "to_playlist_id": 4, "position": 1}]))
    first = row(order(4)[0])
    assert (first["video_id"], first["source_user_id"]) == ("6647smeduGI", 4)
    lines = q("SELECT summary FROM activity_log WHERE action_type = 'grabbed_track' ORDER BY id")
    assert len(lines) == grabs_before + 1 and "from @julian" in lines[-1][0]
    # Keep a copy: insert with a ref to the crate row; the crate row stays put.
    ok(post_ops(demo, 1, [{"kind": "insert", "playlist_id": 4, "items": [{"video_id": "c_56qQqxvm4", "ref": {"track_id": 16}}]}]))
    assert order(5) == [16]
    crate = ok(demo.get("/api/me/crate?user_id=1"))
    assert crate["kind"] == "crate" and crate["visibility"] == "private"
    assert crate["tracks"][0]["also_in"] == [{"playlist_id": 4, "title": "Webjam Inaugural Transmission"}]
    assert crate["comments"] == [] and crate["tracks"][0]["source_username"] == "maya"


def test_filling_the_crate_logs_nothing(demo):
    activity = len(q("SELECT 1 FROM activity_log"))
    ok(post_ops(demo, 2, [{"kind": "insert", "playlist_id": 6, "items": [{"video_id": "4fHw4GeW3EU", "ref": {"track_id": 5}}]}]))
    assert len(q("SELECT 1 FROM activity_log")) == activity
    assert row(order(6)[0])["source_user_id"] == 3


def test_fork_credits_the_source_owner(demo):
    res = ok(demo.post("/api/playlists/1/fork", json={"user_id": 1}), 201)
    fid = res["playlist_id"]
    assert res["forked_from"] == {"playlist_id": 1, "title": "Sunday Morning Coffee & Tape Hiss", "username": "lucas"}
    assert res["version"] == 1 and res["track_count"] == 3
    rows = [row(t) for t in order(fid)]
    assert [(r["source_user_id"], r["source_playlist_id"], r["source_track_id"]) for r in rows] == [(2, 1, 1), (2, 1, 2), (2, 1, 3)]
    assert rows[0]["track_note"] == "Mark Speer's guitar tone in this KEXP room is pure warm sunlight."
    detail = ok(demo.get(f"/api/playlists/{fid}?user_id=3"))
    assert detail["forked_from"]["username"] == "lucas"
    assert ok(demo.get("/api/me/undo?user_id=1"))["can_undo"] is False  # not on the undo stack
    assert q("SELECT summary FROM activity_log WHERE action_type = 'forked_playlist'")[0][0] == \
        "forked 'Sunday Morning Coffee & Tape Hiss' from @lucas"
    assert q("SELECT COUNT(*) FROM activity_log WHERE link_type = 'playlist' AND link_id = ?", str(fid))[0][0] == 1


def test_fork_own_playlist_keeps_provenance_and_crates_cant_be_forked(demo):
    res = ok(demo.post("/api/playlists/4/fork", json={"user_id": 1, "title": "Copy", "visibility": "unlisted"}), 201)
    rows = [row(t) for t in order(res["playlist_id"])]
    assert [r["source_user_id"] for r in rows] == [None, None, 2, 3]
    assert demo.post("/api/playlists/5/fork", json={"user_id": 1}).json()["code"] == "use_promote"
    assert demo.post("/api/playlists/5/fork", json={"user_id": 2}).status_code == 404


def test_delete_playlist_cascade(demo):
    # Lucas deletes playlist 1; rows elsewhere that came from it keep @lucas but lose the playlist link.
    assert demo.delete("/api/playlists/1?user_id=1").status_code == 403
    assert demo.delete("/api/playlists/6?user_id=1").status_code == 404
    crate = demo.delete("/api/playlists/6?user_id=2")
    assert crate.status_code == 400 and crate.json()["code"] == "cannot_delete_crate"
    res = ok(demo.delete("/api/playlists/1?user_id=2"))
    assert res["ok"] is True and res["playlist_id"] == 1
    assert q("SELECT 1 FROM playlists WHERE id = 1") == []
    assert q("SELECT COUNT(*) FROM playlist_tracks WHERE playlist_id = 1")[0][0] == 0
    assert q("SELECT COUNT(*) FROM comments WHERE target_type = 'playlist' AND target_id = '1'")[0][0] == 0
    assert row(7)["source_playlist_id"] is None and row(7)["source_user_id"] == 2
    song = q("SELECT first_added_by_user_id, first_added_playlist_id FROM songs WHERE video_id = '0PsOJ_fRckg'")[0]
    assert tuple(song) == (2, None)
    assert demo.get("/api/playlists/1").status_code == 404
    assert demo.get("/api/users/2?viewer_id=2").status_code == 200


# --- legacy endpoints go through apply_ops ------------------------------------------------------

def test_legacy_writes_are_undoable_and_report_versions(demo):
    add = ok(demo.post("/api/playlists/4/tracks", json={"user_id": 1, "urls_or_ids": "TNsSBhl_2LI"}))
    assert add["version"] == 1
    ok(demo.put("/api/playlists/4/reorder", json={"user_id": 1, "track_ids": list(reversed(order(4)))}))
    assert ok(demo.put("/api/playlists/4", json={"user_id": 1, "title": "T", "description": "D"}))["version"] == 3
    mv = ok(demo.put("/api/playlists/4/tracks/12", json={"user_id": 1, "direction": "up"}))  # 12 is last after the reversal
    assert mv["version"] == 4
    ok(demo.delete("/api/playlists/4/tracks/13?user_id=1"))
    for _ in range(5):
        ok(undo(demo, 1))
    assert order(4) == [12, 13, 14, 15]
    assert ok(demo.get("/api/playlists/4?user_id=1"))["title"] == "Webjam Inaugural Transmission"


def test_reorder_uses_minimal_moves(demo):
    from app.main import reorder_moves

    assert reorder_moves([1, 2, 3, 4, 5], [1, 2, 3, 4, 5]) == []
    assert len(reorder_moves([1, 2, 3, 4, 5], [2, 3, 4, 5, 1])) == 1
    assert len(reorder_moves([1, 2, 3, 4, 5], [5, 4, 3, 2, 1])) == 4
    ok(demo.put("/api/playlists/4/reorder", json={"user_id": 1, "track_ids": [15, 12, 13, 14]}))
    assert order(4) == [15, 12, 13, 14]
    assert len(q("SELECT 1 FROM playlist_ops WHERE kind = 'move'")) == 1  # one drag = one move


def test_quick_drop_into_crate_is_private(demo):
    res = ok(demo.post("/api/playlists/5/tracks", json={"user_id": 1, "urls_or_ids": "NEWVIDEO003"}))
    assert res["results"][0]["is_new_discovery"] is False
    assert q("SELECT first_added_by_user_id FROM songs WHERE video_id = 'NEWVIDEO003'")[0][0] is None


def test_import_commit_can_target_the_crate(demo):
    res = ok(demo.post("/api/playlists/import/commit", json={
        "user_id": 1, "title": "ignored", "target_playlist_id": 5,
        "tracks": [{"video_id": "TNsSBhl_2LI", "migrated_from_video_id": "Z5NoQg8LdDk", "migrated_from_title": "Old"}]}))
    assert res["playlist_id"] == 5
    r = row(order(5)[-1])
    assert (r["migrated_from_video_id"], r["migrated_from_title"]) == ("Z5NoQg8LdDk", "Old")


def test_private_playlist_create_logs_nothing(demo):
    activity = len(q("SELECT 1 FROM activity_log"))
    res = ok(demo.post("/api/playlists", json={"user_id": 1, "title": "Secret", "visibility": "private",
                                               "initial_urls": "NEWVIDEO004"}))
    assert len(q("SELECT 1 FROM activity_log")) == activity
    assert res["added_tracks"][0]["is_new_discovery"] is False
    # Publishing it later claims discovery and logs "published".
    ok(post_ops(demo, 1, [{"kind": "set_meta", "playlist_id": res["playlist_id"], "set": {"visibility": "public"}}]))
    assert q("SELECT first_added_by_user_id FROM songs WHERE video_id = 'NEWVIDEO004'")[0][0] == 1
    assert q("SELECT action_type FROM activity_log ORDER BY id DESC LIMIT 1")[0][0] == "published_playlist"


# --- resolve-paste ------------------------------------------------------------------------------

def paste(client, text, uid=1, html=None):
    return ok(client.post("/api/resolve-paste", json={"user_id": uid, "text": text, "html": html}))


def test_paste_youtube_forms(demo):
    songs_before = q("SELECT COUNT(*) FROM songs")[0][0]
    res = paste(demo, "https://youtu.be/NEWVIDEO010?t=312\nhttps://www.youtube.com/watch?v=c_56qQqxvm4&list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG"
                      "\nhttps://www.youtube.com/playlist?list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG https://youtu.be/NEWVIDEO010")
    assert [i["video_id"] for i in res["items"]] == ["NEWVIDEO010", "c_56qQqxvm4", "NEWVIDEO010"]  # duplicates kept
    assert res["items"][0]["start_seconds"] == 312 and res["items"][0]["known"] is False
    assert res["items"][0]["title"] == "Real Title NEWVIDEO010"
    assert res["items"][1]["known"] is True and res["items"][1]["title"] == "Tycho - Full Performance (Live on KEXP)"
    assert [p["type"] for p in res["pointers"]] == ["youtube_playlist", "youtube_playlist"]
    assert res["pointers"][1]["source_input"].endswith("/playlist?list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG")
    assert res["sources"] == ["youtube_url", "youtube_playlist"]
    assert q("SELECT COUNT(*) FROM songs")[0][0] == songs_before  # never writes
    assert paste(demo, "just some words, nothing here") == {"sources": [], "items": [], "pointers": [], "unresolved": []}
    bare = paste(demo, "TNsSBhl_2LI, 6647smeduGI")
    assert [i["video_id"] for i in bare["items"]] == ["TNsSBhl_2LI", "6647smeduGI"]
    missing = paste(demo, "https://youtu.be/zzzzzzzzzzz")
    assert missing["items"] == [] and missing["unresolved"] == [{"token": "zzzzzzzzzzz", "reason": "not_found"}]


def test_paste_webjam_urls(demo):
    whole = paste(demo, "http://localhost:8000/p/2-2am-terminal-glow")
    assert [i["ref"]["track_id"] for i in whole["items"]] == [4, 5, 6, 7]
    assert whole["items"][0]["ref"] == {"track_id": 4, "playlist_id": 2, "username": "maya"}
    assert whole["pointers"][0]["type"] == "webjam_playlist" and whole["pointers"][0]["playlist_id"] == 2
    one = paste(demo, "http://localhost/p/2?track=4  Tycho - Full Performance (Live on KEXP) (5:12–8:18)")
    assert len(one["items"]) == 1 and one["items"][0]["note"].startswith("Scott Hansen")
    assert paste(demo, "/#playlist/3")["items"][0]["ref"]["playlist_id"] == 3
    clip = paste(demo, "http://localhost/t/c_56qQqxvm4?s=312&e=498")["items"][0]
    assert (clip["start_seconds"], clip["end_seconds"], clip["ref"]) == (312, 498, None)
    assert paste(demo, "http://localhost/#song/TNsSBhl_2LI")["items"][0]["video_id"] == "TNsSBhl_2LI"
    # Someone else's crate looks exactly like a missing playlist.
    hidden = paste(demo, "http://localhost/p/5 http://localhost/p/999", uid=3)
    assert hidden["items"] == [] and [u["reason"] for u in hidden["unresolved"]] == ["not_found", "not_found"]
    assert paste(demo, "http://localhost/p/5", uid=1)["items"][0]["ref"]["track_id"] == 16
    # A foreign host is not a Webjam link.
    assert paste(demo, "https://evil.example/p/2")["items"] == []


def test_paste_json_payload_and_html(demo):
    payload = ('{"webjam": 1, "items": ['
               '{"video_id": "c_56qQqxvm4", "title": "FORGED", "ref": {"track_id": 4, "username": "nobody"}},'
               '{"video_id": "c_56qQqxvm4", "ref": {"track_id": 16}},'
               '{"video_id": "TNsSBhl_2LI", "ref": {"track_id": 4}, "start_seconds": 10}]}')
    res = paste(demo, payload, uid=3)
    assert res["sources"] == ["webjam_json"]
    assert res["items"][0]["title"] == "Tycho - Full Performance (Live on KEXP)"
    assert res["items"][0]["ref"] == {"track_id": 4, "playlist_id": 2, "username": "maya"}
    assert res["items"][1]["ref"] is None  # dan's crate row, not visible to maya
    assert res["items"][2]["ref"] is None and res["items"][2]["start_seconds"] == 10  # video mismatch
    html = "<ul data-webjam='" + payload.replace('"', "&quot;") + "'><li>x</li></ul>"
    assert len(paste(demo, "ignored text", uid=3, html=html)["items"]) == 3
    # The items go straight into an insert op (display hints are ignored there).
    items = paste(demo, payload, uid=1)["items"]
    res = ok(post_ops(demo, 1, [{"kind": "insert", "playlist_id": 4, "items": items}]))
    assert [res["rows"][str(t)]["provenance"] for t in res["results"][0]["track_ids"]] == ["credited", "kept", "none"]


# --- dock -------------------------------------------------------------------------------------

def test_dock_tracks_recent_writes_and_pins(demo):
    dock = ok(demo.get("/api/me/dock?user_id=1"))
    assert dock["crate"]["slot"] == 0 and dock["crate"]["playlist_id"] == 5 and dock["crate"]["track_count"] == 2
    assert dock["crate"]["thumbs"][0].endswith("/c_56qQqxvm4/hqdefault.jpg")
    assert [(c["slot"], c["playlist_id"]) for c in dock["pinned"]] == [(1, 4)]
    assert dock["pinned"][0]["last_used_at"] is None and len(dock["pinned"][0]["thumbs"]) == 4
    ok(post_ops(demo, 1, [insert(4, "TNsSBhl_2LI")]))
    assert ok(demo.get("/api/me/dock?user_id=1"))["pinned"][0]["last_used_at"] is not None
    pids = [ok(demo.post("/api/playlists", json={"user_id": 1, "title": f"d{i}"}))["playlist_id"] for i in range(12)]
    dock = ok(demo.get("/api/me/dock?user_id=1"))
    assert len(dock["recent"]) == 5 and dock["recent"][0]["slot"] == 2
    dock = ok(demo.put("/api/me/dock", json={"user_id": 1, "pinned": [4] + pids[:11]}))
    slots = [c["slot"] for c in dock["pinned"] + dock["recent"]]
    assert slots[:9] == list(range(1, 10)) and set(slots[9:]) == {None}
    assert demo.put("/api/me/dock", json={"user_id": 1, "pinned": [4, 4]}).json()["code"] == "bad_dock"
    assert demo.put("/api/me/dock", json={"user_id": 1, "pinned": [5]}).json()["code"] == "bad_dock"
    assert demo.put("/api/me/dock", json={"user_id": 1, "pinned": pids[:12] + [4]}).json()["code"] == "bad_dock"
    assert demo.put("/api/me/dock", json={"user_id": 1, "pinned": [2]}).status_code == 404


# --- page routes -------------------------------------------------------------------------------

def test_clipboard_page_routes_serve_the_spa(demo):
    for path in ("/p/2", "/p/2-2am-terminal-glow", "/t/c_56qQqxvm4"):
        res = demo.get(path)
        assert res.status_code == 200 and "<html" in res.text.lower()
    assert demo.get("/p/notanid").status_code == 404

"""Regressions for the Phase 0 review findings (undo ids, provenance, invites,
sessions, demo-mode exposure, plays, handles, search quota, input bounds, races)."""

import asyncio
import os
import re
import stat
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from test_security_backend import (  # noqa: F401  (fixtures are used by name)
    OK_HEADERS,
    FakeGoogle,
    FakeResponse,
    _clear_auth_env,
    _google_env,
    db,
    db_path,
    demo,
    fake_metadata,
    fakes,
    google,
    google_sign_in,
)

ROOT = Path(__file__).resolve().parent.parent
# The `fakes` fixture swaps httpx.AsyncClient (module-wide) for a fake Google client.
REAL_ASYNC_CLIENT = httpx.AsyncClient
APP_JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")


def sign_in_as(client, sub, email, name):
    FakeGoogle.userinfo = {"sub": sub, "email": email, "email_verified": True, "name": name}
    cb, _ = google_sign_in(client)
    assert cb.status_code == 303 and cb.headers["location"] == "/", cb.text
    return client.get("/api/bootstrap").json()["current_user"]["id"]


def new_client(**kw):
    from app.main import app

    return TestClient(app, base_url="https://testserver", headers=OK_HEADERS, **kw)


def track_row(track_id):
    conn = db()
    try:
        return dict(conn.execute("SELECT * FROM playlist_tracks WHERE id = ?", (track_id,)).fetchone())
    finally:
        conn.close()


# --- undo (replaces the snapshot stopgap): ids are never client-chosen ---------------

def test_undo_restores_deleted_rows_under_their_old_ids(demo):
    pid = demo.post("/api/playlists", json={"user_id": 1, "title": "mine", "initial_urls": "0PsOJ_fRckg"}).json()["playlist_id"]
    # A client can't choose a row id at all: unknown fields are rejected.
    forged = demo.post("/api/ops", json={"user_id": 1, "op_id": "forge-id", "ops": [
        {"kind": "insert", "playlist_id": pid, "items": [{"id": 2**63 - 1, "video_id": "0PsOJ_fRckg"}]}]})
    assert forged.status_code == 422
    conn = db()
    assert conn.execute("SELECT MAX(id) FROM playlist_tracks").fetchone()[0] < 10_000
    conn.close()
    assert demo.post(f"/api/playlists/{pid}/tracks", json={"user_id": 1, "urls_or_ids": "TNsSBhl_2LI"}).status_code == 200

    # Undoing a delete still brings the row back under its old id.
    pl = demo.get(f"/api/playlists/{pid}?user_id=1").json()
    tid = pl["tracks"][-1]["id"]
    assert demo.delete(f"/api/playlists/{pid}/tracks/{tid}?user_id=1").status_code == 200
    assert demo.post("/api/ops/undo", json={"user_id": 1}).status_code == 200
    assert [t["id"] for t in demo.get(f"/api/playlists/{pid}?user_id=1").json()["tracks"]] == [t["id"] for t in pl["tracks"]]


# --- grab credit can't be moved onto another song ---------------------------------

def test_swap_drops_forged_grab_credit_and_undo_restores_it(demo):
    # Seed playlist 1 (owner 2) contains 0PsOJ_fRckg and TNsSBhl_2LI but not S8fFo4REHOE.
    pid = demo.post("/api/playlists", json={"user_id": 3, "title": "grabs"}).json()["playlist_id"]

    def grab():
        res = demo.post(f"/api/playlists/{pid}/tracks",
                        json={"user_id": 3, "urls_or_ids": "0PsOJ_fRckg", "source_playlist_id": 1}).json()
        tid = res["results"][0]["track_id"]
        assert track_row(tid)["source_user_id"] == 2
        return tid

    tid = grab()
    r = demo.post("/api/ops", json={"user_id": 3, "op_id": "swap-1", "ops": [
        {"kind": "update", "playlist_id": pid, "track_id": tid, "set": {"video_id": "S8fFo4REHOE"}}]})
    assert r.status_code == 200
    row = track_row(tid)
    assert row["video_id"] == "S8fFo4REHOE" and row["source_user_id"] is None and row["source_playlist_id"] is None
    # Undoing that (back to the grabbed song) restores the credit.
    assert demo.post("/api/ops/undo", json={"user_id": 3}).status_code == 200
    row = track_row(tid)
    assert row["video_id"] == "0PsOJ_fRckg" and row["source_user_id"] == 2

    tid2 = grab()
    r = demo.put(f"/api/playlists/{pid}/tracks/{tid2}",
                 json={"user_id": 3, "new_video_id": "S8fFo4REHOE", "custom_title": "Their worst song"})
    assert r.status_code == 200
    assert track_row(tid2)["source_user_id"] is None
    # Swapping to a song the source playlist really has keeps the credit.
    tid3 = grab()
    demo.put(f"/api/playlists/{pid}/tracks/{tid3}", json={"user_id": 3, "new_video_id": "TNsSBhl_2LI"})
    assert track_row(tid3)["source_user_id"] == 2

    lineage = demo.get("/api/users/2").json()["grabs_from_user"]
    assert "S8fFo4REHOE" not in {g["video_id"] for g in lineage}


# --- invites ----------------------------------------------------------------------

def test_removing_an_invite_signs_that_person_out(google):
    from app.invite import remove_invite

    google_sign_in(google)
    assert google.post("/api/playlists", json={"title": "ok"}).status_code == 200
    conn = db()
    assert remove_invite(conn, "friend@example.com")
    conn.close()
    assert google.post("/api/playlists", json={"title": "still here"}).status_code == 401


def test_session_dies_when_invite_row_is_deleted_directly(google):
    google_sign_in(google)
    conn = db()
    conn.execute("DELETE FROM invites")
    conn.commit()
    conn.close()
    assert google.get("/api/bootstrap").json()["auth"]["signed_in"] is False


def test_invite_env_var_is_authoritative_on_restart(google):
    from app.invite import add_invite, is_invited

    conn = db()
    add_invite(conn, "other@example.com")
    conn.close()
    other = new_client()
    sign_in_as(other, "g-2", "other@example.com", "Other Person")
    assert other.post("/api/playlists", json={"title": "x"}).status_code == 200

    with new_client():  # restart: WEBJAM_INVITE_EMAILS is still just friend@example.com
        pass
    conn = db()
    assert not is_invited(conn, "other@example.com") and is_invited(conn, "friend@example.com")
    conn.close()
    assert other.post("/api/playlists", json={"title": "y"}).status_code == 401
    FakeGoogle.userinfo = {"sub": "g-2", "email": "other@example.com", "email_verified": True, "name": "Other"}
    cb, _ = google_sign_in(other)
    assert cb.headers["location"] == "/#not-invited"


def test_google_mode_warns_when_nobody_is_invited(db_path, fakes, monkeypatch, caplog):
    _google_env(monkeypatch, invites="")
    with new_client():
        pass
    assert "No invites" in caplog.text


# --- sessions are bound to the database --------------------------------------------

def test_old_cookie_is_rejected_after_db_wipe(google, db_path):
    first_uid = sign_in_as(google, "g-old", "friend@example.com", "Old Friend")
    cookie = google.cookies.get("webjam_session")
    for p in db_path.parent.glob(db_path.name + "*"):
        p.unlink()
    with new_client() as fresh:
        new_uid = sign_in_as(fresh, "g-new", "friend@example.com", "New Person")
    assert new_uid == first_uid  # same id in the fresh database...
    replay = new_client().get("/api/bootstrap", headers={"Cookie": f"webjam_session={cookie}"}).json()
    assert replay["current_user"]["id"] == 0  # ...but the old cookie no longer maps to it


# --- demo mode doesn't fail open ----------------------------------------------------

def test_demo_mode_needs_explicit_choice_on_a_real_deployment(monkeypatch):
    from app.auth import AuthConfigError, load_config_from_env

    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("WEBJAM_BASE_URL", "https://webjam.example.com")
    with pytest.raises(AuthConfigError, match="WEBJAM_AUTH_MODE"):
        load_config_from_env()
    monkeypatch.setenv("WEBJAM_AUTH_MODE", "demo")
    with pytest.raises(AuthConfigError, match="non-localhost"):
        load_config_from_env()
    monkeypatch.setenv("WEBJAM_BASE_URL", "http://localhost:8000")
    assert load_config_from_env().mode == "demo"

    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("WEBJAM_SESSION_SECRET", "k" * 40)
    with pytest.raises(AuthConfigError, match="WEBJAM_AUTH_MODE"):
        load_config_from_env()


def test_demo_mode_only_serves_loopback(demo, monkeypatch):
    from app.main import app

    lan = TestClient(app, headers=OK_HEADERS, client=("192.168.1.20", 5000))
    assert lan.post("/api/auth/login", json={"username": "alex"}).status_code == 403
    assert lan.get("/api/bootstrap").status_code == 403
    assert demo.get("/api/bootstrap", headers={"X-Forwarded-For": "203.0.113.7"}).status_code == 403
    assert demo.get("/api/bootstrap", headers={"Host": "rebind.example.com"}).status_code == 403
    assert TestClient(app, client=("127.0.0.1", 5000), base_url="http://localhost:8000").get("/api/bootstrap").status_code == 200
    monkeypatch.setenv("WEBJAM_DEMO_ALLOW_REMOTE", "1")
    assert lan.get("/api/bootstrap").status_code == 200


# --- plays --------------------------------------------------------------------------

def test_owner_plays_do_not_count_toward_playlist_plays(google):
    google_sign_in(google)
    vids = " ".join(f"v{i:010d}" for i in range(5))
    pid = google.post("/api/playlists", json={"title": "mine", "initial_urls": vids}).json()["playlist_id"]
    for i in range(5):
        res = google.post(f"/api/playlists/{pid}/play", json={"video_id": f"v{i:010d}"}).json()
        assert res["counted"] is False and res["plays"] == 0
    assert "heavy_rotation" not in [b["key"] for b in google.put(
        f"/api/playlists/{pid}", json={"title": "mine", "description": ""}).json()["new_badges"]]
    # Someone else's listen counts once for the whole playlist.
    guest = new_client()
    assert guest.post(f"/api/playlists/{pid}/play", json={"video_id": "v0000000000"}).json()["plays"] == 1
    assert guest.post(f"/api/playlists/{pid}/play", json={"video_id": "v0000000001"}).json()["plays"] == 1


# --- handles ------------------------------------------------------------------------

def test_released_handle_stays_with_its_old_owner(demo):
    old = demo.get("/api/users/1").json()["user"]["username"]
    assert demo.put("/api/me/handle", json={"user_id": 1, "handle": "brand_new"}).json()["ok"]
    taken = demo.put("/api/me/handle", json={"user_id": 2, "handle": old})
    assert taken.status_code == 409
    assert demo.put("/api/me/handle", json={"user_id": 1, "handle": old}).json()["username"] == old


def test_legacy_google_accounts_are_cleaned_up(db_path, monkeypatch):
    from app.auth import migrate_legacy_google_accounts
    from app.database import init_db

    _clear_auth_env(monkeypatch)
    init_db()
    conn = db()
    conn.execute("INSERT INTO users (username, display_name, created_at) VALUES ('jane_doe', 'Jane D', 'now')")
    uid = conn.execute("SELECT id FROM users WHERE username = 'jane_doe'").fetchone()[0]
    conn.execute(
        "INSERT INTO google_accounts (google_sub, user_id, email, access_token, refresh_token, scope, updated_at) "
        "VALUES ('g-legacy', ?, 'jane_doe@gmail.com', 'at', 'rt', 'https://www.googleapis.com/auth/youtube.readonly', 'now')",
        (uid,),
    )
    conn.commit()
    assert migrate_legacy_google_accounts(conn) == 1
    row = conn.execute("SELECT u.username, ga.access_token, ga.refresh_token FROM users u "
                       "JOIN google_accounts ga ON ga.user_id = u.id WHERE u.id = ?", (uid,)).fetchone()
    assert row["username"] == "jane_d" and row["access_token"] is None and row["refresh_token"] is None
    assert conn.execute("SELECT user_id FROM handle_history WHERE handle = 'jane_doe'").fetchone()[0] == uid
    # One-time: a handle chosen later is left alone.
    conn.execute("UPDATE users SET username = 'jane_doe' WHERE id = ?", (uid,))
    conn.commit()
    assert migrate_legacy_google_accounts(conn) == 0
    conn.close()


# --- search quota -------------------------------------------------------------------

def test_search_needs_sign_in_and_only_misses_are_charged(google, monkeypatch):
    import app.main

    calls = []

    async def fake_search(q, max_results=8):
        calls.append(q)
        return [{"video_id": "dQw4w9WgXcQ", "embeddable": True}]

    monkeypatch.setattr(app.main, "search_embeddable_youtube_songs", fake_search)
    assert google.get("/api/youtube/search?q=anon").status_code == 401
    assert calls == []

    google_sign_in(google)
    monkeypatch.setitem(app.main.RATE_LIMITS, "search", (2, 0.0001))
    monkeypatch.setattr(app.main, "cached_search_results", lambda q, max_results=8: [{"video_id": "x"}] if q == "hit" else None)
    assert [google.get("/api/youtube/search?q=hit").status_code for _ in range(5)] == [200] * 5
    assert [google.get(f"/api/youtube/search?q=miss{i}").status_code for i in range(3)] == [200, 200, 429]
    assert calls == ["miss0", "miss1"]


def test_search_daily_budget(monkeypatch, demo):
    import app.main
    import app.youtube as yt

    yt.clear_youtube_caches()
    monkeypatch.setenv("YOUTUBE_API_KEY", "k")
    monkeypatch.setenv("WEBJAM_SEARCH_DAILY_CAP", "2")
    hits = []

    async def fake_api(q, n, key, client):
        hits.append(q)
        return []

    monkeypatch.setattr(yt, "_search_via_data_api", fake_api)
    monkeypatch.setattr(app.main, "search_embeddable_youtube_songs", yt.search_embeddable_youtube_songs)
    codes = [demo.get(f"/api/youtube/search?q=q{i}").status_code for i in range(3)]
    assert codes == [200, 200, 503] and len(hits) == 2
    yt.clear_youtube_caches()


# --- outbound abuse -----------------------------------------------------------------

def test_swapping_videos_is_rate_limited(demo, monkeypatch):
    import app.main

    monkeypatch.setitem(app.main.RATE_LIMITS, "add", (2, 0.0001))
    app.main.reset_rate_limits()
    pid = demo.post("/api/playlists", json={"user_id": 1, "title": "s"}).json()["playlist_id"]
    conn = db()
    conn.execute(
        "INSERT INTO playlist_tracks (playlist_id, video_id, position, added_by_user_id, created_at) VALUES (?, '0PsOJ_fRckg', 1, 1, 'now')",
        (pid,),
    )
    tid = conn.execute("SELECT MAX(id) FROM playlist_tracks").fetchone()[0]
    conn.commit()
    conn.close()
    codes = [demo.put(f"/api/playlists/{pid}/tracks/{tid}", json={"user_id": 1, "new_video_id": f"swap{i:07d}"}).status_code
             for i in range(3)]
    assert codes == [200, 200, 429]
    # Note-only edits aren't charged.
    assert demo.put(f"/api/playlists/{pid}/tracks/{tid}", json={"user_id": 1, "track_note": "hi"}).status_code == 200


def test_clean_migration_query_is_linear():
    from app.youtube import clean_migration_query

    start = time.perf_counter()
    for _ in range(50):
        clean_migration_query("(remastered" + " " * 3000 + "x", "")
        clean_migration_query("(remastered" + " " * 288 + "x", "")
    assert time.perf_counter() - start < 0.2
    assert clean_migration_query("Song (Remastered 2011)", "Band") == "Band - Song"
    assert clean_migration_query("Artist - Song [Official Video]") == "Artist - Song"


def test_rate_limiter_memory_is_bounded():
    from app.main import TokenBucketLimiter, ip_key

    lim = TokenBucketLimiter(max_buckets=100)
    for i in range(1000):
        lim.allow(("search", f"ip:{i}"), 20, 1 / 3)
    assert len(lim._buckets) == 100
    assert ip_key("2001:db8:1:2::1") == ip_key("2001:db8:1:2:ffff::9") != ip_key("2001:db8:1:3::1")
    assert ip_key("203.0.113.9") == "203.0.113.9"


# --- input bounds -------------------------------------------------------------------

def test_out_of_range_numbers_are_400_not_500(demo):
    huge = "9" * 25
    assert demo.get(f"/api/playlists/{huge}").status_code == 400
    assert demo.get(f"/api/users/{huge}").status_code == 400
    assert demo.get(f"/api/comments?target_type=playlist&target_id=1&before_id={huge}").status_code == 400
    assert demo.get("/api/comments", params={"target_type": "playlist", "target_id": "²"}).status_code == 400
    assert demo.post("/api/comments", json={"author_user_id": 1, "target_type": "profile", "target_id": "١", "body": "x"}).status_code == 400
    assert demo.post("/api/playlists", json={"user_id": int(huge), "title": "x"}).status_code == 404
    assert demo.post("/api/playlists/1/like", json={"user_id": 999}).status_code == 404
    assert demo.post("/api/playlists/1/tracks", json={"user_id": 2, "urls_or_ids": "0PsOJ_fRckg", "source_playlist_id": 10**30}).status_code == 400
    r = demo.put("/api/playlists/1/tracks/1", json={"user_id": 2, "start_time": "9" * 23, "update_clip": True})
    assert r.status_code == 200 and r.json()["start_seconds"] is None


def test_ops_reject_bad_clip_windows(demo):
    n = [0]

    def put(start, end):
        n[0] += 1
        return demo.post("/api/ops", json={"user_id": 2, "op_id": f"clip-{n[0]}", "ops": [
            {"kind": "insert", "playlist_id": 1, "items": [
                {"video_id": "0PsOJ_fRckg", "start_seconds": start, "end_seconds": end}]}]})

    assert put(500, -5).json()["code"] == "bad_clip"
    assert put(10**30, None).status_code == 400
    assert put(500, 100).status_code == 400
    assert put(10, 100).status_code == 200


# --- concurrent adds of the same new video -------------------------------------------

def test_concurrent_adds_of_a_new_video_both_succeed(demo, monkeypatch):
    import app.main

    async def slow_metadata(video_id, custom_title=None):
        await asyncio.sleep(0.2)
        return await fake_metadata(video_id)

    monkeypatch.setattr(app.main, "fetch_youtube_metadata", slow_metadata)
    p1 = demo.post("/api/playlists", json={"user_id": 1, "title": "a"}).json()["playlist_id"]
    p2 = demo.post("/api/playlists", json={"user_id": 2, "title": "b"}).json()["playlist_id"]

    async def run():
        transport = httpx.ASGITransport(app=app.main.app)
        async with REAL_ASYNC_CLIENT(transport=transport, base_url="http://testserver", headers=OK_HEADERS) as c:
            return await asyncio.gather(
                c.post(f"/api/playlists/{p1}/tracks", json={"user_id": 1, "urls_or_ids": "NEWVIDEO123"}),
                c.post(f"/api/playlists/{p2}/tracks", json={"user_id": 2, "urls_or_ids": "NEWVIDEO123"}),
            )

    r1, r2 = asyncio.run(run())
    assert (r1.status_code, r2.status_code) == (200, 200), (r1.text, r2.text)
    firsts = [r.json()["results"][0]["is_new_discovery"] for r in (r1, r2)]
    assert sorted(firsts) == [False, True]


# --- transient YouTube failures -------------------------------------------------------

def test_transient_lookup_failures_are_503_and_reported(demo, monkeypatch):
    import app.main

    async def flaky(video_id, custom_title=None):
        if video_id == "BBBBBBBBBBB":
            return {"video_id": video_id, "status": "UNAVAILABLE", "title": "x", "embeddable": False}
        return await fake_metadata(video_id)

    monkeypatch.setattr(app.main, "fetch_youtube_metadata", flaky)
    single = demo.post("/api/playlists/2/tracks", json={"user_id": 3, "urls_or_ids": "BBBBBBBBBBB"})
    assert single.status_code == 503
    multi = demo.post("/api/playlists/2/tracks", json={"user_id": 3, "urls_or_ids": "AAAAAAAAAAA BBBBBBBBBBB zzzzzzzzzzz"}).json()
    assert {s["video_id"]: s["reason"] for s in multi["skipped"]} == {
        "BBBBBBBBBBB": "youtube_unavailable", "zzzzzzzzzzz": "not_found"}


def test_embeddability_network_error_is_unavailable_not_missing():
    from app.youtube import _fetch_embeddability

    class Boom:
        async def get(self, *a, **k):
            raise httpx.ConnectTimeout("down")

    result, ttl = asyncio.run(_fetch_embeddability("dQw4w9WgXcQ", Boom()))
    assert result["status"] == "UNAVAILABLE" and ttl is None


def test_callback_network_error_redirects_instead_of_500(google, monkeypatch):
    async def down(self, url, data=None, **kwargs):
        raise httpx.ConnectTimeout("down")

    monkeypatch.setattr(FakeGoogle, "post", down)
    cb, _ = google_sign_in(google)
    assert cb.status_code == 303 and cb.headers["location"] == "/#auth-error"

    async def no_token(self, url, data=None, **kwargs):
        return FakeResponse(200, {"scope": "openid"})

    monkeypatch.setattr(FakeGoogle, "post", no_token)
    cb, _ = google_sign_in(google)
    assert cb.status_code == 400


def test_inspect_network_error_is_502(demo, monkeypatch):
    import app.main

    async def down(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(app.main, "inspect_youtube_playlist_or_urls", down)
    assert demo.post("/api/playlists/import/inspect", json={"source_input": "PLxxxxxxxxxxxx"}).status_code == 502


# --- local files ----------------------------------------------------------------------

def test_existing_world_readable_db_is_tightened(tmp_path, monkeypatch):
    import app.database

    path = tmp_path / "old.db"
    path.touch()
    os.chmod(path, 0o644)
    monkeypatch.setattr(app.database, "DB_PATH", path)
    app.database.init_db()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    for side in (Path(f"{path}-wal"), Path(f"{path}-shm")):
        if side.exists():
            assert stat.S_IMODE(os.stat(side).st_mode) & 0o077 == 0


def test_wal_sidecars_are_gitignored():
    try:
        out = subprocess.run(
            ["git", "check-ignore", "data/webjam.db-wal", "data/webjam.db-shm", "x.sqlite3-wal"],
            cwd=ROOT, capture_output=True, text=True,
        )
    except FileNotFoundError:
        pytest.skip("git not available")
    assert out.returncode == 0 and len(out.stdout.split()) == 3


# --- frontend -------------------------------------------------------------------------

def _fn(name):
    m = re.search(r"^(?:async )?function " + re.escape(name) + r"\(.*?^\}", APP_JS, re.S | re.M)
    assert m, name
    return m.group(0)


def test_frontend_follows_the_backend_changes():
    # Phase 1a: undo goes through the server-side op log; the snapshot endpoint is gone.
    assert "/api/ops/undo" in _fn("undoLastPlaylistChange")
    assert "/snapshot" not in APP_JS and "capturePlaylistSnapshot" not in APP_JS
    assert "Array.isArray(data.detail)" in _fn("api")
    assert "res.counted" in _fn("incrementPlaylistSpin") and "res.counted" in _fn("simulateExtraSpin")
    assert "youtube_connected" in _fn("loadMyYouTubePlaylists")
    assert "has_more" in _fn("renderCommentsSection") or "hasMore" in _fn("renderCommentsSection")
    assert re.search(r"\$\{isOwner \? `<button[^`]*auditPlaylistEmbeds", APP_JS)
    for fn in ("autoMigrateAllImportTracks", "autoMigrateAuditedPlaylist"):
        assert "apiWithRetry" in _fn(fn)
    for fn in ("handleQuickDrop", "handleDropIntoPlaylist", "submitCommitPlaylistImport"):
        assert "skipped" in _fn(fn)

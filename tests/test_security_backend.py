"""Phase 0 security regressions for the backend (auth, sessions, CSRF, caps, provenance, plays)."""

import base64
import hashlib
import json
import os
import re
import stat
import urllib.parse

import pytest
from fastapi.testclient import TestClient

OK_HEADERS = {"X-Webjam": "1"}
SECRET = "x" * 40


# --- Fakes -------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code, data):
        self.status_code = status_code
        self._data = data

    def json(self):
        return self._data


class FakeGoogle:
    """Stands in for httpx.AsyncClient inside app.auth. Configure `userinfo` per test."""

    userinfo = {}
    token_posts = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, data=None, **kwargs):
        FakeGoogle.token_posts.append(dict(data or {}))
        return FakeResponse(200, {"access_token": "at", "expires_in": 3600, "scope": "openid email profile"})

    async def get(self, url, headers=None, params=None, **kwargs):
        if "userinfo" in url:
            return FakeResponse(200, dict(FakeGoogle.userinfo))
        raise AssertionError(f"unexpected outbound GET {url}")


MISSING_VIDEOS = {"zzzzzzzzzzz"}
METADATA_CALLS = []


async def fake_metadata(video_id, custom_title=None):
    METADATA_CALLS.append(video_id)
    if video_id in MISSING_VIDEOS:
        return {"video_id": video_id, "title": f"YouTube Track ({video_id})", "channel_title": "YouTube Artist",
                "thumbnail_url": "", "embeddable": False, "status": "NOT_FOUND", "reason": "gone"}
    return {"video_id": video_id, "title": f"Real Title {video_id}", "channel_title": "Real Channel",
            "thumbnail_url": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg", "embeddable": True,
            "status": "OK", "reason": None}


EMBED_CHECKS = []


async def fake_embed_check(video_id, client=None):
    EMBED_CHECKS.append(video_id)
    return {"video_id": video_id, "title": "t", "channel_title": "c", "embeddable": True, "status": "OK", "reason": None}


def _clear_auth_env(monkeypatch):
    for var in (
        "WEBJAM_AUTH_MODE", "WEBJAM_DEMO_MODE", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
        "WEBJAM_SESSION_SECRET", "WEBJAM_BASE_URL", "WEBJAM_INVITE_EMAILS",
    ):
        monkeypatch.delenv(var, raising=False)


def _google_env(monkeypatch, invites="friend@example.com"):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("WEBJAM_AUTH_MODE", "google")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("WEBJAM_SESSION_SECRET", SECRET)
    monkeypatch.setenv("WEBJAM_BASE_URL", "https://testserver")
    monkeypatch.setenv("WEBJAM_INVITE_EMAILS", invites)


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    import app.database

    path = tmp_path / "data" / "sec.db"
    monkeypatch.setattr(app.database, "DB_PATH", path)
    return path


@pytest.fixture
def fakes(monkeypatch):
    import app.auth
    import app.main

    METADATA_CALLS.clear()
    EMBED_CHECKS.clear()
    FakeGoogle.token_posts = []
    FakeGoogle.userinfo = {"sub": "g-1", "email": "Friend@Example.com", "email_verified": True, "name": "Party Person"}
    monkeypatch.setattr(app.main, "fetch_youtube_metadata", fake_metadata)
    monkeypatch.setattr(app.main, "check_video_embeddability", fake_embed_check)
    monkeypatch.setattr(app.auth.httpx, "AsyncClient", FakeGoogle)


@pytest.fixture
def demo(db_path, fakes, monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("WEBJAM_AUTH_MODE", "demo")
    from app.main import app

    with TestClient(app, headers=OK_HEADERS) as c:
        yield c


@pytest.fixture
def google(db_path, fakes, monkeypatch):
    _google_env(monkeypatch)
    from app.main import app

    with TestClient(app, base_url="https://testserver", headers=OK_HEADERS) as c:
        yield c


def db():
    from app.database import get_connection

    return get_connection()


def google_sign_in(client, follow=False):
    login = client.get("/auth/google/login", follow_redirects=False)
    params = urllib.parse.parse_qs(urllib.parse.urlparse(login.headers["location"]).query)
    return client.get(
        "/auth/google/callback",
        params={"code": "c", "state": params["state"][0]},
        follow_redirects=follow,
    ), params


# =============================================================================
# PASS 1 — auth & session
# =============================================================================

# --- #2 explicit auth mode, fail closed --------------------------------------

def test_auth_mode_defaults_to_demo_only_without_google_vars(monkeypatch):
    from app.auth import load_config_from_env

    _clear_auth_env(monkeypatch)
    assert load_config_from_env().mode == "demo"

    # One Google var present and no explicit mode: google (and therefore fail closed), never demo.
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    with pytest.raises(RuntimeError, match="requires"):
        load_config_from_env()


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"GOOGLE_CLIENT_SECRET": ""}, "GOOGLE_CLIENT_SECRET"),
        ({"WEBJAM_SESSION_SECRET": ""}, "WEBJAM_SESSION_SECRET"),
        ({"WEBJAM_SESSION_SECRET": "short"}, "at least 32"),
        ({"WEBJAM_BASE_URL": ""}, "WEBJAM_BASE_URL"),
        ({"WEBJAM_BASE_URL": "http://webjam.example.com"}, "https"),
        ({"WEBJAM_AUTH_MODE": "maybe"}, "must be"),
    ],
)
def test_google_mode_refuses_incomplete_config(monkeypatch, overrides, message):
    from app.auth import AuthConfigError, load_config_from_env

    _google_env(monkeypatch)
    for k, v in overrides.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(AuthConfigError, match=message):
        load_config_from_env()


def test_legacy_demo_flag_is_refused(monkeypatch):
    from app.auth import AuthConfigError, load_config_from_env

    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("WEBJAM_DEMO_MODE", "1")
    with pytest.raises(AuthConfigError, match="WEBJAM_AUTH_MODE"):
        load_config_from_env()


def test_google_config_forces_secure_cookie(monkeypatch):
    from app.auth import load_config_from_env

    _google_env(monkeypatch)
    monkeypatch.setenv("WEBJAM_BASE_URL", "http://localhost:8080")  # dev is allowed...
    assert load_config_from_env().https_only is True  # ...but the cookie is still Secure


def test_app_refuses_to_start_with_bad_google_config(db_path, fakes, monkeypatch):
    from app.auth import AuthConfigError
    from app.main import app

    _google_env(monkeypatch)
    monkeypatch.delenv("WEBJAM_SESSION_SECRET")
    with pytest.raises(AuthConfigError):
        with TestClient(app):
            pass


def test_demo_mode_refuses_db_with_google_accounts(db_path, fakes, monkeypatch):
    from app.auth import AuthConfigError
    from app.database import init_db
    from app.main import app

    init_db()
    conn = db()
    conn.execute("INSERT INTO google_accounts (google_sub, user_id, email, updated_at) VALUES ('s', 1, 'a@b.c', 'now')")
    conn.commit()
    conn.close()

    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("WEBJAM_AUTH_MODE", "demo")
    with pytest.raises(AuthConfigError, match="demo mode"):
        with TestClient(app):
            pass


def test_mode_is_fixed_at_startup_not_per_request(google, monkeypatch):
    # Losing an env var while running must not flip the app into demo mode.
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET")
    monkeypatch.setenv("WEBJAM_AUTH_MODE", "demo")
    boot = google.get("/api/bootstrap?user_id=1").json()
    assert boot["auth"]["mode"] == "google"
    assert boot["current_user"]["id"] == 0
    assert google.post("/api/auth/login", json={"username": "dan"}).status_code == 404


# --- invite-only sign-in ------------------------------------------------------

def _user_count():
    conn = db()
    try:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        conn.close()


def test_uninvited_google_account_is_rejected_without_creating_a_user(google):
    before = _user_count()
    FakeGoogle.userinfo = {"sub": "g-x", "email": "stranger@example.com", "email_verified": True, "name": "S"}
    cb, _ = google_sign_in(google)
    assert cb.status_code == 303 and cb.headers["location"] == "/#not-invited"
    assert _user_count() == before
    assert google.get("/api/bootstrap").json()["auth"]["signed_in"] is False


def test_unverified_email_is_rejected_even_if_invited(google):
    before = _user_count()
    FakeGoogle.userinfo = {"sub": "g-y", "email": "friend@example.com", "email_verified": False, "name": "F"}
    cb, _ = google_sign_in(google)
    assert cb.headers["location"] == "/#not-invited"
    assert _user_count() == before


def test_invited_email_matches_case_insensitively(google):
    cb, _ = google_sign_in(google)  # userinfo email is Friend@Example.com; invite is lowercase
    assert cb.headers["location"] == "/"
    assert google.get("/api/bootstrap").json()["auth"]["signed_in"] is True


def test_invite_cli_add_list_remove(db_path, capsys):
    from app.invite import main

    assert main(["add", "Someone@Example.COM"]) == 0
    assert main(["list"]) == 0
    assert "someone@example.com" in capsys.readouterr().out
    assert main(["add", "not-an-email"]) == 1
    assert main(["remove", "someone@example.com"]) == 0
    capsys.readouterr()
    main(["list"])
    assert "someone@example.com" not in capsys.readouterr().out


# --- #3 handles never derived from email -------------------------------------

def test_handle_comes_from_display_name_not_email(google):
    FakeGoogle.userinfo = {"sub": "g-2", "email": "dan.secret.address@example.com", "email_verified": True,
                           "name": "Party Person"}
    google.get("/auth/google/login")  # noop; keeps cookie jar warm
    conn = db()
    conn.execute("INSERT INTO invites (email, created_at) VALUES ('dan.secret.address@example.com', 'now')")
    conn.commit()
    conn.close()
    google_sign_in(google)
    me = google.get("/api/bootstrap").json()["current_user"]
    assert me["username"] == "party_person"
    users = google.get("/api/bootstrap").json()["users"]
    assert not any("secret" in u["username"] for u in users)


def test_handle_collision_gets_random_suffix(db_path):
    from app.auth import unique_handle
    from app.database import init_db

    init_db()
    conn = db()
    cur = conn.cursor()
    assert unique_handle(cur, "Lucas") != "lucas"  # seeded user 'lucas' exists
    assert re.fullmatch(r"lucas_[a-z0-9]{4}", unique_handle(cur, "Lucas"))
    assert unique_handle(cur, "日本語") == "curator"  # nothing usable -> generic base
    assert re.fullmatch(r"[a-z0-9_]{3,20}", unique_handle(cur, "Émilie   O'Brien-Smith the Third of Many"))
    conn.close()


def test_change_handle_endpoint(google):
    google_sign_in(google)
    assert google.put("/api/me/handle", json={"handle": "No Spaces!"}).status_code == 400
    assert google.put("/api/me/handle", json={"handle": "ab"}).status_code == 400
    assert google.put("/api/me/handle", json={"handle": "guest"}).status_code == 400
    assert google.put("/api/me/handle", json={"handle": "lucas"}).status_code == 409  # seeded
    res = google.put("/api/me/handle", json={"handle": "@DJ_Friend"})
    assert res.status_code == 200 and res.json()["username"] == "dj_friend"
    assert google.get("/api/bootstrap").json()["current_user"]["username"] == "dj_friend"


def test_change_handle_requires_sign_in(google):
    assert google.put("/api/me/handle", json={"handle": "whoever", "user_id": 1}).status_code == 401


# --- #11 no offline access / YouTube scope at login, #18 PKCE ----------------

def test_login_requests_identity_scopes_only_with_pkce(google):
    cb, params = google_sign_in(google)
    assert params["scope"] == ["openid email profile"]
    assert "access_type" not in params
    assert "consent" not in params.get("prompt", [""])[0]
    assert params["code_challenge_method"] == ["S256"]
    verifier = FakeGoogle.token_posts[-1]["code_verifier"]
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert params["code_challenge"] == [expected]


def test_no_google_tokens_are_stored(google):
    google_sign_in(google)
    conn = db()
    row = conn.execute("SELECT access_token, refresh_token FROM google_accounts").fetchone()
    conn.close()
    assert row["access_token"] is None and row["refresh_token"] is None


def test_state_is_single_use(google, monkeypatch):
    """The callback consumes its state even when sign-in then fails, so the same
    browser cannot retry it. (Replaying a cookie captured *before* the callback is
    not prevented by this: sessions are client-side; Google's code is single-use.)"""
    login = google.get("/auth/google/login", follow_redirects=False)
    state = urllib.parse.parse_qs(urllib.parse.urlparse(login.headers["location"]).query)["state"][0]

    async def failing_post(self, url, data=None, **kwargs):
        return FakeResponse(400, {"error": "invalid_grant"})

    monkeypatch.setattr(FakeGoogle, "post", failing_post)
    first = google.get("/auth/google/callback", params={"code": "c", "state": state})
    assert first.status_code == 400 and "exchanging the code" in first.json()["detail"]
    retry = google.get("/auth/google/callback", params={"code": "c", "state": state})
    assert retry.status_code == 400 and "Invalid or expired" in retry.json()["detail"]


# --- #9 session epoch / revocation, #10 Secure cookie -------------------------

def test_session_cookie_is_secure_with_14_day_max_age(google):
    cb, _ = google_sign_in(google)
    cookie = cb.headers["set-cookie"].lower()
    assert "secure" in cookie
    assert "max-age=1209600" in cookie
    assert "samesite=lax" in cookie and "httponly" in cookie


def test_logout_revokes_copied_cookies(google):
    google_sign_in(google)
    stolen = google.cookies.get("webjam_session")
    assert google.get("/api/bootstrap").json()["auth"]["signed_in"] is True
    assert google.post("/auth/logout").status_code == 200

    google.cookies.clear()
    replay = google.get("/api/bootstrap", headers={"Cookie": f"webjam_session={stolen}"}).json()
    assert replay["auth"]["signed_in"] is False
    assert replay["current_user"]["id"] == 0


def test_session_for_deleted_user_is_ignored(google):
    google_sign_in(google)
    uid = google.get("/api/bootstrap").json()["current_user"]["id"]
    conn = db()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DELETE FROM users WHERE id = ?", (uid,))
    conn.commit()
    conn.close()
    assert google.get("/api/bootstrap").json()["current_user"]["id"] == 0
    assert google.post("/api/playlists", json={"title": "x"}).status_code == 401


# --- #13 CSRF -----------------------------------------------------------------

def test_csrf_requires_header_on_unsafe_api_requests(demo):
    raw = TestClient(demo.app)  # no default X-Webjam header
    with raw:
        assert raw.post("/api/playlists", json={"user_id": 1, "title": "x"}).status_code == 403
        assert raw.put("/api/users/1", json={"display_name": "x", "bio": ""}).status_code == 403
        assert raw.delete("/api/playlists/1/tracks/1?user_id=2").status_code == 403
        assert raw.post("/auth/logout").status_code == 403
        assert raw.get("/api/bootstrap").status_code == 200  # safe methods are fine


def test_csrf_checks_origin_and_fetch_site(google):
    assert google.post("/auth/logout", headers={"Origin": "https://evil.example"}).status_code == 403
    assert google.post("/auth/logout", headers={"Origin": "null"}).status_code == 403
    assert google.post("/auth/logout", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert google.post("/auth/logout", headers={"Origin": "https://testserver"}).status_code == 200


def test_csrf_demo_mode_compares_origin_with_host(demo):
    assert demo.post("/auth/logout", headers={"Origin": "http://evil.example"}).status_code == 403
    assert demo.post("/auth/logout", headers={"Origin": "http://testserver"}).status_code == 200


# --- #12 security headers -----------------------------------------------------

@pytest.mark.parametrize("path", ["/", "/api/bootstrap", "/static/js/main.js"])
def test_security_headers(google, path):
    res = google.get(path)
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert res.headers["x-frame-options"] == "DENY"
    from app.main import CONTENT_SECURITY_POLICY

    assert res.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert "camera=()" in res.headers["permissions-policy"]
    assert "autoplay" not in res.headers["permissions-policy"]  # the YouTube iframe needs it
    assert res.headers["strict-transport-security"].startswith("max-age=")


def test_security_headers_on_rejections(demo):
    res = TestClient(demo.app).post("/api/playlists", json={})
    assert res.status_code == 403 and res.headers["x-frame-options"] == "DENY"


# --- #20 demo mode ------------------------------------------------------------

def test_demo_usernames_normalised_and_password_ignored(demo):
    res = demo.post("/api/auth/login", json={"username": "@Bad'User <x>", "password": "anything"}).json()
    assert res["created"] is True and res["username"] == "baduser_x"
    again = demo.post("/api/auth/login", json={"username": "baduser_x", "password": "different"}).json()
    assert again == {"user_id": res["user_id"], "created": False}
    assert demo.post("/api/auth/login", json={"username": "''"}).status_code == 400
    conn = db()
    stored = conn.execute("SELECT password_hash FROM users WHERE id = ?", (res["user_id"],)).fetchone()[0]
    conn.close()
    assert stored == ""
    assert demo.get("/api/bootstrap").json()["auth"]["mode"] == "demo"


# =============================================================================
# PASS 2 — endpoints & data integrity
# =============================================================================

def _track_rows(playlist_id):
    conn = db()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM playlist_tracks WHERE playlist_id = ? ORDER BY position", (playlist_id,)
        ).fetchall()]
    finally:
        conn.close()


def _new_playlist(client, user_id=1, title="Mine"):
    return client.post("/api/playlists", json={"user_id": user_id, "title": title}).json()["playlist_id"]


# --- #4 provenance ------------------------------------------------------------

def test_forged_source_user_is_not_stored(demo):
    pid = _new_playlist(demo, user_id=4)
    # Claim the track came from @maya (3); 0PsOJ_fRckg was first found by lucas (2).
    demo.post(f"/api/playlists/{pid}/tracks", json={"user_id": 4, "urls_or_ids": "0PsOJ_fRckg", "source_user_id": 3})
    row = _track_rows(pid)[-1]
    assert row["source_user_id"] is None and row["source_playlist_id"] is None


def test_source_owner_comes_from_the_source_playlist(demo):
    pid = _new_playlist(demo, user_id=4)
    # Playlist 1 belongs to lucas (2) and contains 0PsOJ_fRckg; the claimed source user is ignored.
    res = demo.post(
        f"/api/playlists/{pid}/tracks",
        json={"user_id": 4, "urls_or_ids": "0PsOJ_fRckg", "source_playlist_id": 1, "source_user_id": 3},
    )
    assert res.status_code == 200
    row = _track_rows(pid)[-1]
    assert (row["source_playlist_id"], row["source_user_id"]) == (1, 2)


def test_source_playlist_must_contain_the_video(demo):
    pid = _new_playlist(demo, user_id=4)
    conn = db()
    grabs_before = conn.execute("SELECT COUNT(*) FROM activity_log WHERE action_type = 'grabbed_track'").fetchone()[0]
    conn.close()
    res = demo.post(
        f"/api/playlists/{pid}/tracks",
        json={"user_id": 4, "urls_or_ids": "S8fFo4REHOE", "source_playlist_id": 1, "source_user_id": 2},
    )  # S8fFo4REHOE is not in playlist 1: the track is added, but nobody is credited
    assert res.status_code == 200
    row = _track_rows(pid)[-1]
    assert row["source_playlist_id"] is None and row["source_user_id"] is None
    conn = db()
    assert conn.execute("SELECT COUNT(*) FROM activity_log WHERE action_type = 'grabbed_track'").fetchone()[0] == grabs_before
    conn.close()


def test_first_finder_credit_is_verified(demo):
    pid = _new_playlist(demo, user_id=4)
    # From a profile's "discovered" list: no source playlist, source = the song's real first finder.
    demo.post(f"/api/playlists/{pid}/tracks", json={"user_id": 4, "urls_or_ids": "0PsOJ_fRckg", "source_user_id": 2})
    row = _track_rows(pid)[-1]
    assert (row["source_playlist_id"], row["source_user_id"]) == (1, 2)


# --- #4 the snapshot stopgap is gone: /api/ops never accepts client provenance --------

def _ops(client, user_id, op_id, ops):
    return client.post("/api/ops", json={"user_id": user_id, "op_id": op_id, "ops": ops})


def test_ops_reject_client_attribution(demo):
    for i, forged in enumerate(({"added_by_user_id": 3}, {"source_user_id": 3}, {"source_playlist_id": 2},
                                {"source_track_id": 4}, {"created_at": "1999-01-01 00:00:00"},
                                {"migrated_from_video_id": "Z5NoQg8LdDk"})):
        res = _ops(demo, 1, f"forge-{i}", [{"kind": "insert", "playlist_id": 4,
                                            "items": [{"video_id": "TNsSBhl_2LI", **forged}]}])
        assert res.status_code == 422, forged
    assert demo.put("/api/playlists/4/snapshot", json={"user_id": 1, "title": "T", "tracks": []}).status_code in (404, 405)
    # The legal way to credit someone is a ref; the server derives the owner.
    res = _ops(demo, 1, "credit-ok", [{"kind": "insert", "playlist_id": 4,
                                       "items": [{"video_id": "TNsSBhl_2LI", "ref": {"playlist_id": 3, "user_id": 2}}]}])
    assert res.status_code == 200
    row = _track_rows(4)[-1]
    assert (row["added_by_user_id"], row["source_user_id"], row["source_playlist_id"]) == (1, 4, 3)
    assert row["created_at"] != "1999-01-01 00:00:00"


def test_ops_validate_rows(demo):
    assert _ops(demo, 1, "v1", [{"kind": "insert", "playlist_id": 4, "items": [{"video_id": "bad id"}]}]).status_code == 422
    missing = _ops(demo, 1, "v2", [{"kind": "insert", "playlist_id": 4, "items": [{"video_id": "zzzzzzzzzzz"}]}])
    assert missing.status_code == 400 and missing.json()["code"] == "video_not_found"
    too_many = _ops(demo, 1, "v3", [{"kind": "insert", "playlist_id": 4, "items": [{"video_id": "0PsOJ_fRckg"}] * 51}])
    assert too_many.status_code == 400 and too_many.json()["code"] == "batch_too_large"
    assert len(_track_rows(4)) == 4  # nothing changed


# --- #7 caps --------------------------------------------------------------------

def test_field_length_caps(demo):
    assert demo.post("/api/playlists", json={"user_id": 1, "title": "t" * 201}).status_code == 422
    assert demo.post("/api/playlists", json={"user_id": 1, "title": "t", "description": "d" * 5001}).status_code == 422
    assert demo.post("/api/comments", json={"author_user_id": 1, "target_type": "playlist", "target_id": "1",
                                            "body": "b" * 2001}).status_code == 422
    assert demo.post("/api/playlists/1/tracks", json={"user_id": 2, "urls_or_ids": "x" * 100_001}).status_code == 422
    assert demo.post("/api/playlists/import/inspect", json={"source_input": "x" * 100_001}).status_code == 422


def test_body_size_limit(demo):
    big = json.dumps({"user_id": 1, "title": "x", "description": "d" * 300_000})
    res = demo.post("/api/playlists", content=big, headers={"Content-Type": "application/json"})
    assert res.status_code == 413

    def chunks():  # no Content-Length: streamed body is counted too
        for _ in range(300):
            yield b"x" * 1024

    res = demo.post("/api/playlists", content=chunks(), headers={"Content-Type": "application/json"})
    assert res.status_code == 413


def test_max_entries_per_request(demo):
    ids = " ".join(f"https://youtu.be/{i:011d}" for i in range(51))
    res = demo.post("/api/playlists/1/tracks", json={"user_id": 2, "urls_or_ids": ids})
    assert res.status_code == 400 and "50" in res.json()["detail"]
    res = demo.post("/api/playlists", json={"user_id": 2, "title": "x", "initial_urls": ids})
    assert res.status_code == 400
    assert METADATA_CALLS == []


def test_max_tracks_per_playlist(demo):
    conn = db()
    conn.executemany(
        "INSERT INTO playlist_tracks (playlist_id, video_id, position, added_by_user_id, created_at) VALUES (1, '0PsOJ_fRckg', ?, 2, 'now')",
        [(100 + i,) for i in range(997)],
    )
    conn.commit()
    conn.close()
    res = demo.post("/api/playlists/1/tracks", json={"user_id": 2, "urls_or_ids": "TNsSBhl_2LI"})
    assert res.status_code == 400 and "1000" in res.json()["detail"]


def test_failed_metadata_lookup_creates_nothing(demo):
    conn = db()
    activity_before = conn.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0]
    conn.close()
    res = demo.post("/api/playlists/1/tracks", json={"user_id": 2, "urls_or_ids": "zzzzzzzzzzz"})
    assert res.status_code == 400
    conn = db()
    assert conn.execute("SELECT COUNT(*) FROM songs WHERE video_id = 'zzzzzzzzzzz'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0] == activity_before
    conn.close()

    # Mixed batch: the good id is added, the missing one is reported as skipped.
    res = demo.post("/api/playlists/1/tracks", json={"user_id": 2, "urls_or_ids": "zzzzzzzzzzz, AAAAAAAAAAA"}).json()
    assert [r["video_id"] for r in res["results"]] == ["AAAAAAAAAAA"]
    assert res["skipped"] == [{"video_id": "zzzzzzzzzzz", "reason": "not_found"}]


# --- #16 per-row display titles -----------------------------------------------

def test_custom_title_is_per_row_only(demo):
    res = demo.post(
        "/api/playlists/1/tracks",
        json={"user_id": 2, "urls_or_ids": "BBBBBBBBBBB", "custom_title": "<img src=x onerror=alert(1)>"},
    ).json()
    assert res["results"][0]["title"] == "<img src=x onerror=alert(1)>"
    conn = db()
    assert conn.execute("SELECT title FROM songs WHERE video_id = 'BBBBBBBBBBB'").fetchone()[0] == "Real Title BBBBBBBBBBB"
    conn.close()

    pl = demo.get("/api/playlists/1?user_id=2").json()
    row = next(t for t in pl["tracks"] if t["video_id"] == "BBBBBBBBBBB")
    assert row["title"] == "<img src=x onerror=alert(1)>" and row["song_title"] == "Real Title BBBBBBBBBBB"

    other = _new_playlist(demo, user_id=3)
    demo.post(f"/api/playlists/{other}/tracks", json={"user_id": 3, "urls_or_ids": "BBBBBBBBBBB"})
    assert demo.get(f"/api/playlists/{other}?user_id=3").json()["tracks"][0]["title"] == "Real Title BBBBBBBBBBB"
    assert demo.get("/api/songs/BBBBBBBBBBB").json()["title"] == "Real Title BBBBBBBBBBB"

    # Renaming a row (custom_title on update) stays per-row too; "" restores the real title.
    tid = row["id"]
    assert demo.put(f"/api/playlists/1/tracks/{tid}", json={"user_id": 2, "custom_title": "Mine"}).json()["title"] == "Mine"
    assert demo.put(f"/api/playlists/1/tracks/{tid}", json={"user_id": 2, "custom_title": ""}).json()["title"] == "Real Title BBBBBBBBBBB"


def test_swap_custom_title_does_not_rename_song(demo):
    tid = _track_rows(1)[0]["id"]
    res = demo.put(
        f"/api/playlists/1/tracks/{tid}",
        json={"user_id": 2, "new_video_id": "CCCCCCCCCCC", "custom_title": "Forged Global Title"},
    )
    assert res.status_code == 200 and res.json()["title"] == "Forged Global Title"
    conn = db()
    assert conn.execute("SELECT title FROM songs WHERE video_id = 'CCCCCCCCCCC'").fetchone()[0] == "Real Title CCCCCCCCCCC"
    conn.close()
    bad = demo.put(f"/api/playlists/1/tracks/{tid}", json={"user_id": 2, "new_video_id": "zzzzzzzzzzz"})
    assert bad.status_code == 400


# --- #6 rate limits, audit-embeds, /check removal -----------------------------

def test_search_is_rate_limited_and_capped(demo, monkeypatch):
    import app.main

    async def fake_search(q, max_results=8):
        return []

    monkeypatch.setattr(app.main, "search_embeddable_youtube_songs", fake_search)
    monkeypatch.setitem(app.main.RATE_LIMITS, "search", (20, 0.0001))
    assert demo.get("/api/youtube/search", params={"q": "q" * 201}).status_code == 422
    codes = [demo.get("/api/youtube/search", params={"q": "x"}).status_code for _ in range(25)]
    assert codes[0] == 200 and codes[-1] == 429
    limited = demo.get("/api/youtube/search", params={"q": "x"})
    assert int(limited.headers["retry-after"]) >= 1


def test_comments_are_rate_limited(demo, monkeypatch):
    import app.main

    monkeypatch.setitem(app.main.RATE_LIMITS, "comment", (10, 0.0001))
    codes = [
        demo.post("/api/comments", json={"author_user_id": 1, "target_type": "playlist", "target_id": "1",
                                         "body": f"c{i}"}).status_code
        for i in range(12)
    ]
    assert codes[:10] == [200] * 10 and codes[-1] == 429


def test_rate_limit_keys_on_signed_in_user(google, monkeypatch):
    import app.main

    # Tiny bucket with ~no refill so the test doesn't depend on how fast requests run.
    monkeypatch.setitem(app.main.RATE_LIMITS, "like", (3, 0.0001))
    google_sign_in(google)
    codes = [google.post("/api/songs/0PsOJ_fRckg/like", json={}).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    uid = google.get("/api/bootstrap").json()["current_user"]["id"]
    assert ("like", f"u:{uid}") in app.main.rate_limiter._buckets


def test_forwarded_for_only_trusted_from_localhost():
    from starlette.requests import Request

    from app.main import client_ip

    def req(peer, xff):
        return Request({"type": "http", "client": (peer, 1), "headers": [(b"x-forwarded-for", xff.encode())]})

    assert client_ip(req("203.0.113.9", "1.2.3.4")) == "203.0.113.9"
    assert client_ip(req("127.0.0.1", "6.6.6.6, 1.2.3.4")) == "1.2.3.4"


def test_audit_embeds_is_owner_only_and_deduped(demo):
    assert demo.get("/api/playlists/1/audit-embeds").status_code == 400  # demo: no user named
    assert demo.get("/api/playlists/1/audit-embeds?user_id=3").status_code == 403
    demo.post("/api/playlists/1/tracks", json={"user_id": 2, "urls_or_ids": "0PsOJ_fRckg"})  # duplicate video
    res = demo.get("/api/playlists/1/audit-embeds?user_id=2").json()
    assert res["total_tracks"] == 4
    assert sorted(EMBED_CHECKS) == sorted({t["video_id"] for t in res["tracks"]})


def test_audit_embeds_requires_sign_in_in_google_mode(google):
    assert google.get("/api/playlists/1/audit-embeds").status_code == 401


def test_youtube_check_endpoint_removed(demo):
    assert demo.get("/api/youtube/check/dQw4w9WgXcQ").status_code == 404


# --- #14 plays ----------------------------------------------------------------

def _plays(pid):
    conn = db()
    try:
        return conn.execute("SELECT plays FROM playlists WHERE id = ?", (pid,)).fetchone()[0]
    finally:
        conn.close()


def test_play_counts_once_and_dedupes(demo):
    start = _plays(1)
    first = demo.post("/api/playlists/1/play", json={"video_id": "0PsOJ_fRckg", "count": 20}).json()
    assert first["plays"] == start + 1 and first["counted"] is True
    again = demo.post("/api/playlists/1/play", json={"video_id": "0PsOJ_fRckg"}).json()
    assert again["plays"] == start + 1 and again["counted"] is False
    # Another track in the same playlist: the song's plays count, the playlist's don't again.
    conn = db()
    song_before = conn.execute("SELECT total_plays FROM songs WHERE video_id = 'TNsSBhl_2LI'").fetchone()[0]
    other = demo.post("/api/playlists/1/play", json={"video_id": "TNsSBhl_2LI"}).json()
    assert other["plays"] == start + 1 and other["counted"] is False
    assert conn.execute("SELECT total_plays FROM songs WHERE video_id = 'TNsSBhl_2LI'").fetchone()[0] == song_before + 1
    conn.close()


def test_play_requires_video_in_playlist(demo):
    start = _plays(1)
    assert demo.post("/api/playlists/1/play", json={"video_id": "S8fFo4REHOE"}).status_code == 400
    assert _plays(1) == start


def test_play_badge_evaluation_is_throttled(demo, monkeypatch):
    import app.main

    calls = []
    real = app.main.evaluate_and_award_badges
    monkeypatch.setattr(app.main, "evaluate_and_award_badges", lambda conn, uid, **kw: calls.append(uid) or real(conn, uid, **kw))
    for vid in ("0PsOJ_fRckg", "TNsSBhl_2LI", "c_56qQqxvm4"):
        demo.post("/api/playlists/1/play", json={"video_id": vid})
    assert calls == [2]


# --- #17 comments ---------------------------------------------------------------

def test_comment_targets_are_validated(demo):
    def post(tt, tid):
        return demo.post("/api/comments", json={"author_user_id": 1, "target_type": tt, "target_id": tid, "body": "hi"})

    assert post("playlist", "abc").status_code == 400
    assert post("profile", "1; drop").status_code == 400
    assert post("playlist", "9999").status_code == 404
    assert post("profile", "9999").status_code == 404
    assert post("song", "nope").status_code == 400
    assert post("song", "AAAAAAAAAAA").status_code == 404
    assert post("profile", "2").status_code == 200
    conn = db()
    assert conn.execute("SELECT COUNT(*) FROM comments WHERE target_id IN ('abc', '9999', 'AAAAAAAAAAA')").fetchone()[0] == 0
    conn.close()


def test_comments_are_paginated(demo):
    conn = db()
    conn.executemany(
        "INSERT INTO comments (author_user_id, target_type, target_id, body, created_at) VALUES (1, 'playlist', '3', ?, 'now')",
        [(f"bulk {i}",) for i in range(60)],
    )
    conn.commit()
    conn.close()
    pl = demo.get("/api/playlists/3").json()
    assert len(pl["comments"]) == 50 and pl["comments_has_more"] is True
    assert pl["comments"][-1]["body"] == "bulk 59"  # newest last, as before
    older = demo.get("/api/comments", params={"target_type": "playlist", "target_id": "3",
                                              "before_id": pl["comments"][0]["id"]}).json()
    assert len(older["comments"]) == 10 and older["has_more"] is False


# --- #21 database file ------------------------------------------------------------

def test_db_file_is_private_and_uses_wal(demo, db_path):
    assert stat.S_IMODE(os.stat(db_path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(db_path.parent).st_mode) == 0o700
    conn = db()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    conn.close()

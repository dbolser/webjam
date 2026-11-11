"""Google sign-in mode: session-based identity, client-supplied user ids ignored."""

import urllib.parse

import pytest
from fastapi.testclient import TestClient


class FakeResponse:
    def __init__(self, status_code, data):
        self.status_code = status_code
        self._data = data

    def json(self):
        return self._data


class FakeAsyncClient:
    """Stands in for httpx.AsyncClient inside app.auth."""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, data=None, **kwargs):
        assert data["code"] == "good-code"
        return FakeResponse(200, {"access_token": "at", "refresh_token": "rt", "expires_in": 3600, "scope": "x"})

    async def get(self, url, headers=None, params=None, **kwargs):
        if "userinfo" in url:
            return FakeResponse(200, {"sub": "g-123", "email": "Test.User@example.com", "name": "Test User"})
        if url.endswith("/playlists"):
            return FakeResponse(200, {"items": [{"id": "PLx", "snippet": {"title": "Mine"}, "contentDetails": {"itemCount": 2}, "status": {"privacyStatus": "private"}}]})
        if url.endswith("/playlistItems"):
            return FakeResponse(200, {"items": [{"contentDetails": {"videoId": "M7lc1UVf-VE"}}]})
        raise AssertionError(url)


@pytest.fixture
def client(tmp_path, monkeypatch):
    import app.auth
    import app.database

    monkeypatch.setattr(app.database, "DB_PATH", tmp_path / "auth.db")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.delenv("WEBJAM_DEMO_MODE", raising=False)
    monkeypatch.setattr(app.auth.httpx, "AsyncClient", FakeAsyncClient)
    from app.main import app as fastapi_app

    with TestClient(fastapi_app) as c:
        yield c


def sign_in(client):
    login = client.get("/auth/google/login", follow_redirects=False)
    assert login.status_code == 307
    params = urllib.parse.parse_qs(urllib.parse.urlparse(login.headers["location"]).query)
    assert "youtube.readonly" in params["scope"][0]
    assert params["redirect_uri"][0] == "http://testserver/auth/google/callback"
    cb = client.get(
        "/auth/google/callback",
        params={"code": "good-code", "state": params["state"][0]},
        follow_redirects=False,
    )
    assert cb.status_code == 307


def test_anonymous_is_guest_and_cannot_write(client):
    boot = client.get("/api/bootstrap?user_id=1").json()
    assert boot["auth"] == {"mode": "google", "signed_in": False}
    assert boot["current_user"]["id"] == 0  # ?user_id=1 is ignored

    assert client.post("/api/playlists", json={"user_id": 1, "title": "x"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "dan"}).status_code == 404
    assert client.delete("/api/playlists/1/tracks/1?user_id=1").status_code == 401


def test_callback_rejects_bad_state(client):
    client.get("/auth/google/login", follow_redirects=False)
    bad = client.get("/auth/google/callback", params={"code": "good-code", "state": "forged"})
    assert bad.status_code == 400


def test_google_user_acts_as_self_only(client):
    sign_in(client)
    boot = client.get("/api/bootstrap?user_id=1").json()
    me = boot["current_user"]
    assert boot["auth"]["signed_in"] is True
    assert me["username"] == "testuser" and me["display_name"] == "Test User"

    # Claimed user_id=1 is overridden by the session user
    created = client.post("/api/playlists", json={"user_id": 1, "title": "My Mix"}).json()
    pl = client.get(f"/api/playlists/{created['playlist_id']}").json()
    assert pl["user_id"] == me["id"]

    # Can't touch someone else's playlist (seeded playlist 1 belongs to another curator)
    assert client.put("/api/playlists/1/reorder", json={"track_ids": []}).status_code == 403
    assert client.delete("/api/playlists/1/tracks/1").status_code == 403
    assert client.put("/api/users/1", json={"display_name": "x", "bio": "y"}).status_code == 403

    assert client.get("/api/youtube/my-playlists").json()["playlists"][0]["title"] == "Mine"
    assert client.get("/api/youtube/my-playlists/PLx/videos").json()["video_ids"] == ["M7lc1UVf-VE"]

    # Signing in again maps to the same Webjam user
    client.post("/auth/logout")
    assert client.get("/api/bootstrap").json()["current_user"]["id"] == 0
    sign_in(client)
    assert client.get("/api/bootstrap").json()["current_user"]["id"] == me["id"]

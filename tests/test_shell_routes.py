"""Phase 1b shell plumbing (docs/phase1b-shell.md §3.8-3.10): page routes, headers, the
sign-in return path and the additive handle_chosen_at migration."""

import urllib.parse

import pytest

from test_security_backend import db, db_path, demo, fakes, google, google_sign_in  # noqa: F401

STRICT_CSP = (
    "default-src 'self'; script-src 'self' https://www.youtube.com https://s.ytimg.com; "
    "frame-src https://www.youtube.com https://www.youtube-nocookie.com; "
    "img-src 'self' data: https://i.ytimg.com https://*.ytimg.com; style-src 'self'; font-src 'self'; "
    "connect-src 'self'; media-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)


# --- page routes ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/", "/discover", "/search?q=tycho", "/crate", "/u/maya", "/u/Maya",
                                  "/u/old_handle_ok", "/p/2", "/p/2-late-shift", "/t/c_56qQqxvm4"])
def test_page_routes_serve_the_shell_uncached(demo, path):
    res = demo.get(path)
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    assert res.headers["cache-control"] == "no-cache"
    assert res.text == demo.get("/").text


@pytest.mark.parametrize("path", ["/u/Bad-Handle!", "/u/ab", "/u/" + "x" * 21, "/u/", "/p/abc", "/t/short"])
def test_bad_page_routes_404(demo, path):
    assert demo.get(path).status_code == 404


# --- headers ---------------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/", "/discover", "/u/maya", "/static/js/main.js", "/api/bootstrap"])
def test_strict_csp(google, path):
    assert google.get(path).headers["content-security-policy"] == STRICT_CSP


@pytest.mark.parametrize("path", ["/legacy/", "/static/legacy/app.js", "/static/app.js", "/static/styles.css"])
def test_classic_ui_is_gone(google, path):
    """Cutover (§7.3): the classic UI and its relaxed CSP no longer exist."""
    res = google.get(path)
    assert res.status_code == 404
    assert res.headers["content-security-policy"] == STRICT_CSP


# --- sign-in return path ---------------------------------------------------------------------

def _sign_in_with_next(client, nxt):
    params = {} if nxt is None else {"next": nxt}
    login = client.get("/auth/google/login", params=params, follow_redirects=False)
    assert login.status_code in (302, 307)
    state = urllib.parse.parse_qs(urllib.parse.urlparse(login.headers["location"]).query)["state"][0]
    return client.get("/auth/google/callback", params={"code": "c", "state": state}, follow_redirects=False)


@pytest.mark.parametrize("nxt", ["/p/12", "/p/12-late-shift?track=4", "/u/maya#badges", "/discover?tab=people"])
def test_sign_in_returns_to_next(google, nxt):
    res = _sign_in_with_next(google, nxt)
    assert res.status_code == 303 and res.headers["location"] == nxt


@pytest.mark.parametrize("nxt", [None, "", "//evil.example", "/\\evil", "https://evil.example", "/api/x",
                                 "/auth/x", "/auth/google/login", "evil", "/p/<script>", "/" + "a" * 600,
                                 "/p/1\r\nX: y"])
def test_sign_in_ignores_unsafe_next(google, nxt):
    res = _sign_in_with_next(google, nxt)
    assert res.status_code == 303 and res.headers["location"] == "/"


def test_callback_accepts_a_pending_login_in_the_old_bare_verifier_form():
    from app.auth import _pending_parts

    assert _pending_parts("verifier") == ("verifier", None)
    assert _pending_parts({"v": "verifier", "next": "/p/1"}) == ("verifier", "/p/1")
    assert _pending_parts({"v": "verifier", "next": "//evil"}) == ("verifier", None)
    assert _pending_parts(None) == (None, None)
    assert _pending_parts({"next": "/p/1"}) == (None, None)


def test_failed_sign_in_still_goes_to_the_hash_routes(google):
    import test_security_backend as sb

    sb.FakeGoogle.userinfo = {"sub": "g-x", "email": "stranger@example.com", "email_verified": True, "name": "X"}
    res = _sign_in_with_next(google, "/p/12")
    assert res.headers["location"] == "/#not-invited"


# --- handle confirmation ----------------------------------------------------------------------

def test_new_google_user_needs_a_handle_until_they_confirm_it(google):
    google_sign_in(google)
    me = google.get("/api/bootstrap").json()["current_user"]
    assert me["needs_handle"] is True
    assert google.put("/api/me/handle", json={"handle": me["username"]}).status_code == 200
    assert google.get("/api/bootstrap").json()["current_user"]["needs_handle"] is False


def test_choosing_a_new_handle_also_clears_the_prompt(google):
    google_sign_in(google)
    assert google.put("/api/me/handle", json={"handle": "party_time"}).status_code == 200
    me = google.get("/api/bootstrap").json()["current_user"]
    assert me["username"] == "party_time" and me["needs_handle"] is False


def test_anonymous_never_needs_a_handle(google):
    assert google.get("/api/bootstrap").json()["current_user"]["needs_handle"] is False


def test_seeded_users_have_chosen_their_handles(demo):
    conn = db()
    try:
        assert conn.execute("SELECT COUNT(*) FROM users WHERE handle_chosen_at IS NULL").fetchone()[0] == 0
    finally:
        conn.close()


def test_guarded_alter_is_idempotent_on_an_existing_database(demo):
    from app.database import init_db

    conn = db()
    conn.execute("ALTER TABLE users DROP COLUMN handle_chosen_at")  # an existing Phase 1a database
    conn.commit()
    conn.close()
    init_db()
    init_db()
    conn = db()
    try:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)")]
        assert cols.count("handle_chosen_at") == 1
        assert conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == "3"
        # Existing users get NULL from the ALTER, so Google users see the prompt once.
        assert conn.execute("SELECT COUNT(*) FROM users WHERE handle_chosen_at IS NULL").fetchone()[0] == 4
    finally:
        conn.close()


@pytest.mark.parametrize("path", ["/static/js/main.js", "/static/js/editing/undo.js", "/static/styles/tokens.css"])
def test_static_assets_revalidate(demo, path):
    """Modules are imported without ?v=, so the browser must revalidate them (ETag -> 304)."""
    res = demo.get(path)
    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-cache"
    assert res.headers.get("etag")

import hashlib
import logging
import os
import secrets
import sqlite3
import stat
from pathlib import Path
from typing import Generator

from app.badges import evaluate_and_award_badges
from app.ops import ensure_crate

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = Path(os.environ.get("WEBJAM_DB_PATH", str(DATA_DIR / "webjam.db")))

logger = logging.getLogger("webjam.database")


def hash_password(password: str) -> str:
    return hashlib.sha256(f"webjam-salt::{password}".encode("utf-8")).hexdigest()


def _ensure_private_db_file(path: Path) -> None:
    """Create the data dir (0700) and the DB file (0600) before SQLite opens it.

    SQLite creates its -wal/-shm files with the same permissions as the DB file,
    so the whole database stays readable only by the service user.
    """
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists():
        fd = os.open(str(path), os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        os.chmod(str(path), 0o600)


def tighten_db_permissions(path: Path) -> None:
    """Make an existing DB (and its -wal/-shm sidecars) private to the service user.

    _ensure_private_db_file only sets modes on files it creates, so a database that
    predates it (or was copied in) may still be group/world readable.
    """
    getuid = getattr(os, "getuid", None)
    for p in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            st = os.stat(p)
        except FileNotFoundError:
            continue
        if stat.S_IMODE(st.st_mode) & 0o077 and (getuid is None or st.st_uid == getuid()):
            os.chmod(p, 0o600)
            logger.warning("Tightened permissions on %s to 0600", p)
    try:
        parent_mode = stat.S_IMODE(os.stat(path.parent).st_mode)
    except FileNotFoundError:
        return
    if parent_mode & 0o077:
        logger.warning("Database directory %s is readable by other users (mode %o); consider chmod 700", path.parent, parent_mode)


def get_connection() -> sqlite3.Connection:
    _ensure_private_db_file(DB_PATH)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn


def get_db() -> Generator[sqlite3.Connection, None, None]:
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    bio TEXT DEFAULT '',
    avatar_color TEXT DEFAULT '#2b5b84',
    password_hash TEXT DEFAULT '',
    session_epoch INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    handle_chosen_at TEXT NULL  -- NULL until the user confirms or picks a handle (Google sign-ups)
);

-- Handles a user gave up. Only that user can take one back: old activity lines
-- still say '@handle', so it must never point at somebody else.
CREATE TABLE IF NOT EXISTS handle_history (
    handle TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    released_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invites (
    email TEXT PRIMARY KEY,  -- always stored lowercased
    invited_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS google_accounts (
    google_sub TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    email TEXT DEFAULT '',
    access_token TEXT,
    refresh_token TEXT,
    token_expires_at INTEGER DEFAULT 0,
    scope TEXT DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS songs (
    video_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    channel_title TEXT NOT NULL,
    thumbnail_url TEXT NOT NULL,
    -- "First found by": NULL until the song's first placement in a public (LISTED) playlist.
    first_added_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    first_added_playlist_id INTEGER REFERENCES playlists(id) ON DELETE SET NULL,
    first_added_at TEXT,
    total_plays INTEGER DEFAULT 0,
    created_at TEXT NOT NULL  -- when Webjam cached the metadata
);

CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'playlist' CHECK (kind IN ('playlist', 'crate')),
    visibility TEXT NOT NULL DEFAULT 'public' CHECK (visibility IN ('public', 'unlisted', 'private')),
    version INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    plays INTEGER DEFAULT 0,
    forked_from_playlist_id INTEGER REFERENCES playlists(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (kind = 'playlist' OR visibility = 'private')  -- a crate is always private
);
CREATE UNIQUE INDEX IF NOT EXISTS one_crate_per_user ON playlists(user_id) WHERE kind = 'crate';
CREATE INDEX IF NOT EXISTS idx_playlists_listed ON playlists(kind, visibility, updated_at);
CREATE INDEX IF NOT EXISTS idx_playlists_owner ON playlists(user_id, updated_at);

CREATE TABLE IF NOT EXISTS playlist_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    video_id TEXT NOT NULL REFERENCES songs(video_id),
    position REAL NOT NULL,  -- fractional sort key, NOT the 1-based index (the API exposes the ordinal)
    added_by_user_id INTEGER NOT NULL REFERENCES users(id),
    track_note TEXT DEFAULT '',
    start_seconds REAL DEFAULT NULL,  -- REAL now; the API keeps whole seconds until Phase 2
    end_seconds REAL DEFAULT NULL,
    migrated_from_video_id TEXT DEFAULT NULL,
    migrated_from_title TEXT DEFAULT NULL,
    display_title TEXT DEFAULT NULL,  -- per-row title override; songs.title stays YouTube's
    source_playlist_id INTEGER REFERENCES playlists(id) ON DELETE SET NULL,
    source_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    -- No FK on purpose: an FK would SET NULL other users' links whenever a row is deleted or moved,
    -- and undo could never put them back. Readers treat a dangling id as no link.
    source_track_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pt_order ON playlist_tracks(playlist_id, position, id);
CREATE INDEX IF NOT EXISTS idx_pt_video ON playlist_tracks(video_id);
CREATE INDEX IF NOT EXISTS idx_pt_source_user ON playlist_tracks(source_user_id);
CREATE INDEX IF NOT EXISTS idx_pt_source_playlist ON playlist_tracks(source_playlist_id);
CREATE INDEX IF NOT EXISTS idx_pt_source_track ON playlist_tracks(source_track_id);

-- The op log: one row per per-playlist effect of an op batch. The undo stack is per user.
CREATE TABLE IF NOT EXISTS playlist_ops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    op_id TEXT NOT NULL UNIQUE,      -- "<client op_id>#<n>"
    batch_id TEXT NOT NULL,          -- the client's op_id (idempotency key)
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    group_id TEXT NOT NULL,          -- undo unit; defaults to batch_id
    kind TEXT NOT NULL CHECK (kind IN ('insert', 'delete', 'move', 'update', 'set_meta', 'reorder')),
    forward_json TEXT NOT NULL,
    inverse_json TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    undone_at TEXT,
    discarded_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_ops_user ON playlist_ops(user_id, id);
CREATE INDEX IF NOT EXISTS idx_ops_group ON playlist_ops(user_id, group_id);
CREATE INDEX IF NOT EXISTS idx_ops_batch ON playlist_ops(user_id, batch_id);
CREATE INDEX IF NOT EXISTS idx_ops_playlist ON playlist_ops(playlist_id, id);

CREATE TABLE IF NOT EXISTS user_dock (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
    sort REAL,
    last_used_at TEXT,
    PRIMARY KEY (user_id, playlist_id)
);
CREATE INDEX IF NOT EXISTS idx_dock_recent ON user_dock(user_id, last_used_at);

CREATE TABLE IF NOT EXISTS play_events (
    viewer_key TEXT NOT NULL,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    video_id TEXT NOT NULL DEFAULT '',
    at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_play_events_lookup ON play_events (viewer_key, playlist_id, video_id, at);
CREATE INDEX IF NOT EXISTS idx_play_events_at ON play_events (at);

CREATE TABLE IF NOT EXISTS playlist_likes (
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    count INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    PRIMARY KEY (playlist_id, user_id)
);

CREATE TABLE IF NOT EXISTS song_likes (
    video_id TEXT NOT NULL REFERENCES songs(video_id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    count INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    PRIMARY KEY (video_id, user_id)
);

CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    author_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    target_type TEXT NOT NULL CHECK(target_type IN ('playlist', 'song', 'profile')),
    target_id TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_comments_target ON comments(target_type, target_id, id);

CREATE TABLE IF NOT EXISTS user_badges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    badge_key TEXT NOT NULL,
    context_note TEXT DEFAULT '',
    awarded_at TEXT NOT NULL,
    op_group_key TEXT NULL,  -- "<actor id>:<group id>" when an undoable op group earned it (undo revokes it)
    UNIQUE(user_id, badge_key)
);

CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    action_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    link_type TEXT DEFAULT '',
    link_id TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    op_group_id TEXT NULL  -- set by apply_ops so that undo can remove the lines
);
CREATE INDEX IF NOT EXISTS idx_activity_group ON activity_log(actor_user_id, op_group_id);
CREATE INDEX IF NOT EXISTS idx_activity_recent ON activity_log(created_at, id);
"""

SCHEMA_VERSION = 3
SCHEMA_CHANGED_MESSAGE = (
    "Webjam Phase 1 changed the schema. Delete the database file (WEBJAM_DB_PATH) and restart; "
    "it will be reseeded."
)


def ensure_initial_song_likes(conn: sqlite3.Connection) -> None:
    """Seed initial song likes (including Superlove multi-counts) if none exist yet."""
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS cnt, COALESCE(MAX(count), 0) AS max_cnt FROM song_likes")
    row = cur.fetchone()
    if row["cnt"] > 0 and row["max_cnt"] > 1:
        return
    default_song_likes = [
        ("0PsOJ_fRckg", 1, 5, "2026-10-29 12:01:00"),
        ("0PsOJ_fRckg", 3, 8, "2026-10-28 09:05:00"),
        ("0PsOJ_fRckg", 4, 2, "2026-10-28 20:10:00"),
        ("TNsSBhl_2LI", 1, 3, "2026-10-29 12:02:00"),
        ("TNsSBhl_2LI", 4, 6, "2026-10-29 10:16:00"),
        ("c_56qQqxvm4", 1, 2, "2026-10-29 12:06:00"),
        ("c_56qQqxvm4", 2, 7, "2026-10-28 11:06:00"),
        ("4fHw4GeW3EU", 4, 4, "2026-10-29 10:21:00"),
        ("w2oGFEsaG5c", 1, 9, "2026-10-29 15:21:00"),
        ("BI2Et19vDCM", 2, 3, "2026-10-30 16:05:00"),
        ("BI2Et19vDCM", 3, 5, "2026-10-31 11:05:00"),
    ]
    for vid, uid, cnt, ts in default_song_likes:
        cur.execute("SELECT 1 FROM songs WHERE video_id = ?", (vid,))
        has_song = cur.fetchone()
        cur.execute("SELECT 1 FROM users WHERE id = ?", (uid,))
        has_user = cur.fetchone()
        if has_song and has_user:
            cur.execute(
                """
                INSERT INTO song_likes (video_id, user_id, count, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(video_id, user_id) DO UPDATE SET count = excluded.count
                """,
                (vid, uid, cnt, ts),
            )
    conn.commit()


def _check_schema_version(conn: sqlite3.Connection) -> None:
    """Refuse to open a pre-Phase 1 database: there is no migration, wipe and reseed instead."""
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "users" not in tables:
        return
    if not conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        return
    version = None
    if "meta" in tables:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        version = row["value"] if row else None
    try:
        ok = version is not None and int(version) >= SCHEMA_VERSION
    except ValueError:
        ok = False
    if not ok:
        raise RuntimeError(SCHEMA_CHANGED_MESSAGE)


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Additive Phase 1b migration (schema_version stays 3): guarded so it is idempotent."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
    if "handle_chosen_at" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN handle_chosen_at TEXT")


def init_db() -> None:
    conn = get_connection()
    try:
        _check_schema_version(conn)
        conn.execute("PRAGMA journal_mode = WAL;")  # persistent; concurrent readers + one writer
        conn.executescript(SCHEMA_SQL)
        _add_missing_columns(conn)
        conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
        )
        # A random id per database: sessions carry it, so a wiped DB rejects old cookies.
        conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('install_id', ?)", (secrets.token_urlsafe(16),)
        )
        conn.commit()
        tighten_db_permissions(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS cnt FROM users")
        if cur.fetchone()["cnt"] == 0:
            seed_initial_data(conn)
    finally:
        conn.close()


def seed_initial_data(conn: sqlite3.Connection) -> None:
    """Seed Webjam with classic Webjay-style community curators, playlists, provenance, and comments."""
    cur = conn.cursor()
    default_pw = hash_password("webjam")

    users = [
        (
            1,
            "dan",
            "Dan (You)",
            "Building Webjam — keeping the spirit of Webjay alive. Drop a link, leave liner notes, steal good tracks.",
            "#1f5f5b",
            default_pw,
            "2026-10-25 10:00:00",
        ),
        (
            2,
            "lucas",
            "Lucas G.",
            "Lover of XSPF, plain text tracklists, and handmade web mixtapes. Simplicity always wins.",
            "#8c3b2b",
            default_pw,
            "2026-10-25 11:15:00",
        ),
        (
            3,
            "maya",
            "Maya Lin-Kratz",
            "Late-night ambient, KEXP live sessions, dub techno, and warm tape saturation.",
            "#3d4e81",
            default_pw,
            "2026-10-26 09:30:00",
        ),
        (
            4,
            "julian",
            "Julian Vance",
            "Post-punk basslines, indie dream-pop, and obscure 12-inch B-sides found at 2am.",
            "#6b4226",
            default_pw,
            "2026-10-27 14:20:00",
        ),
    ]
    cur.executemany(
        """
        INSERT INTO users (id, username, display_name, bio, avatar_color, password_hash, created_at, handle_chosen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [(*u, u[-1]) for u in users],
    )

    # Playlists
    playlists = [
        (
            1,
            2,  # lucas
            "Sunday Morning Coffee & Tape Hiss",
            "A sequence for slow mornings when the espresso machine is warming up. Start from track 1 and let it roll continuously.",
            14,
            "2026-10-26 08:30:00",
            "2026-10-28 11:00:00",
        ),
        (
            2,
            3,  # maya
            "2AM Terminal Glow (Coding Mix)",
            "Instrumental, hypnotic live sessions and electronic cuts that lock your brain into flow state without lyrical distraction.",
            19,
            "2026-10-27 22:10:00",
            "2026-10-30 01:15:00",
        ),
        (
            3,
            4,  # julian
            "Basement Velvet & Motorik Beats",
            "Driving rhythms and reverb-soaked guitars. Half discovered in the wild, half borrowed from Maya and Lucas.",
            11,
            "2026-10-28 19:45:00",
            "2026-10-31 16:00:00",
        ),
        (
            4,
            1,  # dan
            "Webjam Inaugural Transmission",
            "Testing the pipes! A mix of tracks I dug up myself plus a couple gems I grabbed from @lucas and @maya's mixes.",
            8,
            "2026-10-29 15:00:00",
            "2026-11-01 18:20:00",
        ),
    ]
    cur.executemany(
        """
        INSERT INTO playlists (id, user_id, kind, visibility, version, title, description, plays, created_at, updated_at)
        VALUES (?, ?, 'playlist', 'public', 0, ?, ?, ?, ?, ?)
        """,
        playlists,
    )

    # Verified embeddable YouTube music sessions & indie videos
    # (video_id, title, channel_title, first_added_by_user_id, first_added_playlist_id, first_added_at, total_plays)
    songs = [
        (
            "0PsOJ_fRckg",
            "Khruangbin - Full Performance (Live on KEXP)",
            "KEXP",
            2,
            1,
            "2026-10-26 08:31:00",
            22,
        ),
        (
            "TNsSBhl_2LI",
            "Men I Trust - Lauren",
            "Men I Trust",
            2,
            1,
            "2026-10-26 08:35:00",
            15,
        ),
        (
            "c_56qQqxvm4",
            "Tycho - Full Performance (Live on KEXP)",
            "KEXP",
            3,
            2,
            "2026-10-27 22:12:00",
            18,
        ),
        (
            "4fHw4GeW3EU",
            "Kiasmos - Full Performance (Live on KEXP)",
            "KEXP",
            3,
            2,
            "2026-10-27 22:18:00",
            16,
        ),
        (
            "w2oGFEsaG5c",
            "Bicep - Glue & Apricots (Glastonbury 2022)",
            "BBC Music",
            3,
            2,
            "2026-10-27 22:25:00",
            14,
        ),
        (
            "-y9rkNXBuhg",
            "Parcels - Overnight (Live from Hansa Studios, Berlin)",
            "Parcels",
            4,
            3,
            "2026-10-28 19:50:00",
            11,
        ),
        (
            "6647smeduGI",
            "Alvvays - Full Performance (Live on KEXP)",
            "KEXP",
            4,
            3,
            "2026-10-28 19:55:00",
            9,
        ),
        (
            "BI2Et19vDCM",
            "CARIBOU - Can't Do Without You",
            "Caribou",
            1,
            4,
            "2026-10-29 15:05:00",
            10,
        ),
        (
            "S8fFo4REHOE",
            "Floating Points - LesAlpx (Official Audio)",
            "Floating Points",
            1,
            4,
            "2026-10-29 15:10:00",
            7,
        ),
    ]
    for vid, title, ch, uid, pid, ts, plays in songs:
        cur.execute(
            """
            INSERT INTO songs (video_id, title, channel_title, thumbnail_url, first_added_by_user_id, first_added_playlist_id, first_added_at, total_plays, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                vid,
                title,
                ch,
                f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                uid,
                pid,
                ts,
                plays,
                ts,
            ),
        )

    # Playlist tracks with provenance (source_playlist_id, source_user_id)
    # (playlist_id, video_id, position, added_by_user_id, track_note, source_playlist_id, source_user_id, created_at)
    tracks = [
        # Playlist 1: Lucas's Sunday Morning Coffee
        (
            1,
            "0PsOJ_fRckg",
            1,
            2,
            "Mark Speer's guitar tone in this KEXP room is pure warm sunlight.",
            None,
            None,
            "2026-10-26 08:31:00",
        ),
        (
            1,
            "TNsSBhl_2LI",
            2,
            2,
            "That Chorus-drenched bassline never gets old.",
            None,
            None,
            "2026-10-26 08:35:00",
        ),
        (
            1,
            "c_56qQqxvm4",
            3,
            2,
            "Grabbed from @maya — fits the morning transition into afternoon focus.",
            2,
            3,
            "2026-10-28 11:00:00",
        ),
        # Playlist 2: Maya's 2AM Terminal Glow
        (
            2,
            "c_56qQqxvm4",
            1,
            3,
            "Scott Hansen's synth patches + live drums = instant flow state.",
            None,
            None,
            "2026-10-27 22:12:00",
        ),
        (
            2,
            "4fHw4GeW3EU",
            2,
            3,
            "Ólafur Arnalds & Janus Rasmussen in Reykjavik. Minimalist perfection.",
            None,
            None,
            "2026-10-27 22:18:00",
        ),
        (
            2,
            "w2oGFEsaG5c",
            3,
            3,
            "The breakbeat nostalgia peak live at Glastonbury.",
            None,
            None,
            "2026-10-27 22:25:00",
        ),
        (
            2,
            "0PsOJ_fRckg",
            4,
            3,
            "Borrowed from @lucas's Sunday mix for the cooldown.",
            1,
            2,
            "2026-10-28 01:00:00",
        ),
        # Playlist 3: Julian's Basement Velvet
        (
            3,
            "-y9rkNXBuhg",
            1,
            4,
            "Tightest rhythm section on the planet right here.",
            None,
            None,
            "2026-10-28 19:50:00",
        ),
        (
            3,
            "6647smeduGI",
            2,
            4,
            "Jangle pop bliss.",
            None,
            None,
            "2026-10-28 19:55:00",
        ),
        (
            3,
            "TNsSBhl_2LI",
            3,
            4,
            "Hat tip to @lucas for reminding me of this bassline.",
            1,
            2,
            "2026-10-29 10:15:00",
        ),
        (
            3,
            "4fHw4GeW3EU",
            4,
            4,
            "Found via @maya's coding mix — too good not to include.",
            2,
            3,
            "2026-10-29 10:20:00",
        ),
        # Playlist 4: Dan's Webjam Inaugural Transmission
        (
            4,
            "BI2Et19vDCM",
            1,
            1,
            "The slow build at 3:15 is why we make playlists.",
            None,
            None,
            "2026-10-29 15:05:00",
        ),
        (
            4,
            "S8fFo4REHOE",
            2,
            1,
            "Modular synth perfection from Sam Shepherd.",
            None,
            None,
            "2026-10-29 15:10:00",
        ),
        (
            4,
            "0PsOJ_fRckg",
            3,
            1,
            "Snagged from @lucas's Sunday Coffee mix!",
            1,
            2,
            "2026-10-29 15:15:00",
        ),
        (
            4,
            "w2oGFEsaG5c",
            4,
            1,
            "Taken from @maya's 2AM Terminal Glow.",
            2,
            3,
            "2026-10-29 15:20:00",
        ),
    ]
    cur.executemany(
        """
        INSERT INTO playlist_tracks
        (playlist_id, video_id, position, added_by_user_id, track_note, source_playlist_id, source_user_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [(pid, vid, float(pos), *rest) for pid, vid, pos, *rest in tracks],
    )

    # Every seeded user gets a crate (ids 5-8, after playlists 1-4).
    crates = {uid: ensure_crate(conn, uid) for uid in (1, 2, 3, 4)}
    # Dan's crate holds two rows copied from Maya's and Julian's playlists. They are the
    # privacy canary: they must never show up in any count, feed or badge.
    for pos, (vid, src_pid, ts) in enumerate(
        (("c_56qQqxvm4", 2, "2026-11-01 19:00:00"), ("6647smeduGI", 3, "2026-11-01 19:05:00")), start=1
    ):
        src = cur.execute(
            """
            SELECT pt.id, p.user_id FROM playlist_tracks pt JOIN playlists p ON p.id = pt.playlist_id
            WHERE pt.playlist_id = ? AND pt.video_id = ? ORDER BY pt.position, pt.id LIMIT 1
            """,
            (src_pid, vid),
        ).fetchone()
        cur.execute(
            """
            INSERT INTO playlist_tracks
            (playlist_id, video_id, position, added_by_user_id, track_note, source_playlist_id, source_user_id,
             source_track_id, created_at)
            VALUES (?, ?, ?, 1, '', ?, ?, ?, ?)
            """,
            (crates[1], vid, float(pos), src_pid, src["user_id"], src["id"], ts),
        )
    # Dan pins his playlist to the dock.
    cur.execute("INSERT INTO user_dock (user_id, playlist_id, pinned, sort, last_used_at) VALUES (1, 4, 1, 1.0, NULL)")

    # Likes
    likes = [
        (1, 1, "2026-10-29 12:00:00"),
        (1, 3, "2026-10-28 09:00:00"),
        (1, 4, "2026-10-28 20:00:00"),
        (2, 1, "2026-10-29 12:05:00"),
        (2, 2, "2026-10-28 11:05:00"),
        (2, 4, "2026-10-29 10:25:00"),
        (3, 1, "2026-10-30 14:00:00"),
        (4, 2, "2026-10-30 16:00:00"),
        (4, 3, "2026-10-31 11:00:00"),
    ]
    cur.executemany(
        "INSERT INTO playlist_likes (playlist_id, user_id, created_at) VALUES (?, ?, ?)",
        likes,
    )
    ensure_initial_song_likes(conn)

    # Comments on playlists, songs, and profiles
    comments = [
        (
            3,
            "playlist",
            "1",
            "Sequencing Khruangbin right into Men I Trust is an elite Sunday move.",
            "2026-10-27 10:12:00",
        ),
        (
            1,
            "playlist",
            "1",
            "Stole the opener for my inaugural mix. Pure Webjay vibes.",
            "2026-10-29 15:16:00",
        ),
        (
            2,
            "playlist",
            "4",
            "Welcome to the neighborhood! That Caribou opener transitions into Floating Points like butter.",
            "2026-10-30 16:02:00",
        ),
        (
            4,
            "song",
            "0PsOJ_fRckg",
            "Crazy how @lucas dropped this on day one and now three of us have it in rotation.",
            "2026-10-30 18:00:00",
        ),
        (
            2,
            "profile",
            "1",
            "Love what you've built here, Dan. Having per-song lineage and liner notes back on the web feels like coming home.",
            "2026-10-31 09:45:00",
        ),
        (
            3,
            "profile",
            "2",
            "Thanks for the Khruangbin discovery — been spinning your Sunday mix all week.",
            "2026-10-29 19:20:00",
        ),
    ]
    cur.executemany(
        """
        INSERT INTO comments (author_user_id, target_type, target_id, body, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        comments,
    )

    # Activity log entries
    activities = [
        (
            2,
            "discovered_song",
            "was the first on Webjam to add 'Khruangbin - Full Performance (Live on KEXP)'",
            "song",
            "0PsOJ_fRckg",
            "2026-10-26 08:31:00",
        ),
        (
            3,
            "created_playlist",
            "published playlist '2AM Terminal Glow (Coding Mix)'",
            "playlist",
            "2",
            "2026-10-27 22:10:00",
        ),
        (
            4,
            "grabbed_track",
            "grabbed 'Kiasmos - Full Performance (Live on KEXP)' from @maya into 'Basement Velvet & Motorik Beats'",
            "playlist",
            "3",
            "2026-10-29 10:20:00",
        ),
        (
            1,
            "grabbed_track",
            "grabbed 'Khruangbin - Full Performance (Live on KEXP)' from @lucas into 'Webjam Inaugural Transmission'",
            "playlist",
            "4",
            "2026-10-29 15:15:00",
        ),
        (
            2,
            "commented",
            "left a note on @dan's profile",
            "profile",
            "1",
            "2026-10-31 09:45:00",
        ),
    ]
    cur.executemany(
        """
        INSERT INTO activity_log (actor_user_id, action_type, summary, link_type, link_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        activities,
    )

    conn.commit()

    # Evaluate and award initial badges for all seeded users
    for uid in (1, 2, 3, 4):
        evaluate_and_award_badges(conn, uid, record_activity=False)


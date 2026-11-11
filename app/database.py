import hashlib
import os
import sqlite3
from pathlib import Path
from typing import Generator

from app.badges import evaluate_and_award_badges

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = Path(os.environ.get("WEBJAM_DB_PATH", str(DATA_DIR / "webjam.db")))


def hash_password(password: str) -> str:
    return hashlib.sha256(f"webjam-salt::{password}".encode("utf-8")).hexdigest()


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def get_db() -> Generator[sqlite3.Connection, None, None]:
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    bio TEXT DEFAULT '',
    avatar_color TEXT DEFAULT '#2b5b84',
    password_hash TEXT DEFAULT '',
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
    first_added_by_user_id INTEGER NOT NULL REFERENCES users(id),
    first_added_playlist_id INTEGER,
    first_added_at TEXT NOT NULL,
    total_plays INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    plays INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS playlist_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    video_id TEXT NOT NULL REFERENCES songs(video_id),
    position INTEGER NOT NULL,
    added_by_user_id INTEGER NOT NULL REFERENCES users(id),
    track_note TEXT DEFAULT '',
    start_seconds INTEGER DEFAULT NULL,
    end_seconds INTEGER DEFAULT NULL,
    migrated_from_video_id TEXT DEFAULT NULL,
    migrated_from_title TEXT DEFAULT NULL,
    source_playlist_id INTEGER REFERENCES playlists(id) ON DELETE SET NULL,
    source_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);

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

CREATE TABLE IF NOT EXISTS user_badges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    badge_key TEXT NOT NULL,
    context_note TEXT DEFAULT '',
    awarded_at TEXT NOT NULL,
    UNIQUE(user_id, badge_key)
);

CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    action_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    link_type TEXT DEFAULT '',
    link_id TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
"""


LEGACY_VIDEO_ID_MAP = {
    "q4xKvHANqjk": (
        "0PsOJ_fRckg",
        "Khruangbin - Full Performance (Live on KEXP)",
        "KEXP",
    ),
    "U4U19zwFENs": (
        "TNsSBhl_2LI",
        "Men I Trust - Lauren",
        "Men I Trust",
    ),
    "Z5NoQg8LdDk": (
        "c_56qQqxvm4",
        "Tycho - Full Performance (Live on KEXP)",
        "KEXP",
    ),
    "c3yEjD_oijw": (
        "4fHw4GeW3EU",
        "Kiasmos - Full Performance (Live on KEXP)",
        "KEXP",
    ),
    "N7FZ2cUX1rM": (
        "w2oGFEsaG5c",
        "Bicep - Glue & Apricots (Glastonbury 2022)",
        "BBC Music",
    ),
    "2fX6qg03L38": (
        "-y9rkNXBuhg",
        "Parcels - Overnight (Live from Hansa Studios, Berlin)",
        "Parcels",
    ),
    "L9rQ6LA63Y0": (
        "6647smeduGI",
        "Alvvays - Full Performance (Live on KEXP)",
        "KEXP",
    ),
    "T3T9pG9s-h0": (
        "BI2Et19vDCM",
        "CARIBOU - Can't Do Without You",
        "Caribou",
    ),
    "p6vH6ntI1vY": (
        "S8fFo4REHOE",
        "Floating Points - LesAlpx (Official Audio)",
        "Floating Points",
    ),
}


def ensure_playlist_track_clip_columns(conn: sqlite3.Connection) -> None:
    """Ensure start_seconds/end_seconds on playlist_tracks, drop legacy UNIQUE(playlist_id, video_id), and ensure count on likes."""
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(playlist_tracks)")
    cols = {row["name"] for row in cur.fetchall()}
    changed = False
    if "start_seconds" not in cols:
        cur.execute("ALTER TABLE playlist_tracks ADD COLUMN start_seconds INTEGER DEFAULT NULL")
        changed = True
    if "end_seconds" not in cols:
        cur.execute("ALTER TABLE playlist_tracks ADD COLUMN end_seconds INTEGER DEFAULT NULL")
        changed = True
    if "migrated_from_video_id" not in cols:
        cur.execute("ALTER TABLE playlist_tracks ADD COLUMN migrated_from_video_id TEXT DEFAULT NULL")
        changed = True
    if "migrated_from_title" not in cols:
        cur.execute("ALTER TABLE playlist_tracks ADD COLUMN migrated_from_title TEXT DEFAULT NULL")
        changed = True

    # If playlist_tracks still has the legacy UNIQUE(playlist_id, video_id) constraint, rebuild without it
    cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='playlist_tracks'")
    table_sql_row = cur.fetchone()
    if table_sql_row and "UNIQUE" in (table_sql_row["sql"] or "").upper():
        conn.execute("PRAGMA foreign_keys = OFF;")
        try:
            cur.execute(
                """
                CREATE TABLE playlist_tracks_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
                    video_id TEXT NOT NULL REFERENCES songs(video_id),
                    position INTEGER NOT NULL,
                    added_by_user_id INTEGER NOT NULL REFERENCES users(id),
                    track_note TEXT DEFAULT '',
                    start_seconds INTEGER DEFAULT NULL,
                    end_seconds INTEGER DEFAULT NULL,
                    migrated_from_video_id TEXT DEFAULT NULL,
                    migrated_from_title TEXT DEFAULT NULL,
                    source_playlist_id INTEGER REFERENCES playlists(id) ON DELETE SET NULL,
                    source_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                INSERT INTO playlist_tracks_new
                (id, playlist_id, video_id, position, added_by_user_id, track_note, start_seconds, end_seconds, migrated_from_video_id, migrated_from_title, source_playlist_id, source_user_id, created_at)
                SELECT id, playlist_id, video_id, position, added_by_user_id, track_note, start_seconds, end_seconds, migrated_from_video_id, migrated_from_title, source_playlist_id, source_user_id, created_at
                FROM playlist_tracks
                """
            )
            cur.execute("DROP TABLE playlist_tracks")
            cur.execute("ALTER TABLE playlist_tracks_new RENAME TO playlist_tracks")
            changed = True
        finally:
            conn.execute("PRAGMA foreign_keys = ON;")

    # Backfill migrated_from_video_id for any track imported earlier with the migration note pattern
    cur.execute(
        """
        SELECT id, track_note FROM playlist_tracks
        WHERE migrated_from_video_id IS NULL
          AND track_note LIKE 'Migrated to embeddable version from %'
        """
    )
    for row in cur.fetchall():
        orig_vid = row["track_note"].replace("Migrated to embeddable version from ", "").strip()[:11]
        if orig_vid:
            cur.execute(
                "UPDATE playlist_tracks SET migrated_from_video_id = ? WHERE id = ?",
                (orig_vid, row["id"]),
            )
            changed = True

    cur.execute("PRAGMA table_info(playlist_likes)")
    pl_like_cols = {row["name"] for row in cur.fetchall()}
    if "count" not in pl_like_cols:
        cur.execute("ALTER TABLE playlist_likes ADD COLUMN count INTEGER NOT NULL DEFAULT 1")
        changed = True

    cur.execute("PRAGMA table_info(song_likes)")
    song_like_cols = {row["name"] for row in cur.fetchall()}
    if "count" not in song_like_cols:
        cur.execute("ALTER TABLE song_likes ADD COLUMN count INTEGER NOT NULL DEFAULT 1")
        changed = True

    if changed:
        conn.commit()


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


def migrate_legacy_seed_videos(conn: sqlite3.Connection) -> None:
    """Replace any legacy placeholder/restricted seed video IDs in an existing DB."""
    cur = conn.cursor()
    conn.execute("PRAGMA foreign_keys = OFF;")
    try:
        changed = False
        for old_vid, (new_vid, new_title, new_channel) in LEGACY_VIDEO_ID_MAP.items():
            cur.execute("SELECT 1 FROM songs WHERE video_id = ?", (old_vid,))
            if not cur.fetchone():
                continue
            changed = True
            new_thumb = f"https://i.ytimg.com/vi/{new_vid}/hqdefault.jpg"
            cur.execute("SELECT 1 FROM songs WHERE video_id = ?", (new_vid,))
            if cur.fetchone():
                cur.execute("DELETE FROM songs WHERE video_id = ?", (old_vid,))
            else:
                cur.execute(
                    """
                    UPDATE songs
                    SET video_id = ?, title = ?, channel_title = ?, thumbnail_url = ?
                    WHERE video_id = ?
                    """,
                    (new_vid, new_title, new_channel, new_thumb, old_vid),
                )
            cur.execute(
                "UPDATE playlist_tracks SET video_id = ? WHERE video_id = ?",
                (new_vid, old_vid),
            )
            cur.execute(
                "UPDATE OR IGNORE song_likes SET video_id = ? WHERE video_id = ?",
                (new_vid, old_vid),
            )
            cur.execute(
                "UPDATE comments SET target_id = ? WHERE target_type = 'song' AND target_id = ?",
                (new_vid, old_vid),
            )
            cur.execute(
                "UPDATE activity_log SET link_id = ? WHERE link_type = 'song' AND link_id = ?",
                (new_vid, old_vid),
            )
        if changed:
            conn.commit()
    finally:
        conn.execute("PRAGMA foreign_keys = ON;")


def init_db() -> None:
    conn = get_connection()
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        ensure_playlist_track_clip_columns(conn)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS cnt FROM users")
        if cur.fetchone()["cnt"] == 0:
            seed_initial_data(conn)
        else:
            migrate_legacy_seed_videos(conn)
            ensure_initial_song_likes(conn)
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
        INSERT INTO users (id, username, display_name, bio, avatar_color, password_hash, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        users,
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
        INSERT INTO playlists (id, user_id, title, description, plays, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
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
            INSERT INTO songs (video_id, title, channel_title, thumbnail_url, first_added_by_user_id, first_added_playlist_id, first_added_at, total_plays)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
        tracks,
    )

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


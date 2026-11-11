from datetime import datetime, timezone
import sqlite3
from typing import Any, Dict, List

from app.visibility import listed

BADGE_CATALOG: List[Dict[str, Any]] = [
    # 1. Discovery Badges (First to add songs to Webjam)
    {
        "key": "first_finder_1",
        "name": "Crate Digger",
        "icon": "◈",
        "category": "Discovery",
        "description": "Be the very first person to add a new song to Webjam.",
        "threshold": 1,
        "metric": "songs_discovered",
    },
    {
        "key": "first_finder_5",
        "name": "A&R Scout",
        "icon": "✦",
        "category": "Discovery",
        "description": "Be the first on Webjam to discover and add 5 unique songs.",
        "threshold": 5,
        "metric": "songs_discovered",
    },
    {
        "key": "first_finder_12",
        "name": "John Peel Award",
        "icon": "♛",
        "category": "Discovery",
        "description": "First to bring 12 unique songs into the Webjam commons.",
        "threshold": 12,
        "metric": "songs_discovered",
    },
    {
        "key": "golden_ear",
        "name": "Golden Ear",
        "icon": "❂",
        "category": "Discovery",
        "description": "Discover a song first on Webjam that 2+ other curators later include in their playlists.",
        "threshold": 1,
        "metric": "viral_discoveries",
    },

    # 2. Tastemaker Badges (People taking tracks from your playlists)
    {
        "key": "spark_1",
        "name": "The Spark",
        "icon": "⚡",
        "category": "Tastemaker",
        "description": "Have another user grab a track from one of your playlists into theirs.",
        "threshold": 1,
        "metric": "tracks_taken_from_me",
    },
    {
        "key": "trendsetter_5",
        "name": "Trendsetter",
        "icon": "📡",
        "category": "Tastemaker",
        "description": "Have tracks grabbed from your playlists 5 or more times across the community.",
        "threshold": 5,
        "metric": "tracks_taken_from_me",
    },
    {
        "key": "source_material",
        "name": "Source Material",
        "icon": "⛲",
        "category": "Tastemaker",
        "description": "Inspire 3+ distinct users to take tracks from your playlists.",
        "threshold": 3,
        "metric": "distinct_users_inspired",
    },

    # 3. Curation & Lineage Badges (Taking tracks & getting plays/likes)
    {
        "key": "crate_borrower_1",
        "name": "Hat Tip",
        "icon": "↳",
        "category": "Curation & Lineage",
        "description": "Grab a track from another user's playlist into your own.",
        "threshold": 1,
        "metric": "tracks_taken_by_me",
    },
    {
        "key": "cross_pollinator_3",
        "name": "Cross-Pollinator",
        "icon": "🐝",
        "category": "Curation & Lineage",
        "description": "Grab 3+ tracks from other curators' playlists into your mixes.",
        "threshold": 3,
        "metric": "tracks_taken_by_me",
    },
    {
        "key": "alchemist_remix",
        "name": "Crate Alchemist",
        "icon": "⚗",
        "category": "Curation & Lineage",
        "description": "Take tracks from another user's playlist and get 5+ plays on your resulting playlist.",
        "threshold": 1,
        "metric": "alchemist_playlists",
    },
    {
        "key": "block_rockin_remix",
        "name": "Selector's Gold",
        "icon": "🏆",
        "category": "Curation & Lineage",
        "description": "Take tracks from at least 2 different curators into one playlist and reach 10+ plays.",
        "threshold": 1,
        "metric": "multi_source_hit_playlists",
    },

    # 4. Reception & Community Badges (Likes, Plays, Liner Notes, Comments)
    {
        "key": "liked_1",
        "name": "Co-Signed",
        "icon": "♥",
        "category": "Reception & Community",
        "description": "Receive your first like on a playlist.",
        "threshold": 1,
        "metric": "likes_received",
    },
    {
        "key": "liked_5",
        "name": "Crowd Pleaser",
        "icon": "★",
        "category": "Reception & Community",
        "description": "Receive 5+ total likes across your playlists.",
        "threshold": 5,
        "metric": "likes_received",
    },
    {
        "key": "heavy_rotation",
        "name": "Heavy Rotation",
        "icon": "▶",
        "category": "Reception & Community",
        "description": "Reach 10+ plays on any single playlist you curated.",
        "threshold": 10,
        "metric": "max_playlist_plays",
    },
    {
        "key": "liner_note_poet",
        "name": "Liner Note Poet",
        "icon": "✎",
        "category": "Reception & Community",
        "description": "Curate a playlist with a description and at least 3 annotated tracks.",
        "threshold": 1,
        "metric": "annotated_playlists",
    },
    {
        "key": "record_shop_regular",
        "name": "Record Shop Regular",
        "icon": "💬",
        "category": "Reception & Community",
        "description": "Leave 3+ comments across playlists, songs, or user profiles.",
        "threshold": 3,
        "metric": "comments_written",
    },
    {
        "key": "superlover",
        "name": "Superlover",
        "icon": "💖",
        "category": "Reception & Community",
        "description": "Smack the ♥ love button 10+ times on a single track or mixtape to bestow Superlove.",
        "threshold": 10,
        "metric": "max_single_superlove",
    },
]

BADGE_BY_KEY = {b["key"]: b for b in BADGE_CATALOG}

# Every playlist-based metric is public on profiles, so it only counts LISTED playlists:
# p = the playlist being measured, tp = the playlist a copied track now lives in.
LISTED_P = listed("p")
LISTED_TP = listed("tp")


def compute_user_metrics(conn: sqlite3.Connection, user_id: int) -> Dict[str, Any]:
    """Compute all live stats and badge-qualifying contexts for a given user."""
    cur = conn.cursor()

    # 1. Songs discovered first
    cur.execute(
        "SELECT video_id, title FROM songs WHERE first_added_by_user_id = ? ORDER BY first_added_at ASC",
        (user_id,),
    )
    discovered_rows = cur.fetchall()
    songs_discovered = len(discovered_rows)
    first_song_title = discovered_rows[0]["title"] if discovered_rows else ""

    # 2. Viral discoveries (songs first discovered by user that appear in 2+ OTHER users' playlists)
    cur.execute(
        f"""
        SELECT s.video_id, s.title, COUNT(DISTINCT pt.added_by_user_id) AS other_curators
        FROM songs s
        JOIN playlist_tracks pt ON pt.video_id = s.video_id AND pt.added_by_user_id != s.first_added_by_user_id
        JOIN playlists tp ON tp.id = pt.playlist_id
        WHERE s.first_added_by_user_id = ? AND {LISTED_TP}
        GROUP BY s.video_id
        HAVING other_curators >= 2
        """,
        (user_id,),
    )
    viral_rows = cur.fetchall()
    viral_discoveries = len(viral_rows)
    viral_context = (
        f"Discovered '{viral_rows[0]['title']}', picked up by {viral_rows[0]['other_curators']} other curators"
        if viral_rows
        else ""
    )

    # 3. Tracks taken FROM me by other users
    cur.execute(
        f"""
        SELECT pt.id, s.title, u.username AS taker_username,
               CASE WHEN {LISTED_P} THEN p.title END AS source_playlist_title
        FROM playlist_tracks pt
        JOIN songs s ON s.video_id = pt.video_id
        JOIN users u ON u.id = pt.added_by_user_id
        JOIN playlists tp ON tp.id = pt.playlist_id
        LEFT JOIN playlists p ON p.id = pt.source_playlist_id
        WHERE pt.source_user_id = ? AND pt.added_by_user_id != ? AND {LISTED_TP}
        ORDER BY pt.created_at ASC
        """,
        (user_id, user_id),
    )
    taken_from_me_rows = cur.fetchall()
    tracks_taken_from_me = len(taken_from_me_rows)
    distinct_users_inspired = len({r["taker_username"] for r in taken_from_me_rows})
    spark_context = (
        f"@{taken_from_me_rows[0]['taker_username']} grabbed '{taken_from_me_rows[0]['title']}' from '{taken_from_me_rows[0]['source_playlist_title'] or 'your playlist'}'"
        if taken_from_me_rows
        else ""
    )

    # 4. Tracks taken BY me from other users
    cur.execute(
        f"""
        SELECT pt.id, s.title, u.username AS source_username, p.title AS my_playlist_title, p.id AS my_playlist_id, p.plays AS my_playlist_plays
        FROM playlist_tracks pt
        JOIN songs s ON s.video_id = pt.video_id
        JOIN users u ON u.id = pt.source_user_id
        JOIN playlists p ON p.id = pt.playlist_id
        WHERE pt.added_by_user_id = ? AND pt.source_user_id IS NOT NULL AND pt.source_user_id != ? AND {LISTED_P}
        ORDER BY pt.created_at ASC
        """,
        (user_id, user_id),
    )
    taken_by_me_rows = cur.fetchall()
    tracks_taken_by_me = len(taken_by_me_rows)
    hat_tip_context = (
        f"Grabbed '{taken_by_me_rows[0]['title']}' from @{taken_by_me_rows[0]['source_username']} into '{taken_by_me_rows[0]['my_playlist_title']}'"
        if taken_by_me_rows
        else ""
    )

    # 5. Alchemist playlists: playlists owned by user that took >=1 track from another user AND reached >=5 plays
    cur.execute(
        f"""
        SELECT p.id, p.title, p.plays,
               GROUP_CONCAT(DISTINCT u.username) AS source_usernames,
               COUNT(DISTINCT pt.source_user_id) AS distinct_sources
        FROM playlists p
        JOIN playlist_tracks pt ON pt.playlist_id = p.id
        JOIN users u ON u.id = pt.source_user_id
        WHERE p.user_id = ? AND {LISTED_P}
          AND pt.source_user_id IS NOT NULL
          AND pt.source_user_id != ?
          AND p.plays >= 5
        GROUP BY p.id
        ORDER BY p.plays DESC
        """,
        (user_id, user_id),
    )
    alchemist_rows = cur.fetchall()
    alchemist_playlists = len(alchemist_rows)
    alchemist_context = ""
    if alchemist_rows:
        r0 = alchemist_rows[0]
        sources_fmt = ", ".join(f"@{u}" for u in (r0["source_usernames"] or "").split(",") if u)
        alchemist_context = f"Took tracks from {sources_fmt} into '{r0['title']}' and reached {r0['plays']} plays"

    # 6. Multi-source hit playlists: took tracks from >=2 distinct users into one playlist AND reached >=10 plays
    multi_source_rows = [r for r in alchemist_rows if r["distinct_sources"] >= 2 and r["plays"] >= 10]
    multi_source_hit_playlists = len(multi_source_rows)
    multi_source_context = ""
    if multi_source_rows:
        r0 = multi_source_rows[0]
        sources_fmt = ", ".join(f"@{u}" for u in (r0["source_usernames"] or "").split(",") if u)
        multi_source_context = (
            f"Combined tracks from {sources_fmt} in '{r0['title']}' and hit {r0['plays']} plays"
        )

    # 7. Likes received across playlists owned by user + songs first discovered by user (summing multi-loves)
    cur.execute(
        f"""
        SELECT COALESCE(SUM(pl.count), 0) AS cnt
        FROM playlist_likes pl
        JOIN playlists p ON p.id = pl.playlist_id
        WHERE p.user_id = ? AND {LISTED_P}
        """,
        (user_id,),
    )
    playlist_likes_received = cur.fetchone()["cnt"]

    song_likes_received = 0
    try:
        cur.execute(
            """
            SELECT COALESCE(SUM(sl.count), 0) AS cnt
            FROM song_likes sl
            JOIN songs s ON s.video_id = sl.video_id
            WHERE s.first_added_by_user_id = ?
            """,
            (user_id,),
        )
        song_likes_received = cur.fetchone()["cnt"]
    except sqlite3.OperationalError:
        song_likes_received = 0

    likes_received = playlist_likes_received + song_likes_received

    # Superlove given by this user (max count on any single song or playlist, plus total loves given)
    max_single_superlove = 0
    superlove_context = ""
    loves_given = 0
    try:
        cur.execute(
            """
            SELECT sl.count, s.title
            FROM song_likes sl
            JOIN songs s ON s.video_id = sl.video_id
            WHERE sl.user_id = ?
            ORDER BY sl.count DESC
            LIMIT 1
            """,
            (user_id,),
        )
        top_song_love = cur.fetchone()
        if top_song_love and top_song_love["count"] > max_single_superlove:
            max_single_superlove = top_song_love["count"]
            superlove_context = f"Bestowed {max_single_superlove}× Superlove on '{top_song_love['title']}'"

        cur.execute(
            f"""
            SELECT pl.count, p.title
            FROM playlist_likes pl
            JOIN playlists p ON p.id = pl.playlist_id
            WHERE pl.user_id = ? AND {LISTED_P}
            ORDER BY pl.count DESC
            LIMIT 1
            """,
            (user_id,),
        )
        top_pl_love = cur.fetchone()
        if top_pl_love and top_pl_love["count"] > max_single_superlove:
            max_single_superlove = top_pl_love["count"]
            superlove_context = f"Bestowed {max_single_superlove}× Superlove on playlist '{top_pl_love['title']}'"

        cur.execute(
            """
            SELECT
              COALESCE((SELECT SUM(count) FROM song_likes WHERE user_id = ?), 0) +
              COALESCE((SELECT SUM(count) FROM playlist_likes WHERE user_id = ?), 0) AS total_given
            """,
            (user_id, user_id),
        )
        loves_given = cur.fetchone()["total_given"]
    except sqlite3.OperationalError:
        pass

    # 8. Max plays on any single playlist & total plays
    cur.execute(
        f"SELECT COALESCE(MAX(plays), 0) AS max_plays, COALESCE(SUM(plays), 0) AS total_plays, COUNT(*) AS playlist_count FROM playlists p WHERE p.user_id = ? AND {LISTED_P}",
        (user_id,),
    )
    play_row = cur.fetchone()
    max_playlist_plays = play_row["max_plays"]
    total_playlist_plays = play_row["total_plays"]
    playlist_count = play_row["playlist_count"]

    # 9. Annotated playlists (description non-empty and >=3 tracks with non-empty track_note)
    cur.execute(
        f"""
        SELECT p.id, p.title, COUNT(pt.id) AS noted_tracks
        FROM playlists p
        JOIN playlist_tracks pt ON pt.playlist_id = p.id
        WHERE p.user_id = ? AND {LISTED_P}
          AND TRIM(COALESCE(p.description, '')) != ''
          AND TRIM(COALESCE(pt.track_note, '')) != ''
        GROUP BY p.id
        HAVING noted_tracks >= 3
        """,
        (user_id,),
    )
    annotated_rows = cur.fetchall()
    annotated_playlists = len(annotated_rows)
    annotated_context = f"Annotated '{annotated_rows[0]['title']}' with liner notes" if annotated_rows else ""

    # 10. Comments written
    cur.execute("SELECT COUNT(*) AS cnt FROM comments WHERE author_user_id = ?", (user_id,))
    comments_written = cur.fetchone()["cnt"]

    return {
        "songs_discovered": songs_discovered,
        "viral_discoveries": viral_discoveries,
        "tracks_taken_from_me": tracks_taken_from_me,
        "distinct_users_inspired": distinct_users_inspired,
        "tracks_taken_by_me": tracks_taken_by_me,
        "alchemist_playlists": alchemist_playlists,
        "multi_source_hit_playlists": multi_source_hit_playlists,
        "likes_received": likes_received,
        "playlist_likes_received": playlist_likes_received,
        "song_likes_received": song_likes_received,
        "max_single_superlove": max_single_superlove,
        "loves_given": loves_given,
        "max_playlist_plays": max_playlist_plays,
        "total_playlist_plays": total_playlist_plays,
        "playlist_count": playlist_count,
        "annotated_playlists": annotated_playlists,
        "comments_written": comments_written,
        "_contexts": {
            "first_finder_1": f"First to add '{first_song_title}'" if first_song_title else "",
            "first_finder_5": f"First to discover {songs_discovered} tracks on Webjam",
            "first_finder_12": f"First to discover {songs_discovered} tracks on Webjam",
            "golden_ear": viral_context,
            "spark_1": spark_context,
            "trendsetter_5": f"Tracks grabbed {tracks_taken_from_me} times by other curators",
            "source_material": f"Inspired {distinct_users_inspired} different curators",
            "crate_borrower_1": hat_tip_context,
            "cross_pollinator_3": f"Curated {tracks_taken_by_me} tracks via community playlists",
            "alchemist_remix": alchemist_context,
            "block_rockin_remix": multi_source_context,
            "liked_1": "First community co-sign on a playlist or song",
            "liked_5": f"Earned {likes_received} total likes & superloves",
            "heavy_rotation": f"Reached {max_playlist_plays} spins on a playlist",
            "liner_note_poet": annotated_context,
            "record_shop_regular": f"Posted {comments_written} community comments",
            "superlover": superlove_context,
        },
    }


def evaluate_and_award_badges(
    conn: sqlite3.Connection, user_id: int, record_activity: bool = True
) -> List[Dict[str, Any]]:
    """
    Evaluate all badges for `user_id`, insert any newly earned badges into `user_badges`,
    and return the list of newly awarded badge objects.
    """
    metrics = compute_user_metrics(conn, user_id)
    contexts = metrics["_contexts"]
    cur = conn.cursor()

    cur.execute("SELECT badge_key FROM user_badges WHERE user_id = ?", (user_id,))
    existing_keys = {row["badge_key"] for row in cur.fetchall()}

    newly_awarded: List[Dict[str, Any]] = []
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    for badge in BADGE_CATALOG:
        bkey = badge["key"]
        if bkey in existing_keys:
            continue
        val = metrics.get(badge["metric"], 0)
        if val >= badge["threshold"]:
            ctx = contexts.get(bkey) or badge["description"]
            cur.execute(
                """
                INSERT OR IGNORE INTO user_badges (user_id, badge_key, context_note, awarded_at)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, bkey, ctx, now_iso),
            )
            awarded_item = {
                **badge,
                "context_note": ctx,
                "awarded_at": now_iso,
            }
            newly_awarded.append(awarded_item)
            if record_activity:
                cur.execute(
                    """
                    INSERT INTO activity_log (actor_user_id, action_type, summary, link_type, link_id, created_at)
                    VALUES (?, 'earned_badge', ?, 'profile', ?, ?)
                    """,
                    (
                        user_id,
                        f"earned the \"{badge['name']}\" badge ({ctx})",
                        str(user_id),
                        now_iso,
                    ),
                )

    conn.commit()
    return newly_awarded


def get_user_badge_progress(conn: sqlite3.Connection, user_id: int) -> Dict[str, Any]:
    """Return earned badges and full catalog with progress for a user."""
    metrics = compute_user_metrics(conn, user_id)
    cur = conn.cursor()
    cur.execute(
        "SELECT badge_key, context_note, awarded_at FROM user_badges WHERE user_id = ? ORDER BY awarded_at DESC",
        (user_id,),
    )
    earned_map = {row["badge_key"]: dict(row) for row in cur.fetchall()}

    all_badges = []
    earned_badges = []
    for b in BADGE_CATALOG:
        bkey = b["key"]
        current_val = metrics.get(b["metric"], 0)
        is_earned = bkey in earned_map
        item = {
            **b,
            "earned": is_earned,
            "current_value": current_val,
            "progress_pct": min(100, int((current_val / max(1, b["threshold"])) * 100)),
            "context_note": earned_map[bkey]["context_note"] if is_earned else "",
            "awarded_at": earned_map[bkey]["awarded_at"] if is_earned else None,
        }
        all_badges.append(item)
        if is_earned:
            earned_badges.append(item)

    clean_metrics = {k: v for k, v in metrics.items() if not k.startswith("_")}
    return {
        "metrics": clean_metrics,
        "earned": earned_badges,
        "catalog": all_badges,
    }

import os
import tempfile
from fastapi.testclient import TestClient


def test_full_webjam_workflow():
    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["WEBJAM_DB_PATH"] = os.path.join(tmpdir, "test_webjam.db")
        from app.database import init_db
        from app.main import app

        init_db()
        with TestClient(app) as client:
            # 1. Bootstrap & seeded data check
            boot = client.get("/api/bootstrap?user_id=1").json()
            assert len(boot["users"]) >= 4
            assert boot["current_user"]["username"] == "dan"

            # 2. Create a new curator account (e.g. 'elena')
            login_res = client.post(
                "/api/auth/login",
                json={
                    "username": "elena",
                    "display_name": "Elena R.",
                    "bio": "Synthwave and library music.",
                },
            ).json()
            elena_id = login_res["user_id"]
            assert login_res["created"] is True

            # 3. Elena creates a playlist and drops a brand-new YouTube link
            create_pl = client.post(
                "/api/playlists",
                json={
                    "user_id": elena_id,
                    "title": "Elena's Crate",
                    "description": "My favorite synth discoveries.",
                    "initial_urls": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                },
            ).json()
            elena_pl_id = create_pl["playlist_id"]
            assert len(create_pl["added_tracks"]) == 1
            assert create_pl["added_tracks"][0]["is_new_discovery"] is True

            # Elena should have earned the 'Crate Digger' (first_finder_1) badge!
            earned_keys = {b["key"] for b in create_pl["new_badges"]}
            assert "first_finder_1" in earned_keys

            # 4. Grab a track from Lucas's playlist (#1, user_id=2) into Elena's playlist
            grab_res = client.post(
                f"/api/playlists/{elena_pl_id}/tracks",
                json={
                    "user_id": elena_id,
                    "urls_or_ids": "0PsOJ_fRckg",
                    "track_note": "Grabbed from @lucas!",
                    "source_playlist_id": 1,
                    "source_user_id": 2,
                },
            ).json()
            assert grab_res["results"][0]["already_in_playlist"] is False
            grab_badge_keys = {b["key"] for b in grab_res["new_badges"]}
            assert "crate_borrower_1" in grab_badge_keys

            # 5. Now Elena's playlist (which has a track taken from Lucas) gets 5 plays -> earns 'Crate Alchemist' (alchemist_remix)!
            play_res = client.post(
                f"/api/playlists/{elena_pl_id}/play",
                json={"video_id": "0PsOJ_fRckg", "count": 5},
            ).json()
            assert play_res["plays"] == 5
            play_badge_keys = {b["key"] for b in play_res["new_badges"]}
            assert "alchemist_remix" in play_badge_keys

            # 6. Check Song Page for '0PsOJ_fRckg' shows first finder (@lucas) and Elena's playlist with provenance
            song_page = client.get("/api/songs/0PsOJ_fRckg").json()
            assert song_page["finder_username"] == "lucas"
            playlist_ids_on_song = [a["playlist_id"] for a in song_page["appearances"]]
            assert elena_pl_id in playlist_ids_on_song

            # 7. Test commenting on a song, playlist, and profile -> earns 'Record Shop Regular' (record_shop_regular)
            client.post(
                "/api/comments",
                json={
                    "author_user_id": elena_id,
                    "target_type": "song",
                    "target_id": "0PsOJ_fRckg",
                    "body": "Incredible live session.",
                },
            )
            client.post(
                "/api/comments",
                json={
                    "author_user_id": elena_id,
                    "target_type": "playlist",
                    "target_id": "1",
                    "body": "Love this Sunday mix!",
                },
            )
            c3 = client.post(
                "/api/comments",
                json={
                    "author_user_id": elena_id,
                    "target_type": "profile",
                    "target_id": "2",
                    "body": "Signing your guestbook, Lucas!",
                },
            ).json()
            c3_badge_keys = {b["key"] for b in c3["new_badges"]}
            assert "record_shop_regular" in c3_badge_keys

            # 8. Test track start/end position via URL query params, duplicate song entries, and explicit clip editing
            clip_drop = client.post(
                f"/api/playlists/{elena_pl_id}/tracks",
                json={
                    "user_id": elena_id,
                    "urls_or_ids": "https://www.youtube.com/watch?v=TNsSBhl_2LI&t=1m15s&end=4m05s",
                    "track_note": "Just the middle groove.",
                },
            ).json()
            assert clip_drop["results"][0]["already_in_playlist"] is False
            assert clip_drop["results"][0]["occurrences_in_playlist"] == 1
            assert clip_drop["results"][0]["start_seconds"] == 75
            assert clip_drop["results"][0]["end_seconds"] == 245

            # Add the exact same song AGAIN to the same playlist (listen to it 100s of times!)
            repeat_drop = client.post(
                f"/api/playlists/{elena_pl_id}/tracks",
                json={
                    "user_id": elena_id,
                    "urls_or_ids": "TNsSBhl_2LI",
                    "track_note": "Back-to-back repeat!",
                },
            ).json()
            assert repeat_drop["results"][0]["already_in_playlist"] is False
            assert repeat_drop["results"][0]["occurrences_in_playlist"] == 2

            pl_detail = client.get(f"/api/playlists/{elena_pl_id}?user_id={elena_id}").json()
            clipped_track = next(
                t for t in pl_detail["tracks"] if t["id"] == clip_drop["results"][0]["track_id"]
            )
            assert clipped_track["start_seconds"] == 75
            assert clipped_track["end_seconds"] == 245

            # Update start/end position using mm:ss strings
            upd = client.put(
                f"/api/playlists/{elena_pl_id}/tracks/{clipped_track['id']}",
                json={
                    "user_id": elena_id,
                    "start_time": "0:42",
                    "end_time": "3:30",
                    "track_note": "Updated cue points.",
                },
            )
            assert upd.status_code == 200
            upd_json = upd.json()
            assert upd_json["start_seconds"] == 42
            assert upd_json["end_seconds"] == 210

            # Invalid range (end <= start) should return 400
            bad_upd = client.put(
                f"/api/playlists/{elena_pl_id}/tracks/{clipped_track['id']}",
                json={
                    "user_id": elena_id,
                    "start_time": "3:30",
                    "end_time": "1:00",
                },
            )
            assert bad_upd.status_code == 400

            # 9. Test drag-and-drop track reorder and Ctrl+Z snapshot undo
            before_pl = client.get(f"/api/playlists/{elena_pl_id}?user_id={elena_id}").json()
            orig_track_ids = [t["id"] for t in before_pl["tracks"]]
            assert len(orig_track_ids) == 4

            reversed_ids = list(reversed(orig_track_ids))
            reorder_res = client.put(
                f"/api/playlists/{elena_pl_id}/reorder",
                json={"user_id": elena_id, "track_ids": reversed_ids},
            )
            assert reorder_res.status_code == 200
            after_reorder_pl = client.get(f"/api/playlists/{elena_pl_id}?user_id={elena_id}").json()
            assert [t["id"] for t in after_reorder_pl["tracks"]] == reversed_ids

            # Restore snapshot (Ctrl+Z undo)
            undo_res = client.put(
                f"/api/playlists/{elena_pl_id}/snapshot",
                json={
                    "user_id": elena_id,
                    "title": before_pl["title"],
                    "description": before_pl["description"],
                    "tracks": before_pl["tracks"],
                },
            )
            assert undo_res.status_code == 200
            restored_pl = client.get(f"/api/playlists/{elena_pl_id}?user_id={elena_id}").json()
            assert [t["id"] for t in restored_pl["tracks"]] == orig_track_ids

            # 10. Test YouTube Playlist Import & Migrate Song Wizard endpoints
            from app.youtube import clean_migration_query, extract_playlist_id

            assert extract_playlist_id("https://www.youtube.com/playlist?list=PL15B1E77BB5708555") == "PL15B1E77BB5708555"
            assert extract_playlist_id("PL15B1E77BB5708555") == "PL15B1E77BB5708555"
            assert "Rick Astley" in clean_migration_query(
                "Rick Astley - Never Gonna Give You Up (Official Music Video)",
                "RickAstleyVEVO",
            )

            inspect_res = client.post(
                "/api/playlists/import/inspect",
                json={
                    "source_input": "https://www.youtube.com/watch?v=0PsOJ_fRckg\nhttps://www.youtube.com/watch?v=TNsSBhl_2LI",
                    "max_tracks": 5,
                },
            )
            assert inspect_res.status_code == 200
            inspect_data = inspect_res.json()
            assert inspect_data["total_tracks"] == 2
            assert len(inspect_data["tracks"]) == 2
            assert inspect_data["tracks"][0]["video_id"] == "0PsOJ_fRckg"
            assert "suggested_query" in inspect_data["tracks"][0]

            commit_res = client.post(
                "/api/playlists/import/commit",
                json={
                    "user_id": elena_id,
                    "title": "Elena's Imported YT Crate",
                    "description": "Imported with Migrate Song Wizard",
                    "tracks": [
                        {
                            "video_id": "0PsOJ_fRckg",
                            "custom_title": "Bicep - Glue (Embed Version)",
                            "track_note": "Migrated to embeddable version",
                            "start_seconds": 15,
                            "end_seconds": 180,
                            "migrated_from_video_id": "Z5NoQg8LdDk",
                            "migrated_from_title": "Polyphia - Playing God (Official Music Video)",
                        },
                        {
                            "video_id": "TNsSBhl_2LI",
                            "custom_title": "Four Tet Live",
                        },
                    ],
                },
            )
            assert commit_res.status_code == 200
            imported_pl_id = commit_res.json()["playlist_id"]
            imported_pl = client.get(f"/api/playlists/{imported_pl_id}?user_id={elena_id}").json()
            assert imported_pl["title"] == "Elena's Imported YT Crate"
            assert len(imported_pl["tracks"]) == 2
            assert imported_pl["tracks"][0]["start_seconds"] == 15
            assert imported_pl["tracks"][0]["end_seconds"] == 180
            assert imported_pl["tracks"][0]["migrated_from_video_id"] == "Z5NoQg8LdDk"
            assert imported_pl["tracks"][0]["migrated_from_title"] == "Polyphia - Playing God (Official Music Video)"

            # Test audit-embeds on the imported playlist
            audit_res = client.get(f"/api/playlists/{imported_pl_id}/audit-embeds")
            assert audit_res.status_code == 200
            audit_data = audit_res.json()
            assert audit_data["total_tracks"] == 2
            assert len(audit_data["tracks"]) == 2

            # Test in-place song swap on a migrated track (PUT /api/playlists/{id}/tracks/{track_id} with new_video_id)
            first_track_entry_id = imported_pl["tracks"][0]["id"]
            migrate_res_1 = client.put(
                f"/api/playlists/{imported_pl_id}/tracks/{first_track_entry_id}",
                json={
                    "user_id": elena_id,
                    "new_video_id": "https://www.youtube.com/watch?v=q4xKvHANqjk",
                    "custom_title": "Poor Quality Migrated Version",
                },
            )
            assert migrate_res_1.status_code == 200
            assert migrate_res_1.json()["video_id"] == "q4xKvHANqjk"
            assert migrate_res_1.json()["migrated_from_video_id"] == "Z5NoQg8LdDk"
            assert "Polyphia" in (migrate_res_1.json()["migrated_from_title"] or "")

            # Swap the migrated track AGAIN to a better version and verify original provenance is preserved
            migrate_res = client.put(
                f"/api/playlists/{imported_pl_id}/tracks/{first_track_entry_id}",
                json={
                    "user_id": elena_id,
                    "new_video_id": "https://www.youtube.com/watch?v=M7lc1UVf-VE",
                    "custom_title": "Migrated Embeddable Demo Track",
                },
            )
            assert migrate_res.status_code == 200
            assert migrate_res.json()["video_id"] == "M7lc1UVf-VE"
            assert migrate_res.json()["migrated_from_video_id"] == "Z5NoQg8LdDk"
            after_migrate_pl = client.get(f"/api/playlists/{imported_pl_id}?user_id={elena_id}").json()
            assert after_migrate_pl["tracks"][0]["video_id"] == "M7lc1UVf-VE"
            assert after_migrate_pl["tracks"][0]["migrated_from_video_id"] == "Z5NoQg8LdDk"
            assert "Polyphia" in (after_migrate_pl["tracks"][0]["migrated_from_title"] or "")

            # 11. Test Song & Playlist Multi-Smack "Superlove", Enrichment, and Reset
            song_like_res = client.post(
                "/api/songs/M7lc1UVf-VE/like",
                json={"user_id": elena_id},
            )
            assert song_like_res.status_code == 200
            song_like_data = song_like_res.json()
            assert song_like_data["liked"] is True
            assert song_like_data["my_likes"] == 1
            assert song_like_data["like_count"] >= 1
            assert any(u["id"] == elena_id and u["count"] == 1 for u in song_like_data["liked_by_users"])

            # Smack love 9 more times (total 10 smacks) to trigger Superlove and unlock 'superlover' badge
            unlocked_superlover = False
            for i in range(2, 11):
                smack_res = client.post(
                    "/api/songs/M7lc1UVf-VE/like",
                    json={"user_id": elena_id},
                )
                assert smack_res.status_code == 200
                smack_data = smack_res.json()
                assert smack_data["my_likes"] == i
                assert smack_data["superloved"] is (i >= 5)
                if any(b["key"] == "superlover" for b in smack_data.get("new_badges", [])):
                    unlocked_superlover = True

            assert unlocked_superlover is True
            assert smack_data["like_count"] == song_like_data["like_count"] + 9

            # Verify playlist track reflects song_liked_by_me, song_my_likes, and song_like_count
            pl_with_like = client.get(f"/api/playlists/{imported_pl_id}?user_id={elena_id}").json()
            assert pl_with_like["tracks"][0]["song_liked_by_me"] is True
            assert pl_with_like["tracks"][0]["song_my_likes"] == 10
            assert pl_with_like["tracks"][0]["song_like_count"] == smack_data["like_count"]

            # Verify Song Page reflects like_count, my_likes, liked_by_me, and liked_by_users
            song_detail_liked = client.get(f"/api/songs/M7lc1UVf-VE?user_id={elena_id}").json()
            assert song_detail_liked["liked_by_me"] is True
            assert song_detail_liked["my_likes"] == 10
            assert song_detail_liked["like_count"] == smack_data["like_count"]

            # Multi-smack the playlist and verify user profile lists both liked_songs and liked_playlists
            pl_like_res = client.post(
                f"/api/playlists/{imported_pl_id}/like",
                json={"user_id": elena_id, "count": 5},
            )
            assert pl_like_res.status_code == 200
            assert pl_like_res.json()["liked"] is True
            assert pl_like_res.json()["superloved"] is True
            assert pl_like_res.json()["my_likes"] == 5
            assert pl_like_res.json()["like_count"] == 5

            profile_with_likes = client.get(f"/api/users/{elena_id}?viewer_id={elena_id}").json()
            assert any(
                s["video_id"] == "M7lc1UVf-VE" and s["profile_user_likes"] == 10
                for s in profile_with_likes["liked_songs"]
            )
            assert any(
                lp["id"] == imported_pl_id and lp["profile_user_likes"] == 5
                for lp in profile_with_likes["liked_playlists"]
            )

            # Reset song love (Shift+Click)
            song_reset_res = client.post(
                "/api/songs/M7lc1UVf-VE/like",
                json={"user_id": elena_id, "reset": True},
            )
            assert song_reset_res.status_code == 200
            assert song_reset_res.json()["liked"] is False
            assert song_reset_res.json()["my_likes"] == 0
            assert song_reset_res.json()["like_count"] == smack_data["like_count"] - 10






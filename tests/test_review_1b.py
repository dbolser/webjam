"""Regression tests for the Phase 1b review findings (string row ids, fast V-V-V, stale dock
versions, crate recovery, phone player, badges, discovery hops, theme flash)."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.badges import BADGE_CATALOG as BADGES

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "static" / "js"
CSS = ROOT / "static" / "styles"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")
def test_review_fixes_front_end_logic():
    res = subprocess.run(
        ["node", str(ROOT / "tests" / "js" / "review_1b_smoke.mjs"), str(JS)],
        capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "review 1b smoke ok" in res.stdout


# 1. Rows with string ids (search, discover, profile, track page) can be selected, copied, dragged.
@pytest.mark.parametrize("name", ["selection.js", "clipboard.js", "dnd.js", "index.js", "move-to.js"])
def test_editing_never_numbers_a_dom_row_id(name):
    src = _read(JS / "editing" / name)
    assert not re.search(r"Number\(\s*\w+\.dataset\.trackId\s*\)", src), name


# 2. Fast V-V-V: reconcile keeps pending temp rows; a lost anchor appends instead of going to the top.
def test_reconcile_keeps_pending_temps_and_paste_never_falls_to_the_top():
    store = _read(JS / "store.js")
    assert "keepPending(" in store and "pendingTemps()" in store
    assert "for (const id of p.order) if (id < 0) state.tracks.delete(id);" not in store
    clip = _read(JS / "editing" / "clipboard.js")
    assert "if (after === undefined) return {};" in clip


# 3 + 5. 409s adopt the server's versions; track_not_found refreshes; crate edits send versions.
def test_ops_conflicts_recover():
    api = _read(JS / "api.js")
    assert "adoptVersions(err.data && err.data.versions)" in api
    assert "err.code === 'track_not_found'" in api
    crate = _read(JS / "crate" / "crate-actions.js")
    assert crate.count("expectedVersions: () => expectedFor(") == 2
    core = _read(JS / "core.js")
    assert "expectedVersions: () => expectedFor([cid])" in core


# 4. Delete never sends another list's selection.
def test_remove_rows_uses_only_this_lists_selection():
    src = _read(JS / "editing" / "inline-edit.js")
    assert "const picked = rows || rowsHere(list);" in src
    assert "if (state.selection.listKey === key) return selectedRows();" in src


# 6. The floating video can be hidden (pausing, per YouTube's rules) and its height is reserved.
def test_video_tile_can_be_hidden_and_does_not_cover_content():
    player = _read(JS / "player" / "player.js")
    assert "setBody('video-collapsed', true)" in player
    assert 'data-action="player-video"' in player
    assert "pauseVideo()" in player.split("export function toggleVideo")[1].split("export function")[0]
    layout = _read(CSS / "layout.css")
    assert re.search(r"body\.has-player\.has-video:not\(\.video-collapsed\)\s*\{\s*padding-bottom:[^}]*200px", layout)
    assert re.search(r"body\.has-dock\.has-player\.has-video:not\(\.player-paused\):not\(\.video-collapsed\)\s*\{\s*padding-bottom:\s*calc\(48px \+ var\(--player-h\) \+ 200px\)", layout)


# 7. Phones keep like, crate, seam, repeat and video behind a ⋯ in the player bar.
def test_phone_player_has_an_overflow_menu():
    player = _read(JS / "player" / "player.js")
    assert 'data-action="player-more"' in player
    core = _read(JS / "core.js")
    more = core.split("'player-more':")[1].split("\n  },")[0]
    for needle in ("player.more", "playingToCrate", "previewSeam", "toggleRepeat", "toggleVideo"):
        assert needle in more, needle
    assert "slots.register('player.more'" in _read(JS / "discovery" / "likes.js")
    comp = _read(CSS / "components.css")
    assert re.search(r"\.pbar-more \{ display: inline-flex;", comp)


# 8. Badge glyphs are monochrome text, never colour emoji.
EMOJI_PRESENTATION_BMP = set("⌚⌛⏩⏪⏫⏬⏰⏳☔☕♈♉♊♋♌♍♎♏♐♑♒♓♿⚓⚡⚪⚫⚽⚾⛄⛅⛎⛔⛪⛲⛳⛵⛺⛽✅✊✋✨❌❎❓❔❕❗➕➖➗➰➿⬛⬜⭐⭕")
# Text by default but shown as emoji on Apple/Android: avoid them too.
EMOJI_PRONE = set("▶◀♥♠♣♦⚗☀☁✈✉✏✒✔✖❤☺☹♨♻⚠⚙⚛✳✴❇‼⁉™©®↔↕↖↗↘↙")


def test_badge_icons_are_text_glyphs():
    for b in BADGES:
        icon = b["icon"]
        assert len(icon) == 1, (b["key"], icon)
        cp = ord(icon)
        assert cp < 0x1F000, (b["key"], icon)
        assert icon not in EMOJI_PRESENTATION_BMP, (b["key"], icon)
        assert icon not in EMOJI_PRONE, (b["key"], icon)
    assert "font-variant-emoji: text" in _read(CSS / "discovery.css")


# 9 + 10. The playlist-count hop sits right after the channel; drafts show it from N = 1.
def test_playlist_count_hop_is_early_and_respects_visibility():
    src = _read(JS / "components" / "track-row.js")
    meta = src.split("function metaLine")[1]
    assert meta.index("in ${plural(n, 'playlist')}") < meta.index("via @${row.source_username}")
    assert "const min = context === 'playlist' ? 2 : 1;" not in src
    assert "countsItself(row, context) ? 2 : 1" in src
    assert "vis === 'public'" in src


# 11. The row menu previews the seam into and out of a row; Note and clip has its own icon.
def test_row_menu_has_seam_items():
    core = _read(JS / "core.js")
    assert "Preview the seam into this" in core and "Preview the seam out of this" in core
    assert "seamAt(ctx, 'into')" in core and "seamAt(ctx, 'out')" in core
    assert "label: 'Note and clip…', icon: 'seam'" not in _read(JS / "editing" / "index.js")
    assert 'id="i-note"' in _read(ROOT / "static" / "index.html")


# 12. Signed-out phones keep the theme control.
def test_theme_control_stays_for_signed_out_phones():
    assert "theme-anon" in _read(JS / "shell.js")
    assert '[data-action="theme-cycle"]:not(.theme-anon)' in _read(CSS / "components.css")


# 13. The track page heading counts what it lists.
def test_track_page_heading_counts_the_list():
    assert "In ${plural(playlists.length || count, 'playlist')}" in _read(JS / "views" / "track.js")


# 15 + 16. Badges are quiet for visitors and readable on touch.
def test_badges_are_quiet_and_tappable():
    src = _read(JS / "discovery" / "badges.js")
    assert "if (!earned.length && !owner) return nothing;" in src
    assert "const locked = owner ?" in src
    assert 'data-action="badge-info"' in src
    assert "badgesSection(d, { owner: isOwner(u) })" in _read(JS / "views" / "profile.js")


# 17. The related panel refreshes after its playlist's track set changes.
def test_related_panel_refreshes_after_edits():
    src = _read(JS / "discovery" / "related.js")
    assert "onChange((reasons)" in src and "reasons.has('reconcile')" in src
    assert "resetRelated(id);" in src


# 18. A saved theme is applied before first paint.
def test_theme_boot_runs_before_first_paint():
    html = _read(ROOT / "static" / "index.html")
    boot = html.index('<script src="/static/js/theme-boot.js"></script>')
    assert boot < html.index('<link rel="stylesheet"')
    src = _read(JS / "theme-boot.js")
    assert "webjam.theme" in src and "data-theme" in src
    assert "KEY = 'webjam.theme'" in _read(JS / "theme.js")

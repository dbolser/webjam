"""Phone helpers for the player (docs/phase1b-shell.md §12): keep the screen awake, lock-screen
controls, resume where you left off. The pure logic is unit-tested in tests/js/player_helpers.mjs;
the checks here pin the wiring in player.js and the CSP the artwork needs."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "static" / "js"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")
def test_player_helper_logic():
    res = subprocess.run(
        ["node", str(ROOT / "tests" / "js" / "player_helpers.mjs"), str(JS)],
        capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "player helpers ok" in res.stdout


def test_player_uses_the_helpers():
    player = _read(JS / "player" / "player.js")
    assert "createWakeLock(" in player
    assert "wake.setPlaying(true)" in player and "wake.setPlaying(false)" in player
    assert "addEventListener('visibilitychange'" in player
    assert "addEventListener('pagehide'" in player
    assert "ms.wireActions(" in player
    assert "readResume(" in player and "writeResume(" in player
    # Resuming never autoplays on load: the offer is rendered, and only a tap calls resumeSaved().
    offer = player.split("function renderOffer")[1].split("\nfunction ")[0]
    assert "playQueue(" not in offer and "loadCurrent(" not in offer
    core = _read(JS / "core.js")
    for action in ("player-keep-awake", "player-resume", "player-resume-dismiss"):
        assert f"'{action}':" in core, action


def test_resume_key_is_per_user_and_storage_is_guarded():
    src = _read(JS / "player" / "resume.js")
    assert "webjam.resume.u${id}" in src and "'webjam.resume.anon'" in src
    # Every storage call in the helpers sits inside a try.
    for name in ("resume.js", "wake-lock.js"):
        code = _read(JS / "player" / name)
        for m in re.finditer(r"storage\.(?:getItem|setItem|removeItem)\(", code):
            line = code[code.rfind("\n", 0, m.start()) + 1: code.find("\n", m.start())]
            before = code[max(0, m.start() - 200): m.start()]
            assert "try" in line or "try {" in before, f"{name}: {line.strip()}"


def test_csp_allows_youtube_artwork():
    main = _read(ROOT / "app" / "main.py")
    assert re.search(r"img-src[^\"]*https://i\.ytimg\.com", main)


def _fn(src: str, name: str) -> str:
    return src.split(f"function {name}(")[1].split("\nfunction ")[0].split("\nexport ")[0]


def test_player_review_fixes_are_wired():
    player = _read(JS / "player" / "player.js")
    # Resume saves only for whoever started the queue, never for a dead video, and move on in the tail.
    save = _fn(player, "saveResume")
    assert "queueOwner" in save and "state.dead.has(" in save and "planSave(" in save
    assert "clearResume(" in save
    assert "claimQueue()" in _fn(player, "playQueue") and "claimQueue()" in _fn(player, "previewSeam")
    # The 5 s cadence holds during a seam preview (saveResume returns early there).
    tick = _fn(player, "tick")
    assert "lastSaved = now" in tick.split("saveResume()")[0]
    # Lock-screen play/pause work while a track is still loading.
    assert "yt.playVideo()" in _fn(player, "play")
    assert "'loading'" in _fn(player, "pause")
    # A dead video leaves the lock screen paused.
    assert "setPlaybackState(nav, 'paused')" in _fn(player, "onError")
    # Fresh time before the lock-screen position and the "Resume at" offer.
    assert "ytTime()" in _fn(player, "onState") and "awayAt = ytTime()" in _fn(player, "onVisibility")
    # A slow playlist load never overwrites a queue the person started meanwhile.
    assert "token !== loadToken" in _fn(player, "resumeSaved")
    # One Resume button for keyboards and screen readers; the icon is decorative.
    offer = _fn(player, "renderOffer")
    assert offer.count('tabindex="-1" aria-hidden="true"') == 1

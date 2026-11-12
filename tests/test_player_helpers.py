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

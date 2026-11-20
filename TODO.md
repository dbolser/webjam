# Webjam to-do

## Next

- Invite Dan's friend: they sign in, press Request access, and Dan approves at
  /admin. While the OAuth consent screen is in Testing, they must also be added
  there as a test user, or Google blocks the sign-in before webjam sees it.
- Pick a colourful design direction and build it. Close #8 if Dan agrees.

## Bugs

- The last track of a queue that has an end time finishes as "paused" instead
  of "ended": YouTube pauses itself at `endSeconds`, the 250 ms tick stops
  before it sees the end, so `handleEnd()` never runs. Fix: treat a pause
  within 0.5 s of `item.end` as the end (static/js/player/player.js, the
  `YT_PAUSED` branch).
- Avatar initials for a display name like "Dan (You)" come out as "D(".

## Chores

- Plain `pytest` fails to import `app`. Add `pythonpath = ["."]` under
  `[tool.pytest.ini_options]` in pyproject.toml.
- docs/phase1b-shell.md still describes the vendored Instrument and JetBrains
  fonts (gone once #8 merges).
- Gemini Carbon reviews fail: the `iuk-pipelines` service account needs
  `roles/aiplatform.user` on project `gemini-github-pr-agent`.
- On phones, track rows show three icon buttons, so titles wrap early.
  Consider keeping only "More" at narrow widths.

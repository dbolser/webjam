# Webjam to-do

## Next

- Google sign-in on the Hetzner server. Needs a Google OAuth client from Dan
  (redirect URI `https://46-62-228-124.sslip.io/auth/google/callback`), then:
  deploy current main, set `WEBJAM_AUTH_MODE=google` plus the client ID,
  secret, `WEBJAM_SESSION_SECRET` and `WEBJAM_BASE_URL` in a root-only env
  file, drop the Caddy password, invite Dan and friends with
  `WEBJAM_INVITE_EMAILS`. The server database is pre-Phase-1, so it must be
  backed up and reseeded.
- Decide on the Webjay restyle (#8) and merge it.

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

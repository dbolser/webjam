# Webjam to-do

## Next

- Invite Dan's friend: they sign in, press Request access, and Dan approves at
  /admin. While the OAuth consent screen is in Testing, they must also be added
  there as a test user, or Google blocks the sign-in before webjam sees it.
- Close the obsolete Webjay restyle PR (#8) if Dan agrees.

## Bugs

- The last track of a queue that has an end time finishes as "paused" instead
  of "ended": YouTube pauses itself at `endSeconds`, the 250 ms tick stops
  before it sees the end, so `handleEnd()` never runs. Fix: treat a pause
  within 0.5 s of `item.end` as the end (static/js/player/player.js, the
  `YT_PAUSED` branch).

## Chores

- Plain `pytest` fails to import `app`. Add `pythonpath = ["."]` under
  `[tool.pytest.ini_options]` in pyproject.toml.
- Gemini Carbon reviews fail: the `iuk-pipelines` service account needs
  `roles/aiplatform.user` on project `gemini-github-pr-agent`.
- Track titles on desktop stop short because the hidden hover actions keep
  their columns (Sunny pop QA nit, declined as a bigger change).

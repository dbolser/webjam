# Webjam status

Last updated 2026-11-20.

## What it is

A playlist site for friends, built from YouTube embeds, in tribute to Webjay
(2003-2006). FastAPI and SQLite on the back end; no-build ES modules with
lit-html on the front end.

## Where things stand

- **Merged:** Google sign-in (#3), security hardening (#4), the op log with
  server undo, crate, dock and paste resolver (#5), and the playlist-first
  front end with discovery (#6), and phone playback (#7): keep the screen
  awake, lock-screen controls, resume where you left off.
- **In review:** a Webjay-style restyle (#8): white page, Verdana, underlined
  blue links, zebra lists, 16px text, system fonts only. Waiting on Dan's
  verdict on the look.

## Running

Locally, in demo mode (anyone can act as anyone, loopback only):

```bash
WEBJAM_AUTH_MODE=demo uv run uvicorn app.main:app --port 8080
```

Tests: `uv run python -m pytest -q` (plain `pytest` fails to import `app`; see
TODO).

## Hosting

- Hetzner Cloud server "webjam" (cpx02, Helsinki), `ssh root@46.62.228.124`.
- Served at https://46-62-228-124.sslip.io by Caddy (automatic Let's Encrypt
  certificate), which proxies to uvicorn on 127.0.0.1:8080. The systemd unit
  is `webjam`.
- The app lives in `/opt/webjam/app` (a copied tree, not a git checkout). The
  database is `/var/lib/webjam/webjam.db`.
- Today it runs the pre-#6 code in demo mode behind a Caddy password. The next
  step is Google sign-in mode (needs an OAuth client) so friends can sign in.
- The Hetzner Cloud API token that was saved on chaz on 2026-11-10 is gone.
  Changing the server, firewall or DNS through Hetzner needs a new token. SSH
  still works from chaz and lenny.

## Database

Phase 1 changed the schema. The app refuses to open an older database and
asks for it to be deleted, so it can reseed. Back the old file up first.
The pre-Phase-1 database from lenny is kept at
`data/backups/webjam-pre-phase1-2026-11-06.db` (git-ignored).

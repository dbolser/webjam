# Webjam — Handmade YouTube Playlists & Track Lineage

A tribute to the classic UI/UX of **Webjay** (c. 2003–2006), rebuilt with **YouTube IFrame embeds** as the streaming backend and a lightweight local **SQLite + FastAPI** metadata store.

## Core Features

1. **Drop YouTube Links to Curate Playlists**:
   - Paste any `youtube.com/watch?v=...` or `youtu.be/...` link(s).
   - Automatically fetches track title and channel via YouTube's public oEmbed API.
   - Only playlist metadata, liner notes, and track provenance are stored locally in SQLite (`data/webjam.db`); YouTube handles all video/audio streaming and native ads unobstructed.
2. **Song Pages & First-Finder Provenance**:
   - Every song added to Webjam gets a dedicated Song Page (`#song/<video_id>`) showing **who discovered it first on Webjam**, which playlist it debuted in, every other playlist that includes it, and how it spread across curators.
3. **One-Click Track Grabbing (`+ Grab`) & Lineage**:
   - Easily grab songs from other users' playlists into your own playlists with optional per-track liner notes.
   - Tracks display `↳ via @curator` provenance pills linking back to the source playlist.
4. **Playlist Liner Notes & Comments**:
   - Add descriptions/liner notes to playlists and individual tracks.
   - Comment on playlists, song pages, and curator profile guestbooks.
5. **16 Provenance & Curation Badges**:
   - **Discovery**: *Crate Digger* (first to add 1 song), *A&R Scout* (5 songs), *John Peel Award* (12 songs), *Golden Ear* (discover a song first that 2+ other users later add).
   - **Tastemaker**: *The Spark* (someone grabs a track from your playlist), *Trendsetter* (5+ grabs), *Source Material* (3+ distinct curators grab from you).
   - **Curation & Lineage**: *Hat Tip* (grab a track from someone), *Cross-Pollinator* (grab 3+ tracks), *Crate Alchemist* (take tracks from X and reach 5+ plays on your playlist), *Selector's Gold* (take tracks from 2+ curators into one playlist and reach 10+ plays).
   - **Reception & Community**: *Co-Signed*, *Crowd Pleaser*, *Heavy Rotation*, *Liner Note Poet*, *Record Shop Regular*.

## Running Locally

```bash
cd /home/dan/Build/webjam
uv run uvicorn app.main:app --reload --port 8080
```

Then open **http://localhost:8080** in your browser.

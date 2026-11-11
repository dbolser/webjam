# Phase 1a backend contract: ops, undo, crate, dock, paste

Status: **contract for implementation**. Branch `t3/phase1-ops-crate`, stacked on `t3/security-hardening`.
Scope: backend only, plus the smallest frontend changes needed to keep the current UI working (§8).
The visual design is not chosen yet, so nothing here prescribes UI beyond what the API implies.

Ground rules carried over from Phase 0 and still binding:

- Every write resolves its user with `resolve_actor(request, claimed_user_id)` (session in google mode, the `user_id` claim in demo mode). Every read uses `resolve_viewer`.
- Every unsafe method needs the `X-Webjam: 1` header (CSRF middleware) and fits in the 256 KB body cap.
- Provenance (`added_by_user_id`, `source_*`, `created_at`, `migrated_from_*` when it comes from a row) is **always derived on the server**. Nothing in a client body can set it.
- Owner decisions: invite-only friends site; the DB may be wiped and reseeded (no data migrations); badges stay as they are for now.

Conventions in this document:

- `LISTED(p)`: `p.kind = 'playlist' AND p.visibility = 'public'`. Appears in feeds, lists, counts, badges and discovery.
- `VISIBLE(p, viewer)`: `(p.kind = 'playlist' AND p.visibility IN ('public','unlisted')) OR p.user_id = viewer`. Can be opened by id.
- `LINKABLE(p, viewer)`: `LISTED(p) OR p.user_id = viewer`. May be named or linked from *another* page (a "via" credit, a "debut playlist" title, "also in").
- "Not visible" is always reported as **404**, never 403, so private playlists can't be probed.
- Error bodies keep `detail` as a **string** (the current `api()` helper in `static/app.js` prints `detail`), and add a machine-readable top-level `code` plus extra fields. New endpoints raise an `ApiError(status, code, detail, **extra)` with its own exception handler instead of `HTTPException(detail=dict)`.

---

## 1. Data model

The DB is recreated from `SCHEMA_SQL`. `init_db()` must work on an empty file and on a fresh test DB.

### 1.1 Schema version guard

```sql
-- meta(key, value) already exists
INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', '3');
```

Version 3 (Phase 1a review fixes): `playlist_tracks.source_track_id` lost its FK, `playlist_ops.kind` allows `'reorder'`, and `user_badges` gained `op_group_key`. A version-2 file is refused like any older one: wipe and reseed.

`init_db()`: if `users` has rows but `meta.schema_version` is missing or `< 3`, raise
`RuntimeError("Webjam Phase 1 changed the schema. Delete the database file (WEBJAM_DB_PATH) and restart; it will be reseeded.")`.
No ALTER-based migration is written for Phase 1. `ensure_playlist_track_clip_columns` and `migrate_legacy_seed_videos` are only for pre-Phase 1 DBs, so they become dead code and can be deleted. (`ensure_initial_song_likes` is still called by the seed.)

### 1.2 `playlists`

```sql
CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'playlist' CHECK (kind IN ('playlist', 'crate')),
    visibility TEXT NOT NULL DEFAULT 'public' CHECK (visibility IN ('public', 'unlisted', 'private')),
    version INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    plays INTEGER DEFAULT 0,
    forked_from_playlist_id INTEGER REFERENCES playlists(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (kind = 'playlist' OR visibility = 'private')      -- a crate is always private
);
CREATE UNIQUE INDEX IF NOT EXISTS one_crate_per_user ON playlists(user_id) WHERE kind = 'crate';
CREATE INDEX IF NOT EXISTS idx_playlists_listed ON playlists(kind, visibility, updated_at);
CREATE INDEX IF NOT EXISTS idx_playlists_owner ON playlists(user_id, updated_at);
```

- `version` starts at 0 and goes up by exactly 1 per committed op batch, undo or redo that touches the playlist's tracks or meta. Likes, plays and comments don't change it.
- `kind` changes only through promote (§3.5). That doesn't flip the kind: it creates a new playlist.
- Crate title is `"Crate"`. Its title and description can be edited with `set_meta`, but not its visibility.
- `ensure_crate(conn, user_id) -> int` runs `INSERT OR IGNORE INTO playlists (user_id, kind, visibility, title, ...) VALUES (?, 'crate', 'private', 'Crate', ...)` and returns the id. The partial unique index makes it idempotent. It is called by the seed, by demo registration (`login_or_register`), by `upsert_google_user` in `auth.py` for a new user, and lazily by `GET /api/me/crate`, `GET /api/me/dock` and `GET /api/bootstrap`.

### 1.3 `playlist_tracks`

```sql
CREATE TABLE IF NOT EXISTS playlist_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    video_id TEXT NOT NULL REFERENCES songs(video_id),
    position REAL NOT NULL,                       -- sort key, fractional; NOT the 1-based index
    added_by_user_id INTEGER NOT NULL REFERENCES users(id),
    track_note TEXT DEFAULT '',
    start_seconds REAL DEFAULT NULL,              -- REAL now; API keeps whole seconds until Phase 2
    end_seconds REAL DEFAULT NULL,
    migrated_from_video_id TEXT DEFAULT NULL,
    migrated_from_title TEXT DEFAULT NULL,
    display_title TEXT DEFAULT NULL,
    source_playlist_id INTEGER REFERENCES playlists(id) ON DELETE SET NULL,
    source_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    source_track_id INTEGER,                      -- no FK on purpose, see below
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pt_order ON playlist_tracks(playlist_id, position, id);
CREATE INDEX IF NOT EXISTS idx_pt_video ON playlist_tracks(video_id);
CREATE INDEX IF NOT EXISTS idx_pt_source_user ON playlist_tracks(source_user_id);
CREATE INDEX IF NOT EXISTS idx_pt_source_playlist ON playlist_tracks(source_playlist_id);
CREATE INDEX IF NOT EXISTS idx_pt_source_track ON playlist_tracks(source_track_id);
```

**`source_track_id` has no FK.** With `ON DELETE SET NULL`, every `delete` op and every cross-playlist `move` silently nulled the links of other users' rows (grabs, crates), and undo could not put them back. Ids are `AUTOINCREMENT` and never reused, so a dangling id can't point at the wrong row: readers show `source_track_id` only while that row still exists **in** the (LINKABLE) `source_playlist_id`. Delete-then-undo and move-then-undo give back the exact provenance state, because `restore` brings the row back under its original id.

**Ordering** is always `ORDER BY position ASC, id ASC`. There is no unique constraint on position.

**API position**: every response that exposes `position` gives the **1-based ordinal** (`ROW_NUMBER() OVER (PARTITION BY playlist_id ORDER BY position, id)`), because the old UI uses it as an index (`app.js:1527`). The raw REAL is never exposed.

**Fractional insert.** To place `n` rows between neighbours with keys `a` (previous) and `b` (next):

| Situation | Keys |
|---|---|
| between `a` and `b` | `a + (b - a) * i / (n + 1)` for `i = 1..n` |
| at the end (no next) | `a + i` (an empty list uses `a = 0`) |
| at the start (no previous) | `b - (n + 1) + i` |

**Renormalisation.** Inside the same transaction, before computing keys, apply_ops renormalises the playlist to `1.0, 2.0, … N` (in current order) when either:
- the gap `(b - a) / (n + 1) < 1e-6`, or
- any `|position| > 1e9`.

Renormalising never changes the order. It is not an op, is not logged and doesn't bump `version` on its own. It is safe because ops and inverses always refer to **track ids (anchors)**, never to raw keys.

### 1.4 `songs`: discovery credit becomes nullable

```sql
CREATE TABLE IF NOT EXISTS songs (
    video_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    channel_title TEXT NOT NULL,
    thumbnail_url TEXT NOT NULL,
    first_added_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,   -- NULL until first LISTED placement
    first_added_playlist_id INTEGER REFERENCES playlists(id) ON DELETE SET NULL,
    first_added_at TEXT,
    total_plays INTEGER DEFAULT 0,
    created_at TEXT NOT NULL                       -- when Webjam cached the metadata
);
```

A `songs` row is created the first time a video is placed anywhere (the FK needs it), crate included. **Discovery credit ("first found by") is only claimed by a LISTED placement.** See `claim_discovery` in §2.8.
Every query that did `JOIN users fu ON fu.id = s.first_added_by_user_id` must become a `LEFT JOIN` (see §4). Otherwise crate-only songs vanish from the owner's own crate view.

### 1.5 `playlist_ops`

```sql
CREATE TABLE IF NOT EXISTS playlist_ops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,           -- global order; the undo stack is ordered by this
    op_id TEXT NOT NULL UNIQUE,                     -- "<client op_id>#<n>", n = 0.. within the batch
    batch_id TEXT NOT NULL,                         -- the client's op_id (idempotency key)
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    group_id TEXT NOT NULL,                         -- undo unit; defaults to batch_id
    kind TEXT NOT NULL CHECK (kind IN ('insert', 'delete', 'move', 'update', 'set_meta', 'reorder')),
    forward_json TEXT NOT NULL,                     -- the op AS APPLIED (normalised, with created ids), plus "label"
    inverse_json TEXT NOT NULL,                     -- server-generated, may use internal kinds (see 2.6)
    request_hash TEXT NOT NULL,                     -- sha256 of canonical JSON {group_id, ops, expected_versions}
    created_at TEXT NOT NULL,
    undone_at TEXT,                                 -- set while undone; cleared by redo
    discarded_at TEXT                               -- no longer undoable/redoable (redo branch dropped, playlist deleted, pruned)
);
CREATE INDEX IF NOT EXISTS idx_ops_user ON playlist_ops(user_id, id);
CREATE INDEX IF NOT EXISTS idx_ops_group ON playlist_ops(user_id, group_id);
CREATE INDEX IF NOT EXISTS idx_ops_batch ON playlist_ops(user_id, batch_id);
CREATE INDEX IF NOT EXISTS idx_ops_playlist ON playlist_ops(playlist_id, id);
```

- One row per **per-playlist effect**. A cross-playlist move writes two rows in the same batch and group: a `delete` on the source and an `insert` on the destination, both with `forward_json.via = "move"`.
- `op_id`, `batch_id` and `group_id` are 1–64 characters of `[A-Za-z0-9_-]`. Clients should use ULIDs or UUIDv4. Uniqueness is global, so a different user can't replay or squat someone else's op_id (§3.1).
- The prefixes `srv-`, `fork-` and `promote-` (any case) are **reserved** for ids the server mints. A client `op_id` or `group_id` that starts with one is `400 bad_op`. The server never infers behaviour from an id: fork and promote pass an explicit `origin` to `apply_ops`, stored as `forward_json.origin` (`"fork"` suppresses the grab line, `"promote"` enables the `playlist_left_empty` undo note).
- `forward_json.label` is the label of the **whole group** (every row of a group carries the same one, recomputed when a later batch joins the group); `forward_json.effect_label` is the row's own.
- Retention: after each batch, if the user has more than 500 groups, set `discarded_at` on the oldest groups' rows and delete rows older than 90 days that are discarded. Discarded groups can't be undone.

### 1.6 `user_dock`

```sql
CREATE TABLE IF NOT EXISTS user_dock (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
    sort REAL,                                       -- pinned order; NULL when unpinned
    last_used_at TEXT,                               -- last write by this user to this playlist
    PRIMARY KEY (user_id, playlist_id)
);
CREATE INDEX IF NOT EXISTS idx_dock_recent ON user_dock(user_id, last_used_at);
```

The crate never needs a dock row (it is always slot 0), but it gets one through the generic upsert, and that does no harm. Unpinned rows beyond the user's 20 most recent are deleted after each batch.

### 1.7 `activity_log`

Add `op_group_id TEXT NULL` and `CREATE INDEX idx_activity_group ON activity_log(actor_user_id, op_group_id)`. apply_ops tags its activity lines so that undo can remove them (§3.2).

### 1.8 Seed changes (`seed_initial_data`)

- Seeded playlists 1–4 get `kind='playlist'`, `visibility='public'` and `version=0`. Track positions are written as `1.0, 2.0, …`.
- Each seeded user (1–4) gets a crate through `ensure_crate`. Crates get ids 5–8, because they are inserted after playlists 1–4.
- Dan's crate (user 1) gets two seeded rows so the feature can be demoed:
  - `c_56qQqxvm4` copied from Maya's playlist 2. Provenance is `source_playlist_id=2`, `source_user_id=3`, and `source_track_id` = Maya's row id for that video.
  - `6647smeduGI` copied from Julian's playlist 3, with the same kind of provenance.
  These rows must **not** appear in any count, feed or badge. They are the canary for the privacy tests (§9).
- `songs` seed rows keep their `first_added_*` values, because all seeded first placements are in public playlists. Add `created_at` = `first_added_at`.
- `user_dock` seed: Dan pins playlist 4.
- Badge evaluation at the end of the seed is unchanged. It must give the same badges as today, which proves the crate rows are filtered out.

---

## 2. `apply_ops`: the only write path for playlist tracks and meta

```python
@dataclass(frozen=True)
class Actor:
    user_id: int
    # Phase 5: memberships are looked up per playlist; nothing else changes here.

def apply_ops(
    conn: sqlite3.Connection,
    actor: Actor,
    ops: list[Op],                      # parsed and validated Pydantic models (2.2)
    *,
    batch_id: str,                      # client op_id, or a server-generated "srv-<ulid>" for legacy endpoints
    group_id: str | None = None,        # defaults to batch_id
    expected_versions: dict[int, int] | None = None,
    song_meta: dict[str, dict] | None = None,   # pre-fetched YouTube metadata for unknown video_ids
    skip_unavailable: bool = False,     # legacy add/import behaviour: drop items whose video can't be looked up
    max_items: int = 50,                # client batches; server-composed batches (promote, fork, import) pass 1000
    undoable: bool = True,              # fork passes False (rows are written already discarded)
) -> OpsOutcome
```

The brief described this as `expected_version=None`. It is a **per-playlist mapping** here, because one batch can touch several playlists (a cross-move, or a paste into three).

`apply_ops` is synchronous. YouTube lookups for video ids that have no `songs` row happen **before** it is called, in the async endpoint (`await prefetch_song_meta(conn, video_ids)`), so no network call ever runs while the SQLite write lock is held. If an item's video has neither a `songs` row nor an entry in `song_meta`:
- with `skip_unavailable=False`, the whole batch fails (`400 video_not_found` or `503 youtube_unavailable`);
- with `skip_unavailable=True`, that item is dropped and reported in `skipped`.

```python
@dataclass
class OpsOutcome:
    group_id: str
    replayed: bool
    results: list[dict]                 # one per request op, same order (2.3)
    versions: dict[int, int]            # every touched playlist -> new version
    order: dict[int, list[int]]         # every touched playlist -> full ordered track id list
    rows: dict[int, dict]               # track_id -> rendered row (same shape as GET playlist tracks) for inserted/updated rows
    skipped: list[dict]                 # [{op_index, item_index, video_id, reason}]
    new_badges: list[dict]              # the actor's newly earned badges
```

### 2.1 Pipeline (one call = one transaction)

1. **Validate the shape** (Pydantic plus `check_batch_shape`, before any DB work; `POST /api/ops` also runs it and the read-only role check of step 4 **before** any YouTube lookup):
   - op kinds and fields;
   - at most 20 ops;
   - at most `max_items` items in total (insert items + delete ids + move ids + 1 per update or set_meta);
   - at most 10 distinct playlists touched.
2. **Idempotency.** `SELECT … FROM playlist_ops WHERE batch_id = ? ORDER BY id`:
   - The rows exist, belong to `actor`, and have the same `request_hash`: return the stored outcome, rebuilt from `forward_json`, with `replayed=True`, current versions and `new_badges=[]`.
   - The rows exist but belong to another user, or the hash differs: `409 op_id_conflict`.
3. `BEGIN IMMEDIATE`.
4. **Load and authorise** every playlist the ops touch, using `require_playlist_role(conn, playlist_id, actor, OP_MIN_ROLE[kind])` (2.4), and the tracks they reference. A track id that isn't in the stated playlist gives `404 track_not_found` with `op_index`.
5. **Version check.** For each `(pid, v)` in `expected_versions` where `playlists.version != v`, roll back with `409 stale_version`. No op from the batch is applied and the op_id is not consumed.
6. **Group check.** If `group_id` is given and already exists for this user, it must be the user's **latest live group**, otherwise `409 group_closed`. Groups must stay contiguous so that undo can pop them.
7. **Apply ops in order.** Each op sees the state left by the previous op in the same batch. For each op:
   - compute the effect;
   - derive provenance (2.5);
   - compute the inverse (2.6);
   - check the cap: after the op, the playlist has at most 1000 tracks, otherwise `400 playlist_full`.
8. **Write `playlist_ops` rows.** `op_id = f"{batch_id}#{n}"`, with `forward_json` holding the applied form (created ids, resolved anchors) and a server-made `label` such as `"Added 1 track to 'Basement Velvet'"`. The label describes everything one undo of the group reverts: one effect keeps its own label; effects of one kind are summed (`"Added 3 tracks to 3 playlists"`, `"Swapped 2 tracks in 'X'"`; the source half of a cross-playlist move doesn't count); mixed kinds give `"Changed 'X'"` or `"Changed N playlists"`. When the batch joins an existing group, the label is recomputed over the whole group and written to its earlier rows too.
9. **Bump** `version = version + 1` and `updated_at = now` on every touched playlist.
10. **Discard the redo branch.** If this batch is a new user action (not a redo), set `discarded_at = now` on this user's rows where `undone_at IS NOT NULL AND discarded_at IS NULL`.
11. **Discovery claims and activity**, for LISTED targets only (2.8). Activity lines are tagged `op_group_id`.
12. **Dock.** Upsert `user_dock(actor, pid, last_used_at=now)` for every touched playlist the actor owns, then prune.
13. `COMMIT`.
14. **Badges**, after the commit, only if at least one touched playlist is LISTED after the batch. Evaluate the actor, every `source_user_id` credited by rows placed in LISTED playlists, and the `first_added_by_user_id` of inserted videos. Return only the actor's new badges. For an undoable batch, every badge it awards (to anyone) is tagged `user_badges.op_group_key = "<actor id>:<group_id>"`, and so is its `earned_badge` activity line (`activity_log.op_group_id`); see 2.8.

### 2.2 Op kinds (request form)

Common fields: `kind` and `playlist_id`. Clip times accept what `parse_timestamp_seconds` accepts. They must be between 0 and 86400, and `end_seconds > start_seconds` when both are set (`400 bad_clip`).

**Placement**, used by `insert` and `move`:
- `after_track_id: int`: place right after this track, which must be in the target playlist and must not be one of the tracks being moved.
- or `position: int >= 1`: the **first** placed track becomes the N-th track of the resulting list, clamped to `[1, len + 1]`.
- If neither is given, append at the end. Giving both is `400 bad_placement`.
- A missing anchor (deleted in another tab) is `409 anchor_missing`, so the client refetches.

#### `insert`

```json
{
  "kind": "insert",
  "playlist_id": 3,
  "after_track_id": 42,
  "items": [
    {
      "video_id": "c_56qQqxvm4",
      "start_seconds": 312,
      "end_seconds": 498,
      "note": "the build at 5:10",
      "display_title": null,
      "ref": { "track_id": 345 }
    }
  ]
}
```

Item fields:
- `video_id` is required (11 characters).
- `start_seconds` and `end_seconds` are optional.
- `note` (at most 5000 characters) is stored as `track_note`.
- `display_title` (at most 200 characters) is cleaned with `clean_display_title`.
- `ref` is optional: `{track_id}` (preferred) or `{playlist_id}`.

Items are placed contiguously, in the order given. Duplicates are allowed and kept: V-V-V means three rows.

#### `delete`

```json
{ "kind": "delete", "playlist_id": 3, "track_ids": [42, 43] }
```

The ids are deleted in list order. That order matters for the inverse (2.6).

#### `move`

```json
{ "kind": "move", "playlist_id": 8, "track_ids": [901, 902], "to_playlist_id": 3, "position": 4 }
```

- `to_playlist_id` defaults to `playlist_id`, which gives a move within the playlist.
- The moved block keeps its **current relative order in the source**, whatever order the ids are given in, so a multi-select drag is predictable.
- **Within a playlist**, the rows keep their ids and only `position` changes. That is one `move` row in `playlist_ops`.
- **Across playlists**, the result is a `delete` on the source and an `insert` on the destination, in the same batch and group. The new rows get new ids, and the result maps them (`moved: [{from_track_id, track_id}]`).
  - The inserted rows carry over the source rows' `track_note`, clip, `display_title`, `migrated_from_*`, `source_*` and `added_by_user_id`. A move is never a new grab: the actor owns both ends, so "copy within your own keeps provenance" applies.
  - `created_at` is set to now, so the destination shows the track as just added.

#### `update`

```json
{ "kind": "update", "playlist_id": 3, "track_id": 42,
  "set": { "note": "new liner note", "start_seconds": 12, "end_seconds": null, "display_title": "", "video_id": "TNsSBhl_2LI" } }
```

- Only the keys present in `set` change.
- `null` clears a clip bound, and `""` clears `display_title`.
- `video_id` is the existing **swap** (migrate to another version). It reuses the current swap rules from `update_playlist_track`:
  - `migrated_from_*` is set or reverted on the server;
  - grab credit survives only if `verified_source` confirms the source playlist has the new video **and** that playlist is `VISIBLE` to the actor.
- The swap's metadata lookup goes through `prefetch_song_meta` like any insert.

#### `set_meta`

```json
{ "kind": "set_meta", "playlist_id": 3, "set": { "title": "Basement Velvet II", "description": "…", "visibility": "unlisted" } }
```

- `title` is 1–200 characters after trim, and `description` at most 5000.
- `visibility` on a crate is `400 crate_visibility_fixed`. `kind` is never settable.
- Going from not-LISTED to LISTED (for example private → public) runs `claim_discovery` for all the playlist's videos and logs `published_playlist`.
- Going from LISTED to not-LISTED logs nothing and revokes nothing (badges are never revoked). The read-time filters hide the playlist from then on.

### 2.3 Results (per op, in request order)

```json
[
  { "kind": "insert", "playlist_id": 3, "track_ids": [1201], "skipped": [] },
  { "kind": "delete", "playlist_id": 3, "track_ids": [42, 43] },
  { "kind": "move", "playlist_id": 8, "to_playlist_id": 3,
    "moved": [ { "from_track_id": 901, "track_id": 1202 }, { "from_track_id": 902, "track_id": 1203 } ] },
  { "kind": "update", "playlist_id": 3, "track_id": 1201, "changed": ["note", "start_seconds"] },
  { "kind": "set_meta", "playlist_id": 3, "changed": ["visibility"] }
]
```

Each inserted row also reports how its provenance came out, in `rows[track_id].provenance`. `"credited"` means it was credited to the ref's owner, `"kept"` means it was copied from your own row, `"none"` means it had no ref, and `"dropped"` means the ref was invalid or not visible.

### 2.4 Role check, shaped for later roles

```python
ROLE_RANK = {"viewer": 1, "contributor": 2, "editor": 3, "owner": 4}
OP_MIN_ROLE = {"insert": "editor", "delete": "editor", "move": "editor", "update": "editor", "set_meta": "owner"}

def playlist_role(conn, pl: sqlite3.Row, user_id: int) -> str | None:
    if pl["user_id"] == user_id:
        return "owner"
    # Phase 5: SELECT role FROM playlist_members WHERE playlist_id = ? AND user_id = ?
    if pl["kind"] == "playlist" and pl["visibility"] in ("public", "unlisted"):
        return "viewer"
    return None

def require_playlist_role(conn, playlist_id: int, actor: Actor, min_role: str) -> sqlite3.Row:
    # role None -> 404 playlist_not_found; role below min_role -> 403 not_allowed
```

- For a cross-playlist move, the actor needs `editor` on **both** playlists.
- `require_playlist_owner` (`main.py:425`) becomes a thin wrapper, `require_playlist_role(..., "owner")`, for the remaining non-op owner checks (audit-embeds, delete playlist, import target).
- Phase 5's contributor rule ("append only, remove only rows you added") is a per-op check on `added_by_user_id` that slots in at step 4.

### 2.5 Provenance (server-side, per inserted row)

`added_by_user_id = actor`, `created_at = now`. Then:

| `ref` | Condition | `source_playlist_id` / `source_user_id` / `source_track_id` | `migrated_from_*` |
|---|---|---|---|
| none | n/a | NULL / NULL / NULL | NULL |
| `{track_id: R}` | R exists, R's playlist P is `VISIBLE` to actor, `R.video_id == item.video_id`, `P.user_id != actor` | `P.id` / `P.user_id` / `R.id` (credit the **immediate** source, Webjay "via") | copied from R |
| `{track_id: R}` | same checks, `P.user_id == actor` (your own playlist or crate) | **copied verbatim from R** (keeps the original provenance) | copied from R |
| `{track_id: R}` | any check fails | NULL / NULL / NULL, `provenance: "dropped"` | NULL |
| `{playlist_id: P}` | `verified_source(P, video_id)` and P `VISIBLE` to actor; credit is the owner if not the actor, else no credit | `P.id` / `P.user_id` / NULL | NULL |

`verified_source` and `derive_grab_source` (`main.py:704`, `main.py:1309`) gain the `VISIBLE` check. Without it they are an oracle for "does private playlist N contain video X".

When rows are read, `source_playlist_id` and `source_playlist_title` are blanked unless the source playlist is `LINKABLE` for the viewer. `source_user_id` and `source_username` ("via @maya") are always shown.

### 2.6 Inverses and the internal op forms

The inverse is computed while applying, against the exact pre-state. It is stored in `inverse_json` and is **never accepted from a client**: `POST /api/ops` rejects any `kind` other than the five public ones.

| Forward | Inverse (internal) |
|---|---|
| `insert` → created ids `[t1..tn]` | `{"kind":"delete","playlist_id":P,"track_ids":[tn..t1],"expect":{"t1":"<video_id>",…}}` |
| `delete [d1..dn]` (applied in order; before removing each `di` the server records `prev_i`, the row before it **at that moment**, and `index_i`) | `{"kind":"restore","playlist_id":P,"rows":[{…full row incl. id, provenance, created_at…,"after_track_id":prev_n,"index":index_n}, …in reverse order…]}` |
| `move` within P (each moved `ti` is removed in block order; record `prev_i` and `index_i`) | `{"kind":"move_each","playlist_id":P,"moves":[{"track_id":tn,"after_track_id":prev_n,"index":index_n}, …reverse…]}` |
| cross-move | the inverse of its `delete` row (`restore` on the source) and of its `insert` row (`delete` on the destination) |
| `update` with old values `o` and new values `n` | `{"kind":"update","playlist_id":P,"track_id":T,"set":o,"expect":n}`. For a swap, `set` also restores `video_id`, `migrated_from_*` and `source_*` |
| `set_meta` with old `o` and new `n` | `{"kind":"set_meta","playlist_id":P,"set":o,"expect":n}` |
| `reorder` (server-only, from `PUT /api/playlists/{id}/reorder`; never a client op kind) with the new full order `[t1..tn]` | `{"kind":"reorder","playlist_id":P,"track_ids":[previous order]}`. Applying it sets positions `1..n` in one pass; a listed id that is gone is a conflict, rows that aren't listed keep their relative order at the end |

When an `update`'s `expect` names a `source_playlist_id` / `source_user_id` that an FK has since nulled (a deleted playlist), it is compared as NULL, so that undo or redo doesn't conflict over a change nobody made through the op log.

`restore` re-inserts with the **original `id`**. That is safe because SQLite `AUTOINCREMENT` never reissues an id, so client references and later inverses stay valid.

Why recording the anchors in sequence is correct: removing `[A, B]` where `Z, A, B` are adjacent records `prev(A) = Z`, then `prev(B) = Z`. Undo restores `B` after `Z`, then `A` after `Z`, which gives `Z, A, B` again.

If an anchor is missing at undo time, the row goes back at its recorded `index` (clamped). A missing anchor alone is not a conflict.

### 2.7 Caps

| Cap | Value | Error |
|---|---|---|
| Tracks per playlist (after each op) | 1000 (`MAX_TRACKS_PER_PLAYLIST`) | `400 playlist_full` |
| Items per client batch (insert items + delete/move ids + updates + set_metas) | 50 (`MAX_ENTRIES_PER_REQUEST`) | `400 batch_too_large` |
| Ops per batch | 20 | `400 batch_too_large` |
| Playlists touched per batch | 10 | `400 batch_too_large` |
| Server-composed batches (promote, fork, import commit) | `max_items=1000`, still subject to the per-playlist cap | n/a |
| Legacy reorder | one `reorder` effect (one op row) for any reorder bigger than a single drag | n/a |
| YouTube lookups per `/api/ops` request | bounded by the 50-item cap, which is checked (with the role check) before any lookup | `400 batch_too_large`, `403`/`404` |
| Body | 256 KB (existing middleware) | `413` |
| Rate limit | new bucket `"ops": (120, 2.0)` on `/api/ops`, undo, redo, `PUT /api/playlists/{id}/reorder`, `POST /api/playlists/{id}/fork` and `POST /api/me/crate/promote`. The existing `"add"` bucket is charged once per batch that needs any YouTube lookup | `429` + `Retry-After` |

### 2.8 Discovery, activity and badges: only for LISTED playlists

`claim_discovery(conn, playlist_row, video_ids, group_id)`:
- Runs only when `LISTED(playlist_row)`.
- For each video with `songs.first_added_by_user_id IS NULL`, sets `first_added_by_user_id = playlist owner`, `first_added_playlist_id`, `first_added_at = now`, and logs `discovered_song`.
- Claims are sticky. Deleting a track doesn't revoke them, which matches today.
- Undoing the group that made a claim **does** revert it. The claim is recorded in `forward_json.claims`. Undo resets those songs to NULL, then re-runs the claim against the earliest remaining LISTED row of that video (`ORDER BY pt.created_at, pt.id`).
- Undoing a group also **revokes the badges it earned**, so a paste-then-undo can't farm the first-finder badges (or Spark / Trendsetter for the credited users). Undo deletes the `user_badges` rows tagged `op_group_key = "<actor id>:<group_id>"` and their `earned_badge` activity lines, then re-evaluates each affected user with `record_activity=False`, so a badge whose criteria still hold without the group comes back silently. Redo awards (and tags) them again. Badges from non-undoable batches (a fork) are never revoked.

Activity, written only when the target is LISTED, with one line per op (not per track):

| Event | `action_type` | Summary example |
|---|---|---|
| insert/move-in, all rows credited to one other user | `grabbed_track` | `grabbed 'Tycho - …' from @maya into 'Basement Velvet'` (`… and 2 more` for n > 1) |
| insert/move-in, otherwise | `added_track` | `added 'Tycho - …' to 'Basement Velvet'` |
| first LISTED placement of a song | `discovered_song` | unchanged wording |
| `set_meta` making a playlist LISTED, or `POST /api/playlists` / promote / fork creating a LISTED one | `published_playlist` / `created_playlist` / `forked_playlist` | `forked 'Sunday Morning…' from @lucas`. A public fork of an unlisted or private source never names it: it logs `created_playlist` `published playlist '<fork title>'` (§4.1) |

Nothing is logged for `delete`, within-playlist `move`, `update`, crate writes, or writes to unlisted or private playlists.

**Unload is the payoff moment.** A track copied from @maya into your crate logs nothing and credits nothing visible. When you move it into a public playlist, the destination `insert` carries Maya's `source_user_id`: the activity line, Maya's Spark badge and the discovery claim all happen then.

---

## 3. Endpoints

All new endpoints:
- In google mode: 401 when signed out.
- In demo mode: take `user_id` (body for POST/PUT/DELETE, query for GET), like the existing ones.

### 3.1 `POST /api/ops`

Request:

```json
{
  "op_id": "01JCX4M2Q8H7ZK3V5T1N0B9AWE",
  "group_id": "01JCX4M2Q8H7ZK3V5T1N0B9AWE",
  "expected_versions": { "3": 13, "8": 5 },
  "ops": [
    { "kind": "move", "playlist_id": 8, "track_ids": [901], "to_playlist_id": 3, "after_track_id": 42 }
  ]
}
```

200 response:

```json
{
  "op_id": "01JCX4M2Q8H7ZK3V5T1N0B9AWE",
  "group_id": "01JCX4M2Q8H7ZK3V5T1N0B9AWE",
  "replayed": false,
  "results": [
    { "kind": "move", "playlist_id": 8, "to_playlist_id": 3, "moved": [ { "from_track_id": 901, "track_id": 1202 } ] }
  ],
  "versions": { "3": 14, "8": 6 },
  "order": { "3": [17, 18, 42, 1202, 19], "8": [905, 906] },
  "rows": { "1202": { "id": 1202, "playlist_id": 3, "video_id": "c_56qQqxvm4", "position": 4, "title": "Tycho - Full Performance (Live on KEXP)", "track_note": "", "start_seconds": null, "end_seconds": null, "source_user_id": 3, "source_username": "maya", "source_playlist_id": 2, "source_playlist_title": "2AM Terminal Glow (Coding Mix)", "source_track_id": 4, "added_by_user_id": 1, "created_at": "2026-11-11 10:02:11", "provenance": "kept" } },
  "skipped": [],
  "undo": { "can_undo": true, "undo_label": "Moved 1 track to 'Basement Velvet'", "undo_group_id": "01JCX4M2Q8H7ZK3V5T1N0B9AWE", "can_redo": false, "redo_label": null, "redo_group_id": null },
  "new_badges": []
}
```

`order` and `rows` let the client reconcile its optimistic state without refetching the playlist.

Errors:

| Status | `code` | When |
|---|---|---|
| 400 | `bad_op`, `bad_clip`, `bad_placement`, `batch_too_large`, `playlist_full`, `crate_visibility_fixed`, `video_not_found` | validation. The body includes `op_index` (and `item_index`). `bad_op` also for an `op_id` / `group_id` with a reserved prefix (`srv-`, `fork-`, `promote-`) |
| 401 | `signed_out` | google mode with no session |
| 403 | `not_allowed` | the playlist is visible but the actor's role is too low |
| 404 | `playlist_not_found`, `track_not_found` | missing **or not visible**. The body includes `op_index` |
| 409 | `stale_version` | `{"detail":"Playlist 3 changed since you loaded it.","code":"stale_version","conflicts":[{"playlist_id":3,"expected":13,"current":14}],"versions":{"3":14}}` |
| 409 | `op_id_conflict` | op_id reused with a different body, or by another user |
| 409 | `group_closed` | the group_id isn't the user's latest live group |
| 409 | `anchor_missing` | the `after_track_id` is gone |
| 413 | n/a | body over 256 KB |
| 422 | n/a | Pydantic shape errors (list `detail`, as today) |
| 429 | n/a | rate limit |
| 503 | `youtube_unavailable` | a metadata lookup failed transiently; `Retry-After: 30` |

Order of checks: shape caps (2.1 step 1), reserved ids and the role check for every touched playlist all run **before** the YouTube lookups for unknown videos, so a batch that will be refused costs no outbound calls. The 50-item cap also bounds the lookups per request.

**Client contract:**
- Send ops through a **single serial queue**, one request in flight per tab.
- Each paste or keystroke gets a new `op_id`. V-V-V is three requests, each anchored after the previous result's last `track_id`, which gives three undo groups.
- Retry on network failure with the **same** `op_id`.
- On `409 stale_version`, refetch the playlist, rebase the pending local ops, and send them with a **new** op_id.

### 3.2 `POST /api/ops/undo` and `POST /api/ops/redo`

The stack is per user, spans all of that user's playlists, is stored on the server (so it survives a reload), and undoes groups atomically.

**Undo** picks the user's latest live group: `MAX(id)` over the rows with `undone_at IS NULL AND discarded_at IS NULL`, taking all rows of that `group_id`. In one `BEGIN IMMEDIATE` transaction it:
1. checks for conflicts (below);
2. applies the inverses of the group's rows in reverse `id` order;
3. sets `undone_at = now`;
4. bumps `version` on the touched playlists;
5. deletes the group's `activity_log` rows (`op_group_id`);
6. reverts the group's discovery claims and revokes the badges it earned (2.8).

Badges are never revoked.

**Redo** picks the live-but-undone group with the **lowest** id among the rows where `undone_at IS NOT NULL AND discarded_at IS NULL`. Because step 10 of §2.1 discards the redo branch on any new action, that set is always a suffix of the stack, and its lowest member is the next one to redo. Redo then re-applies `forward_json` in `id` order, clears `undone_at`, bumps versions, writes the activity lines again and runs the discovery claim again.

Request: `{}`. In demo mode, `{"user_id": 1}`. Optional fields:
- `group_id`: an assertion that this group is the expected one, so a double keypress can't undo twice. On mismatch: `409 stack_moved` with the current top.
- `skip: true`: mark the top group discarded without applying it. This is the escape hatch after an `undo_conflict`. Skip only discards; it undoes nothing. A client that offers "skip it and undo the step before" sends `skip` with the conflicting `group_id` (from the `undo_conflict` body), then a normal undo with the new `undo.undo_group_id`.

Clients should send `group_id` on every undo, redo and skip, taken from `undo.undo_group_id` / `undo.redo_group_id`, ignore key auto-repeat, and keep at most one undo request in flight. Then a held Ctrl+Z or a double click undoes one group and the extra presses get a harmless `409 stack_moved`.

200 response (same reconcile fields as `/api/ops`):

```json
{
  "group_id": "01JCX4M2Q8H7ZK3V5T1N0B9AWE",
  "label": "Moved 1 track to 'Basement Velvet'",
  "results": [
    { "kind": "delete", "playlist_id": 3, "track_ids": [1202] },
    { "kind": "restore", "playlist_id": 8, "track_ids": [901] }
  ],
  "versions": { "3": 15, "8": 7 },
  "order": { "3": [17, 18, 42, 19], "8": [901, 905, 906] },
  "rows": { "901": { "id": 901, "playlist_id": 8, "position": 1, "…": "…" } },
  "notes": [],
  "undo": { "can_undo": true, "undo_label": "Added 1 track to 'Crate'", "undo_group_id": "01JCX3…", "can_redo": true, "redo_label": "Moved 1 track to 'Basement Velvet'", "redo_group_id": "01JCX4M2Q8H7ZK3V5T1N0B9AWE" }
}
```

When the stack is empty: `200 {"group_id": null, "reason": "nothing_to_undo", "undo": {…}}` (or `nothing_to_redo`).

**Conflicts.** The inverse's `expect` preconditions fail when an inserted row is gone or its `video_id` differs, or when the current values don't equal the forward `set`. The check also fails when a later live op **by another user** on the same playlist touches the same track ids, or the same meta field for `set_meta`. In either case nothing is applied and the response is:

```json
{ "detail": "Can't undo: that playlist was changed afterwards.", "code": "undo_conflict",
  "group_id": "01JCX…", "conflicts": [ { "playlist_id": 3, "track_id": 1202, "by_user_id": 4 } ] }
```

With owner-only writes and stack order, the "another user" branch can't fire yet. It is written now so that Phase 5 only adds members.

`GET /api/me/undo` returns `{"can_undo", "undo_label", "undo_group_id", "can_redo", "redo_label", "redo_group_id"}`. The same object is also embedded in `GET /api/bootstrap` as `current_user.undo`.

### 3.3 `GET /api/me/crate`

Calls `ensure_crate`, then returns the same shape as `GET /api/playlists/{id}`, plus one field per track:
- `also_in`: the actor's own **non-crate** playlists that contain the same `video_id` with the same clip, as `[{playlist_id, title}]`.

That gives each crate row its "already unloaded to…" memory, and "Clear unloaded" becomes a client-side `delete` of rows where `also_in` is non-empty.

```json
{ "id": 5, "kind": "crate", "visibility": "private", "version": 7, "title": "Crate", "user_id": 1,
  "tracks": [ { "id": 977, "position": 1, "video_id": "c_56qQqxvm4", "title": "Tycho - …", "source_username": "maya",
                "source_playlist_id": 2, "source_playlist_title": "2AM Terminal Glow (Coding Mix)", "also_in": [ { "playlist_id": 4, "title": "Webjam Inaugural Transmission" } ] } ] }
```

The crate has no comments, likes or play counts (§3.9).

### 3.4 Unload: plain ops, no new endpoint

| User intent | Request |
|---|---|
| Unload selection into P at the drop point (default from the crate: **move**) | `{"kind":"move","playlist_id":<crate>,"track_ids":[…],"to_playlist_id":P,"after_track_id":…}` |
| Unload but keep a copy in the crate | `{"kind":"insert","playlist_id":P,"items":[{…,"ref":{"track_id":<crate row>}}]}`. Copying your own row keeps the original provenance |
| Fill the crate from anywhere (the ＋ button, paste, the C key) | `{"kind":"insert","playlist_id":<crate>,"items":[…]}` |
| Clear unloaded | `{"kind":"delete","playlist_id":<crate>,"track_ids":[ids with also_in]}` |

### 3.5 `POST /api/me/crate/promote`

The crate's contents become a new playlist, and the crate is emptied.

Request:

```json
{ "title": "Late Shift", "description": "", "visibility": "public", "track_ids": null }
```

`track_ids: null` means everything. A list means only those tracks; the rest stay in the crate.

Server steps:
1. Create playlist P: `kind='playlist'`, the given `visibility` (default `'public'`), `version=0`.
2. Log `created_playlist` if P is LISTED.
3. Run `apply_ops` with `max_items=1000` and `group_id = batch_id = "promote-<ulid>"`, containing one cross-playlist `move` of the selected crate rows into P that keeps crate order.
4. Upsert dock `last_used_at` for P.

The crate keeps its id. Provenance travels with the rows, so Maya is credited at this moment if P is public.

201 response:

```json
{ "playlist_id": 41, "group_id": "promote-01JCX…", "moved": 12,
  "versions": { "5": 9, "41": 1 }, "crate": { "id": 5, "version": 9, "track_count": 0 }, "new_badges": [] }
```

Errors: `400 crate_empty`, `400 bad_title`, `400 playlist_full`. If `track_ids` contains an id that isn't in the crate: `404 track_not_found`.

Undo of a promote moves the rows back into the crate and leaves P in place, empty. The undo response includes `"notes": [{"code": "playlist_left_empty", "playlist_id": 41}]`, so the client can offer to delete P as well. Redo moves them into P again.

### 3.6 `GET /api/me/dock` and `PUT /api/me/dock`

`GET` response. Chips are numbered: the crate is slot 0, then the pinned chips in `sort` order, then the 5 most recent unpinned playlists by `last_used_at`. Slots stop at 9, and later chips have `slot: null`.

```json
{
  "crate":  { "slot": 0, "playlist_id": 5, "kind": "crate", "title": "Crate", "visibility": "private", "track_count": 2, "version": 7, "thumbs": ["https://i.ytimg.com/vi/c_56qQqxvm4/hqdefault.jpg"] },
  "pinned": [ { "slot": 1, "playlist_id": 4, "kind": "playlist", "title": "Webjam Inaugural Transmission", "visibility": "public", "track_count": 4, "version": 3, "thumbs": ["…", "…", "…", "…"], "last_used_at": "2026-11-11 09:58:00" } ],
  "recent": [ { "slot": 2, "playlist_id": 12, "…": "…" } ]
}
```

- `thumbs` holds up to 4 `songs.thumbnail_url` values, in playlist order.
- Only the user's own playlists appear in the dock in Phase 1a, because owner-only writes mean only those are drop targets.
- `last_used_at` is updated by every successful apply_ops, undo or redo that touches a playlist the actor owns, and by creating, forking or promoting one. Reads never update it.

`PUT` request, a full replacement of the pinned set and its order: `{"pinned": [4, 12, 9]}`.
- Response: the `GET` shape.
- `400 bad_dock`: a duplicate id, more than 12 pins, or the crate's id included.
- `404`: a playlist that isn't the actor's.

### 3.7 `POST /api/resolve-paste`

Turns pasted text into insert-ready items. It **never writes**: it creates no `songs` rows. Metadata for unknown videos is fetched with `fetch_youtube_metadata`, which is TTL-cached, so the following `/api/ops` insert reuses it.

Request:

```json
{ "text": "https://youtu.be/c_56qQqxvm4?t=312\nhttps://webjam.example/p/2?track=4\nhttps://www.youtube.com/playlist?list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG", "html": null }
```

- `text`: at most 100,000 characters (`MAX_URL_BLOB`).
- `html`: optional, at most 256 KB. When it contains `data-webjam='…'`, that JSON payload is used first (§5).

Recognition order. The result is a union of everything found, in text order, **keeping duplicates**. This is a new `extract_paste_entries` function, because `extract_video_entries` dedupes.

1. **A Webjam JSON payload**: the text or HTML attribute is JSON with `"webjam": 1`. Each item is validated: `ref.track_id` must be `VISIBLE` and its row's `video_id` must match, otherwise the ref is dropped. Server values for title, channel and thumbnail replace the client's.
2. **Webjam URLs**: the host must equal the `WEBJAM_BASE_URL` host, or the URL is a relative path; in demo mode a loopback host also counts.
   - `/p/{id}`, `/p/{id}-{slug}` or `/#playlist/{id}`: all of that playlist's tracks in order, each with `ref.track_id`, plus a `webjam_playlist` pointer so the client can offer "fork instead".
   - `/p/{id}?track={tid}`: one item, with ref `tid`.
   - `/t/{video_id}?s=&e=` or `/#song/{video_id}`: one item with that clip and no ref.
   - A target that is missing or not `VISIBLE` goes to `unresolved` with `not_found`. Private and missing look the same.
3. **YouTube video URLs** (watch, youtu.be, shorts, embed, music): `t=`, `start=` and `end=` become the clip. For `watch?v=X&list=PL…`, the result is item X **and** a `youtube_playlist` pointer.
4. **YouTube playlist URLs** (`/playlist?list=` or a bare `PL…` / `OLAK5uy_…` id, through `extract_playlist_id`): a pointer only, never expanded. The client feeds `pointer.source_input` into the existing `POST /api/playlists/import/inspect`, aimed at the crate.
5. **Bare 11-character ids**, only if steps 2–4 found nothing, and only when the **whole** paste is ids separated by whitespace or commas, none of them shaped like a word (`^[A-Za-z][a-z]{10}$` or `^[A-Z]{11}$`, e.g. `programming`, `Information`, `INFORMATION`). So free text such as "really interesting information about programming" returns empty lists and makes no lookups (and charges no `add`). A real id of that shape (about 1 in 10,000) can still be pasted as a URL.
6. **Anything else**: no items.

200 response:

```json
{
  "sources": ["youtube_url", "webjam_url", "youtube_playlist"],
  "items": [
    { "video_id": "c_56qQqxvm4", "start_seconds": 312, "end_seconds": null, "note": "", "display_title": null, "ref": null,
      "title": "Tycho - Full Performance (Live on KEXP)", "channel_title": "KEXP", "thumbnail_url": "https://i.ytimg.com/vi/c_56qQqxvm4/hqdefault.jpg",
      "known": true, "embeddable": null, "from": "youtube_url" },
    { "video_id": "c_56qQqxvm4", "start_seconds": null, "end_seconds": null, "note": "Scott Hansen's synth patches…", "display_title": null,
      "ref": { "track_id": 4, "playlist_id": 2, "username": "maya" },
      "title": "Tycho - Full Performance (Live on KEXP)", "channel_title": "KEXP", "thumbnail_url": "…", "known": true, "embeddable": null, "from": "webjam_url" }
  ],
  "pointers": [
    { "type": "youtube_playlist", "list_id": "PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG", "source_input": "https://www.youtube.com/playlist?list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG", "import_endpoint": "/api/playlists/import/inspect" }
  ],
  "unresolved": []
}
```

- Free text returns `{"sources": [], "items": [], "pointers": [], "unresolved": []}` with status 200.
- `embeddable` comes from the existing embed cache when a value is cached, else `null`. No embed checks are made here.
- `unresolved` entries look like `{token, reason}`, with `reason` one of `not_found`, `youtube_unavailable` or `too_many`.
- Limits:
  - at most 1000 items;
  - at most 200 parsed URLs (`MAX_PARSED_ENTRIES`);
  - at most 50 **unknown** videos looked up per call (the rest are `too_many`);
  - the `"add"` bucket is charged once if any lookup happened.

Status codes:
- 200 for any parse, including an empty result;
- 401 when signed out in google mode, because the endpoint fans out to YouTube;
- 422 when `text` is over the limit;
- 429 for the rate limit.

The items are already the `insert` item shape. The extra display fields (`title`, `channel_title`, `thumbnail_url`, `known`, `embeddable`, `from`, `ref.playlist_id`, `ref.username`) are ignored by `/api/ops`.

### 3.8 `DELETE /api/playlists/{id}`

Owner only: `404` if not visible, `403` if visible but not the owner's. The crate can't be deleted: `400 cannot_delete_crate` (empty it with a `delete` op instead). It is **not undoable**; the UI must confirm.

In one transaction:
1. Mark `discarded_at` on **every row of every group** that has a row on this playlist, including that group's rows on other playlists. For example, undoing a crate→P move after P is deleted is no longer possible.
2. `DELETE FROM comments WHERE target_type='playlist' AND target_id=?` and `DELETE FROM activity_log WHERE link_type='playlist' AND link_id=?`. These have no FK.
3. `DELETE FROM playlists WHERE id=?`. This cascades to `playlist_tracks`, `playlist_likes`, `play_events`, `playlist_ops` and `user_dock`. It sets NULL in other rows' `source_playlist_id`, in `songs.first_added_playlist_id`, and in `playlists.forked_from_playlist_id`. Other rows' `source_track_id` keep the (now dangling) id, which readers ignore (§1.3).

Credits to the user (`source_user_id`, `first_added_by_user_id`) and awarded badges remain.

Response: `200 {"ok": true, "playlist_id": 7, "discarded_undo_groups": 3}`.

### 3.9 `POST /api/playlists/{id}/fork`

"Copy all", credited "via".

Request: `{"title": null, "visibility": null}`. A null title means the source title. A null (omitted) visibility means **the source's visibility**, so forking an unlisted or private playlist doesn't publish its title by default; `"public"` must be asked for explicitly. The source must be `VISIBLE` to the actor, else `404`. Forking a crate is `400 use_promote`. Rate limited by the `ops` bucket.

Server steps:
1. Create playlist F with `forked_from_playlist_id = id` and the source description.
2. Run `apply_ops(..., max_items=1000, undoable=False)` with one `insert` of all source rows in order, each item with `ref.track_id` set to the source row. Provenance follows 2.5:
   - for someone else's playlist, the credit is the source owner, row by row;
   - for your own playlist, the original provenance is kept.
   Track notes, clips and display titles are copied.
3. Log `forked_playlist` (`forked '<source title>' from @owner`) if F is LISTED **and the source is LISTED**. If F is LISTED but the source is unlisted or private, log `created_playlist` (`published playlist '<F title>'`) instead: the feed must not name or link a non-LISTED playlist (§4.1, the same rule that blanks `forked_from`).
4. Run badges as in 2.1 step 14.

The fork is not on the undo stack; delete the fork to reverse it.

201 response:

```json
{ "playlist_id": 42, "version": 1, "track_count": 4, "forked_from": { "playlist_id": 1, "title": "Sunday Morning Coffee & Tape Hiss", "username": "lucas" }, "new_badges": [] }
```

Playlist responses gain `forked_from: {playlist_id, title, username} | null`. It is non-null only when the source is `LINKABLE` to the viewer. If the source was deleted, it shows `{"playlist_id": null, "username": …}` only when that is still derivable; otherwise it is `null`.

> Badge note (owner said badges stay for now): a fork of N tracks counts as N grabs for Spark and Trendsetter. `forked_from_playlist_id` is there so that a later badge pass can count a fork as one grab.

### 3.10 Page routes for clipboard links (tiny, backend)

- `GET /p/{id}`, `GET /p/{id}-{slug}` and `GET /t/{video_id}` serve `index.html`, unchanged; Phase 3 adds OG meta.
- The SPA maps `location.pathname` to the existing hash routes (§8).
- These routes make the text/plain clipboard URLs (§5) open somewhere.

### 3.11 Existing endpoints: same request and response shapes, now through `apply_ops`

Every one of these keeps its current request and response shape and adds `version` (for the touched playlist) to its response. Each runs as one server-generated batch (`batch_id = "srv-<ulid>"`), so each is **one undo group** in the server stack. Quick drop and import, which today skip the client snapshot, become undoable.

| Endpoint (line in `app/main.py`) | Becomes |
|---|---|
| `POST /api/playlists` (1230) | Create the row; new optional `visibility` (default `'public'`). `initial_urls` becomes one `insert` with `skip_unavailable=True`. `created_playlist` is logged only if LISTED |
| `PUT /api/playlists/{id}` (1287) | `set_meta {title, description}` |
| `POST /api/playlists/{id}/tracks` (1338) | `insert`, append, `skip_unavailable=True`. `source_playlist_id` becomes `ref: {playlist_id}`. A bare `source_user_id` keeps today's "first finder" path in `derive_grab_source`, plus the `VISIBLE` check. `custom_title` becomes `display_title` when there is one entry |
| `PUT /api/playlists/{id}/tracks/{tid}` (1414) | `update {note, start/end, display_title, video_id}`. `direction: up/down` becomes `move` with `position = index ∓ 1`. Both go in one batch. Optional `group_id` (same rules as `/api/ops`, reserved prefixes refused): calls that share it form one undo group, which the old UI's auto-migrate uses so one Ctrl+Z reverts the whole run |
| `DELETE /api/playlists/{id}/tracks/{tid}` (1759) | `delete`. The manual renumbering loop goes away |
| `PUT /api/playlists/{id}/reorder` (1784) | Diff the current order against the requested one (longest increasing subsequence). A single drag (one track out of place) is one `move` op. Anything bigger is **one** server-only `reorder` effect: one op row whose inverse is the previous order, applied in one O(n) pass (2.6). Rate limited by the `ops` bucket |
| `POST /api/playlists/import/commit` (1605) | Create the playlist if needed, then one `insert` with `skip_unavailable=True`. Item `migrated_from_*` is accepted **only** on this internal path, as today. `target_playlist_id` may be the crate |
| `PUT /api/playlists/{id}/snapshot` (1820) | **Deleted in Phase 1a**; see below |

`add_single_video_to_playlist` (`main.py:722`) is removed. Its song-insert and discovery logic moves into `prefetch_song_meta`, `apply_ops` and `claim_discovery`. A test greps `app/` for `INSERT INTO playlist_tracks`, `UPDATE playlist_tracks` and `DELETE FROM playlist_tracks`, and fails on any hit outside `app/ops.py`, the seed, and the playlist delete.

**`PUT /snapshot` retirement path.**

- **Phase 1a (recommended, ships with this contract):**
  1. Switch the old UI's Undo (the button at `app.js:1429/1466`, and Ctrl+Z at `app.js:2614`) to `POST /api/ops/undo`. Delete `capturePlaylistSnapshot`'s payload building, and drive the button from `current_user.undo.can_undo` / `undo_label`.
  2. Delete the endpoint, `SnapshotTrackItem`, `RestorePlaylistSnapshotRequest`, `SNAPSHOT_PATH` and `MAX_SNAPSHOT_BODY_BYTES` (the body-cap exemption).
  3. Replace the snapshot tests:
     - `tests/test_review_fixes.py` (11 refs), `tests/test_security_backend.py` (9) and `tests/test_webjam.py` (3) become undo/redo tests;
     - the provenance-forgery assertions move to a test that `/api/ops` ignores client provenance fields (`added_by_user_id`, `source_*`, `created_at` are rejected as unknown fields by `extra="forbid"`).
  - Behaviour change, acceptable: undo becomes "my last action anywhere", where the old UI preferred the open playlist.
- **Fallback, only if (1) slips.** Keep `PUT /snapshot` as a compatibility shim that never deletes and reinserts:
  - It diffs the submitted list against the server rows by id. Missing ids become a `delete`. Unknown ids become an `insert` with no provenance; old deleted ids can't be revived, because revival is undo's job. Order changes become the reorder diff above. Title and description become `set_meta`.
  - Everything goes through `apply_ops`, so the op log stays the truth.
  - Remove it in Phase 1b, when the new shell lands.

---

## 4. Crate and private-playlist privacy

### 4.1 Rules

- **Crates** and **private** playlists are seen only by their owner, and only by id or in the owner's own lists.
- **Unlisted** playlists open by id for anyone, but are never listed, counted, credited or linked from elsewhere.
- None of these three ever count toward feeds, song pages, curator lists, "first found by", badges, search, other people's profile views, or any count.
- Owner views (`/api/bootstrap` `my_playlists`, `/api/me/*`, your own profile) show your own private and unlisted playlists with `visibility`. The crate shows only in bootstrap and dock, never in the profile's `playlists`.

### 4.2 Shared predicates (`app/visibility.py`)

Hand-written `WHERE` clauses get forgotten. These are the only allowed spellings:

```python
LISTED = "({a}.kind = 'playlist' AND {a}.visibility = 'public')"
VISIBLE = "(({a}.kind = 'playlist' AND {a}.visibility IN ('public','unlisted')) OR {a}.user_id = :viewer)"
LINKABLE = "(({a}.kind = 'playlist' AND {a}.visibility = 'public') OR {a}.user_id = :viewer)"
def listed(a="p"): ...; def visible(a="p"): ...; def linkable(a="p"): ...
def require_visible_playlist(conn, playlist_id, viewer) -> Row   # 404 otherwise
def song_visible(conn, video_id, viewer) -> bool  # EXISTS a row in a VISIBLE playlist where (LISTED or owned by viewer)
```

`time_grabbed`-style counts filter the **target** playlist (where the copy now lives) with `LISTED`. Joins that only display a source or debut playlist's title use `LINKABLE`, and blank the title and id when it fails.

### 4.3 Every query that must filter

`app/main.py` (line numbers at `2a55327`):

| # | Function / line | Query | Required filter |
|---|---|---|---|
| 1 | `fetch_all_users_summary` 588 | `playlist_count` | `LISTED(p)` (the curator list is public) |
| 2 | same | `songs_discovered` | `first_added_by_user_id = u.id`. Correct by construction, since only LISTED placements claim |
| 3 | same | `times_grabbed` | join `playlists tp ON tp.id = pt.playlist_id` + `LISTED(tp)` |
| 4 | `get_bootstrap` 880 | `my_playlists` | owner view: all of the actor's playlists including the crate (listed first), with `kind`, `visibility` and `version`. Add `crate_id` and `undo` |
| 5 | `get_community_feed` 1013 | playlists | `WHERE LISTED(p)` |
| 6 | same, 1020 | `times_grabbed_from` subquery | target `LISTED` |
| 7 | same, 1044 | per-playlist tracks | `LEFT JOIN users fu` (nullable finder). `source_*` projection per 2.5 |
| 8 | same, 1074 | `recent_songs` | `WHERE s.first_added_at IS NOT NULL` **and the song is visible to the viewer** (`EXISTS` a row in a `LISTED` playlist or one the viewer owns, i.e. `song_visible`). Claims stay sticky for badge counts, but a song whose public placements were all made private or moved to a crate drops out of the list, like its activity lines and its page. `debut_playlist_title` only if `LINKABLE`. `playlist_count` subquery: count only `LISTED` playlists |
| 9 | same, 1102 | `activity` | `WHERE a.link_type != 'playlist' OR EXISTS (SELECT 1 FROM playlists p WHERE p.id = CAST(a.link_id AS INTEGER) AND LISTED(p))`. This hides lines about playlists that later went private (writes already skip non-LISTED) |
| 10 | same, 1115 | curators | covered by #1. Badges unaffected |
| 11 | `get_playlist_detail` 1136 | header | `require_visible_playlist`, 404 otherwise. Add `kind`, `visibility`, `version`, `forked_from`. `times_grabbed_from` target `LISTED` |
| 12 | same, 1175 | tracks | `JOIN users fu` → `LEFT JOIN`. `sp` (source playlist) title only if `LINKABLE`. `playlist_count` subquery `LISTED` only. `position` = ordinal |
| 13 | same, 1211 | `other_playlists` | `LISTED(p2) OR (p2.user_id = :viewer AND p2.kind = 'playlist')` |
| 14 | same, 1226 | comments | gated by #11. Crate: no comments (`[]`) |
| 15 | `create_playlist` 1230, `commit_playlist_import` 1642 | `created_playlist` activity | only if LISTED |
| 16 | `verified_source` 704 / `derive_grab_source` 1309 | source lookup | add `VISIBLE(p)` for the actor (an existence oracle otherwise) |
| 17 | `update_playlist_track` 1507 (swap credit) | `verified_source` | same as #16 |
| 18 | `toggle_playlist_like` 1953 | target | `require_visible_playlist`. Crate: `400 not_likeable`. `liked_playlist` / `superloved_playlist` activity only if LISTED |
| 19 | `toggle_song_like` 2049 | target | `song_visible(video_id, actor)`, else 404 |
| 20 | `record_playlist_play` 2162 | target | `require_visible_playlist`. Crate or private: never increment `playlists.plays` **or** `songs.total_plays` |
| 21 | `get_song_page` 2228 | song | 404 unless `song_visible`. `JOIN users u` (finder) → `LEFT JOIN`, so finder fields can be null. `debut_playlist_title` only if `LINKABLE` |
| 22 | same, 2269 | `appearances` | `LISTED(p) OR (p.user_id = :viewer AND p.kind = 'playlist')`. `sp` title `LINKABLE`. `position` = ordinal |
| 23 | `get_user_profile` 2312 | `playlists` | viewer is the profile owner: `p.kind = 'playlist'` (any visibility, with a `visibility` field). Otherwise `LISTED(p)`. `forked_from_playlist_id` is nulled unless the source is `LINKABLE` to the viewer (as on the detail endpoint) |
| 24 | same, 2355 | `discovered_songs` | as #8 (visible-to-viewer filter, `debut_playlist_title` `LINKABLE`, `playlist_count` `LISTED`) |
| 25 | same, 2379 | `liked_songs` | for non-owners, only songs where `song_visible(…, viewer)` |
| 26 | same, 2404 | `liked_playlists` | `LISTED(p)`, or `VISIBLE(p)` when the viewer is the profile owner |
| 27 | same, 2427 | `grabs_from_user` | `LISTED(tp)`. `sp` title `LINKABLE` |
| 28 | same, 2446 | `grabs_by_user` | `LISTED(tp)`, unless the viewer is the profile owner (then `tp.kind = 'playlist'`) |
| 29 | `resolve_comment_target` 2481, `list_comments` 2508, `create_comment` 2521 | playlist targets | `require_visible_playlist(viewer)` (`list_comments` gains `resolve_viewer`). Crate: `400`. `commented` activity only if LISTED. Song targets: `song_visible` |
| 30 | `audit_playlist_embeds` 1686 | n/a | already owner-only |
| 31 | new: `resolve-paste` webjam URLs, `fork`, `promote`, dock, `GET /api/me/crate` | n/a | `VISIBLE` (or owner) as specified in §3 |

`app/badges.py`, `compute_user_metrics` (every metric is public on profiles, so every playlist-based metric uses `LISTED`):

| # | Line | Metric | Required filter |
|---|---|---|---|
| B1 | 177 | `songs_discovered` | correct by construction (claims are LISTED-only). `first_song_title` unchanged |
| B2 | 186 | `viral_discoveries` | join `pt.playlist_id` → `LISTED` |
| B3 | 205 | `tracks_taken_from_me` / `distinct_users_inspired` / `spark_context` | target `LISTED`. In the context string, the source playlist title is used only if the source is `LISTED`, else `'your playlist'`. The context note is shown publicly |
| B4 | 229 | `tracks_taken_by_me` | target `p` `LISTED` |
| B5 | 251 | `alchemist` / `multi_source` | `LISTED(p)` |
| B6 | 287 | `playlist_likes_received` | `LISTED(p)` |
| B7 | 299 | `song_likes_received` | unchanged (finder credit is LISTED-only) |
| B8 | 319 / 336 | `max_single_superlove` | playlist branch: `LISTED(p)` (the context shows the title). Song branch: the love still counts, but the context names the song only if it is in a `LISTED` playlist; otherwise it is `Bestowed N× Superlove` (a crate-only or private-only song must not reach the feed) |
| B9 | 364 | `max_playlist_plays` / `total_playlist_plays` / `playlist_count` | `WHERE user_id = ? AND kind = 'playlist' AND visibility = 'public'` |
| B10 | 375 | `annotated_playlists` | `LISTED(p)` |
| B11 | 391 | `comments_written` | no filter needed (it counts your own comments) |

**Search.** There is no Webjam search endpoint yet. The `#songs` directory reads `/api/feed` `recent_songs` (#8). Any future `/api/search`, and the Phase 3 OG and export routes, must use `LISTED` for lists and `require_visible_playlist` for direct ids. Private and crate pages get a generic OG card.

---

## 5. Clipboard payload (for the future frontend)

### 5.1 Formats written on copy

| Format | How | Content |
|---|---|---|
| `application/x-webjam+json` | `copy` event: `e.clipboardData.setData(...)`. Async API: `new ClipboardItem({"web application/x-webjam+json": blob, ...})` when `ClipboardItem.supports("web application/x-webjam+json")` | the JSON below |
| `text/html` | always | `<ul data-webjam='{escaped JSON}'><li><a href="…">Title</a></li>…</ul>`. This carries the JSON through browsers that drop custom types |
| `text/plain` | always | one line per item, `URL  Title (m:ss–m:ss)` |
| `text/uri-list` | drag-out only | one URL per line |

```json
{
  "webjam": 1,
  "origin": "https://webjam.example",
  "copied_at": "2026-11-11T10:02:11Z",
  "mode": "copy",
  "items": [
    {
      "video_id": "c_56qQqxvm4",
      "start_seconds": 312,
      "end_seconds": 498,
      "note": "the build at 5:10",
      "display_title": null,
      "ref": { "track_id": 4, "playlist_id": 2, "user_id": 3, "username": "maya" },
      "title": "Tycho - Full Performance (Live on KEXP)",
      "channel_title": "KEXP",
      "thumbnail_url": "https://i.ytimg.com/vi/c_56qQqxvm4/hqdefault.jpg"
    }
  ]
}
```

- `mode: "cut"` marks a pending cut. Paste then sends a `move` op if every `ref.track_id` is still in a playlist the user owns; otherwise it falls back to `insert`.
- At most 1000 items. A paste larger than 50 items is sent as consecutive batches that share one `group_id`.
- **Trust:** only `video_id`, `start_seconds`, `end_seconds`, `note`, `display_title` and `ref.track_id` (or `ref.playlist_id`) are used by the server. `title`, `channel_title`, `thumbnail_url`, `ref.user_id` and `ref.username` are display hints for the paste preview. They are ignored, and the server re-derives them (§2.5, §3.7).

### 5.2 text/plain fallback lines

| Item | Line |
|---|---|
| a playlist row (has a ref) | `https://webjam.example/p/2?track=4  Tycho - Full Performance (Live on KEXP) (5:12–8:18)` |
| a song or clip with no ref | `https://webjam.example/t/c_56qQqxvm4?s=312&e=498  Tycho - Full Performance (Live on KEXP)` |
| a whole playlist ("copy link") | `https://webjam.example/p/2-2am-terminal-glow` |

Pasting this text back into Webjam goes through `POST /api/resolve-paste`, which turns the URLs back into the same items **with refs**, so credit survives a round trip through a chat app. The `/p/2?track=4` form takes the clip and note from row 4 on the server.

### 5.3 Paste order on the reading side

1. The `application/x-webjam+json` data, parsed locally. Paste it straight into an `insert` op: the server validates refs.
2. Else the `text/html` `data-webjam` attribute.
3. Else `text/plain` (plus `text/html`) sent to `/api/resolve-paste`.
4. If the response has `pointers` of type `youtube_playlist`, open the import flow aimed at the crate.

Only the `paste` event is handled, never `navigator.clipboard.read()`, so no permission prompt appears.

---

## 6. Response field additions (read side)

- **Playlist objects** (detail, feed, profile, bootstrap, dock) gain:
  - `kind`
  - `visibility`
  - `version`
  - `forked_from` (detail only)
- **Track rows** gain:
  - `source_track_id` (null unless that row still exists in the `LINKABLE` source playlist)
  - `position` as the 1-based ordinal (it already looks like that today)
- **`current_user` in bootstrap** gains:
  - `crate_id`
  - `undo: {can_undo, undo_label, undo_group_id, can_redo, redo_label, redo_group_id}`
- `songs.first_added_*` fields may be `null`, along with the finder fields derived from them.

---

## 7. Module layout

- `app/ops.py`:
  - Pydantic op models (`extra="forbid"`, discriminated on `kind`)
  - `apply_ops`, `undo_group`, `redo_group`
  - `claim_discovery`, `prefetch_song_meta`, `ensure_crate`
  - position helpers, the renormaliser and inverse builders
- `app/visibility.py`: the §4.2 predicates and gates.
- `app/paste.py`: `extract_paste_entries` and `resolve_paste`. It reuses `_parse_youtube_url`, `extract_playlist_id`, `fetch_youtube_metadata` and the embed cache.
- `app/main.py`: thin endpoints only. The legacy endpoints in §3.11 call `apply_ops`.

---

## 8. Minimal frontend changes in Phase 1a (old UI keeps working)

1. **Undo.** `undoLastPlaylistChange` calls `POST /api/ops/undo` with `group_id = state.currentUser.undo.undo_group_id`, drops presses while a request is in flight, and Ctrl+Z ignores key auto-repeat (`e.repeat`). On `undo_conflict` "skip it and undo the step before" skips the conflicting `group_id`, then undoes the new top. On `stack_moved` it refreshes and asks for another press. The Undo buttons and Ctrl+Z use `state.currentUser.undo`. The snapshot payload in `capturePlaylistSnapshot` and its call sites are removed. (Required, because `PUT /snapshot` is deleted.) Auto-migrate sends one shared `group_id` with all of its swap PUTs, so it stays one undo step. The player deck's "Love Mix" stays disabled while a crate is playing.
2. **Quick drop.** The `__new__` path in `handleQuickDrop` (`app.js:2170`) no longer creates an ordinary playlist titled "X's Crate". It targets `state.currentUser.crate_id`, and the select lists the crate first. (Recommended; without it users get confusing fake crates.)
3. **Path routes.** At boot, map `location.pathname` `/p/{id}` (with an optional `?track=`) to `#playlist/{id}`, and `/t/{vid}` to `#song/{vid}`. (Needed only for the clipboard text links in §5.2.)
4. **Crate playlist view.** Opening the crate with the old playlist view must not call like or comment. Hide those controls when `pl.kind === 'crate'`. (Small; otherwise the user sees a 400 toast.)

No other UI work happens in Phase 1a.

---

## 9. Tests to add (pytest, fresh DB per test)

- **Schema:** `init_db` works on an empty file. Seeding creates exactly one crate per user, and a second `ensure_crate` is a no-op. Inserting a second crate raises IntegrityError. A crate with `visibility='public'` violates the CHECK.
- **Privacy canary:** with Dan's seeded crate rows, check feed, song page, curators, profile (as another user), badges, `times_grabbed`, `playlist_count`, `recent_songs`, activity and comments. Each must be the same as with no crate rows. `GET /api/playlists/<crate>` is 404 for another user and 200 for Dan. Run the same set for a private playlist, and for unlisted (detail 200 for others, but absent from every list and count).
- **Provenance:**
  - copying a ref from another user's playlist credits that owner;
  - copying a ref from your own crate keeps the original credit;
  - a ref to a private playlist is dropped;
  - a ref whose `video_id` doesn't match is dropped;
  - client provenance fields are rejected with 422.
- **Positions:**
  - 60 inserts at the same spot keep order and trigger renormalisation;
  - `position: N` places the item at ordinal N;
  - moving a block keeps its relative order.
- **Undo/redo:**
  - V-V-V gives three groups, and three undos remove them in reverse;
  - a cross-move group undoes atomically and restores the original ids;
  - a new op after an undo discards redo;
  - the stack survives a new connection (a "reload");
  - undoing a claiming insert reverts the discovery claim;
  - after a playlist is deleted, groups touching it are skipped;
  - a forced precondition failure gives `409 undo_conflict`, and `skip: true` moves past it.
- **Idempotency and concurrency:**
  - the same op_id and body is a replay with no duplicate rows;
  - the same op_id with a different body gives 409;
  - another user's op_id gives 409;
  - a stale `expected_versions` gives 409 and nothing is applied;
  - `group_closed`.
- **Caps:**
  - 51 items gives 400;
  - an insert that would make 1001 tracks gives 400;
  - promote and fork with 1000 tracks pass.
- **Promote, unload and fork:**
  - promote empties the crate, keeps order, credits Maya once the playlist is public, and undo returns the rows to the crate with a `playlist_left_empty` note;
  - fork credits the source owner and sets `forked_from`;
  - forking a crate gives 400.
- **resolve-paste:**
  - JSON payload, Webjam URL forms (including private → `not_found`), YouTube `t=`, `watch?v&list=` (item + pointer), playlist URL (pointer only), duplicates kept, free text empty;
  - no `songs` rows are created.
- **Dock:**
  - writes update `last_used_at`;
  - PUT validation;
  - slots are numbered 0–9.
- **Single write path:** a grep finds no `playlist_tracks` writes outside `app/ops.py`, the seed and the playlist delete.
- **Legacy endpoints:** the existing 154 tests still pass, except the snapshot tests, which are replaced by undo tests (§3.11).

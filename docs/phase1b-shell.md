# Phase 1b build spec: the new shell

Status: **contract for implementation**. Branch `t3/phase1b-shell`, stacked on `main` (Phase 0 + Phase 1a merged).
This is the single document that the Foundation lane and the three parallel lanes (A Editing, B Discovery, C Import/Share/Crate) build against. Where it says "must", it is binding. Where something is not specified, pick the simplest option that fits §1 and write it down in your PR.

Binding inputs: `docs/phase1-ops-api.md` (ops, undo, crate, dock, paste, privacy). Nothing here changes that contract. This document only adds to it.

---

## 0. Decisions in one page

1. **Look: "tasteful".** Quiet paper-and-ink neutrals, one restrained accent, hairline rows, 48px thumbnails, generous margins around dense lists, a real dark mode. No halftone, handwriting, rotations, particles, confetti, bounce or glow. Colour only signals state (selected, playing, crate, drop, just landed).
2. **After sign-in you land on your own page (`/`) and see your playlists first.** Anonymous `/` is Discover. Listening never needs an account.
3. **Discovery is a chain of quiet links.** Every track row links to "in N playlists". Every track page lists those playlists and their curators. Every playlist says "N playlists share these tracks". Every profile lists taste neighbours with overlap counts. Every hop is one click, set in the tertiary ink colour, never a button.
4. **Curation is one key or one drag.** Click selects, ⌘/Ctrl-C copies, V or ⌘V pastes after the selection (three presses give three copies), digits 0–9 send to dock slots, `G` + number + Enter moves to a position, `B`/`N` preview the seam before or after the focused track, `E` jumps to 10 s before the end.
5. **Badges are rare and quiet.** They appear only in a small section at the bottom of a profile, plus one understated toast when you unlock one. They never appear in rows, cards, headers, curator lists or activity feeds.
6. **Architecture:** no-build ES modules, vendored `lit-html@3.2.1` (auto-escaping templates, keyed `repeat`), a strict CSP (`script-src 'self'` + YouTube), one delegated `data-action` listener, `pushState` paths, optimistic edits reconciled from `/api/ops` responses, and no whole-page refetch after an edit.
7. **Word list.** Say "preview", never the other word for it. Say "crate", "playlist", "curator", "track". Sentence case everywhere. No exclamation marks. No emoji in UI chrome (badge glyphs from the catalog are the only exception, and only in the badge section and toast).

### 0.1 Lane order

| Step | Lane | Runs | Owns |
|---|---|---|---|
| 1 | **F-back** (Foundation backend) | first, in parallel with F-front | `app/discovery.py`, additive changes to `app/main.py`, `app/auth.py`, `app/database.py`, new backend tests |
| 1 | **F-front** (Foundation frontend) | first, in parallel with F-back | shell, tokens, vendor, core modules, player, home, read-only playlist view, dock and crate shells, the safety test rewrite |
| 2 | **A Editing** | after F merges, in parallel with B and C | selection, clipboard, keys, drag, move-to, undo UI, playlist editing |
| 2 | **B Discovery** | after F merges | track, profile, discover, search views, related panel, likes, comments, badges |
| 2 | **C Import/Share/Crate** | after F merges | import sheet, paste-anywhere, crate unload/promote, share, fork, account pill, handle prompt, demo personas, mobile polish |
| 3 | **Cutover** | after A, B and C merge | delete the legacy UI, retarget the remaining tests (§7) |

F-back and F-front touch disjoint files and share only this document, so they can be built by two agents at once. F-front may stub any F-back endpoint with a fixture in `static/js/dev-fixtures.js` (deleted before F merges).

---

## 1. Visual system

### 1.1 Fonts (self-hosted, CSP-strict)

No Google Fonts. Fonts are vendored in `static/vendor/fonts/` with their OFL licence files, so `font-src 'self'` and `style-src 'self'` hold.

| Family | Use | Source file (copy byte-for-byte from jsDelivr) |
|---|---|---|
| Instrument Sans (variable, wght 400–700) | all UI text | `https://cdn.jsdelivr.net/npm/@fontsource-variable/instrument-sans@5.1.0/files/instrument-sans-latin-wght-normal.woff2` → `instrument-sans.woff2` |
| Instrument Serif 400 | page titles only (`h1` of a playlist, profile, track, and the home greeting) | `https://cdn.jsdelivr.net/npm/@fontsource/instrument-serif@5.1.0/files/instrument-serif-latin-400-normal.woff2` → `instrument-serif.woff2` |
| JetBrains Mono (variable) | positions, durations, clip ranges, counts, keycaps | `https://cdn.jsdelivr.net/npm/@fontsource-variable/jetbrains-mono@5.1.1/files/jetbrains-mono-latin-wght-normal.woff2` → `jetbrains-mono.woff2` |

These URLs were checked and return 200 (30 KB, 21 KB and 40 KB). `@font-face` uses `font-display: swap`. Each family falls back to a system stack, so the page works if the files fail to load.

### 1.2 Tokens (`static/styles/tokens.css`)

Copy this block as written. Components must use tokens, never raw colours.

```css
:root {
  color-scheme: light;
  /* surfaces */
  --bg: #faf9f6;          /* page */
  --surface: #ffffff;     /* popovers, sheets, hovered row */
  --surface-2: #f3f1ec;   /* rails, crate panel, input wells */
  --line: #e8e4db;        /* hairlines between rows */
  --line-strong: #d4cec1; /* inputs, chip outlines */
  /* ink */
  --ink: #1d1c1a;
  --ink-2: #55524b;       /* secondary: channel, byline */
  --ink-3: #75716a;       /* tertiary: hop links, counts, labels (4.6:1 on --bg) */
  /* the one accent: links, focus, selection, drop */
  --accent: #3552b8;
  --accent-ink: #ffffff;
  --accent-soft: #edf0fa;  /* selected row */
  --accent-line: #b9c4ea;  /* selected row hairline */
  /* state colours, used sparingly and never as decoration */
  --play: #2c7a5b;   --play-soft: #e6f2ec;   /* now playing */
  --crate: #946a22;  --crate-soft: #f6efe1;  /* crate chip dot, crate panel header */
  --new-soft: #fbf4d9;                       /* "just landed" wash, fades out */
  --danger: #b3261e;
  /* elevation: only popovers, sheets and toasts */
  --shadow-pop: 0 1px 2px rgba(29,28,26,.06), 0 8px 28px -10px rgba(29,28,26,.22);
  /* type */
  --f-ui: "Instrument Sans", ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  --f-title: "Instrument Serif", ui-serif, Georgia, "Times New Roman", serif;
  --f-mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  --fs-xs: 11.5px;  /* mono labels, keycaps, section labels */
  --fs-sm: 12.5px;  /* meta lines, buttons, chips */
  --fs-base: 14px;  /* body, track titles */
  --fs-md: 16px;    /* card titles, section headings */
  --fs-lg: 20px;    /* sheet titles */
  --fs-xl: 32px;    /* page titles (serif); 26px under 760px */
  --lh-tight: 1.25; --lh: 1.5;
  /* spacing (4px grid) */
  --sp-1: 4px; --sp-2: 8px; --sp-3: 12px; --sp-4: 16px; --sp-5: 24px; --sp-6: 32px; --sp-7: 48px; --sp-8: 64px;
  /* radii */
  --r-xs: 3px;  /* thumbnails, clip chip */
  --r-s: 5px;   /* buttons, inputs, chips */
  --r: 8px;     /* popovers, sheets, cards */
  /* layout */
  --topbar-h: 56px; --player-h: 60px; --rail-l: 232px; --rail-r: 340px; --content-max: 720px;
  --row-h: 60px; --thumb: 48px;
  /* motion */
  --ease: cubic-bezier(.2, 0, 0, 1);
  --dur-1: 120ms;  /* hover, press */
  --dur-2: 200ms;  /* popovers, panels */
  --dur-land: 1600ms; /* the "just landed" wash */
}

@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --bg: #141413; --surface: #1c1c1a; --surface-2: #222220;
    --line: #2b2a27; --line-strong: #3b3a36;
    --ink: #ecebe7; --ink-2: #b4b1a9; --ink-3: #8b887f;
    --accent: #93a8ff; --accent-ink: #0e1430; --accent-soft: #1d2340; --accent-line: #34407a;
    --play: #6fc7a1; --play-soft: #152a21;
    --crate: #e0b360; --crate-soft: #2c2414;
    --new-soft: #2e2913;
    --danger: #f2867d;
    --shadow-pop: 0 1px 2px rgba(0,0,0,.4), 0 10px 30px -10px rgba(0,0,0,.7);
  }
}
:root[data-theme="dark"] { /* the same dark values, repeated verbatim */ }
```

- The theme toggle (account menu) cycles system → light → dark and writes `data-theme` on `<html>`; the choice is kept in `localStorage["webjam.theme"]` (wrapped in try/catch).
- `body { background: var(--bg); color: var(--ink); font: var(--fs-base)/var(--lh) var(--f-ui); }`. Numbers in mono use `font-variant-numeric: tabular-nums`.
- Links: `--accent` in body copy, no underline until hover. Hop links inside rows and cards are `--ink-3` and turn `--accent` on hover.
- Focus: `:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }`. Never remove focus outlines.

### 1.3 Layout

| Width | Layout |
|---|---|
| ≥ 1100px | Top bar (56px). Left rail 232px: the **dock**. Centre column: content, max 720px, centred in the remaining space with at least 48px side margins. Right rail 340px: **now playing** (video tile 340×191, which is ≥200px tall) above the context panel (related playlists, queue). Player bar 60px fixed at the bottom. |
| 760–1099px | No right rail. The dock rail narrows to 200px. The video tile floats bottom-right above the player bar at 356×200, and can be collapsed only while paused. The related panel renders inline under the playlist header. |
| < 760px | Single column with 16px gutters. The dock becomes a 48px horizontal strip of chips above the 60px player bar (108px in total, under the 120px budget). The crate is a bottom sheet. The video tile sits above the dock strip at full width × 200px while playing and collapses to nothing when paused. No horizontal page scroll. |

YouTube requires a visible player of at least 200×200 CSS pixels while playing, and no overlay on top of it. Hiding the video pauses playback; there is no "audio only" mode that hides a playing player.

Whitespace rule: a list never touches the edge of its column. Sections are separated by `--sp-7` (48px) on desktop and `--sp-6` on phones. Section headings are `--fs-xs` mono, uppercase, letter-spacing .06em, `--ink-3`, with `--sp-3` below. This is the only uppercase in the UI.

### 1.4 Track row anatomy (`components/track-row.js`, the only place a row is built)

```
┌──────┬──────┬───────────────────────────────────────────────┬───────────┬───────┬──────────┐
│  12  │ ▢48  │ Title of the track (one line, ellipsis)       │ ✂ 5:12–8:18│  4:31 │ ▶  +  ⋯  │
│ mono │thumb │ Channel · via @maya · in 4 playlists          │ clip chip  │ mono  │ actions  │
└──────┴──────┴───────────────────────────────────────────────┴───────────┴───────┴──────────┘
```

- Grid: `grid-template-columns: 28px var(--thumb) minmax(0,1fr) auto 44px auto; column-gap: var(--sp-3); align-items: center; min-height: var(--row-h); padding: 6px var(--sp-2); border-bottom: 1px solid var(--line);`. Phones drop the position column and the duration (shown in the meta line instead).
- **Position**: mono `--fs-xs`, `--ink-3`, right-aligned. While playing it is replaced by a small play glyph in `--play`.
- **Thumbnail**: 48×48, `object-fit: cover`, `border-radius: var(--r-xs)`, `background: var(--surface-2)`, `loading="lazy" decoding="async" alt="" draggable="false"`. Source: `songs.thumbnail_url`.
- **Title**: `--fs-base`, weight 500, `--ink`, one line with ellipsis. It is a link to `/t/{video_id}` (with `?s=&e=` when clipped). A `display_title` wins over the song title (the API already does this).
- **Meta line**: `--fs-sm`, `--ink-2` for the channel; then hop links in `--ink-3`, separated by ` · `:
  - `via @user` → `/u/{handle}` when `source_username` is set;
  - `in N playlists` → `/t/{video_id}` when `playlist_count ≥ 2` (the row's own playlist counts as one);
  - in the crate only: `from 'Playlist title'` → `/p/{id}` when `source_playlist_id` is set;
  - the track note, if any, appears as a second meta line in regular `--ink-2`, clamped to 2 lines.
- **Clip chip**: shown only when `start_seconds` or `end_seconds` is set: mono `--fs-xs`, `--ink-2`, 1px `--line-strong` border, `--r-xs`, text `5:12–8:18` (open ends render as `5:12–end` / `0:00–8:18`).
- **Duration**: mono `--fs-xs`, `--ink-3`. Known only after the player has loaded the video once: `player/durations.js` keeps a `video_id → seconds` cache in `localStorage["webjam.durations"]` (try/catch, capped at 2000 entries). Unknown is an empty cell. For a clip it shows the clip length. (Phase 2 adds `songs.duration_seconds`.)
- **Actions**: three 28px icon buttons: play, add to crate (+), more (⋯). Opacity 0 until `:hover`, `:focus-within` or `.is-selected`; on `(pointer: coarse)` only ⋯ is shown, always. Each is a `<button data-action="…">` with an `aria-label`.
- **Attributes on the `<li>`**: `data-track-id`, `data-playlist-id`, `data-video-id`, `data-index` (0-based), `role="option"`, `aria-selected`, `tabindex="-1"` (the roving cursor gets `tabindex="0"`).

Row states (class on the `<li>`; at most one background colour at a time, priority top to bottom):

| State | Class | Look |
|---|---|---|
| Dragging source | `.is-dragging` | opacity .4 |
| Selected | `.is-selected` | background `--accent-soft`, a 2px inset left bar in `--accent` (`box-shadow: inset 2px 0 0 var(--accent)`), hairline `--accent-line` |
| Cursor (keyboard focus, not selected) | `.is-cursor` | inset 1px `--line-strong` ring; plus the normal `:focus-visible` outline when it has DOM focus |
| Just landed | `.is-new` | background `--new-soft` that fades to transparent over `--dur-land`; the class is removed after the animation |
| Playing | `.is-playing` | title in `--play`; position cell shows the play glyph |
| Cut pending | `.is-cut` | opacity .55, title struck through in `--ink-3` |
| Hover | `:hover` | background `--surface` |
| Unplayable | `.is-dead` | title `--ink-3`, a quiet "unavailable" label in the meta line |

Insertion cursor (where V pastes and where a drop lands): a 2px `--accent` line across the row gap with a 6px circle at its left end, drawn by a single absolutely positioned element in the list (`.insert-line`), not by row borders.

Drop target states: a dock chip or crate chip under a drag gets `--accent-soft` background and a 1px `--accent` outline (`.is-drop`). A list gap under a drag shows the insertion line. Nothing else moves; rows don't animate apart.

Crate visuals: the crate chip has a 6px `--crate` dot before its label. The crate panel header uses `--crate-soft`. Crate rows look like any other row; their meta line shows `from …`. Nothing else is ochre.

### 1.5 Other components (F-front builds these in `styles/components.css`)

- **Buttons**: text buttons with 1px `--line-strong` border, `--r-s`, 30px tall, `--fs-sm`, weight 500. Primary (one per view at most, e.g. "Play"): `--accent` background, `--accent-ink` text. Icon buttons: 28px square, no border, `--ink-2`, hover background `--surface-2`.
- **Playlist card** (`components/playlist-card.js`): title (`--fs-md`, 500, link to `/p/{id}`), then the meta line in `--ink-3` mono `--fs-xs` ("14 tracks · 3 related · updated 2d"), curator (`by @maya`, link) unless the card is on that curator's own page, the description clamped to 2 lines in `--ink-2`, then up to 3 **preview tracks** as compact rows (32px tall, 32px thumbnail, title + channel on one line, a play button). A visibility tag (`unlisted` / `private`, mono `--fs-xs`, outlined) appears only to the owner. Cards are separated by hairlines, not boxes: no card backgrounds and no shadows.
- **Chips** (dock): 32px tall, `--r-s`, label with ellipsis, a mono slot number on the left in `--ink-3`, the track count on the right in mono. Hover `--surface`.
- **Popover** (`ui/popover.js`): `--surface`, 1px `--line`, `--r`, `--shadow-pop`, 8px padding, anchored to its trigger, closes on Esc, outside click and route change. One popover at a time.
- **Sheet** (`ui/sheet.js`): a centred dialog (desktop, max 560px) or a bottom sheet (phones), using `<dialog>` with `showModal()`; Esc closes; focus returns to the trigger.
- **Toast** (`ui/toast.js`): bottom-left above the player bar (bottom-centre on phones), `--surface`, 1px `--line`, `--shadow-pop`, `--fs-sm`. One line of text plus at most one action ("Undo", "Open"). 4 s, paused on hover. At most 2 stacked; older ones are dropped.
- **Avatar** (`components/avatar.js`): a circle of initials (24px in headers, 20px inline). Colour comes from a class: `avatar_color` is mapped to `.av-0`…`.av-5` by its index in the fixed server palette (`#1f5f5b #8c3b2b #3d4e81 #6b4226 #4a6b2c #7a3b69`); anything else is `.av-x` (`--ink-3`). There is no `style` attribute (see §4.6).
- **Icons**: one inline SVG sprite of `<symbol>`s at the top of `index.html` (static markup, no data), used as `<svg class="i"><use href="#i-play"></use></svg>`. 16px, 1.5px stroke, `currentColor`. Set: play, pause, prev, next, plus, crate, more, share, heart, heart-fill, undo, redo, close, search, chevron-down, grip, link, copy, seam, check, sun, moon.

### 1.6 Motion

- Hover and press: `background-color`/`color`/`opacity` over `--dur-1`. Popovers and sheets: opacity plus a 4px translate over `--dur-2`. The "just landed" wash: a background fade over `--dur-land`. That is the complete list of animations.
- No layout animation of lists. Rows don't slide on reorder; the moved row gets the "just landed" wash at its new place.
- The playing indicator is a static glyph, not animated bars.
- `@media (prefers-reduced-motion: reduce)`: every transition and animation duration becomes `0.01ms`; `.is-new` shows a static `--new-soft` background for 1.6 s and is then removed by JS; the scroll-into-view calls use `behavior: "auto"`.

### 1.7 Badges

- **Where**: only (a) the "Badges" section at the bottom of a profile (`/u/{handle}`), and (b) the unlock toast. Nowhere else: not in rows, cards, top bar, curator lists, the dock, the player, the activity feed (the server filters `earned_badge` lines out of home and discover activity, §3) or the old leaderboard.
- **Profile section** (B): heading "Badges" (section label style). Earned badges are a wrapped line of small items: the catalog `icon` glyph at 14px in `--ink-2`, then the name in `--fs-sm`, separated by generous spacing, each with a `title` tooltip holding the description and `context_note`. Under it, one `--ink-3` link: "11 more to find". It toggles a quiet list of locked badges showing **name and description only** (no progress bars, no thresholds, no percentages). The owner sees the same list; there is no separate progress UI.
- **Unlock toast**: every write response that carries `new_badges` passes them to `toast.badge(badge)`. The toast reads `Unlocked · <icon> <name>`, has a "View" action linking to `/u/{me}#badges`, and stays 6 s. If several arrive at once, one toast says `Unlocked · <first name> and 2 more`. No sound, no confetti, no colour other than the normal toast surface.
- Phase 1b adds no new badges. The catalog stays as `app/badges.py` defines it.

---

## 2. Information architecture and routes

### 2.1 Routes

The server serves `static/index.html` for every page route below (F-back adds the missing ones). The SPA router (`router.js`) maps `location.pathname` to a view. Unknown paths that the server does serve render `views/not-found.js`.

| Path | Server route | View | Who |
|---|---|---|---|
| `/` | exists | signed in: `views/home.js` (**my page**); anonymous: `views/discover.js` | all |
| `/discover` | **new** | `views/discover.js` (`?tab=playlists|tracks|people`, `?sort=fresh|popular`) | all |
| `/search` | **new** | `views/search.js` (`?q=`) | all |
| `/p/{id}` and `/p/{id}-{slug}` | exists | `views/playlist.js` (`?track={tid}` selects and scrolls to that row) | all (visibility rules) |
| `/t/{video_id}` | exists | `views/track.js` (`?s=&e=` sets the clip when played from this page) | all (song must be visible) |
| `/u/{handle}` | **new** (`^[a-z0-9_]{3,20}$`) | `views/profile.js` | all |
| `/crate` | **new** | `views/playlist.js` on the viewer's crate (anonymous: sign-in prompt) | owner |
| `/legacy/` | **new**, removed at cutover | the old UI, moved to `static/legacy/` (§7) | all |

Slugs: `/p/{id}-{slug}` where `slug` is the title lowercased, non-alphanumerics collapsed to `-`, trimmed to 60 characters. The router always accepts a missing or stale slug and calls `history.replaceState` with the current one.

### 2.2 Old links

At boot, before the first render, `router.js` rewrites legacy hashes with `history.replaceState`:

| Old | New |
|---|---|
| `#feed`, `#` , empty | `/` |
| `#playlist/{id}` | `/p/{id}` |
| `#song/{vid}` | `/t/{vid}` |
| `#user/{id}` | fetch `/api/users/{id}`, then `/u/{username}` (404 → `/`) |
| `#songs` | `/discover?tab=tracks` |
| `#curators` | `/discover?tab=people` |
| `#badges` | `/u/{me}#badges` when signed in, else `/discover` |
| `#not-invited` | `/` with a notice: "That Google account isn't on the invite list." |
| `#auth-error` | `/` with a notice: "Sign-in didn't complete. Try again." |

`/u/{handle}` with a handle the user gave up resolves to the user's current handle (the API answers with the current `username`), and the router replaces the URL.

### 2.3 The shell

```
┌ top bar ───────────────────────────────────────────────────────────────────────┐
│ webjam   [ Search or paste a link…  / ]                  Discover   (account)  │
├ dock (left rail) ─┬ content ───────────────────────────────┬ right rail ───────┤
│ 0 ● Crate      12 │                                        │ [ video tile ]    │
│ 1 Basement V.  14 │   view                                 │ now playing       │
│ 2 Late Shift    9 │                                        │ ─────────────     │
│ …                 │                                        │ context panel     │
│ + New playlist    │                                        │ (related / queue) │
├───────────────────┴────────────────────────────────────────┴───────────────────┤
│ player bar:  ◀◀  ▶  ▶▶   Title — Channel   ───●──────── 1:12 / 4:31   seam   ♡  │
└────────────────────────────────────────────────────────────────────────────────┘
```

- **Top bar** (F): lowercase wordmark `webjam` (links to `/`), the omnibox (an `<input type="search">`; Enter goes to `/search?q=`; pasting a URL into it is handled by the paste router like any paste, §5.4), a "Discover" text link, and the account slot (C fills it; F renders a plain "Sign in" link until then). In demo mode a thin `--surface-2` strip above the top bar reads "Demo mode: anyone can act as anyone."
- **Dock** (F shell): shown only when signed in. Anonymous visitors get no left rail; the content column centres.
- **Player bar** (F): always present once something has played; hidden before that.

### 2.4 Views and their hop links

Each view lists its sections in order. "Hop" marks the discovery links that must be present.

**Home, signed in (`/`, `views/home.js`, F)**, one call: `GET /api/me/home`.
1. Handle prompt card when `needs_handle` (C fills the `home.top` slot; F leaves the slot).
2. Title row: serif page title = your display name; right side a quiet `+ New playlist` button.
3. **Your playlists**: every playlist you own (not the crate), newest edit first, as playlist cards with 3 preview tracks each. Unlisted and private show their tag. Hop: `N related` in each card's meta line → `/p/{id}#related`.
4. **Crate**: count, the 5 newest crate rows as compact rows, "Open crate" (opens the crate panel). Hidden when the crate is empty; then one `--ink-3` line says "Paste a YouTube link anywhere to start collecting."
5. **From people who share your taste**: up to 10 recent tracks added to listed playlists by your taste neighbours that you don't have yet, as track rows (hop: `via` the neighbour's playlist, `in N playlists`).
6. **Taste neighbours**: up to 6 people with overlap counts ("@maya · 7 shared"). Hop → `/u/{handle}`.
7. **About you**: recent grabs of your tracks ("@lucas took 'X' into 'Y'") and comments on your playlists. Hop → the target playlist and the person.
8. **Activity**: up to 20 lines from your neighbours, plain text with links, timestamps in mono `--ink-3`.
- New user with no playlists: section 3 shows one line, "No playlists yet.", with two buttons: "New playlist" and "Import from YouTube" (C), and section 5–8 fall back to the discover feed's first page under the heading "Start here".

**Discover (`/discover`, and `/` when anonymous; `views/discover.js`, B)**: tabs Playlists · Tracks · People (underline style). Playlists: sort `fresh` (default) / `popular`, cards with 3 preview tracks, "More" button paging by 20. Tracks: recent discoveries (`songs` with a first finder), each row hop `found by @x` and `in N playlists`. People: curators with playlist counts (no badges). Right rail on desktop: recent activity (no badge lines). Anonymous visitors also see, above the tabs, one sentence: "Playlists of YouTube music, made by people. Press play; no account needed."

**Playlist (`/p/{id}`, `views/playlist.js`, F then A)**:
1. Header: serif title; byline `by @maya` (hop → `/u/maya`) · `14 tracks` · `updated 2d` · visibility tag for the owner; `forked from 'X' by @lucas` when present (hop); description.
2. Actions: Play (primary), ♡ (B, slot `playlist.actions`), Share (C), ⋯ menu: Copy all / Fork (C), Edit details, Delete (A, owner only).
3. **"N playlists share these tracks"**: one `--ink-3` line under the header when `related_count > 0` (hop). On wide screens it opens the related panel in the right rail; otherwise it expands inline. B renders the panel through slot `playlist.related`.
4. Tracks (`<ol role="listbox">` of track rows). The owner edits in place (A); others can select, copy, drag out and add to crate.
5. Comments (B, slot `playlist.footer`). The crate shows none of 2's ♡/Share/Fork, none of 3 and none of 5.

**Track (`/t/{video_id}`, `views/track.js`, B)**, `GET /api/songs/{video_id}`:
1. Header: 48px thumbnail, serif title, channel, `first found by @x in 'Playlist'` (hops). Actions: Play, + Crate, ♡, Share (C's popover in "track" mode).
2. **In N playlists**: one compact card per playlist (title, curator, position "#4", the track note from that playlist). Hops → playlist, curator.
3. **Curators**: avatars + handles with how many of their playlists include it. Hop → profile.
4. **Often alongside**: up to 10 tracks that share playlists with this one, with "together in 3" counts. Hop → `/t/{vid}`.
5. Comments.

**Profile (`/u/{handle}`, `views/profile.js`, B)**, `GET /api/users/by-handle/{handle}`:
1. Header: avatar (24px), serif display name, `@handle`, bio, a mono `--ink-3` line `12 playlists · since Nov 2026`. When the viewer is signed in and is someone else: "You share 7 tracks" (hop: expands the sample of shared tracks).
2. Playlists (cards with previews; the owner also sees unlisted and private, tagged; never the crate).
3. **Taste neighbours** (hop → profiles).
4. Discovered (songs this person found first), Loved (tracks and playlists), Lineage: "took tracks from @a, @b" / "inspired @c, @d" (hops).
5. Guestbook (comments on the profile).
6. Badges (§1.7). The owner also gets "Edit profile" (display name, bio) in the header ⋯.

**Search (`/search?q=`, `views/search.js`, B)**, `GET /api/search`: three groups (Playlists, Tracks, People), 10 each with "More". When signed in, a fourth quiet section "On YouTube" with a button "Search YouTube for 'q'" that calls `/api/youtube/search` only on click (it costs quota), each result a track row with + Crate.

---

## 3. Backend contract (F-back)

All new code lives in **`app/discovery.py`** (query functions that take `conn`, `viewer` and return dicts) with thin endpoints in `app/main.py`. Every query uses the `app/visibility.py` predicates (`listed`, `visible`, `linkable`, `require_visible_playlist`, `song_visible`). Crates never appear in any response here except the owner's own `crate` field in `/api/me/home`.

Common rules:
- Reads use `resolve_viewer(request, user_id)`; anonymous viewer is `0`. Demo mode passes `user_id` as a query parameter, like the other endpoints.
- "Listed" below means `LISTED(p)`. "Own" means `p.user_id = viewer AND p.kind = 'playlist'` (any visibility, never the crate).
- Errors use `ApiError(status, code, detail)` as in Phase 1a. 404 for anything not visible.
- New rate-limit bucket `"find": (60, 1.0)` charged by `/api/search` and `/api/discover`.
- Response timestamps are the stored `YYYY-MM-DD HH:MM:SS` UTC strings, as today.

### 3.1 Shared shapes

```jsonc
// UserMini
{ "id": 3, "username": "maya", "display_name": "Maya Okafor", "avatar_color": "#3d4e81" }

// TrackMini (preview rows on cards and home lists)
{ "id": 4, "video_id": "c_56qQqxvm4", "position": 2, "title": "Tycho - Full Performance (Live on KEXP)",
  "channel_title": "KEXP", "thumbnail_url": "https://i.ytimg.com/vi/c_56qQqxvm4/hqdefault.jpg",
  "start_seconds": null, "end_seconds": null }

// PlaylistCard
{ "id": 2, "kind": "playlist", "visibility": "public", "version": 3,
  "title": "2AM Terminal Glow (Coding Mix)", "description": "…",
  "track_count": 12, "like_count": 4, "plays": 31, "related_count": 3,
  "updated_at": "2026-11-11 09:58:00", "user": { /* UserMini */ },
  "preview": [ /* up to 3 TrackMini */ ] }
```

- **`preview`**: the playlist's 3 "best" tracks, Webjay style: rank rows by `(SUM(song_likes.count) DESC, songs.total_plays DESC, position ASC)`, take 3, then return them **in playlist order**. A playlist with ≤3 tracks returns all of them.
- **`related_count`**: the number of *other* listed playlists that share at least one `video_id` with this one:
  ```sql
  SELECT COUNT(DISTINCT p2.id)
  FROM playlist_tracks a JOIN playlist_tracks b ON b.video_id = a.video_id
  JOIN playlists p2 ON p2.id = b.playlist_id
  WHERE a.playlist_id = :pid AND p2.id != :pid AND {listed('p2')}
  ```
  Uses the existing `idx_pt_video`. It counts other people's and the same curator's playlists alike.
- `visibility` is always present; only the owner can ever receive a non-public value (other viewers only get listed playlists in lists, or open an unlisted one by id).

### 3.2 `GET /api/me/home` (new)

Signed in only: `401 signed_out` in google mode without a session; demo mode takes `user_id`.

```jsonc
{
  "user": { /* UserMini */ "bio": "…", "needs_handle": false },
  "playlists": [ /* PlaylistCard for every playlist I own with kind='playlist', any visibility,
                    ORDER BY updated_at DESC, id DESC */ ],
  "crate": { "id": 5, "version": 7, "track_count": 2,
             "recent": [ /* up to 5 TrackMini, newest created_at first, plus "source_username" */ ] },
  "dock": { /* exactly the GET /api/me/dock shape */ },
  "undo": { /* exactly the GET /api/me/undo shape */ },
  "neighbours": [ { "user": { /* UserMini */ }, "shared_count": 7,
                    "sample": [ { "video_id": "…", "title": "…", "thumbnail_url": "…" } ] } ],   // ≤ 6
  "fresh": [ { "track": { /* TrackMini */ }, "playlist": { "id": 9, "title": "…" }, "user": { /* UserMini */ },
               "added_at": "…", "playlist_count": 3 } ],   // ≤ 10
  "about_you": [
    { "type": "grab", "at": "…", "user": { /* UserMini */ }, "video_id": "…", "title": "…",
      "playlist": { "id": 12, "title": "…" } },
    { "type": "comment", "at": "…", "user": { /* UserMini */ }, "body": "…",
      "playlist": { "id": 4, "title": "…" } }
  ],   // ≤ 10, newest first
  "activity": [ /* activity_log rows as /api/feed returns them */ ]   // ≤ 20
}
```

Rules:
- **Taste neighbours (`neighbours`)** for the home page: my side is every playlist I own with `kind='playlist'` (any visibility; only I see the result); their side is **listed** playlists of other users. Rank by distinct shared `video_id`s, ties by most recent shared placement. `sample` is up to 3 shared tracks. Users with 0 shared are omitted.
  ```sql
  WITH mine AS (SELECT DISTINCT pt.video_id FROM playlist_tracks pt JOIN playlists p ON p.id = pt.playlist_id
                WHERE p.user_id = :me AND p.kind = 'playlist')
  SELECT p.user_id, COUNT(DISTINCT pt.video_id) AS shared_count, MAX(pt.created_at) AS last_at
  FROM playlist_tracks pt JOIN playlists p ON p.id = pt.playlist_id JOIN mine m ON m.video_id = pt.video_id
  WHERE p.user_id != :me AND {listed('p')}
  GROUP BY p.user_id ORDER BY shared_count DESC, last_at DESC LIMIT :n
  ```
- **`fresh`**: rows in listed playlists owned by my top 20 neighbours, whose `video_id` is not in any of my `kind='playlist'` playlists **or my crate** (the crate is mine; using it here leaks nothing), newest `created_at` first, one row per `video_id`, at most 10.
- **`about_you`**: grabs = rows with `source_user_id = me AND added_by_user_id != me` in **listed** target playlists (the profile's `grabs_from_user` query); comments = comments on my playlists (`kind='playlist'`) by other users. Merge, newest first, 10.
- **`activity`**: the `/api/feed` activity query (same visibility filters) restricted to `actor_user_id IN (my top 20 neighbours)` and `action_type != 'earned_badge'`, limit 20. If I have no neighbours, use the global feed activity with the same badge filter.
- `needs_handle` (also added to `current_user` in `/api/bootstrap`): true only when auth mode is google, the user is signed in, and `users.handle_chosen_at IS NULL` (§3.8).

### 3.3 `GET /api/discover` (new)

Anonymous OK. Query: `tab=playlists|tracks|people` (default `playlists`), `sort=fresh|popular` (playlists only, default `fresh`), `offset` (default 0, ≥0), `limit` (default 20, 1–50). Charged to the `find` bucket.

```jsonc
// tab=playlists
{ "tab": "playlists", "sort": "fresh", "offset": 0, "next_offset": 20,      // null when exhausted
  "playlists": [ /* PlaylistCard, listed only */ ],
  "activity": [ /* only when offset == 0: global feed activity, no earned_badge, ≤ 15 */ ] }
// tab=tracks
{ "tab": "tracks", "offset": 0, "next_offset": 20,
  "tracks": [ { "video_id": "…", "title": "…", "channel_title": "…", "thumbnail_url": "…",
                "first_added_at": "…", "finder": { /* UserMini */ } | null,
                "debut_playlist": { "id": 2, "title": "…" } | null, "playlist_count": 3, "like_count": 2 } ] }
// tab=people
{ "tab": "people", "offset": 0, "next_offset": null,
  "people": [ { "user": { /* UserMini */ }, "bio": "…", "playlist_count": 4, "track_count": 51,
                "last_active_at": "…" } ] }
```

- Playlists: `WHERE listed(p)`. `fresh` = `ORDER BY p.updated_at DESC, p.id DESC`. `popular` = `ORDER BY (p.plays + 3 * like_count) DESC, p.updated_at DESC`. Empty playlists are skipped.
- Tracks: the `/api/feed` `recent_songs` query (it already applies `song_visible` for the viewer and `LINKABLE` for the debut title), paged.
- People: users with at least one listed playlist, `ORDER BY last_active_at DESC` (max `updated_at` of their listed playlists). Counts are listed-only. **No badges in this payload.**

`/api/feed` stays as it is until cutover (tests use it); nothing in the new UI calls it.

### 3.4 `GET /api/search` (new)

Anonymous OK. Query: `q` (required, 1–100 characters after trimming and collapsing whitespace, else 422), `type=all|playlists|tracks|people` (default `all`), `limit` (default 10, 1–25 per group). Charged to the `find` bucket.

Matching: case-insensitive substring with SQLite `LIKE :pat ESCAPE '\'`, where `:pat = '%' + q with \ % _ escaped + '%'`.

```jsonc
{ "query": "tycho",
  "playlists": [ /* PlaylistCard, listed only, matching title or description, updated_at DESC */ ],
  "tracks": [ { "video_id": "…", "title": "…", "channel_title": "…", "thumbnail_url": "…", "playlist_count": 3 } ],
  "people": [ { "user": { /* UserMini */ }, "playlist_count": 4 } ] }
```

- Tracks: `songs` matching title or channel, filtered with `_song_visible_sql('s.video_id', viewer)` (a listed placement, or one in the viewer's own playlists including their crate; only the viewer sees their own). Order: `playlist_count DESC, title`. Also match `playlist_tracks.display_title` only within listed playlists.
- People: `username` or `display_name` match, users with ≥1 listed playlist, or the viewer themself.
- Groups not requested by `type` are returned as empty arrays.

### 3.5 `GET /api/playlists/{id}/related` (new)

The Webjay "shares these links" view. Gate: `require_visible_playlist(conn, id, viewer)` (404 otherwise). This works for the owner's private playlists and crate too, because the output only names **listed** playlists and the viewer can already see the source.

Query: `limit` (default 12, 1–50), `offset` (default 0).

```jsonc
{ "playlist_id": 2, "total": 7, "next_offset": null,
  "related": [
    { "playlist": { /* PlaylistCard without "preview" */ },
      "shared_count": 3,
      "shared": [ { "video_id": "…", "title": "…", "thumbnail_url": "…", "position": 5 } ]  // ≤ 4, in the related playlist's order
    } ] }
```

```sql
WITH mine AS (SELECT DISTINCT video_id FROM playlist_tracks WHERE playlist_id = :pid)
SELECT p2.id, COUNT(DISTINCT pt.video_id) AS shared_count
FROM playlist_tracks pt JOIN mine m ON m.video_id = pt.video_id
JOIN playlists p2 ON p2.id = pt.playlist_id
WHERE p2.id != :pid AND {listed('p2')}
GROUP BY p2.id
ORDER BY shared_count DESC, p2.updated_at DESC, p2.id DESC
LIMIT :limit OFFSET :offset
```

`total` = `related_count`. `GET /api/playlists/{id}` gains `related_count` (same definition) so the header line renders without a second request.

### 3.6 `GET /api/songs/{video_id}` (extended)

Unchanged gate (`song_visible`, else 404) and unchanged existing fields (`appearances` stays for back-compat). Added:

```jsonc
{
  "playlist_count": 3,                      // distinct listed playlists
  "playlists": [ { "playlist": { /* PlaylistCard without "preview" */ },
                   "occurrences": 1, "first_position": 4, "note": "the build at 5:10" } ],
  "curators": [ { "user": { /* UserMini */ }, "playlist_count": 2 } ],
  "alongside": [ { "video_id": "…", "title": "…", "channel_title": "…", "thumbnail_url": "…", "together_count": 3 } ]
}
```

- `playlists`: grouped from the same rows as `appearances` (listed, or own with `kind='playlist'`), ordered listed first, then `updated_at DESC`. `note` is the first non-empty `track_note` in that playlist.
- `curators`: owners of **listed** playlists containing the song, by `playlist_count DESC`. Own private playlists never contribute.
- `alongside`: songs co-occurring in **listed** playlists that contain this song, excluding itself, `together_count` = distinct shared listed playlists, top 10 by `together_count DESC, title`. Each must itself be in a listed playlist (true by construction).

### 3.7 Profiles (extended)

- **New** `GET /api/users/by-handle/{handle}`: `handle` must match `^[a-z0-9_]{3,20}$` (lowercased) else 404. Looks up `users.username`; if absent, looks up `handle_history.handle` and uses that row's `user_id`. Returns exactly the `GET /api/users/{id}` payload. The client reads `user.username` to fix the URL.
- `GET /api/users/{id}` accepts `user_id` as an alias of `viewer_id` (the new client always sends `user_id`; `viewer_id` keeps working).
- Both add:
  ```jsonc
  { "neighbours": [ /* as in /api/me/home, but BOTH sides listed-only, because others see this */ ],   // ≤ 8
    "shared_with_viewer": { "shared_count": 7, "sample": [ /* ≤ 5 TrackMini-like {video_id,title,thumbnail_url} */ ] } | null }
  ```
  `shared_with_viewer` is null for anonymous viewers and on your own profile. Otherwise it is the overlap between the viewer's own `kind='playlist'` playlists (any visibility; the result goes only to the viewer) and the profile user's **listed** playlists.
- Each item in the profile's `playlists` gains `related_count` and `preview` (the PlaylistCard fields). The existing `tracks` arrays stay until cutover; the new UI ignores them.
- The `badge_catalog`, `earned_badges` and `metrics` fields stay as they are.

### 3.8 Handle confirmation

- Schema: `users.handle_chosen_at TEXT NULL`. Added to `SCHEMA_SQL` and, for existing databases, by a guarded `ALTER TABLE users ADD COLUMN handle_chosen_at TEXT` in `init_db()` that runs only when `PRAGMA table_info(users)` lacks the column. `schema_version` stays 3. This is the one additive migration in Phase 1b; the live database must not need a wipe.
- Set to now by: `PUT /api/me/handle` (including when the handle is unchanged, which is how "Keep @dan_b" works), demo registration (`login_or_register`), and the seed (all seeded users).
- `upsert_google_user` leaves it NULL for new users. Existing google users get NULL from the ALTER, so they see the prompt once.
- `needs_handle` appears in `/api/bootstrap` `current_user` and in `/api/me/home` `user`.

### 3.9 Sign-in return path

- `GET /auth/google/login?next=/p/12-late-shift` keeps an optional `next`. It is accepted only if it matches `^/(?![/\\])[A-Za-z0-9/_\-.?=&%#]{0,511}$` and does not start with `/auth/` or `/api/`; anything else is ignored. It is stored with the PKCE verifier in the session's pending-state dict (change the value from a bare verifier string to `{"v": verifier, "next": next}`; reading code accepts both forms during rollout).
- The callback redirects (303) to `next` when present, else to `/`. Failure redirects stay `/#not-invited` and `/#auth-error`.
- The top-bar "Sign in" link never sends `next`, so a deliberate sign-in lands on **my page**. Only an action-triggered sign-in (C, §4.4) sends `next` so the user returns to what they were doing.

### 3.10 Page routes and headers

- Add page routes `GET /discover`, `GET /search`, `GET /crate` and `GET /u/{handle}` (`^[a-z0-9_]{3,20}$`, else 404) serving `static/index.html`. Add `GET /legacy/` serving `static/legacy/index.html` until cutover.
- `index.html` responses get `Cache-Control: no-cache`. Files under `/static/` keep StaticFiles defaults (F-front adds a `?v=` query to the `main.js` entry tag only; module imports are relative and revalidate).
- **CSP.** `SECURITY_HEADERS["Content-Security-Policy"]` becomes:
  ```
  default-src 'self'; script-src 'self' https://www.youtube.com https://s.ytimg.com;
  frame-src https://www.youtube.com https://www.youtube-nocookie.com;
  img-src 'self' data: https://i.ytimg.com https://*.ytimg.com; style-src 'self'; font-src 'self';
  connect-src 'self'; media-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self';
  frame-ancestors 'none'
  ```
  (one line, `; `-separated). Responses for `/legacy/` and `/static/legacy/…` keep the old value `frame-ancestors 'none'` until cutover, since the old UI needs inline handlers. If the YouTube player turns out to need inline styles, the only allowed relaxation is adding `'unsafe-inline'` to `style-src`, never to `script-src`, and the PR must say so.
- `tests/test_security_backend.py::test_security_headers` is updated to assert the new value (and the `/static/app.js` parameter becomes `/static/js/main.js`).

### 3.11 Backend tests (F-back, `tests/test_discovery.py`, `tests/test_shell_routes.py`)

Fresh DB per test, using the existing `demo` and `google` fixtures.

- **Related**: playlist 2 lists the seeded playlists that share a video, ranked by `shared_count`; `shared` is in the related playlist's order; `total` equals the detail's `related_count`. Making a sharing playlist private or unlisted removes it from `related` and from `related_count`. A crate containing a shared video never appears. Dan's crate rows (the Phase 1a canary) don't change any count. A private playlist's related call is 404 for another user and 200 for its owner. Unknown id 404.
- **Song**: `playlists`, `curators`, `alongside` and `playlist_count` ignore crates and private playlists of other users; the viewer's own private playlist appears in `playlists` for them only and never in `curators`.
- **Profiles**: `by-handle` returns the same payload as by id; an old handle from `handle_history` resolves to the current user; bad shapes 404. `neighbours` on a profile is identical for two different viewers (listed-only). `shared_with_viewer` counts the viewer's private playlist only for that viewer, and is null when anonymous or on your own profile.
- **Home**: 401 signed out (google); demo returns my playlists in `updated_at` order with `preview` ≤3 in playlist order; `crate.recent` holds the canary rows; `fresh` excludes videos I already have (including in my crate); `activity` contains no `earned_badge` lines; `about_you` lists a grab made by another user into a public playlist and not one into a private playlist.
- **Discover**: never returns crates, private or unlisted playlists; `popular` and `fresh` order; `next_offset` paging; `tab=people` has no `badges` key; rate limit 429 after the bucket empties.
- **Search**: finds listed playlists by title and description; never returns private, unlisted or crate playlists; a track that is only in another user's crate is not found, but is found by its owner; `%` and `_` in `q` are literal; `q` over 100 characters is 422.
- **Handle**: a new google user has `needs_handle: true`; `PUT /api/me/handle` with the unchanged handle flips it to false; demo users are never prompted; the guarded ALTER is idempotent on an existing file (run `init_db()` twice).
- **Sign-in `next`**: kept for `/p/12`, ignored for `//evil.example`, `/\evil`, `https://evil.example`, `/api/x`, `/auth/x`; a callback without `next` goes to `/`.
- **Routes and headers**: `/discover`, `/search`, `/crate`, `/u/maya` serve the shell; `/u/Bad-Handle!` is 404; the CSP header equals the string above on `/` and `/static/js/main.js`, and is the legacy value on `/legacy/`.
- Every existing test keeps passing (update only the CSP assertion and file paths, §7).

---

## 4. Frontend modules and file ownership

### 4.1 Tree

```
static/
  index.html                         F   shell markup, icon sprite, <script type="module" src="/static/js/main.js?v=…">
  styles/
    tokens.css  base.css  layout.css  components.css     F
    editing.css                       A
    discovery.css                     B
    share-import.css  mobile.css      C   (mobile.css is loaded last; overrides only)
  vendor/
    lit-html/  (VERSION, LICENSE, lit-html.js, directive.js, directive-helpers.js, async-directive.js,
                directives/repeat.js, directives/class-map.js, directives/live.js, directives/ref.js)   F
    fonts/     (instrument-sans.woff2, instrument-serif.woff2, jetbrains-mono.woff2, OFL.txt ×3)        F
  js/
    main.js                           F   boot: bootstrap fetch, theme, router start, register core actions/keys
    lit.js                            F   the only importer of vendor/lit-html; re-exports html, svg, render, nothing, repeat, classMap, live, ref, createRef
    api.js                            F   fetch wrapper, X-Webjam, demo user_id, errors, ops queue
    store.js                          F   state, subscribe, reconcile
    router.js                         F   paths, links, legacy hashes, scroll restore
    actions.js                        F   one delegated click/submit/change listener → registry
    slots.js                          F   named extension points other lanes register into
    keys.js                           F creates, A owns afterwards
    paste-router.js                   F   the one document 'paste' listener; dispatches to handlers
    format.js                         F   mm:ss, relative time, plural, slug, track URL builders
    ui/  toast.js popover.js sheet.js menu.js confirm.js          F
    components/ track-row.js track-list.js playlist-card.js avatar.js empty.js     F
    player/ player.js queue.js seam.js durations.js youtube.js   F
    panels/ dock.js                   F
    panels/ crate.js                  F creates the shell, C owns afterwards
    views/ home.js not-found.js       F
    views/ playlist.js                F creates read-only, A owns afterwards
    editing/ selection.js clipboard.js dnd.js move-to.js undo.js inline-edit.js cheatsheet.js   A
    views/ track.js profile.js discover.js search.js                                           B
    discovery/ related.js likes.js comments.js badges.js                                       B
    import/ import-sheet.js paste-to-crate.js                                                  C
    share/ share-popover.js fork.js                                                            C
    account/ account-pill.js handle-prompt.js personas.js signin-intent.js                     C
    crate/ crate-actions.js                                                                     C
  legacy/ index.html app.js styles.css    moved by F (git mv), deleted at cutover
```

Ownership rules:
- A lane edits only files it owns. A shared file (anything F owns) may be changed by A, B or C **only additively** (a new export, a new slot name, a new token), in a commit titled `shared: …`, and never by renaming or changing an existing signature. If a lane needs more, it says so in its PR instead.
- Each lane wires itself in through **registries**, never by editing another lane's module: `registerActions`, `registerKeys`, `slots.register`, `pasteRouter.register`, `router.register`. Each lane has an entry module (`editing/index.js`, `discovery/index.js`, `share/index.js`) that `main.js` imports with **one line per lane**. F adds the three lines with the imports commented out; each lane uncomments its own line.
- CSS: each lane's stylesheet is linked from `index.html` by F up front (empty files are created by F). Lanes style only their own classes, prefixed `.ed-` (A), `.dc-` (B), `.sh-` (C). Tokens are only added in `tokens.css` with a `shared:` commit.

### 4.2 `lit.js` and vendoring

- Copy the eight files listed above from `https://cdn.jsdelivr.net/npm/lit-html@3.2.1/<path>` byte-for-byte into `static/vendor/lit-html/`, keeping the relative layout (the directives import `../lit-html.js`, `../directive.js`, `../directive-helpers.js` and `../async-directive.js`, all checked to resolve). Write `3.2.1` into `VERSION`, add the BSD-3-Clause `LICENSE`. Strip nothing. `//# sourceMappingURL` comments may stay (a missing `.map` is harmless).
- No other module imports from `vendor/`. `unsafeHTML`, `unsafeSVG`, `unsafeStatic` and `templateContent` are never vendored or used.

### 4.3 Core module contracts (F-front)

**`api.js`**
```js
export async function get(path, query = {})           // adds user_id in demo mode; returns parsed JSON
export async function post(path, body = {})           // POST with X-Webjam: 1, JSON body; adds user_id in demo mode
export async function put(path, body = {})            // same
export async function del(path, body = {})            // same
export class ApiError extends Error { status; code; detail; data }   // detail is always a string for display
export const ops = { send(ops, { groupId, expectedVersions, label } = {}) }  // see below
export async function undo(), redo()                   // POST /api/ops/undo|redo with group_id from store.undo
```
- Every request uses `credentials: 'same-origin'` and `Accept: application/json`; every non-GET sends `X-Webjam: 1` and `Content-Type: application/json`. This is the **only** module that calls `fetch`.
- Demo mode: `user_id` (from `store.auth.user.id`) is added to GET queries and JSON bodies. Google mode sends no user ids at all.
- 422 bodies with a list `detail` become a readable string. 401 `signed_out` triggers `signin-intent` (C) through a `slots.call('auth.required', intent)` hook; F's fallback is a toast with a "Sign in" link.
- **Ops queue**: one request in flight per tab. Each call gets `op_id = crypto.randomUUID()`; `group_id` defaults to it. A network failure retries the **same** `op_id` up to 3 times (0.5 s, 1 s, 2 s). On success it calls `store.reconcile(response)` and `toast.badge` for `new_badges`. On `409 stale_version` it refetches the affected playlists, drops their optimistic state, and shows "That playlist changed elsewhere; refreshed." On `409 anchor_missing` the same. Other errors roll back the optimistic change and toast the `detail`.
- The ops queue never sends `undo`/`redo` concurrently with ops; they share the queue.

**`store.js`**
```js
export const state = {
  auth: { mode: 'google'|'demo', signedIn: false, user: null /* bootstrap current_user */, crateId: null },
  route: { name: '', params: {}, query: {} },
  playlists: new Map(),   // id -> { meta, version, order: [trackId], related_count, loadedAt }
  tracks: new Map(),      // trackId -> row (GET playlist row shape)
  dock: null, home: null, undo: null,
  selection: { playlistId: null, ids: new Set(), anchorId: null, cursorId: null, insertAfterId: undefined },
  clipboard: { items: [], mode: 'copy'|'cut', fromPlaylistId: null },
  player: { queue: [], index: -1, status: 'idle', time: 0, duration: 0, seam: null, source: null },
  ui: { theme: 'system', cratePanel: false },
};
export function update(mutator, reason)   // mutate in place, then notify once per microtask
export function subscribe(selector, cb)   // cb(selected, prev) when selector(state) changes (Object.is per key)
export function reconcile(res)            // apply /api/ops, undo, redo and promote responses
export function optimistic(op) -> token   // local insert/delete/move preview; returns a token
export function rollback(token)
export function loadPlaylist(id, { force }) // GET /api/playlists/{id} into the maps, deduping concurrent loads
```
- `reconcile` applies `versions`, replaces `order[pid]` wholesale, merges `rows`, deletes rows no longer in any `order`, updates `undo` and the dock's `track_count`/`version` for touched playlists, and marks newly inserted ids so their rows render with `.is-new`. Optimistic temp ids are negative integers and vanish when the real `order` lands.
- Views hold no copies of server data; they render from the store. After an edit nothing refetches the whole page.

**`router.js`**: `register(name, pattern, loadView)`; `navigate(path, { replace })`; intercepts same-origin `<a href>` clicks without modifier keys, `target` or `download`; keeps `scrollY` per history entry; focuses the view's `h1` after navigation (with `tabindex="-1"`) for screen readers; closes popovers. Views are `async function view(ctx) → TemplateResult` plus optional `unmount()`; the router renders into `<main id="view">` via `render()`.

**`actions.js`**: one `click`, `submit`, `change` and `input` listener on `document`, reading the closest `[data-action]`. `registerActions({ 'name': (el, event, ctx) => {} })`; duplicate names throw. `ctx` holds `trackId`, `playlistId`, `videoId` read from the closest row or card. Forms use `data-action` on the `<form>` and call `preventDefault()`. No `on*` attribute or property handler anywhere (`addEventListener` only, and only in F's listeners, `dnd.js`, `keys.js`, `paste-router.js`, the player and `ui/*`).

Action names (prefix = owner): F `play-track`, `play-playlist`, `play-from`, `crate-add`, `open-crate`, `close-crate`, `dock-open`, `new-playlist`, `theme-cycle`, `player-*`, `toast-action`, `menu-open`; A `sel-*`, `edit-*`, `move-to`, `undo`, `redo`, `delete-playlist`; B `like-*`, `comment-*`, `related-*`, `badges-toggle`; C `share-*`, `import-*`, `fork`, `crate-unload`, `crate-promote`, `crate-clear-unloaded`, `signin`, `signout`, `persona-*`, `handle-*`.

**`slots.js`**: `register(name, fn, { order })`, `render(name, ctx) → TemplateResult[]`, `call(name, ...args)`. Names defined by F: `topbar.account`, `home.top`, `playlist.actions`, `playlist.menu`, `playlist.related`, `playlist.footer`, `row.menu`, `crate.actions`, `crate.row.menu`, `auth.required`, `share.open`, `import.open`. A missing registration renders nothing (or, for `call`, uses F's fallback).

**`paste-router.js`**: the only `document` `paste` listener. It ignores pastes into inputs, textareas and contenteditable (except the omnibox, which it handles when the pasted text contains a URL). It decodes the clipboard in Phase 1a's §5.3 order (`application/x-webjam+json` → `text/html` `data-webjam` → `text/plain`) into `{ json, html, text }` and offers it to registered handlers in priority order: A registers `playlist-edit` (priority 10; accepts when the focused list is an editable playlist); C registers `crate` (priority 0; accepts everything else and calls `/api/resolve-paste`). Each handler returns `true` when it took the paste.

**`components/track-row.js`**: `trackRow(row, { index, context, editable, selected, cursor, playing, isNew, cut })` and `trackList(rows, opts)` using `repeat(rows, r => r.id, …)`. `context` is `'playlist'|'crate'|'track-page'|'search'|'card'`. The ⋯ menu content comes from `slots.render('row.menu', ctx)` plus F's defaults (Play, Add to crate, Open track page, Copy link).

**`player/`** (F; details in §6). **`panels/dock.js`** (F): renders `store.dock` as chips with slot numbers 0–9 and the "+ New playlist" chip; each chip has `data-drop-target="playlist:{id}"` (`crate:{id}` for slot 0) for A's `dnd.js`; click opens the playlist (the crate chip opens the crate panel). **`panels/crate.js`** (F shell, C afterwards): a slide-over panel (right side, 380px; bottom sheet on phones) listing the crate with `trackList(…, { context: 'crate', editable: true })`, header "Crate · 12", and `slots.render('crate.actions')`.

### 4.4 What each lane builds

**F-front**: everything in §4.3, `views/home.js`, a read-only `views/playlist.js` (header, track list, play, the `playlist.*` slots, `?track=` select-and-scroll, `/crate` mode), `views/not-found.js`, the top bar, the omnibox (Enter → `/search?q=`), the dock, the crate panel shell with remove-row, the player (§6), the theme toggle, toasts including `toast.badge`, the "+ New playlist" flow (an inline title field in the home list and the dock; `POST /api/playlists` with `{title, visibility: 'public'}`; on success navigate to it), and the safety test rewrite (§7.2). Basic responsive layout for all three breakpoints.

**A Editing** (owner of `views/playlist.js` and `keys.js` after F):
- Selection model (`editing/selection.js`): click selects one; Shift-click range; ⌘/Ctrl-click toggles; ↑/↓ cursor; Shift-↑/↓ extend; ⌘/Ctrl-A all; Esc clears (and cancels a pending cut). Works in every track list (others' playlists, crate, track and search results) for copy and drag; editing keys only act on editable lists.
- Clipboard (`editing/clipboard.js`): copy, cut, paste, duplicate per Phase 1a §5 (writes `application/x-webjam+json` via the `copy` event, `text/html` with `data-webjam`, `text/plain` lines). Crate and private rows are written as `/t/{vid}?s=&e=` lines, never `/p/{crate}?track=`. Paste inserts after the insertion cursor and moves the cursor after the pasted block, so V, V, V gives three copies in order, three requests, three undo groups. A cut becomes a `move` op on paste when every ref is still in a playlist the user owns, else an `insert`.
- Drag and drop (`editing/dnd.js`, §5.3), including the dock spring-open popover.
- Move to position (`editing/move-to.js`): `G` (or `#`) opens a tiny inline field `Move to #__` beside the selection; Enter sends `move` with `position`; Alt/Option-↑/↓ moves one row, Shift-Alt-↑/↓ to top/bottom.
- Undo/redo (`editing/undo.js`): ⌘/Ctrl-Z, ⌘/Ctrl-Shift-Z and Ctrl-Y; ignores `e.repeat`; one request in flight; sends `group_id`; handles `undo_conflict` (toast with "Skip it" → `skip: true`, then undo again), `stack_moved`, `nothing_to_undo`, and the `playlist_left_empty` note (toast "Delete the empty playlist?" with an action). Every op toast carries "Undo".
- Inline editing (`editing/inline-edit.js`): title and description edited in place (`set_meta`), visibility select (public / unlisted / private, with one-line explanations), per-row "Note & clip" expander with a note textarea and two `m:ss` fields (`update`), remove (Delete key and row menu), delete playlist with a confirm sheet ("This can't be undone.").
- `?` cheat sheet (`editing/cheatsheet.js`): a sheet listing every registered key, grouped, generated from the `keys.js` registry.

**B Discovery**: `views/track.js`, `views/profile.js` (incl. edit profile via `PUT /api/users/{id}`), `views/discover.js`, `views/search.js` (incl. the opt-in YouTube section), `discovery/related.js` (the `playlist.related` panel and the header line behaviour; each related item expands to show its shared tracks, playable), `discovery/likes.js` (♡ toggles with `count: 1`, Shift-click adds one more and shows a mono `×n`; reset via a second plain click when `my_likes ≥ 1`; no particles or animation; registered into `playlist.actions`, the track header and the player bar slot), `discovery/comments.js` (list, post, "Older" paging with `before_id`; on playlist, track and profile), `discovery/badges.js` (the §1.7 profile section).

**C Import / Share / Crate**: `import/import-sheet.js` (a sheet: paste a YouTube playlist URL or several video URLs → `POST /api/playlists/import/inspect` → a list with checkboxes, unavailable items unchecked with their reason, destination "Crate" (default) or "New playlist" with the title prefilled, "Import N"; commit via `POST /api/playlists/import/commit` with `target_playlist_id` = crate id or a new playlist; opened from the home empty state, the dock ⋯, the omnibox and `youtube_playlist` paste pointers); `import/paste-to-crate.js` (the `crate` paste handler: `/api/resolve-paste`, then an `insert` into the crate, toast "Added 3 tracks to your crate · Open"; pointers open the import sheet; `unresolved` reported in the toast); `crate/crate-actions.js` and ownership of `panels/crate.js` (Unload ▸ menu of dock playlists → `move` ops, "Keep a copy in crate" checkbox → `insert` with `ref.track_id`; "Make it a playlist" → `POST /api/me/crate/promote` with a title field and visibility, for all or the selection; "Clear unloaded" → `delete` of rows with non-empty `also_in`, using `GET /api/me/crate`); `share/share-popover.js` (copies the link on open and says "Link copied"; options: start at the selected track `/p/{id}?track={tid}`, copy as a text list, `navigator.share` when present; private and crate: "Only you can open this" and no link; unlisted: "Anyone with the link can open it"); `share/fork.js` ("Copy all" → `POST /api/playlists/{id}/fork` with `{title: null, visibility: null}` → navigate, toast "Copied 14 tracks · via @maya"); `account/*` (sign-in link, account menu with profile, theme, sign out; the handle prompt card in `home.top`; demo persona switcher in the account menu only when `auth.mode === 'demo'`, using `POST /api/auth/login` and the users list from bootstrap; `signin-intent.js` registers `auth.required`: it stores `{action, payload, path}` in `sessionStorage` and goes to `/auth/google/login?next=<path>`, and after boot replays only `crate-add` and `like` intents, then toasts "Added to your crate"); `styles/mobile.css` (phone polish: dock strip, sheets, tap targets ≥ 40px, safe-area insets).

---

## 5. Interaction rules

### 5.1 Keymap (`keys.js`)

Global rules: keys are ignored when focus is in `input`, `textarea`, `select` or `[contenteditable]`, during IME composition, and when a modal sheet is open (except Esc). "Mod" is ⌘ on macOS and Ctrl elsewhere. `e.repeat` is ignored for every key except ↑/↓. Registration: `registerKeys([{ combo: 'mod+c', when: 'list', label: 'Copy', group: 'Edit', run }])`; `when` is `'always'|'list'|'editable'|'player'`.

| Key | Action | Owner |
|---|---|---|
| `?` | Keyboard shortcuts sheet | A (F stub shows "coming soon") |
| `/` | Focus the omnibox | F |
| `Esc` | Close popover/sheet → cancel drag → cancel cut → clear selection | F (A extends) |
| `Space` / `K` | Play / pause | F |
| `Shift-←` / `Shift-→` | Previous / next track | F |
| `←` / `→` (player focused) | Seek −5 s / +5 s | F |
| `E` | Jump to 10 s before the end (clip end if set) | F |
| `B` | Preview the seam **before** the focused track: previous track's last 10 s, then this one | F |
| `N` | Preview the seam **after** the focused track: its last 10 s, then the next | F |
| `Enter` | Play the focused track (on a dock chip: open it) | F |
| `↑` / `↓` | Move the cursor | A |
| `Shift-↑` / `Shift-↓` | Extend the selection | A |
| `Mod-A` | Select all in the list | A |
| `Mod-C` / `Mod-X` | Copy / cut the selection | A |
| `Mod-V`, or `V` alone | Paste after the insertion cursor (repeatable) | A |
| `Mod-D` | Duplicate the selection in place | A |
| `Delete` / `Backspace` | Remove the selection | A |
| `Alt-↑` / `Alt-↓` | Move the selection one row | A |
| `Shift-Alt-↑` / `Shift-Alt-↓` | Move to top / bottom | A |
| `G` or `#`, digits, `Enter` | Move the selection to position n | A |
| `0`–`9` | Send (copy) the selection to dock slot n (0 = crate; from the crate it moves) | A |
| `Alt-0`–`Alt-9` | Open dock slot n | A |
| `C` | Add the selection (or focused track) to the crate | F (core action, works in every list) |
| `Mod-Z` / `Mod-Shift-Z` / `Ctrl-Y` | Undo / redo | A |
| `L` | Like the focused or playing track | B |
| `S` | Share the current playlist (or the selected track) | C |

Never bind bare letters that browsers or screen readers need when focus is on a link or button: the list keys act only when the list (or a row) has focus.

### 5.2 Selection and the insertion cursor

- The cursor row has DOM focus (roving `tabindex`). The insertion cursor sits after the last selected row; with nothing selected it sits after the cursor row; in an empty list, at the start. `store.selection.insertAfterId` holds it (`null` = at the start).
- Selection belongs to one list at a time. Selecting in another list clears the first.
- After a paste or a drop, the new rows become the selection and the cursor moves to the last of them, so repeated pastes chain.

### 5.3 Drag rules (`editing/dnd.js`, pointer events)

| From | To | Default | With Alt/Option |
|---|---|---|---|
| my playlist | the same playlist | **move** (reorder) | copy |
| my crate | any of my playlists | **move** (unload) | copy (keep in crate) |
| my playlist | another of my playlists, or my crate | **copy** | move |
| anything else (others' playlists, track page, search, cards, home lists) | my playlist or crate | **copy** (credited via the ref) | copy |
| anywhere | the "+ New playlist" chip | create "Untitled playlist", insert the tracks, focus the title | |

- Start: mouse after 4 px of movement from a `pointerdown` on a row (not on a button, link or input); touch and pen after a 350 ms long-press without moving more than 8 px (with a short vibration where supported). Rows set `touch-action: pan-y` so scrolling still works.
- Ghost: a small pill following the pointer: the first thumbnail (24px), "3 tracks", and the mode word ("Copy" / "Move") which updates live when Alt changes.
- Targets: gaps in an editable list (the insertion line), dock chips (`[data-drop-target]`), the open crate panel. Hovering a dock chip for 600 ms springs open a popover listing that playlist's titles (no thumbnails) with drop gaps between them, so you can drop at an exact spot; leaving the popover closes it.
- Auto-scroll when the pointer is within 48 px of a scroll container's top or bottom edge.
- Drop sends one ops batch: within a playlist, `move` with `after_track_id`; across playlists, `move` (with `to_playlist_id`) or `insert` items with `ref.track_id`; a non-owner source is always `insert`. Optimistic, reconciled, one undo group. The landed rows get `.is-new`; the target chip shows a brief "+3" in mono that fades with the same wash.
- Esc or `pointercancel` cancels. A drop on nothing does nothing.
- Drag out: rows are also `draggable="true"` only for native `dragstart` that sets `text/uri-list` and `text/plain` share URLs, so a track can be dropped into a chat app. Native drag never drives in-app drops.

### 5.4 Touch fallback (tap to arm)

- On `(pointer: coarse)`: tapping a row selects it (no play); a second tap on a selected row opens its ⋯ menu; the play button plays.
- With a selection, the dock strip changes to "Send 2 to…": tapping a chip sends a copy (from the crate: a move), and the strip shows "+2" on that chip. Tapping elsewhere or Esc disarms.
- The row ⋯ menu always offers: Play, Add to crate, Send to… (dock list), Move to position… (owner), Remove (owner), Share, Open track page.
- Long-press drag (above) remains available.

### 5.5 Paste anywhere

- In an editable playlist with focus in its list: A's handler inserts at the insertion cursor (§4.4).
- Anywhere else (including other people's pages and the omnibox when the paste contains a URL): C's handler fills the crate, or opens the import sheet for a YouTube playlist pointer. Free text with no links is ignored silently (the omnibox keeps it as a search).
- Anonymous visitors: the paste triggers `auth.required` with a `crate-add` intent.

---

## 6. Player (F-front, `static/js/player/`)

- **`youtube.js`** loads `https://www.youtube.com/iframe_api` once (a `<script>` element created in JS, allowed by CSP) and resolves when `onYouTubeIframeAPIReady` fires. The handler is assigned in JS (`window.onYouTubeIframeAPIReady = …`), never as an attribute.
- **`player.js`**: one `YT.Player` in the video tile, `playerVars: { playsinline: 1, rel: 0, modestbranding: 1, origin: location.origin }`. API: `playQueue(items, index)`, `toggle()`, `next()`, `prev()`, `seek(seconds)`, `jumpToEnd(lead = 10)`, `previewSeam(aIndex)`. Items are `{ trackId?, playlistId?, video_id, start, end, title, channel, thumb }`. Clip bounds go to `loadVideoById({videoId, startSeconds, endSeconds})`, backed by a 250 ms monitor (rAF-free `setInterval`, cleared on pause) that advances at `end`.
- Queue: playing a row plays its list from that row. Playing a single track (track page, search) makes a queue of one. There is no "up next" queue editing in Phase 1b. Errors 100/101/150 mark the row `.is-dead` for the session, toast "Unavailable on YouTube; skipped", and advance after 1.5 s.
- **`jumpToEnd`**: seeks to `(end ?? duration) − 10` (clamped to `start`). Duration comes from `getDuration()` after load and is cached by `durations.js`.
- **`seam.js` / `previewSeam(i)`**: plays track `i−1` from `max(start, (end ?? duration) − 10)`; when it reaches its end, the queue advances to track `i` normally (a hard cut; Phase 2 adds crossfades and two decks). While a seam preview runs, the player bar shows a thin seam lane: a 120px track split in two halves ("A" tail and "B" head) with a playhead, in `--ink-3` with the playhead in `--accent`. If the previous track's duration isn't known yet, it loads the video, waits for the first `PLAYING` state, reads the duration, then seeks.
- Play counting: once per (playlist, track) per session, after 30 s of playback or at the end, `POST /api/playlists/{id}/play` with `{video_id}`; failures are ignored. Not for crate or single-track queues (the server refuses crates anyway).
- Player bar: prev, play/pause, next; title and channel (title links to `/t/{vid}`, and a `from 'Playlist'` link when the queue came from one); a progress bar (`<input type="range">`, CSS-only styling) with current time and duration in mono; the seam lane when active; the `player.like` slot (B). Media Session API metadata and handlers when available.
- Anonymous visitors can play everything. Nothing in the player ever requires sign-in.

---

## 7. What gets deleted, and the test rewrites

### 7.1 F: legacy move (in F's PR)

- `git mv static/app.js static/legacy/app.js`, `static/styles.css → static/legacy/styles.css`, `static/index.html → static/legacy/index.html` (update its two asset paths), then create the new `static/index.html`. `/legacy/` serves it with the old CSP (§3.10), so the old UI stays usable for comparison during the lanes.
- Update the three test files that read `static/app.js` (`tests/test_frontend_safety.py`, `tests/test_review_fixes.py`, `tests/test_phase1a_review.py`) to read `static/legacy/app.js`. No assertion changes in F.

### 7.2 F: new `tests/test_frontend_safety.py`

Keep the legacy checks in a separate file `tests/test_legacy_frontend_safety.py` (moved verbatim, pointed at `static/legacy/`) until cutover. The new file guards `static/index.html` and every `static/js/**/*.js`:

1. No `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, `DOMParser`, `createContextualFragment`, `srcdoc`, `eval(`, `new Function`, or `setTimeout`/`setInterval` with a string argument.
2. No `unsafeHTML`, `unsafeSVG`, `unsafeStatic` or `templateContent`, and no import of anything under `vendor/` except from `js/lit.js`.
3. No inline event handlers: no `\son[a-z]+\s*=` inside any `html\`` / `svg\`` template or in `index.html`, and no `.on[a-z]+ =` property assignment in `js/` except `window.onYouTubeIframeAPIReady` in `player/youtube.js`.
4. No `style=` attribute in templates or `index.html`; no inline `<style>` or `<script>` without `src` in `index.html`; no `javascript:` anywhere.
5. `fetch(` appears only in `js/api.js`, and `api.js` sets `'X-Webjam': '1'` on non-GET requests.
6. Every `import … from '…'` specifier in `js/` is relative (`./` or `../`) and resolves to an existing file; no bare specifiers, no `http(s):` imports.
7. `static/vendor/lit-html/VERSION` is exactly `3.2.1`, and every relative import inside `vendor/lit-html/` resolves.
8. `href=${…}` bindings in templates use a helper from `format.js` (`trackUrl`, `playlistUrl`, `userUrl`) or a string literal starting with `/`, never a raw data field (prevents `javascript:` URLs from data).
9. If `node` is on PATH, `node --check` passes for every file in `js/` (skip with a reason otherwise).
10. `index.html` loads exactly one module script (`/static/js/main.js`) and no third-party scripts.

### 7.3 Cutover (after A, B, C)

- Delete `static/legacy/`, the `/legacy/` route and the legacy CSP exception, and `tests/test_legacy_frontend_safety.py`.
- In `tests/test_review_fixes.py::test_frontend_follows_the_backend_changes` and the five `test_frontend_*` tests in `tests/test_phase1a_review.py`, replace the `app.js` string assertions with equivalents on the new modules: undo ignores `e.repeat`, keeps one request in flight and sends `group_id` (`editing/undo.js`); skip-then-undo on `undo_conflict` (`editing/undo.js`); `api.js` turns list `detail` into a string; the like control is not rendered for crates (`views/playlist.js`). Tests about features that no longer exist (audit-embeds button, auto-migrate group, "+1 Spin") are deleted with a note in the commit message.
- `/api/feed` stays (other tests use it) but is marked deprecated in a comment.
- Grep check: no file outside `static/` references `static/app.js` or `styles.css`.

---

## 8. Acceptance checklists

Each lane's PR must tick its list, run `python -m pytest -q` green, and run `node --check` on its files. There is no browser in CI; each lane also includes a short manual-test note in the PR (what was clicked, in which theme, at which width) from a local run.

### F-back
- [ ] `app/discovery.py` implements §3.2–3.7 using only the `visibility.py` predicates; `app/main.py` endpoints are thin.
- [ ] `/api/me/home`, `/api/discover`, `/api/search`, `/api/playlists/{id}/related`, `/api/users/by-handle/{handle}` exist with the exact shapes above; `/api/songs/{vid}`, `/api/users/{id}`, `/api/playlists/{id}` gain the listed fields without changing existing ones.
- [ ] `handle_chosen_at` with the guarded ALTER; `needs_handle` in bootstrap and home.
- [ ] Sign-in `next` with validation.
- [ ] Page routes, `Cache-Control: no-cache` on the shell, strict CSP, the legacy exception.
- [ ] `find` rate-limit bucket.
- [ ] All tests in §3.11 written and passing; every pre-existing test passing.

### F-front
- [ ] lit-html 3.2.1 vendored (8 files, VERSION, LICENSE); fonts vendored with licences; no third-party requests from the page except YouTube.
- [ ] Tokens exactly as §1.2; light, dark (system) and both `data-theme` overrides work; theme cycle persists.
- [ ] Signed-in `/` shows **my playlists first**, then crate, neighbours, fresh tracks, about-you and activity; anonymous `/` shows Discover (a minimal F version: listed playlist cards from `/api/discover`, which B then replaces).
- [ ] Track rows match §1.4 anatomy and states; hop links present (`via`, `in N playlists`, `from`).
- [ ] Read-only playlist view with header, byline hop, `related_count` line (opens a plain list until B lands), play, `?track=`; `/crate` works for the owner.
- [ ] Dock (slots 0–9 + New) and crate panel shell (list, remove, open from chip).
- [ ] Player: play rows and cards anonymously, next/prev, clip bounds, `E`, `B`, `N` seam preview with the seam lane, dead-video skip, play counting, Media Session.
- [ ] Router: all §2.1 paths, legacy hashes from §2.2, link interception, scroll restore, h1 focus.
- [ ] `api.js` ops queue: serial, same op_id on retry, reconcile, stale_version refresh, badge toast.
- [ ] No `innerHTML` etc.: the new safety test (§7.2) passes; old UI reachable at `/legacy/`.
- [ ] Layout works at 1280, 900 and 375 px wide with no horizontal scroll; dock + player ≤ 120px on phones.
- [ ] `prefers-reduced-motion` honoured.

### A Editing
- [ ] Click / Shift / Mod selection, cursor and insertion line in every list; editing only where the viewer owns the playlist.
- [ ] Mod-C then V, V, V inserts three copies after the selection, in order, as three undo groups; Mod-V works the same; Mod-X + paste moves; Mod-D duplicates.
- [ ] System clipboard: copying writes the three formats; pasting a copied Webjam URL list or JSON round-trips with credit; crate and private rows copy as `/t/` links.
- [ ] Digits 0–9 send the selection to dock slots (crate move vs copy rules); Alt-digit opens.
- [ ] Drag: reorder within, unload from crate, copy from elsewhere, Alt toggles, dock chips, spring-open popover, "+ New playlist" chip, auto-scroll, Esc cancels, touch long-press.
- [ ] `G`/`#` + number + Enter moves to position; Alt-↑/↓ and Shift-Alt-↑/↓.
- [ ] Optimistic everywhere, reconciled from `order`/`rows`; no whole-playlist refetch after an edit (verified by watching the network log).
- [ ] Undo/redo keys and toasts with labels; conflict skip; `playlist_left_empty` offer; held keys don't double-undo.
- [ ] Inline title, description, visibility, note & clip editing; remove; delete playlist with confirm.
- [ ] `?` sheet lists every registered key from all lanes.

### B Discovery
- [ ] Track page: header, in-N-playlists cards, curators, often-alongside, comments; every item is a one-click hop.
- [ ] Profile by handle (old handles redirect), playlists with previews, taste neighbours, "You share N tracks", discovered, loved, lineage, guestbook, edit profile, badges section per §1.7.
- [ ] Discover tabs and paging; anonymous intro line; no badges anywhere in it.
- [ ] Search groups with "More"; YouTube section only for signed-in users and only on click.
- [ ] Related panel in the right rail (inline on narrower screens) with expandable shared tracks; header line "N playlists share these tracks".
- [ ] Likes: quiet ♡ with count, Shift-click ×n, no animation; hidden on crates.
- [ ] Comments with paging on playlist, track, profile; hidden on crates.
- [ ] Unlock toast appears for `new_badges` from any write, coalesced; never for other users.
- [ ] Crate contents never appear in any B view for anyone but the owner (checked with Dan's canary rows while signed in as Maya).

### C Import / Share / Crate
- [ ] Import sheet from a YouTube playlist URL or several URLs into the crate or a new playlist, with unavailable items unchecked; opened from paste pointers, home empty state, omnibox and the dock menu.
- [ ] Paste anywhere outside an editable list adds to the crate with a toast; anonymous paste asks to sign in and completes after sign-in.
- [ ] Crate panel: unload to a dock playlist (move; keep-a-copy option), make it a playlist (all or selection, title, visibility), clear unloaded.
- [ ] Share popover: link copied on open, start-at-track option, text list, `navigator.share`; correct wording for private/crate and unlisted.
- [ ] Copy all / fork credited "via @user".
- [ ] Account pill: sign in (no `next`, so you land on your page), account menu with profile, theme, sign out; action-triggered sign-in returns to the page and replays the intent.
- [ ] Handle prompt card on home when `needs_handle`; "Keep @x" and "Save" both clear it; validation errors inline.
- [ ] Demo persona switcher and demo strip only in demo mode.
- [ ] Mobile: dock strip, crate bottom sheet, sheets full-width, tap targets ≥ 40px, safe-area insets, 375px with no horizontal scroll.

### Cutover
- [ ] §7.3 done; `rg -n "app\.js|legacy" static app tests` finds nothing unintended; full test suite green.

---

## 9. Known risks and how to handle them

- **YouTube and strict CSP.** The iframe API injects a script from `www.youtube.com/s/player/…` (allowed). If it ever needs `s.ytimg.com` or inline styles, adjust only as §3.10 allows and note it.
- **lit-html and `style`.** Bound `style` attributes would be blocked by `style-src 'self'`. That is why the avatar uses classes and dynamic numbers (progress, playhead) are set through `ref` + `el.style.setProperty('--p', value)` (CSSOM changes are not blocked by CSP).
- **Single-key bindings and accessibility.** Bare-letter keys act only when a list or the player has focus, never on the page body, so screen-reader quick navigation keeps working.
- **Performance of overlap queries.** At invite-only scale the per-card `related_count` subquery is fine. If a page exceeds ~50 ms server time, cache `related_count` per playlist keyed by `version` in memory, without changing the API.
- **Clipboard formats.** Custom MIME types are Chromium-only; plain text with Webjam URLs is always sufficient, and `/api/resolve-paste` restores refs from it.

## 10. Foundation notes for view lanes

Written after F-back and F-front merged (`t3/phase1b-shell`). Where this differs from §§2–4, this section wins.

### 10.1 Entry, routes and views
- `static/js/main.js` imports `core.js` and `routes.js`, then one commented line per lane: `// import './editing/index.js';` (A), `// import './discovery/index.js';` (B), `// import './share/index.js';` (C). Create your `index.js` and uncomment only your own line.
- Routes: `router.register(name, pattern, () => import('./views/x.js'))`. Registering an existing name replaces it. Patterns are `'/p/:ref'` strings or RegExps with named groups. B replaces `track`, `profile` and `search` (they point at `views/interim.js` now) and overwrites `views/discover.js`. The `profile` regex accepts mixed case; `userUrl()` always emits lowercase and the API folds case.
- Server side, `/`, `/discover`, `/search`, `/crate`, `/p/…`, `/t/{vid}` and `/u/{handle}` serve `static/index.html` (`Cache-Control: no-cache`). A new top-level page path needs a matching shell route in `app/main.py`. The old UI lives at `/legacy/` (`static/legacy/`).
- A view module exports `load(ctx)` (async; throw an `ApiError` 404 for not-found), `render(ctx)` (sync, from the store; runs again on every store change), and optionally `title(ctx)` and `unmount()`. `ctx = { name, params, query, hash, path, signal, rerender() }`.

### 10.2 Store (`store.js`)
```
auth      { mode: null|'google'|'demo', signedIn, user /* bootstrap current_user, has needs_handle */, crateId, users, youtubeConnected, badgeCatalog }
route     { name, params, query, path }
playlists Map id -> { meta /* GET /api/playlists/{id} minus tracks */, version, order: [trackId], related_count, kind, ownerId, loadedAt }
tracks    Map trackId -> row (playlist row shape + playlist_id)
dock      GET /api/me/dock      home  GET /api/me/home      undo  { can_undo, undo_label, undo_group_id, can_redo, redo_label, redo_group_id }
selection { playlistId, ids: Set, anchorId, cursorId, insertAfterId }
clipboard { items, mode, fromPlaylistId }
player    { queue, index, status, time, duration, seam, source }
ui        { theme, cratePanel, newPlaylist: 'home'|'dock'|null, notice }
landed    Set of track ids rendered with .is-new;   dead  Set of video ids YouTube refused
```
- Mutate only with `update(fn, reason)`; listen with `subscribe(selector, cb)` or `onChange(cb)`. Helpers: `rowsOf(id)`, `isMine(id)`, `loadPlaylist(id, { force })`, `ingestPlaylist`, `forgetPlaylist`, `markLanded(ids)`.
- Edits: `const token = optimistic(op)`, then `api.ops.send(ops, { token, groupId, expectedVersions, quiet })`; it reconciles on success and rolls back on failure. Never re-fetch a whole page after an edit. `api.undo()`/`api.redo()` share the ops queue; wrap other writes in `api.queued(fn)`.
- `api.get(path, query, { quiet })`, `api.post/put/del(path, body, { quiet })`. Writes always carry `X-Webjam: 1`; demo mode adds `user_id`, Google mode never does. `quiet` stops a 401 from prompting sign-in.

### 10.3 Actions, keys, slots, paste
- `registerActions({ name: (el, event, ctx) => … })`; duplicate names throw. Clicks reach `button`/`a[data-action]`; submits reach `form[data-action]` (already `preventDefault`ed); change/input reach fields that carry `data-action`. `ctx = { trackId, playlistId, videoId, row, card, list }`. `data-cancel-action` on a form runs on Esc. Never use inline handlers or `innerHTML` with data.
- F's names beyond the spec list: `crate-remove`, `new-playlist-submit`, `new-playlist-cancel`, `omnibox-submit`, `menu-item`, `sheet-close`, `confirm-yes`/`confirm-no`, `toast-close`, `pl-related-basic`, `discover-more`. C must register `import-open` (the home empty state shows it when the `import.open` slot exists).
- Keys: `registerKeys([{ combo, when, label, group, run }])`; later registrations win; `run` returning `false` passes the key on. `?` is a "coming soon" toast for A to replace.
- Slots: `slots.register(name, fn, { order })`, `render`, `call`, `has`. Includes `home.top`, `player.like`, `rail.context`, `playlist.actions`, `crate.actions`, `playlist.related`, `auth.required`, and menu slots `row.menu`, `crate.row.menu`, `playlist.menu` that return `{ label, icon, run | href, danger, kbd }` or `'sep'`.
- Paste: `pasteRouter.register({ name, priority, handle(payload, event) })`, `payload = { json, html, text, target, inOmnibox }`; return `true` or a promise to claim it.
- Rows: `trackList(rows, { key, context, editable, playlistId, label, source })` and `compactList(...)`; `listRows(key)` returns `{ rows, source }`. Dock chips carry `data-drop-target` (`crate:{id}`, `playlist:{id}`, `new`) and `data-slot`.
- URLs: every bound `href` goes through `trackUrl`, `playlistUrl`, `userUrl`, `localUrl` or `iconHref`; every `src` through `thumbSrc`. Action-triggered sign-in links use `signInUrl(location.pathname + location.search)`, which adds a validated `?next=`. The top-bar link stays bare. `tests/test_frontend_safety.py` enforces these rules.

### 10.4 Styles
- Write only in your own stylesheet: `editing.css` (`.ed-*`), `discovery.css` (`.dc-*`), `share-import.css` and `mobile.css` (`.sh-*`). All are already linked from `index.html`. New tokens go in `tokens.css` in a separate `shared:` commit. Do not use `style=` attributes, because CSP `style-src 'self'` blocks them. Set CSS variables with `el.style.setProperty` from a `ref`.
- A new stylesheet or vendored file must be same-origin under `static/`. The CSP allows scripts from `'self'` and `www.youtube.com`/`s.ytimg.com`, frames from YouTube, images from `'self'`, `data:` and `*.ytimg.com`, and fonts and styles from `'self'` only.

### 10.5 Endpoint field names (as implemented)
- **PlaylistCard** (home, discover, related, song `playlists`): `{ id, kind, visibility, version, title, description, track_count, like_count, plays, related_count, updated_at, user: UserMini, preview?: [TrackMini ×≤3] }`. `UserMini = { id, username, display_name, avatar_color }`. `TrackMini = { id, video_id, position, title, channel_title, thumbnail_url, start_seconds, end_seconds }`.
- **`GET /api/me/home`** (401 `signed_out` in Google mode): `user {…UserMini, bio, needs_handle}`, `playlists [PlaylistCard]` (mine, any visibility, newest edit first), `crate { id, version, track_count, recent: [TrackMini + source_username] }`, `dock`, `undo`, `neighbours [{ user, shared_count, sample: [{ video_id, title, thumbnail_url }] }]` (≤6), `fresh [{ track: TrackMini, playlist: { id, title }, user, added_at, playlist_count }]` (≤10), `about_you [{ type: 'grab', at, user, video_id, title, playlist: {id, title} } | { type: 'comment', at, user, body, playlist }]` (≤10), `activity [{ id, actor_user_id, action_type, summary, link_type, link_id, created_at, username, display_name, avatar_color }]` (≤20, no badge lines).
- **`GET /api/discover?tab=playlists|tracks|people&sort=fresh|popular&offset&limit`**: `{ tab, sort, offset, next_offset /* null at the end */, playlists | tracks | people, … }`. It skips empty playlists, has no `badges` key, and uses the `find` rate bucket.
- **`GET /api/search?q&type&limit`**: `q` is trimmed to 1–100 chars, else 422. Returns `{ query, playlists?, tracks?, people? }`; which lists appear depends on `type` (`all` returns all three). `people` items are `{ user, playlist_count }`. `%`, `_` and `\` match literally. Uses the `find` bucket.
- **`GET /api/playlists/{id}/related?limit&offset`**: `{ playlist_id, total, next_offset, related: [{ playlist: PlaylistCard (no preview), shared_count, shared: [{ video_id, title, thumbnail_url, position } ×≤4] }] }`. It works on my private playlists and my crate, and only ever names listed playlists.
- **`GET /api/playlists/{id}`** adds `related_count`. **`GET /api/songs/{vid}`** keeps `appearances` and adds `playlist_count`, `playlists [{ playlist: PlaylistCard, occurrences, first_position, note }]`, `curators [{ user, playlist_count }]` and `alongside [{ video_id, title, channel_title, thumbnail_url, together_count }]`.
- **`GET /api/users/{id}`** and **`GET /api/users/by-handle/{handle}`** (lowercased; old handles resolve; a bad shape is 404): `user`, `playlists` (each with `related_count` and `preview`), `neighbours` (same shape as home, listed-only), `shared_with_viewer { shared_count, sample } | null`, plus the older keys (`metrics`, `earned_badges`, `comments`, …).
- **`/api/bootstrap`** `current_user.needs_handle`. **`/auth/google/login?next=`** returns you to `next` after the callback when it passes validation, otherwise to `/`.

## 11. Cutover notes (integration of A, B and C)

Written when the three lanes merged into `t3/phase1b-shell`. Where this differs from earlier sections, this section wins.

- **Classic UI deleted:** `static/legacy/`, the `/legacy/` route, its relaxed CSP and `tests/test_legacy_frontend_safety.py` are gone. Every path now gets the strict CSP. `/api/feed` stays but is marked deprecated.
- **Static assets revalidate:** every `/static/` response carries `Cache-Control: no-cache` (ETag gives cheap 304s), because ES modules are imported without a `?v=`. Still bump `main.js?v=` in `index.html` when boot changes.
- **One set of playlist-menu items:** Lane A's "Edit details" and "Delete playlist…" (always last, order 1000). Lane C's settings sheet (`share/settings.js`) was removed. F's "Copy link" now sits near the top, after "Play from the start".
- **One "Send to…" item:** Lane A's row-menu item works in every list. Lane B's search-only "Add to playlist…" (`discovery/add-to.js`) was removed.
- **Routes:** `routes.js` points `search`, `track` and `profile` at the real views. `views/interim.js` and F's `pl-related-basic` fallback were removed.
- **Track page Share:** Lane C registers it in Lane B's `track.actions` slot.
- **Like intents:** Lane B sends `{ action: 'like', payload: { playlist_id } | { video_id } }`. Lane C's replay goes through `toggleLike(…, { add: true })`, so the ♡ on screen updates.
- **`ui/sheet.js` fix:** opening a sheet while another is open swaps its content in place. A pending `close` event from an earlier sheet no longer closes the new one.
- **Import entry points:** the import sheet now opens from the dock ("Import from YouTube…" under New playlist) and from the omnibox (typing a YouTube link and pressing Enter).
- **Ported from the classic UI at cutover:**
  - "Your YouTube playlists" picker in the import sheet, shown only when `youtube_connected` is true.
  - Repeat one track (player bar).
  - "Small video, low data", which replaces the old audio-only mode. The tile is 200×200, the minimum YouTube allows, and the setting is remembered per browser.
  - "Add to crate" for the track that is playing (player bar).
- **Dropped with the classic UI, per §7.3:**
  - audit-embeds;
  - the auto-migrate group and the per-track migrate search (replaced by "Swap video…" with a pasted link);
  - "+1 Spin", love particles and the badges directory page (badges live on profiles).
- **Tests:**
  - `tests/test_frontend_safety.py` now also checks that the classic UI is gone, that every lane entry is imported, that routes point at the real views and that `href` literals keep a fixed origin.
  - It also checks `target=_blank` with `noopener`, and the owner's vocabulary (no "audition").
  - It runs `tests/js/wiring_smoke.mjs`, which loads every module and checks that each `data-action` used in a template has a handler.

---

## 12. Phone helpers in the player

YouTube embeds can't play in the background on phones, and YouTube expects a visible player, so webjam does not try background playback. It makes the foreground better instead. Each helper is a small module in `static/js/player/` with no DOM access of its own, unit-tested by `tests/js/player_helpers.mjs` (run from `tests/test_player_helpers.py`). `player.js` wires them in.

### 12.1 Keep the screen awake (`wake-lock.js`)

- Uses the Screen Wake Lock API (`navigator.wakeLock.request('screen')`). The lock is wanted only when the API is supported, the setting is on, the player is playing and the page is visible.
- It is acquired on `PLAYING` and released on pause, error, end of queue or hide. Browsers drop the lock when the tab is hidden, so on `visibilitychange` back to visible it is requested again if music is still playing.
- A sentinel that arrives after the player has already paused is released straight away. Every `request()` rejection is swallowed.
- **Setting:** "Keep screen awake while playing", in the phone ⋯ menu. On touch devices wider than a phone, where `.pbar-extra` is visible, it also appears as an icon toggle there, with the accent marking "on". It is hidden when the API is missing.
- The default is on for touch devices (`pointer: coarse`) and off elsewhere. The choice is remembered in `localStorage["webjam.keepAwake"]`. Every storage call is wrapped in try/catch.

### 12.2 Lock-screen and headphone controls (`media-session.js`)

- **Metadata:** `navigator.mediaSession.metadata` holds:
  - the title;
  - the artist: the channel, or `@curator` when there is no channel;
  - the album: the playlist title, "Your crate" or "webjam";
  - artwork: `i.ytimg.com/vi/<id>/mqdefault.jpg` (320×180) and `hqdefault.jpg` (480×360). CSP `img-src` already allows `https://i.ytimg.com`.
- **Handlers:**
  - `play` and `pause` are idempotent (`player.play()` / `player.pause()`), so a headset button never flips twice;
  - `previoustrack`, `nexttrack`;
  - `seekto` and `seekbackward`/`seekforward` (10 s, or the offset the OS asks for). All of them go through `seek()`, which keeps clip bounds.
- **`playbackState`** follows the YouTube state.
- **`setPositionState`** works across the clip, so a clip from 0:30 to 3:20 shows as 2:50 long. It is set on play, pause, seek and every 5 s. It is cleared when the track changes. Invalid values are never sent, and the call is wrapped in try/catch.
- **Caveat:** the `<video>` lives in YouTube's cross-origin iframe. Some browsers show YouTube's own session, or none, instead of ours. We set ours correctly and let it degrade quietly.

### 12.3 Resume where you left off (`resume.js`)

- **What is saved:** `localStorage["webjam.resume.u<user id>"]`, or `["webjam.resume.anon"]` when signed out. It holds `{ v, src: { kind, playlistId, title } | null, trackId, videoId, index, start, end, title, channel, pos, at }`. That is only the playing track and where it came from, never the queue or a crate's contents.
- **Keying by user:** a second person on the same browser never sees the first person's spot. A crate source is never written under the anonymous key, and a crate is only saved and rebuilt for the signed-in owner (`crateId` must match). The player remembers who started the queue; after a demo persona switch (or a sign-in) mid-play, nothing more is saved until the new person starts something themselves. If a saved list no longer loads, the fallback queue of one carries no playlist or track id.
- **When it is written:**
  - every ~5 s while playing;
  - on pause;
  - on `visibilitychange` to hidden;
  - on `pagehide`.
- **When it is not written:** during a seam preview, or for a video YouTube refused. Within 3 s of the clip's end the record moves on to the next track at its start (the same track when repeating); on the last track it is cleared, as finishing the list does.
- **Validation and expiry:** records older than 7 days, or from another version or malformed, are dropped and removed on read.
- **On load:** with nothing playing, the player bar shows one quiet line: ▶ "Resume ‘Title’ at 2:31" and a ×. The time is relative to the clip, like the bar's clock.
  - Tapping it reloads the playlist and finds the track (by track id, then index, then video). If the list is gone, it falls back to a queue of one. Playback then starts at the saved position, kept inside the clip's start and end.
  - It never autoplays.
  - × forgets the spot.
- **Back from the background:** if music was playing when the tab was hidden and the OS paused it, the bar's subline becomes "Resume at 2:31" until playback restarts. The time is read from YouTube when the tab comes back (background timers are throttled). One tap seeks forward to it if needed (never backwards) and plays.
- **Lock screen while loading:** pause during a track's start holds it paused; play nudges a load the browser refused to start (iOS after an await).

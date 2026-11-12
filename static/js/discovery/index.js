// Lane B (Discovery) entry, imported once by main.js after F's core and routes.
// Wires in through registries only: registerActions, registerKeys, slots.register. The track,
// profile and search routes are registered in routes.js.
//
//   views/track.js     /t/{video_id}   the track page: which playlists hold it, their curators
//   views/profile.js   /u/{handle}     a curator: playlists, taste neighbours, lineage, badges
//   views/discover.js  /discover       tabs, paging, recent activity (also anonymous "/")
//   views/search.js    /search?q=      webjam groups + the opt-in YouTube section
//   related.js   playlist.related + rail.context   "N playlists share these tracks"
//   likes.js     playlist.actions + player.like + L key
//   comments.js  playlist.footer (and used by the track and profile pages)
//   badges.js    the profile's Badges section

import './likes.js';
import './comments.js';
import './related.js';
import './badges.js';

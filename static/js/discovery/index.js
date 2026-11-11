// Lane B (Discovery) entry, imported once by main.js after F's core and routes.
// Wires in through registries only: router.register, registerActions, registerKeys, slots.register.
//
//   views/track.js     /t/{video_id}   the track page: which playlists hold it, their curators
//   views/profile.js   /u/{handle}     a curator: playlists, taste neighbours, lineage, badges
//   views/discover.js  /discover       tabs, paging, recent activity (also anonymous "/")
//   views/search.js    /search?q=      webjam groups + the opt-in YouTube section
//   related.js   playlist.related + rail.context   "N playlists share these tracks"
//   likes.js     playlist.actions + player.like + L key
//   comments.js  playlist.footer (and used by the track and profile pages)
//   badges.js    the profile's Badges section
//   add-to.js    row.menu "Add to playlist…" for read-only discovery lists

import { register } from '../router.js';
import './likes.js';
import './comments.js';
import './related.js';
import './badges.js';
import './add-to.js';

register('search', '/search', () => import('../views/search.js'));
register('track', /^\/t\/(?<vid>[A-Za-z0-9_-]{11})\/?$/, () => import('../views/track.js'));
register('profile', /^\/u\/(?<handle>[A-Za-z0-9_]{3,20})\/?$/, () => import('../views/profile.js'));

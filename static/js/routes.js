// F's routes (docs/phase1b-shell.md §2.1). Imported by main.js before the lane entry modules.
// A lane swaps in its own view by registering the same route name again, e.g. Lane B:
//   router.register('track', '/t/:vid', () => import('./views/track.js'));

import { register } from './router.js';
import { state } from './store.js';

register('home', '/', () => (state.auth.signedIn ? import('./views/home.js') : import('./views/discover.js')));
register('discover', '/discover', () => import('./views/discover.js'));
register('search', '/search', () => import('./views/search.js'));
register('playlist', '/p/:ref', () => import('./views/playlist.js'));
register('track', /^\/t\/(?<vid>[A-Za-z0-9_-]{11})\/?$/, () => import('./views/track.js'));
register('profile', /^\/u\/(?<handle>[A-Za-z0-9_]{3,20})\/?$/, () => import('./views/profile.js'));
register('crate', '/crate', () => import('./views/playlist.js'));
register('not-found', null, () => import('./views/not-found.js'));

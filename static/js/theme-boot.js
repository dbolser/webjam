// Applies a saved theme before first paint. A classic (non-module) script loaded from <head>, so it
// runs before the stylesheets paint; theme.js takes over once the module graph has loaded.
// Same key and values as theme.js: localStorage["webjam.theme"] = 'system' | 'light' | 'dark'.
(function () {
  try {
    if (typeof document === 'undefined' || typeof localStorage === 'undefined') return;
    var t = localStorage.getItem('webjam.theme');
    if (t === 'light' || t === 'dark') document.documentElement.setAttribute('data-theme', t);
  } catch (e) {
    /* storage blocked: follow the system */
  }
})();

// Loads the YouTube IFrame API once. The ready handler is assigned in JS, never as an attribute.

let loading = null;

export function loadYouTube() {
  if (window.YT && window.YT.Player) return Promise.resolve(window.YT);
  if (loading) return loading;
  loading = new Promise((resolve, reject) => {
    const previous = window.onYouTubeIframeAPIReady;
    window.onYouTubeIframeAPIReady = () => {
      if (typeof previous === 'function') previous();
      resolve(window.YT);
    };
    const s = document.createElement('script');
    s.src = 'https://www.youtube.com/iframe_api';
    s.async = true;
    s.addEventListener('error', () => {
      loading = null;
      reject(new Error('The YouTube player could not load.'));
    });
    document.head.appendChild(s);
  });
  return loading;
}

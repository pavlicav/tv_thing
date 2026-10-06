/* Live startup policy shared by hls.js and native HLS. No DVR is promised. */
(function (root) {
  function bufferedAhead(media) {
    for (let i = 0; i < media.buffered.length; i++) {
      if (media.buffered.start(i) <= media.currentTime + 0.05 &&
          media.buffered.end(i) > media.currentTime) {
        return media.buffered.end(i) - media.currentTime;
      }
    }
    return 0;
  }
  function livePosition(media, hls) {
    if (hls && Number.isFinite(hls.liveSyncPosition)) return hls.liveSyncPosition;
    if (!media.seekable.length) return null;
    const i = media.seekable.length - 1;
    return Math.max(media.seekable.start(i), media.seekable.end(i) - 2.4);
  }
  function prepare(media, options) {
    let stopped = false, requested = false, positioned = false;
    const listeners = [];
    function listen(event, fn) {
      media.addEventListener(event, fn);
      listeners.push([event, fn]);
    }
    function cleanup() {
      stopped = true;
      clearInterval(timer);
      clearTimeout(deadline);
      listeners.forEach(([event, fn]) => media.removeEventListener(event, fn));
    }
    function check() {
      if (stopped || requested) return;
      if (!positioned) {
        const target = livePosition(media, options.hls);
        if (target !== null) { media.currentTime = target; positioned = true; }
      }
      if (bufferedAhead(media) < 1.8) return;
      requested = true;
      if (options.onCanPlay) options.onCanPlay();
      media.play().catch(error => {
        if (stopped) return;
        if (error.name === 'NotAllowedError') { clearTimeout(deadline); clearInterval(timer); options.onBlocked(); }
        else { cleanup(); options.onFail(); }
      });
    }
    listen('playing', () => { if (!stopped) { cleanup(); options.onReady(); } });
    listen('progress', check);
    listen('loadeddata', check);
    const timer = setInterval(check, 100);
    const deadline = setTimeout(() => { cleanup(); options.onFail(); }, 20000);
    return cleanup;
  }
  const api = { bufferedAhead, livePosition, prepare };
  if (typeof module !== 'undefined') module.exports = api;
  else root.LivePlayer = api;
})(globalThis);

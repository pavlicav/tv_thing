"""HLS relayed from another box, its segments fetched ahead of the player.

Two relays use it:

* control's VizelVision relay on a second node: the station's HLS, from the primary
  (server.VIZELVISION_RELAY, at ``/stream/vizelvision/``);
* web-basic's peer relay: every stream of the peer node, at ``/stream/peer/`` (web_basic.py):
  its antenna tuners, its internet streams, its slide channels, whatever plays there.

A second node (viviserv2, ~97 ms from the primary over the tailnet) used to fetch each file
only when its player asked, on a new connection each time (VizelVision's server speaks
HTTP/1.0): a 1 s, 1 MB segment spent ~0.8 s in TCP slow start, so the station arrived barely
at real time and stuttered. The peer's tuners did the same in the other direction through
Apache's proxy: a 0.6 s, 200 KB segment of Lansing's WILX took 0.25-0.9 s to reach viviserv
(2026-11-23), and VizelVision's master, reading it at the live edge, stalled and dropped it.

Now a media playlist starts fetches of the newest segments it lists, a segment request
starts the next few after it, several at once, and the player's requests are answered from
memory as soon as those fetches land. A player that keeps to the live edge (a Roku does; so
does GStreamer's hlsdemux, VizelVision's master) would still ask for each segment the moment
it is listed, and wait out its whole transfer, so a media playlist is passed on without its
newest segments until they are in memory: the player plays a second or so further behind,
never waiting. It also says where to start (#EXT-X-START): ExoPlayer, on TV Thing's Android
TV app, keeps only three segments (3 s) behind the newest by default, so any longer hiccup
clipped the picture; 10 s back, it keeps 10 s of buffer.

A segment is named by its path below the playlist's own folder (``0/g17954e-000012.ts`` in
``internet/cabride/0.m3u8`` is ``internet/cabride/0/g17954e-000012.ts``); a URI with a scheme
or a leading slash is the player's to fetch, not this relay's.
"""
from __future__ import annotations

import re
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request

# A relative URI as a playlist lists it: a path below the playlist's own folder (no scheme, no
# leading slash, no "..", every part starting with a letter, digit or underscore), then its query.
RELATIVE = re.compile(r"(?:[A-Za-z0-9_][A-Za-z0-9_.-]*/)*[A-Za-z0-9_][A-Za-z0-9_.-]*(?:\?\S*)?")
FORWARDED = ("Content-Type", "Content-Range", "Accept-Ranges")


class TooLarge(Exception):
    pass


def _path(uri):
    return uri.split("?", 1)[0]


def _is_segment(uri):
    """A URI this relay fetches ahead: relative, and not a playlist."""
    return bool(RELATIVE.fullmatch(uri)) and not _path(uri).endswith(".m3u8")


class Relay:
    AHEAD = 3            # segments fetched after the one a player asks for
    NEWEST = 2           # a media playlist's newest segments, fetched as soon as it lists them
    HOLD = 2             # of those, how many a playlist may leave out while they are fetched
    START = 10           # seconds behind the newest segment a player should play (#EXT-X-START)
    KEEP = 48            # segments held: ~48 MB at 8 Mbps, and past the primary's 30 s window
    WAIT = 30            # seconds a request waits on a fetch already under way
    MAX_BYTES = 16 * 1024 * 1024

    def __init__(self, base, opener, workers=4, *, keep=None, newest=None, by_name=True, start_within=None):
        """``base``: the source's media URL, ending in a slash; ``opener(request, timeout)``.
        ``keep``: segments held (KEEP). ``newest``: a media playlist's newest segments fetched at
        once and, while they are on their way, left out (NEWEST and HOLD): as many as last longer
        than a fetch takes. ``by_name``: a segment's path alone names it, whatever
        its query (MediaMTX's names are unique to its muxer, and the query is the player's
        session); otherwise its query is part of it. ``start_within``: #EXT-X-START at most
        this share of what the playlist lists, for a source whose window is short."""
        self.base, self.opener = base, opener
        self.by_name, self.start_within = by_name, start_within
        if keep is not None:
            self.KEEP = keep
        if newest is not None:
            self.NEWEST = self.HOLD = newest
        self.lock = threading.Lock()
        self.segments = OrderedDict()   # segment key -> Future of (status, headers, content)
        self.playlists = {}             # media playlist path -> its segment URIs, oldest first
        self.sequences = {}             # media playlist path -> its EXT-X-MEDIA-SEQUENCE
        self.pool = ThreadPoolExecutor(workers, thread_name_prefix="hls-relay")

    def get(self, uri, range_header=None):
        """``(status, forwarded headers, content)`` of one file. ``uri``: its path below
        ``base`` and its query."""
        if range_header or not _is_segment(uri):   # playlists and byte ranges go straight through
            result = self.fetch(uri, range_header)
            path = _path(uri)
            if path.endswith(".m3u8") and not range_header and result[0] == 200 and self._listed(path, result[2]):
                result = (result[0], result[1], self._started(self._held(result[2], path)))
            return result
        future = self._start(uri)
        self._ahead(uri)
        return future.result(timeout=self.WAIT)

    def fetch(self, uri, range_header=None):
        headers = {"Accept-Encoding": "identity"}
        if range_header:
            headers["Range"] = range_header
        with self.opener(Request(self.base + uri, headers=headers), timeout=5) as response:
            content = response.read(self.MAX_BYTES + 1)
            if len(content) > self.MAX_BYTES:
                raise TooLarge(uri)
            return (response.status, {name: response.headers[name] for name in FORWARDED if response.headers.get(name)},
                    content)

    def status(self):
        """How much it holds: for a service's status."""
        with self.lock:
            return {"segments": len(self.segments), "playlists": sorted(self.playlists)}

    def _key(self, uri):
        return _path(uri) if self.by_name else uri

    @staticmethod
    def _folder(path):
        """What a playlist's relative URIs are below: ``internet/cabride/`` for
        ``internet/cabride/0.m3u8``, nothing for ``digital.m3u8``."""
        return path.rpartition("/")[0] + "/" if "/" in path else ""

    def _listed(self, path, playlist):
        """A media playlist arrived: remember its segments, and fetch its init and newest ones.
        Whether it was a media playlist."""
        text = playlist.decode("utf-8", "replace")
        if "#EXTINF" not in text or "#EXT-X-ENDLIST" in text or "#EXT-X-PART" in text:
            return False   # a multivariant playlist names playlists; a finished or low-latency one is left as is
        folder = self._folder(path)
        uris = [folder + line.strip() for line in text.splitlines() if _is_segment(line.strip())]
        init = re.search(r'#EXT-X-MAP:URI="([^"]+)"', text)
        sequence = re.search(r"^#EXT-X-MEDIA-SEQUENCE:(\d+)", text, re.M)
        sequence = int(sequence.group(1)) if sequence else 0
        with self.lock:
            # Its numbering went back: the source started again, and may be reusing names.
            if sequence < self.sequences.get(path, 0):
                for stale in {self._key(uri) for uri in self.playlists.get(path, []) + uris}:
                    self.segments.pop(stale, None)
            self.playlists[path], self.sequences[path] = uris, sequence
        for uri in ([folder + init.group(1)] if init and _is_segment(init.group(1)) else []) + uris[-self.NEWEST:]:
            self._start(uri)
        return True

    def _started(self, playlist):
        """``playlist`` with #EXT-X-START after its first line, unless it names its own start."""
        if b"#EXT-X-START:" in playlist or not playlist.startswith(b"#EXTM3U"):
            return playlist
        offset = float(self.START)
        if self.start_within is not None:
            listed = sum(float(seconds) for seconds in re.findall(rb"^#EXTINF:([0-9.]+)", playlist, re.M))
            offset = min(offset, round(listed * self.start_within, 1))
        head, _, rest = playlist.partition(b"\n")
        return head + b"\n#EXT-X-START:TIME-OFFSET=-%.1f\n" % offset + rest

    def _held(self, playlist, path=""):
        """``playlist`` (at ``path``) without its newest segments (HOLD at most) that are not
        in memory yet."""
        folder = self._folder(path)
        lines = playlist.split(b"\n")
        listed = [i for i, line in enumerate(lines) if _is_segment(line.strip().decode("utf-8", "replace"))]
        keep = len(listed)
        with self.lock:
            while keep > 1 and len(listed) - keep < self.HOLD:
                key = self._key(folder + lines[listed[keep - 1]].strip().decode())
                future = self.segments.get(key)
                if future is not None and future.done() and not future.exception():
                    break
                keep -= 1
        if keep == len(listed):
            return playlist
        return b"\n".join(lines[:listed[keep - 1] + 1]) + b"\n"

    def _ahead(self, uri):
        """Fetch the segments that follow ``uri`` in whichever playlist lists it."""
        key = self._key(uri)
        with self.lock:
            following = next((uris[i + 1:i + 1 + self.AHEAD] for uris in self.playlists.values()
                              for i, listed in enumerate(uris) if self._key(listed) == key), [])
        for listed in following:
            self._start(listed)

    def _start(self, uri):
        """The fetch of one segment, shared by every player that asks for it."""
        key = self._key(uri)
        with self.lock:
            future = self.segments.get(key)
            if future is not None:
                self.segments.move_to_end(key)
                return future
            future = self.pool.submit(self.fetch, uri)
            self.segments[key] = future
            while len(self.segments) > self.KEEP:
                oldest = next((held for held, kept in self.segments.items() if kept.done()), None)
                if oldest is None:
                    break
                del self.segments[oldest]
        future.add_done_callback(lambda done: done.exception() and self._forget(key, done))
        return future

    def _forget(self, key, future):
        """A failed fetch is not kept: the next request for that segment tries again."""
        with self.lock:
            if self.segments.get(key) is future:
                del self.segments[key]

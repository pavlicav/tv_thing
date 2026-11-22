"""VizelVision's HLS, relayed from the primary with its segments fetched ahead.

A second node (viviserv2, ~97 ms from the primary over the tailnet) used to fetch each file
only when its player asked, on a new connection each time (VizelVision's server speaks
HTTP/1.0): a 1 s, 1 MB segment spent ~0.8 s in TCP slow start, so the station arrived barely
at real time and stuttered. Now a media playlist starts fetches of the newest segments it
lists, a segment request starts the next few after it, several at once, and the player's
requests are answered from memory as soon as those fetches land.
"""
from __future__ import annotations

import re
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request

# A flat segment URI as MediaMTX's playlists list them: name, then its query.
SEGMENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\.(?:mp4|m4s|ts)(?:\?\S*)?")
FORWARDED = ("Content-Type", "Content-Range", "Accept-Ranges")


class TooLarge(Exception):
    pass


class Relay:
    AHEAD = 3            # segments fetched after the one a player asks for
    NEWEST = 2           # a media playlist's newest segments, fetched as soon as it lists them
    KEEP = 48            # segments held: ~48 MB at 8 Mbps, and past the primary's 30 s window
    WAIT = 30            # seconds a request waits on a fetch already under way
    MAX_BYTES = 16 * 1024 * 1024

    def __init__(self, base, opener, workers=4):
        """``base``: the station's media URL, ending in a slash; ``opener(request, timeout)``."""
        self.base, self.opener = base, opener
        self.lock = threading.Lock()
        self.segments = OrderedDict()   # segment name -> Future of (status, headers, content)
        self.playlists = {}             # media playlist name -> its segment URIs, oldest first
        self.pool = ThreadPoolExecutor(workers, thread_name_prefix="vizelvision-relay")

    def get(self, uri, range_header=None):
        """``(status, forwarded headers, content)`` of one station file. ``uri``: its name and
        query, as the playlist lists it."""
        if range_header or not SEGMENT.fullmatch(uri):   # playlists and byte ranges go straight through
            result = self.fetch(uri, range_header)
            if uri.split("?", 1)[0].endswith(".m3u8") and result[0] == 200:
                self._listed(uri.split("?", 1)[0], result[2])
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

    def _listed(self, name, playlist):
        """A media playlist arrived: remember its segments, and fetch its init and newest ones."""
        text = playlist.decode("utf-8", "replace")
        if "#EXTINF" not in text:
            return   # a multivariant playlist names playlists, not segments
        uris = [line.strip() for line in text.splitlines() if SEGMENT.fullmatch(line.strip())]
        init = re.search(r'#EXT-X-MAP:URI="([^"]+)"', text)
        with self.lock:
            self.playlists[name] = uris
        for uri in ([init.group(1)] if init and SEGMENT.fullmatch(init.group(1)) else []) + uris[-self.NEWEST:]:
            self._start(uri)

    def _ahead(self, uri):
        """Fetch the segments that follow ``uri`` in whichever playlist lists it."""
        name = uri.split("?", 1)[0]
        with self.lock:
            following = next((uris[i + 1:i + 1 + self.AHEAD] for uris in self.playlists.values()
                              for i, listed in enumerate(uris) if listed.split("?", 1)[0] == name), [])
        for listed in following:
            self._start(listed)

    def _start(self, uri):
        """The fetch of one segment, shared: segment names are unique to MediaMTX's muxer, so
        one fetch serves every player whatever its session."""
        name = uri.split("?", 1)[0]
        with self.lock:
            future = self.segments.get(name)
            if future is not None:
                self.segments.move_to_end(name)
                return future
            future = self.pool.submit(self.fetch, uri)
            self.segments[name] = future
            while len(self.segments) > self.KEEP:
                oldest = next((key for key, held in self.segments.items() if held.done()), None)
                if oldest is None:
                    break
                del self.segments[oldest]
        future.add_done_callback(lambda done: done.exception() and self._forget(name, done))
        return future

    def _forget(self, name, future):
        """A failed fetch is not kept: the next request for that segment tries again."""
        with self.lock:
            if self.segments.get(name) is future:
                del self.segments[name]

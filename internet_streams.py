"""Internet streams: live HLS from the web, as TV Thing channels.

``internet_streams.json`` lists them. Each has an id (its key in the lineup,
``internet/<id>``), a name, and a resolver that finds its current HLS URL:

* ``url`` -- a fixed playlist URL (``"url": "https://.../index.m3u8"``).
* ``yle-areena`` -- a live Yle Areena programme (``"program": "1-78104441"``).
  Areena hands out a playlist URL with a CDN token good for about a day, so
  it is looked up again every ``RESOLVE_SECONDS`` and whenever the CDN
  refuses it.

Finding the URL is the control plane's (``resolve_stream``, by ``RESOLVERS``);
relaying it is the feed's (``InternetStreams``, which asks through its
``resolver``). Split, the feed asks control's ``GET /internal/internet/<id>/resolve``
(``resolve_through``); a fixed URL it reads from the file itself. A resolver that
fails while a good URL is in hand leaves that URL playing and is asked again a
little later, so a relay keeps going while the resolving side is down or
restarting. A stream no one has played since the feed started cannot start
until control answers.

TV Thing relays the stream at ``/stream/internet/<id>/index.m3u8``: the
master's renditions become ``0.m3u8``, ``1.m3u8``, ... and their segments
``0/seg_1.ts`` and so on, in that same namespace, so a client keeps one
stable URL however often the upstream one changes. A media playlist is
trimmed to its live edge (a broadcaster's DVR window can list hours of
segments, re-read every few seconds).

The segments are relayed too, not handed to the client: Yle's CDN ties a
segment token to the request headers that fetched its playlist (a different
User-Agent, or any Origin, is refused), so every request upstream goes out
alike from here. A segment from another host than its playlist is left
absolute and fetched by the client directly. No tuner, lock or encoder is
involved.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import threading
import time
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import Request, urlopen

SOURCE = "internet"
MAX_PLAYLIST_BYTES = 16 * 1024 * 1024
MAX_SEGMENT_BYTES = 64 * 1024 * 1024
TIMEOUT_SECONDS = 5
RESOLVE_SECONDS = 30 * 60      # How long a resolved URL is used before asking again.
RESOLVE_RETRY_SECONDS = 30     # How long a URL kept past a failed look-up waits for the next.
RESOLVE_THROUGH_SECONDS = 10   # How long the feed waits for control's look-up (Yle's is 5 s).
MASTER_SECONDS = 5 * 60        # How long a master playlist is reused.
LISTINGS_SECONDS = 60          # How long a stream's guide listings are reused.
LISTINGS_STALE_SECONDS = 15 * 60   # How long the last good listings stand in when the URL fails.
LIVE_WINDOW_SECONDS = 120      # How much of a live media playlist is passed on.
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) TV-Thing"

# Yle's public player keys, the ones areena.yle.fi's own player sends.
YLE_PREVIEW_URL = ("https://player.api.yle.fi/v1/preview/{program}.json?language=fin&ssl=true"
                   "&countryCode=US&host=areenaylefi&app_id=player_static_prod"
                   "&app_key=8930d72170e48303cf5f3867780d549b&isPortabilityRegion=true")

# Tags that belong to the segment after them rather than to the playlist.
_SEGMENT_TAGS = ("#EXTINF", "#EXT-X-BYTERANGE", "#EXT-X-DISCONTINUITY", "#EXT-X-KEY",
                 "#EXT-X-MAP", "#EXT-X-PROGRAM-DATE-TIME", "#EXT-X-DATERANGE", "#EXT-X-GAP",
                 "#EXT-X-CUE")
_URI_ATTRIBUTE = re.compile(r'URI="([^"]*)"')
_RENDITION_NAME = re.compile(r"(\d{1,3})\.m3u8")
# A segment's path below its playlist: flat names and subdirectories, no climbing out.
_SEGMENT_PATH = re.compile(r"(?!.*(?:^|/)\.\.?(?:/|$))[A-Za-z0-9_~%=,+.-][A-Za-z0-9_~%=,+./-]*")
TOP_LEVEL = "media"     # The segment directory of a stream whose URL is itself a media playlist.


# Who relays a stream: the feed (beside the tuners), or web-basic, the simple web streams'
# own service (web_basic.py), so a feed restart leaves it playing.
SERVICES = ("feed", "web-basic")


class StreamConfigError(ValueError):
    """internet_streams.json does not describe usable streams."""


class StreamUnavailable(Exception):
    """The stream cannot be played right now; ``status`` is the HTTP status to answer."""

    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Stream:
    id: str
    name: str
    resolver: str
    params: dict = field(default_factory=dict)
    note: str | None = None
    guide: str | None = None   # a listings URL for TV Thing's guide (see parse_listings)
    service: str = "feed"      # which service relays it: the feed, or web-basic (web_basic.py)


@dataclass(frozen=True)
class Resolver:
    fields: tuple           # what a stream of this kind lists in internet_streams.json
    find: object            # (params, fetch) -> the playlist URL it plays now
    fixed: bool = False     # the URL never changes, so it is never looked up again


# A new kind of stream is one function and one entry here.
RESOLVERS = {
    "url": Resolver(("url",), lambda params, fetch: params["url"], fixed=True),
    "yle-areena": Resolver(("program",), lambda params, fetch: resolve_yle_areena(params["program"], fetch)),
}


def parse_streams(document):
    """Validate an internet_streams.json document: ``{id: Stream}`` in file order."""
    if not isinstance(document, dict) or not isinstance(document.get("streams", []), list):
        raise StreamConfigError("internet_streams.json must be an object with a streams list")
    streams = {}
    for index, raw in enumerate(document.get("streams") or []):
        where = f"streams[{index}]"
        if not isinstance(raw, dict):
            raise StreamConfigError(f"{where}: a stream is an object")
        stream_id = str(raw.get("id") or "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", stream_id):
            raise StreamConfigError(f"{where}: an id is lowercase letters, digits and dashes")
        if stream_id in streams:
            raise StreamConfigError(f"{where}: {stream_id} is listed twice")
        resolver = raw.get("resolver")
        if resolver not in RESOLVERS:
            raise StreamConfigError(f"{where}: resolver is one of {', '.join(sorted(RESOLVERS))}")
        params = {key: str(raw.get(key) or "") for key in RESOLVERS[resolver].fields}
        missing = [key for key, value in params.items() if not value]
        if missing:
            raise StreamConfigError(f"{where}: a {resolver} stream needs {', '.join(missing)}")
        if resolver == "url" and not params["url"].startswith(("http://", "https://")):
            raise StreamConfigError(f"{where}: url must be http or https")
        guide = raw.get("guide") or None
        if guide is not None and not str(guide).startswith(("http://", "https://")):
            raise StreamConfigError(f"{where}: guide must be http or https")
        service = raw.get("service", "feed")
        if service not in SERVICES:
            raise StreamConfigError(f"{where}: service is one of {', '.join(SERVICES)}")
        streams[stream_id] = Stream(stream_id, str(raw.get("name") or stream_id), resolver,
                                    params, raw.get("note") or None, guide and str(guide), service)
    return streams


def parse_listings(document):
    """Guide events from a stream's listings, ``{"events": [{"start", "duration", "title",
    "description"?}]}`` (Unix seconds, whole seconds), in start order. An event without a
    start, a positive duration or a title is left out."""
    events = []
    raw_events = document.get("events") if isinstance(document, dict) else None
    for raw in raw_events if isinstance(raw_events, list) else []:
        if not isinstance(raw, dict):
            continue
        start, duration, title = raw.get("start"), raw.get("duration"), raw.get("title")
        if (not isinstance(start, int) or isinstance(start, bool) or not isinstance(duration, int)
                or isinstance(duration, bool) or duration <= 0 or not isinstance(title, str) or not title):
            continue
        event = {"start": start, "duration": duration, "title": title}
        if isinstance(raw.get("description"), str) and raw["description"]:
            event["description"] = raw["description"]
        events.append(event)
    events.sort(key=lambda event: event["start"])
    return events


def _open(url, accept="*/*", headers=None):
    """An open GET response for ``url``, every request alike (see the module doc)."""
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept,
                                    "Accept-Encoding": "identity", **(headers or {})})
    return urlopen(request, timeout=TIMEOUT_SECONDS)


def _fetch(url, accept="*/*", headers=None):
    """GET ``url``: (final URL, body text). Raises HTTPError/URLError/OSError."""
    with _open(url, accept, headers) as response:
        body = response.read(MAX_PLAYLIST_BYTES + 1)
        if len(body) > MAX_PLAYLIST_BYTES:
            raise StreamUnavailable("Upstream playlist too large")
        return response.geturl(), body.decode("utf-8", "replace")


def resolve_yle_areena(program, fetch=_fetch):
    """The playlist URL of what Yle Areena programme ``program`` has live now."""
    # Without Areena's own origin the API answers "not_allowed".
    _, body = fetch(YLE_PREVIEW_URL.format(program=quote(program, safe="-")), "application/json",
                    {"Origin": "https://areena.yle.fi", "Referer": "https://areena.yle.fi/"})
    try:
        data = json.loads(body).get("data") or {}
    except ValueError:
        raise StreamUnavailable(f"Yle Areena answered {program} with something other than JSON")
    for kind in ("ongoing_event", "ongoing_channel", "ongoing_ondemand"):
        item = data.get(kind) or {}
        if item.get("manifest_url"):
            return item["manifest_url"]
    if data.get("not_allowed"):
        raise StreamUnavailable(f"Yle Areena will not play {program} here", status=403)
    if data.get("pending_event"):
        raise StreamUnavailable(f"Yle Areena's {program} has not started yet", status=503)
    raise StreamUnavailable(f"Yle Areena has nothing live for {program}", status=503)


def resolve_stream(stream, fetch=_fetch):
    """The playlist URL ``stream`` plays now, by its resolver: the control plane's side of
    the relay. Raises StreamUnavailable, or HTTPError/URLError/OSError from the lookup."""
    return RESOLVERS[stream.resolver].find(stream.params, fetch)


def resolve_through(control_url, stream, timeout=RESOLVE_THROUGH_SECONDS):
    """The playlist URL ``stream`` plays now, as control's resolve_stream finds it: the feed's
    side of the relay when split. Raises StreamUnavailable, with control's status when it
    refuses and 503 when it does not answer."""
    url = f"{control_url.rstrip('/')}/internal/internet/{quote(stream.id, safe='')}/resolve"
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read(64 * 1024))["url"]
    except HTTPError as error:
        try:
            message = json.loads(error.read(64 * 1024)).get("error")
        except (OSError, ValueError, AttributeError):
            message = None
        raise StreamUnavailable(message or f"TV Thing's control answered {error.code} for "
                                f"{stream.name}'s URL", status=error.code) from None
    except (URLError, OSError, TimeoutError) as error:
        raise StreamUnavailable(f"TV Thing's control is not answering, so {stream.name}'s URL "
                                f"cannot be looked up: {error}", status=503) from None
    except (KeyError, TypeError, ValueError):
        raise StreamUnavailable(f"TV Thing's control gave no URL for {stream.name}") from None


def _is_media(text):
    return "#EXTINF" in text


def _rendition_uris(master):
    """A master playlist's rendition URIs, in order: variants and EXT-X-MEDIA alike."""
    uris = []
    for line in master.splitlines():
        line = line.strip()
        if line.startswith("#"):
            if line.startswith("#EXT-X-MEDIA:") or line.startswith("#EXT-X-I-FRAME-STREAM-INF:"):
                uris += _URI_ATTRIBUTE.findall(line)
        elif line:
            uris.append(line)
    return uris


def rewrite_master(master):
    """The master with each rendition renamed ``<n>.m3u8``, relative to it."""
    counter = iter(range(10 ** 6))
    lines = []
    for line in master.splitlines():
        stripped = line.strip()
        if stripped.startswith("#EXT-X-MEDIA:") or stripped.startswith("#EXT-X-I-FRAME-STREAM-INF:"):
            line = _URI_ATTRIBUTE.sub(lambda _: f'URI="{next(counter)}.m3u8"', stripped)
        elif stripped and not stripped.startswith("#"):
            line = f"{next(counter)}.m3u8"
        lines.append(line)
    return "\n".join(lines) + "\n"


def _duration(tag):
    try:
        return float(tag.split(":", 1)[1].split(",", 1)[0])
    except (IndexError, ValueError):
        return 0.0


def _directory(url):
    """``url`` up to and including the last slash of its path, without a query."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{posixpath.dirname(parts.path)}/"


def rewrite_media(media, base_url, window=LIVE_WINDOW_SECONDS, relay=None):
    """A media playlist, if live only its last ``window`` seconds.

    With ``relay`` (a directory name such as ``"1"``), a URI below the
    playlist's own directory becomes ``<relay>/<path below it>``; every other
    URI is made absolute.
    """
    directory = _directory(base_url)

    def absolute(uri):
        target = urljoin(base_url, uri)
        if relay is not None and target.startswith(directory) and len(target) > len(directory):
            return f"{relay}/{target[len(directory):]}"
        return target

    header, segments, pending, trailer = [], [], [], []
    for line in media.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            stripped = _URI_ATTRIBUTE.sub(lambda m: f'URI="{absolute(m.group(1))}"', stripped)
            if stripped.startswith("#EXT-X-ENDLIST"):
                trailer.append(stripped)
            elif segments or pending or stripped.startswith(_SEGMENT_TAGS):
                pending.append(stripped)
            else:
                header.append(stripped)
        else:
            segments.append(pending + [absolute(stripped)])
            pending = []

    live = not trailer and not any(tag.startswith("#EXT-X-PLAYLIST-TYPE:VOD") for tag in header)
    if live and window:
        kept, total = len(segments), 0.0
        while kept > 0 and total < window:
            kept -= 1
            total += next((_duration(t) for t in segments[kept] if t.startswith("#EXTINF")), 0.0)
        dropped, segments = segments[:kept], segments[kept:]
        if dropped:
            # Renumber, and carry the key and init section the kept segments still use.
            carried = {}
            for tag in (t for segment in dropped for t in segment):
                for name in ("#EXT-X-KEY", "#EXT-X-MAP"):
                    if tag.startswith(name):
                        carried[name] = tag
            if segments:
                first = segments[0]
                segments[0] = [tag for name, tag in carried.items()
                               if not any(t.startswith(name) for t in first)] + first
            discontinuities = sum(t.startswith("#EXT-X-DISCONTINUITY") and
                                  not t.startswith("#EXT-X-DISCONTINUITY-SEQUENCE")
                                  for segment in dropped for t in segment)
            header = [_bump(tag, "#EXT-X-MEDIA-SEQUENCE:", len(dropped))
                      for tag in header if not tag.startswith("#EXT-X-PLAYLIST-TYPE:EVENT")]
            if discontinuities:
                if any(t.startswith("#EXT-X-DISCONTINUITY-SEQUENCE:") for t in header):
                    header = [_bump(t, "#EXT-X-DISCONTINUITY-SEQUENCE:", discontinuities)
                              for t in header]
                else:
                    header.append(f"#EXT-X-DISCONTINUITY-SEQUENCE:{discontinuities}")
            if not any(t.startswith("#EXT-X-MEDIA-SEQUENCE:") for t in header):
                header.append(f"#EXT-X-MEDIA-SEQUENCE:{len(dropped)}")
    lines = header + [line for segment in segments for line in segment] + pending + trailer
    return "\n".join(lines) + "\n"


def _bump(tag, name, by):
    if not tag.startswith(name):
        return tag
    try:
        return f"{name}{int(tag[len(name):]) + by}"
    except ValueError:
        return tag


class InternetStreams:
    """The configured streams and the playlist relay, safe across threads."""

    def __init__(self, config_path, fetch=_fetch, clock=time.monotonic, open_url=_open, resolver=None):
        self.config_path = config_path
        self.fetch = fetch
        # Who finds a stream's URL: control's resolve_stream, in this process or (split) over
        # HTTP. Called with a Stream; raises when it cannot say.
        self.resolver = resolver or (lambda stream: resolve_stream(stream, self.fetch))
        self.open_url = open_url
        self.clock = clock
        self._lock = threading.Lock()
        self._config = (None, {})    # (stat signature, {id: Stream} or StreamConfigError)
        self._resolved = {}          # id -> (url, at, next look-up if the last one failed)
        self._masters = {}           # id -> (master URL, text, at)
        self._listings = {}          # id -> (events, at)

    def streams(self):
        """``{id: Stream}``; StreamConfigError when the file is unusable, {} when absent."""
        try:
            stat = os.stat(self.config_path)
        except FileNotFoundError:
            return {}
        signature = (stat.st_mtime_ns, stat.st_size)
        with self._lock:
            cached_signature, cached = self._config
            if cached_signature != signature:
                try:
                    with open(self.config_path) as f:
                        cached = parse_streams(json.load(f))
                except (OSError, ValueError) as error:
                    cached = error if isinstance(error, StreamConfigError) else StreamConfigError(
                        f"internet_streams.json: {error}")
                self._config = (signature, cached)
        if isinstance(cached, StreamConfigError):
            raise cached
        return cached

    def _stream(self, stream_id):
        stream = self.streams().get(stream_id)
        if stream is None:
            raise StreamUnavailable(f"No internet stream {stream_id!r}", status=404)
        return stream

    def _resolve(self, stream):
        now = self.clock()
        with self._lock:
            url, at, retry_at = self._resolved.get(stream.id, (None, 0, None))
        if url and (RESOLVERS[stream.resolver].fixed or now - at < RESOLVE_SECONDS
                    or (retry_at is not None and now < retry_at)):
            return url
        try:
            fresh = self.resolver(stream)
        except (StreamUnavailable, OSError, ValueError) as error:
            if not url:
                raise
            # The URL in hand still played a moment ago: keep it, and ask again later. One the
            # CDN refuses was forgotten (forget), so this never props up a dead token.
            print(f"[Internet] {stream.name}: kept its URL, the look-up failed: {error}", flush=True)
            with self._lock:
                self._resolved[stream.id] = (url, at, now + RESOLVE_RETRY_SECONDS)
            return url
        with self._lock:
            self._resolved[stream.id] = (fresh, now, None)
        return fresh

    def _master(self, stream):
        now = self.clock()
        with self._lock:
            cached = self._masters.get(stream.id)
        if cached and now - cached[2] < MASTER_SECONDS:
            return cached[0], cached[1]
        final_url, text = self.fetch(self._resolve(stream), "application/vnd.apple.mpegurl")
        if not text.lstrip().startswith("#EXTM3U"):
            raise StreamUnavailable(f"{stream.name}'s playlist is not HLS")
        with self._lock:
            self._masters[stream.id] = (final_url, text, now)
        return final_url, text

    def listings(self, stream_id):
        """The guide events a stream's ``guide`` URL lists (parse_listings); [] for a stream
        without one. Reused for a minute; when the URL fails, the last good listings stand in
        for up to 15 minutes, then the row goes empty."""
        stream = self.streams().get(stream_id)
        if stream is None or not stream.guide:
            return []
        now = self.clock()
        with self._lock:
            cached = self._listings.get(stream_id)
        if cached and now - cached[1] < LISTINGS_SECONDS:
            return cached[0]
        try:
            _, text = self.fetch(stream.guide, "application/json")
            events = parse_listings(json.loads(text))
        except (HTTPError, URLError, OSError, TimeoutError, ValueError) as error:
            print(f"[Internet] {stream.name}'s listings: {error}", flush=True)
            return cached[0] if cached and now - cached[1] < LISTINGS_STALE_SECONDS else []
        with self._lock:
            self._listings[stream_id] = (events, now)
        return events

    def forget(self, stream_id):
        """Drop what is cached for a stream, so the next request resolves it afresh."""
        with self._lock:
            self._resolved.pop(stream_id, None)
            self._masters.pop(stream_id, None)

    def _retrying(self, stream, action):
        """``action()``, looking the stream up again once if the CDN refuses its token."""
        for attempt in (1, 2):
            try:
                return action()
            except HTTPError as error:
                # An expired CDN token answers 403 (or 404, 410).
                if attempt == 2 or error.code not in (401, 403, 404, 410):
                    raise StreamUnavailable(f"{stream.name}: upstream answered {error.code}",
                                            status=502) from None
                self.forget(stream.id)
            except (URLError, OSError, TimeoutError) as error:
                raise StreamUnavailable(f"{stream.name} unreachable: {error}") from None

    def _media_url(self, stream, rendition):
        """The upstream URL of a rendition's media playlist (``TOP_LEVEL`` for the stream's own)."""
        master_url, master = self._master(stream)
        if _is_media(master):
            if rendition != TOP_LEVEL:
                raise StreamUnavailable(f"{stream.name} has no rendition {rendition}", status=404)
            return master_url
        uris = _rendition_uris(master)
        if not rendition.isdigit() or int(rendition) >= len(uris):
            raise StreamUnavailable(f"{stream.name} has no rendition {rendition}", status=404)
        return urljoin(master_url, uris[int(rendition)])

    def playlist(self, stream_id, filename):
        """The relayed playlist ``filename`` (``index.m3u8`` or ``<n>.m3u8``) of a stream."""
        stream = self._stream(stream_id)
        return self._retrying(stream, lambda: self._playlist(stream, filename))

    def _playlist(self, stream, filename):
        master_url, master = self._master(stream)
        if filename == "index.m3u8" and _is_media(master):
            return rewrite_media(master, master_url, relay=TOP_LEVEL)
        if filename == "index.m3u8":
            return rewrite_master(master)
        match = _RENDITION_NAME.fullmatch(filename)
        if not match:
            raise StreamUnavailable(f"{stream.name} has no {filename}", status=404)
        rendition = str(int(match.group(1)))
        media_url, media = self.fetch(self._media_url(stream, rendition),
                                      "application/vnd.apple.mpegurl")
        return rewrite_media(media, media_url, relay=rendition)

    def segment(self, stream_id, rendition, path, query=""):
        """An open upstream response for ``<rendition>/<path>``; the caller closes it."""
        stream = self._stream(stream_id)
        if not _SEGMENT_PATH.fullmatch(path) or "//" in path:
            raise StreamUnavailable("Invalid segment path", status=400)

        def open_segment():
            url = urljoin(_directory(self._media_url(stream, rendition)), path)
            return self.open_url(url + (f"?{query}" if query else ""))
        return self._retrying(stream, open_segment)

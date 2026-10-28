"""TV Thing's channel list: one registry of every channel, whatever carries it.

Each kind of source has one adapter here:

* ``digital``: the antenna's channels (channels.json), played on the digital lane;
* ``cable``: the analog tuner's RF plan, on the analog lane. It tunes, but is no
  part of the lineup: its numbers are RF channels, not TV Thing's;
* ``roku``: the Roku's sources (YouTube TV's rows, the day's games, Manual
  Roku), on the Roku lane;
* ``internet``: the web streams the feed relays (internet_streams.py);
* ``vizelvision``: the station itself, at each number it holds;
* ``slides``: the station's slide channels, such as the Sports Guide.

An adapter lists its channels as Channel, describes the sources a tune names,
adds its rows to the guide, and tunes. server.py's lineup, source catalog,
guide, guide-now and tune iterate the registry: a new kind of source is a new
adapter, not a new branch in each of them.

A channel's id is ``source/channel``, as lineup.json keys it (lineup.py):
``digital/22.4``, ``youtube-tv/NBC``, ``internet/pentulive``,
``vizelvision/sports-guide``. A game's is ``mlb/<game id>``; the station's is
``vizelvision`` at each number it holds. An id never holds a TV Thing number,
so numbers move freely, and recordings, VCR rules and VizelVision's targets,
which name a source and its channel id, need no migration.

The adapters are server.py's. ``host`` is that module (or a stand-in in a
test), read whenever an adapter asks, so what a test patches there is what
they use.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import internet_streams
import lineup
import roku_lane
from roku_control.networks import NETWORKS


@dataclass(frozen=True)
class Channel:
    id: str                     # source/channel; never a TV Thing number
    source_id: str              # what a tune or a catalog names: digital, youtube-tv, mlb, internet...
    channel_id: str | None      # the source's own id for it: wrc_hd_4_1, NBC, a game's id
    number: int | None          # its TV Thing number
    name: str | None            # its TV Thing name
    section: str | None         # its lineup section
    kind: str                   # its adapter: digital, roku, internet, vizelvision, slides
    lane: str | None = None     # the feed lane that plays it; None for a relay or the station
    node: str | None = None     # the feed node that makes its stream; None for the station's
    network: str | None = None  # the broadcast network it carries (networks.json)
    market: str | None = None   # the market whose station it is
    tune: dict | None = None    # the POST /api/v1/tune body that plays it; None: open the stream
    stream: str | None = None   # the playlist that plays it (a lane's, once tuned)
    listings: str | None = None  # its row's key in /api/v1/guide
    detail: dict = field(default_factory=dict)   # what its kind says besides
    # The row /api/v1/lineup has always given it, in its kind's own shape.
    row: dict = field(default_factory=dict, repr=False, compare=False)

    def view(self):
        """The channel as /api/v1/channels gives it."""
        return {"id": self.id, "source_id": self.source_id, "channel_id": self.channel_id,
                "number": self.number, "name": self.name, "section": self.section,
                "kind": self.kind, "lane": self.lane, "node": self.node,
                "network": self.network, "market": self.market, "tune": self.tune,
                "stream": self.stream, "listings": self.listings, "detail": self.detail}


def lineup_order(channel):
    """The lineup's order: by number, the unnumbered last, then by the source's name."""
    return channel.number is None, channel.number or 0, str(channel.row.get("name") or "")


@dataclass
class Found:
    """What a listing of the channels learned besides them: the games' day, and the
    catalogs that did not answer (each costs its own channels only)."""
    date: str | None = None
    errors: list = field(default_factory=list)


@dataclass(frozen=True)
class GuideWindow:
    """The part of the guide asked for: ``floor`` to ``ceiling`` (None: all of it),
    and one row's key, ``number``, or every row."""
    now: int
    floor: int
    ceiling: int | None = None
    number: str | None = None

    def current(self, events):
        return [event for event in events
                if event["start"] + event["duration"] > self.floor
                and (self.ceiling is None or event["start"] < self.ceiling)]

    def wants(self, key):
        return not self.number or key == self.number

    def may_want(self, prefix):
        """Whether a row keyed ``prefix...`` can be asked for: rows that cost a request each
        (a schedule, a listing) are read only then."""
        return not self.number or str(self.number).startswith(prefix)


def now_and_next(rows, now):
    """``{key: {"now", "next"}}`` for the guide rows with either: the last event on now,
    the first after it."""
    entries = {}
    for key, channel in rows.items():
        current = following = None
        for event in channel["events"]:
            if event["start"] <= now < event["start"] + event["duration"]:
                current = event
            elif event["start"] > now and following is None:
                following = event
        if current or following:
            entries[key] = {"now": current, "next": following}
    return entries


class Adapter:
    kind = None
    tunes = ()          # the source ids its tune() takes
    aliases = {}        # other names a tune request may give them
    tune_label = None   # how a refused tune names them

    def __init__(self, host):
        self.host = host

    def catalog(self):
        """Its part of /api/v1/sources: ``{"sources", "listed", "lineup"}``, each optional."""
        return {}

    def channels(self, day, found):
        """Its channels in the lineup, with ``day``'s games."""
        return []

    def guide(self, window):
        """``(rows by key, summary fields)``: its part of /api/v1/guide."""
        return {}, {}

    def guide_now(self, now):
        """``(now-and-next by key, summary fields)``: its part of /api/v1/guide/now."""
        return {}, {}

    def tune(self, source_id, body, quality):
        """Tune one of ``tunes``: ``(payload, status)``, in the shape every tune answers."""
        raise NotImplementedError


class AntennaChannels(Adapter):
    kind = "digital"
    tunes = ("digital",)
    aliases = {"tv": "digital", "television": "digital"}
    tune_label = "digital"

    def catalog(self):
        source = self.host.api_v1_digital_source()["source"]
        return {"sources": [dict(source, items_path="/api/v1/sources/digital")]}

    def channels(self, day, found):
        host = self.host
        listed = host.load_channels()
        lane = host.LANE_DIGITAL
        found_channels = []
        for key, channel in zip(lineup.antenna_keys(listed), host.numbered_channels(listed)):
            network, market = NETWORKS.antenna_channel(channel.get("number")) or (None, None)
            found_channels.append(Channel(
                id=f"{lineup.ANTENNA}/{key}", source_id="digital", channel_id=channel["id"],
                number=channel.get("tv_number"), name=channel.get("tv_name") or channel.get("name"),
                section=channel.get("tv_section"), kind=self.kind, lane=lane, node=host.FEED_CONFIG.node,
                network=network, market=market, tune={"source": "digital", "channel_id": channel["id"]},
                stream=f"/stream/{host.LANE_PLAYLIST[lane]}", listings=channel.get("number"),
                detail={"source_name": channel.get("name"), "antenna_number": channel.get("number"),
                        "rf_channel": channel.get("rf_channel")},
                row={key: channel.get(key) for key in ("tv_number", "tv_name", "tv_section")}
                | {"kind": "digital", "source_id": "digital", "channel_id": channel["id"],
                   "name": channel.get("name"), "antenna_number": channel.get("number"),
                   "rf_channel": channel.get("rf_channel")}))
        return found_channels

    def guide(self, window):
        """The broadcast guide's rows, keyed by antenna number, its games named from ESPN;
        and the summary of the stored guide, which the page's top line describes."""
        host = self.host
        guide = host.epg.load_guide()
        refresh_times = [f"{h:02d}:{m:02d}" for h, m in host.EPG_REFRESH_TIMES]
        if not guide:
            return {}, {"available": False, "refresh_times": refresh_times,
                        "refreshing": host.guide_refreshing, "last_error": host.guide_status["last_error"]}
        numbers = {}
        for channel in host.numbered_channels(host.load_channels()):
            if "tv_number" in channel:
                numbers.setdefault(channel.get("number"), {
                    key: channel[key] for key in ("tv_number", "tv_name", "tv_section")})
        rows = {}
        for key, channel in guide["channels"].items():
            if window.wants(key):
                rows[key] = {**channel, **numbers.get(key, {}), "key": key, "kind": "digital",
                             "events": window.current(channel["events"])}
        now = window.now
        return host.name_antenna_games(rows), {
            "available": True,
            "generated_at": guide.get("generated_at"),
            "age_seconds": now - guide.get("generated_at", now),
            # Recomputed against the clock, never passed through. The stored
            # figure was true when the sweep ran; hours later it overstates what
            # is left by exactly the age of the file, which is the one number a
            # viewer must not be misled about.
            "coverage_hours": host.remaining_coverage_hours(guide, now),
            "coverage_hours_at_refresh": guide.get("coverage_hours"),
            "horizon": guide.get("horizon"),
            "event_count": guide.get("event_count"),
            "channels_with_events": guide.get("channels_with_events"),
            "compressed_strings": guide.get("compressed_strings", 0),
            "descriptions": guide.get("descriptions", False),
            "blocks_requested": guide.get("blocks_requested"),
            "elapsed_seconds": guide.get("elapsed_seconds"),
            "refresh_times": refresh_times,
            "next_refresh": host.next_refresh_at(host.EPG_REFRESH_TIMES),
            "refreshing": host.guide_refreshing,
            "last_error": host.guide_status["last_error"],
        }

    def guide_now(self, now):
        guide = self.host.epg.load_guide()
        entries = now_and_next((guide or {}).get("channels", {}), now)
        # The antenna's games named by their teams, as the guide names them; the
        # named airings come back in order, now before next.
        airings = {number: {"events": [event for event in entry.values() if event]}
                   for number, entry in entries.items()}
        for number, channel in self.host.name_antenna_games(airings).items():
            named = iter(channel["events"])
            entries[number] = {slot: event and next(named) for slot, event in entries[number].items()}
        return entries, {"available": bool(guide), "generated_at": (guide or {}).get("generated_at")}

    def tune(self, source_id, body, quality):
        host = self.host
        result, status = host.perform_digital_tune(
            body.get("channel_id"), quality, token=body.get("token"), override=bool(body.get("override")))
        return host.lane_tune_response(result, status, host.LANE_DIGITAL)


class CableChannels(Adapter):
    kind = "cable"
    tunes = ("cable",)
    aliases = {"analog": "cable", "ntsc": "cable"}
    tune_label = "cable"

    def catalog(self):
        source = self.host.api_v1_cable_source()["source"]
        return {"sources": [dict(source, items_path="/api/v1/sources/cable")]}

    def tune(self, source_id, body, quality):
        host = self.host
        result, status = host.perform_cable_tune(
            body.get("channel"), quality, token=body.get("token"), override=bool(body.get("override")))
        return host.lane_tune_response(result, status, host.LANE_ANALOG)


class RokuChannels(Adapter):
    kind = "roku"
    tunes = roku_lane.SOURCES
    tune_label = "a Roku source (" + ", ".join(roku_lane.SOURCES) + ")"
    # Catalogs whose items are a day's games, numbered on the Live Sports numbers.
    GAMES = (*roku_lane.GAME_SOURCES, *roku_lane.LISTED_SOURCES)

    def _stream(self):
        return f"/stream/{self.host.LANE_PLAYLIST[self.host.LANE_ROKU]}"

    def catalog(self):
        host = self.host
        if host.roku is None:
            return {"sources": [], "listed": []}
        available = host.roku_sources_available()
        return {"sources": [dict(source, stream_path=self._stream()) for source in host.roku.source_list(available)],
                # Catalogs the Watch page lists but nothing can tune. Apart from "sources",
                # so vizelVision and the VCR never offer them.
                "listed": host.roku.listed_source_list(available)}

    def channels(self, day, found):
        host = self.host
        if host.roku is None:
            return []
        listings = {}
        for source_id in (*roku_lane.SOURCES, *roku_lane.LISTED_SOURCES):
            try:
                listings[source_id] = host.roku.catalog(source_id, day)
            except Exception as error:   # noqa: BLE001 - one catalog never sinks the lineup
                found.errors.append({"source_id": source_id, "message": str(error)})
        found_channels = []
        for source_id, listing in host.numbered_roku_listings(listings).items():
            found.date = listing.get("date") or found.date
            found_channels += [self._channel(source_id, listing, item) for item in listing["items"]]
        return found_channels

    def _channel(self, source_id, listing, item):
        channel_id, number = str(item.get("id")), item.get("tv_number")
        games = source_id in self.GAMES
        timing = {key: item[key] for key in ("start_time", "state", "status", "playable") if key in item}
        network, market = (NETWORKS.youtube_tv_row((item.get("id"), item.get("name"), *(item.get("aliases") or ())))
                           if source_id == roku_lane.YOUTUBE_TV else None) or (None, None)
        tune = None
        if source_id in roku_lane.SOURCES:
            tune = {"source": source_id, "channel_id": channel_id}
            if games and listing.get("date"):
                tune["date"] = listing["date"]
        listings = (f"{roku_lane.YOUTUBE_TV}/{channel_id}" if source_id == roku_lane.YOUTUBE_TV
                    else f"games/{number}" if games and isinstance(number, int) else None)
        return Channel(
            id=f"{source_id}/{channel_id}", source_id=source_id, channel_id=channel_id, number=number,
            name=item.get("tv_name") or item.get("name"), section=item.get("tv_section"), kind=self.kind,
            lane=self.host.LANE_ROKU, node=self.host.FEED_CONFIG.node, network=network, market=market,
            tune=tune, stream=self._stream(), listings=listings,
            detail={"source_name": item.get("name"), **({"date": listing.get("date")} if games else {}), **timing},
            row={key: item.get(key) for key in ("tv_number", "tv_name", "tv_section")}
            | {"kind": "roku", "source_id": source_id, "channel_id": channel_id, "name": item.get("name"),
               "date": listing.get("date"), **timing})

    def guide(self, window):
        """YouTube TV's rows, keyed ``youtube-tv/NAME``; then the day's games on the Live
        Sports numbers, keyed ``games/NUMBER``, read only when they can be asked for."""
        host = self.host
        rows = {}
        yttv_rows, stored = host.yttv_guide_channels()
        for key, channel in yttv_rows.items():
            if window.wants(key):
                rows[key] = {**channel, "events": window.current(channel["events"])}
        game_rows, sports = (host.sports_guide_channels(window.floor, window.ceiling, window.now)
                             if window.may_want("games/") else ({}, None))
        for key, channel in game_rows.items():
            if window.wants(key):
                rows[key] = {**channel, "events": window.current(channel["events"])}
        return rows, {"youtube_tv": host.yttv_guide_summary(stored, window.now), "sports": sports}

    def guide_now(self, now):
        yttv_rows, stored = self.host.yttv_guide_channels()
        return now_and_next(yttv_rows, now), {
            "youtube_tv_available": bool(stored and stored.get("channels_with_events"))}

    def tune(self, source_id, body, quality):
        return self.host.perform_roku_tune(dict(body, source=source_id))


class InternetChannels(Adapter):
    kind = "internet"

    def catalog(self):
        return {"lineup": {"internet": self.host.internet_views()}}

    def channels(self, day, found):
        host = self.host
        try:
            streams = host.web_streams.streams()
        except internet_streams.StreamConfigError:
            streams = {}
        found_channels = []
        for view in host.internet_views():
            channel_id, stream = view["channel_id"], streams.get(view["channel_id"])
            found_channels.append(Channel(
                id=f"{internet_streams.SOURCE}/{channel_id}", source_id=internet_streams.SOURCE,
                channel_id=channel_id, number=view.get("tv_number"), name=view.get("tv_name") or view["name"],
                section=view.get("tv_section"), kind=self.kind, node=host.FEED_CONFIG.node,
                stream=view["stream_path"],
                listings=f"{internet_streams.SOURCE}/{channel_id}" if stream and stream.guide else None,
                detail={"source_name": view["name"], "note": view.get("note")},
                row=dict(view, kind="internet", source_id=internet_streams.SOURCE)))
        return found_channels

    def guide(self, window):
        """The streams that publish listings, keyed ``internet/ID``; a stream whose listings
        say nothing in the window (or did not answer) has no row."""
        if not window.may_want(f"{internet_streams.SOURCE}/"):
            return {}, {}
        rows = {}
        for key, channel in self.host.internet_guide_channels().items():
            events = window.current(channel["events"])
            if window.wants(key) and events:
                rows[key] = {**channel, "events": events}
        return rows, {}

    def guide_now(self, now):
        entries = {}
        for key, channel in self.host.internet_guide_channels().items():
            on = next((event for event in channel["events"]
                       if event["start"] <= now < event["start"] + event["duration"]), None)
            later = next((event for event in channel["events"] if event["start"] > now), None)
            if on or later:
                entries[key] = {"now": on, "next": later}
        return entries, {}


class StationChannels(Adapter):
    """VizelVision itself, at each number it holds."""
    kind = "vizelvision"
    STREAM = "/stream/vizelvision/index.m3u8"

    def catalog(self):
        return {"lineup": {"house": self.host.channel_lineup.house()}}

    def channels(self, day, found):
        return [Channel(id=lineup.HOUSE, source_id=lineup.HOUSE, channel_id=None, number=view["tv_number"],
                        name=view["tv_name"], section=view["tv_section"], kind=self.kind, stream=self.STREAM,
                        row=dict(view, kind="vizelvision", name="VizelVision", stream_path=self.STREAM))
                for view in self.host.channel_lineup.house()]


class SlideChannels(Adapter):
    """The station's slide channels (lineup.SLIDES): pages in a browser, video on a Roku."""
    kind = "slides"

    def catalog(self):
        return {"lineup": {"slides": self.host.slide_views()}}

    def channels(self, day, found):
        host = self.host
        return [Channel(id=f"{lineup.HOUSE}/{view['channel_id']}", source_id=lineup.HOUSE,
                        channel_id=view["channel_id"], number=view["tv_number"], name=view["tv_name"],
                        section=view["tv_section"], kind=self.kind, node=host.FEED_CONFIG.node,
                        stream=view["stream_path"],
                        detail={"page_path": view["page_path"], "data_path": view["data_path"]},
                        row=dict(view, kind="slides", name=lineup.SLIDES[view["channel_id"]]))
                for view in host.slide_views()]


class Registry:
    """The adapters, in the order their sources are listed and their guide rows given."""

    def __init__(self, adapters):
        self.adapters = tuple(adapters)

    def channels(self, day=None, found=None):
        found = Found() if found is None else found
        return [channel for adapter in self.adapters for channel in adapter.channels(day, found)]

    def catalog(self):
        merged = {"sources": [], "listed": [], "lineup": {}}
        for adapter in self.adapters:
            part = adapter.catalog()
            merged["sources"] += part.get("sources", [])
            merged["listed"] += part.get("listed", [])
            merged["lineup"].update(part.get("lineup", {}))
        return merged

    def guide(self, window):
        rows, summary = {}, {}
        for adapter in self.adapters:
            part, fields = adapter.guide(window)
            rows.update(part)
            summary.update(fields)
        return rows, summary

    def guide_now(self, now):
        rows, summary = {}, {}
        for adapter in self.adapters:
            part, fields = adapter.guide_now(now)
            rows.update(part)
            summary.update(fields)
        return rows, summary

    def tuner(self, name):
        """``(adapter, source id)`` for a tune request's source, aliases resolved; ``(None,
        name)`` when nothing tunes it."""
        for adapter in self.adapters:
            source_id = adapter.aliases.get(name, name)
            if source_id in adapter.tunes:
                return adapter, source_id
        return None, name

    def tune_choices(self):
        """What a tune's source may be, in words: "digital, cable, or a Roku source (...)"."""
        labels = [adapter.tune_label for adapter in self.adapters if adapter.tune_label]
        return labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + ", or " + labels[-1]


def registry(host):
    """Every kind of source TV Thing has, in the order the API has always listed them."""
    return Registry(adapter(host) for adapter in (
        AntennaChannels, CableChannels, RokuChannels, InternetChannels, StationChannels, SlideChannels))

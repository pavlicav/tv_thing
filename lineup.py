"""TV Thing channel numbers: one lineup for the antenna, the Roku and the station.

``lineup.json`` is the lineup card. Like an old cable system's, it puts the
local stations low, the cable networks next, the game channels after them,
and the antenna's diginets and shopping channels up in the hundreds. Each
section is a range of numbers. A fixed channel names its number; the rest
are placed by rule:

* **Games** (a section with ``games``) are numbered each day from the
  section's first number up, in kickoff order, so a day's slate keeps its
  numbers from morning to night. A block can be split around a pinned
  channel: sections listing the same game sources fill one after another.
* **Anything new** -- a channel a rescan turns up, a YouTube TV row added to
  roku.json -- takes the first free number in the section whose ``new``
  names its source (``*`` takes any source no section names). The number is
  kept in ``lineup_state.json`` so it never moves; a fixed number in
  lineup.json takes precedence.
* Whatever does not fit goes to the ``overflow`` section.

A channel is keyed by its source and its id there: ``digital/22.4`` (the
antenna's virtual channel number, which survives a rescan; the id embeds the
PSIP name, which need not), ``youtube-tv/NBC``, ``internet/pentulive`` (internet_streams.py). The station is
``vizelvision`` and may hold several numbers. With a ``channel`` it is one of
the station's own slide channels instead (``SLIDES``), such as the Sports
Guide, which plays in the browser rather than relaying the station.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass

HOUSE = "vizelvision"
ANTENNA = "digital"
ANY_SOURCE = "*"
# The station's slide channels: channel id -> name. server.py knows the pages that play them.
SLIDES = {"sports-guide": "VizelVision Sports Guide"}


class LineupError(ValueError):
    """lineup.json is missing or does not describe a usable lineup."""


@dataclass(frozen=True)
class Section:
    first: int
    last: int
    name: str
    games: tuple = ()
    new: tuple = ()
    overflow: bool = False

    def holds(self, number):
        return self.first <= number <= self.last


@dataclass(frozen=True)
class Plan:
    sections: tuple
    fixed: dict         # key -> (number, name override or None)
    house: tuple        # (number, name) of each of the station's numbers
    slides: tuple = ()  # (number, name, channel id) of each of its slide channels

    def station_numbers(self):
        return {number for number, _ in self.house} | {number for number, _, _ in self.slides}

    def section_of(self, number):
        return next((s for s in self.sections if s.holds(number)), None)

    def games_sections(self, source_id):
        """The sections a game source fills, in order; () for none."""
        return tuple(s for s in self.sections if source_id in s.games)

    def new_section(self, source_id):
        named = next((s for s in self.sections if source_id in s.new), None)
        return named or next((s for s in self.sections if ANY_SOURCE in s.new), None)

    def overflow_section(self):
        return next((s for s in self.sections if s.overflow), None)


def _number(value, where):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LineupError(f"{where}: a channel number is a whole number from 0 up")
    return value


def parse_plan(document):
    """Validate a lineup.json document and return its Plan."""
    if not isinstance(document, dict):
        raise LineupError("lineup.json must be an object")
    sections = []
    for index, raw in enumerate(document.get("sections") or []):
        where = f"sections[{index}]"
        if not isinstance(raw, dict) or not str(raw.get("name") or "").strip():
            raise LineupError(f"{where}: a section needs a name")
        first = _number(raw.get("first"), where)
        last = _number(raw.get("last"), where)
        if last < first:
            raise LineupError(f"{where}: last comes before first")
        games, new = tuple(raw.get("games") or ()), tuple(raw.get("new") or ())
        overflow = raw.get("overflow") is True
        if sum(map(bool, (games, new, overflow))) > 1:
            raise LineupError(f"{where}: a section holds games, new channels or overflow, not several")
        sections.append(Section(first, last, raw["name"].strip(), games, new, overflow))
    sections.sort(key=lambda s: s.first)
    for before, after in zip(sections, sections[1:]):
        if after.first <= before.last:
            raise LineupError(f"sections {before.name!r} and {after.name!r} overlap")
    if sum(s.overflow for s in sections) != 1:
        raise LineupError("exactly one section must be the overflow")
    seen = [source for s in sections for source in s.new]
    if len(seen) != len(set(seen)):
        raise LineupError("a source is listed under new in more than one section")
    for section in sections:
        for other in sections:
            if set(section.games) & set(other.games) and set(section.games) != set(other.games):
                raise LineupError(f"sections {section.name!r} and {other.name!r} share some game "
                                  "sources but not all; a split block lists the same ones")

    plan = Plan(tuple(sections), {}, ())
    fixed, house, slides, numbers = {}, [], [], set()
    for index, raw in enumerate(document.get("channels") or []):
        where = f"channels[{index}]"
        if not isinstance(raw, dict) or not raw.get("source"):
            raise LineupError(f"{where}: a channel needs a source")
        number = _number(raw.get("number"), where)
        if number in numbers:
            raise LineupError(f"{where}: {number} is given out twice")
        numbers.add(number)
        section = plan.section_of(number)
        if section and (section.games or section.overflow):
            raise LineupError(f"{where}: {number} is in {section.name!r}, which numbers itself")
        name = raw.get("name") or None
        if raw["source"] == HOUSE and raw.get("channel"):
            if raw["channel"] not in SLIDES:
                raise LineupError(f"{where}: the station has no {raw['channel']!r} channel; "
                                  f"it has {', '.join(sorted(SLIDES))}")
            slides.append((number, name or SLIDES[raw["channel"]], raw["channel"]))
            continue
        if raw["source"] == HOUSE:
            house.append((number, name or "VizelVision"))
            continue
        if not raw.get("channel"):
            raise LineupError(f"{where}: a channel needs its channel id")
        key = f"{raw['source']}/{raw['channel']}"
        if key in fixed:
            raise LineupError(f"{where}: {key} already has {fixed[key][0]}")
        fixed[key] = (number, name)
    return Plan(tuple(sections), fixed, tuple(sorted(house)), tuple(sorted(slides)))


def antenna_keys(channels):
    """Each antenna channel's key: its virtual channel number.

    Should two channels share a number (a distant station on another RF),
    the first keeps the plain number and the others add their RF channel.
    """
    keys, seen = [], set()
    for channel in channels:
        number = channel.get("number")
        key = str(number) if number else f"id:{channel.get('id')}"
        if key in seen:
            key = f"{key}@rf{channel.get('rf_channel')}"
        seen.add(key)
        keys.append(key)
    return keys


def _game_order(item):
    try:
        identity = (0, int(item.get("id")), "")
    except (TypeError, ValueError):
        identity = (1, 0, str(item.get("id")))
    return (str(item.get("start_time") or "~"), identity)


class Lineup:
    """lineup.json plus the numbers given out since, safe across threads."""

    def __init__(self, plan_path, state_path, clock=time.time):
        self.plan_path = plan_path
        self.state_path = state_path
        self.clock = clock
        self._lock = threading.Lock()
        self._plan_cache = (None, None)      # (stat signature, Plan or LineupError)
        self._state = None

    # -- files ----------------------------------------------------------------
    def plan(self):
        """The current Plan; LineupError when lineup.json is unusable."""
        try:
            stat = os.stat(self.plan_path)
            signature = (stat.st_mtime_ns, stat.st_size)
        except OSError as error:
            raise LineupError(f"cannot read {self.plan_path}: {error.strerror}") from None
        cached_signature, cached = self._plan_cache
        if cached_signature != signature:
            try:
                with open(self.plan_path) as f:
                    cached = parse_plan(json.load(f))
            except (OSError, ValueError) as error:
                cached = error if isinstance(error, LineupError) else LineupError(
                    f"{os.path.basename(self.plan_path)}: {error}")
            self._plan_cache = (signature, cached)
        if isinstance(cached, LineupError):
            raise cached
        return cached

    def _assigned(self):
        if self._state is None:
            try:
                with open(self.state_path) as f:
                    state = json.load(f)
                assigned = state.get("assigned") if isinstance(state, dict) else None
                self._state = assigned if isinstance(assigned, dict) else {}
            except (OSError, ValueError):
                self._state = {}
        return self._state

    def _save(self):
        # A failed write keeps the numbers for this run rather than failing
        # the channel list that asked for them; the next change retries.
        temp = f"{self.state_path}.tmp"
        try:
            with open(temp, "w") as f:
                json.dump({"_comment": "Numbers TV Thing gave channels lineup.json does not list. "
                           "Give one a number in lineup.json to move it.",
                           "assigned": self._state}, f, indent=2, sort_keys=True)
            os.replace(temp, self.state_path)
        except OSError as error:
            print(f"[Lineup] could not save {self.state_path}: {error}", flush=True)

    # -- numbering --------------------------------------------------------------
    def _view(self, plan, number, name):
        section = plan.section_of(number)
        return {"tv_number": number, "tv_name": name, "tv_section": section.name if section else None}

    @staticmethod
    def _free(section, taken):
        for number in range(section.first, section.last + 1):
            if number not in taken:
                return number
        return None

    def _overflow(self, plan, taken):
        section = plan.overflow_section()
        number = self._free(section, taken)
        if number is None:          # Seven hundred channels in: keep counting.
            number = max(taken | {section.last}) + 1
        return number

    def number(self, source_id, entries):
        """Numbers for one source's channels: ``entries`` is ``[(id, name)]``.

        Returns one view per entry, in order; ``None`` for all of them when
        lineup.json is unusable. A channel lineup.json does not list is given
        a number here, and keeps it.
        """
        try:
            plan = self.plan()
        except LineupError:
            return [None] * len(entries)
        with self._lock:
            assigned = self._assigned()
            changed = False
            fixed_numbers = {number for number, _ in plan.fixed.values()} | plan.station_numbers()
            for key in [key for key in assigned if key in plan.fixed]:
                del assigned[key]     # Now listed: lineup.json has the say.
                changed = True
            taken = fixed_numbers | {entry["number"] for entry in assigned.values()}
            views = []
            for channel_id, name in entries:
                key = f"{source_id}/{channel_id}"
                if key in plan.fixed:
                    number, override = plan.fixed[key]
                    views.append(self._view(plan, number, override or name))
                    continue
                entry = assigned.get(key)
                if entry and entry["number"] in fixed_numbers:
                    del assigned[key]  # lineup.json gave its number to another.
                    entry = None
                if entry is None:
                    section = plan.new_section(source_id)
                    number = self._free(section, taken) if section else None
                    if number is None:
                        number = self._overflow(plan, taken)
                    entry = {"number": number, "name": name, "first_seen": int(self.clock())}
                    assigned[key] = entry
                    taken.add(number)
                    changed = True
                views.append(self._view(plan, entry["number"], name))
            if changed:
                self._save()
            return views

    def number_games(self, pools):
        """Numbers for one day's games: ``pools`` is ``{source_id: items}``.

        Returns ``{(source_id, item id): view}``. Each games section numbers
        the games of its sources together, by start time.
        """
        try:
            plan = self.plan()
        except LineupError:
            return {}
        with self._lock:
            taken = ({number for number, _ in plan.fixed.values()} | plan.station_numbers()
                     | {entry["number"] for entry in self._assigned().values()})
        by_block = {}
        for source_id, items in pools.items():
            block = plan.games_sections(source_id)
            for item in items:
                by_block.setdefault(block, []).append((source_id, item))
        views = {}
        # Blocks first, so a block's own numbers are never taken by overflow.
        for block in sorted(by_block, key=lambda b: (not b, b[0].first if b else 0)):
            games = sorted(by_block[block], key=lambda pair: _game_order(pair[1]))
            for source_id, item in games:
                number = next((n for n in (self._free(s, taken) for s in block) if n is not None), None)
                if number is None:
                    number = self._overflow(plan, taken)
                taken.add(number)
                views[(source_id, str(item.get("id")))] = self._view(plan, number, item.get("name"))
        return views

    def game_partners(self, source_id):
        """The game sources numbered together with ``source_id``, itself included."""
        try:
            block = self.plan().games_sections(source_id)
        except LineupError:
            block = ()
        return block[0].games if block else (source_id,)

    def house(self):
        """The station's numbers, as views."""
        try:
            plan = self.plan()
        except LineupError:
            return []
        return [self._view(plan, number, name) for number, name in plan.house]

    def slides(self):
        """The station's slide channels, as views with their ``channel_id``."""
        try:
            plan = self.plan()
        except LineupError:
            return []
        return [dict(self._view(plan, number, name), channel_id=channel)
                for number, name, channel in plan.slides]

    def sections(self):
        """The lineup card's sections, for headings; [] when unusable."""
        try:
            plan = self.plan()
        except LineupError:
            return []
        return [{"first": s.first, "last": s.last, "name": s.name,
                 **({"games": list(s.games)} if s.games else {}),
                 **({"new": list(s.new)} if s.new else {})}
                for s in plan.sections]

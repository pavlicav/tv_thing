"""Unit tests for the PSIP guide parsers.

Everything here is bit-level table parsing, which fails silently and
plausibly -- an off-by-one in a field offset gives you a schedule, just the
wrong one. Anything needing a tuner lives in the integration suite.
"""

import struct
import time

import epg


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def _mss(text, mode=0x00, compression=0x00):
    """Build a multiple string structure holding one segment."""
    if mode == 0x3F:
        body = text.encode("utf-16-be")
    else:
        body = bytes((ord(char) & 0xFF) for char in text)
    segment = bytes([compression, mode, len(body)]) + body
    return bytes([1]) + b"eng" + bytes([1]) + segment


def _header(table_id, extension, version=0, section=0, last=0):
    """The nine bytes every long-form PSIP section starts with."""
    return (bytes([table_id])
            + struct.pack(">H", 0xF000)          # length, unused by parsers
            + struct.pack(">H", extension)
            + bytes([0xC0 | (version << 1) | 1])
            + bytes([section, last, 0]))


def _eit_section(source_id, events, version=0, section=0, last=0):
    body = b""
    for event_id, start_gps, duration, title, descriptors in events:
        title_raw = _mss(title)
        body += struct.pack(">H", 0xC000 | event_id)
        body += struct.pack(">I", start_gps)
        body += bytes([(duration >> 16) & 0x0F,
                      (duration >> 8) & 0xFF, duration & 0xFF])
        body += bytes([len(title_raw)]) + title_raw
        body += struct.pack(">H", 0xF000 | len(descriptors)) + descriptors
    return (_header(epg.EIT_TABLE_ID, source_id, version, section, last)
            + bytes([len(events)]) + body + bytes(4))


def _mgt_section(tables):
    """tables: list of (table_type, pid)."""
    body = struct.pack(">H", len(tables))
    for table_type, pid in tables:
        body += struct.pack(">H", table_type)
        body += struct.pack(">H", 0xE000 | pid)
        body += bytes([0xE0])                    # version
        body += struct.pack(">I", 0)             # number_bytes
        body += struct.pack(">H", 0xF000)        # descriptors_length 0
    return _header(epg.MGT_TABLE_ID, 0) + body + bytes(4)


def _stt_section(system_time, offset):
    return (_header(epg.STT_TABLE_ID, 0)
            + struct.pack(">I", system_time)
            + bytes([offset])
            + struct.pack(">H", 0)
            + bytes(4))


def _ett_section(source_id, event_id, text, channel_level=False):
    etm_id = (source_id << 16) | (event_id << 2) | (0 if channel_level else 2)
    return (_header(epg.ETT_TABLE_ID, source_id)
            + struct.pack(">I", etm_id)
            + _mss(text) + bytes(4))


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------

def test_multiple_string_decodes_the_common_latin_case():
    text, compressed = epg.decode_multiple_string(_mss("NBC Nightly News"))
    assert text == "NBC Nightly News"
    assert compressed == 0


def test_multiple_string_decodes_utf16_segments():
    text, _ = epg.decode_multiple_string(_mss("Noticias Univisión", mode=0x3F))
    assert text == "Noticias Univisión"


def test_mode_byte_is_the_high_byte_of_the_code_point():
    """Mode 0x01 means every byte is a code point in the 0x0100 page."""
    text, _ = epg.decode_multiple_string(_mss("Łł", mode=0x01))
    assert text == "Łł"


def test_huffman_segments_are_counted_not_silently_dropped():
    text, compressed = epg.decode_multiple_string(_mss("xx", compression=0x01))
    assert text == ""
    assert compressed == 1


def test_empty_string_structure_is_harmless():
    assert epg.decode_multiple_string(b"") == ("", 0)
    assert epg.decode_multiple_string(bytes([1])) == ("", 0)


# ---------------------------------------------------------------------------
# EIT
# ---------------------------------------------------------------------------

def test_eit_parsing_reads_events():
    section = _eit_section(1001, [
        (7, 1_000_000, 1800, "Jeopardy!", b""),
        (8, 1_001_800, 3600, "Nightly News", b""),
    ])
    parsed = epg.parse_eit_section(section)
    assert parsed["source_id"] == 1001
    assert [e["title"] for e in parsed["events"]] == ["Jeopardy!",
                                                      "Nightly News"]
    assert [e["event_id"] for e in parsed["events"]] == [7, 8]
    assert [e["duration"] for e in parsed["events"]] == [1800, 3600]
    assert parsed["events"][0]["start_gps"] == 1_000_000


def test_eit_parsing_walks_variable_length_titles_and_descriptors():
    """Every field after the first event is at an offset the parser computes."""
    section = _eit_section(2, [
        (1, 100, 60, "A", b"\x86\x02\x01\x02"),
        (2, 160, 60, "A much longer programme title", b""),
        (3, 220, 60, "C", b"\x81\x01\x00"),
    ])
    parsed = epg.parse_eit_section(section)
    assert len(parsed["events"]) == 3
    assert [e["start_gps"] for e in parsed["events"]] == [100, 160, 220]
    assert [e["title"] for e in parsed["events"]][2] == "C"


def test_eit_parsing_flags_captioned_events():
    section = _eit_section(2, [
        (1, 100, 60, "Captioned", b"\x86\x02\x01\x02"),
        (2, 160, 60, "Not captioned", b"\x81\x01\x00"),
    ])
    events = epg.parse_eit_section(section)["events"]
    assert [e["captions"] for e in events] == [True, False]


def test_eit_duration_uses_only_its_twenty_bits():
    """The top four bits of that field are ETM_location and reserved."""
    section = _eit_section(1, [(1, 100, 0x0FFFFF, "Long", b"")])
    assert epg.parse_eit_section(section)["events"][0]["duration"] == 0x0FFFFF


def test_eit_parsing_rejects_foreign_tables():
    assert epg.parse_eit_section(b"") is None
    assert epg.parse_eit_section(bytes(40)) is None
    assert epg.parse_eit_section(_mgt_section([])) is None


def test_truncated_eit_stops_instead_of_raising():
    section = _eit_section(1, [(1, 100, 60, "Fine", b"")])[:-8]
    parsed = epg.parse_eit_section(section)
    assert parsed is not None


# ---------------------------------------------------------------------------
# MGT, STT, ETT
# ---------------------------------------------------------------------------

def test_mgt_maps_eit_and_ett_blocks_to_pids():
    section = _mgt_section([
        (0x0000, 0x1FFB),                      # TVCT, ignored
        (epg.MGT_EIT_BASE + 0, 0x1000),
        (epg.MGT_EIT_BASE + 1, 0x1001),
        (epg.MGT_EIT_BASE + 3, 0x1003),
        (epg.MGT_ETT_BASE + 0, 0x1100),
        (0x0301, 0x1FFB),                      # RRT, ignored
    ])
    tables = epg.parse_mgt_section(section)
    assert tables["eit"] == {0: 0x1000, 1: 0x1001, 3: 0x1003}
    assert tables["ett"] == {0: 0x1100}


def test_mgt_of_a_foreign_table_is_empty_not_an_error():
    assert epg.parse_mgt_section(b"") == {"eit": {}, "ett": {}}
    assert epg.parse_mgt_section(bytes(40)) == {"eit": {}, "ett": {}}


def test_stt_reads_the_leap_second_offset():
    parsed = epg.parse_stt_section(_stt_section(1_400_000_000, 18))
    assert parsed["gps_utc_offset"] == 18
    assert parsed["system_time"] == 1_400_000_000


def test_gps_timestamps_convert_to_unix_time():
    # 2026-01-01 00:00:00 UTC, as GPS seconds with the 18 s offset applied.
    unix = epg.gps_to_unix(1_451_260_818, 18)
    assert time.strftime("%Y-%m-%d %H:%M", time.gmtime(unix)) \
        == "2026-01-01 00:00"
    # A wrong offset is an 18-second error, not an 18-hour one.
    assert epg.gps_to_unix(1_451_260_818, 0) - unix == 18


def test_ett_links_text_to_one_event():
    parsed = epg.parse_ett_section(_ett_section(1001, 7, "A quiz show."))
    assert parsed == {"source_id": 1001, "event_id": 7,
                      "text": "A quiz show.", "compressed": 0}


def test_channel_level_ett_is_not_a_programme_description():
    assert epg.parse_ett_section(
        _ett_section(1001, 0, "About WUSA", channel_level=True)
    ) is None


# ---------------------------------------------------------------------------
# Section assembly
# ---------------------------------------------------------------------------

def test_completeness_needs_every_section_of_a_table():
    two_of_three = {
        epg._section_key(s): s for s in [
            _eit_section(5, [], section=0, last=2),
            _eit_section(5, [], section=1, last=2),
        ]
    }
    assert epg._complete_extensions(two_of_three) == set()

    two_of_three[epg._section_key(_eit_section(5, [], section=2, last=2))] = \
        _eit_section(5, [], section=2, last=2)
    assert epg._complete_extensions(two_of_three) == {5}


def test_completeness_is_tracked_per_source():
    sections = {
        epg._section_key(s): s for s in [
            _eit_section(5, [], section=0, last=0),
            _eit_section(6, [], section=0, last=1),
        ]
    }
    # Source 5 is a single-section table and is done; source 6 is not.
    assert epg._complete_extensions(sections) == {5}


def test_a_new_version_does_not_complete_the_old_one():
    """A mid-refresh version bump must not look like a finished table."""
    sections = {
        epg._section_key(s): s for s in [
            _eit_section(5, [], version=1, section=0, last=1),
            _eit_section(5, [], version=2, section=1, last=1),
        ]
    }
    assert epg._complete_extensions(sections) == set()


# ---------------------------------------------------------------------------
# Guide shape
# ---------------------------------------------------------------------------

def _guide(events, now=1_800_000_000):
    return {
        "generated_at": now,
        "channels": {"9.1": {"number": "9.1", "name": "WUSA-HD",
                             "events": events}},
    }


def test_coverage_is_measured_from_the_last_event_end():
    now = 1_800_000_000
    guide = _guide([
        {"event_id": 1, "start": now, "duration": 3600, "title": "Now"},
        {"event_id": 2, "start": now + 3600, "duration": 7200, "title": "Next"},
    ], now)
    summary = epg.summarize(guide)
    assert summary["event_count"] == 2
    assert summary["channels_with_events"] == 1
    assert summary["coverage_hours"] == 3.0
    assert summary["horizon"] == now + 10800


def test_a_guide_with_no_events_reports_no_coverage():
    summary = epg.summarize(_guide([]))
    assert summary == {"event_count": 0, "channels_with_events": 0,
                       "horizon": None, "coverage_hours": 0}


def test_upcoming_picks_the_event_on_air():
    now = 1_800_000_000
    guide = _guide([
        {"event_id": 1, "start": now - 1800, "duration": 1800, "title": "Past"},
        {"event_id": 2, "start": now, "duration": 1800, "title": "Now"},
        {"event_id": 3, "start": now + 1800, "duration": 1800, "title": "Next"},
    ], now)
    assert epg.upcoming(guide, "9.1", at=now)["title"] == "Now"
    assert epg.upcoming(guide, "9.1", at=now + 1799)["title"] == "Now"
    assert epg.upcoming(guide, "9.1", at=now + 1800)["title"] == "Next"
    assert epg.upcoming(guide, "9.1", at=now + 99999) is None
    assert epg.upcoming(guide, "44.1", at=now) is None
    assert epg.upcoming(None, "9.1", at=now) is None


def test_frequencies_are_visited_once_each_in_order():
    channels = [
        {"number": "9.1", "frequency": 189_000_000, "rf_channel": 9},
        {"number": "9.2", "frequency": 189_000_000, "rf_channel": 9},
        {"number": "7.1", "frequency": 177_000_000, "rf_channel": 7},
    ]
    targets = epg.frequencies_from_channels(channels)
    assert targets == [
        {"frequency": 177_000_000, "rf_channel": 7},
        {"frequency": 189_000_000, "rf_channel": 9},
    ]


def test_refresh_without_channels_reports_why_rather_than_tuning():
    guide = epg.refresh(channels=[])
    assert "band scan" in guide["error"]
    assert guide["channels"] == {}


def test_until_stops_collection_once_the_caller_has_enough():
    """ETTs carry no completeness marker, so the caller decides when to stop."""
    seen = []

    def until(raw):
        seen.append(epg.parse_ett_section(raw)["event_id"])
        return len(seen) >= 2

    sections = [_ett_section(1, event_id, f"Text {event_id}")
                for event_id in (10, 11, 12)]
    assert until(sections[0]) is False
    assert until(sections[1]) is True
    assert seen == [10, 11]


def test_repeated_sections_are_not_counted_twice():
    """Every table cycles forever; a second copy must not look like progress."""
    section = _ett_section(1, 10, "Text")
    assert epg._section_key(section) == epg._section_key(section)
    store = {epg._section_key(section): section}
    store.setdefault(epg._section_key(section), section)
    assert len(store) == 1


def test_channels_join_on_service_id_not_on_a_stale_number(monkeypatch):
    """A channel list built before PSIP was read carries invented numbers.

    RF 7 in DC really carries WJLA on 7.1 and WHUT on 32.1, but a scan that
    never read the VCT numbers them 7.1 and 7.3 in PMT order. Matching on the
    number would pair WJLA's schedule with WHUT's tuning data.
    """
    channels = [
        {"number": "7.1", "name": "WHUT", "id": "whut_7_1",
         "frequency": 177_000_000, "service_id": 1, "rf_channel": 7},
        {"number": "7.3", "name": "WJLA", "id": "wjla_7_3",
         "frequency": 177_000_000, "service_id": 3, "rf_channel": 7},
    ]

    def fake_collect(fd, frequency, **kwargs):
        return {
            "frequency": frequency, "locked": True, "snr_db": 30.0,
            "eit_blocks": [0], "compressed": 0, "gps_utc_offset": 18,
            "services": [
                {"number": "7.1", "name": "WJLA", "source_id": 1,
                 "service_id": 3, "frequency": frequency,
                 "events": [{"event_id": 1, "start": 1_800_000_000,
                             "duration": 1800, "title": "7News"}]},
                {"number": "32.1", "name": "WHUT", "source_id": 5,
                 "service_id": 1, "frequency": frequency,
                 "events": [{"event_id": 2, "start": 1_800_000_000,
                             "duration": 1800, "title": "BBC News"}]},
            ],
        }

    monkeypatch.setattr(epg.atsc_scan, "open_frontend", lambda *a, **k: 99)
    monkeypatch.setattr(epg.os, "close", lambda fd: None)
    monkeypatch.setattr(epg, "collect_frequency", fake_collect)

    guide = epg.refresh(channels=channels)

    # The guide is keyed by the number the broadcast actually uses.
    assert set(guide["channels"]) == {"7.1", "32.1"}
    # 7.1 is WJLA, and Watch must tune WJLA's record, not the one the stale
    # list filed under 7.1.
    assert guide["channels"]["7.1"]["name"] == "WJLA"
    assert guide["channels"]["7.1"]["id"] == "wjla_7_3"
    assert guide["channels"]["32.1"]["name"] == "WHUT"
    assert guide["channels"]["32.1"]["id"] == "whut_7_1"


def test_one_unreadable_frequency_does_not_lose_the_others(monkeypatch):
    channels = [
        {"number": "7.1", "name": "WJLA", "id": "wjla_7_1",
         "frequency": 177_000_000, "service_id": 3, "rf_channel": 7},
        {"number": "9.1", "name": "WUSA", "id": "wusa_9_1",
         "frequency": 189_000_000, "service_id": 1, "rf_channel": 9},
    ]

    def flaky(fd, frequency, **kwargs):
        if frequency == 177_000_000:
            raise OSError(5, "Input/output error")
        return {
            "frequency": frequency, "locked": True, "snr_db": 30.0,
            "eit_blocks": [0], "compressed": 0, "gps_utc_offset": 18,
            "services": [{"number": "9.1", "name": "WUSA", "source_id": 1,
                          "service_id": 1, "frequency": frequency,
                          "events": [{"event_id": 1, "start": 1_800_000_000,
                                      "duration": 1800, "title": "News"}]}],
        }

    monkeypatch.setattr(epg.atsc_scan, "open_frontend", lambda *a, **k: 99)
    monkeypatch.setattr(epg.os, "close", lambda fd: None)
    monkeypatch.setattr(epg, "collect_frequency", flaky)

    guide = epg.refresh(channels=channels)

    assert set(guide["channels"]) == {"9.1"}
    assert guide["event_count"] == 1
    # The failure is reported rather than swallowed.
    failed = [f for f in guide["frequencies"] if f["error"]]
    assert len(failed) == 1
    assert "Input/output error" in failed[0]["error"]


def test_an_aborted_refresh_keeps_channels_it_did_not_reach():
    now = 1_800_000_000
    stored = {
        "channels": {
            "7.1": {"number": "7.1", "name": "WJLA", "events": [
                {"event_id": 1, "start": now - 7200, "duration": 1800,
                 "title": "Over"},
                {"event_id": 2, "start": now + 600, "duration": 1800,
                 "title": "Still ahead"},
            ]},
        },
    }
    fresh = {"generated_at": now, "channels": {
        "9.1": {"number": "9.1", "name": "WUSA", "events": [
            {"event_id": 3, "start": now, "duration": 1800, "title": "Fresh"},
        ]},
    }}

    merged = epg.merge_guides(stored, fresh, now=now)

    assert set(merged["channels"]) == {"7.1", "9.1"}
    # Only the part of the old guide that has not already aired is kept,
    # and it is flagged so the UI can say where it came from.
    assert [e["title"] for e in merged["channels"]["7.1"]["events"]] \
        == ["Still ahead"]
    assert merged["channels"]["7.1"]["stale"] is True
    assert "stale" not in merged["channels"]["9.1"]
    assert merged["event_count"] == 2


def test_merging_never_overwrites_a_channel_that_did_refresh():
    now = 1_800_000_000
    stored = {"channels": {"9.1": {"number": "9.1", "name": "WUSA", "events": [
        {"event_id": 1, "start": now, "duration": 1800, "title": "Yesterday"},
    ]}}}
    fresh = {"generated_at": now, "channels": {
        "9.1": {"number": "9.1", "name": "WUSA", "events": [
            {"event_id": 2, "start": now, "duration": 1800, "title": "Today"},
        ]},
    }}
    merged = epg.merge_guides(stored, fresh, now=now)
    assert [e["title"] for e in merged["channels"]["9.1"]["events"]] == ["Today"]


def test_blocks_past_the_mandatory_four_get_the_slow_budget(monkeypatch):
    """EIT-4 and up repeat once a minute; the fast budget only misses them."""
    budgets = {}

    def fake_collect(adapter, pid, table_id, timeout, expected=None,
                     until=None, stop=None):
        budgets[pid] = timeout
        return []

    monkeypatch.setattr(epg, "collect_sections", fake_collect)
    monkeypatch.setattr(epg.atsc_scan, "tune", lambda *a, **k: None)
    monkeypatch.setattr(epg.atsc_scan, "wait_for_lock", lambda *a, **k: {
        "locked": True, "snr_db": 30.0,
    })
    monkeypatch.setattr(epg.time, "sleep", lambda _s: None)
    monkeypatch.setattr(epg.atsc_scan, "read_sections", lambda *a, **k: {
        "mgt": _mgt_section([(epg.MGT_EIT_BASE + block, 0x1000 + block)
                             for block in range(6)]),
        # Truthy: collect_frequency only parses the VCT if a section arrived.
        "vct": b"\xc8stub", "stt": None,
    })
    # A VCT is needed for the sweep to reach the EIT stage.
    monkeypatch.setattr(epg.atsc_scan, "parse_tvct_section", lambda _raw: [
        {"number": "9.1", "name": "WUSA", "service_id": 1, "source_id": 1},
    ])

    epg.collect_frequency(99, 189_000_000, blocks=6,
                          block_timeout=4.0, slow_block_timeout=65.0)

    # Blocks 0-3 are the mandatory, fast-repeating ones.
    assert [budgets[0x1000 + b] for b in range(4)] == [4.0] * 4
    assert [budgets[0x1000 + b] for b in (4, 5)] == [65.0, 65.0]

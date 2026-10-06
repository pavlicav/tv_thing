"""Unit tests for the native ATSC scanner.

These cover the band plan and the PSI/PSIP parsers, which are the parts that
are wrong silently. Anything needing a tuner lives in the integration suite.
"""

import struct

import atsc_scan


def test_rf_channel_frequencies_match_the_us_band_plan():
    # Tuning uses the channel centre: lower edge + 3 MHz.
    assert atsc_scan.rf_channel_frequency_hz(2) == 57_000_000
    assert atsc_scan.rf_channel_frequency_hz(6) == 85_000_000
    assert atsc_scan.rf_channel_frequency_hz(7) == 177_000_000
    assert atsc_scan.rf_channel_frequency_hz(9) == 189_000_000
    assert atsc_scan.rf_channel_frequency_hz(14) == 473_000_000
    assert atsc_scan.rf_channel_frequency_hz(36) == 605_000_000
    # The VHF-Lo gap at 72-76 MHz sits between channels 4 and 5.
    assert atsc_scan.rf_channel_lower_edge_mhz(4) == 66
    assert atsc_scan.rf_channel_lower_edge_mhz(5) == 76


def test_out_of_range_rf_channels_are_rejected():
    for channel in (0, 1, 70, -3):
        try:
            atsc_scan.rf_channel_frequency_hz(channel)
        except ValueError:
            continue
        raise AssertionError(f"RF {channel} should be rejected")


def test_default_plan_stops_at_the_repack_boundary():
    """RF 37-69 became cellular in 2020; scanning it is pure waste."""
    plan = atsc_scan.rf_channels()
    assert plan[0]["rf_channel"] == 2
    assert plan[-1]["rf_channel"] == 36
    assert not any(entry["repacked"] for entry in plan)

    legacy = atsc_scan.rf_channels(include_repacked=True)
    assert legacy[-1]["rf_channel"] == 69
    assert sum(entry["repacked"] for entry in legacy) == 69 - 36
    # Roughly half a full-band sweep is spectrum nothing broadcasts on.
    assert len(plan) / len(legacy) < 0.6


def test_bands_are_labelled():
    bands = {e["rf_channel"]: e["band"] for e in atsc_scan.rf_channels()}
    assert bands[2] == "VHF-Lo"
    assert bands[6] == "VHF-Lo"
    assert bands[7] == "VHF-Hi"
    assert bands[13] == "VHF-Hi"
    assert bands[14] == "UHF"


def _tvct_section(entries):
    """Build a minimal Terrestrial VCT section for parsing.

    Entries are ``(name, major, minor, service_id)`` or the same with a
    trailing source_id.
    """
    body = b""
    for entry in entries:
        name, major, minor, service_id = entry[:4]
        source_id = entry[4] if len(entry) > 4 else 0
        body += name.encode("utf-16-be").ljust(14, b"\x00")
        body += struct.pack(">I", (major << 18) | (minor << 8) | 0x04)
        body += bytes(6)                       # through carrier/modulation
        body += struct.pack(">H", service_id)  # program_number at +24
        body += bytes(2)                       # ETM_location, service_type
        body += struct.pack(">H", source_id)   # source_id at +28
        body += struct.pack(">H", 0)           # descriptors_length at +30
    return bytes([atsc_scan.TVCT_TABLE_ID]) + bytes(8) \
        + bytes([len(entries)]) + body


def test_tvct_parsing_reads_virtual_channel_numbers():
    section = _tvct_section([("WUSA-HD", 9, 1, 1), ("Crime", 9, 2, 2)])
    channels = atsc_scan.parse_tvct_section(section)
    assert [c["number"] for c in channels] == ["9.1", "9.2"]
    assert [c["name"] for c in channels] == ["WUSA-HD", "Crime"]
    assert [c["service_id"] for c in channels] == [1, 2]


def test_tvct_parsing_reads_the_source_id_the_guide_needs():
    """EIT events are keyed by source_id, which is not the program number."""
    section = _tvct_section([("WUSA-HD", 9, 1, 1, 1001),
                             ("Crime", 9, 2, 2, 1002)])
    channels = atsc_scan.parse_tvct_section(section)
    assert [c["source_id"] for c in channels] == [1001, 1002]


def test_tvct_parsing_rejects_a_foreign_table():
    assert atsc_scan.parse_tvct_section(b"\x00" * 40) == []
    assert atsc_scan.parse_tvct_section(b"") == []


def _pat_section(programs):
    body = b"".join(
        struct.pack(">HH", number, 0xE000 | pid)
        for number, pid in programs.items()
    )
    length = 5 + len(body) + 4          # from byte 3, including CRC
    return (bytes([atsc_scan.PAT_TABLE_ID])
            + struct.pack(">H", 0xB000 | length)
            + bytes(5) + body + bytes(4))


def test_pat_parsing_maps_programs_to_pmt_pids():
    section = _pat_section({1: 0x30, 2: 0x40, 0: 0x10})
    programs = atsc_scan.parse_pat_section(section)
    # Program 0 is the network PID, not a service.
    assert programs == {1: 0x30, 2: 0x40}


def _pmt_section(streams, pcr_pid=0x31):
    body = b""
    for stream_type, pid in streams:
        body += bytes([stream_type]) + struct.pack(">H", 0xE000 | pid) \
            + struct.pack(">H", 0xF000)
    length = 9 + len(body) + 4
    return (bytes([atsc_scan.PMT_TABLE_ID])
            + struct.pack(">H", 0xB000 | length)
            + bytes(5)
            + struct.pack(">H", 0xE000 | pcr_pid)
            + struct.pack(">H", 0xF000)      # program_info_length 0
            + body + bytes(4))


def test_pmt_parsing_picks_the_video_and_audio_pids():
    section = _pmt_section([(0x02, 49), (0x81, 52)])
    parsed = atsc_scan.parse_pmt_section(section)
    assert parsed["video_pid"] == 49      # MPEG-2 video
    assert parsed["audio_pid"] == 52      # AC-3
    assert parsed["pcr_pid"] == 0x31

    # H.264 video and AAC audio are recognised too.
    parsed = atsc_scan.parse_pmt_section(_pmt_section([(0x1B, 17), (0x11, 18)]))
    assert (parsed["video_pid"], parsed["audio_pid"]) == (17, 18)


def test_pmt_parsing_ignores_unknown_stream_types():
    parsed = atsc_scan.parse_pmt_section(
        _pmt_section([(0x06, 90), (0x02, 49), (0x81, 52)])
    )
    assert parsed["video_pid"] == 49
    assert parsed["audio_pid"] == 52
    assert len(parsed["streams"]) == 3


def test_status_decoding_names_the_flags():
    flags = atsc_scan.decode_status(0x1F)
    assert flags == {"signal": True, "carrier": True, "viterbi": True,
                     "sync": True, "lock": True}
    assert atsc_scan.decode_status(0x07)["lock"] is False

"""Unit tests for the minimal direct Linux DVB source."""

import ctypes

import dvb_source
import tuner


def ts_packet(pid, payload=b"", payload_start=False, counter=0):
    second = ((pid >> 8) & 0x1F) | (0x40 if payload_start else 0)
    header = bytes([0x47, second, pid & 0xFF, 0x10 | counter])
    return (header + payload + bytes([0xFF]) * 188)[:188]


def pat_packet(service_id=4, pmt_pid=0x30):
    section = bytes([
        0x00, 0xB0, 0x0D,
        0x00, 0x01, 0xC1, 0x00, 0x00,
        service_id >> 8, service_id & 0xFF,
        0xE0 | (pmt_pid >> 8), pmt_pid & 0xFF,
        0x00, 0x00, 0x00, 0x00,
    ])
    return ts_packet(0, b"\x00" + section, payload_start=True)


def pmt_packet(pmt_pid=0x30, service_id=4, pcr_pid=65):
    ac3_descriptor = b"\x05\x04AC-3"
    streams = bytes([
        0x02, 0xE0, 65, 0xF0, 0x00,
        0x81, 0xE0, 68, 0xF0, len(ac3_descriptor),
    ]) + ac3_descriptor
    section_length = 9 + len(streams) + 4
    section = bytes([
        0x02, 0xB0 | (section_length >> 8), section_length & 0xFF,
        service_id >> 8, service_id & 0xFF,
        0xC1, 0x00, 0x00,
        0xE0 | (pcr_pid >> 8), pcr_pid & 0xFF,
        0xF0, 0x00,
    ]) + streams + bytes([0x00, 0x00, 0x00, 0x00])
    return ts_packet(pmt_pid, b"\x00" + section, payload_start=True)


def test_pat_and_pmt_parsing():
    assert dvb_source.pat_pmt_pid(pat_packet(), service_id=4) == 0x30
    assert dvb_source.pat_pmt_pid(pat_packet(), service_id=3) is None
    assert dvb_source.pmt_pcr_pid(pmt_packet()) == 65
    metadata = dvb_source.pmt_stream_metadata(pmt_packet(), 65, 68)
    assert metadata["video"] == (0x02, b"")
    assert metadata["audio"] == (0x81, b"\x05\x04AC-3")


def test_linux_dvb_ctypes_abi_sizes():
    assert ctypes.sizeof(tuner.DtvProperty) == 76
    assert ctypes.sizeof(tuner.DtvProperties) == 16
    assert ctypes.sizeof(tuner.DmxPesFilterParams) == 20
    assert tuner.DTV_TUNE == 1
    assert tuner.DTV_CLEAR == 2
    assert tuner.DMX_IMMEDIATE_START == 4


def test_stable_program_tables_have_valid_crc_and_pids():
    pat = dvb_source.stable_pat_packet()
    pmt = dvb_source.stable_pmt_packet()

    assert dvb_source.packet_pid(pat) == 0
    assert dvb_source.pat_pmt_pid(pat, service_id=1) == \
        dvb_source.OUTPUT_PMT_PID
    assert dvb_source.packet_pid(pmt) == dvb_source.OUTPUT_PMT_PID
    assert dvb_source.pmt_pcr_pid(pmt) == dvb_source.OUTPUT_VIDEO_PID
    assert b"\x05\x04AC-3" in pmt

    for packet, table_id in ((pat, 0), (pmt, 2)):
        section = dvb_source.psi_section(packet, table_id)
        assert dvb_source.mpeg_crc32(section) == 0


def test_remap_normalizes_payload_continuity_counters():
    packet = ts_packet(65, b"video", counter=11)
    first, counter = dvb_source.remap_packet(
        packet, dvb_source.OUTPUT_VIDEO_PID, 3,
    )
    second, counter = dvb_source.remap_packet(
        packet, dvb_source.OUTPUT_VIDEO_PID, counter,
    )

    assert dvb_source.packet_pid(first) == dvb_source.OUTPUT_VIDEO_PID
    assert first[3] & 0x0F == 3
    assert second[3] & 0x0F == 4
    assert counter == 5


def test_program_relay_filters_and_udp_aligns_packets():
    relay = dvb_source.ProgramRelay(
        service_id=4, video_pid=65, audio_pid=68,
    )
    packets = [
        ts_packet(500),
        pat_packet(),
        pmt_packet(),
        ts_packet(65, b"video-1"),
        ts_packet(68, b"audio-1"),
        ts_packet(65, b"video-2"),
        ts_packet(500),
        ts_packet(68, b"audio-2"),
        ts_packet(65, b"video-3"),
    ]
    stream = b"noise" + b"".join(packets)

    datagrams = relay.feed(stream[:411])
    datagrams += relay.feed(stream[411:])

    assert len(datagrams) == 1
    assert len(datagrams[0]) == 7 * 188
    assert [dvb_source.packet_pid(datagrams[0][i:i + 188])
            for i in range(0, len(datagrams[0]), 188)] == [
                0,
                dvb_source.OUTPUT_PMT_PID,
                dvb_source.OUTPUT_VIDEO_PID,
                dvb_source.OUTPUT_AUDIO_PID,
                dvb_source.OUTPUT_VIDEO_PID,
                dvb_source.OUTPUT_AUDIO_PID,
                dvb_source.OUTPUT_VIDEO_PID,
            ]


def test_program_relay_preserves_partial_packet_between_large_reads():
    relay = dvb_source.ProgramRelay(
        service_id=4, video_pid=65, audio_pid=68,
    )
    packets = [
        pat_packet(), pmt_packet(),
        ts_packet(65, b"v1"), ts_packet(68, b"a1"),
        ts_packet(65, b"v2"), ts_packet(68, b"a2"),
        ts_packet(65, b"v3"),
    ]
    stream = b"".join(packets)

    assert relay.feed(stream[:1000]) == []
    datagrams = relay.feed(stream[1000:])

    assert len(datagrams) == 1
    assert len(datagrams[0]) == dvb_source.UDP_DATAGRAM_BYTES
    assert relay._input == bytearray()

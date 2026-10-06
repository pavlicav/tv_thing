#!/usr/bin/env python3
"""Minimal Linux DVB to localhost UDP source for TV Thing.

This intentionally bypasses VLC. It configures one all-PID transport tap on
``demux0`` and reads aligned MPEG-TS packets from ``dvr0``. The important
detail for cx18 is using Linux's actual ``DMX_IMMEDIATE_START`` value (4);
the former diagnostic used 1, which requests CRC checking but never starts a
PES filter and made the DVR path incorrectly appear broken.
"""

import argparse
import ctypes
import fcntl
import os
import select
import signal
import socket
import sys
import time

from tuner import (
    DTV_CLEAR,
    DTV_DELIVERY_SYSTEM,
    DTV_FREQUENCY,
    DTV_MODULATION,
    DTV_TUNE,
    FE_HAS_LOCK,
    FE_READ_STATUS,
    SYS_ATSC,
    VSB_8,
    DmxPesFilterParams,
    set_dtv_props,
)


DMX_SET_PES_FILTER = 0x40146F2C
DMX_STOP = 0x6F2A
DMX_SET_BUFFER_SIZE = 0x6F2D
DMX_IN_FRONTEND = 0
DMX_OUT_TS_TAP = 2
DMX_PES_OTHER = 20
DMX_IMMEDIATE_START = 4

TS_PACKET_BYTES = 188
UDP_TS_PACKETS = 7
UDP_DATAGRAM_BYTES = TS_PACKET_BYTES * UDP_TS_PACKETS
ALL_PIDS = 0x2000
PAT_PID = 0
OUTPUT_PMT_PID = 0x1000
OUTPUT_VIDEO_PID = 0x0100
OUTPUT_AUDIO_PID = 0x0101


def packet_pid(packet):
    """Return the 13-bit MPEG-TS PID, or None for a malformed packet."""
    if len(packet) != TS_PACKET_BYTES or packet[0] != 0x47:
        return None
    return ((packet[1] & 0x1F) << 8) | packet[2]


def psi_section(packet, table_id):
    """Return a complete single-packet PSI section when one is present."""
    if packet_pid(packet) is None or not packet[1] & 0x40:
        return None

    adaptation_control = (packet[3] >> 4) & 0x03
    if adaptation_control not in (1, 3):
        return None
    offset = 4
    if adaptation_control == 3:
        offset += 1 + packet[4]
    if offset >= len(packet):
        return None

    pointer = packet[offset]
    offset += 1 + pointer
    if offset + 3 > len(packet) or packet[offset] != table_id:
        return None
    section_length = ((packet[offset + 1] & 0x0F) << 8) | packet[offset + 2]
    end = offset + 3 + section_length
    if end > len(packet):
        return None
    return packet[offset:end]


def pat_pmt_pid(packet, service_id):
    """Find ``service_id``'s PMT PID in a PAT transport packet."""
    section = psi_section(packet, table_id=0x00)
    if not section or len(section) < 12:
        return None
    for offset in range(8, len(section) - 4, 4):
        program = (section[offset] << 8) | section[offset + 1]
        pid = ((section[offset + 2] & 0x1F) << 8) | section[offset + 3]
        if program == service_id:
            return pid
    return None


def pmt_pcr_pid(packet):
    """Return the PCR PID from a PMT transport packet."""
    section = psi_section(packet, table_id=0x02)
    if not section or len(section) < 12:
        return None
    return ((section[8] & 0x1F) << 8) | section[9]


def mpeg_crc32(data):
    """Return the big-endian MPEG-2 PSI CRC-32 value."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF \
                if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc


def psi_packet(pid, section, counter=0):
    """Wrap a compact PSI section in one 188-byte transport packet."""
    if len(section) > 183:
        raise ValueError("PSI section does not fit in one transport packet")
    header = bytes([
        0x47,
        0x40 | ((pid >> 8) & 0x1F),
        pid & 0xFF,
        0x10 | (counter & 0x0F),
    ])
    payload = b"\x00" + section
    return header + payload + bytes([0xFF]) * (188 - len(header) - len(payload))


def stable_pat_packet(counter=0):
    """Build a one-program PAT pointing at the stable internal PMT PID."""
    body = bytes([
        0x00, 0x01,             # transport stream id
        0xC1, 0x00, 0x00,       # current version, section 0/0
        0x00, 0x01,             # stable program number
        0xE0 | (OUTPUT_PMT_PID >> 8), OUTPUT_PMT_PID & 0xFF,
    ])
    section_length = len(body) + 4
    section = bytes([
        0x00,
        0xB0 | (section_length >> 8),
        section_length & 0xFF,
    ]) + body
    return psi_packet(PAT_PID, section + mpeg_crc32(section).to_bytes(4, "big"),
                      counter)


def pmt_stream_metadata(packet, video_pid, audio_pid):
    """Return program and selected ES metadata from a compact input PMT."""
    section = psi_section(packet, table_id=0x02)
    if not section or len(section) < 16:
        return None

    program_info_length = ((section[10] & 0x0F) << 8) | section[11]
    cursor = 12
    program_descriptors = section[cursor:cursor + program_info_length]
    cursor += program_info_length
    end = len(section) - 4
    streams = {}
    while cursor + 5 <= end:
        stream_type = section[cursor]
        pid = ((section[cursor + 1] & 0x1F) << 8) | section[cursor + 2]
        info_length = ((section[cursor + 3] & 0x0F) << 8) | section[cursor + 4]
        descriptors = section[cursor + 5:cursor + 5 + info_length]
        if pid in (video_pid, audio_pid):
            streams[pid] = (stream_type, descriptors)
        cursor += 5 + info_length

    return {
        "program_descriptors": program_descriptors,
        "video": streams.get(video_pid),
        "audio": streams.get(audio_pid),
    }


def stable_pmt_packet(counter=0, metadata=None):
    """Build a stable-PID program map while preserving codec descriptors."""
    # ATSC carries AC-3 as private stream type 0x81. Generic MPEG-TS demuxers
    # need the registration descriptor to resolve it as audio instead of an
    # unknown private-data stream.
    ac3_registration = b"\x05\x04AC-3"
    metadata = metadata or {}
    video_type, video_descriptors = metadata.get("video") or (0x02, b"")
    audio_type, audio_descriptors = metadata.get("audio") or (
        0x81, ac3_registration,
    )
    program_descriptors = metadata.get("program_descriptors", b"")

    streams = bytes([
        video_type,
        0xE0 | (OUTPUT_VIDEO_PID >> 8), OUTPUT_VIDEO_PID & 0xFF,
        0xF0 | (len(video_descriptors) >> 8), len(video_descriptors) & 0xFF,
    ]) + video_descriptors + bytes([
        audio_type,
        0xE0 | (OUTPUT_AUDIO_PID >> 8), OUTPUT_AUDIO_PID & 0xFF,
        0xF0 | (len(audio_descriptors) >> 8), len(audio_descriptors) & 0xFF,
    ]) + audio_descriptors
    body = bytes([
        0x00, 0x01,             # stable program number
        0xC1, 0x00, 0x00,       # current version, section 0/0
        0xE0 | (OUTPUT_VIDEO_PID >> 8), OUTPUT_VIDEO_PID & 0xFF,
        0xF0 | (len(program_descriptors) >> 8),
        len(program_descriptors) & 0xFF,
    ]) + program_descriptors + streams
    section_length = len(body) + 4
    section = bytes([
        0x02,
        0xB0 | (section_length >> 8),
        section_length & 0xFF,
    ]) + body
    return psi_packet(
        OUTPUT_PMT_PID,
        section + mpeg_crc32(section).to_bytes(4, "big"),
        counter,
    )


def remap_packet(packet, pid, counter):
    """Rewrite a packet PID/continuity counter and return its next counter."""
    remapped = bytearray(packet)
    remapped[1] = (remapped[1] & 0xE0) | ((pid >> 8) & 0x1F)
    remapped[2] = pid & 0xFF
    remapped[3] = (remapped[3] & 0xF0) | (counter & 0x0F)
    adaptation_control = (remapped[3] >> 4) & 0x03
    next_counter = (counter + 1) & 0x0F \
        if adaptation_control in (1, 3) else counter
    return bytes(remapped), next_counter


class ProgramRelay:
    """Select one program's transport packets and emit UDP-sized chunks."""

    def __init__(self, service_id, video_pid, audio_pid):
        self.service_id = service_id
        self.video_pid = video_pid
        self.audio_pid = audio_pid
        self.pmt_pid = None
        self.pcr_pid = None
        self._pat_counter = 0
        self._pmt_counter = 0
        self._video_counter = 0
        self._audio_counter = 0
        self._input = bytearray()
        self._output = bytearray()

    def feed(self, data):
        """Consume arbitrary bytes and return aligned 1,316-byte datagrams."""
        # DVR reads are normally aligned and tens of kilobytes long. Process
        # them by index: deleting 188 bytes from the front of a bytearray for
        # every full-multiplex packet turns a 19.4 Mbps ATSC feed into an O(n²)
        # copy loop and eventually overruns the demux buffer.
        buffered = bytes(self._input) + data
        self._input.clear()
        datagrams = []
        offset = 0
        length = len(buffered)

        while offset + TS_PACKET_BYTES <= length:
            if buffered[offset] != 0x47:
                sync = buffered.find(
                    b"\x47", offset + 1,
                    min(length, offset + TS_PACKET_BYTES),
                )
                if sync < 0:
                    offset += TS_PACKET_BYTES
                else:
                    offset = sync
                continue

            packet = buffered[offset:offset + TS_PACKET_BYTES]
            offset += TS_PACKET_BYTES
            pid = packet_pid(packet)

            if pid == PAT_PID:
                discovered = pat_pmt_pid(packet, self.service_id)
                if discovered is not None:
                    self.pmt_pid = discovered
                packet = stable_pat_packet(self._pat_counter)
                self._pat_counter = (self._pat_counter + 1) & 0x0F
                wanted = True
            elif self.pmt_pid is not None and pid == self.pmt_pid:
                discovered = pmt_pcr_pid(packet)
                if discovered is not None:
                    self.pcr_pid = discovered
                metadata = pmt_stream_metadata(
                    packet, self.video_pid, self.audio_pid,
                )
                packet = stable_pmt_packet(self._pmt_counter, metadata)
                self._pmt_counter = (self._pmt_counter + 1) & 0x0F
                wanted = True
            elif pid == self.video_pid:
                packet, self._video_counter = remap_packet(
                    packet, OUTPUT_VIDEO_PID, self._video_counter,
                )
                wanted = True
            elif pid == self.audio_pid:
                packet, self._audio_counter = remap_packet(
                    packet, OUTPUT_AUDIO_PID, self._audio_counter,
                )
                wanted = True
            else:
                wanted = False

            if wanted:
                self._output.extend(packet)
                if len(self._output) == UDP_DATAGRAM_BYTES:
                    datagrams.append(bytes(self._output))
                    self._output.clear()

        self._input.extend(buffered[offset:])
        return datagrams


def open_frontend(adapter, frequency):
    """Open, tune, and return a frontend descriptor held for source life."""
    path = f"/dev/dvb/adapter{adapter}/frontend0"
    fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    try:
        set_dtv_props(fd, [(DTV_CLEAR, 0)])
        set_dtv_props(fd, [
            (DTV_DELIVERY_SYSTEM, SYS_ATSC),
            (DTV_FREQUENCY, frequency),
            (DTV_MODULATION, VSB_8),
            (DTV_TUNE, 0),
        ])
        return fd
    except Exception:
        os.close(fd)
        raise


def wait_for_lock(fd, timeout, stopped):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not stopped():
        status = ctypes.c_uint32(0)
        fcntl.ioctl(fd, FE_READ_STATUS, status)
        if status.value & FE_HAS_LOCK:
            return status.value
        time.sleep(0.05)
    raise TimeoutError(f"no tuner lock within {timeout:g}s")


def open_transport_tap(adapter):
    """Return ``(control_fd, reader_fd)`` for an all-PID transport tap."""
    demux_path = f"/dev/dvb/adapter{adapter}/demux0"
    dvr_path = f"/dev/dvb/adapter{adapter}/dvr0"
    control_fd = os.open(demux_path, os.O_RDWR | os.O_NONBLOCK)
    try:
        fcntl.ioctl(control_fd, DMX_SET_BUFFER_SIZE, 4 * 1024 * 1024)
        params = DmxPesFilterParams()
        params.pid = ALL_PIDS
        params.input = DMX_IN_FRONTEND
        params.output = DMX_OUT_TS_TAP
        params.pes_type = DMX_PES_OTHER
        params.flags = DMX_IMMEDIATE_START
        fcntl.ioctl(control_fd, DMX_SET_PES_FILTER, params)
        reader_fd = os.open(dvr_path, os.O_RDONLY | os.O_NONBLOCK)
        return control_fd, reader_fd
    except Exception:
        os.close(control_fd)
        raise


def run(args):
    stopping = False

    def request_stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    frontend_fd = None
    demux_fd = None
    transport_fd = None
    sock = None
    tap_sock = None
    try:
        frontend_fd = open_frontend(args.adapter, args.frequency)
        status = wait_for_lock(frontend_fd, args.lock_timeout,
                               lambda: stopping)
        print(f"DVB lock {args.frequency}Hz status=0x{status:02x}",
              flush=True)

        demux_fd, transport_fd = open_transport_tap(args.adapter)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # A recording taps the same datagrams on its own port. It gets a
        # separate socket so a tap problem can never fail the live send.
        taps = [(args.udp_host, port) for port in args.tap_port or ()]
        if taps:
            tap_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        relay = ProgramRelay(args.service_id, args.video_pid, args.audio_pid)
        first_packet_deadline = time.monotonic() + args.packet_timeout
        sent = 0

        while not stopping:
            readable, _, _ = select.select([transport_fd], [], [], 0.25)
            if not readable:
                if sent == 0 and time.monotonic() >= first_packet_deadline:
                    raise TimeoutError(
                        f"no transport packets within {args.packet_timeout:g}s"
                    )
                continue

            try:
                data = os.read(transport_fd, TS_PACKET_BYTES * 256)
            except BlockingIOError:
                continue
            if not data:
                continue
            for datagram in relay.feed(data):
                sock.sendto(datagram, (args.udp_host, args.udp_port))
                for tap in taps:
                    try:
                        tap_sock.sendto(datagram, tap)
                    except OSError:
                        pass
                sent += len(datagram)
                if sent == UDP_DATAGRAM_BYTES:
                    print("DVB transport started", flush=True)
        return 0
    finally:
        if transport_fd is not None:
            os.close(transport_fd)
        if demux_fd is not None:
            try:
                fcntl.ioctl(demux_fd, DMX_STOP)
            except OSError:
                pass
            os.close(demux_fd)
        if frontend_fd is not None:
            os.close(frontend_fd)
        if sock is not None:
            sock.close()
        if tap_sock is not None:
            tap_sock.close()


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--adapter", type=int, default=0)
    parser.add_argument("--frequency", type=int, required=True)
    parser.add_argument("--service-id", type=int, required=True)
    parser.add_argument("--video-pid", type=int, required=True)
    parser.add_argument("--audio-pid", type=int, required=True)
    parser.add_argument("--udp-host", default="127.0.0.1")
    parser.add_argument("--udp-port", type=int, required=True)
    parser.add_argument("--tap-port", type=int, action="append",
                        help="also send the stream here (repeatable)")
    parser.add_argument("--lock-timeout", type=float, default=3.0)
    parser.add_argument("--packet-timeout", type=float, default=5.0)
    return parser.parse_args(argv)


def main(argv=None):
    try:
        return run(parse_args(argv))
    except Exception as error:
        print(f"DVB source failed: {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

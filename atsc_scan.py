#!/usr/bin/env python3
"""Native ATSC channel scanning and signal measurement.

This replaces w_scan for TV Thing's rescan. w_scan is a general DVB-T/C/S
scanner for dozens of countries; TV Thing needs one modulation (8VSB) on one
band plan, and already talks to the DVB frontend directly. Doing it natively
buys three things w_scan cannot give:

* **Range control.** w_scan's US ATSC table still sweeps RF 2-69. The 2017
  incentive auction and the repack that finished in 2020 cleared everything
  above RF 36, so roughly half of a w_scan run is spent locking against
  spectrum that is now cellular. Scanning 2-36 by default halves the time.
* **One channel at a time.** A single RF channel can be tuned, measured, and
  released, which is what DX work needs.
* **Signal quality.** Lock state, signal strength, SNR, BER, and uncorrected
  block counts, sampled while tuned.

The frontend is opened read/write only to tune. Reading statistics uses a
read-only handle, which the driver permits while another process is
streaming, so a live signal meter never interrupts playback.
"""

import ctypes
import errno
import fcntl
import os
import select
import struct
import time

from tuner import (
    DTV_CLEAR,
    DTV_DELIVERY_SYSTEM,
    DTV_FREQUENCY,
    DTV_MODULATION,
    DTV_TUNE,
    FE_HAS_CARRIER,
    FE_HAS_LOCK,
    FE_HAS_SIGNAL,
    FE_HAS_SYNC,
    FE_HAS_VITERBI,
    FE_READ_STATUS,
    SYS_ATSC,
    VSB_8,
    set_dtv_props,
)

# Legacy DVB statistics ioctls. The S5H1409 implements all four.
FE_READ_BER = 0x80046F46
FE_READ_SIGNAL_STRENGTH = 0x80026F47
FE_READ_SNR = 0x80026F48
FE_READ_UNCORRECTED_BLOCKS = 0x80046F49

# ATSC PSIP, shared with fetch_vct.py.
DMX_SET_FILTER = 0x403C6F2B
DMX_STOP = 0x00006F2A
# _IO('o', 45): the size is passed by value, not through a pointer, and only
# takes effect if it is set before the filter starts.
DMX_SET_BUFFER_SIZE = 0x00006F2D
PSIP_PID = 0x1FFB
TVCT_TABLE_ID = 0xC8
PAT_PID = 0x0000
PAT_TABLE_ID = 0x00
PMT_TABLE_ID = 0x02

# ISO/IEC 13818-1 stream_type values that appear in US ATSC multiplexes.
VIDEO_STREAM_TYPES = {0x02, 0x1B, 0x24}      # MPEG-2, H.264, HEVC
AUDIO_STREAM_TYPES = {0x81, 0x03, 0x04, 0x11}  # AC-3, MPEG-1/2 audio, AAC
DMX_CHECK_CRC = 1
DMX_IMMEDIATE_START = 4

# US broadcast RF channels still carrying ATSC after the 2020 repack.
FIRST_RF_CHANNEL = 2
LAST_RF_CHANNEL_IN_USE = 36
# Channels 37-69 were reallocated; kept selectable for DX and for receiving
# anything still radiating there.
LAST_RF_CHANNEL_LEGACY = 69

STATUS_FLAGS = (
    (FE_HAS_SIGNAL, "signal"),
    (FE_HAS_CARRIER, "carrier"),
    (FE_HAS_VITERBI, "viterbi"),
    (FE_HAS_SYNC, "sync"),
    (FE_HAS_LOCK, "lock"),
)


def rf_channel_lower_edge_mhz(channel):
    """Lower band edge of one US broadcast RF channel, in MHz."""
    channel = int(channel)
    if 2 <= channel <= 4:
        return 54 + (channel - 2) * 6
    if 5 <= channel <= 6:
        return 76 + (channel - 5) * 6
    if 7 <= channel <= 13:
        return 174 + (channel - 7) * 6
    if 14 <= channel <= LAST_RF_CHANNEL_LEGACY:
        return 470 + (channel - 14) * 6
    raise ValueError(
        f"RF channel must be {FIRST_RF_CHANNEL}-{LAST_RF_CHANNEL_LEGACY}"
    )


def rf_channel_frequency_hz(channel):
    """Tuning frequency for one RF channel.

    ATSC carries the visual carrier 310 kHz above the lower edge, but tuners
    take the channel centre, which is the lower edge plus 3 MHz.
    """
    return int((rf_channel_lower_edge_mhz(channel) + 3) * 1_000_000)


def rf_channels(include_repacked=False):
    """The selectable RF channel plan."""
    last = LAST_RF_CHANNEL_LEGACY if include_repacked \
        else LAST_RF_CHANNEL_IN_USE
    plan = []
    for channel in range(FIRST_RF_CHANNEL, last + 1):
        lower = rf_channel_lower_edge_mhz(channel)
        plan.append({
            "rf_channel": channel,
            "frequency_hz": rf_channel_frequency_hz(channel),
            "frequency_mhz": rf_channel_frequency_hz(channel) / 1_000_000,
            "lower_edge_mhz": lower,
            "band": (
                "VHF-Lo" if channel <= 6
                else "VHF-Hi" if channel <= 13
                else "UHF"
            ),
            "repacked": channel > LAST_RF_CHANNEL_IN_USE,
        })
    return plan


def decode_status(value):
    """Split a raw fe_status bitmask into named flags."""
    return {name: bool(value & bit) for bit, name in STATUS_FLAGS}


def _read_scalar(fd, request, ctype):
    value = ctype()
    try:
        fcntl.ioctl(fd, request, value)
    except OSError:
        return None
    return value.value


def read_signal(fd):
    """Sample the frontend's current lock state and signal quality.

    Safe on a read-only handle, so it can run against a frontend another
    process is streaming from.
    """
    raw_status = _read_scalar(fd, FE_READ_STATUS, ctypes.c_uint32) or 0
    strength = _read_scalar(fd, FE_READ_SIGNAL_STRENGTH, ctypes.c_uint16)
    snr = _read_scalar(fd, FE_READ_SNR, ctypes.c_uint16)
    return {
        "status": raw_status,
        "flags": decode_status(raw_status),
        "locked": bool(raw_status & FE_HAS_LOCK),
        "signal_strength": strength,
        "signal_percent": (
            round(strength * 100 / 65535, 1) if strength is not None else None
        ),
        # The s5h1409 driver reports 8VSB SNR in tenths of a dB. Demodulation
        # needs roughly 15 dB, so the useful range is about 15-35 dB.
        "snr_raw": snr,
        "snr_db": round(snr / 10, 1) if snr is not None else None,
        "ber": _read_scalar(fd, FE_READ_BER, ctypes.c_uint32),
        "uncorrected_blocks": _read_scalar(
            fd, FE_READ_UNCORRECTED_BLOCKS, ctypes.c_uint32
        ),
    }


def open_frontend(adapter=0, writable=False):
    """Open one DVB frontend. Read-only handles may be shared."""
    path = f"/dev/dvb/adapter{adapter}/frontend0"
    return os.open(path, os.O_RDWR if writable else os.O_RDONLY)


def measure(adapter=0):
    """Read the current signal without tuning or disturbing a stream."""
    fd = open_frontend(adapter, writable=False)
    try:
        return read_signal(fd)
    finally:
        os.close(fd)


def tune(fd, frequency_hz):
    """Point an exclusively-held frontend at one 8VSB frequency."""
    set_dtv_props(fd, [(DTV_CLEAR, 0)])
    set_dtv_props(fd, [
        (DTV_DELIVERY_SYSTEM, SYS_ATSC),
        (DTV_FREQUENCY, int(frequency_hz)),
        (DTV_MODULATION, VSB_8),
        (DTV_TUNE, 0),
    ])


def wait_for_lock(fd, timeout=2.0, poll=0.1, stop=None):
    """Poll until the demodulator locks, the timeout expires, or stop is set.

    The default timeout is deliberately short: a present ATSC signal locks in
    well under a second, so a longer wait only slows down empty channels.

    A marginal signal makes the lock bit flap, so a single read is not
    trustworthy in either direction. The returned sample reports ``locked``
    if lock was seen at any point in the window, ``lock_stable`` for the
    final read, and ``lock_ratio`` for the fraction of samples that held it
    -- the last is what distinguishes a solid local from a fading DX catch.
    """
    deadline = time.monotonic() + timeout
    samples = [read_signal(fd)]
    while not samples[-1]["locked"] and time.monotonic() < deadline:
        if stop is not None and stop.is_set():
            break
        time.sleep(poll)
        samples.append(read_signal(fd))

    latest = dict(samples[-1])
    locks = [item["locked"] for item in samples]
    latest["locked"] = any(locks)
    latest["lock_stable"] = samples[-1]["locked"]
    latest["lock_ratio"] = round(sum(locks) / len(locks), 2)
    latest["samples"] = len(samples)
    return latest


def sample_signal(fd, duration=2.0, poll=0.2, stop=None):
    """Watch one tuned frequency for a while and summarise its behaviour.

    Averages hide exactly the thing DX work cares about, so the spread is
    reported alongside the mean.
    """
    deadline = time.monotonic() + duration
    samples = [read_signal(fd)]
    while time.monotonic() < deadline:
        if stop is not None and stop.is_set():
            break
        time.sleep(poll)
        samples.append(read_signal(fd))

    def spread(key):
        values = [item[key] for item in samples if item[key] is not None]
        if not values:
            return None
        return {
            "min": min(values),
            "max": max(values),
            "mean": round(sum(values) / len(values), 1),
        }

    locks = [item["locked"] for item in samples]
    latest = dict(samples[-1])
    latest.update({
        "locked": any(locks),
        "lock_stable": samples[-1]["locked"],
        "lock_ratio": round(sum(locks) / len(locks), 2),
        "samples": len(samples),
        "snr_db_range": spread("snr_db"),
        "signal_percent_range": spread("signal_percent"),
    })
    return latest


def read_section(adapter, pid, table_id, timeout=4.0):
    """Read one complete PSI/PSIP section, or None if none arrives in time.

    The kernel returns whole CRC-checked sections per read, so no
    reassembly is needed here.
    """
    demux_fd = os.open(f"/dev/dvb/adapter{adapter}/demux0", os.O_RDWR)
    try:
        params = bytearray(struct.pack(
            "<H48s2sII",
            pid,
            bytes([table_id]) + bytes(15)
            + bytes([0xFF]) + bytes(15) + bytes(16),
            b"\x00\x00",
            0,
            DMX_CHECK_CRC | DMX_IMMEDIATE_START,
        ))
        fcntl.ioctl(demux_fd, DMX_SET_FILTER, params)
        ready, _, _ = select.select([demux_fd], [], [], timeout)
        if not ready:
            return None
        return os.read(demux_fd, 4096)
    except OSError as exc:
        if exc.errno in (errno.EOVERFLOW, errno.ETIMEDOUT):
            return None
        raise
    finally:
        try:
            fcntl.ioctl(demux_fd, DMX_STOP)
        except OSError:
            pass
        os.close(demux_fd)


def open_section_filter(adapter, pid, table_id, buffer_bytes=None):
    """Open one demux handle with a section filter already running.

    ``buffer_bytes`` enlarges the kernel ring buffer. The default holds only
    a couple of maximum-size sections, which is plenty for reading one PMT
    but overflows while collecting a whole multi-section table off a busy
    PID -- the guide's EIT sweep is the case that needs it.
    """
    fd = os.open(f"/dev/dvb/adapter{adapter}/demux0", os.O_RDWR)
    try:
        if buffer_bytes:
            fcntl.ioctl(fd, DMX_SET_BUFFER_SIZE, int(buffer_bytes))
        params = bytearray(struct.pack(
            "<H48s2sII",
            pid,
            bytes([table_id]) + bytes(15)
            + bytes([0xFF]) + bytes(15) + bytes(16),
            b"\x00\x00",
            0,
            DMX_CHECK_CRC | DMX_IMMEDIATE_START,
        ))
        fcntl.ioctl(fd, DMX_SET_FILTER, params)
    except OSError:
        os.close(fd)
        raise
    return fd


def close_section_filter(fd):
    try:
        fcntl.ioctl(fd, DMX_STOP)
    except OSError:
        pass
    os.close(fd)


def read_sections(adapter, wanted, timeout=3.0, max_filters=16):
    """Read several sections at once, keyed by caller-supplied labels.

    ``wanted`` is a sequence of ``(key, pid, table_id)``. The demux supports
    many simultaneous section filters, and each table repeats on its own
    cycle, so waiting for them serially costs the sum of the cycles instead
    of the longest one -- roughly 1.2 s per PMT on this hardware, which
    dominated the scan on multiplexes carrying eight services.
    """
    results = {}
    pending = list(wanted)
    while pending:
        batch, pending = pending[:max_filters], pending[max_filters:]
        by_fd = {}
        try:
            for key, pid, table_id in batch:
                try:
                    by_fd[open_section_filter(adapter, pid, table_id)] = key
                except OSError:
                    results[key] = None
            deadline = time.monotonic() + timeout
            waiting = set(by_fd)
            while waiting:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                ready, _, _ = select.select(list(waiting), [], [], remaining)
                if not ready:
                    break
                for fd in ready:
                    waiting.discard(fd)
                    try:
                        results[by_fd[fd]] = os.read(fd, 4096)
                    except OSError:
                        results[by_fd[fd]] = None
            for fd in waiting:
                results.setdefault(by_fd[fd], None)
        finally:
            for fd in by_fd:
                close_section_filter(fd)
    return results


def read_virtual_channels(adapter=0, timeout=4.0):
    """Read the PSIP Terrestrial VCT for the currently tuned frequency."""
    raw = read_section(adapter, PSIP_PID, TVCT_TABLE_ID, timeout)
    return parse_tvct_section(raw) if raw else []


def parse_pat_section(data):
    """Return {program_number: pmt_pid} from a PAT section."""
    if not data or len(data) < 12 or data[0] != PAT_TABLE_ID:
        return {}
    section_length = struct.unpack_from(">H", data, 1)[0] & 0x0FFF
    # section_length counts from byte 3; the last four bytes are the CRC.
    end = min(len(data), 3 + section_length - 4)
    programs = {}
    for pos in range(8, end, 4):
        if pos + 4 > len(data):
            break
        program_number = struct.unpack_from(">H", data, pos)[0]
        pid = struct.unpack_from(">H", data, pos + 2)[0] & 0x1FFF
        if program_number != 0:      # 0 is the network PID, not a program
            programs[program_number] = pid
    return programs


def parse_pmt_section(data):
    """Return the elementary stream PIDs from one PMT section."""
    if not data or len(data) < 12 or data[0] != PMT_TABLE_ID:
        return {}
    section_length = struct.unpack_from(">H", data, 1)[0] & 0x0FFF
    end = min(len(data), 3 + section_length - 4)
    program_info_length = struct.unpack_from(">H", data, 10)[0] & 0x0FFF
    pos = 12 + program_info_length

    video_pid = audio_pid = None
    streams = []
    while pos + 5 <= end:
        stream_type = data[pos]
        pid = struct.unpack_from(">H", data, pos + 1)[0] & 0x1FFF
        info_length = struct.unpack_from(">H", data, pos + 3)[0] & 0x0FFF
        streams.append({"stream_type": stream_type, "pid": pid})
        if video_pid is None and stream_type in VIDEO_STREAM_TYPES:
            video_pid = pid
        if audio_pid is None and stream_type in AUDIO_STREAM_TYPES:
            audio_pid = pid
        pos += 5 + info_length

    return {
        "video_pid": video_pid,
        "audio_pid": audio_pid,
        "pcr_pid": struct.unpack_from(">H", data, 8)[0] & 0x1FFF,
        "streams": streams,
    }


def read_program_pids(adapter=0, service_ids=(), timeout=3.0):
    """Map each service on the tuned frequency to its video/audio PIDs.

    Every PMT is filtered concurrently, so a multiplex with eight services
    costs one PMT cycle rather than eight.
    """
    pat = parse_pat_section(
        read_section(adapter, PAT_PID, PAT_TABLE_ID, timeout)
    )
    if not pat:
        return {}
    wanted = [sid for sid in service_ids if sid in pat] or list(pat)
    sections = read_sections(
        adapter,
        [(sid, pat[sid], PMT_TABLE_ID) for sid in wanted],
        timeout=timeout,
    )
    return {
        service_id: parse_pmt_section(raw)
        for service_id, raw in sections.items() if raw
    }


def parse_tvct_section(data):
    """Parse one ATSC Terrestrial VCT section (table_id 0xC8)."""
    if len(data) < 10 or data[0] != TVCT_TABLE_ID:
        return []

    channels = []
    pos = 10
    for _ in range(data[9]):
        if pos + 32 > len(data):
            break
        try:
            name = data[pos:pos + 14].decode("utf-16-be").rstrip("\x00").strip()
        except (UnicodeDecodeError, ValueError):
            name = ""
        word = struct.unpack_from(">I", data, pos + 14)[0]
        major = (word >> 18) & 0x3FF
        minor = (word >> 8) & 0x3FF
        service_id = struct.unpack_from(">H", data, pos + 24)[0]
        # The EIT is keyed by source_id, not by program number, so the guide
        # cannot match events to channels without carrying it out of here.
        source_id = struct.unpack_from(">H", data, pos + 28)[0]
        desc_len = struct.unpack_from(">H", data, pos + 30)[0] & 0x03FF
        if major > 0 and minor > 0:
            channels.append({
                "name": name,
                "major": major,
                "minor": minor,
                "service_id": service_id,
                "source_id": source_id,
                "number": f"{major}.{minor}",
            })
        pos += 32 + desc_len
    return channels


def scan_channel(channel, adapter=0, lock_timeout=2.0, vct_timeout=4.0,
                 read_vct=True, settle=0.15, fd=None, dwell=0.0):
    """Tune one RF channel and report what is on it.

    Pass ``fd`` to reuse an already-open exclusive frontend across a sweep;
    otherwise one is opened and closed for this channel alone.
    """
    frequency = rf_channel_frequency_hz(channel)
    owned = fd is None
    if owned:
        fd = open_frontend(adapter, writable=True)
    try:
        tune(fd, frequency)
        # The demodulator needs a moment after DTV_TUNE before its status
        # register means anything.
        time.sleep(settle)
        sample = wait_for_lock(fd, timeout=lock_timeout)
        # DX work wants to watch a marginal signal rather than take one
        # reading, so an optional dwell reports how it behaves over time.
        if dwell > 0:
            sample = sample_signal(fd, duration=dwell)
        result = {
            "rf_channel": int(channel),
            "frequency_hz": frequency,
            "frequency_mhz": frequency / 1_000_000,
            "lower_edge_mhz": rf_channel_lower_edge_mhz(channel),
            "repacked": int(channel) > LAST_RF_CHANNEL_IN_USE,
            "virtual_channels": [],
            **sample,
        }
        if sample["locked"] and read_vct:
            try:
                services = read_virtual_channels(adapter, timeout=vct_timeout)
                # One pass: the old flow ran w_scan for PIDs and then re-tuned
                # every hit with fetch_vct for PSIP names. Both tables are
                # available right here while the frequency is already locked.
                pids = read_program_pids(
                    adapter,
                    [item["service_id"] for item in services],
                    timeout=min(vct_timeout, 3.0),
                )
                for item in services:
                    entry = pids.get(item["service_id"], {})
                    item["video_pid"] = entry.get("video_pid")
                    item["audio_pid"] = entry.get("audio_pid")
                result["virtual_channels"] = services
            except OSError as exc:
                result["vct_error"] = str(exc)
        return result
    finally:
        if owned:
            os.close(fd)


def scan(channels=None, adapter=0, lock_timeout=2.0, vct_timeout=4.0,
         on_result=None, stop=None):
    """Sweep a list of RF channels, reporting each as it completes.

    One exclusive frontend handle is held for the whole sweep, which avoids
    an open/close cycle per channel.
    """
    if channels is None:
        channels = [item["rf_channel"] for item in rf_channels()]
    results = []
    fd = open_frontend(adapter, writable=True)
    try:
        for channel in channels:
            if stop is not None and stop.is_set():
                break
            result = scan_channel(
                channel, adapter=adapter, lock_timeout=lock_timeout,
                vct_timeout=vct_timeout, fd=fd,
            )
            results.append(result)
            if on_result is not None:
                on_result(result)
    finally:
        os.close(fd)
    return results


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", type=int,
                        help="scan one RF channel and exit")
    parser.add_argument("--start", type=int, default=FIRST_RF_CHANNEL)
    parser.add_argument("--end", type=int, default=LAST_RF_CHANNEL_IN_USE)
    parser.add_argument("--adapter", type=int, default=0)
    parser.add_argument("--lock-timeout", type=float, default=2.0)
    parser.add_argument("--dwell", type=float, default=0.0,
                        help="watch each channel for N seconds and report "
                             "the SNR spread and lock ratio")
    parser.add_argument("--measure", action="store_true",
                        help="read the current signal without tuning")
    args = parser.parse_args(argv)

    if args.measure:
        sample = measure(args.adapter)
        print(f"lock={sample['locked']} "
              f"signal={sample['signal_percent']}% "
              f"snr={sample['snr_db']} dB ber={sample['ber']}")
        return 0

    targets = [args.channel] if args.channel \
        else list(range(args.start, args.end + 1))

    def report(result):
        if result["locked"]:
            names = ", ".join(
                f"{item['number']} {item['name']}"
                + (f" (v{item['video_pid']}/a{item['audio_pid']})"
                   if item.get("video_pid") else "")
                for item in result["virtual_channels"]
            ) or "locked, no PSIP"
            spread = result.get("snr_db_range")
            detail = (
                f"SNR {spread['min']}-{spread['max']} dB "
                f"(mean {spread['mean']}) lock {result['lock_ratio']:.0%}"
                if spread else
                f"SNR {result['snr_db']} dB signal {result['signal_percent']}%"
            )
            print(f"RF {result['rf_channel']:2d} "
                  f"({result['frequency_mhz']:.0f} MHz)  {detail}  {names}")
        else:
            print(f"RF {result['rf_channel']:2d} "
                  f"({result['frequency_mhz']:.0f} MHz)  no lock")

    fd = open_frontend(args.adapter, writable=True)
    try:
        for channel in targets:
            report(scan_channel(
                channel, adapter=args.adapter,
                lock_timeout=args.lock_timeout, dwell=args.dwell, fd=fd,
            ))
    finally:
        os.close(fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

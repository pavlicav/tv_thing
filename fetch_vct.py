#!/usr/bin/env python3
"""Fetch ATSC PSIP Virtual Channel Table (VCT) for all broadcast frequencies.

The cx18 DVB driver's DVR device never produces data, but VLC's DVB stack
works around this. We tune each frequency with VLC (to lock the frontend),
then open a separate fd on /dev/dvb/adapter0/demux0 and install a DVB section
filter for PID 0x1FFB (PSIP base PID), table 0xC8 (Terrestrial VCT).
Section filters go through the demux device directly — a different code path
from the broken DVR device — so they should work even though DVR doesn't.

Output: vct_data.json
    {"<freq_hz>": {"<service_id>": {"major": N, "minor": N, "name": "..."}}}

parse_channels.py reads this file and uses the major.minor numbers instead of
the RF-channel-based fallback.
"""

import fcntl
import json
import os
import select
import signal
import struct
import subprocess
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent
VCT_CACHE = BASE_DIR / "vct_data.json"
WSCAN_OUTPUT = BASE_DIR / "w_scan_output.txt"
DEMUX_DEV = "/dev/dvb/adapter0/demux0"

# Linux DVB demux ioctls (linux/dvb/dmx.h, x86_64)
# DMX_SET_FILTER = _IOW('o', 43, struct dmx_sct_filter_params)  size=60 → 0x403C6F2B
# DMX_STOP       = _IO ('o', 42)                                         → 0x00006F2A
DMX_SET_FILTER = 0x403C6F2B
DMX_STOP       = 0x00006F2A

# ATSC PSIP
PSIP_PID      = 0x1FFB
TVCT_TABLE_ID = 0xC8   # Terrestrial Virtual Channel Table

# DMX flags
DMX_CHECK_CRC       = 1
DMX_IMMEDIATE_START = 4


def _make_section_filter(pid, table_id):
    """Pack a dmx_sct_filter_params struct (60 bytes).

    struct dmx_sct_filter_params layout:
      __u16 pid              2 bytes  offset 0
      struct dmx_filter {
        __u8 filter[16]     16 bytes  offset 2   ← table_id in byte 0
        __u8 mask[16]       16 bytes  offset 18  ← 0xFF in byte 0
        __u8 mode[16]       16 bytes  offset 34  ← all 0 (positive filter)
      }                     48 bytes total
      __u8  _pad[2]          2 bytes  offset 50  ← alignment to __u32
      __u32 timeout          4 bytes  offset 52  ← 0 = block until data
      __u32 flags            4 bytes  offset 56
    Total: 60 bytes
    """
    filt = bytes([table_id]) + bytes(15)
    mask = bytes([0xFF])     + bytes(15)
    mode = bytes(16)
    return struct.pack('<H48s2sII',
        pid,
        filt + mask + mode,
        b'\x00\x00',
        0,
        DMX_CHECK_CRC | DMX_IMMEDIATE_START,
    )


def _parse_tvct_section(data):
    """Parse one ATSC Terrestrial VCT section (table_id 0xC8).

    Returns list of dicts: {name, major, minor, service_id}.
    """
    if len(data) < 10 or data[0] != TVCT_TABLE_ID:
        return []

    num_channels = data[9]
    channels = []
    pos = 10

    for _ in range(num_channels):
        if pos + 32 > len(data):
            break

        # short_name: 7 UTF-16BE chars, NUL-padded (14 bytes)
        try:
            name = data[pos:pos+14].decode('utf-16-be').rstrip('\x00').strip()
        except Exception:
            name = ''

        # 4-byte word: reserved(4) | major_channel_number(10) | minor_channel_number(10) | modulation_mode(8)
        word = struct.unpack_from('>I', data, pos + 14)[0]
        major = (word >> 18) & 0x3FF
        minor = (word >> 8)  & 0x3FF

        # program_number == service_id, at pos+24
        service_id = struct.unpack_from('>H', data, pos + 24)[0]

        # descriptors_length at pos+30: low 10 bits of a 16-bit field
        desc_len = struct.unpack_from('>H', data, pos + 30)[0] & 0x03FF

        if major > 0 and minor > 0:   # skip hidden/reserved entries
            channels.append({
                'name': name,
                'major': major,
                'minor': minor,
                'service_id': service_id,
            })

        pos += 32 + desc_len

    return channels


def _kill(proc):
    if proc and proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    if proc and proc.stderr:
        try:
            proc.stderr.read()
        except Exception:
            pass


def fetch_vct_for_freq(freq_hz, lock_timeout=10, section_timeout=8):
    """Tune to freq_hz with VLC, read the Terrestrial VCT via a DVB section filter.

    Returns list of channel dicts, or [] if the section could not be read.
    """
    vlc_proc = None
    demux_fd = None

    try:
        # VLC tunes the frontend and holds the lock.
        # --no-video --no-audio: don't try to render anything.
        vlc_proc = subprocess.Popen(
            ['cvlc', f'atsc://frequency={freq_hz}', '--dvb-adapter=0',
             '--no-video', '--no-audio'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        # Wait for VLC to report a signal lock (or fall through after timeout).
        deadline = time.time() + lock_timeout
        while time.time() < deadline:
            r, _, _ = select.select([vlc_proc.stderr], [], [], 0.3)
            if r:
                line = vlc_proc.stderr.readline()
                if b'signal ok' in line.lower():
                    break
            if vlc_proc.poll() is not None:
                return []

        # Open a separate fd on demux0 for our section filter.
        # Section filters deliver data through the demux fd itself (not dvr0),
        # so this works even though the DVR device produces no data.
        demux_fd = os.open(DEMUX_DEV, os.O_RDWR)
        params = bytearray(_make_section_filter(PSIP_PID, TVCT_TABLE_ID))
        fcntl.ioctl(demux_fd, DMX_SET_FILTER, params)

        # Read one complete section (the kernel returns whole sections per read).
        r, _, _ = select.select([demux_fd], [], [], section_timeout)
        if not r:
            return []

        raw = os.read(demux_fd, 4096)
        return _parse_tvct_section(raw)

    except Exception as e:
        print(f"    VCT error at {freq_hz/1e6:.0f} MHz: {e}")
        return []

    finally:
        if demux_fd is not None:
            try:
                fcntl.ioctl(demux_fd, DMX_STOP)
            except Exception:
                pass
            try:
                os.close(demux_fd)
            except Exception:
                pass
        _kill(vlc_proc)
        time.sleep(1)   # let the DVB device settle before the next tune


def _get_frequencies():
    """Return sorted unique frequencies (Hz) from w_scan_output.txt."""
    if not WSCAN_OUTPUT.exists():
        return []
    freqs = set()
    with open(WSCAN_OUTPUT) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith(';'):
                continue
            parts = line.split(':')
            if len(parts) >= 2:
                try:
                    freqs.add(int(parts[1]))
                except ValueError:
                    pass
    return sorted(freqs)


def main(progress_cb=None):
    """Fetch VCT for all scanned frequencies and write vct_data.json.

    progress_cb: optional callable(str) for status messages (used by the
                 rescan SSE handler to send live updates to the browser).
    After writing vct_data.json, calls parse_channels.main() to rebuild
    channels.json with real PSIP virtual channel numbers.
    """
    frequencies = _get_frequencies()
    if not frequencies:
        if progress_cb:
            progress_cb("No frequencies found for VCT fetch.")
        return

    n = len(frequencies)
    if progress_cb:
        progress_cb(f"Reading PSIP channel numbers ({n} frequencies)...")

    vct_data = {}

    for i, freq in enumerate(frequencies, 1):
        mhz = f"{freq/1e6:.0f}"
        if progress_cb:
            progress_cb(f"VCT {i}/{n}: {mhz} MHz")
        print(f"  {mhz} MHz...", end='', flush=True)

        channels = fetch_vct_for_freq(freq)

        if channels:
            vct_data[str(freq)] = {
                str(ch['service_id']): {
                    'major': ch['major'],
                    'minor': ch['minor'],
                    'name':  ch['name'],
                }
                for ch in channels
            }
            nums = ', '.join(f"{c['major']}.{c['minor']}" for c in channels[:4])
            if len(channels) > 4:
                nums += f" +{len(channels)-4}"
            print(f" {nums}")
        else:
            print(" no VCT data")

    with open(VCT_CACHE, 'w') as f:
        json.dump(vct_data, f, indent=2)
    print(f"Wrote {VCT_CACHE}")

    if progress_cb:
        progress_cb("Updating channel list with PSIP numbers...")

    import parse_channels
    parse_channels.main()


if __name__ == '__main__':
    main()

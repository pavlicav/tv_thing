#!/usr/bin/env python3
"""Scan ATSC Virtual Channel Table (VCT) from each frequency using VLC.

Captures a few seconds of transport stream from each frequency,
then parses the PSIP TVCT (table_id 0xC8) to extract the real
virtual major.minor channel numbers for each program.
"""

import json
import os
import signal
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent
CHANNELS_CONF = BASE_DIR / "channels.conf"
CHANNELS_JSON = BASE_DIR / "channels.json"
VCT_CACHE = BASE_DIR / "vct_data.json"

TS_PACKET_SIZE = 188
PSIP_PID = 0x1FFB
TVCT_TABLE_ID = 0xC8


def parse_ts_packets(data, target_pid):
    """Extract payload bytes from TS packets matching target_pid."""
    sections = []
    buf = bytearray()
    collecting = False

    # Sync to first 0x47 byte
    start = data.find(b'\x47')
    if start < 0:
        return sections

    pos = start
    while pos + TS_PACKET_SIZE <= len(data):
        if data[pos] != 0x47:
            # Lost sync, find next
            pos = data.find(b'\x47', pos + 1)
            if pos < 0:
                break
            continue

        packet = data[pos:pos + TS_PACKET_SIZE]
        pos += TS_PACKET_SIZE

        pid = ((packet[1] & 0x1F) << 8) | packet[2]
        if pid != target_pid:
            continue

        pusi = (packet[1] & 0x40) != 0
        adaptation = (packet[3] & 0x30) >> 4
        payload_start = 4

        if adaptation in (2, 3):
            adapt_len = packet[4]
            payload_start = 5 + adapt_len

        if adaptation in (0, 2):
            continue  # No payload

        if payload_start >= TS_PACKET_SIZE:
            continue

        payload = packet[payload_start:]

        if pusi:
            # Pointer field
            pointer = payload[0]
            if pointer > 0 and collecting and len(buf) > 0:
                buf.extend(payload[1:1 + pointer])
                sections.append(bytes(buf))
            # Start new section
            buf = bytearray(payload[1 + pointer:])
            collecting = True
        elif collecting:
            buf.extend(payload)

    if collecting and len(buf) > 0:
        sections.append(bytes(buf))

    return sections


def parse_tvct(section_data):
    """Parse a TVCT section, return list of (short_name, major, minor, program_number)."""
    if len(section_data) < 10:
        return []

    table_id = section_data[0]
    if table_id != TVCT_TABLE_ID:
        return []

    section_length = ((section_data[1] & 0x0F) << 8) | section_data[2]
    if len(section_data) < section_length + 3:
        # Truncated, try anyway
        pass

    # Skip to num_channels_in_section (offset 9)
    if len(section_data) < 10:
        return []

    num_channels = section_data[9]
    channels = []
    offset = 10

    for _ in range(num_channels):
        if offset + 32 > len(section_data):
            break

        # short_name: 7 x 16-bit UTF-16 characters (14 bytes)
        short_name_raw = section_data[offset:offset + 14]
        try:
            short_name = short_name_raw.decode('utf-16-be').rstrip('\x00').strip()
        except Exception:
            short_name = ""
        offset += 14

        # 4 reserved bits + 10 major + 10 minor = 24 bits = 3 bytes
        bits = (section_data[offset] << 16) | (section_data[offset + 1] << 8) | section_data[offset + 2]
        major = (bits >> 10) & 0x3FF
        minor = bits & 0x3FF
        offset += 3

        # modulation_mode (1 byte)
        offset += 1

        # carrier_frequency (4 bytes)
        offset += 4

        # channel_TSID (2 bytes)
        offset += 2

        # program_number (2 bytes) = service_id
        program_number = (section_data[offset] << 8) | section_data[offset + 1]
        offset += 2

        # ETM_location(2) + access_controlled(1) + hidden(1) + reserved(2) + hide_guide(1) + reserved(3) + service_type(6) = 16 bits = 2 bytes
        offset += 2

        # source_id (2 bytes)
        offset += 2

        # descriptors_length (6 reserved + 10 length = 2 bytes)
        if offset + 2 > len(section_data):
            break
        desc_len = ((section_data[offset] & 0x03) << 8) | section_data[offset + 1]
        offset += 2 + desc_len

        if major > 0 and minor > 0:
            channels.append((short_name, major, minor, program_number))

    return channels


def capture_frequency(freq, duration=8):
    """Capture TS data from a frequency using VLC."""
    with tempfile.NamedTemporaryFile(suffix='.ts', delete=False) as tmp:
        tmp_path = tmp.name

    try:
        cmd = [
            "cvlc",
            f"atsc://frequency={freq}",
            "--dvb-adapter=0",
            "--run-time", str(duration),
            "--sout", f"#std{{access=file,mux=ts,dst={tmp_path}}}",
            "vlc://quit",
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            proc.wait(timeout=duration + 15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

        if os.path.exists(tmp_path):
            with open(tmp_path, 'rb') as f:
                data = f.read()
            return data
        return b''
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def get_frequencies():
    """Get unique frequencies from channels.conf."""
    freqs = set()
    if not CHANNELS_CONF.exists():
        return []
    with open(CHANNELS_CONF) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split(':')
            if len(parts) >= 2:
                freqs.add(int(parts[1]))
    return sorted(freqs)


def scan_all_vct():
    """Scan VCT data from all frequencies. Returns {freq: {service_id: (major, minor, name)}}."""
    freqs = get_frequencies()
    if not freqs:
        print("No frequencies found in channels.conf")
        return {}

    vct_map = {}
    for i, freq in enumerate(freqs):
        print(f"[{i+1}/{len(freqs)}] Scanning {freq/1e6:.0f} MHz...", end=" ", flush=True)
        data = capture_frequency(freq)
        if not data:
            print("no data")
            continue

        print(f"{len(data)} bytes", end=" ", flush=True)

        # Parse PSIP sections from TS
        sections = parse_ts_packets(data, PSIP_PID)
        channels = []
        seen = set()
        for section in sections:
            for entry in parse_tvct(section):
                key = (entry[1], entry[2], entry[3])
                if key not in seen:
                    seen.add(key)
                    channels.append(entry)

        if channels:
            vct_map[str(freq)] = {}
            for short_name, major, minor, program_number in channels:
                vct_map[str(freq)][str(program_number)] = {
                    "major": major,
                    "minor": minor,
                    "vct_name": short_name,
                }
            names = [f"{c[1]}.{c[2]} {c[0]}" for c in channels]
            print(f"-> {', '.join(names)}")
        else:
            print("no VCT found")

    return vct_map


def apply_vct_to_channels(vct_map):
    """Read channels.conf, apply VCT data, write channels.json."""
    if not CHANNELS_CONF.exists():
        print("No channels.conf found")
        return

    channels = []
    with open(CHANNELS_CONF) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split(':')
            if len(parts) < 6:
                continue

            import re
            name = parts[0].strip()
            freq = int(parts[1])
            modulation = parts[2].replace("8VSB", "VSB_8")
            video_pid = int(parts[3])
            audio_pid = int(parts[4])
            service_id = int(parts[5])

            # Look up VCT data
            freq_data = vct_map.get(str(freq), {})
            vct_entry = freq_data.get(str(service_id))

            if vct_entry:
                major = vct_entry["major"]
                minor = vct_entry["minor"]
                number = f"{major}.{minor}"
            else:
                # Fallback: use frequency-based numbering
                number = f"?.{service_id}"

            chan_id = re.sub(r'[^a-zA-Z0-9]', '_', name).strip('_').lower()
            chan_id = f"{chan_id}_{number.replace('.', '_')}"

            channels.append({
                "id": chan_id,
                "name": name,
                "number": number,
                "frequency": freq,
                "video_pid": video_pid,
                "audio_pid": audio_pid,
                "service_id": service_id,
                "modulation": modulation,
            })

    # Sort by major, then minor
    def sort_key(c):
        parts = c["number"].split(".")
        try:
            return (int(parts[0]), int(parts[1]))
        except (ValueError, IndexError):
            return (9999, 0)

    channels.sort(key=sort_key)

    with open(CHANNELS_JSON, "w") as f:
        json.dump(channels, f, indent=2)

    print(f"\nWrote {len(channels)} channels to {CHANNELS_JSON}")
    for ch in channels:
        print(f"  {ch['number']:>6s}  {ch['name']:<35s}  {ch['frequency']/1e6:.0f} MHz")

    return channels


def main():
    print("ATSC Virtual Channel Table Scanner")
    print("===================================")
    print("This captures a few seconds from each frequency to read")
    print("the real virtual channel numbers (major.minor) from PSIP.")
    print()

    vct_map = scan_all_vct()

    if vct_map:
        # Cache VCT data for future use
        with open(VCT_CACHE, 'w') as f:
            json.dump(vct_map, f, indent=2)
        print(f"\nCached VCT data to {VCT_CACHE}")

        apply_vct_to_channels(vct_map)
    else:
        print("No VCT data found on any frequency!")


if __name__ == "__main__":
    main()

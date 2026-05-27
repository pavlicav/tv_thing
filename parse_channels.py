#!/usr/bin/env python3
"""Parse w_scan/channels.conf output into channels.json for the TV Thing server.

Uses VCT data (vct_data.json) for correct virtual channel numbers when available.
Falls back to RF channel number + sequential minor numbering.

Channel data format (w_scan -X or channels.conf):
  NAME:FREQUENCY:MODULATION:VIDEO_PID:AUDIO_PID:SERVICE_ID
"""

import json
import re
from pathlib import Path

BASE_DIR = Path(__file__).parent
WSCAN_OUTPUT = BASE_DIR / "w_scan_output.txt"
CHANNELS_CONF = BASE_DIR / "channels.conf"
CHANNELS_JSON = BASE_DIR / "channels.json"
VCT_CACHE = BASE_DIR / "vct_data.json"

# Map frequency (Hz) to RF channel number (for fallback numbering)
def freq_to_rf(freq):
    """Convert frequency in Hz to ATSC RF channel number."""
    f = freq / 1_000_000  # MHz
    if 54 <= f < 72:        # VHF-Lo ch 2-4: 54, 60, 66 MHz
        return int((f - 54) / 6) + 2
    elif 76 <= f < 88:      # VHF-Lo ch 5-6: 76, 82 MHz (gap at 72-76)
        return int((f - 76) / 6) + 5
    elif 174 <= f <= 216:   # VHF-Hi: ch 7-13
        return int((f - 174) / 6) + 7
    elif 470 <= f <= 890:   # UHF: ch 14-83
        return int((f - 470) / 6) + 14
    return 0


def load_vct_data():
    """Load cached VCT data if available."""
    if VCT_CACHE.exists():
        with open(VCT_CACHE) as f:
            return json.load(f)
    return {}


def parse():
    # Use w_scan output if available, fall back to channels.conf
    if WSCAN_OUTPUT.exists() and WSCAN_OUTPUT.stat().st_size > 0:
        source = WSCAN_OUTPUT
    elif CHANNELS_CONF.exists():
        source = CHANNELS_CONF
    else:
        print("No scan data found")
        return []

    vct_data = load_vct_data()

    # First pass: collect entries grouped by frequency
    by_freq = {}
    with open(source) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith(";") or line.startswith("#"):
                continue
            parts = line.split(":")
            if len(parts) < 6:
                continue

            name = parts[0].strip()
            freq = int(parts[1])
            modulation = parts[2].replace("8VSB", "VSB_8")
            video_pid = int(parts[3])
            audio_pid = int(parts[4])
            service_id = int(parts[5])

            by_freq.setdefault(freq, []).append({
                "name": name,
                "frequency": freq,
                "video_pid": video_pid,
                "audio_pid": audio_pid,
                "service_id": service_id,
                "modulation": modulation,
            })

    # Second pass: assign channel numbers
    channels = []
    for freq, entries in by_freq.items():
        freq_vct = vct_data.get(str(freq), {})
        rf = freq_to_rf(freq)

        # Sort by service_id within each frequency
        entries.sort(key=lambda e: e["service_id"])

        for minor, entry in enumerate(entries, start=1):
            sid = str(entry["service_id"])

            # Use VCT data if available, otherwise RF channel + sequential minor
            if sid in freq_vct:
                v = freq_vct[sid]
                number = f"{v['major']}.{v['minor']}"
            else:
                number = f"{rf}.{minor}"

            chan_id = re.sub(r'[^a-zA-Z0-9]', '_', entry["name"]).strip('_').lower()
            chan_id = f"{chan_id}_{number.replace('.', '_')}"
            entry["id"] = chan_id
            entry["number"] = number
            entry["rf_channel"] = rf
            channels.append(entry)

    # All .1 channels first (sorted by major), then subchannels grouped by major
    def sort_key(c):
        parts = c["number"].split(".")
        try:
            major, minor = int(parts[0]), int(parts[1])
            if minor == 1:
                return (0, major, 0)
            return (1, major, minor)
        except (ValueError, IndexError):
            return (9999, 0, 0)

    channels.sort(key=sort_key)
    return channels


def main():
    channels = parse()
    if not channels:
        print("No channels found. Run: w_scan -fa -c US -X > w_scan_output.txt")
        return

    with open(CHANNELS_JSON, "w") as f:
        json.dump(channels, f, indent=2)

    print(f"Wrote {len(channels)} channels to {CHANNELS_JSON}")
    if not VCT_CACHE.exists():
        print("(Using RF channel numbers — VCT data not available)")
    print()
    for ch in channels:
        print(f"  {ch['number']:>6s}  {ch['name']:<35s}  {ch['frequency']/1e6:.0f} MHz")


if __name__ == "__main__":
    main()

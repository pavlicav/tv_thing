#!/usr/bin/env python3
"""Read ATSC VCT using DVB section filtering.

Uses the demux device's section filter (DMX_SET_FILTER) to read
PSIP tables directly. This uses a different kernel path than bulk
TS streaming via the DVR device.
"""

import fcntl
import json
import os
import struct
import time
from pathlib import Path

# DVB device paths
FRONTEND = "/dev/dvb/adapter0/frontend0"
DEMUX = "/dev/dvb/adapter0/demux0"

# PSIP constants
PSIP_PID = 0x1FFB
TVCT_TABLE_ID = 0xC8
MGT_TABLE_ID = 0xC7

# Ioctl constants (from linux/dvb/frontend.h and dmx.h)
FE_SET_PROPERTY = 0x40106F52
FE_READ_STATUS = 0x80046F45

# DTV property IDs
DTV_CLEAR = 2
DTV_FREQUENCY = 3
DTV_MODULATION = 4
DTV_DELIVERY_SYSTEM = 17
DTV_TUNE = 1
SYS_ATSC = 11
VSB_8 = 7

# DMX ioctl
DMX_SET_FILTER = 0x403C6F2B  # _IOW('o', 43, struct dmx_sct_filter_params)
DMX_START = 0x00006F29
DMX_STOP = 0x00006F2A

BASE_DIR = Path(__file__).parent
CHANNELS_CONF = BASE_DIR / "channels.conf"
VCT_CACHE = BASE_DIR / "vct_data.json"


def dtv_property(prop_id, value):
    """Create a single DTV property command."""
    # struct dtv_property: u32 cmd, u32 reserved[3], union data (u32), u32 result
    prop = struct.pack("=I3I", prop_id, 0, 0, 0)
    prop += struct.pack("=I", value)
    prop += b'\x00' * 16  # rest of union + result
    return prop


def tune_frontend(fd, frequency):
    """Tune the DVB frontend to an ATSC frequency."""
    for prop_id, value in [
        (DTV_CLEAR, 0),
        (DTV_DELIVERY_SYSTEM, SYS_ATSC),
        (DTV_FREQUENCY, frequency),
        (DTV_MODULATION, VSB_8),
        (DTV_TUNE, 0),
    ]:
        prop_data = dtv_property(prop_id, value)
        # struct dtv_properties: u32 num, dtv_property* props
        # We need to pass this via ioctl - use bytearray approach
        props_buf = bytearray(struct.pack("=I", 1))
        # Pad to 8 bytes for pointer alignment
        props_buf += b'\x00' * 4
        props_buf += prop_data
        # The ioctl expects {u32 num, dtv_property *props}
        # We need the pointer to point to our prop_data
        # Use a simpler approach: allocate combined buffer
        import ctypes
        prop_array = (ctypes.c_byte * len(prop_data))(*prop_data)
        ptr = ctypes.addressof(prop_array)
        cmd_buf = struct.pack("=IxxxxQ" if struct.calcsize("P") == 8 else "=IxxxxI",
                              1, ptr)
        try:
            fcntl.ioctl(fd, FE_SET_PROPERTY, cmd_buf)
        except OSError as e:
            print(f"  ioctl prop {prop_id} failed: {e}")
            return False
    return True


def wait_for_lock(fd, timeout=5):
    """Wait for frontend lock."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        status_buf = bytearray(4)
        try:
            fcntl.ioctl(fd, FE_READ_STATUS, status_buf)
            status = struct.unpack("=I", status_buf)[0]
            if status & 0x10:  # FE_HAS_LOCK
                return True
        except OSError:
            pass
        time.sleep(0.2)
    return False


def setup_section_filter(fd, pid, table_id):
    """Set up a section filter on the demux device."""
    # struct dmx_sct_filter_params {
    #   __u16 pid;
    #   dmx_filter_t filter;  // 16 + 16 + 16 bytes = 48 bytes
    #   __u32 timeout;
    #   __u32 flags;
    # };
    # dmx_filter_t { __u8 filter[16]; __u8 mask[16]; __u8 mode[16]; }
    filter_bytes = bytearray(16)
    mask_bytes = bytearray(16)
    mode_bytes = bytearray(16)

    filter_bytes[0] = table_id
    mask_bytes[0] = 0xFF

    params = struct.pack("=H", pid)
    params += bytes(filter_bytes) + bytes(mask_bytes) + bytes(mode_bytes)
    params += struct.pack("=II", 5000, 0x01)  # timeout=5s, flags=DMX_CHECK_CRC

    # Pad to expected size
    while len(params) < 60:
        params += b'\x00'

    fcntl.ioctl(fd, DMX_SET_FILTER, params)


def parse_tvct_section(data):
    """Parse a TVCT section."""
    if len(data) < 10 or data[0] != TVCT_TABLE_ID:
        return []

    section_length = ((data[1] & 0x0F) << 8) | data[2]
    num_channels = data[9]
    channels = []
    offset = 10

    for _ in range(num_channels):
        if offset + 32 > len(data):
            break

        # short_name: 7 x 16-bit UTF-16BE (14 bytes)
        short_name_raw = data[offset:offset + 14]
        try:
            short_name = short_name_raw.decode('utf-16-be').rstrip('\x00').strip()
        except Exception:
            short_name = ""
        offset += 14

        # 4 reserved + 10 major + 10 minor = 3 bytes
        bits = (data[offset] << 16) | (data[offset + 1] << 8) | data[offset + 2]
        major = (bits >> 10) & 0x3FF
        minor = bits & 0x3FF
        offset += 3

        # modulation(1) + carrier_freq(4) + tsid(2) + program_number(2)
        offset += 1 + 4 + 2
        program_number = (data[offset] << 8) | data[offset + 1]
        offset += 2

        # flags(2) + source_id(2)
        offset += 2 + 2

        # descriptors_length
        if offset + 2 > len(data):
            break
        desc_len = ((data[offset] & 0x03) << 8) | data[offset + 1]
        offset += 2 + desc_len

        if major > 0:
            channels.append({
                "name": short_name,
                "major": major,
                "minor": minor,
                "program_number": program_number,
            })

    return channels


def scan_frequency(frequency):
    """Tune to frequency and read VCT via section filter."""
    fe_fd = None
    dmx_fd = None

    try:
        fe_fd = os.open(FRONTEND, os.O_RDWR)
        print(f"  Tuning to {frequency/1e6:.0f} MHz...", end=" ", flush=True)

        if not tune_frontend(fe_fd, frequency):
            print("tune failed")
            return []

        if not wait_for_lock(fe_fd):
            print("no lock")
            return []

        print("locked.", end=" ", flush=True)

        # Open demux and set section filter for TVCT
        dmx_fd = os.open(DEMUX, os.O_RDWR | os.O_NONBLOCK)
        setup_section_filter(dmx_fd, PSIP_PID, TVCT_TABLE_ID)

        # Read sections
        channels = []
        deadline = time.time() + 8
        seen = set()

        while time.time() < deadline:
            try:
                data = os.read(dmx_fd, 4096)
                if data:
                    entries = parse_tvct_section(data)
                    for e in entries:
                        key = (e["major"], e["minor"])
                        if key not in seen:
                            seen.add(key)
                            channels.append(e)
            except BlockingIOError:
                time.sleep(0.1)
            except OSError as e:
                if e.errno == 11:  # EAGAIN
                    time.sleep(0.1)
                else:
                    break

        if channels:
            names = [f"{c['major']}.{c['minor']} {c['name']}" for c in channels]
            print(f"found: {', '.join(names)}")
        else:
            print("no VCT data")

        return channels

    except OSError as e:
        print(f"error: {e}")
        return []
    finally:
        if dmx_fd is not None:
            try:
                fcntl.ioctl(dmx_fd, DMX_STOP)
            except OSError:
                pass
            os.close(dmx_fd)
        if fe_fd is not None:
            os.close(fe_fd)


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


def main():
    print("ATSC VCT Scanner (section filter)")
    print("==================================")

    freqs = get_frequencies()
    if not freqs:
        print("No frequencies in channels.conf")
        return

    all_vct = {}
    for i, freq in enumerate(freqs):
        print(f"[{i+1}/{len(freqs)}]", end=" ")
        channels = scan_frequency(freq)
        if channels:
            freq_map = {}
            for ch in channels:
                freq_map[str(ch["program_number"])] = {
                    "major": ch["major"],
                    "minor": ch["minor"],
                    "vct_name": ch["name"],
                }
            all_vct[str(freq)] = freq_map
        time.sleep(1)

    if all_vct:
        with open(VCT_CACHE, 'w') as f:
            json.dump(all_vct, f, indent=2)
        print(f"\nSaved VCT data to {VCT_CACHE}")
        print("Run: python3 parse_channels.py  to rebuild channels.json")
    else:
        print("\nNo VCT data found on any frequency")


if __name__ == "__main__":
    main()

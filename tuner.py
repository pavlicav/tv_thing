#!/usr/bin/env python3
"""Direct DVB frontend tuning via ioctl.

Tunes the DVB adapter to an ATSC frequency without needing
dvbv5-zap, azap, or other external tools.
"""

import ctypes
import fcntl
import os
import time


# --- ioctl numbers (Linux DVB API v5, 64-bit) ---
# FE_SET_PROPERTY = _IOW('o', 82, struct dtv_properties)  [16 bytes on x86_64]
# FE_GET_PROPERTY = _IOR('o', 83, struct dtv_properties)
# FE_READ_STATUS  = _IOR('o', 69, fe_status_t)            [4 bytes]
FE_SET_PROPERTY = 0x40106F52
FE_GET_PROPERTY = 0x80106F53
FE_READ_STATUS = 0x80046F45

# DTV property commands
DTV_FREQUENCY = 3
DTV_MODULATION = 4
DTV_DELIVERY_SYSTEM = 17
DTV_TUNE = 18
DTV_CLEAR = 19

# Delivery systems
SYS_ATSC = 11

# Modulation
VSB_8 = 7

# Frontend status flags
FE_HAS_SIGNAL = 0x01
FE_HAS_CARRIER = 0x02
FE_HAS_VITERBI = 0x04
FE_HAS_SYNC = 0x08
FE_HAS_LOCK = 0x10

# DMX ioctl constants
DMX_SET_PES_FILTER = 0x40146F2C
DMX_START = 0x6F29
DMX_STOP = 0x6F2A
DMX_SET_BUFFER_SIZE = 0x6F2D

# DMX enums
DMX_IN_FRONTEND = 0
DMX_OUT_TS_TAP = 2
DMX_PES_OTHER = 20
DMX_IMMEDIATE_START = 0x01


class DtvProperty(ctypes.Structure):
    """struct dtv_property — 48 bytes on x86_64.

    Layout from linux/dvb/frontend.h:
        __u32 cmd;
        __u32 reserved[3];
        union { __u32 data; ... } u;  (48 bytes)
        int result;
    Total = 4 + 12 + 48 + 4 = 68 bytes? No...

    Actually the kernel struct is simpler for our purposes.
    We just need cmd + data at known offsets. The union starts
    at offset 16 and data is the first __u32 in it.
    """
    _fields_ = [
        ("cmd", ctypes.c_uint32),           # offset 0
        ("reserved", ctypes.c_uint32 * 3),  # offset 4
        ("data", ctypes.c_uint32),          # offset 16 (first member of union)
        ("_pad", ctypes.c_uint8 * 44),      # rest of union
        ("result", ctypes.c_int32),         # after union
    ]


class DtvProperties(ctypes.Structure):
    """struct dtv_properties — 16 bytes on x86_64.

    { __u32 num; struct dtv_property *props; }
    With padding: 4 + 4pad + 8ptr = 16 bytes.
    """
    _fields_ = [
        ("num", ctypes.c_uint32),
        ("_pad", ctypes.c_uint32),
        ("props", ctypes.c_uint64),  # pointer as uint64
    ]


class DmxPesFilterParams(ctypes.Structure):
    """struct dmx_pes_filter_params"""
    _fields_ = [
        ("pid", ctypes.c_uint16),
        ("input", ctypes.c_uint32),
        ("output", ctypes.c_uint32),
        ("pes_type", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
    ]


def set_dtv_props(fd, prop_list):
    """Send a list of (cmd, data) tuples to the DVB frontend."""
    n = len(prop_list)
    PropArray = DtvProperty * n
    props = PropArray()
    for i, (cmd, data) in enumerate(prop_list):
        props[i].cmd = cmd
        props[i].data = data

    dtv = DtvProperties()
    dtv.num = n
    dtv.props = ctypes.addressof(props)

    fcntl.ioctl(fd, FE_SET_PROPERTY, dtv)


def tune_atsc(adapter=0, frequency_hz=0):
    """Tune DVB adapter to an ATSC frequency.

    Args:
        adapter: DVB adapter number
        frequency_hz: Frequency in Hz (e.g., 177000000 for 177 MHz)

    Returns:
        True if tuned and locked, False otherwise
    """
    frontend_path = f"/dev/dvb/adapter{adapter}/frontend0"
    fd = os.open(frontend_path, os.O_RDWR)

    try:
        # Clear old properties
        set_dtv_props(fd, [(DTV_CLEAR, 0)])

        # Set tuning properties
        set_dtv_props(fd, [
            (DTV_DELIVERY_SYSTEM, SYS_ATSC),
            (DTV_FREQUENCY, frequency_hz),
            (DTV_MODULATION, VSB_8),
            (DTV_TUNE, 0),
        ])

        # Wait for lock
        for i in range(30):  # up to 3 seconds
            status = ctypes.c_uint32(0)
            fcntl.ioctl(fd, FE_READ_STATUS, status)
            if status.value & FE_HAS_LOCK:
                print(f"Tuner locked on {frequency_hz/1e6:.1f} MHz "
                      f"(status=0x{status.value:02x})")
                return True
            time.sleep(0.1)

        status = ctypes.c_uint32(0)
        fcntl.ioctl(fd, FE_READ_STATUS, status)
        print(f"Tune failed: no lock after 3s "
              f"(status=0x{status.value:02x}, freq={frequency_hz/1e6:.1f} MHz)")
        return False

    finally:
        os.close(fd)


def setup_demux(adapter=0):
    """Set up the demux to pass all PIDs to the DVR device.

    Returns the demux file descriptor (keep open while streaming).
    """
    demux_path = f"/dev/dvb/adapter{adapter}/demux0"
    fd = os.open(demux_path, os.O_RDWR)

    # Set large buffer
    fcntl.ioctl(fd, DMX_SET_BUFFER_SIZE, 1024 * 1024)

    # Set PES filter to pass all PIDs
    params = DmxPesFilterParams()
    params.pid = 0x2000  # all PIDs
    params.input = DMX_IN_FRONTEND
    params.output = DMX_OUT_TS_TAP
    params.pes_type = DMX_PES_OTHER
    params.flags = DMX_IMMEDIATE_START

    fcntl.ioctl(fd, DMX_SET_PES_FILTER, params)

    return fd


def stop_demux(fd):
    """Stop and close the demux."""
    try:
        fcntl.ioctl(fd, DMX_STOP)
    except Exception:
        pass
    os.close(fd)


if __name__ == "__main__":
    import sys
    freq_mhz = float(sys.argv[1]) if len(sys.argv) > 1 else 177.0
    freq_hz = int(freq_mhz * 1_000_000)
    print(f"Tuning to {freq_mhz} MHz...")
    if tune_atsc(frequency_hz=freq_hz):
        print("Locked! Setting up demux...")
        dmx_fd = setup_demux()
        print(f"Demux active. Stream at /dev/dvb/adapter0/dvr0")
        print("Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            stop_demux(dmx_fd)
            print("Stopped.")
    else:
        print("Failed to lock.")
        sys.exit(1)

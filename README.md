# TV Thing

A personal LAN server that streams live over-the-air ATSC broadcast TV to any browser or Roku on your home network.

---

## What it does

TV Thing scans the ATSC broadcast frequencies, builds a channel list from the PSIP virtual channel table, and streams any channel on demand as HLS (HTTP Live Streaming). You pick a channel in the web UI or the Roku app; the server tunes the tuner card, transcodes via VLC, and serves the stream over your LAN.

---

## Hardware

- **Tuner card:** Hauppauge HVR-1600 (PCIe), using the `cx18` kernel driver
- **Works with:** any Linux machine with a supported DVB/ATSC tuner card
- **Coverage:** DC metro area — 69 ATSC channels across 12 RF frequencies

---

## How it works

```
Antenna → DVB tuner card → VLC (tuning + transcode) → HLS → Browser / Roku
```

1. **Channel scan** — `w_scan` finds active RF frequencies; `fetch_vct.py` reads PSIP Virtual Channel Table data via DVB section filter to get real virtual channel numbers (e.g. 4.1, 7.2).
2. **Tuning** — `server.py` spawns `cvlc` pointed at the selected ATSC frequency and program/service ID. VLC handles DVB tuning internally, bypassing a quirk in the `cx18` driver where the kernel DVR device produces no data.
3. **Transcoding** — VLC transcodes the MPEG-2 transport stream to H.264/AAC at the selected quality preset and outputs HLS (`.m3u8` + `.ts` segments).
4. **Serving** — A Python HTTP server (`server.py`) serves the web UI, the HLS segments, and a small JSON API.

---

## Components

| File / Directory | Purpose |
|---|---|
| `server.py` | Main HTTP server and streaming controller |
| `tuner.py` | Low-level DVB frontend tuning via ioctl (diagnostic use) |
| `fetch_vct.py` | Reads PSIP VCT from DVB section filter to get virtual channel numbers |
| `parse_channels.py` | Parses `w_scan` output into `channels.json` |
| `scan_channels.sh` | Runs `w_scan` to detect active ATSC frequencies |
| `channels.json` | Channel database: name, virtual ch #, RF ch #, frequency, SID |
| `static/` | Web UI: channel list, player (hls.js), rescan page |
| `roku_app/` | Roku sideload app (BrightScript / SceneGraph) |
| `tv-thing.service` | systemd unit to run the server at boot |

---

## Web UI

Browse to `http://<server-ip>:8080` on any device on your LAN.

- Channel list sorted by virtual channel number (primaries first, then subchannels)
- Shows RF channel alongside virtual channel number
- Quality selector: **Low** (480p / 500 kbps), **Medium** (720p / 1 Mbps), **High** (native / 2 Mbps)
- Live HLS playback via [hls.js](https://github.com/video-dev/hls.js) — bundled locally, no internet required
- Rescan page (`/rescan`) re-runs `w_scan` and repopulates the channel list

---

## Roku App

A native Roku app is included in `roku_app/`. Sideload it onto any Roku on the same LAN.

- Channel list mirrors the web UI
- D-pad navigation; OK to tune, Back to stop
- HLS played natively by the Roku media player

See [`roku_app/README.md`](roku_app/README.md) for setup and sideloading instructions.

---

## Setup

### Dependencies

```bash
# Debian/Ubuntu
sudo apt install vlc w-scan dvb-apps python3
```

### Run the server

```bash
python3 server.py
# Listening on 0.0.0.0:8080
```

### Run at boot (systemd)

```bash
sudo cp tv-thing.service /etc/systemd/system/
sudo systemctl enable --now tv-thing
```

### Scan for channels

```bash
bash scan_channels.sh          # runs w_scan, saves w_scan_output.txt
python3 fetch_vct.py           # reads PSIP VCT for virtual channel numbers
python3 parse_channels.py      # generates channels.json
```

Or use the **Rescan** button in the web UI, which runs this pipeline and reloads the channel list automatically.

---

## cx18 driver quirk

The Hauppauge HVR-1600 uses the `cx18` kernel driver. The DVB frontend tunes and locks correctly, but the kernel DVR device (`/dev/dvb/adapter0/dvr0`) never produces data — neither all-PIDs nor filtered. VLC's internal DVB stack bypasses this entirely and works correctly, so the server uses `cvlc` as its tuning and streaming engine rather than reading from the DVR device directly.

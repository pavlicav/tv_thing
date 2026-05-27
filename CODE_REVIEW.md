# TV Thing — Code Review

## How It Works

TV Thing is an over-the-air ATSC TV streaming server built around a hardware quirk: the Hauppauge HVR-1600's DVB kernel driver (`cx18`) tunes correctly but its `/dev/dvb` device never produces data. VLC's built-in DVB stack bypasses that broken path entirely and works.

### Data flow

```
Antenna → HVR-1600 (cx18) → cvlc (DVB tune + demux + transcode) → HLS segments → HTTP → hls.js → browser
```

1. `w_scan -fa -c US -X` scans ATSC frequencies and outputs channel data in channels.conf format
2. `parse_channels.py` converts that to `channels.json` with virtual channel numbers
3. `server.py` serves the web UI and API on port 8080
4. When a channel is selected, `cvlc` is spawned to tune the tuner, extract the requested program from the MPEG-TS stream, transcode to H.264/AAC, and write HLS segments to `streams/`
5. The browser loads `live.m3u8` via hls.js and plays the stream

### Component map

| File | Role |
|---|---|
| `server.py` | Main HTTP server, tuner control, rescan SSE |
| `parse_channels.py` | Converts `w_scan` output + VCT data → `channels.json` |
| `fetch_vct.py` | Reads ATSC PSIP Virtual Channel Table via DVB section filter |
| `scan_channels.sh` | One-shot shell wrapper for manual scans |
| `static/index.html` | Player UI with channel list |
| `static/rescan.html` | Rescan page — RF channel grid, live scan progress |
| `tv-thing.service` | systemd unit |

### VLC command

```
cvlc atsc://frequency=FREQ
     --dvb-adapter=0
     --program=SERVICE_ID
     --no-sout-all
     --sout "#transcode{vcodec=h264,vb=2000,acodec=aac,ab=128,channels=2}
             :std{access=livehttp{seglen=4,delsegs=true,numsegs=5,
                  index=streams/live.m3u8,index-url=segment_###.ts},
                  mux=ts{use-key-frames},dst=streams/segment_###.ts}"
```

4-second segments, 5 kept at a time, old ones deleted. The client waits 5 seconds after the tune call before loading the playlist to let the first segments accumulate.

### Channel numbering

`parse_channels.py` groups channels by RF frequency and assigns virtual channel numbers in two ways:
- **VCT data** (`vct_data.json`): Uses the actual major.minor numbers broadcast in the ATSC Virtual Channel Table (e.g., `4.1` for WRC-TV on RF 34)
- **Fallback**: Derives the RF channel number from the frequency, then numbers sub-channels sequentially (e.g., RF 7 → `7.1`, `7.2`, ...)

Channels are sorted so all `.1` primaries appear first (sorted by major number), then subchannels grouped by their parent station.

### PSIP / VCT fetch

After `w_scan` completes, `fetch_vct.py` reads the ATSC PSIP Virtual Channel Table for each found frequency. It tunes with VLC (to lock the frontend) then opens a separate fd on `/dev/dvb/adapter0/demux0` and installs a DVB section filter for PID `0x1FFB` (PSIP base PID), table `0xC8` (Terrestrial VCT). Section filters use the demux device directly — a different kernel codepath from the broken DVR device — so they work even though DVR doesn't. The parsed major.minor numbers (e.g., WRC-TV = 4.1, Fox = 5.1) are written to `vct_data.json` and picked up by `parse_channels.py`.

### Rescan page

The `/api/rescan/stream` endpoint is a Server-Sent Events stream that runs `w_scan` followed by `fetch_vct.py`. The UI shows a pre-built grid of all 35 RF channels (2–36) split into three band sections (VHF Low, VHF High, UHF). Each cell updates live as the scan progresses: pulsing amber while scanning, green when channels are found (with names listed inside), red for no signal. The `frequency` and `lock` SSE events carry an `rf_channel` field used to directly address grid cells; the `channel` event uses center-frequency tolerance matching (±4 MHz).

### ATSC frequency table

The server pre-computes center frequencies for RF channels 2–36 (the entire post-repack ATSC band):
- VHF-Lo (ch 2–6): 57–87 MHz
- VHF-Hi (ch 7–13): 177–213 MHz
- UHF (ch 14–36): 473–605 MHz

---

## Bugs Found and Fixed

### 1. `checkStatus()` used wrong field names ✓ fixed

`static/index.html` — `checkStatus()` read `data.tuned`, `data.channel_id`, `data.channel_name`, none of which exist in the `/api/status` response. Fixed to `data.streaming`, `data.channel.id`, `data.channel.name`. Refreshing the page now correctly reattaches playback if a stream is active.

### 2. Race condition: `/api/tune` during rescan ✓ fixed

`server.py` — The rescan handler released its lock before running `w_scan`, so a concurrent tune request could start VLC while the scanner was using the DVB device. Fixed with a `scanning` boolean flag that the tune handler checks and rejects (returns 409) for the full duration of the scan including the VCT fetch phase.

### 3. `parse_channels.py` spawned as subprocess ✓ fixed

`server.py` — Was calling `subprocess.run(["python3", ...])` which could use the wrong interpreter and silently swallowed errors. Replaced with a direct `import parse_channels; parse_channels.main()` call.

### 4. `scan_channels.sh` merged stdout and stderr ✓ fixed

`scan_channels.sh` — `2>&1` mixed w_scan's status output into the channel data file. Fixed to `2>/dev/null`.

### 5. VHF-Lo ch 5–6 wrong frequency ✓ fixed

`server.py` and `parse_channels.py` — VHF-Lo has a 4 MHz gap at 72–76 MHz (ch 4 ends at 72, ch 5 starts at 76). The original code computed ch 5 as 72 MHz. Fixed by splitting the loop: ch 2–4 from 54 MHz base, ch 5–6 from 76 MHz base.

### 6. VLC `canvas` filter cropped video ✓ fixed

`server.py` — The quality scaling used `vfilter=canvas{...}` which is a padding/cropping filter, not a scaler. This caused video to show only the top-left portion. Fixed by removing `vfilter` entirely and keeping only `height=N` in the transcode options.

### 7. Rescan UI cells never updated ✓ fixed

`static/rescan.html` — The grid cells were keyed by lower-edge frequency (from the ATSC table) but SSE events carry w_scan's center frequency. Nothing ever matched. Fixed: cells are now keyed by RF channel number; `frequency`/`lock` events use `rf_channel` directly; `channel` events use ±4 MHz tolerance matching against the lower-edge table.

---

## Open Minor Issues

### `log_message` drops status code

**`server.py`** — The log override prints `args[0]` (request line) but drops `args[1]` (status code). Low impact.

### Thread-safety of status reads

**`server.py`** — `/api/status` reads `current_channel` and `current_quality` without holding the lock. Safe in CPython due to the GIL, but technically a data race.

### 5-second fixed buffer delay

**`static/index.html`** — The client always waits 5 seconds after tuning before loading the playlist, regardless of how quickly VLC produces the first segment. Could poll for `live.m3u8` instead.

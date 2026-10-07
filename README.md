# TV Thing

A personal LAN server that streams live over-the-air ATSC broadcast TV and the card's analog NTSC tuner to any browser on your home network.

The Hauppauge HVR-1600 carries **two independent RF front-ends** -- a Samsung S5H1409 ATSC demodulator and a separate analog tuner feeding the CX23418 hardware encoder. They tune independently, so TV Thing streams **one digital and one analog channel at the same time**, each on its own HLS playlist. FM shares the analog front-end, so it displaces analog TV and nothing else.

---

## What it does

TV Thing scans the ATSC broadcast frequencies, builds a channel list from the PSIP virtual channel table, and streams any channel on demand as HLS (HTTP Live Streaming). You pick a channel in the web UI or Roku app; the server tunes the card and serves a low-latency HLS feed over the LAN.

### Lanes

Each source is a **lane** with its own tuner, encoder, and playlist:

| Lane | Front-end | Playlist | Displaces |
|---|---|---|---|
| `digital` | S5H1409 ATSC demodulator (`/dev/dvb/adapter0`) | `/stream/digital.m3u8` | nothing |
| `analog` | Analog tuner + CX23418 encoder (`/dev/video*`) | `/stream/analog.m3u8` | `fm` |
| `fm` | Same analog tuner, radio demod mode (`/dev/radio0`) | `/stream/radio.m3u8` | `analog` |
| `roku` | Source Roku via Hauppauge HD PVR (`roku.json`) | `/stream/roku.m3u8` | nothing |

Because `digital` and `analog` sit on different front-ends, tuning one never
disturbs the other. `/stream/live.m3u8` remains as an alias for the most
recently tuned video lane, so older clients keep working.

The main player presents **Digital** and **Analog** as equal sources. Analog
exposes the NTSC tuner as a frequency-ordered list of all US cable STD channel
numbers 1–135; selecting a number maps it directly to its video-carrier
frequency, with no cable scan.

**The Roku lane** (since 2026-10-06, moved from vizelVision) is the source Roku's
HDMI through a component converter into a Hauppauge HD PVR. One ffmpeg reads the
HD PVR and copies its own H.264 and AAC, without transcoding, to the lane's HLS
and to a loopback tap. `roku_lane.py` puts YouTube TV channels, baseball games,
Thursday Night Football or Sunday Ticket games on the Roku by navigating it with
`roku_control/` (ECP keys, screen recognition on frames decoded from the tap only
while needed, OCR and calibrated logo templates in `roku_state/`), and verifies
each selection on screen. **Manual Roku** hands it to an on-screen remote on the
Watch page's Roku tab. The Roku has its own lock (`lane: "roku"`), the VCR records
it alongside the digital tuner, and `roku.html` shows its capture, signal and
calibration. It is not a video lane for the legacy `live.m3u8` alias. See
`API.md`, "The Roku lane".

TV Thing drives the tuner card and that one Roku capture. Generic V4L2 capture hardware -- USB
grabbers, webcams, composite/S-video dongles -- is deliberately **not**
supported: tuner discovery keeps only nodes that expose a V4L2 *tuner* input,
which excludes capture cards by construction rather than by name matching.

Every retune replaces both the tiny tuner-side source and FFmpeg. This gives each broadcaster a fresh timestamp domain; carrying one CFR encoder across unrelated MPEG clocks can create a catastrophic duplicate-frame backlog. Generation-specific segment names let the browser reload the new feed without stale cache entries. Playback is always-on: the server remembers the most recent channel and quality, resumes them at boot or after a scan, and restarts failed media processes. The public stop endpoint is intentionally a no-op.

---

## Hardware

- **Tuner card:** Hauppauge HVR-1600 (PCIe), using the `cx18` kernel driver
- **Works with:** any Linux machine with a supported DVB/ATSC tuner card
- **Coverage:** DC metro area — currently 69 ATSC services across 12 RF frequencies

The installed card has two television inputs and no FM input. The FM implementation remains in the repository for a future compatible receiver, but its web controls are disabled with `FM_UI_ENABLED = false`.

---

## How it works

```
Antenna → DVB frontend → minimal DVB source → localhost UDP
                                              ↓
              Browser / Roku ← HLS ← tune-scoped FFmpeg packager
```

1. **Channel scan** — `w_scan` finds active RF frequencies. `fetch_vct.py` then tunes each active frequency and reads its PSIP Virtual Channel Table via a DVB section filter. The scan page displays each station name and virtual channel number (for example, 4.1 or 7.2) as soon as that identification tune completes.
2. **TV tuning** — `dvb_source.py` holds the Linux DVB frontend, configures an all-PID `DMX_OUT_TS_TAP`, selects the requested program, remaps it onto stable internal video/audio PIDs with a compact PAT/PMT, and emits seven-packet (1,316-byte) datagrams to localhost UDP. This bypasses VLC's startup and termination overhead.
3. **Bounded encoding** — a fresh `ffmpeg` process receives that UDP transport stream for each tune, deinterlaces interlaced broadcasts, encodes H.264/AAC, and writes 600 ms HLS segments with generation-specific names. Native station timing is passed through; CFR gap filling is forbidden. The server terminates a packager on duplicated-frame warnings, UDP overruns, oversized/stalled segments, or an NVIDIA temperature limit. It probes NVENC, Quick Sync, VA-API, and VideoToolbox in that order and falls back to low-latency `libx264` when a hardware backend fails safely.
4. **Serving** — a Python HTTP server (`server.py`) serves the web UI, HLS playlist and segments, and a small JSON API.
5. **Optional FM backend** — on compatible hardware, `fm_tuner.py` opens `/dev/radio0` and tunes by V4L2 ioctl while `ffmpeg` captures the card's ALSA audio into audio-only HLS. This path is retained but not exposed on `viviserv`. FM holds the analog front-end, so it stops the analog lane and leaves the digital lane running.

---

## Components

| File / Directory | Purpose |
|---|---|
| `server.py` | Main HTTP server, streaming controller, tuner arbitration |
| `analog_tuner.py` | Tuner-only V4L2 discovery, fixed US cable channel plan, direct analog tuner control |
| `atsc_scan.py` | Native ATSC band scan, single-channel DX probe, PSIP/PSI parsing, signal statistics |
| `epg.py` | Broadcast guide: PSIP MGT/EIT/ETT/STT parsing, scheduled schedule refresh |
| `recorder.py` | VCR: timer schedule, guide-name matching, library file naming, recording/remux commands |
| `dvb_source.py` | Minimal direct DVB demux-to-UDP source used for TV |
| `fm_tuner.py` | FM radio: V4L2 radio-node ioctls, signal strength, band sweep |
| `tuner.py` | Low-level DVB frontend tuning via ioctl (diagnostic use) |
| `fetch_vct.py` | Reads PSIP VCT from DVB section filter to get virtual channel numbers |
| `parse_channels.py` | Parses `w_scan` output into `channels.json` |
| `scan_channels.sh` | Runs `w_scan` to detect active ATSC frequencies |
| `channels.json` | Channel database: name, virtual ch #, RF ch #, frequency, SID |
| `epg.json` | Stored programme guide, rebuilt on a schedule from the broadcast |
| `fm_stations.json` | FM station presets, updated by the FM band sweep |
| `static/` | Web UI: `app.css` design system, player, scan page |
| `roku_app/` | Roku sideload app (BrightScript / SceneGraph) |
| `tv-thing.service` | systemd unit to run the server at boot |

---

## Web UI

Browse to `http://<server-ip>:8080` on any device on your LAN.

- One player and source panel for Digital and Analog; the dormant FM markup and backend remain available for compatible hardware
- Digital and analog stream concurrently; switching tabs does not retune the other lane
- Frequency-ordered analog list with all channels 1–135 and no channel scan
- Channel list sorted by virtual channel number (primaries first, then subchannels)
- Shows RF channel alongside virtual channel number
- Quality selector: **Low** (480p / 1 Mbps), **Medium** (720p / 2.5 Mbps), **High** (native / 4.5 Mbps)
- Filter box (`/` to focus) over channels
- Live HLS playback via [hls.js](https://github.com/video-dev/hls.js) — bundled locally, no internet required
- Scan page (`/rescan.html`) shows RF progress, then labels each locked RF channel with its PSIP virtual channels and station names as they are identified
- Guide page (`/guide.html`) is a scrolling time grid of the broadcast schedule: click a programme for details, click a channel to watch it
- VCR page (`/vcr.html`) records digital channels or the Roku to the DLNA media library, programmed by source, channel and time or by guide name (see [Recording](#recording-the-vcr))

---

## Roku App

A native Roku app is included in `roku_app/`. Sideload it onto any Roku on the same LAN.

- Channel list mirrors the web UI
- D-pad navigation; OK to tune, Back toggles the channel overlay
- HLS played natively by the Roku media player

See [`roku_app/README.md`](roku_app/README.md) for setup and sideloading instructions.

---

## Setup

### Dependencies

```bash
# Debian/Ubuntu
sudo apt install w-scan dvb-apps ffmpeg python3
```

The service account must be able to open the applicable `/dev/video*` node and,
for capture audio, its ALSA PCM device. On typical Debian/Ubuntu systems that
means membership in the `video` and `audio` groups. Inaccessible devices remain
visible in the source list with an error instead of disappearing silently.

### Enable the FM tuner

The `cx18` driver normally autodetects radio support from the card EEPROM
(`radio=-1`). A missing `/dev/radio0` usually means that the exact board does
not advertise a connected FM audio path. Do not force `radio=1` merely to make
the node appear: unsupported variants can accept tuning commands and emit
valid-looking signal/stereo metadata while capturing only digital silence.

Only for a card independently known to include FM hardware, override
autodetection as follows:

```bash
# Stop TV playback and unload the dependent ALSA module before reloading cx18.
sudo systemctl stop tv-thing
sudo modprobe -r cx18_alsa cx18
sudo modprobe cx18 radio=1
sudo modprobe cx18_alsa
sudo systemctl start tv-thing

# Make radio=1 apply after the next boot or module reload.
echo "options cx18 radio=1" | sudo tee /etc/modprobe.d/cx18.conf
```

Verify the radio and capture devices before testing the app:

```bash
cat /sys/module/cx18/parameters/radio   # should print 1
ls -l /dev/radio0
arecord -l                              # should list CX18-0 / CX23418 PCM
```

#### `viviserv` hardware result

The installed HVR-1600 does not advertise radio support in its EEPROM. With
`radio=1` forced, its tuner reports the same 50% signal and stereo flag at
every tested frequency from 65.0 through 107.9 MHz. Both independent capture
paths—the CX18 ALSA PCM stream and its hardware MPEG encoder—produce the same
near-digital-silence level (mean `-90.3 dB`, maximum `-78.3 dB`). The web/API
implementation is functional, but this board cannot supply usable FM audio.
Real FM playback therefore requires a receiver with a Linux-supported audio
path (or an external receiver feeding a capture input).

### Run the server

```bash
python3 server.py
# Listening on 0.0.0.0:8080
```

Set `TV_THING_BIND` when another service owns port 8080 on a different local
interface. On `viviserv`, Tailscale Serve owns the Tailscale address, so the
systemd unit binds TV Thing to its LAN address instead:

```bash
TV_THING_BIND=192.168.86.230 python3 server.py
```

Encoding defaults to `cpu`, the configuration that passed the sustained
resource gate on `viviserv`. Hardware selection must be opted into after a
machine-specific thermal/resource gate; an unavailable or failing hardware
encoder still falls back to CPU:

```bash
TV_THING_VIDEO_ENCODER=nvenc python3 server.py
TV_THING_VIDEO_ENCODER=qsv python3 server.py
TV_THING_VIDEO_ENCODER=vaapi TV_THING_VAAPI_DEVICE=/dev/dri/renderD128 python3 server.py
TV_THING_VIDEO_ENCODER=cpu python3 server.py
```

### Versioned local API

See [the API contract and locking flow](API.md) for generation-aware playback,
lease renewal, retries, and application-relative stream URLs. The core OpenAPI
contract is served at `/api/v1/openapi.json`.


Other services can use the same source model as the browser through `/api/v1`.
On `viviserv`, use its LAN-bound address even from another process on the same
machine:

```bash
export TV_THING_URL=http://127.0.0.1:8080

# Discover the whole surface: lanes, endpoints, quality presets.
curl -sS "$TV_THING_URL/api/v1"

# What exists, and what each lane is doing right now.
curl -sS "$TV_THING_URL/api/v1/sources"
curl -sS "$TV_THING_URL/api/v1/status"
curl -sS "$TV_THING_URL/api/v1/streams"
curl -sS "$TV_THING_URL/api/v1/health"

# Selectable entries per source.
curl -sS "$TV_THING_URL/api/v1/sources/digital"
curl -sS "$TV_THING_URL/api/v1/sources/cable"
curl -sS "$TV_THING_URL/api/v1/sources/fm"

# Tune each lane. Digital and analog are independent, so these two run
# concurrently -- the second does not disturb the first.
curl -sS -X POST "$TV_THING_URL/api/v1/tune" \
  -H 'Content-Type: application/json' \
  -d '{"source":"digital","channel_id":"whut_7_1","quality":"medium"}'
curl -sS -X POST "$TV_THING_URL/api/v1/tune" \
  -H 'Content-Type: application/json' \
  -d '{"source":"cable","channel":69,"quality":"low"}'
curl -sS -X POST "$TV_THING_URL/api/v1/tune" \
  -H 'Content-Type: application/json' \
  -d '{"source":"fm","frequency":88.5}'

# Stop one lane without touching the others (or omit "lane" for all).
curl -sS -X POST "$TV_THING_URL/api/v1/stop" \
  -H 'Content-Type: application/json' -d '{"lane":"analog"}'
```

`GET /api/v1/status` is the main document. It reports every lane under
`lanes`, the currently running ones under `active_lanes`, whether more than
one is running under `concurrent`, and which RF front-end each lane holds
under `frontends`. Each lane carries its own `stream.playlist_path`,
`stream.ready`, and `stream.health`, so a client can consume the digital and
analog feeds at the same time.

A successful tune returns the `lane` it acted on plus normalized `source`,
`selection`, `quality`, `stream`, and `encoder` objects. Wait until that
lane's `stream.ready` is `true`, then consume its `stream.playlist_path`.
Within one lane the stream is shared: a tune from any client replaces that
lane's selection for every viewer. The API has no authentication and is
intended only for a trusted LAN. CORS and `OPTIONS` are enabled for
browser-based local services.

The original `/api/channels`, `/api/sources`, `/api/status`, `/api/tune`, and
`/api/cable/*` routes remain available for existing clients, including the
Roku app, and `/stream/live.m3u8` still resolves to the most recently tuned
video lane. `/api/v1/sources/capture` and `/api/capture/tune` were removed
along with capture-card support.

### Reverse proxy

Apache publishes TV Thing at `http://viviserv/tv/`. The server always binds
`127.0.0.1` in addition to its LAN address, so the proxy targets loopback and
is unaffected by DHCP changes or a move between interfaces:

```apache
RedirectMatch 301 ^/tv$ /tv/
ProxyPass        /tv/  http://127.0.0.1:8080/  flushpackets=on timeout=3600
ProxyPassReverse /tv/  http://127.0.0.1:8080/
```

`flushpackets=on` matters for the rescan SSE stream and for HLS segment
delivery; the long timeout keeps a scan from being cut off. The trailing
slash is required because TV Thing's pages use relative URLs.

The route lives in
`~/viviserv-dashboard/deployment/apache-proxy-routes/tv.conf` and is rendered
into `/etc/apache2/conf-available/viviserv-dashboard.conf` by
`install-apache-proxies.sh`; edit the fragment, not the generated file.

### Run at boot (systemd)

```bash
sudo cp tv-thing.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tv-thing
```

The unit applies independent containment (`CPUQuota=400%`, `MemoryMax=1200M`,
`LimitFSIZE=16M`) and sets the `viviserv` NVIDIA ceiling to 80 C. Application
watchdogs are still required; the systemd limits are a final safety net. The
service pins `libx264` on `viviserv`: its legacy K4200 reached the 80 C safety
ceiling during the sustained NVENC gate. GPU auto-selection can be restored
after the card's cooling is serviced and a full NVENC gate passes.

### Scan for channels

TV Thing scans the ATSC band itself; `w_scan` is no longer used. The sweep
tunes the DVB frontend directly, and on each locked frequency reads the PSIP
Terrestrial VCT (station names and virtual channel numbers) and the PAT/PMT
(elementary PIDs) in the same visit.

Two things make that materially faster than the previous `w_scan` pipeline:

* **RF 2-36 by default.** `w_scan`'s US ATSC table still sweeps to RF 69, but
  the 2017 incentive auction and the repack that finished in 2020 cleared
  everything above RF 36. Half of every old scan was spent locking against
  spectrum that is now cellular.
* **One visit per frequency.** The old flow ran `w_scan` for PIDs and then
  re-tuned every hit with `fetch_vct.py` for PSIP names. Both tables are read
  while the frequency is already locked.
* **Concurrent PMT filters.** A multiplex carrying eight services needed
  eight serial section reads at about 1.2 s each. The demux supports many
  simultaneous filters, so they now cost one PMT cycle in total -- which took
  a locked frequency from roughly 12 s to under 4 s.

Scan the whole band from the Scan page, or drive it directly:

```bash
# Defaults to RF 2-36.
curl -N "$TV_THING_URL/api/rescan/stream"

# Narrow the sweep, or include the repacked channels.
curl -N "$TV_THING_URL/api/rescan/stream?start=14&end=36"
curl -N "$TV_THING_URL/api/rescan/stream?include_repacked=1"
```

### DX: one channel at a time

The DX page (`/dx.html`) probes a single RF channel and reports what the
demodulator sees, holding the digital tuner only while it measures. An analog
stream keeps playing throughout, because it uses the other front-end.

```bash
# Probe RF 35, watching it for five seconds.
curl -sS -X POST "$TV_THING_URL/api/v1/scan/channel" \
  -H 'Content-Type: application/json' \
  -d '{"rf_channel":35,"dwell":5,"lock_timeout":2}'

# The RF channel plan, with the stations already known on each.
curl -sS "$TV_THING_URL/api/v1/scan/plan?include_repacked=1"

# Live meter for whatever is tuned. Read-only, so it never interrupts a stream.
curl -sS "$TV_THING_URL/api/v1/signal"
```

A probe reports lock state, signal strength, SNR in dB, BER, and uncorrected
block counts. With `dwell` set it samples repeatedly and adds `lock_ratio`
and the SNR spread (`snr_db_range`), which is what separates a solid local
signal from a fading catch: a station can show a healthy mean SNR while
holding lock only 70% of the time.

The S5H1409 reports 8VSB SNR in tenths of a dB. Demodulation needs roughly
15 dB, so the useful range is about 15-35 dB.

### Scanning from the command line

`atsc_scan.py` is the scanner the server uses, and runs standalone:

```bash
python3 atsc_scan.py                          # sweep RF 2-36
python3 atsc_scan.py --start 14 --end 36      # UHF only
python3 atsc_scan.py --end 69                 # include the repacked channels
python3 atsc_scan.py --channel 35 --dwell 10  # watch one channel (DX)
python3 atsc_scan.py --measure                # read the live signal, no tuning
```

The older `w_scan` pipeline is still present for comparison, but nothing
calls it:

```bash
bash scan_channels.sh          # runs w_scan, saves w_scan_output.txt
python3 fetch_vct.py           # reads PSIP VCT for virtual channel numbers
python3 parse_channels.py      # generates channels.json
```

### Scan for FM stations

The FM scan UI is hidden on `viviserv`. On compatible hardware, sweep from the command line or re-enable `FM_UI_ENABLED` in the two static pages:

```bash
python3 fm_tuner.py scan       # prints signal strength per channel
python3 fm_tuner.py 88.5       # tune and hold, showing live signal
```

---

## Tests

```bash
pytest test_server.py -q                     # streaming command/GPU unit tests
pytest test_epg.py -q                        # PSIP guide parsers, no hardware
pytest test_fm.py -q -m "not api"          # FM unit tests, no server/hardware
pytest -q -m "not slow and not hardware"   # fast suite against the running server
TV_THING_URL=http://localhost:8099 pytest test_fm.py -q -m api  # target a dev instance
TV_THING_URL=http://localhost:8080 pytest test_fm.py -q -m hardware  # includes non-silence check
```

The request-to-stream latency and glitchiness checks need the running ATSC
tuner. The latency test measures from `POST /api/tune` until a new segment is
downloadable. The glitch test verifies that every retune gets a fresh stream
generation, then checks segment availability/duration, MPEG-TS sync, transport
error flags, and continuity counters:

```bash
TV_THING_URL=http://localhost:8080 pytest test_integration.py -q -m hardware
```

The default limits are 8 seconds request-to-stream, 3 seconds maximum segment
duration, and 6 inspected post-retune segments. They can be adjusted for a
particularly slow tuner with `TV_THING_MAX_CHANNEL_CHANGE_SECONDS`,
`TV_THING_MAX_SEGMENT_DURATION`, and `TV_THING_GLITCH_TEST_SEGMENTS`.

### Programme guide

The schedule is not fetched from the internet — every ATSC station transmits
its own, and `epg.py` reads it off the air with DVB section filters.

**How much guide there is.** The Event Information Table is sent in three-hour
blocks. A/65 requires stations to send EIT-0 through EIT-3, so **twelve hours
is the guarantee**; a station may send up to EIT-127 (sixteen days). Measured
in DC, most multiplexes send exactly the mandatory four, but not all:

| RF | Station | Blocks advertised | Guide available |
|---|---|---|---|
| 31 | WETA | 12 | 36 h |
| 34 | WRC | 8 | 24 h |
| 35 | ION | 8 | 24 h |
| 7, 9, 15, 20, 21, 24, 36 | — | 4 | 12 h |
| 14, 32 | shopping/religious | 0 | none at all |

RF 14 and RF 32 lock cleanly and their VCTs read fine, but their MGT defines a
single table (the VCT). Those stations transmit no schedule; ten of the
seventy channels have no guide for that reason and nothing can be done about
it from this end.

The default reads four blocks everywhere, which is why the stored guide
reaches about twelve hours. `TV_THING_EPG_BLOCKS=12` picks up WETA's full day
and a half — at a real cost, below.

That is what sets the refresh schedule. A single overnight pass would run out
around midday and leave the evening — the part anyone actually looks up —
uncovered, so the default is **03:00 and 15:00**: two passes twelve hours
apart, each at a time nobody is watching.

Reading the guide needs the digital front-end one frequency at a time, so a
refresh interrupts digital TV exactly like a band scan does. It takes roughly
a second per frequency. Three things keep that from being disruptive:

- an analog stream uses the other front-end and plays straight through;
- tuning a channel, or starting a scan, cuts the refresh short at the next
  frequency and takes the tuner back — nobody waits out a sweep;
- a refresh that is interrupted keeps what it read, and channels it did not
  reach keep their previous entries until those run out.

```bash
# Read the guide now and store it.
python3 epg.py

# Print one channel's schedule from the stored guide, without tuning.
python3 epg.py --show 7.1

# Go deeper than the mandatory twelve hours, if the station sends more.
python3 epg.py --blocks 8
```

| Variable | Default | Meaning |
|---|---|---|
| `TV_THING_EPG` | `1` | Set to `0` to disable scheduled refreshes entirely |
| `TV_THING_EPG_TIMES` | `03:00,15:00` | Local wall-clock refresh times |
| `TV_THING_EPG_BLOCKS` | `4` | EIT blocks to read per frequency, 3 hours each |
| `TV_THING_EPG_DESCRIPTIONS` | `0` | Also read ETT programme descriptions |
| `TV_THING_EPG_STARTUP_DELAY` | `120` | Seconds to wait before a stale-guide catch-up at boot |
| `TV_THING_EPG_MIN_COVERAGE_HOURS` | `3` | Hours of guide still ahead below which that catch-up runs |

**Blocks past the fourth are slow.** A/65 asks for EIT-0..3 twice a second but
only once a minute for everything after, so the collector gives the mandatory
blocks a few seconds each and the rest a full minute (`SLOW_BLOCK_TIMEOUT`).
Raising `TV_THING_EPG_BLOCKS` is therefore not free: a full sweep costs about
three minutes at the default, and roughly a minute per extra block on each
frequency that carries one. Reading WETA's twelve would add around eight
minutes by itself.

**Descriptions are expensive too.** Programme synopses ride in a separate
table (ETT) on the same once-a-minute budget. Measured on RF 7, one EIT block
arrives in about a second while one ETT block takes over a minute. They are
off by default; the Guide page has a checkbox to request them for a manual
refresh.

Titles may also be Huffman-coded against the tables in A/65 Annex C, which are
not implemented. Such strings are counted and reported rather than silently
blanked — the DC stations measured send none.

```bash
# The stored guide, past programmes dropped.
curl -s localhost:8080/api/v1/guide | jq '.coverage_hours, .event_count'

# One channel, or a shorter window.
curl -s 'localhost:8080/api/v1/guide?channel=7.1&hours=6' | jq

# What is on right now, per channel.
curl -s localhost:8080/api/v1/guide/now | jq

# Refresh now (server-sent events, takes the digital tuner).
curl -sN localhost:8080/api/guide/refresh/stream
```

---

### Recording (the VCR)

`/vcr.html` is a shelf of recorders above a TV with an on-screen programming
menu. Each input is its own recorder with its own display, REC and STOP: the
digital tuner is a VCR, the Roku an old Scientific Atlanta Explorer cable box
whose arrow pad steps the timer menu's channel and day (a lane with no look of
its own is another VCR). The TV shows one of them, the SOURCE; a recorder's PROG
key or a click on it switches the TV to it. The menu sets timers three ways:

| Mode | How | Window |
|---|---|---|
| **CH/TIME** | source, channel, date, ON and OFF, like a deck's timer menu | exactly as entered; OFF before ON runs past midnight |
| **GUIDE NAME** | search the broadcast guide, then **ONCE** or **EVERY** | the airing, 1 min early and 2 min late |
| **REC** (One Touch) | what the chosen source shows now | digital: to the end of the current programme, or 30 min; Roku: 30 min |

**SOURCE** picks the input: the digital tuner, or the Roku. Its CH list is the
broadcast channels, or the Roku's YouTube TV channels, the DATE's games
(baseball, Thursday Night Football, Sunday Ticket) and Manual Roku (whatever
is on). Choosing a game sets ON a few minutes before the start, since the Roku
takes up to 3.5 minutes to get there, and OFF half an hour past its usual
length. The menu is built from `inputs` in `GET api/v1/recordings`, so a lane
that learns to record (another tuner, a capture card) is listed there and
needs no page change. Digital and Roku timers run side by side; see
"The Roku lane" in `API.md` for how a Roku timer navigates and shares.

**EVERY** stores a rule (title, pinned to that channel) and sets a timer for
each matching airing as guides arrive. The guide page's programme detail has
a **Record** button too.

**How it records.** The card has one ATSC front-end, so a recording *is* the
digital lane: it takes a tuner lock (owner `VCR`), tunes the lane, and
`dvb_source.py` sends a second copy of its transport stream to a tap port read
by a dedicated ffmpeg. Live viewers keep watching what is being taped. Any
two overlapping timers conflict, whatever their channels, and are refused
at scheduling time. A timer that finds someone else's lock waits and retries
rather than overriding it; overriding the VCR's lock ends the recording as
`partial`.

**Encoding** is x264 `veryfast` at CRF 23 (capped at 4 Mbit/s), deinterlaced
and capped at 720p, with AAC stereo at 160 kbit/s, on four threads. On
`viviserv` that is about one core and 220 MB, and roughly 1-2 GB per hour
against 7 GB for the raw broadcast. The file is written as MPEG-TS first, so a
crash or power loss still leaves a playable file, then remuxed to MP4 with the
index at the front. The in-progress `.part` suffix keeps MiniDLNA from indexing
a half-written file; the final rename is what makes it appear.

**Failure handling.** A recorder that dies is restarted while the window is
open (up to five times), and a server restart resumes the recording, renewing
its own lock. Either way the recording carries on into another hidden piece
(`Title ... .2.ts.part`), and when the timer ends the pieces are joined into
one MP4 without re-encoding. Only if that join fails are the pieces filed as
separate `(2)` files. Recording stops below
1 GB free and refuses to start below 5 GB. Recorder processes die with the
server (`PR_SET_PDEATHSIG`) and are reaped at startup if orphaned.

**Where tapes live.** Recordings go to `/mnt/sda1/media/TV Recordings` on the
4 TB data drive, never the boot SSD. The VCR page's TAPES screen plays a tape
in the browser or saves it (`GET api/v1/recordings/file`), so neither needs
MiniDLNA; MiniDLNA also lists the folder for TVs.

**One-time setup.** The unit (file-size limit and recordings path) and
MiniDLNA's access to the data drive need root. `/mnt/sda1` is
`root:backup2 770`, so MiniDLNA is given traverse-only access with an ACL
rather than opening the drive to everyone:

```bash
sudo cp tv-thing.service /etc/systemd/system/   # LimitFSIZE=16M:16G, recordings dir
sudo systemctl daemon-reload
sudo setfacl -m u:minidlna:x /mnt/sda1 /mnt/sda1/media
echo 'media_dir=V,/mnt/sda1/media/TV Recordings' | sudo tee -a /etc/minidlna.conf
sudo systemctl restart minidlna
```

The unit keeps its 16 MB soft cap on every file; only recorder processes
raise their own soft limit, to `TV_THING_RECORD_MAX_GB`. Until both are done
the VCR page says which is missing and prints the command.

| Variable | Default | Meaning |
|---|---|---|
| `TV_THING_RECORDINGS_DIR` | `/mnt/sda1/media/TV Recordings` | Library folder (4 TB data drive; MiniDLNA serves it too) |
| `TV_THING_RECORD_PRESET` / `_CRF` | `veryfast` / `23` | x264 speed and quality |
| `TV_THING_RECORD_MAX_HEIGHT` | `720` | Downscale anything taller |
| `TV_THING_RECORD_MAXRATE_KBPS` | `4000` | Bitrate ceiling |
| `TV_THING_RECORD_THREADS` | `4` | x264 threads (memory vs. headroom) |
| `TV_THING_RECORD_PRE_PAD_SECONDS` / `_POST_` | `60` / `120` | Guide-timer padding |
| `TV_THING_RECORD_MIN_FREE_GB` / `_STOP_FREE_GB` | `5` / `1` | Disk-space guards |
| `TV_THING_RECORD_MAX_GB` | `16` | Largest single file |
| `TV_THING_RECORD_TAP_PORT` | `TV_UDP_PORT + 64` | First tap port |

A staging candidate must set its own `TV_THING_RECORDINGS_DIR`; it only ever
finalizes parts its own schedule names, but it would still write into the
real library.

```bash
curl -s localhost:8080/api/v1/recordings | jq '.active, .timers, .storage'
curl -s 'localhost:8080/api/v1/recordings/search?q=jeopardy' | jq '.results'
curl -s -X POST localhost:8080/api/v1/recordings -H 'Content-Type: application/json' \
  -d '{"mode":"guide","channel_number":"7.1","event_start":1790190000}'
```

---

### Tuner locks

A service that needs the tuner to stay where it is — a recorder, a set-top
part-way through a programme — can claim a front-end so nothing retunes it
away. The lock guards **changes only**: the always-on keeper still restarts the
locked channel if its encoder dies, because a lock whose stream cannot recover
is worse than no lock at all. That is why the lock is deliberately not part of
`busy_reason()`, which the keeper consults.

A lock blocks tuning, stopping the lane, a band scan, and the scheduled guide
refresh — a recording running at 03:00 keeps its tuner. Locks are per-lane, so
a digital lock leaves the analog tuner free.

A deferred guide refresh is **retried**, not dropped. Skipping to the next slot
would cost twelve hours, which is the guide's entire span, so it would run dry
by afternoon; instead it re-attempts every `TV_THING_EPG_LOCK_RETRY_SECONDS`
(default 300) until the lock clears, stopping once the next scheduled slot is
due. Only a lock is retried — an ordinary failure waits for the next slot
rather than spinning.

```bash
# Claim the digital tuner for half an hour. The token is the proof of
# ownership -- keep it, it is the only way to release early.
curl -s -X POST localhost:8080/api/v1/tuner/lock \
  -H 'Content-Type: application/json' \
  -d '{"lane":"digital","owner":"recorder","duration_seconds":1800,
       "reason":"taping the news"}'

# Or hold it until the programme now airing finishes, per the guide.
curl -s -X POST localhost:8080/api/v1/tuner/lock \
  -H 'Content-Type: application/json' \
  -d '{"lane":"digital","owner":"roku","until":"program_end",
       "channel_id":"wusa_hd_9_1"}'

# Renew by passing the token back; the lock keeps its identity.
# Release early -- only the holder's token is accepted.
curl -s -X POST localhost:8080/api/v1/tuner/lock/release \
  -H 'Content-Type: application/json' \
  -d '{"lane":"digital","token":"..."}'

# Who holds what.
curl -s localhost:8080/api/v1/tuner/lock
```

A blocked change returns **423 Locked** carrying the lock itself, so a client
can say who holds it and for how long rather than just failing. Releasing
without the right token is **403**.

Locks expire on their own and are capped, so a service that dies without
releasing cannot strand the tuner. They are persisted, so a server restart
does not silently hand the tuner back.

| Variable | Default | Meaning |
|---|---|---|
| `TV_THING_LOCK_MAX_SECONDS` | `21600` | Ceiling on any single lock (6 h) |
| `TV_THING_LOCK_DEFAULT_SECONDS` | `3600` | Used when no duration is given |
| `TV_THING_LOCK_PROGRAM_GRACE_SECONDS` | `120` | Added to a `program_end` lock, since broadcasts run over |
| `TV_THING_EPG_LOCK_RETRY_SECONDS` | `300` | How often a lock-deferred guide refresh re-attempts |

**Overriding.** The web UI can break someone else's lock, but only through a
warning dialog naming the holder, the reason given, and what is about to be
interrupted — the safe button holds focus, Escape and a backdrop click both
cancel, and the page never retries past a 423 on its own. Confirming re-sends
the request with `"override": true`; the server logs every override. The lock
pill in the header does the same thing for releasing a lock outright.

---

### Mandatory deployment gate

`AGENTS.md` defines the project workflow: production must remain stopped while
a candidate runs from a separate directory on a separate HTTP/UDP port and
streams directory. After the fast suite passes, run the bounded gate on
`viviserv` against at least three real retunes:

```bash
# $CANDIDATE_PID is the staged server's pid -- the gate drives that server,
# so it must already be running on the non-production port.
python3 stream_sanity_check.py \
  --url http://127.0.0.1:8099 \
  --server-pid "$CANDIDATE_PID" \
  --retunes 3 --dwell-seconds 10 \
  --max-gpu-temp 80
```

**On the CPU peak limit.** It is 350%, not 300%. Peak is sampled with `ps`
`%CPU`, which is cputime divided by elapsed time, so a freshly spawned
multi-threaded x264 reads around 300% for a fraction of a second without
being sustained load. Measured here: 153% steady state, 110% peak across a
single tune, and 200-300% only in the instant of a retune, against the unit's
own `CPUQuota=400%`. A real runaway still trips the check.

The gate fails on slow channel changes, malformed or oversized segments,
implausible encoded/write rates, CPU/RSS/GPU overuse, excess temperature,
encoder reuse, or any application safety trip. A failed candidate is stopped
and is never promoted. The same gate runs once more after production starts.

---

## cx18 driver quirks

**The demux start flag matters.** Keep `DMX_IMMEDIATE_START` at the Linux ABI value `4`. The older diagnostic used `1`, which means CRC checking and does not start a PES tap; that made `/dev/dvb/adapter0/dvr0` incorrectly appear broken. With the correct flag, cx18 returns aligned transport packets normally.

**The two front-ends really are independent.** The HVR-1600 is often assumed
to share one RF tuner between its analog and digital paths. It does not.
Holding a DVB lock while simultaneously streaming NTSC off the analog side
works, and both produce real, different pictures at once:

```bash
# Terminal 1 - lock ATSC and stream one program to UDP.
python3 dvb_source.py --adapter 0 --frequency 189000000 \
  --service-id 1 --video-pid 49 --audio-pid 52 \
  --udp-host 127.0.0.1 --udp-port 18777

# Terminal 2 - capture the analog encoder at the same time.
python3 -c "import analog_tuner as a; \
  a.configure_tuner('/dev/video0', 0, 0, a.cable_frequency_hz(3))"
dd if=/dev/video0 of=/tmp/analog.ts bs=64k count=200
```

The digital capture decodes as 1920x1080 MPEG-2 + AC-3 while the analog one
decodes as 720x480 MPEG-2 + MP2, with the DVB frontend still reporting
`FE_HAS_LOCK` throughout. This is what the `digital` and `analog` lanes rely
on. The i2c bus backs it up: `2-0043` and `2-0061` are two separate tuner
chips, alongside the S5H1409 demodulator.

**Closing the radio node drops FM mode.** Opening `/dev/radio0` is what switches the CX23418 A/V core into FM demodulation; the driver reverts to TV mode as soon as that descriptor closes. Setting a frequency with `v4l2-ctl` therefore accomplishes nothing — the tool exits, the node closes, and the card leaves FM mode before any audio is captured. `fm_tuner.py` holds the descriptor open for the life of the stream.

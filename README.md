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
calibration. `inputs.html` shows every input at once: each front-end and its lanes with
signal and encoder health, the Roku and its capture, the web streams made here and from
the web, and the services, disks and health behind them (`API.md`, "Inputs"). It is not a video lane for the legacy `live.m3u8` alias. See
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
4. **Serving** — in production, two services behind Apache's front door on `:8080`: the feed (`feed_server.py`) holds the hardware and every `ffmpeg` and serves the HLS under `/stream/`; control (`server.py`) serves the web UI and the JSON API, and can restart without dropping a stream. `python3 server.py` alone still runs both in one process. See [Run at boot](#run-at-boot-systemd-two-services) and `INVARIANTS.md`.
5. **Optional FM backend** — on compatible hardware, `fm_tuner.py` opens `/dev/radio0` and tunes by V4L2 ioctl while `ffmpeg` captures the card's ALSA audio into audio-only HLS. This path is retained but not exposed on `viviserv`. FM holds the analog front-end, so it stops the analog lane and leaves the digital lane running.

---

## Components

| File / Directory | Purpose |
|---|---|
| `server.py` | Main HTTP server, streaming controller, tuner arbitration |
| `channels.py` | The channel list: every channel as one `Channel`, one adapter per kind of source (antenna, cable, Roku, internet, the station, its slides) that the lineup, source catalog, guide and tune iterate |
| `networks.json` | Which station carries each broadcast network in the antenna's market (Washington) and the Roku's YouTube TV market (Lansing); read by `roku_control/networks.py` |
| `analog_tuner.py` | Tuner-only V4L2 discovery, fixed US cable channel plan, direct analog tuner control |
| `atsc_scan.py` | Native ATSC band scan, single-channel DX probe, PSIP/PSI parsing, signal statistics |
| `epg.py` | Broadcast guide: PSIP MGT/EIT/ETT/STT parsing, scheduled schedule refresh |
| `yttv_epg.py` | YouTube TV's guide, read from the web (Gracenote, FAST-app XMLTV); `yttv_listings.json` maps each row to its source |
| `recorder.py` | VCR: timer schedule, guide-name matching, library file naming, recording/remux commands |
| `dvb_source.py` | Minimal direct DVB demux-to-UDP source used for TV |
| `fm_tuner.py` | FM radio: V4L2 radio-node ioctls, signal strength, band sweep |
| `tuner.py` | Low-level DVB frontend tuning via ioctl (diagnostic use) |
| `fetch_vct.py` | Reads PSIP VCT from DVB section filter to get virtual channel numbers |
| `parse_channels.py` | Parses `w_scan` output into `channels.json` |
| `scan_channels.sh` | Runs `w_scan` to detect active ATSC frequencies |
| `channels.json` | Channel database: name, virtual ch #, RF ch #, frequency, SID |
| `epg.json` | Stored programme guide, rebuilt on a schedule from the broadcast |
| `yttv_epg.json` | Stored YouTube TV guide, two days ahead, rebuilt daily from the web |
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
- One channel lineup with **TV Thing channel numbers** across the antenna, the Roku and VizelVision (see [TV Thing channel numbers](#tv-thing-channel-numbers))
- **Antenna** tab: the antenna channels by their own virtual numbers (primaries first, then subchannels), with the RF channel
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

That is the one-process build: feed and control in one process, as before the
split, and what a rollback runs. Production runs `feed_server.py` and
`TV_THING_ROLE=control python3 server.py` as two services instead, each on
loopback only.

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

Apache owns `:8080`. The front door (`deploy/apache/tv-thing-front-door.conf`,
which includes `tv-thing-routes.conf`) listens on `127.0.0.1`, `192.168.1.98`
and `10.0.0.98`; sends `/stream/` to the feed on `127.0.0.1:8086` and
everything else to control on `127.0.0.1:8087`; never routes `/internal/` or
`/feed/v1/`; and answers `/api/` with a JSON 503 while control restarts.

The main site publishes TV Thing at `http://viviserv/tv/` by proxying to the
front door on loopback:

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

### Run at boot (systemd): three services

`scripts/deploy-feed-split.sh` installs the units in `deploy/systemd/` and the
front door, through the AGENTS.md gate (`RUNBOOK.md` is the same by hand). The
root `tv-thing.service` is the one-process unit, kept for `--rollback`; never
install it over the split.

| Service | Runs | Containment |
|---|---|---|
| `tv-thing-feed` | `feed_server.py` on `127.0.0.1:8086` | `CPUQuota=800%`, `MemoryMax=1800M`, `TasksMax=192` |
| `tv-thing` (control) | `server.py`, `TV_THING_ROLE=control`, on `127.0.0.1:8087` | `CPUQuota=400%`, `MemoryMax=1200M`, `TasksMax=160` |
| `tv-thing-web-basic` | `web_basic.py` on `127.0.0.1:8085`: the slide channels' video (the Sports Guide, Cairo Radio) and the simple web relays (pen2live) | `CPUQuota=200%`, `MemoryMax=768M`, `TasksMax=96` |

`tv-thing-web-basic` is installed by hand (`deploy/systemd/`, then `systemctl
enable --now`); Apache's front door sends it its streams' paths and the rest of
`/stream/` to the feed.

Which restart is safe:

- `sudo systemctl restart tv-thing` (control): no stream, recording or relay
  moves. The API answers a JSON 503 for a few seconds, and VizelVision rides
  out up to 45 s of that. A control that hangs instead of dying for longer
  than that makes VizelVision drop its input and recover (`INVARIANTS.md`, A4).
- `sudo systemctl restart tv-thing-feed`: every stream drops and every
  recording ends (the feed's journal says why). Only for core stream stack
  changes, under the gate.
- `sudo systemctl restart tv-thing-web-basic`: the slide channels' video and
  pen2live drop and start again at their next request; nothing else moves.
  For web streams stack changes, checked with `scripts/web_stream_check.py`.
- `sudo apachectl graceful`: no stream request fails. A full Apache restart
  drops every viewer.

Both units also cap file size (`LimitFSIZE=16M:16G`). Application watchdogs
are still required; the systemd limits are a final safety net. The feed pins
`libx264` on `viviserv` (`TV_THING_VIDEO_ENCODER=cpu`): its legacy K4200
reached the 80 C safety ceiling during the sustained NVENC gate. GPU
auto-selection can be restored after the card's cooling is serviced and a full
NVENC gate passes.

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

A rescan replaces `channels.json`. A channel it finds that the lineup does
not list is numbered automatically in 300–349 ("New on the antenna"); see
[TV Thing channel numbers](#tv-thing-channel-numbers).

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
pytest test_yttv_epg.py -q                   # YouTube TV listings parsers and map, no network
pytest test_fm.py -q -m "not api"          # FM unit tests, no server/hardware
pytest -q --ignore=test_integration.py -m "not slow and not api and not hardware and not resource"  # the fast suite
pytest test_integration.py -q              # retunes the running server: never against production
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

The antenna's schedule is not fetched from the internet — every ATSC station
transmits its own, and `epg.py` reads it off the air with DVB section filters.
YouTube TV's channels are the exception; see [YouTube TV's
listings](#youtube-tvs-listings) below.

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

#### YouTube TV's listings

The Roku's YouTube TV channels carry no schedule TV Thing can read, but theirs
are published on the web, free, and `yttv_epg.py` reads them. It runs daily at
04:30, and at startup when less than a day is left. It needs no tuner, so it
never interrupts anything:

- **Gracenote**, which supplies YouTube TV's own guide, serves two weeks for
  any station by its station id through the listings site Zap2it used to front.
  The networks are their national East feeds. The locals are the Lansing
  stations the Roku's YouTube TV market carries (WILX, WSYM, WLAJ, WLNS, WKAR,
  and WLAJ-DT2 for The CW). 92 rows read this way.
- **Free streaming channels** in YouTube TV's guide (Stories by AMC, AMC
  Thrillers, Portlandia, Pickleball TV, T2, Scripps News, Bloomberg Originals,
  All Reality, Discovery Turbo) are in no public Gracenote lineup. Their
  schedules come from the Plex, Roku and Samsung TV Plus apps' XMLTV, mirrored
  at i.mjh.nz. Stories by AMC, AMC Thrillers and Discovery Turbo run different
  schedules on different apps; if one disagrees with the Roku, point it at
  another feed. T2's listing only runs a couple of hours ahead.
- **Local Now** (a local loop) and **World at War** have no listing anywhere.
  Their rows are in the guide, empty, so they can still be tuned from it.

A read is about 100 requests a second apart, roughly 25 MB, and takes about
three minutes. Gracenote only answers for its full fortnight; two days are kept
(`yttv_epg.json`, about 2 MB). A row whose source fails keeps its last
listing, marked stale. A read where nothing answers is retried hourly.
`yttv_listings.json` maps every `roku.json` row to its source, or to `null`.
A test fails if a row is added to one and not the other.

#### Live Sports

The Live Sports games (61–99) are in the guide too, read from the leagues'
schedules (ESPN's scoreboard, MLB's Stats API) each time the guide loads, two
days ahead. A row is a number, not a channel: each day's games of every sport
are numbered together by start time, so 61 can be a hockey game tonight and a
football game on Sunday. An airing runs from its start to the projected final
and goes once the game is over. **Watch** puts one of today's games on the
Roku through the Watch page, as its own source would (Sunday Ticket, Thursday
Night Football, MLB, NHL). Record is not offered on these rows yet; the VCR
page tapes a game.

**Hockey is ESPN Unlimited's.** YouTube TV's main plan has carried ESPN
Unlimited since 2026-09-01, and with it the NHL's out-of-market games (NHL
Power Play, 1,050 a season, each with home and away streams) and its ESPN+
exclusives. ESPN's scoreboard marks both with a national "ESPN+" broadcast;
an exclusive also lists Disney+ and Hulu. Games on ESPN, ABC, TNT or NHL
Network are left out, since they are on those channels' rows. Out-of-market
games are blacked out locally, and YouTube TV places the Roku in DC or
Lansing, so Capitals and Red Wings games are marked "may be blacked out"
(exclusives never are).

**Sunday's football follows YouTube TV's market.** The Sunday-afternoon CBS
and FOX games are regional, and the Roku's YouTube TV carries Lansing's
stations (WLNS, WSYM), not Washington's. A game is suggested on YouTube TV's
CBS or FOX only when YouTube TV's own listings (`yttv_epg.json`) name it at
kickoff ("NFL Football", "Indianapolis Colts at Minnesota Vikings"), or, past
the listings' two days, when the Lions play. Every other game in those windows
is on Sunday Ticket, which blacks out only Lansing's games, and only those are
in its Live Sports catalog (`roku_control/sports.py`). A Commanders or Ravens
game is on CBS 9 or FOX 5 over the antenna either way.

The Roku finds a game on YouTube TV's ESPN page (`nhl` source,
`roku_control/espn_unlimited.py`). The games have no guide rows of their
own; the page, opened from the ESPN row's station tile, lists them under
**Live**, in one On Now row of cards labelled ESPN+, two per game: the away
stream, then the home (seen on 2026-10-19). A tune starts from Roku Home,
because while a stream plays behind it the page keeps the row it first
loaded. It walks the verified guide to the ESPN row and opens its page,
checked against the calibrated ESPN page logo. Then Down to the section menu
(Live focused), Right into On Now, and right again card by card. The focused
card is always the leftmost, under a white ring; OCR reads its caption until
it names both teams. It takes the home stream's card when there are two,
re-reads it, and Select plays it, verified as any YouTube TV tune (two
playing frames). A game not on the row (not started, or blacked out) fails
with "not on the ESPN page's On Now row".

The guide shows these rows by their TV Thing numbers among the antenna's.
**Watch** opens the Watch page, which sends the Roku there and shows its
progress. **Record** sets a Roku timer that starts early enough for the Roku
to reach the channel. The Watch page's list and banner show what is on them.
**Antenna numbers** hides them; that view is the antenna's own.

```bash
# Read YouTube TV's listings now and store them (the server picks them up).
python3 yttv_epg.py

# Check one row's source without saving anything.
python3 yttv_epg.py --only "Stories by AMC"

# Print one row's stored schedule.
python3 yttv_epg.py --show CNN
```

| Variable | Default | Meaning |
|---|---|---|
| `TV_THING_YTTV_GUIDE` | `1` | Set to `0` to stop reading YouTube TV's listings |
| `TV_THING_YTTV_GUIDE_TIMES` | `04:30` | Local wall-clock read times |
| `TV_THING_YTTV_GUIDE_MIN_COVERAGE_HOURS` | `24` | Hours still ahead below which a starting server reads at once |

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

**On the CPU limits.** Both CPU checks charge the server's process tree the
CPU time it used between samples, about a second apart, read from
`/proc/<pid>/stat`. A process's count includes the children it has reaped, and
a reaped encoder's last count is subtracted so that it is not charged twice.
Until 2026-10-17 the gate used `ps` `%CPU`, which is CPU time divided by age.
For a process only milliseconds old that is mostly rounding: just after a
retune, a one-thread ffmpeg 0.08 s old read 116% while the whole unit used
51%. Two deploys failed on "peaks" of 404% and 504% that the unit's cgroup
never came near. The limits did not change: 200% average and 350% peak. On
the 2.6 GHz Xeon the candidate's cgroup averaged 92% across three retunes and
used at most 159% in any one second. The unit's own quota is `CPUQuota=800%`.
A real runaway still trips the check. Unlike a lifetime average, it also does
so when a process that has run for hours suddenly spins.

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


### Unified tunable channels (Watch)

Watch opens with **All channels**: one searchable lineup, in TV Thing channel
number order under lineup-card headings, containing ATSC antenna channels, the
Roku’s YouTube TV channels and dated game feeds, Manual Roku, and
**VizelVision** (channels 0 and 70) with its Sports Guide (60, slides that play
on the Watch stage). Each row names its source; matching station
names on ATSC and Roku remain separate choices. Search accepts TV Thing and
antenna channel numbers, station names, Roku aliases, and source names. The
Antenna, Cable and Roku tabs remain available, including the Roku game date,
locks and Manual Roku remote.

Selecting VizelVision watches its existing station output in the same player.
It does not tune the Roku or ATSC receiver, take a source lock, change master
control, or launch an encoder. The quality selector is hidden for both Roku and
VizelVision. If the station is unavailable, select its channel again to retry.

TV Thing relays station HLS through `stream/vizelvision/index.m3u8` so playback
works on the direct TV Thing port and under `/tv/`. The upstream defaults to
`http://127.0.0.1:6970/v1/master/media/`; override
`TV_THING_VIZELVISION_MEDIA_URL` with an HLS directory URL if needed. The station
must supply relative, flat playlist/media references (as VizelVision does).
Playlist query strings, initialization segments and byte ranges are preserved.
Only GETs for HLS assets are relayed, with a 5-second upstream timeout and a
16 MiB response limit.

### Internet streams

Live streams from the web are channels too, listed in
[`internet_streams.json`](internet_streams.json) and played by
`internet_streams.py`. A stream has an `id`, a `name`, an optional `note` (shown
in the Watch list) and a resolver: `url` with a fixed HLS `url`, or
`yle-areena` with an Areena `program` id (the `1-…` in its areena.yle.fi
address). Areena's playlist URLs carry a CDN token good for about a day, so the
resolver asks Yle's player API again every 30 minutes and whenever the CDN
refuses one. TV Thing relays the playlists and segments at
`stream/internet/<id>/index.m3u8` (see `API.md`, "Internet streams"); no tuner,
lock or encoder is involved, and the Watch page plays them as it plays
VizelVision. Give a new stream a number in `lineup.json`, or it takes the first
free one in 370–379.

A stream can also publish listings: `guide` names a URL answering
`{"events": [{"start", "duration", "title", "description"}]}`, and the stream
gets a row in the Guide (and its NOW/NEXT banner on Watch), read at most once a
minute. Channel 1, **Trains** (the stream Cab Ride), uses it: hazel's `cabride.service`
(`~hazel/cabride`, `127.0.0.1:8791`) plays train cab rides laid end to end from
the wall clock, as if it never stopped, and runs its one stream-copy `ffmpeg`
only while someone watches. Its schedule follows the clock, so the Guide shows
the rides hours ahead.

### TV Thing channel numbers

Every channel has a TV Thing number, laid out like an old cable lineup card in
[`lineup.json`](lineup.json):

| Numbers | Section |
|---|---|
| 0–9 | Our channels: VizelVision on 0, Trains (Cab Ride) on 1, pen2live (Yle's puppy cam) on 2, VizelVision's Sports Guide on 6 (see [The Sports Guide](#the-sports-guide-channel-6)); 3–5 and 7–9 held for more |
| 10–24 | Washington & Baltimore: NHK World first on 10, then each network on the number ending in its channel on the air (14 NBC 4, 15 FOX 5, 17 ABC 7, 19 CBS 9), a station's second channel right after it, then ION, Univision, Telemundo, WDVM and Baltimore's CW |
| 25–39 | Michigan, in the Lansing and Flint dial's order: the Roku's Lansing locals from YouTube TV (26 CBS, 27 NBC, 30 PBS, 32 FOX, 34 ABC, 35 The CW); the rest held for a Michigan antenna receiver |
| 40–59 | Sports cable from YouTube TV: TBS, TNT, the ESPNs and the rest in their old order, then USA and truTV |
| 60–69 | News and more: CNN, MS NOW, FOX News, CNBC, the Weather Channel, Discovery, HGTV, Smithsonian, FX, Comedy Central |
| 70 | VizelVision again |
| 71–99 | Live Sports: the day's games, football, baseball and hockey together by start time (NFL Sunday Ticket, Thursday Night Football, MLB, and the NHL on ESPN Unlimited) |
| 100–109 | Roku (Manual Roku on 109) |
| 110–289 | The digital tier, by genre, YouTube TV's other channels and the antenna's subchannels together: news and business, learning and lifestyle, kids, entertainment, music (Cairo Radio on 197), classic TV, movies and westerns, crime and reality, game shows, Spanish, shopping, faith |
| 290–299 | Repeaters and other markets; 292–297 held for YouTube TV's Lansing locals once a Michigan antenna takes their numbers |
| 300–349 | New on the antenna |
| 350–369 | New on YouTube TV |
| 370–379 | New internet streams |
| 380–399 | Held for a Michigan receiver's subchannels |
| 400 up | Overflow |

A few channels are off the dial (`hidden` in `lineup.json`): TLC, All Reality We
TV, Busted, and the antenna's second copies of HSN2 and Shop LC. They have no
number and are in no list, but still tune by their id, and their rows stay in
`roku.json` and `channels.json` (the Roku's fast seek must still recognise TLC's
row when it passes it).

Games are numbered afresh each day from the start of their section, so a
day's slate keeps its numbers all day. Anything `lineup.json` does not list
is numbered automatically: a channel a rescan turns up takes the first free
number in 300–349, a YouTube TV row added to `roku.json` the first free one in
350–369. That number is kept in `lineup_state.json` (not in git) so
it never moves. To move a channel, list it in `lineup.json`; that takes
precedence, and a UI reload shows it. An unusable `lineup.json` leaves
channels unnumbered (the Watch list falls back to alphabetical) rather than
renumbering anything.

`GET /api/v1/channels` lists the same channels in one shape, each with the lane
and node that play it, its network and market, the tune that plays it, its
stream and its guide row (see `API.md`). The channel list is `channels.py`: one
adapter per kind of source, which the lineup, the source catalog, the guide and
tune iterate, so a new kind of source is one new adapter.

Which station is NBC, FOX, ABC, CBS and CW in each market is
[`networks.json`](networks.json): the antenna's (Washington, by virtual
channel) and the Roku's YouTube TV market (Lansing, whose stations are YouTube
TV's network rows), with each market's NFL and NHL teams. The Sports Guide and
the Roku's Sunday NFL routing read it. If YouTube TV moves the Roku to another
market, change `youtube_tv_market` there and restart control. A broken file is
reported in control's log and leaves the sports without station placement;
control still starts.

The Guide shows TV Thing numbers too; its **Antenna numbers** option (kept
per browser) lists the antenna's own numbers in antenna order instead. Tuning
and recording always use the antenna channel itself, so either view tunes
and tapes the same channel.

### The Sports Guide (channel 6)

Channel 6, among our own channels, is **VizelVision's Sports Guide**: a page of
slides in the manner of a mid-90s cable character generator. A round opens on
"Today's Sports...", then pages through the day's games by sport (NFL football,
college football, baseball, basketball, hockey, soccer), each sport's by start
time, and starts over with fresh listings. It plays in the browser
([`static/sports-guide.html`](static/sports-guide.html)), on the Watch page's
stage or full screen on its own; it takes no tuner, lock or encoder.

**Tuning in lands mid-round, as on cable.** The rounds are laid end to end from
the epoch, so the page (and the video below) starts wherever the wall clock
says a channel that never stopped would be, part-way through a page if need be:
two viewers who tune in together see the same page. Until the listings come in,
the title page waits for them, and they cut in at once.

**Its music** is smooth jazz, as a listings channel had under its pages in the
nineties: three tunes, about five and a half minutes, synthesized by
[`sports_guide_music.py`](sports_guide_music.py) (numpy and scipy: an FM
electric piano, fretless bass, a soft drum machine, strings, and a soprano sax,
vibraphone or flute on top; the station's open logo on the vibraphone in the
first tune's intro, the closed logo at its end), levelled to -20 LUFS, in
`static/sports-guide-music.m4a` with its length beside it in the `.json`. It
too plays from the wall clock's place in it. The page starts it as it loads
(the Watch page's frame may autoplay); where a browser refuses, the first key
or click starts it. `?mute` keeps it quiet. Rebuild it with
`python3 sports_guide_music.py` (the same file each time: it is seeded).

A game is listed only when it can be watched here, and shows where:

* **On a national broadcaster in the lineup**, by that channel's name and
  number: TBS 40, ESPN 42. A network game is on its DC station over the
  antenna (ABC 17, FOX 15, CBS 19, NBC 14, CW 18). Sunday afternoon's CBS and FOX
  NFL games are regional: a Commanders or Ravens game is on CBS 19 or FOX 15,
  and the game YouTube TV's listings name on Lansing's CBS 26 or FOX 32 is
  there.
* **On a Roku game source**, by its Live Sports number and the service:
  Sunday Ticket for any other Sunday-afternoon CBS or FOX game, Prime Video
  for Thursday Night Football, MLB.TV, and ESPN+ for the NHL on ESPN
  Unlimited.

Streaming-only games (Peacock, Apple TV's MLS, Prime Video's NBA, ESPN+ outside
hockey), regional sports networks, and finished games are left out. Before
06:00 the night's games still being played stay in. The listings come from
`GET /api/v1/sports-guide` ([`sports_guide.py`](sports_guide.py)): ESPN's
scoreboards for every league but baseball (every FBS and Division I game, not
only the ranked ones), and the Roku's own catalogs for baseball and the game
sources, cached for a minute. Its serif is TeX Gyre Pagella, shipped in
`static/fonts/` (see the README there), so the page needs no internet.

**As video, for a Roku.** The same slides are a live stream at
`/stream/sports-guide/index.m3u8` (the lineup's `stream_path`), made only while
someone watches. The first request for the playlist starts
[`sports_guide_video.py`](sports_guide_video.py), which draws each frame with
PIL and numpy (no browser) and pipes raw 1280x720 frames at 30 fps into one
`ffmpeg`: libx264 `superfast` with `stillimage,zerolatency` on two threads,
BT.709, the music looped from the wall clock's place in it as AAC at 96 kbit/s
(silence from `/dev/zero` if the file is missing: some players insist on a
sound track), 2 s HLS segments in `streams-web/sports-guide/`. The playlist answers once two
segments exist, about 2 s after the request. [`slide_stream.py`](slide_stream.py)
supervises the pair: it stops both 60 s after the last request
(`TV_THING_SLIDES_IDLE_SECONDS`), restarts a pair that dies or stalls while
watched (three times a minute at most, then it rests a minute), and empties the
folder. The renderer leaves when its parent does, and a startup reaps any
orphan. Measured on viviserv: about 10% of a core to draw and 35% to encode
(45% with the music, measured outside the server on 2026-10-21),
about 110 MiB each, 0.4-0.6 Mbit/s. The web-basic service makes it
([`web_basic.py`](web_basic.py), its own unit and limits), so it is the web
streams stack's: check a change with `scripts/web_stream_check.py sports-guide`,
which watches it for ten minutes and requires its CPU to stay in bounds, its
memory not to grow, and the video to stop once nobody watches.

### Cairo Radio (channel 197)

hazel's Cairo Radio, under a title card: the station's name, the show on now
and what is playing, as VizelVision's off-air card words them, and the time.
[`cairo_radio_video.py`](cairo_radio_video.py) draws the card on the Sports
Guide's dusk once a second, reading the station's master control (`/radio/api/state`,
the show from its log or weekly grid) every five seconds; `web_basic.py` lays
the station's audio (`http://127.0.0.1:1615/cairo.mp3`) under it in one
`ffmpeg` (libx264 `stillimage` on one thread, AAC at 128 kbit/s, 2 s
segments), made only while someone watches and stopped 60 s after. The page,
[`static/cairo-radio.html`](static/cairo-radio.html), plays that video. When
the station stops answering, the card says only that it is on the air; when
its audio stops, the encoder ends and the pair starts again. It is the web
streams stack's: `scripts/web_stream_check.py cairo-radio`.

The About page offers the lineup as a printable card,
`static/lineup-card.pdf`, typeset in LaTeX from
[`lineup_card.tex`](lineup_card.tex), and a print version on white paper,
`static/lineup-card-print.pdf`. They are snapshots, so rebuild them after
changing `lineup.json` or when a rescan adds channels:
`python3 lineup_card.py` reads `/api/v1/lineup` from the running server
(`--json` takes a saved copy instead), writes both, and needs
[Tectonic](https://tectonic-typesetting.github.io). The day's games are left
off; each games block carries a note in their place.

### YouTube TV channels on the Roku

`roku.json` lists the YouTube TV Live-guide rows TV Thing can tune, by name
and approximate `guide_index`, in guide order. Rows past the locals assume 15
of YouTube TV's temporary NFL Sunday Ticket rows after The CW; with fewer, a
row comes sooner. Each needs a guide-logo template in `roku_state/templates-720p/`
(not in git): select its row on the Roku, then POST `api/v1/roku/calibrate`
with `{"kind": "guide", "channel": NAME}` and the Roku lock's token. Rows the
antenna already carries (QVC, HSN, Charge!, Roar, ION and the like) are left out.
Control takes up a changed channel list without a restart, at the next look
after any navigation ends; `roku.json`'s other settings still need one. A
`roku.json` that does not load keeps the list there is and says so in the log.

Tuning walks the guide verifying each row it lands on. A target six or more
rows away is reached by **fast seek** first: bursts of five quick keys, each
followed by one look that checks the landing row for the target and otherwise
identifies it (correcting for dropped keys or an overshoot). A burst never ends
within four rows of where the target could be (nineteen while the Sunday
Ticket rows lie in between), so a channel that has moved up the guide is still
walked onto. `navigation.fast_seek_*` in `roku.json` tunes it; `fast_seek_rows: 0`
turns it off.

Additional fast checks: `node --test test_tunable_channels.cjs test_live_player.cjs`
and `node test_tunable_browser.cjs /path/to/playwright`. The browser check mocks
source APIs and covers both URL layouts, combined search, source routing,
station playback, declined locks, delayed retunes, keyboard focus and mobile
layout. UI-only changes need just these checks and the fast suite; changes to
the core stream stack still need the resource gate in `AGENTS.md`.

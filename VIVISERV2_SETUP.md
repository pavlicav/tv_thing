# TV Thing on viviserv2

viviserv2 is a NixOS machine on the same LAN as **viviserv**, which still runs production. It
runs TV Thing as a **second node**: the same three services as viviserv (`tv-thing-feed`,
`tv-thing` control, `tv-thing-web-basic`) behind an Apache front door on `:8080`, declared in
NixOS rather than installed from `deploy/systemd/`. The WinTV-dualHD's two tuners are two digital
lanes: tuner A (`digital`, always on) and tuner B (`digital-b`, live only). The analog and Roku
lanes are absent. Its tuners and viviserv's are **one pool on one dial** (section 4): a live
tune on either box takes whichever tuner is free, and a stream from the other box is relayed
through this one's front door.

It has been running since 2026-10-31. Since 2026-11-03 its checkout is `master`, pulled from
GitHub and never committed to here (section 5). This file says how it was set up, how it is
run, and what does not work here.

- The Watch page: <http://10.0.0.99:8080/> or <http://viviserv2.local:8080/>.
- The Config page (this host's settings and hardware): `/config.html`.
- The dashboard is still on `:80`, in the same Apache.

---

## What is different here

| | viviserv (production) | viviserv2 (this box) |
|---|---|---|
| OS | Debian: `apt`, `/etc/apache2`, units in `/etc/systemd/system` | **NixOS 26.05**: packages, units, groups and Apache are declared in `/etc/nixos/` and applied with `nixos-rebuild` |
| Python | `~/miniconda3/bin/python3` | `python3.withPackages` (numpy, pillow, scipy, requests, pytest, jsonschema), 3.13, from `/etc/nixos/tv-thing.nix` |
| Tuner | HVR-1600 PCIe: S5H1409 digital + CX23418 analog | **Hauppauge WinTV-dualHD 01595** (USB `2040:826d`, `em28xx`): two digital tuners, each an LGDT3306A + Si2157, at `/dev/dvb/adapter0` and `adapter1`. No analog under Linux |
| Roku lane | HD PVR + the house Roku at `10.0.0.240` | None: no HD PVR. That Roku answers from here, so the lane is pinned off (hazard 1) |
| GPU | Quadro K4200 (encoder pinned to `cpu`) | Intel UHD 630 (`i915`). Encoder `vaapi` (h264_vaapi, iHD driver) |
| CPU / RAM | 14 cores / 28 threads at 2.6 GHz | i5-9500: 6 cores / 6 threads, 3.0–4.4 GHz; 15 GiB |
| Disk | Nearly full boot SSD + 4 TB `/mnt/sda1` for recordings | One 225 GB NVMe. Recordings in `~/tv-recordings` |
| Address | `10.0.0.98` (static) | `10.0.0.99` on `eno1`, DHCP; also `viviserv2.local` (avahi) |
| Front door | Apache, explicit addresses on `:8080`; the main site's `/tv/` proxies to it; Tailscale Serve forwards the tailnet's `:8080` to it | Apache, `*:8080`, sharing the dashboard's Apache on `:80`, whose `/tv/` proxies to it (the dashboard's TV Thing card links there) |
| Neighbours | VizelVision, Trains, Cairo Radio's audio | None. Cairo Radio and VizelVision media come from viviserv |

## How this host knows it is viviserv2

`host_config.py` (the Config page) resolves each setting from, first that has it: the unit's
environment, then `~/.config/tv-thing/host.json`, then "auto" from the hostname and hardware.
Here that gives:

| Setting | Value | From |
|---|---|---|
| Node role | second | auto: the hostname is `viviserv2`, `feed.json` describes `viviserv` |
| Node name | `viviserv2` | auto |
| Roku lane | **off** | the units' environment, `TV_THING_ROKU=off` (hazard 1). Auto would also say off: roku.json's HD PVR is not plugged in here |
| YouTube TV guide | off | auto: follows the Roku lane |
| Digital tuner | adapter 0 | auto: feed.json's |
| Video encoder | vaapi | the feed's environment (`tv-thing.nix`) |
| Recordings folder | `~/tv-recordings` | auto: no `/mnt/sda1/media` here |
| Primary's address | `10.0.0.98` | `~/.config/tv-thing/host.json` |
| Peer's address | `10.0.0.98` | auto: a second node pools with its primary (section 4) |
| Second digital tuner | adapter 1 (`digital-b`) | auto: the first DVB adapter besides the digital tuner's |
| Location | (unset) | the Inputs page's Set location; read on every use, no restart |

So the tracked `feed.json` and `roku.json` are used as they are: nothing host-specific is in
the checkout. The digital lane describes itself from sysfs ("ATSC digital television (LGDT3306A
+ Si2157)"), and the analog and Roku lanes say "not fitted on viviserv2".

## Hazards

1. **The house Roku.** `roku.json` points at `http://10.0.0.240:8060`, and that Roku answers
   from viviserv2. A Roku lane here would press keys on the Roku that viviserv and VizelVision
   are airing. Every unit sets `TV_THING_ROKU=off`, which wins over the Config page and auto.
   A hand run should set it too.

2. **The checkout is shared with production.** `origin` is `github.com/pavlicav/tv_thing`,
   which viviserv pulls. This host's settings live outside it: in `/etc/nixos/tv-thing.nix`
   and `~/.config/tv-thing/host.json`. The tracked `channels.json` is the **channel plan both
   boxes share**, and a rescan here never rewrites it: it writes only this box's reception,
   `state/reception.json` (git-ignored). The plan is the primary's (viviserv's rescan writes
   it). Until 2026-10-31 this checkout kept its own `channels.json` under `skip-worktree`;
   that is undone, and the old scans are kept in `~/tv_thing-host/`.

5. **A staged run is not in the pool unless told.** A copy started by hand reads
   `~/.config/tv-thing/host.json` and the auto settings like the services do, so it would
   pool with viviserv and hand its tunes to viviserv's real tuner when its own is locked or
   unknown, and grow tuner B on the real adapter 1. Give every staged run
   `TV_THING_PEER_HOST=off`, and `TV_THING_TUNER_B=off` unless tuner B is what it gates
   (AGENTS.md).

3. **The old procedures are viviserv's.** Never run `scripts/deploy-*.sh`, `RUNBOOK.md`, or
   `scripts/staged_split_check.sh` unmodified here. They hard-code `~/miniconda3`,
   `/etc/apache2`, viviserv's addresses and VizelVision's config. AGENTS.md's gate applies
   here with this host's paths (below).

4. **The fast suite describes viviserv.** `tests/conftest.py` pins the role to primary and
   ignores `host.json`, so the suite tests the shipped node whichever host runs it.

---

## 1. NixOS

`/etc/nixos/configuration.nix` imports `./tv-thing.nix` (next to `./viviserv2-dashboard.nix`),
and its bare `python3` was removed from `environment.systemPackages`: `tv-thing.nix` provides
`bin/python3`. The dashboard's user unit runs `/run/current-system/sw/bin/python3`, which is
now this Python, a superset of the bare one.

The originals are kept beside it: `configuration.nix.before-tv-thing`, and
`tv-thing.nix.stage1` (groups and Python only, before the services). The module itself is in
the repo, [`deploy/nixos/tv-thing.nix`](deploy/nixos/tv-thing.nix), and
`deploy/nixos/deploy-nixos.sh` installs it: it shows the change and asks, evaluates the whole
system with it before touching `/etc/nixos`, keeps a dated copy of the module it replaces,
rebuilds, and checks the iHD driver and the services afterwards. Run it as yourself; sudo asks
for the copy and the rebuild.

What it declares, in order: the video group; the iHD VA-API driver; the services' Python (and
the fast suite's); the three services, translated from `deploy/systemd/`; the front door on
`:8080` joining the dashboard's Apache, with TV Thing at `/tv/` on the dashboard's site too; and
Apache reloading, not restarting, when its config changes.


Two things the dashboard's module already provides, and this one must not repeat:
`services.httpd.adminAddr` (two different values are an evaluation error) and the proxy,
proxy_http, headers and alias modules (NixOS loads them by default).

NixOS's Apache runs as `wwwrun` and cannot read `/home/vivian` (mode 700), so the routes are
copied into its config at rebuild time rather than `Include`d.

To check a change to the module before applying it, without root, evaluate a copy of
`/etc/nixos/` and syntax-check the Apache config it generates:

```bash
S=$(mktemp -d); cp /etc/nixos/*.nix "$S"/ && cp ~/tv_thing/deploy/nixos/tv-thing.nix "$S"/
C=$(nix-build '<nixpkgs/nixos>' -I nixos-config="$S"/configuration.nix -A config.services.httpd.configFile --no-out-link)
mkdir -p "$S"/run "$S"/log
sed -e "s#/run/httpd#$S/run#g" -e "s#/var/log/httpd#$S/log#g" "$C" > "$S"/httpd.conf && httpd -f "$S"/httpd.conf -t
```

Then `sudo nixos-rebuild dry-activate` lists what it would restart and reload.

## 2. The tuners

The kernel binds the dualHD at boot as `em28xx` card 100, and both front-ends register. The
LGDT3306A needs no firmware.

Both tuners have their own antenna. A sweep of RF 2–36 on each (2026-10-31,
`~/tv_thing-host/scan-a.txt` and `scan-b.txt`) locked RF 7, 9, 14, 15, 20, 21, 24, 31, 32,
34, 35 and 36, at 20–31 dB SNR on both tuners. Those are the 70 services in the tracked
`channels.json`, with the same virtual numbers and PIDs, so this box sees viviserv's market and
the shipped `channels.json`, `lineup.json` and `networks.json` apply unchanged.

Rescans here have been uneven: the Rescan page's 16:15 scan missed RF 9, and the 17:34 one
got RF 9 but missed RF 24 (WDCO 10.1). Neither changes the dial any more. The plan is the
shared `channels.json` (70 services), and the latest scan is this box's reception:
`state/reception.json` holds RF 7, 9, 14, 15, 20, 21, 31, 32, 34, 35 and 36, seeded from the
17:34 scan (copies of both scans in `~/tv_thing-host/`). The pool never sends a channel to a
tuner whose box did not receive its RF, so WDCO 10.1 plays here from viviserv's tuner until a
rescan here locks RF 24. The two tuners have their own antennas but share one reception record,
the box's.

```bash
cd ~/tv_thing
timeout 60 python3 atsc_scan.py --channel 7 --dwell 5               # tuner A, WJLA's RF
timeout 60 python3 atsc_scan.py --adapter 1 --channel 7 --dwell 5   # tuner B
timeout 600 python3 atsc_scan.py                                     # the whole band, writes nothing
```

The scanner was written against the S5H1409's SNR unit, tenths of a dB. The readings here are
plausible ATSC figures in that unit, but have not been compared with viviserv's DX page.

## 3. The gate on this host

AGENTS.md's discipline, with this host's paths. The fast suite:

```bash
python3 -m pytest -q --ignore=tests/test_integration.py -m "not slow and not api and not hardware and not resource"
```

2026-10-31: **802 passed, 4 failed, 2 skipped.** The skips need `tectonic` (the lineup card).
The four failures are Roku OCR tests that read real screenshots with Tesseract: NixOS has
Tesseract 5.5.3, which reads them differently from viviserv's. They do not affect the
antenna, but this box cannot judge Roku changes until its Tesseract and `eng.traineddata`
match viviserv's. Do not mark them skipped.

- `roku_control/tests/test_espn_unlimited.py::SportRowTests::test_other_sports_rows_are_passed_over_down_to_nhl`
- `roku_control/tests/test_espn_unlimited.py::SportRowTests::test_row_labels_and_blocked_cards`
- `roku_control/tests/test_prime_video.py::ObservedFrameTests::test_actual_focused_profile_and_other_name`
- `roku_control/tests/test_prime_video.py::ObservedFrameTests::test_event_page_with_watch_focused_only_for_this_game`

**A staged run** before the services existed: a copy of the commit (`git archive HEAD`) in
`~/tv_thing-stage`, one process, its own ports, streams, state and recordings, time-boxed:

```bash
mkdir ~/tv_thing-stage && git -C ~/tv_thing archive HEAD | tar -x -C ~/tv_thing-stage
cd ~/tv_thing-stage && TV_THING_ROKU=off PORT=8099 TV_THING_BIND=127.0.0.1 TV_THING_TV_UDP_PORT=18420 \
  TV_THING_STREAMS_DIR=$HOME/tv_thing-scratch/streams TV_THING_STATE_DIR=$HOME/tv_thing-scratch/state \
  TV_THING_RECORDINGS_DIR=$HOME/tv_thing-scratch/recordings TV_THING_ALWAYS_ON=0 TV_THING_EPG=0 \
  timeout 3600 python3 -u server.py 2>&1 | tee ~/tv_thing-scratch/server.log
timeout 900 python3 scripts/stream_sanity_check.py --url http://127.0.0.1:8099 --server-pid <its pid> --retunes 3
```

One process listens on this host's LAN addresses as well as loopback, whatever `TV_THING_BIND`
says (`bind_candidates` always adds them); the firewall keeps `:8099` off the LAN.

**The services**, through the front door:

```bash
timeout 900 python3 scripts/stream_sanity_check.py --url http://127.0.0.1:8080 --retunes 3 \
    --server-pid "$(systemctl show -p MainPID --value tv-thing-feed)" \
    --server-pid "$(systemctl show -p MainPID --value tv-thing)"
timeout 1200 python3 scripts/web_stream_check.py sports-guide        # and cairo-radio, internet/pentulive
sudo -v && python3 scripts/restart_drill.py --vizelvision '' --command 'sudo systemctl restart tv-thing'
```

Results, 2026-10-31, with the default limits:

| Gate | Result |
|---|---|
| Staged, 3 retunes (WHUT, WJLA, WHUT) | PASSED: request-to-stream 3.2–3.5 s; CPU 109% average, 209% peak; RSS 153 MiB; 3/3 clean generations |
| Services, 3 retunes | PASSED: request-to-stream 2.4–3.2 s; CPU 115% average, 161% peak (feed + control); RSS 186 MiB; 3/3 clean generations |
| Control restart drill | OK: the digital lane never went more than 0 s without a new segment |
| `web_stream_check.py sports-guide`, as shipped | FAILED: stalled and restarted every ~20 s; RSS 840 MiB (see below) |
| `web_stream_check.py sports-guide`, fixed | OK: started in 2.2 s; CPU 73.9% average, 98.7% peak; RSS growth 2.3 MiB (peak 200 MiB); 300 segments, no stall; stopped 59 s after its last viewer |
| `web_stream_check.py cairo-radio` | OK: started in 15.2 s; CPU 6.1% average, 8.8% peak; RSS growth −1.8 MiB; stopped 60 s after its last viewer |
| `web_stream_check.py internet/pentulive` | OK: started in 1.0 s; CPU 3.7% average, 12% peak; RSS growth 3.1 MiB |

The GPU checks read `nvidia-smi` only, so they read 0 here, which is right for libx264.

**Tuner B and VA-API, 2026-11-01.** `deploy/nixos/gate-staged.sh` gates a staged candidate
(branch `station-ids`) with both tuners on the UHD 630 (`TV_THING_VIDEO_ENCODER=vaapi`, the
system iHD driver): each tuner alone, then tuner B retuned while tuner A holds WETA, with
`--max-dvb-sources 2` (two streams on purpose). With no NVIDIA card the gate reads the CPU
package temperature as the GPU's; the iGPU's utilization needs root and goes unmeasured.

| Gate | Result |
|---|---|
| Tuner A alone, 3 retunes, VA-API | PASSED: request-to-stream 2.9–3.2 s; CPU 102% average, 144% peak; 3/3 clean |
| Tuner B alone, 3 retunes, VA-API | PASSED: request-to-stream 2.3–3.6 s; CPU 100% average, 143% peak; 3/3 clean |
| Tuner B with tuner A streaming, VA-API | PASSED: request-to-stream 1.7–2.8 s; CPU 137% average, 171% peak; 3/3 clean; package 50 C at most |
| The same on libx264 | FAILED: CPU 304% average, 372% peak (limits 200 / 350), so this host encodes on the iGPU |
| Tuner B unwatched | OK: stopped 60 s after its last fetch (`TV_THING_EXTRA_TUNER_IDLE_SECONDS=60` staged; 300 s in production) |

What CPU is left is decoding and deinterlacing the broadcast MPEG-2, about one core a stream.

**The Sports Guide's music on ffmpeg 7.1 and 8.1.** As shipped, the encoder plays its music with
`-stream_loop -1 -ss <wall clock's place>`. On this host's ffmpeg 8.1.3 (and nixpkgs' 7.1.5) that
fails: the accurate seek's trim ends the audio filter with `No space left on device` (−28) a
few seconds in, raw video frames pile up to web-basic's `MemoryMax`, and the stream stalls.
viviserv's older ffmpeg does not do this. `-noaccurate_seek` avoids the error, but at the loop
ffmpeg then loses the seek's offset: the music's timestamps jump ahead by it, and `-shortest`
holds that much music (about 1 MB a second of the offset, 75–256 MiB measured). The fix in
`web_basic.py` keeps the frame-accurate seek off and counts the music's timestamps from its
samples (`-af asetpts=N/SR/TB`): in bounded repros, flat memory across the loop and audio
within 16 ms of video. Two other fixes failed the same way (an `atrim` filter, and the
`concat` filter over the track's remainder and the looped track); ffmpeg's concat demuxer with
an `inpoint` also worked, but needs a playlist file per generation.

## 4. Two nodes, one dial (the tuner pool)

viviserv and viviserv2 pool their digital tuners: viviserv's one, and this box's tuner A and
tuner B. Each box still owns its own tuners, locks and feed; neither ever commands the other's
feed. They talk only through each other's public front door on `:8080`, so moving to Tailscale
means changing `peer_host` (Config, both boxes) and the `TV_THING_PEER_URL` Define (here, in
`/etc/nixos/tv-thing.nix`; on viviserv, in `deploy/apache/tv-thing-front-door.conf`).

- **A live tune** takes, in order: a tuner already playing that channel (watching along, even a
  locked one); a free tuner of this box's, an idle one before a running one; a free tuner of the
  other box's. "Free" means known, unlocked and not scanning. A **lock** is what moves a tune:
  locking a tuner sends the next tune to another one.
- **The other box's stream** plays here at `/stream/peer/...`: Apache proxies it to the other
  box's `/stream/`. A viewer only ever talks to the box it opened.
- **A box that stops answering** for 6 s reads every tuner `unknown`, never free.
- **Locks** on the other box's tuner (`lane: "viviserv:digital"`) are forwarded to that box,
  which issues and checks the token.
- **The Watch page** shows a tuner strip ("Tuners 2/3"). The **Inputs page** shows both boxes
  in full, and each box's location is set there.
- **The same channel twice.** One channel normally occupies one tuner: a tune for a channel any
  tuner already plays healthily joins that stream instead of tuning another, whichever box asked,
  so ten viewers of WRC on both boxes cost one tuner and one encoder. Two tuners end up on the
  same channel only when someone asks for that: a tune naming a tuner, an override, a lock
  holder's tune on its own tuner (VizelVision, the VCR), or two tunes racing. Each relayed
  viewer fetches its own copy across the link (Apache does not cache), which matters only once
  the boxes are on different networks.
- **Tuner B** (`digital-b`, adapter 1, `tunerb.m3u8`) is live only: started by a tune, it feeds
  no recording, is not the always-on channel and never `live.m3u8`. Control stops it once nobody
  has fetched one of its segments for five minutes (`TV_THING_EXTRA_TUNER_IDLE_SECONDS`) and it
  is unlocked, which leaves it idle: the pool's first choice for the next tune here. The scan,
  the guide read, the DX meter and the VCR stay on tuner A. `TV_THING_TUNER_B=off` in a unit's
  environment, or `tuner_b: off` on the Config page, takes it away.

```bash
curl -s http://127.0.0.1:8080/api/v1/tuners | python3 -m json.tool      # the pool, as this box sees it
curl -s http://127.0.0.1:8080/api/v1/peer/inputs | python3 -c 'import json,sys; print(json.load(sys.stdin)["peer"])'
```

## Operating it

```bash
systemctl status tv-thing-feed tv-thing tv-thing-web-basic httpd --no-pager
journalctl -u tv-thing-feed -u tv-thing -u tv-thing-web-basic -f | grep -v '\[HTTP\]'
curl -s http://127.0.0.1:8080/api/v1/status | python3 -m json.tool | less
```

- **Restarting:** `sudo systemctl restart tv-thing` drops nothing (use the restart drill).
  `tv-thing-web-basic` drops only its own web streams. `tv-thing-feed` drops every stream and
  ends every recording: do it deliberately.
- **Code** (a pull, section 5): reaches each service at its next restart, crash restarts
  included, as on viviserv.
- **Unit settings** (`/etc/nixos/tv-thing.nix`): `sudo nixos-rebuild switch` restarts control
  and web-basic when their units change, and never the feed (`restartIfChanged = false`: it
  says "NOT restarting the following changed units"). Restart the feed yourself afterwards
  if its unit changed.
- **Apache** (the routes, or the module): reloaded gracefully at the next rebuild. If a
  rebuild ever lists httpd under "restarting" or "stopping", the overrides were lost, and that
  restart dropped every viewer. (The first activation restarted it once, because its unit
  itself changed.)
- **Host settings** (the Config page, `~/.config/tv-thing/host.json`): read by each service
  when it starts; the page says which restart a change waits for.
- **The digital lane is always on.** With no channel saved, the always-on keeper tunes the first
  in the list (WRC 4.1) at startup, as viviserv does. The first second or so of each tune logs
  MPEG-2 "Invalid frame dimensions 0x0" lines until the first sequence header arrives; the lane's
  health is in `/api/v1/status`.
- **The guide** reads the tuner at control's start when it has less than a day, and at 03:00
  and 15:00, interrupting live TV for about 80 seconds, as on viviserv.

## 5. Version control: one master, pulled here

Both boxes run commits of one branch, `master` on GitHub (`pavlicav/tv_thing`). Every change is
made, gated and committed on viviserv, merged to its `master` and pushed from there (viviserv
has the GitHub key). This box only pulls: it reads GitHub anonymously over HTTPS, has no key,
and nothing is ever committed here. What differs between the boxes is never in git: each
box's settings are `~/.config/tv-thing/host.json`, and its state (`state/`: reception, signal
grades, the peer's plan, a non-home market's own plan) is git-ignored.

To deploy here what viviserv runs:

```bash
cd ~/tv_thing
git fetch && git log --oneline HEAD..origin/master    # what is coming
git pull --ff-only
```

Then restart what the change needs, under its gate (AGENTS.md; section 3 for the paths here):
control under the restart drill; web-basic after `scripts/web_stream_check.py`; the feed only
after `deploy/nixos/gate-staged.sh` (a staged candidate in `~/tv_thing-gate-b`, production
stopped) and then `deploy/nixos/gate-production.sh`; a change to `deploy/nixos/tv-thing.nix` by
`deploy/nixos/deploy-nixos.sh`. A pull alone restarts nothing, but every service takes the new
code at its next restart, a crash's included: pull only what is meant to run here.

Until 2026-11-03 this box ran the `host-config` branch, fed by bundles copied over from
viviserv; that branch is kept locally (`git branch -v`) and no longer used.

## What does not work here

- **Analog lane.** No analog hardware. The lane stays in `feed.json` (`server.py` refuses to
  start without it) and says "not fitted". Cable channels do not tune.
- **Roku lane.** Off by design (hazard 1). YouTube TV, Sunday Ticket, MLB and Prime Video
  channels do not tune, and the Watch page's Roku tab is empty: the pool shares digital tuners
  only. Turning it on here would also need roku.json's `ffmpeg_path` (`/usr/bin/ffmpeg`, absent
  on NixOS) fixed.
- **Sports Guide listings.** The channel renders, but its games come from the Roku lane's
  schedules, so it lists none here.
- **Trains (`cabride`).** It relays `127.0.0.1:8791`, a service on viviserv that does not
  answer off that box, so it plays from viviserv's relay through `/stream/peer/`
  (`server.played_from_primary`). It works only while viviserv is up, and has no listings here.
- **Cairo Radio and VizelVision media** come from viviserv (`primary_host`), so they work only
  while viviserv is up. The feed relays VizelVision, so a new `primary_host` reaches it at the
  feed's next restart.
- **Leaving this LAN.** `peer_host` and `primary_host` can be Tailscale addresses, but
  `/stream/peer/` is Apache's: `TV_THING_PEER_URL` is a LAN address here and in viviserv's
  `deploy/apache/tv-thing-front-door.conf`. Both must change before this box moves.
- **The lineup card PDF.** It needs `tectonic` (`pkgs.tectonic`).
- **Tuner B as the guide and scan tuner**, so the 03:00 and 15:00 guide reads stop interrupting
  live TV on tuner A: not done. Tuner B is a live lane (section 4).

# TV Thing on viviserv2

viviserv2 is a NixOS machine on the same LAN as **viviserv**, which still runs production. It
runs TV Thing as a **second node**: the same three services as viviserv (`tv-thing-feed`,
`tv-thing` control, `tv-thing-web-basic`) behind an Apache front door on `:8080`, declared in
NixOS rather than installed from `deploy/systemd/`. There is one digital lane, on the
WinTV-dualHD's tuner A. The analog and Roku lanes are absent.

It has been running since 2026-10-31, from the `host-config` branch (`e3b8379`). This file says
how it was set up, how it is run, and what does not work here.

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
| GPU | Quadro K4200 (encoder pinned to `cpu`) | Intel UHD 630 (`i915`). Encoder `cpu` (libx264) |
| CPU / RAM | 14 cores / 28 threads at 2.6 GHz | i5-9500: 6 cores / 6 threads, 3.0–4.4 GHz; 15 GiB |
| Disk | Nearly full boot SSD + 4 TB `/mnt/sda1` for recordings | One 225 GB NVMe. Recordings in `~/tv-recordings` |
| Address | `10.0.0.98` (static) | `10.0.0.99` on `eno1`, DHCP; also `viviserv2.local` (avahi) |
| Front door | Apache, explicit addresses on `:8080` | Apache, `*:8080`, sharing the dashboard's Apache on `:80` |
| Neighbours | VizelVision, Trains, Cairo Radio's audio, main site `/tv/` | None. Cairo Radio and VizelVision media come from viviserv |

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
| Video encoder | cpu | the feed's environment |
| Recordings folder | `~/tv-recordings` | auto: no `/mnt/sda1/media` here |
| Primary's address | `10.0.0.98` | `~/.config/tv-thing/host.json` |

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
   and `~/.config/tv-thing/host.json`. A rescan from the server rewrites the tracked
   `channels.json`, so this checkout has `git update-index --skip-worktree channels.json`:
   this box's scan never reaches viviserv. A pull that changes `channels.json` then refuses
   to overwrite it; `git update-index --no-skip-worktree channels.json` and
   `git checkout channels.json` take viviserv's, and rescanning makes it this box's again.

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
`tv-thing.nix.stage1` (groups and Python only, before the services). A working copy of the
module is in `~/tv_thing-host/tv-thing.nix`.

`/etc/nixos/tv-thing.nix`:

```nix
# /etc/nixos/tv-thing.nix: TV Thing on viviserv2, a second node (~/tv_thing/VIVISERV2_SETUP.md).
# The video group, the services' Python, the three services (translations of
# deploy/systemd/*.service) and the front door on :8080 (deploy/apache/). This host's own settings
# are host_config.py's: auto from the hostname and hardware, plus ~/.config/tv-thing/host.json.
{ config, lib, pkgs, ... }:

let
  tv = "/home/vivian/tv_thing";

  # The services' and the fast suite's Python 3.13. It stands in for viviserv's miniconda.
  python = pkgs.python3.withPackages (ps: with ps; [
    numpy pillow scipy requests   # the services (scipy: sports_guide_music.py; requests: test_integration.py)
    pytest jsonschema             # the fast suite
  ]);

  # Pinned here, over the Config page and auto: nothing on this box may press a key on the house
  # Roku, which answers from anywhere on the LAN and belongs to viviserv and VizelVision.
  hostEnvironment = {
    TV_THING_ROKU = "off";
    # Each line reaches the journal when printed, not when an 8 KiB buffer fills.
    PYTHONUNBUFFERED = "1";
  };
  unit = {
    wantedBy = [ "multi-user.target" ];
    path = [ pkgs.ffmpeg pkgs.procps ];   # NixOS units get a minimal PATH; the code runs `ffmpeg`/`ffprobe`
  };
  service = {
    Type = "simple";
    User = "vivian";
    WorkingDirectory = tv;
    Restart = "always";
    TimeoutStopSec = 5;
  };
in
{
  # /dev/dvb/adapter*/* are root:video 0660.
  users.users.vivian.extraGroups = [ "video" ];

  environment.systemPackages = [
    python
    pkgs.tesseract   # only the fast suite's Roku OCR tests use it on this host
  ];

  systemd.services.tv-thing-feed = unit // {
    description = "TV Thing feed (tuners, recorders, streams)";
    wants = [ "network-online.target" ];
    after = [ "network-online.target" ];
    # A rebuild must never restart the feed behind your back: that drops every stream and ends
    # every recording. Restart it yourself, deliberately.
    restartIfChanged = false;
    environment = hostEnvironment // {
      TV_THING_VIDEO_ENCODER = "cpu";
      TV_THING_CONTROL_URL = "http://127.0.0.1:8087";
    };
    serviceConfig = service // {
      ExecStart = "${python}/bin/python3 ${tv}/feed_server.py";
      RestartSec = 5;
      CPUQuota = "400%";      # viviserv: 800% of 28 threads. Here: 4 of 6 faster cores
      MemoryMax = "1800M";
      TasksMax = 192;
      OOMPolicy = "continue";
      LimitFSIZE = "16M:16G";
    };
  };

  systemd.services.tv-thing = unit // {
    description = "TV Thing control (API, UI, channels, guides, VCR)";
    wants = [ "network-online.target" "tv-thing-feed.service" ];
    after = [ "network-online.target" "tv-thing-feed.service" ];
    environment = hostEnvironment // {
      TV_THING_ROLE = "control";
      PORT = "8087";
      TV_THING_FEED_URL = "http://127.0.0.1:8086/";
    };
    serviceConfig = service // {
      ExecStart = "${python}/bin/python3 ${tv}/server.py";
      RestartSec = 2;
      CPUQuota = "200%";      # viviserv: 400%
      MemoryMax = "1200M";
      TasksMax = 160;
      OOMPolicy = "stop";
      LimitFSIZE = "16M:16G";
    };
  };

  systemd.services.tv-thing-web-basic = unit // {
    description = "TV Thing web-basic (slide channels' video, simple web relays)";
    wants = [ "network-online.target" ];
    after = [ "network-online.target" ];
    environment = hostEnvironment // {
      TV_THING_CONTROL_URL = "http://127.0.0.1:8087";
    };
    serviceConfig = service // {
      ExecStart = "${python}/bin/python3 ${tv}/web_basic.py";
      RestartSec = 5;
      CPUQuota = "200%";      # as viviserv: web_stream_check.py's own peak limit
      MemoryMax = "768M";
      TasksMax = 96;
      LimitFSIZE = "16M:2G";
    };
  };

  # The front door: Apache on :8080. /stream/ goes to the feed (and web-basic's paths to
  # web-basic), everything else to control, and /internal/ and /feed/v1/ are never routed.
  # It joins the dashboard's Apache (viviserv2-dashboard.nix), which already sets adminAddr and
  # loads proxy, proxy_http, headers and alias. *:8080, not explicit addresses: viviserv binds
  # its own only because another service holds tailscale0's :8080 there.
  # The routes are the repo's, read at rebuild time, so a change to them takes effect at the
  # next `nixos-rebuild`, not on a pull.
  services.httpd.virtualHosts.tv-thing-front-door = {
    listen = [ { port = 8080; } ];
    extraConfig = ''
      Define TV_THING_FEED_PORT 8086
      Define TV_THING_CONTROL_PORT 8087
    '' + builtins.readFile "${tv}/deploy/apache/tv-thing-routes.conf";
  };
  systemd.services.httpd = {
    # NixOS restarts Apache whenever its config changes, which drops every viewer. Reload it
    # instead: its ExecReload is `httpd -k graceful`.
    restartTriggers = lib.mkForce [ ];
    reloadTriggers = [ config.services.httpd.configFile ];
  };

  networking.firewall.allowedTCPPorts = [ 8080 ];
}
```

Two things the dashboard's module already provides, and this one must not repeat:
`services.httpd.adminAddr` (two different values are an evaluation error) and the proxy,
proxy_http, headers and alias modules (NixOS loads them by default).

NixOS's Apache runs as `wwwrun` and cannot read `/home/vivian` (mode 700), so the routes are
copied into its config at rebuild time rather than `Include`d.

To check a change to the module before applying it, without root, evaluate a copy of
`/etc/nixos/` and syntax-check the Apache config it generates:

```bash
S=$(mktemp -d); cp /etc/nixos/*.nix "$S"/ && cp ~/tv_thing-host/tv-thing.nix "$S"/
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

A rescan from the Rescan page (2026-10-31 16:15) missed RF 9, which both sweeps had locked at
20 dB, and the server kept its result: 63 services, without WUSA's 9.1–9.5, AltaVsn 68.1 and
DEFY 68.2. That is this box's `channels.json` now (copy in
`~/tv_thing-host/channels-rescan-2026-10-31.json`). The dial leaves out the seven numbers it
has no service for (19, 243, 247–249, 263, 270), and CBS (`networks.json`: antenna 9.1) does
not tune here. A rescan that locks RF 9 brings them back.

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

## Operating it

```bash
systemctl status tv-thing-feed tv-thing tv-thing-web-basic httpd --no-pager
journalctl -u tv-thing-feed -u tv-thing -u tv-thing-web-basic -f | grep -v '\[HTTP\]'
curl -s http://127.0.0.1:8080/api/v1/status | python3 -m json.tool | less
```

- **Restarting:** `sudo systemctl restart tv-thing` drops nothing (use the restart drill).
  `tv-thing-web-basic` drops only its own web streams. `tv-thing-feed` drops every stream and
  ends every recording: do it deliberately.
- **Code** (a pull): reaches each service at its next restart, crash restarts included, as on
  viviserv. The checkout is on `host-config`; once that is merged, switch it to `master`.
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

## What does not work here

- **Analog lane.** No analog hardware. The lane stays in `feed.json` (`server.py` refuses to
  start without it) and says "not fitted". Cable channels do not tune.
- **Roku lane.** Off by design (hazard 1). YouTube TV, Sunday Ticket, MLB and Prime Video
  channels do not tune. Turning it on here would also need roku.json's `ffmpeg_path`
  (`/usr/bin/ffmpeg`, absent on NixOS) fixed.
- **Sports Guide listings.** The channel renders, but its games come from the Roku lane's
  schedules, so it lists none here.
- **Trains (`cabride`).** It relays `127.0.0.1:8791`, a service on viviserv that does not
  answer on the LAN.
- **Cairo Radio and VizelVision media** come from viviserv (`primary_host`), so they work only
  while viviserv is up.
- **The lineup card PDF.** It needs `tectonic` (`pkgs.tectonic`).
- **Tuner B (`adapter1`).** It works (see the scan) but TV Thing tunes one adapter. Using it as
  a second digital lane, or as the guide and scan tuner so the guide reads stop interrupting
  live TV, is a core-stream-stack change under AGENTS.md's gate.
- **VA-API on the UHD 630.** ffmpeg has `h264_vaapi`, but the packager's temperature trip and
  the gate's GPU checks read `nvidia-smi` only, so an Intel GPU would run unmeasured. Stay on
  libx264 until an Intel probe is added (also a gated change). libx264 has room: the gate's
  average CPU above is about a fifth of this box's six cores.

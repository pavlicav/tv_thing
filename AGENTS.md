# TV Thing change and deployment rules

All repository changes must pass the fast local test suite before deployment:
`pytest -q --ignore=tests/test_integration.py -m "not slow and not api and not
hardware and not resource"`. The tests live in `tests/`. `tests/test_integration.py`
retunes the server it is pointed at (production unless `TV_THING_URL` says
otherwise), so it runs only with `TV_THING_INTEGRATION=1`, never by accident.

Production is three services, all run from this checkout (`INVARIANTS.md`,
`RUNBOOK.md`), behind Apache's front door on `:8080` (`deploy/apache/`):

- `tv-thing-feed` (`feed_server.py`, `127.0.0.1:8086`): the tuners, the Roku
  capture, their FFmpeg, the recorders, Trains' relay, the rest of `/stream/`.
  Restarting it drops every tuner stream and ends every recording.
- `tv-thing`, control (`server.py` with `TV_THING_ROLE=control`,
  `127.0.0.1:8087`): the API and UI, locks, tune resolution, Roku navigation,
  guides, the VCR's rules and finalizing. Restarting it drops no stream.
- `tv-thing-web-basic` (`web_basic.py`, `127.0.0.1:8085`): the web streams
  stack (below). Restarting it drops only its own streams.

A change merged into this checkout reaches each service at its next restart,
crash restarts included: merging is deploying, to whichever service runs the
changed code, whichever one you meant to restart.

Changes to the core stream stack must also pass the real-stream resource gate
on `viviserv` before production files or services are changed. The core stream
stack is:

- FFmpeg: every invocation and its arguments (live packagers, the recorder, the
  HD PVR capture copy, the Roku verifier's frame decoder), encoder selection,
  and thread or resource limits;
- HLS output: playlists, segments, and the streams directory;
- the digital and analog tuners and capture hardware access (`dvb_source.py`,
  `tuner.py`, `analog_tuner.py`, the HD PVR device);
- process supervision (spawning, restarting, reaping, watchdogs, the always-on
  keeper) and the feed's and control's systemd units (`deploy/systemd/`);
- the feed: `feed_node.py`, `feed_server.py`, `feed_api.py` (the feed's API,
  and `RemoteFeed`, which decides what control may treat as free or dead), and
  what `server.py` runs in the feed role.

Roku tuning (navigating YouTube TV, Sunday Ticket, MLB, or Prime Video: screen
recognition, OCR, key sequences, and the lane's selection logic) and UI changes
(`static/`, `roku_app/`) do not need the gate; the fast suite is enough. Nor
do the Sports Guide's listings (`sports_guide.py`) and its browser slides; its
video is the web streams stack's. A change that also touches the core stream
stack is gated.

The web streams stack is the simple web streams, which the web-basic service
runs under its own CPU and memory limits, so one that misbehaves never takes
the tuners' share:

- the station's slide channels as video: the Sports Guide
  (`sports_guide_video.py`) and Cairo Radio (`cairo_radio_video.py`), their
  encoders (`web_basic.py`), and `slide_stream.py`'s on-demand start and idle
  stop;
- the relays of `internet_streams.json`'s `"service": "web-basic"` streams
  (pen2live), and `internet_streams.py`'s relay;
- `web_basic.py` itself, `deploy/systemd/tv-thing-web-basic.service`, and its
  line in Apache's routes.

It does not need the gate. A change to it needs the fast suite and
`scripts/web_stream_check.py` for each stream it touches: against a staged
web-basic first (its own port, folder and `--pid`, under `timeout`), then
against production after `sudo systemctl restart tv-thing-web-basic`. The check
watches the stream for ten minutes and fails on CPU excess (100% of a core on
average, 200% at peak), memory growth (24 MiB from the first minute after
warm-up to the last), a stall, or video still running after its last viewer.
Never waive a limit to pass; diagnose instead. Trains (`cabride`) is relayed by
the feed, so check a change to `internet_streams.py` on it too, through the
feed. A new web-basic stream also needs its path in Apache's web-basic route (a
front-door change).

A change only control runs is deployed by restarting control under the restart
drill, which fails if the restart moved a stream, a recording, a relay or
VizelVision:

    scripts/restart_drill.py --command 'sudo systemctl restart tv-thing'

A front-door change (`deploy/apache/`) needs `scripts/staged_split_check.sh`
and `apachectl configtest`, then `apachectl graceful`, never a full Apache
restart, which drops every viewer.

Never reuse the older `scripts/deploy-*.sh`: they install the root
`tv-thing.service`, the one-process unit kept for rollback, and would undo the
split. `scripts/deploy-feed-split.sh` refuses once the split is in, except
`--rollback`.

The gate:

1. Keep both production services, `tv-thing-feed` and `tv-thing`, stopped.
2. Copy the candidate into a separate staging directory.
3. Run it on a non-production HTTP port, UDP port, and streams directory. A
   split candidate is a feed, a control and a private front door, as
   `scripts/deploy-feed-split.sh` builds one; ports 8088, 8090 and 8096
   belong to other services.
4. Run `scripts/stream_sanity_check.py` against at least three real retunes, including
   request-to-stream latency, MPEG-TS integrity, segment size/duration and
   write-rate checks, process CPU/RSS, GPU utilization, and GPU temperature.
5. Do not promote the candidate if any check fails. Never waive a resource
   limit merely to obtain a pass; diagnose or choose a safer encoder instead.
6. After promotion, run the same gate against the production services and roll
   back/stop them immediately if the production gate fails.

Do not run an unbounded FFmpeg experiment. Every hardware test must have a
time limit, a separate output directory, and cleanup/stop handling. Never use
the nearly-full production filesystem for synthetic or long-duration media.


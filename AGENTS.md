# TV Thing change and deployment rules

All repository changes must pass the fast local test suite before deployment:
`pytest -q --ignore=test_integration.py -m "not slow and not api and not
hardware and not resource"`. Bare `pytest` also runs `test_integration.py`,
which retunes production.

Production is two services, both run from this checkout (`INVARIANTS.md`,
`RUNBOOK.md`), behind Apache's front door on `:8080` (`deploy/apache/`):

- `tv-thing-feed` (`feed_server.py`, `127.0.0.1:8086`): the tuners, the Roku
  capture, every FFmpeg, the recorders, the relays, `/stream/`. Restarting it
  drops every stream and ends every recording.
- `tv-thing`, control (`server.py` with `TV_THING_ROLE=control`,
  `127.0.0.1:8087`): the API and UI, locks, tune resolution, Roku navigation,
  guides, the VCR's rules and finalizing. Restarting it drops no stream.

A change merged into this checkout reaches each service at its next restart,
crash restarts included: merging is deploying, to whichever service runs the
changed code, whichever one you meant to restart.

Changes to the core stream stack must also pass the real-stream resource gate
on `viviserv` before production files or services are changed. The core stream
stack is:

- FFmpeg: every invocation and its arguments (live packagers, the recorder, the
  HD PVR capture copy, the Roku verifier's frame decoder, the Sports Guide's
  video encoder and `sports_guide_video.py`, the renderer that feeds it),
  encoder selection, and thread or resource limits;
- HLS output: playlists, segments, and the streams directory;
- the digital and analog tuners and capture hardware access (`dvb_source.py`,
  `tuner.py`, `analog_tuner.py`, the HD PVR device);
- process supervision (spawning, restarting, reaping, watchdogs, the always-on
  keeper, and `slide_stream.py`'s on-demand start and idle stop) and the
  systemd units (`deploy/systemd/`);
- the feed: `feed_node.py`, `feed_server.py`, `feed_api.py` (the feed's API,
  and `RemoteFeed`, which decides what control may treat as free or dead), and
  what `server.py` runs in the feed role.

Roku tuning (navigating YouTube TV, Sunday Ticket, MLB, or Prime Video: screen
recognition, OCR, key sequences, and the lane's selection logic) and UI changes
(`static/`, `roku_app/`) do not need the gate; the fast suite is enough. Nor
do the Sports Guide's listings (`sports_guide.py`) and its browser slides; its
video is core stream stack, gated with `stream_sanity_check.py --slides
sports-guide`. A change that also touches the core stream stack is gated.

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
4. Run `stream_sanity_check.py` against at least three real retunes, including
   request-to-stream latency, MPEG-TS integrity, segment size/duration and
   write-rate checks, process CPU/RSS, GPU utilization, and GPU temperature.
5. Do not promote the candidate if any check fails. Never waive a resource
   limit merely to obtain a pass; diagnose or choose a safer encoder instead.
6. After promotion, run the same gate against the production services and roll
   back/stop them immediately if the production gate fails.

Do not run an unbounded FFmpeg experiment. Every hardware test must have a
time limit, a separate output directory, and cleanup/stop handling. Never use
the nearly-full production filesystem for synthetic or long-duration media.


# TV Thing change and deployment rules

All repository changes must pass the fast local test suite before deployment.

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
- process supervision (spawning, restarting, reaping, watchdogs, and
  `slide_stream.py`'s on-demand start and idle stop) and the systemd unit.

Roku tuning (navigating YouTube TV, Sunday Ticket, MLB, or Prime Video: screen
recognition, OCR, key sequences, and the lane's selection logic) and UI changes
(`static/`, `roku_app/`) do not need the gate; the fast suite is enough. Nor
do the Sports Guide's listings (`sports_guide.py`) and its browser slides; its
video is core stream stack, gated with `stream_sanity_check.py --slides
sports-guide`. A change that also touches the core stream stack is gated.

The gate:

1. Keep `tv-thing.service` stopped.
2. Copy the candidate into a separate staging directory.
3. Run it on a non-production HTTP port, UDP port, and streams directory.
4. Run `stream_sanity_check.py` against at least three real retunes, including
   request-to-stream latency, MPEG-TS integrity, segment size/duration and
   write-rate checks, process CPU/RSS, GPU utilization, and GPU temperature.
5. Do not promote the candidate if any check fails. Never waive a resource
   limit merely to obtain a pass; diagnose or choose a safer encoder instead.
6. After promotion, run the same gate against the production service and roll
   back/stop it immediately if the production gate fails.

Do not run an unbounded FFmpeg experiment. Every hardware test must have a
time limit, a separate output directory, and cleanup/stop handling. Never use
the nearly-full production filesystem for synthetic or long-duration media.


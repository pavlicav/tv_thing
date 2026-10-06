# TV Thing change and deployment rules

All repository changes must pass the fast local test suite before deployment.

Any change that can affect tuning, FFmpeg, HLS, the browser/Roku player,
process supervision, hardware access, or the systemd service must also pass the
real-stream resource gate on `viviserv` before production files or services are
changed:

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


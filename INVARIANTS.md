# TV Thing invariants

TV Thing is splitting into two services:
- **feed** (`tv-thing-feed`): the media plane. Devices, lanes, every ffmpeg, recorders, relays, `/stream/*`. It almost never restarts.
- **control** (`tv-thing`): the control plane. API, UI, channel list, locks, Roku navigation, guides, VCR rules. It restarts at will.

Apache owns `:8080` in front of both. The plan is `~/.claude/plans/can-we-come-up-swift-hinton.md`.

These rules hold whichever service crashes, hangs or restarts. They drive the feed/control interface (`feed_client.py`) and the restart drill (`scripts/restart_drill.py`). A change that would break one is wrong, even if every test passes.

Until Phase 2 lands, TV Thing is one process, and a restart breaks invariants 1 and 8 by design. The drill says so; that is its point.

## The invariants

| # | Invariant | Why | Proved by |
|---|---|---|---|
| 1 | Restarting, killing or hanging control never stops, restarts or retunes a feed, a recording or a relay, and never takes VizelVision off air. | This is the reason for the split: control is where the changes are made. | Drill rows A1–A4. VizelVision rides out 45 s of no answer (`roku_stream/tv_control.py`, `OUTAGE_GRACE_SECONDS`). |
| 2 | Only the feed starts and stops lane and recorder ffmpeg. Its reaper never touches a process of another systemd service (read from `/proc/<pid>/cgroup`); an orphan left by a run from a terminal is still reaped. | The reaper picks by command line (`reap_orphaned_children`). After the split, control's Roku verifier would match it, and so would VizelVision's readers of the same taps. Strictly "own cgroup only" would reap nothing in production: `KillMode=control-group` already empties the unit's cgroup on every stop, and the orphan that does happen (a terminal run's `dvb_source.py` holding the tuner) is in a session scope. | `test_feed_node.py` (`test_the_reaper_never_touches_another_service`), `test_server.py` (`test_the_reaper_kills_orphans_but_never_another_services`); drill row A5. |
| 3 | A lane command is applied at most once (`op_id`), and only at the `(feed_epoch, revision)` it was issued against. | A command retried after a crash, or delayed past a feed restart, must never retune twice or retune a lane that has moved on. | `test_feed_node.py` (Phase 1); drill rows B1, B5. |
| 4 | A recording has exactly one writer. The feed enforces its end. Every outcome lands in the durable journal. Each finalize step runs once. | Recordings must survive control being down when they start, end or finalize. | `test_feed_node.py`, control's reconcile tests (Phase 2); drill rows B3, B4, C1. |
| 5 | A Roku generation changes when the capture restarts or a navigation begins, and never because control restarted. | VizelVision fails a Roku lease closed on any generation change. A spurious change drops YouTube TV from air; a missing one airs the wrong screen. | `test_feed_node.py` (navigation), `test_roku_lane.py` (`test_a_restart_reads_…`, `test_a_navigation_cut_off_…`); drill rows A1, B2. |
| 6 | Control never treats an unreachable or hung feed's lanes as free. | A tune or recording would otherwise grab a tuner that is still busy. | `RemoteFeed` tests (Phase 2); drill rows C2, C3. |
| 7 | Client URLs and API shapes on `:8080` and `/tv/` do not change. `/internal/` and `/feed/v1/` are never reachable through Apache. | VizelVision, the Watch page and the sideloaded Roku app take no part in the migration. Resolved upstream URLs carry tokens. | `test_api_contract.py` unchanged; drill rows D2–D4. |
| 8 | A relay that is playing keeps playing while control is down. | Web streams are on air too. | Drill row A1 (with an internet stream playing). |

## The state model

**Control owns intent and policy. The feed owns actual state, physical enforcement, and keeping its current intent running.**

- **Devices and lanes.**
  - `feed.json` lists the devices.
  - A lane is a session on one device, 1:1 today. Nothing may assume a lane *is* a device.
  - Every lane carries a `node` (default `viviserv`).
- **Lane state machine:** idle → starting → running → stopping → error, with a `revision`.
- **The revision bumps when, and only when, what the lane outputs or its state changes:**
  - start, stop, tune;
  - a supervisor restart;
  - moving to error;
  - `begin_navigation`.

  A recorder starting on a lane does not bump it.
- **Epoch.** Every feed start gets a new random `feed_epoch`.
- **Commands** carry an `op_id` and the precondition `(feed_epoch, lane_revision)`.
  - A mismatch gets 409.
  - The `op_id` is registered before the side effect begins, and a retry returns the first result (or "in progress").
  - Ids are kept for the last 1000 ops or 24 h. A retry of an evicted id is still caught by the revision precondition.
- **409 rule for control:** re-read the lane, re-evaluate intent, then reissue once with a new `op_id`, or give up with a clear error. A UI request never sees a raw 409.
- **Supervision.** The feed restarts a crashed ffmpeg for its current intent, with backoff (a new revision and stream id). After a limit it stops and reports `error`.
- **Adoption.** On startup, control takes the feed's actual state as the truth and rewrites its own intent to match. It never pushes stale intent onto the hardware.
- **Unreachable feed.** `RemoteFeed` uses 1–2 s timeouts and a status cache. A feed that is down or hung reads `unknown`, and tunes are refused with 503.
- **Lock order.** The feed never calls control while holding its own lock; it emits events instead.
- **Roku generation** is `<capture id>.<revision>`, kept by the feed.
  1. Control calls `begin_navigation(op_id)` before the first key; the revision bumps.
  2. Control navigates.
  3. Control calls `end_navigation(op_id, selection)`; the feed stores the verified selection on the lane.

  A control crash mid-navigation leaves the lane `navigating` with the generation already bumped. The restarted control ends the navigation with `selection: unknown`. Nothing needs to be durable.

## Who writes each state file

Exactly one writer process per file.

| File | Writer |
|---|---|
| `recordings.json`, `tuner_locks.json`, `playback_state.json` (always-on intent), `channels.json` (scan results), `epg.json`, `yttv_epg.json`, `lineup_state.json`, `roku_state/` | control |
| `state/feed/recordings-journal.jsonl` (git-ignored; one place for both builds, so a rollback reads it), lane playlists and segments in `streams/` | feed |

## The failure matrix

Each row is run with `scripts/restart_drill.py`, on air, with all four of these at once:
- VizelVision airing YouTube TV from the Roku lane, holding its lock;
- a Watch-page player on the antenna;
- an internet stream playing;
- a recording running.

| Row | Failure | Expected |
|---|---|---|
| A1 | `sudo systemctl restart tv-thing` ×3 | VizelVision stays on air: no test card, no fallback, no lease lost. Streams, relays and recordings continue. Generations unchanged. |
| A2 | `kill -9` control | As A1 |
| A3 | SIGSTOP control for 30 s, then SIGCONT | As A1; the API returns 503 or times out meanwhile |
| A4 | SIGSTOP control for 60 s | VizelVision fails the input once its 45 s grace runs out, and recovery puts it back (documented, not a failure) |
| A5 | OOM in control | systemd kills only control's cgroup; feed ffmpeg untouched |
| B1 | Kill control during a tune | No duplicate retune; after adoption the lane's revision and intent agree |
| B2 | Kill control during Roku navigation | Lane left `navigating`; generation already bumped; selection ends `unknown`; VizelVision's lease fails closed and recovers |
| B3 | Kill control twice during a finalize | File, history entry and cleanup each happen once |
| B4 | Control down past a recording's scheduled end | Marked missed, never started |
| B5 | A stale command (old revision or pre-restart epoch) | 409; lane unchanged; the 409 rule applies |
| C1 | Restart the feed during a recording | Epoch and generations change; the journal records the end reason; clients recover (`--expect-change`) |
| C2 | SIGSTOP the feed | Control reports lanes `unknown` within 2 s, refuses tunes, and its API threads don't block |
| C3 | Feed unreachable (block `:8086`) | Control reports lanes `unknown`; nothing treated as free |
| C4 | Control down; a new viewer opens a relay with no cached upstream | Fails until control is back (expected) |
| D1 | `apachectl graceful` during playback | HLS through `:8080` and `/tv/` never fails; MJPEG and server-sent events survive or reconnect |
| D2 | Control stopped; `GET /api/v1/status` via `:8080` and `/tv/` | 503 with a JSON body and a JSON content type |
| D3 | `GET /internal/…` and `/feed/v1/…` via `:8080` and `/tv/` | 404 |
| D4 | Any playlist served through `:8080` or `/tv/` | Never contains `:8086`, `:8087` or `127.0.0.1` |

## Running the drill

```
scripts/restart_drill.py --watch-only --seconds 30          # read-only: is everything flowing now?
scripts/restart_drill.py --command 'sudo systemctl restart tv-thing'
scripts/restart_drill.py --expect-change --command 'sudo systemctl restart tv-thing-feed'
```

- It only reads TV Thing's and VizelVision's APIs and playlists. The failure is whatever `--command` runs.
- Never pass `--command` on production without the owner's OK.
- It exits non-zero and names the broken invariant.

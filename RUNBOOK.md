# TV Thing runbooks

## Phase 1: deploying the feed seam

This puts branch `feed-seam` live through the AGENTS.md gate. It is a core stream stack change (process supervision, the recorders, the tuners' scans), so it is gated. The plan is `~/.claude/plans/can-we-come-up-swift-hinton.md`; the rules it must keep are in `INVARIANTS.md`.

**The same deploy as one script:** `scripts/deploy-feed-seam.sh`. It runs these steps in order and rolls back by itself on any failure, using `reset --keep`.

```bash
bash /home/vivian/tv_thing-feed-seam/scripts/deploy-feed-seam.sh --check
```

`--check` runs the prechecks alone: it stops nothing and needs no sudo.

Run it inside `tmux` all the same. A dropped SSH session no longer stops it (it ignores the hang-up and keeps logging to the file), but tmux lets you watch it to the end.

```bash
sudo bash /home/vivian/tv_thing-feed-seam/scripts/deploy-feed-seam.sh
```

This is the deploy. It refuses while VizelVision has something on air or prepared. Pass `--on-air` to go ahead anyway; what was on air is put back at the end. The steps below do the same by hand.

**What it costs.**
- **VizelVision is off air for about 20–35 minutes.** The Roku navigations take most of it: up to 150 s each, six of them.
- **TV Thing is down** from step 3 to step 7, about 10–20 minutes. Nothing records meanwhile.
- **The restart budget:** this is the plan's first feed-dropping restart. Phase 1 is still one process, so this restart, like any restart today, drops every feed.

**When.** Not during a YouTube TV broadcast. With TV Thing stopped, VizelVision has no antenna to fall back on (2026-10-04, SNF). Sunday Night Football runs to about 23:30. Nothing on air, no recording running and no VCR timer due in the next hour is best. The prechecks report all three.

**What changes in production.**
- **Code only.**
  - New files: `feed.json`, `feed_node.py`.
  - Changed: `server.py`, `roku_lane.py`, `internet_streams.py`, `conftest.py`, `.gitignore`, the tests, `INVARIANTS.md`.
  - `tv-thing.service` is unchanged, so no `daemon-reload`.
- **A new state file:** `~/tv_thing/state/feed/recordings-journal.jsonl`, the recorders' journal. The split keeps it there too, so a rollback reads what the feed wrote. `state/` is git-ignored.
- **The API:** `/api/v1/status` gains `node`, `feed_epoch`, and per lane `lifecycle`, `navigating` and `stream.revision`. Nothing is removed or renamed, and `test_api_contract.py` is unchanged.
- **The Roku generation** becomes `<capture id>.<revision>`, where the old form was `<capture id>.<serial>`. VizelVision only compares it for equality, and the restart changes the capture id anyway.

### At a glance

| Step | What | Air | TV Thing |
|---|---|---|---|
| 0–1 | Shell setup, prechecks (read-only) | on | up |
| 2 | Record what is on air; stop VizelVision web and master | **off** | up |
| 3 | Stop production | off | **down** |
| 4–6 | Candidate on :8098: 3 digital retunes, Phase 1 checks, a Roku navigation during a recording, 3 Roku navigations | off | down |
| 7 | Promote, start production (the one restart) | off | up |
| 8 | Production gate: 3 digital retunes, 3 Roku navigations | off | up |
| 9 | VizelVision back; restore what was on air | **on** | up |
| 10 | The drill (`--watch-only`) | on | up |

If any check fails: **stop, do not promote**, and roll back (see Rollback). Never loosen a limit to get a pass (AGENTS.md rule 5).

### 0. The shell

Run everything as `vivian`, in one terminal. Only the system units need `sudo`; VizelVision's are user units and don't.

```bash
set -o pipefail                       # so `| tee` never hides a failure
TV=/home/vivian/tv_thing
WT=/home/vivian/tv_thing-feed-seam
BRANCH=feed-seam
PY=/home/vivian/miniconda3/bin/python3
STAMP=$(date +%Y%m%d-%H%M%S)
LOG=/home/vivian/feed-seam-$STAMP
STAGE=/home/vivian/tv_thing-candidate.feed-seam-$STAMP
CAND=tv-thing-candidate-$STAMP
P=http://127.0.0.1:8080               # production
C=http://127.0.0.1:8098               # the candidate
VV=http://127.0.0.1:6970
KEY=$("$PY" -c "import json; print(json.load(open('/home/vivian/vizelVision/config.master.json'))['server']['api_key'])")
BASE=$(git -C "$TV" rev-parse --short HEAD)
mkdir -p "$LOG"

j() { curl -s --max-time 5 "$1" | "$PY" -c "import json,sys; d=json.load(sys.stdin); print($2)"; }
ready() { [ "$(j "$1/api/v1/status" "bool(d['lanes']['$2']['stream'].get('ready'))")" = True ]; }
wait_ready() {                        # url lane seconds
    local deadline=$((SECONDS + $3))
    until ready "$1" "$2"; do [ $SECONDS -ge $deadline ] && { echo "NOT READY: $2 at $1"; return 1; }; sleep 2; done
    echo "ready: $2 at $1"
}
```

### 1. Prechecks (read-only; any time)

```bash
# The branch passes the fast suite. Never bare pytest: test_integration.py retunes production.
(cd "$WT" && "$PY" -m pytest -q -m "not slow and not api and not hardware and not resource" --ignore=test_integration.py)

# Production is on master at the branch's base, and the branch fast-forwards it.
git -C "$TV" rev-parse --abbrev-ref HEAD                      # master
git -C "$TV" merge-base --is-ancestor HEAD "$BRANCH" && echo "fast-forward ok from $BASE"

# Production's uncommitted work (today: roku_app/) touches none of the branch's files.
# This must print nothing:
comm -12 <(git -C "$TV" status --porcelain | awk '{print $NF}' | sort) \
         <(git -C "$TV" diff --name-only HEAD "$BRANCH" | sort)

# Nothing recording, and no timer due soon.
j $P/api/v1/recordings "len(d['active_recordings']), [(t.get('start'), t.get('channel_id')) for t in d['timers']]"

# The HD PVR is plugged in, and production's Roku capture runs on it.
ls /dev/v4l/by-id/usb-AMBA_Hauppauge_HD_PVR_00A4E5AC-video-index0
ready $P roku && echo "Roku capture running"

# What is on air now, and whether it is held.
j $VV/v1/master "d.get('on_air'), d.get('prepared')"
curl -s -H "X-API-Key: $KEY" $VV/v1/queue | "$PY" -c "import json,sys; d=json.load(sys.stdin); print('hold', d.get('hold'), 'paused', d.get('paused'))"

# The drill passes on today's production (read-only, 30 s).
"$PY" "$WT/scripts/restart_drill.py" --watch-only --seconds 30 | tee "$LOG/drill-before.log"
```

### 2. Record what is on air, and pause VizelVision

**Stop both web and master.** Master holds the tuner locks. Web holds its own Roku lock (`RokuHold`) and its Up next would tune mid-gate. Stopping master alone leaves both of those. The ad detector and MediaMTX never talk to TV Thing, so they keep running.

```bash
curl -s $VV/v1/master > "$LOG/master-before.json"
curl -s -H "X-API-Key: $KEY" $VV/v1/queue > "$LOG/queue-before.json"
cp -p "$TV/streams/tuner_locks.json" "$LOG/" 2>/dev/null
systemctl --user stop vizelvision-web.service vizelvision-master.service

# Their locks expire within 90 s (web releases its Roku lock as it stops).
until [ "$(j $P/api/v1/tuner/lock "all(v is None for v in d['locks'].values())")" = True ]; do sleep 2; done; echo "locks clear"
```

### 3. Stop production

```bash
sudo systemctl stop tv-thing.service
pgrep -u vivian -af "$TV/(dvb_source|sports_guide_video)|$TV/streams" || echo "nothing of production left"
```

### 4. Stage the candidate, and start it on the staging ports

The candidate is production's tree as it runs: its uncommitted files, Roku calibration and lineup state, with the branch's files laid over it. That is exactly what promotion leaves running.

| | Production | Candidate |
|---|---|---|
| HTTP | `:8080` on 127.0.0.1, 192.168.1.98, 10.0.0.98 | `:8098`, 127.0.0.1 only |
| DVB source UDP | 18320 | 18420 |
| Recording taps | 18384+ | 18484+ |
| Roku verifier tap | 18370 | 18470 |
| Streams | `~/tv_thing/streams` | `$STAGE/streams` |
| Recordings | `/mnt/sda1/media/TV Recordings` | `$STAGE/recordings` |
| State (the journal) | `~/tv_thing/state` | `$STAGE/state` |
| PSIP guide sweeps | on | off (`TV_THING_EPG=0`), so nothing sweeps the tuner mid-gate |
| Unit | `tv-thing.service` | `$CAND`: the unit's limits (`TimeoutStopSec=5` included), and a one-hour cap (`RuntimeMaxSec=3600`) |

```bash
mkdir -p "$STAGE/streams" "$STAGE/recordings" "$STAGE/state"
rsync -a --exclude /.git --exclude /streams --exclude /state --exclude __pycache__ "$TV/" "$STAGE/"
git -C "$TV" diff --name-only HEAD "$BRANCH" > "$LOG/branch-files.txt"
rsync -a --files-from="$LOG/branch-files.txt" "$WT/" "$STAGE/"

sudo systemd-run --unit="$CAND" --collect --uid=vivian --gid=vivian \
    -p WorkingDirectory="$STAGE" -p MemoryMax=1800M -p CPUQuota=800% -p TasksMax=192 \
    -p LimitFSIZE=16M:16G -p OOMPolicy=continue -p RuntimeMaxSec=3600 -p TimeoutStopSec=5 \
    --setenv=PORT=8098 --setenv=TV_THING_BIND=127.0.0.1 --setenv=TV_THING_BIND_LOOPBACK=1 \
    --setenv=TV_THING_STREAMS_DIR="$STAGE/streams" --setenv=TV_THING_RECORDINGS_DIR="$STAGE/recordings" \
    --setenv=TV_THING_STATE_DIR="$STAGE/state" \
    --setenv=TV_THING_TV_UDP_PORT=18420 --setenv=TV_THING_RECORD_TAP_PORT=18484 \
    --setenv=TV_THING_ROKU_TAP_PORT=18470 --setenv=TV_THING_EPG=0 \
    --setenv=TV_THING_VIDEO_ENCODER=cpu --setenv=TV_THING_MAX_GPU_TEMP_C=90 \
    --setenv=TV_THING_RECORD_MIN_FREE_GB=1 --setenv=PYTHONUNBUFFERED=1 \
    "$PY" "$STAGE/server.py"

wait_ready $C digital 90 && wait_ready $C roku 60 && sleep 20
CPID=$(systemctl show -p MainPID --value "$CAND")
journalctl -u "$CAND" --no-pager | grep -E 'Reaped|Traceback|FeedConfigError' || echo "clean start"
```

### 5. Gate the candidate

Each gate passes when it exits 0. All three must pass.

```bash
# a. Three real digital retunes. Covers latency, MPEG-TS integrity, segment size, duration and
#    write rate, CPU/RSS, GPU utilization and temperature.
"$PY" "$STAGE/stream_sanity_check.py" --url $C --server-pid "$CPID" --retunes 3 \
    | tee "$LOG/gate-candidate-digital.log"

# b. A Roku navigation while the VCR records the digital lane, with no task refused by TasksMax.
#    This exercises the feed's recorders and journal, and a navigation's begin and end.
"$PY" "$STAGE/scripts/task_headroom_check.py" --url $C \
    --cgroup "/sys/fs/cgroup/system.slice/$CAND.service" | tee "$LOG/headroom-candidate.log"

# c. Three Roku navigations (ESPN, NBC, ESPN) on one continuous capture, under the Roku limits.
#    A gate locks every lane, and b's cancelled recording lets go of the digital lane's lock
#    only on the VCR's next tick, so wait for every lock to clear first.
until [ "$(j $C/api/v1/tuner/lock "all(v is None for v in d['locks'].values())")" = True ]; do sleep 2; done
"$PY" "$STAGE/stream_sanity_check.py" --url $C --server-pid "$CPID" \
    --roku-target youtube-tv:ESPN --roku-target youtube-tv:NBC --retunes 3 \
    | tee "$LOG/gate-candidate-roku.log"
```

Then the Phase 1 checks, each a few seconds:

```bash
# The feed's state is in the status.
j $C/api/v1/status "d['node'], d['feed_epoch'], {n: (l['lifecycle'], l['navigating'], l['stream'].get('revision')) for n, l in d['lanes'].items()}"
#   expect: viviserv, a 16-hex epoch, digital and roku 'running', navigating False everywhere

# The recorder journal has b's recording, from start to end.
cat "$STAGE/state/feed/recordings-journal.jsonl"
#   expect: a "start" and an "end" per recorded piece (normally one), each pair with the same id;
#   the last end's reason is "stopped" (b cancels it) and its bytes > 0

# A re-tune of what the Roku shows moves nothing: the generation stays (invariant 5).
G1=$(j $C/api/v1/status "d['lanes']['roku']['stream']['generation']")
curl -s -X POST -H 'Content-Type: application/json' \
    -d '{"source": "youtube-tv", "channel_id": "ESPN", "navigate": false}' $C/api/v1/tune \
    | "$PY" -c "import json,sys; d=json.load(sys.stdin); print(d.get('reused'), d['stream']['generation'])"
echo "before: $G1"
#   expect: True and the same generation as before

# The signal meter and a DX scan go through the feed client. The scan stops the digital lane
# for a moment, and the always-on keeper brings it back.
j $C/api/v1/signal "d['ok'], d['signal']"
RF=$(j $C/api/v1/status "d['lanes']['digital']['selection']['rf_channel']")
curl -s -X POST -H 'Content-Type: application/json' -d "{\"rf_channel\": $RF}" $C/api/v1/scan/channel \
    | "$PY" -c "import json,sys; d=json.load(sys.stdin); print(d.get('ok'), d.get('locked'), d.get('snr_db'))"
wait_ready $C digital 60

# The internet relay asks control's resolver (only meaningful while the stream is live upstream).
curl -s --max-time 10 $C/stream/internet/pentulive/index.m3u8 | head -2
curl -s --max-time 10 $C/stream/internet/cabride/index.m3u8 | head -2
#   expect: #EXTM3U from each that is live
```

Leave out the band rescan (`/api/rescan/stream`) here. A partial sweep rewrites the candidate's `channels.json` with only the channels it found. The sweep's code path is covered by `test_a_rescan_sweeps_through_the_feed_and_lets_go_of_the_tuner`.

### 6. Stop the candidate

```bash
journalctl -u "$CAND" --no-pager > "$LOG/candidate.journal"
cp -p "$STAGE/state/feed/recordings-journal.jsonl" "$LOG/candidate-journal.jsonl"
sudo systemctl stop "$CAND.service"
pgrep -u vivian -af "$STAGE/" && echo "LEFT PROCESSES BEHIND: stop here" || echo "candidate gone, nothing left"

# The HD PVR still answers. Selecting its input is what hangs when it has hung (--get-input does not).
timeout -k 2 5 v4l2-ctl --device /dev/v4l/by-id/usb-AMBA_Hauppauge_HD_PVR_00A4E5AC-video-index0 --set-input=0 \
    && echo "HD PVR answers" || echo "HD PVR HUNG: reset it (below) before going on"
```

**If the HD PVR has hung** (2026-10-25, after a candidate run): production's capture opens it to no data ("could not find codec parameters"), and VizelVision cannot air YouTube TV. `sudo dmesg | grep hdpvr` says whether its driver bound.

- **Unplug its power for 10 s**, then press its button if its light doesn't come on. A USB re-plug alone did not bring it back that night.
- **If the driver still doesn't bind** ("unexpected answer of status request, len -110"), move it to another USB port directly on the computer. Port 2-12 had turned bad; it came back on 2-3.
- TV Thing picks it up again within seconds once `/dev/v4l/by-id/usb-AMBA_Hauppauge_HD_PVR_*` exists.

A USB re-plug, if you want to try one first (the port is the one `lsusb -t` shows it on):

```bash
P=$(for d in /sys/bus/usb/devices/*; do [ "$(cat $d/idVendor 2>/dev/null):$(cat $d/idProduct 2>/dev/null)" = 2040:4902 ] && basename $d; done); echo 0 | sudo tee /sys/bus/usb/devices/$P/authorized; sleep 3; echo 1 | sudo tee /sys/bus/usb/devices/$P/authorized
```

### 7. Promote, and start production (the one restart)

```bash
git -C "$TV" merge --ff-only "$BRANCH"            # roku_app/'s uncommitted work stays as it is
git -C "$TV" log --oneline -1
sudo systemctl start tv-thing.service
wait_ready $P digital 120 && wait_ready $P roku 60
ss -ltnH 'sport = :8080'                          # 127.0.0.1, 192.168.1.98 and 10.0.0.98 (and tailscale's, not ours)
sleep 20
PPID_TV=$(systemctl show -p MainPID --value tv-thing)
journalctl -u tv-thing --since -5min --no-pager | grep -E 'Reaped|Traceback|FeedConfigError' || echo "clean start"
```

### 8. Gate production

These are the same gates, against `:8080`. VizelVision is still stopped, so it cannot take a lock mid-gate.

```bash
"$PY" "$TV/stream_sanity_check.py" --url $P --server-pid "$PPID_TV" --retunes 3 \
    | tee "$LOG/gate-production-digital.log"
"$PY" "$TV/stream_sanity_check.py" --url $P --server-pid "$PPID_TV" \
    --roku-target youtube-tv:ESPN --roku-target youtube-tv:NBC --retunes 3 \
    | tee "$LOG/gate-production-roku.log"
awk '$1 == "max" {print "TasksMax refusals:", $2}' /sys/fs/cgroup/system.slice/tv-thing.service/pids.events   # expect 0
j $P/api/v1/status "d['node'], d['feed_epoch'], {n: (l['lifecycle'], l['navigating']) for n, l in d['lanes'].items()}"
```

If either gate fails, roll back now (B below).

### 9. VizelVision back, and the air restored

```bash
systemctl --user start vizelvision-master.service vizelvision-web.service
sleep 15
# Web starts Up next paused ("Review the queue and resume after startup").
curl -s -X POST -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d '{}' $VV/v1/queue/resume >/dev/null
j $VV/v1/master "d.get('on_air')"
```

If something was on air in step 2 and isn't back, queue it as Now, with its hold:

```bash
"$PY" - "$LOG" > "$LOG/restore.json" <<'EOF'
import json, sys
log = sys.argv[1]
on_air = json.load(open(f"{log}/master-before.json")).get("on_air")
hold = json.load(open(f"{log}/queue-before.json")).get("hold") or {}
if not on_air:
    sys.exit("nothing was on air: resuming Up next was enough")
body = {"target": {"source_id": on_air["source_id"], "channel_id": on_air["channel_id"]},
        "release_type": "now", "resume": True}
if hold.get("until"):
    body["until"] = {"kind": "time", "at": hold["until"]}
elif hold.get("game"):
    body["until"] = {"kind": "game", **hold["game"]}
print(json.dumps(body))
EOF
cat "$LOG/restore.json"                           # check it, then:
curl -s -X POST -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d @"$LOG/restore.json" $VV/v1/queue
```

A YouTube TV target takes a Roku navigation, 1–2 minutes, before it is on air. Wait for `on_air` to show it before step 10.

### 10. The drill

This is read-only: two minutes of everything flowing on the new build. Every lane's generation holds, every playlist advances, VizelVision's programme stays on air, and no new input failure appears.

```bash
"$PY" "$TV/scripts/restart_drill.py" --watch-only --seconds 120 | tee "$LOG/drill-after.log"
```

It must exit 0. Then clean up, and keep `$LOG`:

```bash
rm -rf "$STAGE/streams" "$STAGE/recordings"
echo "Phase 1 live: $(git -C "$TV" rev-parse --short HEAD). Logs: $LOG"
```

### Rollback

**A. Before promotion** (a candidate gate failed). Production's code is unchanged.

```bash
journalctl -u "$CAND" --no-pager > "$LOG/candidate.journal"
sudo systemctl stop "$CAND.service"
sudo systemctl start tv-thing.service
wait_ready $P digital 120
```

Then step 9.

**B. After promotion** (a production gate or the drill failed).

```bash
journalctl -u tv-thing --since -20min --no-pager > "$LOG/production.journal"
sudo systemctl stop tv-thing.service
git -C "$TV" reset --keep "$BASE"
sudo systemctl start tv-thing.service
wait_ready $P digital 120 && wait_ready $P roku 60
```

Then step 9.

- **Use `--keep`, never `--hard`.** `reset --keep` restores only the files that differ between the two commits and refuses if one of them has local changes. The uncommitted `roku_app/` work in the checkout survives it; `--hard` would destroy it. The older `deploy-*.sh` scripts roll back with `--hard`.
- **What the old build ignores:** the `state/feed/` journal left behind, and the new status fields it no longer sends.
- **What it cannot read:** a recording that started on the new build. The prechecks make sure none is running.

### Optional: rehearse a restart under the drill

This costs **a second feed drop**, so do it off air only. In Phase 1, `tv-thing` is still the feed as well as control. So this is matrix row C1 (`--expect-change`): every lane and VizelVision must be back by the end. The Roku lane comes back by renavigating: until Phase 2, its selection lives in the same process.

```bash
"$PY" "$TV/scripts/restart_drill.py" --expect-change --seconds 240 \
    --command 'sudo systemctl restart tv-thing' | tee "$LOG/drill-restart.log"
```

Row A1, a restart with nothing changing, only passes after Phase 2. On Phase 1 it fails by design.

## Phase 2: the cut-over to two services

`scripts/deploy-feed-split.sh` runs all of this, gated: `--check` first (no sudo, nothing stopped), then `sudo bash …/deploy-feed-split.sh`, then `--rollback` if it is ever needed. The steps by hand are below. `scripts/staged_split_check.sh` (31 checks, no hardware) covers the roles, `RemoteFeed`, `/internal/`, the front door's routes and the restart behaviour. What only the deploy can cover is the gate with real hardware, which needs production stopped, like any gate.

**What changes.** Today `tv-thing` is one process on `:8080`. After:

| Unit | Process | Listens |
|---|---|---|
| `tv-thing-feed` (`deploy/systemd/tv-thing-feed.service`) | `feed_server.py` | 127.0.0.1:8086 |
| `tv-thing` (`deploy/systemd/tv-thing.service`) | `server.py`, control | 127.0.0.1:8087 |
| Apache (`deploy/apache/tv-thing-front-door.conf`) | the front door | 127.0.0.1, 192.168.1.98 and 10.0.0.98 on :8080 |

- Every client URL stays the same.
- `viviserv-proxy.conf` is not touched: its `/tv/` already proxies to `127.0.0.1:8080`, which becomes the front door.
- The recorders' journal stays in `state/feed/`, so either build reads what the other wrote.
- The repo's root `tv-thing.service` stays the one-process unit, the rollback's. The installed `tv-thing.service` becomes control's (`deploy/systemd/`), so an older deploy script that copies the root unit would undo the split.

**The restart budget.** One feed drop at the cut-over, and one full Apache restart for its new `Listen` lines (a second or two for backup-thing and the other sites). After that, `sudo systemctl restart tv-thing` drops nothing, and Apache only ever needs a graceful reload.

### Before

1. Merge `feed-split` to `master` only as part of this cut-over. The checkout is what runs.
2. Gate the build: AGENTS.md's gate, with the candidate run as **two** units (feed on :8091, control on :8092) behind a private Apache on :8098 running the production routes. The gates run through that front door. They measure both services' process trees as one (`--server-pid` twice), and watch both cgroups' TasksMax. The drill restarts, then kills, the candidate's control.
3. Off air, with VizelVision's web and master stopped, as in Phase 1. No recording running or due.

### The cut-over

```bash
sudo install -m 644 ~/tv_thing/deploy/systemd/tv-thing-feed.service ~/tv_thing/deploy/systemd/tv-thing.service /etc/systemd/system/
sudo install -D -m 644 ~/tv_thing/deploy/apache/apache2-front-door.conf /etc/systemd/system/apache2.service.d/tv-thing-front-door.conf
sudo install -m 644 ~/tv_thing/deploy/apache/tv-thing-front-door.conf /etc/apache2/sites-available/
sudo a2ensite tv-thing-front-door
sudo apache2ctl configtest
sudo systemctl daemon-reload
sudo systemctl stop tv-thing
ss -ltn 'sport = :8080'                        # only tailscale0's
sudo systemctl enable --now tv-thing-feed
sudo systemctl start tv-thing
sudo systemctl restart apache2                 # the one full restart: new Listen lines
curl -s http://127.0.0.1:8080/api/v1/status | head -c 200; curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1/tv/
```

Then gate production through `:8080` (`--server-pid` for `tv-thing-feed` and for `tv-thing`), bring VizelVision back and resume Up next, as in Phase 1.

### The proof

These are the drill rows that fail by design before the split, and must pass now, with VizelVision on air:

```bash
sudo ~/tv_thing/scripts/restart_drill.py --command 'systemctl restart tv-thing'      # A1
sudo ~/tv_thing/scripts/restart_drill.py --command 'systemctl kill -s KILL tv-thing' # A2
```

The script does not roll back on a failed proof. By then the split has passed its gates, and one process fails the drill by design. It says what failed.

### Rollback

`sudo bash ~/tv_thing/scripts/deploy-feed-split.sh --rollback` puts back the one process on the code that is checked out, which reads the journal, `finish_as` and the Roku's navigation record. Every stream drops once. By hand:

```bash
sudo systemctl stop tv-thing tv-thing-feed
sudo systemctl disable tv-thing-feed
sudo a2dissite tv-thing-front-door
sudo rm /etc/apache2/sites-available/tv-thing-front-door.conf /etc/systemd/system/apache2.service.d/tv-thing-front-door.conf /etc/systemd/system/tv-thing-feed.service
sudo install -m 644 ~/tv_thing/tv-thing.service /etc/systemd/system/tv-thing.service
sudo systemctl daemon-reload
sudo apache2ctl configtest && sudo systemctl restart apache2    # frees :8080
sudo systemctl start tv-thing                  # one process, binding :8080 itself
```

The all-in-one role (`TV_THING_ROLE` unset) keeps binding the LAN itself, as it always has, for the weeks the rollback stays possible.

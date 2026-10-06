#!/bin/bash
# Move the source Roku from vizelVision into TV Thing's Roku lane (2026-10-06 plan).
#
#   sudo bash /home/vivian/tv_thing-roku/scripts/deploy-roku-migration.sh          # deploy
#   bash /home/vivian/tv_thing-roku/scripts/deploy-roku-migration.sh --check       # prechecks only, no sudo
#
# Run it when nothing is on air: it stops vizelVision and TV Thing. AGENTS.md's gate:
#   1. prechecks (nothing on air, prepared or recording; clean checkouts at the expected commits)
#   2. stop vizelVision's stack (it holds the HD PVR) and production tv-thing.service
#   3. candidate on :8098 (own UDP ports, streams and recordings dirs, the production unit's
#      limits, a one-hour runtime cap): 3 digital retunes, 3 Roku navigations, then a digital
#      and a Roku recording at once
#   4. promote TV Thing (fast-forward master, roku_state, the unit file) and gate production
#   5. promote vizelVision (fast-forward master, config without the Roku, MediaMTX) and run a
#      live NBC prepare/take/fallback through master control
# Any failure after step 2 rolls both back and restarts the old services.
set -uo pipefail

TV=/home/vivian/tv_thing
TV_WORKTREE=/home/vivian/tv_thing-roku
TV_BASE=322b16a
TV_BRANCH=roku-lane
VV=/home/vivian/vizelVision
VV_WORKTREE=/home/vivian/vizelVision-roku
VV_BASE=6ccafd3
VV_BRANCH=roku-to-tv-thing
PY=/home/vivian/miniconda3/bin/python3
DEVICE=/dev/v4l/by-id/usb-AMBA_Hauppauge_HD_PVR_00A4E5AC-video-index0
STAMP=$(date +%Y%m%d-%H%M%S)
LOGDIR=/home/vivian/roku-migration-$STAMP
STAGE=/home/vivian/tv_thing-candidate.roku-$STAMP
# A name of its own each run: the server exits 1 when signalled, so systemd keeps a
# stopped candidate loaded as failed, and a fixed name refused the second run.
CANDIDATE=tv-thing-candidate-$STAMP
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

TV_PROMOTED=0
VV_PROMOTED=0
STOPPED=0

as_vivian() {
    if [ "$(id -un)" = vivian ]; then env XDG_RUNTIME_DIR=/run/user/1000 "$@"
    else sudo -u vivian -H env XDG_RUNTIME_DIR=/run/user/1000 "$@"; fi
}
git_v() { as_vivian git "$@"; }  # root's git refuses vivian's repositories ("dubious ownership")
user_systemctl() { as_vivian systemctl --user "$@"; }
say() { echo "[$(date +%H:%M:%S)] $*"; }
fail() { say "FAILED: $*"; [ "$STOPPED" = 1 ] && rollback; exit 1; }
json() { curl -s --max-time 5 "$1" | "$PY" -c "import json,sys; d=json.load(sys.stdin); print($2)"; }

precheck() {
    say "Prechecks"
    local on_air prepared
    on_air=$(json http://127.0.0.1:6970/v1/master "json.dumps(d.get('on_air'))") || fail "vizelVision master is not answering"
    prepared=$(json http://127.0.0.1:6970/v1/master "json.dumps(d.get('prepared'))")
    [ "$on_air" = "null" ] || fail "something is on air in vizelVision: $on_air"
    [ "$prepared" = "null" ] || fail "something is prepared in vizelVision: $prepared"
    local recording
    recording=$(json http://127.0.0.1:8080/api/v1/recordings "len(d.get('active_recordings') or ([d['active']] if d.get('active') else []))") \
        || fail "TV Thing is not answering"
    [ "$recording" = "0" ] || fail "TV Thing is recording"
    for repo in "$TV" "$VV"; do
        [ -z "$(git_v -C "$repo" status --porcelain)" ] || fail "$repo has uncommitted changes"
    done
    [ "$(git_v -C "$TV" rev-parse --short HEAD)" = "$TV_BASE" ] || fail "$TV is not at $TV_BASE"
    [ "$(git_v -C "$VV" rev-parse --short HEAD)" = "$VV_BASE" ] || fail "$VV is not at $VV_BASE"
    git_v -C "$TV" merge-base --is-ancestor "$TV_BASE" "$TV_BRANCH" || fail "$TV_BRANCH does not descend from $TV_BASE"
    git_v -C "$VV" merge-base --is-ancestor "$VV_BASE" "$VV_BRANCH" || fail "$VV_BRANCH does not descend from $VV_BASE"
    for repo in "$TV_WORKTREE" "$VV_WORKTREE"; do
        [ -z "$(git_v -C "$repo" status --porcelain)" ] || fail "$repo has uncommitted changes"
    done
    [ -d "$TV_WORKTREE/roku_state/templates-720p" ] || fail "the Roku's calibration templates are missing"
    [ -e "$DEVICE" ] || fail "the HD PVR is not connected ($DEVICE)"
    say "Prechecks passed"
}

wait_for() {  # seconds, description, command...
    local limit=$1 what=$2; shift 2
    local deadline=$((SECONDS + limit))
    until "$@" >/dev/null 2>&1; do
        [ $SECONDS -ge $deadline ] && return 1
        sleep 2
    done
    say "$what"
}

locks_clear() { [ "$(json http://127.0.0.1:8080/api/v1/tuner/lock "d['locks'].get('digital') is None and d['locks'].get('analog') is None")" = "True" ]; }
device_free() { ! fuser "$DEVICE" >/dev/null 2>&1; }
lane_ready() {  # url lane
    [ "$(json "$1/api/v1/status" "bool(d['lanes']['$2']['stream'].get('ready'))")" = "True" ]
}

stop_vizelvision() {
    say "Stopping vizelVision (it holds the HD PVR and a TV Thing lock)"
    user_systemctl stop vizelvision-admute-recordings.timer vizelvision-stack.target \
        vizelvision-web.service vizelvision-master.service vizelvision-admute.service vizelvision-mediamtx.service
    STOPPED=1
    wait_for 60 "The HD PVR is free" device_free || fail "the HD PVR is still open"
    wait_for 150 "TV Thing's tuner locks have expired" locks_clear || fail "a tuner lock is still held"
}

start_vizelvision() {
    user_systemctl start vizelvision-stack.target vizelvision-admute-recordings.timer
}

rollback() {
    say "ROLLING BACK"
    journalctl -u "$CANDIDATE" --no-pager > "$LOGDIR/candidate.journal" 2>&1
    systemctl stop "$CANDIDATE.service" 2>/dev/null
    if [ "$VV_PROMOTED" = 1 ]; then
        user_systemctl stop vizelvision-stack.target vizelvision-web.service vizelvision-master.service
        as_vivian git -C "$VV" reset -q --hard "$VV_BASE"
        cp -p "$LOGDIR/config.master.json" "$VV/config.master.json"
    fi
    if [ "$TV_PROMOTED" = 1 ]; then
        systemctl stop tv-thing.service
        as_vivian git -C "$TV" reset -q --hard "$TV_BASE"
        rm -rf "$TV/roku_state"  # the calibration copy: untracked at $TV_BASE
        cp "$LOGDIR/tv-thing.service" /etc/systemd/system/tv-thing.service
        systemctl daemon-reload
    fi
    systemctl start tv-thing.service
    wait_for 120 "TV Thing is back" lane_ready http://127.0.0.1:8080 digital || say "TV Thing did not come back; check journalctl -u tv-thing"
    start_vizelvision
    say "Rolled back to $TV_BASE / $VV_BASE. Logs: $LOGDIR"
}

gate() {  # name, url, pid, args...
    local name=$1 url=$2 pid=$3; shift 3
    say "Gate: $name"
    as_vivian "$PY" "$STAGE/stream_sanity_check.py" --url "$url" --server-pid "$pid" "$@" \
        2>&1 | tee "$LOGDIR/gate-$name.log"
    return "${PIPESTATUS[0]}"
}

# The owner's combined digital + Roku caps (2026-10-06): the Roku gate measures the whole
# server, whose always-on digital lane runs alongside.
ROKU_GATE=(--roku-target youtube-tv:NBC --roku-target youtube-tv:ESPN --roku-target youtube-tv:FOX
           --retunes 3 --dwell-seconds 10 --max-rss-mib 800 --max-cpu-average 260)

recordings_test() {  # url, recordings dir
    local url=$1 dir=$2 now digital
    say "Recording the digital lane and the Roku at once"
    now=$(date +%s)
    digital=$(curl -s -X POST "$url/api/v1/recordings" -H 'Content-Type: application/json' \
        -d '{"mode":"otr","from_live":true}' | "$PY" -c "import json,sys; print(json.load(sys.stdin)['timer']['id'])") \
        || return 1
    curl -s -X POST "$url/api/v1/recordings" -H 'Content-Type: application/json' \
        -d "{\"lane\":\"roku\",\"mode\":\"otr\",\"end\":$((now + 100))}" >/dev/null || return 1
    sleep 105
    curl -s -X POST "$url/api/v1/recordings/cancel" -H 'Content-Type: application/json' -d "{\"id\":\"$digital\"}" >/dev/null
    sleep 30
    as_vivian "$PY" - "$dir" <<'EOF'
import json, subprocess, sys
from pathlib import Path
files = sorted(Path(sys.argv[1]).glob('*.mp4'))
lengths = {}
for f in files:
    probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'json', str(f)],
                           capture_output=True, text=True)
    lengths[f.name] = float(json.loads(probe.stdout)['format']['duration'])
print(json.dumps(lengths, indent=1))
sys.exit(0 if len(lengths) >= 2 and all(v >= 60 for v in lengths.values()) else 1)
EOF
}

# ---------------------------------------------------------------------------
precheck
[ "$CHECK_ONLY" = 1 ] && exit 0
[ "$(id -u)" = 0 ] || { echo "Run with sudo (the unit file and tv-thing.service need root)."; exit 1; }
mkdir -p "$LOGDIR" && chown vivian:vivian "$LOGDIR"
exec > >(tee -a "$LOGDIR/deploy.log") 2>&1
say "Logs in $LOGDIR"
cp /etc/systemd/system/tv-thing.service "$LOGDIR/tv-thing.service"
cp -p "$VV/config.master.json" "$LOGDIR/config.master.json"
cp -p "$TV/streams/tuner_locks.json" "$LOGDIR/" 2>/dev/null

stop_vizelvision
say "Stopping production TV Thing"
systemctl stop tv-thing.service || fail "could not stop tv-thing"

# --- 3. the candidate -------------------------------------------------------------------------
say "Staging the candidate in $STAGE"
as_vivian mkdir -p "$STAGE/streams" "$STAGE/recordings"
as_vivian rsync -a --exclude .git --exclude streams --exclude __pycache__ "$TV_WORKTREE/" "$STAGE/"
systemctl reset-failed 'tv-thing-candidate*' 2>/dev/null
systemd-run --unit="$CANDIDATE" --collect --uid=vivian --gid=vivian \
    -p WorkingDirectory="$STAGE" -p MemoryMax=1800M -p CPUQuota=400% -p TasksMax=192 \
    -p LimitFSIZE=16M:16G -p OOMPolicy=continue -p RuntimeMaxSec=3600 \
    --setenv=PORT=8098 --setenv=TV_THING_BIND=127.0.0.1 --setenv=TV_THING_BIND_LOOPBACK=1 \
    --setenv=TV_THING_STREAMS_DIR="$STAGE/streams" --setenv=TV_THING_RECORDINGS_DIR="$STAGE/recordings" \
    --setenv=TV_THING_TV_UDP_PORT=18420 --setenv=TV_THING_RECORD_TAP_PORT=18484 \
    --setenv=TV_THING_ROKU_TAP_PORT=18470 --setenv=TV_THING_EPG=0 \
    --setenv=TV_THING_VIDEO_ENCODER=cpu --setenv=TV_THING_MAX_GPU_TEMP_C=80 \
    --setenv=TV_THING_RECORD_MIN_FREE_GB=1 --setenv=PYTHONUNBUFFERED=1 \
    "$PY" "$STAGE/server.py" || fail "could not start the candidate"
wait_for 90 "The candidate's digital lane is ready" lane_ready http://127.0.0.1:8098 digital || fail "candidate digital lane"
wait_for 60 "The candidate's Roku capture is ready" lane_ready http://127.0.0.1:8098 roku || fail "candidate Roku capture"
sleep 20
PID=$(systemctl show -p MainPID --value "$CANDIDATE")
gate candidate-digital http://127.0.0.1:8098 "$PID" --retunes 3 || fail "candidate digital gate"
gate candidate-roku http://127.0.0.1:8098 "$PID" "${ROKU_GATE[@]}" || fail "candidate Roku gate"
recordings_test http://127.0.0.1:8098 "$STAGE/recordings" | tee "$LOGDIR/recordings.log"
[ "${PIPESTATUS[0]}" = 0 ] || fail "candidate recordings"
journalctl -u "$CANDIDATE" --no-pager > "$LOGDIR/candidate.journal" 2>&1
systemctl stop "$CANDIDATE.service"

# --- 4. promote TV Thing and gate production -----------------------------------------------------
say "Promoting TV Thing"
TV_PROMOTED=1
as_vivian git -C "$TV" merge -q --ff-only "$TV_BRANCH" || fail "could not fast-forward $TV"
as_vivian rsync -a "$TV_WORKTREE/roku_state/" "$TV/roku_state/" || fail "could not copy roku_state"
cp "$TV/tv-thing.service" /etc/systemd/system/tv-thing.service && systemctl daemon-reload
systemctl start tv-thing.service || fail "tv-thing did not start"
wait_for 120 "Production's digital lane is ready" lane_ready http://127.0.0.1:8080 digital || fail "production digital lane"
wait_for 60 "Production's Roku capture is ready" lane_ready http://127.0.0.1:8080 roku || fail "production Roku capture"
sleep 20
PID=$(systemctl show -p MainPID --value tv-thing)
gate production-digital http://127.0.0.1:8080 "$PID" --retunes 3 || fail "production digital gate"
gate production-roku http://127.0.0.1:8080 "$PID" "${ROKU_GATE[@]}" || fail "production Roku gate"

# --- 5. promote vizelVision and take the Roku to master ----------------------------------------
say "Promoting vizelVision"
VV_PROMOTED=1
as_vivian git -C "$VV" merge -q --ff-only "$VV_BRANCH" || fail "could not fast-forward $VV"
as_vivian "$PY" - "$VV/config.master.json" <<'EOF' || fail "could not update config.master.json"
import json, os, sys
path = sys.argv[1]
config = json.load(open(path))
# The Roku is TV Thing's: its address, capture, calibration and channels live in roku.json.
for key in ('channels', 'feedback', 'roku', 'navigation', 'prime_video'):
    config.pop(key, None)
temporary = path + '.tmp'
fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, 'w') as handle:
    json.dump(config, handle, indent=2, ensure_ascii=False)
    handle.write('\n')
os.replace(temporary, path)
EOF
start_vizelvision
wait_for 90 "vizelVision's master is up" sh -c "curl -sf --max-time 3 http://127.0.0.1:6970/v1/master | grep -q '\"publisher_ready\": *true'" \
    || fail "vizelVision's master did not come up"
say "Live check: NBC through master control (prepare, take, 30 s on air, fallback)"
(cd "$VV" && as_vivian /usr/bin/python3 tests/live_roku_master.py --fallback --report "$LOGDIR/live-roku-master.json") \
    2>&1 | tee "$LOGDIR/live-roku-master.log"
[ "${PIPESTATUS[0]}" = 0 ] || fail "the live Roku take through vizelVision"

as_vivian rm -rf "$STAGE/streams" "$STAGE/recordings"
say "DONE. TV Thing runs the Roku lane; vizelVision plays it. Up next is paused after the restart:"
say "resume it on vizelVision's On Air page. Logs: $LOGDIR"

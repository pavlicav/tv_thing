#!/bin/bash
# Deploy the sports-guide branch (2026-10-19/20): channel 60, VizelVision's Sports Guide, as
# browser slides and as on-demand video for the Roku (sports_guide_video.py into one libx264).
#
#   sudo bash /home/vivian/tv_thing-sports-guide/scripts/deploy-sports-guide.sh       # deploy
#   bash /home/vivian/tv_thing-sports-guide/scripts/deploy-sports-guide.sh --check    # prechecks only
#
# Run it when nothing is on air: it stops vizelVision (its master holds TV Thing's locks) and
# TV Thing. AGENTS.md's gate, for a change to the core stream stack (a new encoder and its
# supervisor):
#   1. prechecks (nothing on air, prepared or recording; clean checkouts; the HD PVR present;
#      the service's Python has numpy and PIL)
#   2. stop vizelVision's stack and production tv-thing.service
#   3. candidate on :8098 (own UDP ports, streams and recordings dirs, the unit's limits, a
#      one-hour cap): 3 digital retunes; the Sports Guide video started cold 3 times and seen to
#      stop once nobody watches; then a Roku navigation during a digital recording while a
#      viewer keeps the Sports Guide video up, with no thread or process refused by TasksMax
#   4. promote (fast-forward master) and run the digital and Sports Guide gates on production
# Any failure after step 2 rolls back and restarts the old services. The Roku lane's code is
# unchanged, so its navigation gate is not repeated; the headroom check still navigates it.
set -uo pipefail
# Read whole before running: editing a running script made bash resume mid-command (2026-10-06).
{

TV=/home/vivian/tv_thing
TV_WORKTREE=/home/vivian/tv_thing-sports-guide
TV_BRANCH=sports-guide
PY=/home/vivian/miniconda3/bin/python3
DEVICE=/dev/v4l/by-id/usb-AMBA_Hauppauge_HD_PVR_00A4E5AC-video-index0
STAMP=$(date +%Y%m%d-%H%M%S)
LOGDIR=/home/vivian/sports-guide-$STAMP
STAGE=/home/vivian/tv_thing-candidate.sports-guide-$STAMP
CANDIDATE=tv-thing-candidate-$STAMP
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

TV_PROMOTED=0
STOPPED=0
VIEWER=

as_vivian() {
    if [ "$(id -un)" = vivian ]; then env XDG_RUNTIME_DIR=/run/user/1000 "$@"
    else sudo -u vivian -H env XDG_RUNTIME_DIR=/run/user/1000 "$@"; fi
}
git_v() { as_vivian git "$@"; }  # root's git refuses vivian's repositories ("dubious ownership")
user_systemctl() { as_vivian systemctl --user "$@"; }
say() { echo "[$(date +%H:%M:%S)] $*"; }
fail() { say "FAILED: $*"; stop_viewer; [ "$STOPPED" = 1 ] && rollback; exit 1; }
json() { curl -s --max-time 5 "$1" | "$PY" -c "import json,sys; d=json.load(sys.stdin); print($2)"; }

TV_BASE=$(git_v -C "$TV" rev-parse --short HEAD)

precheck() {
    say "Prechecks"
    local on_air prepared recording
    on_air=$(json http://127.0.0.1:6970/v1/master "json.dumps(d.get('on_air'))") || fail "vizelVision master is not answering"
    prepared=$(json http://127.0.0.1:6970/v1/master "json.dumps(d.get('prepared'))")
    [ "$on_air" = "null" ] || fail "something is on air in vizelVision: $on_air"
    [ "$prepared" = "null" ] || fail "something is prepared in vizelVision: $prepared"
    recording=$(json http://127.0.0.1:8080/api/v1/recordings "len(d.get('active_recordings') or ([d['active']] if d.get('active') else []))") \
        || fail "TV Thing is not answering"
    [ "$recording" = "0" ] || fail "TV Thing is recording"
    [ -z "$(git_v -C "$TV" status --porcelain)" ] \
        || fail "$TV has uncommitted changes (they are live in production; commit or stash them first)"
    [ "$(git_v -C "$TV" rev-parse --abbrev-ref HEAD)" = master ] || fail "$TV is not on master"
    git_v -C "$TV" merge-base --is-ancestor HEAD "$TV_BRANCH" || fail "$TV_BRANCH does not descend from master ($TV_BASE)"
    [ -z "$(git_v -C "$TV_WORKTREE" status --porcelain)" ] || fail "$TV_WORKTREE has uncommitted changes"
    [ -d "$TV/roku_state/templates-720p" ] || fail "the Roku's calibration templates are missing"
    [ -e "$DEVICE" ] || fail "the HD PVR is not connected ($DEVICE)"
    "$PY" -c "import numpy, PIL" 2>/dev/null || fail "$PY lacks numpy or PIL (the Sports Guide's renderer)"
    say "Prechecks passed (master $TV_BASE -> $TV_BRANCH $(git_v -C "$TV" rev-parse --short "$TV_BRANCH"))"
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

locks_clear() { [ "$(json http://127.0.0.1:8080/api/v1/tuner/lock "all(v is None for v in d['locks'].values())")" = "True" ]; }
lane_ready() {  # url lane
    [ "$(json "$1/api/v1/status" "bool(d['lanes']['$2']['stream'].get('ready'))")" = "True" ]
}
refused() {  # unit -> times its TasksMax refused a thread or process
    awk '$1 == "max" {print $2}' "/sys/fs/cgroup/system.slice/$1.service/pids.events"
}

stop_vizelvision() {
    say "Stopping vizelVision (its master holds TV Thing's locks)"
    user_systemctl stop vizelvision-admute-recordings.timer vizelvision-stack.target \
        vizelvision-web.service vizelvision-master.service vizelvision-admute.service vizelvision-mediamtx.service
    STOPPED=1
    wait_for 150 "TV Thing's tuner locks have expired" locks_clear || fail "a tuner lock is still held"
}

start_vizelvision() {
    user_systemctl start vizelvision-stack.target vizelvision-admute-recordings.timer
}

rollback() {
    say "ROLLING BACK"
    journalctl -u "$CANDIDATE" --no-pager > "$LOGDIR/candidate.journal" 2>&1
    systemctl stop "$CANDIDATE.service" 2>/dev/null
    if [ "$TV_PROMOTED" = 1 ]; then
        journalctl -u tv-thing --since "-15min" --no-pager > "$LOGDIR/production.journal" 2>&1
        systemctl stop tv-thing.service
        as_vivian git -C "$TV" reset -q --hard "$TV_BASE"
    fi
    systemctl start tv-thing.service
    wait_for 120 "TV Thing is back" lane_ready http://127.0.0.1:8080 digital || say "TV Thing did not come back; check journalctl -u tv-thing"
    start_vizelvision
    say "Rolled back to $TV_BASE. Logs: $LOGDIR"
}

gate() {  # name, url, pid, args...
    local name=$1 url=$2 pid=$3; shift 3
    say "Gate: $name"
    as_vivian "$PY" "$STAGE/stream_sanity_check.py" --url "$url" --server-pid "$pid" "$@" \
        2>&1 | tee "$LOGDIR/gate-$name.log"
    return "${PIPESTATUS[0]}"
}

start_viewer() {  # url: keep the Sports Guide video up, as a Roku watching channel 60 would
    ( while true; do curl -s -o /dev/null --max-time 10 "$1/stream/sports-guide/index.m3u8"; sleep 2; done ) &
    VIEWER=$!
}
stop_viewer() { [ -n "$VIEWER" ] && kill "$VIEWER" 2>/dev/null; VIEWER=; }

guide_running() {  # url
    [ "$(json "$1/api/v1/status" "bool(d['slides']['sports-guide']['running'])")" = "True" ]
}

headroom_test() {  # name, url, unit
    say "Task headroom: a Roku navigation during a digital recording, the Sports Guide video up ($1)"
    start_viewer "$2"
    wait_for 30 "The Sports Guide video is up" guide_running "$2" || { stop_viewer; return 1; }
    as_vivian "$PY" "$STAGE/scripts/task_headroom_check.py" --url "$2" \
        --cgroup "/sys/fs/cgroup/system.slice/$3.service" 2>&1 | tee "$LOGDIR/headroom-$1.log"
    local status=${PIPESTATUS[0]}
    guide_running "$2" || { say "The Sports Guide video stopped while it was watched"; status=1; }
    stop_viewer
    [ "$status" = 0 ] || return 1
    [ "$(refused "$3")" = 0 ] || { say "$3: TasksMax refused $(refused "$3") thread(s) or process(es)"; return 1; }
}

lan_listening() {  # every LAN address the host has is listened on at :8080
    local address
    for address in $(ip -4 -o addr show scope global | awk '$2 !~ /^(docker|br-|veth|tailscale|virbr)/ {sub("/.*", "", $4); print $4}'); do
        ss -ltnH "src $address:8080" | grep -q . || return 1
    done
}

# ---------------------------------------------------------------------------
precheck
[ "$CHECK_ONLY" = 1 ] && exit 0
[ "$(id -u)" = 0 ] || { echo "Run with sudo (tv-thing.service and the candidate unit need root)."; exit 1; }
mkdir -p "$LOGDIR" && chown vivian:vivian "$LOGDIR"
exec > >(tee -a "$LOGDIR/deploy.log") 2>&1
say "Logs in $LOGDIR"
cp -p "$TV/streams/tuner_locks.json" "$LOGDIR/" 2>/dev/null

stop_vizelvision
say "Stopping production TV Thing"
systemctl stop tv-thing.service || fail "could not stop tv-thing"

# --- 3. the candidate -------------------------------------------------------------------------
say "Staging the candidate in $STAGE"
as_vivian mkdir -p "$STAGE/streams" "$STAGE/recordings"
as_vivian rsync -a --exclude .git --exclude streams --exclude __pycache__ "$TV_WORKTREE/" "$STAGE/"
as_vivian rsync -a "$TV/roku_state/" "$STAGE/roku_state/"
systemctl reset-failed 'tv-thing-candidate*' 2>/dev/null
# A 20 s idle stop on the candidate keeps its Sports Guide gate short; production runs the 60 s.
systemd-run --unit="$CANDIDATE" --collect --uid=vivian --gid=vivian \
    -p WorkingDirectory="$STAGE" -p MemoryMax=1800M -p CPUQuota=800% -p TasksMax=192 \
    -p LimitFSIZE=16M:16G -p OOMPolicy=continue -p RuntimeMaxSec=3600 \
    --setenv=PORT=8098 --setenv=TV_THING_BIND=127.0.0.1 --setenv=TV_THING_BIND_LOOPBACK=1 \
    --setenv=TV_THING_STREAMS_DIR="$STAGE/streams" --setenv=TV_THING_RECORDINGS_DIR="$STAGE/recordings" \
    --setenv=TV_THING_TV_UDP_PORT=18420 --setenv=TV_THING_RECORD_TAP_PORT=18484 \
    --setenv=TV_THING_ROKU_TAP_PORT=18470 --setenv=TV_THING_EPG=0 \
    --setenv=TV_THING_VIDEO_ENCODER=cpu --setenv=TV_THING_MAX_GPU_TEMP_C=90 \
    --setenv=TV_THING_RECORD_MIN_FREE_GB=1 --setenv=TV_THING_SLIDES_IDLE_SECONDS=20 \
    --setenv=PYTHONUNBUFFERED=1 \
    "$PY" "$STAGE/server.py" || fail "could not start the candidate"
wait_for 90 "The candidate's digital lane is ready" lane_ready http://127.0.0.1:8098 digital || fail "candidate digital lane"
wait_for 60 "The candidate's Roku capture is ready" lane_ready http://127.0.0.1:8098 roku || fail "candidate Roku capture"
sleep 20
PID=$(systemctl show -p MainPID --value "$CANDIDATE")
gate candidate-digital http://127.0.0.1:8098 "$PID" --retunes 3 || fail "candidate digital gate"
gate candidate-sports-guide http://127.0.0.1:8098 "$PID" --slides sports-guide --retunes 3 \
    || fail "candidate Sports Guide gate"
headroom_test candidate http://127.0.0.1:8098 "$CANDIDATE" || fail "candidate task headroom"
say "The candidate's TasksMax refused nothing"
journalctl -u "$CANDIDATE" --no-pager > "$LOGDIR/candidate.journal" 2>&1
systemctl stop "$CANDIDATE.service"
pgrep -u vivian -f "$STAGE/" >/dev/null && fail "the candidate left processes behind: $(pgrep -u vivian -af "$STAGE/")"

# --- 4. promote and gate production ----------------------------------------------------------
say "Promoting TV Thing"
TV_PROMOTED=1
as_vivian git -C "$TV" merge -q --ff-only "$TV_BRANCH" || fail "could not fast-forward $TV"
systemctl start tv-thing.service || fail "tv-thing did not start"
wait_for 120 "Production's digital lane is ready" lane_ready http://127.0.0.1:8080 digital || fail "production digital lane"
wait_for 60 "Production's Roku capture is ready" lane_ready http://127.0.0.1:8080 roku || fail "production Roku capture"
wait_for 60 "Production listens on every LAN address" lan_listening || fail "production is not listening on the LAN"
sleep 20
PID=$(systemctl show -p MainPID --value tv-thing)
gate production-digital http://127.0.0.1:8080 "$PID" --retunes 3 || fail "production digital gate"
gate production-sports-guide http://127.0.0.1:8080 "$PID" --slides sports-guide --retunes 3 \
    || fail "production Sports Guide gate"
[ "$(refused tv-thing)" = 0 ] || fail "production's TasksMax refused $(refused tv-thing) thread(s) or process(es)"
say "Production's TasksMax refused nothing"

start_vizelvision
as_vivian rm -rf "$STAGE/streams" "$STAGE/recordings"
say "DONE. Production runs $TV_BRANCH ($(git_v -C "$TV" rev-parse --short HEAD)). Logs: $LOGDIR"
exit
}

#!/bin/bash
# Deploy the feed-seam branch (2026-10-25): Phase 1 of the feed/control split, the feed seam inside
# today's one process (INVARIANTS.md; the plan is ~/.claude/plans/can-we-come-up-swift-hinton.md).
# It covers:
#   * feed.json lanes, and the feed's epochs, revisions and once-only commands;
#   * the recorders and their journal;
#   * the Roku's generation and selection, kept by the feed;
#   * scans, the signal meter and guide reads through the feed client;
#   * the relay asking control's resolver;
#   * a reaper that never touches another service.
# RUNBOOK.md is the same deploy, by hand.
#
#   sudo bash /home/vivian/tv_thing-feed-seam/scripts/deploy-feed-seam.sh            # deploy
#   bash /home/vivian/tv_thing-feed-seam/scripts/deploy-feed-seam.sh --check         # prechecks only
#   sudo bash /home/vivian/tv_thing-feed-seam/scripts/deploy-feed-seam.sh --on-air   # see below
#
# Run it when nothing is on air. It stops VizelVision's web and master for about 20-35 minutes,
# and TV Thing for 10-20 of them. Both web and master take TV Thing's locks: master its leases,
# web its RokuHold and Up next. With something on air it refuses, unless you pass --on-air;
# whatever was on air is put back at the end either way.
#
# AGENTS.md's gate:
#   1. Prechecks:
#      - the fast suite passes;
#      - production is on master and fast-forwards to the branch;
#      - its uncommitted work (roku_app/) touches none of the branch's files;
#      - nothing is recording, and no timer is due within the hour;
#      - the HD PVR is present;
#      - nothing is on air or prepared in VizelVision (unless --on-air);
#      - the drill passes on today's build.
#   2. Note what VizelVision has on air, stop its web and master, and wait for TV Thing's locks
#      to expire.
#   3. Stop production tv-thing.service.
#   4. A candidate on :8098: production's tree with the branch's files over it, with its own UDP
#      ports, streams, recordings and state dirs, the unit's limits and a one-hour cap. It must pass:
#      - 3 digital retunes;
#      - a Roku navigation during a digital recording, with nothing refused by TasksMax;
#      - 3 Roku navigations on one capture;
#      - the Phase 1 checks: the status fields, the recorder journal, a re-tune that keeps the
#        Roku's generation, the signal meter and a DX scan.
#   5. Promote (fast-forward master; uncommitted changes elsewhere stay), start production, and
#      run the same gates on it.
#   6. Bring VizelVision back, put back what was on air, resume Up next, and run the drill
#      (--watch-only).
# Any failure after step 2 rolls back and restarts the old services. The rollback is
# `git reset --keep`, never --hard, so roku_app/'s uncommitted work survives it.
set -uo pipefail
# Read whole before running: editing a running script made bash resume mid-command (2026-10-06).
{

TV=/home/vivian/tv_thing
TV_WORKTREE=/home/vivian/tv_thing-feed-seam
TV_BRANCH=feed-seam
PY=/home/vivian/miniconda3/bin/python3
DEVICE=/dev/v4l/by-id/usb-AMBA_Hauppauge_HD_PVR_00A4E5AC-video-index0
P=http://127.0.0.1:8080
C=http://127.0.0.1:8098
VV=http://127.0.0.1:6970
VV_CONFIG=/home/vivian/vizelVision/config.master.json
STAMP=$(date +%Y%m%d-%H%M%S)
LOGDIR=/home/vivian/feed-seam-$STAMP
STAGE=/home/vivian/tv_thing-candidate.feed-seam-$STAMP
CANDIDATE=tv-thing-candidate-$STAMP
CHECK_ONLY=0
ON_AIR_OK=0
for arg in "$@"; do
    case "$arg" in
        --check) CHECK_ONLY=1 ;;
        --on-air) ON_AIR_OK=1 ;;
        *) echo "usage: [sudo] bash $0 [--check] [--on-air]"; exit 2 ;;
    esac
done

TV_PROMOTED=0
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

KEY=$("$PY" -c "import json; print(json.load(open('$VV_CONFIG'))['server']['api_key'])" 2>/dev/null)
vv_get() { curl -s --max-time 10 -H "X-API-Key: $KEY" "$VV$1"; }
vv_post() {  # path, JSON body
    curl -s --max-time 20 -X POST -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d "$2" "$VV$1"
}

TV_BASE=$(git_v -C "$TV" rev-parse --short HEAD)
as_vivian mkdir -p "$LOGDIR"
trap 'chown -R vivian:vivian "$LOGDIR" 2>/dev/null' EXIT

wait_for() {  # seconds, description, command...
    local limit=$1 what=$2; shift 2
    local deadline=$((SECONDS + limit))
    until "$@" >/dev/null 2>&1; do
        [ $SECONDS -ge $deadline ] && return 1
        sleep 2
    done
    say "$what"
}

locks_clear() {  # url
    [ "$(json "$1/api/v1/tuner/lock" "all(v is None for v in d['locks'].values())")" = "True" ]
}
# Selecting the HD PVR's input (component, roku.json's input_index, as TV Thing does before every
# capture) answers at once. When the device has hung, it blocks until killed, and every capture
# opens it to no data (2026-10-25). --get-input is no test: the driver answers it from memory.
hdpvr_answers() { timeout -k 2 5 v4l2-ctl --device "$DEVICE" --set-input=0; }
HDPVR_RESET="echo 0 | sudo tee /sys/bus/usb/devices/2-12/authorized; sleep 3; echo 1 | sudo tee /sys/bus/usb/devices/2-12/authorized (or unplug its power for 10 s)"
lane_ready() {  # url lane
    [ "$(json "$1/api/v1/status" "bool(d['lanes']['$2']['stream'].get('ready'))")" = "True" ]
}
refused() {  # unit -> times its TasksMax refused a thread or process
    awk '$1 == "max" {print $2}' "/sys/fs/cgroup/system.slice/$1.service/pids.events"
}
lan_listening() {  # every LAN address the host has is listened on at :8080
    local address
    for address in $(ip -4 -o addr show scope global | awk '$2 !~ /^(docker|br-|veth|tailscale|virbr)/ {sub("/.*", "", $4); print $4}'); do
        ss -ltnH "src $address:8080" | grep -q . || return 1
    done
}

precheck() {
    say "Prechecks"
    [ "$(git_v -C "$TV_WORKTREE" rev-parse --abbrev-ref HEAD)" = "$TV_BRANCH" ] || fail "$TV_WORKTREE is not on $TV_BRANCH"
    [ -z "$(git_v -C "$TV_WORKTREE" status --porcelain)" ] || fail "$TV_WORKTREE has uncommitted changes"
    [ "$(git_v -C "$TV" rev-parse --abbrev-ref HEAD)" = master ] || fail "$TV is not on master"
    git_v -C "$TV" merge-base --is-ancestor HEAD "$TV_BRANCH" || fail "$TV_BRANCH does not descend from master ($TV_BASE)"
    # Uncommitted changes in production stay through a fast-forward and a reset --keep, so the
    # branch's files must be clear of them; the candidate is staged with them, as production will run.
    local dirty overlap recording due on_air prepared
    dirty=$(git_v -C "$TV" status --porcelain | awk '{print $NF}' | sort)
    overlap=$(comm -12 <(echo "$dirty") <(git_v -C "$TV" diff --name-only HEAD "$TV_BRANCH" | sort))
    [ -z "$overlap" ] || fail "$TV has uncommitted changes to files the branch changes: $overlap"
    [ -z "$dirty" ] || say "Production's $(echo "$dirty" | wc -l) other uncommitted path(s) stay, and are staged with the candidate"
    recording=$(json "$P/api/v1/recordings" "len(d.get('active_recordings') or [])") || fail "TV Thing is not answering"
    [ "$recording" = "0" ] || fail "TV Thing is recording"
    due=$(json "$P/api/v1/recordings" "sum(1 for t in d.get('timers') or [] if t.get('seconds_until_start', 0) < 3600)")
    [ "$due" = "0" ] || fail "a VCR timer starts within the hour: TV Thing would be down for it"
    [ -d "$TV/roku_state/templates-720p" ] || fail "the Roku's calibration templates are missing"
    [ -e "$DEVICE" ] || fail "the HD PVR is not connected ($DEVICE)"
    lane_ready "$P" roku || fail "production's Roku capture is not running: is the HD PVR hung? Reset it: $HDPVR_RESET"
    [ -n "$KEY" ] || fail "no VizelVision API key in $VV_CONFIG"
    on_air=$(json "$VV/v1/master" "json.dumps(d.get('on_air'))") || fail "VizelVision is not answering"
    prepared=$(json "$VV/v1/master" "json.dumps(d.get('prepared'))")
    if [ "$on_air" != "null" ] || [ "$prepared" != "null" ]; then
        [ "$ON_AIR_OK" = 1 ] || fail "VizelVision has something on air or prepared ($on_air); run after it, or pass --on-air to take it off and put it back"
        say "On air, to be put back afterwards: $on_air"
    fi
    say "The fast suite (about 2 minutes)"
    as_vivian bash -c "cd '$TV_WORKTREE' && '$PY' -m pytest -q -m 'not slow and not api and not hardware and not resource' --ignore=test_integration.py" \
        > "$LOGDIR/fast-suite.log" 2>&1 || fail "the fast suite failed: $LOGDIR/fast-suite.log"
    tail -1 "$LOGDIR/fast-suite.log"
    say "The drill on today's build (30 s, read-only)"
    as_vivian "$PY" "$TV_WORKTREE/scripts/restart_drill.py" --watch-only --seconds 30 > "$LOGDIR/drill-before.log" 2>&1 \
        || fail "the drill already fails on today's build: $LOGDIR/drill-before.log"
    say "Prechecks passed (master $TV_BASE -> $TV_BRANCH $(git_v -C "$TV" rev-parse --short "$TV_BRANCH"))"
}

# --- VizelVision ------------------------------------------------------------------------------
stop_vizelvision() {
    say "Noting what VizelVision has on air, then stopping its web and master"
    curl -s --max-time 5 "$VV/v1/master" > "$LOGDIR/master-before.json"
    vv_get /v1/queue > "$LOGDIR/queue-before.json"
    cp -p "$TV/streams/tuner_locks.json" "$LOGDIR/" 2>/dev/null
    user_systemctl stop vizelvision-web.service vizelvision-master.service
    STOPPED=1
    wait_for 150 "TV Thing's tuner locks have expired" locks_clear "$P" || fail "a tuner lock is still held"
}

start_vizelvision() {
    user_systemctl start vizelvision-master.service vizelvision-web.service
}

vv_answering() { vv_get /v1/queue | "$PY" -c "import json,sys; json.load(sys.stdin)"; }
air_target() {  # (source_id, channel_id) on air now, or None
    json "$VV/v1/master" "(lambda a: a and (a.get('source_id'), a.get('channel_id')))(d.get('on_air'))"
}
air_is() { [ "$(air_target)" = "$1" ]; }
was_on_air() {  # (source_id, channel_id) on air before the deploy, or None
    "$PY" -c "import json; a = json.load(open('$LOGDIR/master-before.json')).get('on_air'); print(a and (a.get('source_id'), a.get('channel_id')))" 2>/dev/null || echo None
}
restore_body() {  # a Now entry for what was on air, with its hold
    "$PY" - "$LOGDIR" <<'EOF'
import json, sys
log = sys.argv[1]
on_air = json.load(open(f"{log}/master-before.json")).get("on_air")
try:
    hold = json.load(open(f"{log}/queue-before.json")).get("hold") or {}
except (OSError, ValueError):
    hold = {}
body = {"target": {"source_id": on_air["source_id"], "channel_id": on_air["channel_id"]},
        "release_type": "now", "resume": True}
if hold.get("until"):
    body["until"] = {"kind": "time", "at": hold["until"]}
elif hold.get("game"):
    body["until"] = {"kind": "game", **hold["game"]}
print(json.dumps(body))
EOF
}

restore_air() {  # Up next resumed (web starts it paused); what was on air put back
    local before body
    before=$(was_on_air)
    wait_for 90 "VizelVision's web answers" vv_answering \
        || { say "VizelVision's web does not answer: resume Up next and put $before back by hand"; return 1; }
    vv_post /v1/queue/resume '{}' >/dev/null && say "Up next resumed"
    [ "$before" = "None" ] && return 0
    sleep 20
    if ! air_is "$before"; then
        body=$(restore_body)
        say "Putting $before back on air: $body"
        vv_post /v1/queue "$body" > "$LOGDIR/restore-reply.json"
    fi
    # A YouTube TV target is a Roku navigation away, 1-2 minutes.
    wait_for 300 "$before is on air again" air_is "$before" \
        || { say "$before is not back on air after 5 minutes: check VizelVision"; return 1; }
}

# --- gates ------------------------------------------------------------------------------------
gate() {  # name, url, pid, args...
    local name=$1 url=$2 pid=$3; shift 3
    # A gate locks every lane. A recording cancelled just before lets go of its lock only on
    # the VCR's next tick (the headroom check's did not, 2026-10-25).
    wait_for 60 "Every lane is unlocked" locks_clear "$url" || { say "a lane is still locked at $url"; return 1; }
    say "Gate: $name"
    as_vivian "$PY" "$STAGE/stream_sanity_check.py" --url "$url" --server-pid "$pid" "$@" \
        2>&1 | tee "$LOGDIR/gate-$name.log"
    return "${PIPESTATUS[0]}"
}

roku_gate() {  # name, url, pid: three navigations, ending on ESPN for the re-tune check
    gate "$1" "$2" "$3" --roku-target youtube-tv:ESPN --roku-target youtube-tv:NBC --retunes 3
}

journal_check() {  # journal file: each recorded piece started and ended, the last one cancelled
    "$PY" - "$1" <<'EOF'
import json, sys
try:
    records = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
except (OSError, ValueError) as error:
    sys.exit(f"journal unreadable: {error}")
starts = {r["id"] for r in records if r["event"] == "start"}
ends = [r for r in records if r["event"] == "end"]
last = ends[-1] if ends else {}
print(f"journal: {len(starts)} piece(s), last end {last.get('reason')} with {last.get('bytes')} bytes")
ok = starts and starts == {r["id"] for r in ends} and last.get("reason") == "stopped" and (last.get("bytes") or 0) > 0
sys.exit(0 if ok else 1)
EOF
}

phase1_checks() {  # url, journal file ("" for none), scan (1 to run a DX scan)
    local url=$1 journal=$2 scan=$3 generation reply rf stream
    say "Phase 1 checks ($url)"
    if [ "$(json "$url/api/v1/status" "d['node'] == 'viviserv' and len(d['feed_epoch']) == 16 and all(d['lanes'][n]['lifecycle'] == 'running' for n in ('digital', 'roku')) and not any(l['navigating'] for l in d['lanes'].values())")" != "True" ]; then
        say "the status lacks the feed's state: $(json "$url/api/v1/status" "d.get('node'), d.get('feed_epoch'), {n: (l.get('lifecycle'), l.get('navigating')) for n, l in d['lanes'].items()}")"
        return 1
    fi
    say "status: node, feed epoch, lifecycles and navigation flags as expected"
    if [ -n "$journal" ]; then
        journal_check "$journal" || { say "the recorder journal is not a clean start and end"; return 1; }
    fi
    # A re-tune of what the Roku shows moves nothing: its generation stays (invariant 5).
    generation=$(json "$url/api/v1/status" "d['lanes']['roku']['stream']['generation']")
    reply=$(curl -s --max-time 20 -X POST -H 'Content-Type: application/json' \
        -d '{"source": "youtube-tv", "channel_id": "ESPN", "navigate": false}' "$url/api/v1/tune" \
        | "$PY" -c "import json,sys; d=json.load(sys.stdin); print(d.get('reused'), (d.get('stream') or {}).get('generation'))")
    [ "$reply" = "True $generation" ] || { say "a re-tune of what the Roku shows moved it: $reply (was $generation)"; return 1; }
    say "re-tune reused the Roku's generation $generation"
    [ "$(json "$url/api/v1/signal" "d['ok']")" = "True" ] || { say "the signal meter failed"; return 1; }
    say "signal meter: $(json "$url/api/v1/signal" "d['signal']")"
    if [ "$scan" = 1 ]; then
        rf=$(json "$url/api/v1/status" "d['lanes']['digital']['selection']['rf_channel']")
        curl -s --max-time 30 -X POST -H 'Content-Type: application/json' -d "{\"rf_channel\": $rf}" "$url/api/v1/scan/channel" \
            | "$PY" -c "import json,sys; d=json.load(sys.stdin); print('DX scan:', d.get('ok'), d.get('locked'), d.get('snr_db')); sys.exit(0 if d.get('ok') and d.get('locked') else 1)" \
            || { say "a DX scan of RF $rf failed"; return 1; }
        wait_for 60 "The digital lane is back after the scan" lane_ready "$url" digital || return 1
    fi
    for stream in pentulive cabride; do      # only meaningful while live upstream: never fails the gate
        curl -s --max-time 10 -o "$LOGDIR/relay-$stream.m3u8" "$url/stream/internet/$stream/index.m3u8"
        if head -1 "$LOGDIR/relay-$stream.m3u8" 2>/dev/null | grep -q '^#EXTM3U'; then
            say "relay $stream: playing"
        else
            say "relay $stream: no playlist (fine if it is not live upstream)"
        fi
    done
}

headroom_test() {  # name, url, unit
    say "Task headroom: a Roku navigation during a digital recording ($1)"
    as_vivian "$PY" "$STAGE/scripts/task_headroom_check.py" --url "$2" \
        --cgroup "/sys/fs/cgroup/system.slice/$3.service" 2>&1 | tee "$LOGDIR/headroom-$1.log"
    [ "${PIPESTATUS[0]}" = 0 ] || return 1
    [ "$(refused "$3")" = 0 ] || { say "$3: TasksMax refused $(refused "$3") thread(s) or process(es)"; return 1; }
}

# --- rollback ---------------------------------------------------------------------------------
rollback() {
    say "ROLLING BACK"
    local restart=1
    journalctl -u "$CANDIDATE" --no-pager > "$LOGDIR/candidate.journal" 2>&1
    systemctl stop "$CANDIDATE.service" 2>/dev/null
    if [ "$TV_PROMOTED" = 1 ]; then
        journalctl -u tv-thing --since "-30min" --no-pager > "$LOGDIR/production.journal" 2>&1
        systemctl stop tv-thing.service
        # --keep, never --hard: it restores only the branch's files and keeps roku_app/'s uncommitted work.
        if as_vivian git -C "$TV" reset -q --keep "$TV_BASE"; then
            say "Production's checkout is back at $TV_BASE, its uncommitted work kept"
        else
            restart=0
            say "git reset --keep $TV_BASE refused: tv-thing stays STOPPED rather than run the failed build."
            say "Check git -C $TV status, reset it to $TV_BASE, then: sudo systemctl start tv-thing"
        fi
    fi
    if [ "$restart" = 1 ]; then
        hdpvr_answers >/dev/null 2>&1 || say "THE HD PVR DOES NOT ANSWER: the Roku lane stays down until it is reset: $HDPVR_RESET"
        systemctl start tv-thing.service
        wait_for 120 "TV Thing is back" lane_ready "$P" digital || say "TV Thing did not come back; check journalctl -u tv-thing"
        wait_for 60 "TV Thing's Roku capture is back" lane_ready "$P" roku || say "TV Thing's Roku capture did not come back: reset the HD PVR: $HDPVR_RESET"
    fi
    start_vizelvision
    restore_air
    say "Rolled back to $TV_BASE. Logs: $LOGDIR"
}

# ---------------------------------------------------------------------------
precheck
[ "$CHECK_ONLY" = 1 ] && { say "Prechecks only: nothing was stopped. Logs: $LOGDIR"; exit 0; }
[ "$(id -u)" = 0 ] || { echo "Run with sudo (tv-thing.service and the candidate unit need root)."; exit 1; }
exec > >(tee -a "$LOGDIR/deploy.log") 2>&1
say "Logs in $LOGDIR"

# --- 2-3. VizelVision and production stopped --------------------------------------------------
stop_vizelvision
say "Stopping production TV Thing"
systemctl stop tv-thing.service || fail "could not stop tv-thing"

# --- 4. the candidate -------------------------------------------------------------------------
say "Staging the candidate in $STAGE"
as_vivian mkdir -p "$STAGE/streams" "$STAGE/recordings" "$STAGE/state"
# Production's tree as it runs (its uncommitted changes, Roku calibration and lineup state
# included), then the branch's files over it: what promotion will leave running.
as_vivian rsync -a --exclude /.git --exclude /streams --exclude /state --exclude __pycache__ "$TV/" "$STAGE/"
git_v -C "$TV" diff --name-only HEAD "$TV_BRANCH" > "$LOGDIR/branch-files.txt"
chown vivian:vivian "$LOGDIR/branch-files.txt"
as_vivian rsync -a --files-from="$LOGDIR/branch-files.txt" "$TV_WORKTREE/" "$STAGE/" || fail "could not stage the branch"
systemctl reset-failed 'tv-thing-candidate*' 2>/dev/null
# No PSIP guide sweeps on the candidate: one would take the tuner mid-gate.
systemd-run --unit="$CANDIDATE" --collect --uid=vivian --gid=vivian \
    -p WorkingDirectory="$STAGE" -p MemoryMax=1800M -p CPUQuota=800% -p TasksMax=192 \
    -p LimitFSIZE=16M:16G -p OOMPolicy=continue -p RuntimeMaxSec=3600 -p TimeoutStopSec=5 \
    --setenv=PORT=8098 --setenv=TV_THING_BIND=127.0.0.1 --setenv=TV_THING_BIND_LOOPBACK=1 \
    --setenv=TV_THING_STREAMS_DIR="$STAGE/streams" --setenv=TV_THING_RECORDINGS_DIR="$STAGE/recordings" \
    --setenv=TV_THING_STATE_DIR="$STAGE/state" \
    --setenv=TV_THING_TV_UDP_PORT=18420 --setenv=TV_THING_RECORD_TAP_PORT=18484 \
    --setenv=TV_THING_ROKU_TAP_PORT=18470 --setenv=TV_THING_EPG=0 \
    --setenv=TV_THING_VIDEO_ENCODER=cpu --setenv=TV_THING_MAX_GPU_TEMP_C=90 \
    --setenv=TV_THING_RECORD_MIN_FREE_GB=1 --setenv=PYTHONUNBUFFERED=1 \
    "$PY" "$STAGE/server.py" || fail "could not start the candidate"
wait_for 90 "The candidate's digital lane is ready" lane_ready "$C" digital || fail "candidate digital lane"
wait_for 60 "The candidate's Roku capture is ready" lane_ready "$C" roku || fail "candidate Roku capture"
sleep 20
journalctl -u "$CANDIDATE" --no-pager | grep -E 'Reaped|Traceback|FeedConfigError' \
    && say "(the candidate's start logged the lines above)" || say "The candidate started clean"
PID=$(systemctl show -p MainPID --value "$CANDIDATE")
gate candidate-digital "$C" "$PID" --retunes 3 || fail "candidate digital gate"
headroom_test candidate "$C" "$CANDIDATE" || fail "candidate task headroom"
roku_gate candidate-roku "$C" "$PID" || fail "candidate Roku gate"
phase1_checks "$C" "$STAGE/state/feed/recordings-journal.jsonl" 1 || fail "candidate Phase 1 checks"
say "The candidate's TasksMax refused nothing"
journalctl -u "$CANDIDATE" --no-pager > "$LOGDIR/candidate.journal" 2>&1
cp -p "$STAGE/state/feed/recordings-journal.jsonl" "$LOGDIR/candidate-journal.jsonl" 2>/dev/null
systemctl stop "$CANDIDATE.service"
pgrep -u vivian -f "$STAGE/" >/dev/null && fail "the candidate left processes behind: $(pgrep -u vivian -af "$STAGE/")"
hdpvr_answers >/dev/null 2>&1 || fail "the HD PVR stopped answering after the candidate: reset it before deploying again: $HDPVR_RESET"

# --- 5. promote, restart, and gate production -------------------------------------------------
say "Promoting TV Thing"
TV_PROMOTED=1
as_vivian git -C "$TV" merge -q --ff-only "$TV_BRANCH" || fail "could not fast-forward $TV"
systemctl start tv-thing.service || fail "tv-thing did not start"
wait_for 120 "Production's digital lane is ready" lane_ready "$P" digital || fail "production digital lane"
wait_for 60 "Production's Roku capture is ready" lane_ready "$P" roku || fail "production Roku capture"
wait_for 60 "Production listens on every LAN address" lan_listening || fail "production is not listening on the LAN"
sleep 20
journalctl -u tv-thing --since "-3min" --no-pager | grep -E 'Reaped|Traceback|FeedConfigError' \
    && say "(production's start logged the lines above)" || say "Production started clean"
PID=$(systemctl show -p MainPID --value tv-thing)
gate production-digital "$P" "$PID" --retunes 3 || fail "production digital gate"
roku_gate production-roku "$P" "$PID" || fail "production Roku gate"
[ "$(refused tv-thing)" = 0 ] || fail "production's TasksMax refused $(refused tv-thing) thread(s) or process(es)"
say "Production's TasksMax refused nothing"
phase1_checks "$P" "" 0 || fail "production Phase 1 checks"

# --- 6. VizelVision back, and the drill -------------------------------------------------------
say "Starting VizelVision"
start_vizelvision
DRILL=(--watch-only --seconds 120)
if restore_air; then
    say "The drill on the new build (2 minutes, read-only)"
else
    # VizelVision did not settle, so its part of the drill would fail for its own reasons.
    DRILL+=(--vizelvision '')
    say "The drill on the new build, TV Thing only (2 minutes, read-only)"
fi
as_vivian "$PY" "$TV/scripts/restart_drill.py" "${DRILL[@]}" 2>&1 | tee "$LOGDIR/drill-after.log" \
    || fail "the drill on the new build"

as_vivian rm -rf "$STAGE/streams" "$STAGE/recordings"
say "DONE. Production runs $TV_BRANCH ($(git_v -C "$TV" rev-parse --short HEAD)). Logs: $LOGDIR"
say "Optional, off air only (a second feed drop): $TV/scripts/restart_drill.py --expect-change --seconds 240 --command 'sudo systemctl restart tv-thing'"
exit
}

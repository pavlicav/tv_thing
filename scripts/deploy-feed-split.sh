#!/bin/bash
# Deploy the feed-split branch: Phase 2 of the feed/control split (INVARIANTS.md; RUNBOOK.md's
# Phase 2; the plan is ~/.claude/plans/can-we-come-up-swift-hinton.md). TV Thing becomes two
# services behind Apache:
#   * tv-thing-feed (feed_server.py, 127.0.0.1:8086): the tuners, the HD PVR's capture, every
#     ffmpeg, the VCR's recorders and the relays. It serves /stream/, and /feed/v1/ to this host.
#   * tv-thing (server.py as control, 127.0.0.1:8087): the API, the pages, locks, the Roku's
#     navigation, guides, the VCR's timers and finishing. It serves /internal/ to the feed.
#   * Apache on :8080 (127.0.0.1, 192.168.1.98 and 10.0.0.98, never tailscale0's): /stream/ to the
#     feed and everything else to control. viviserv-proxy.conf's /tv/ already proxies to
#     127.0.0.1:8080, so the main site is not touched. Client URLs do not change.
# Afterwards a restart, or a crash, of tv-thing drops no stream. The drill at the end proves it.
#
#   bash /home/vivian/tv_thing-feed-split/scripts/deploy-feed-split.sh --check        # prechecks only, no sudo
#   sudo bash /home/vivian/tv_thing-feed-split/scripts/deploy-feed-split.sh           # deploy
#   sudo bash /home/vivian/tv_thing-feed-split/scripts/deploy-feed-split.sh --on-air  # see below
#   sudo bash /home/vivian/tv_thing/scripts/deploy-feed-split.sh --rollback          # back to one process, later
#
# A dropped SSH session does not stop it (tmux is still nicer). Run it when nothing is on air: it
# stops VizelVision's web and master for about 30-40 minutes, and TV Thing for 15-25 of them. With
# something on air it refuses, unless you pass --on-air; whatever was on air is put back at the end
# either way. Apache restarts once (a second or two of backup-thing and the other sites).
#
# AGENTS.md's gate:
#   1. Prechecks:
#      - Phase 1's: the fast suite; production on master, fast-forwarding to the branch, its
#        uncommitted work clear of the branch's files; nothing recording or due within the hour;
#        the HD PVR answering; VizelVision with nothing on air (unless --on-air); the drill on
#        today's build;
#      - the staged split check (31 checks, no hardware, about a minute);
#      - Apache's modules and config test, the two LAN addresses, ports 8086, 8087, 8091, 8092
#        and 8098 free, and the installed tv-thing.service the repo's (the rollback puts it back).
#   2. Note what VizelVision has on air, stop its web and master, and wait for TV Thing's locks.
#   3. Stop production tv-thing.service.
#   4. The candidate: production's tree with the branch's files over it, run as production will
#      run. That is a feed unit (:8091) and a control unit (:8092), with the production units'
#      limits, behind a private Apache on :8098 running the production routes. It has its own UDP
#      ports, streams, recordings and state dirs, and a one-hour cap. Through :8098 it must pass:
#      - 3 digital retunes;
#      - a Roku navigation during a digital recording, with nothing refused by either TasksMax;
#      - 3 Roku navigations on one capture;
#      - the Phase 1 checks;
#      - the split checks: each service on 127.0.0.1 only, /internal/ and /feed/v1/ never routed,
#        control on the feed's epoch;
#      - the drill restarting the candidate's control, then killing it: no lane's generation
#        changes and every playlist keeps advancing.
#      The gate measures the feed's and control's process trees as one, against the same limits.
#   5. Cut-over:
#      - fast-forward master;
#      - install the two units, the front-door site and Apache's drop-in, saving what they replace
#        in the log directory;
#      - run Apache's config test;
#      - start the feed, then control, then restart Apache fully (its new Listen lines);
#      - run the same gates and split checks on production.
#   6. Start VizelVision, put back what was on air and resume Up next. Then the proof, with
#      VizelVision on air: the drill restarts tv-thing (A1), then kills it (A2).
# Any failure in 2-5 rolls back to the one process: `git reset --keep` (never --hard, so roku_app/'s
# uncommitted work survives), the old unit, Apache without the front door. A failed drill in 6 does
# not roll back, since the split has passed its gates and one process fails that drill by design.
# It says what failed; --rollback goes back.
set -uo pipefail
# Read whole before running: editing a running script made bash resume mid-command (2026-10-06).
{

TV=/home/vivian/tv_thing
TV_WORKTREE=/home/vivian/tv_thing-feed-split
TV_BRANCH=feed-split
PY=/home/vivian/miniconda3/bin/python3
DEVICE=/dev/v4l/by-id/usb-AMBA_Hauppauge_HD_PVR_00A4E5AC-video-index0
P=http://127.0.0.1:8080
P_TV=http://127.0.0.1/tv          # the main site's /tv/ (viviserv-proxy.conf)
CF=8091 CC=8092 CD=8098           # the candidate's feed, control and front door
C=http://127.0.0.1:$CD
VV=http://127.0.0.1:6970
VV_CONFIG=/home/vivian/vizelVision/config.master.json
LAN_ADDRESSES="192.168.1.98 10.0.0.98"
UNITS=/etc/systemd/system
APACHE_SITE=/etc/apache2/sites-available/tv-thing-front-door.conf
APACHE_DROPIN=$UNITS/apache2.service.d/tv-thing-front-door.conf
STAMP=$(date +%Y%m%d-%H%M%S)
LOGDIR=/home/vivian/feed-split-$STAMP
STAGE=/home/vivian/tv_thing-candidate.feed-split-$STAMP
CAND_FEED=tv-thing-candidate-feed-$STAMP
CAND_CONTROL=tv-thing-candidate-control-$STAMP
CAND_APACHE=tv-thing-candidate-apache-$STAMP
MODE=deploy
ON_AIR_OK=0
for arg in "$@"; do
    case "$arg" in
        --check) MODE=check ;;
        --rollback) MODE=rollback ;;
        --on-air) ON_AIR_OK=1 ;;
        *) echo "usage: [sudo] bash $0 [--check | --rollback] [--on-air]"; exit 2 ;;
    esac
done

TV_PROMOTED=0
CUT=0              # the units and the front-door site are installed
APACHE_CUT=0       # Apache has been restarted with them
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
hdpvr_port() {  # the HD PVR's USB port (2-3), found by its USB id whether or not its driver bound
    local d
    for d in /sys/bus/usb/devices/*; do
        [ "$(cat "$d/idVendor" 2>/dev/null):$(cat "$d/idProduct" 2>/dev/null)" = "2040:4902" ] && { basename "$d"; return; }
    done
    echo "PORT"
}
# What brought it back on 2026-10-25: a power cycle, then a port that works (2-12 had turned bad).
hdpvr_reset() {
    local port; port=$(hdpvr_port)
    echo "unplug its power for 10 s; if its driver still does not bind (sudo dmesg | grep hdpvr), move it to another USB port. A USB re-plug alone: echo 0 | sudo tee /sys/bus/usb/devices/$port/authorized; sleep 3; echo 1 | sudo tee /sys/bus/usb/devices/$port/authorized"
}
lane_ready() {  # url lane
    [ "$(json "$1/api/v1/status" "bool(d['lanes']['$2']['stream'].get('ready'))")" = "True" ]
}
refused() {  # unit -> times its TasksMax refused a thread or process
    awk '$1 == "max" {print $2}' "/sys/fs/cgroup/system.slice/$1.service/pids.events"
}
unit_pid() { systemctl show -p MainPID --value "$1"; }
port_free() { ! ss -ltnH "sport = :$1" | grep -q .; }
lan_listening() {  # every LAN address the host has is listened on at :8080
    local address
    for address in $(ip -4 -o addr show scope global | awk '$2 !~ /^(docker|br-|veth|tailscale|virbr)/ {sub("/.*", "", $4); print $4}'); do
        ss -ltnH "src $address:8080" | grep -q . || return 1
    done
}
loopback_only() {  # ports: each listened on, and on 127.0.0.1 alone
    local port listeners
    for port in "$@"; do
        listeners=$(ss -ltnH "sport = :$port" | awk '{print $4}')
        [ -n "$listeners" ] || return 1
        echo "$listeners" | grep -qv "^127\.0\.0\.1:$port$" && return 1
    done
    return 0
}

precheck() {
    say "Prechecks"
    [ "$(git_v -C "$TV_WORKTREE" rev-parse --abbrev-ref HEAD)" = "$TV_BRANCH" ] || fail "$TV_WORKTREE is not on $TV_BRANCH"
    [ -z "$(git_v -C "$TV_WORKTREE" status --porcelain)" ] || fail "$TV_WORKTREE has uncommitted changes"
    [ "$(git_v -C "$TV" rev-parse --abbrev-ref HEAD)" = master ] || fail "$TV is not on master"
    git_v -C "$TV" merge-base --is-ancestor HEAD "$TV_BRANCH" || fail "$TV_BRANCH does not descend from master ($TV_BASE)"
    # Uncommitted changes in production stay through a fast-forward and a reset --keep, so the
    # branch's files must be clear of them; the candidate is staged with them, as production will run.
    local dirty overlap recording due on_air prepared address port mod
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
    lane_ready "$P" roku || fail "production's Roku capture is not running: is the HD PVR hung? Reset it: $(hdpvr_reset)"
    [ -n "$KEY" ] || fail "no VizelVision API key in $VV_CONFIG"
    on_air=$(json "$VV/v1/master" "json.dumps(d.get('on_air'))") || fail "VizelVision is not answering"
    prepared=$(json "$VV/v1/master" "json.dumps(d.get('prepared'))")
    if [ "$on_air" != "null" ] || [ "$prepared" != "null" ]; then
        [ "$ON_AIR_OK" = 1 ] || fail "VizelVision has something on air or prepared ($on_air); run after it, or pass --on-air to take it off and put it back"
        say "On air, to be put back afterwards: $on_air"
    fi
    # The front door.
    [ -x /usr/sbin/apache2 ] || fail "no /usr/sbin/apache2"
    for mod in proxy proxy_http headers alias; do
        [ -e "/etc/apache2/mods-enabled/$mod.load" ] || fail "Apache's $mod module is not enabled (sudo a2enmod $mod)"
    done
    [ ! -e "$APACHE_SITE" ] || fail "$APACHE_SITE is already there: is the split already deployed? (--rollback undoes it)"
    [ ! -e "$UNITS/tv-thing-feed.service" ] || fail "$UNITS/tv-thing-feed.service is already there: is the split already deployed?"
    cmp -s "$UNITS/tv-thing.service" "$TV/tv-thing.service" \
        || fail "the installed tv-thing.service is not $TV/tv-thing.service, which the rollback would put back"
    git_v -C "$TV" diff --quiet HEAD "$TV_BRANCH" -- tv-thing.service || fail "the branch changes tv-thing.service, the rollback's unit"
    grep -qE '^ProxyPass +/tv/ +http://127\.0\.0\.1:8080/' /etc/apache2/conf-available/viviserv-proxy.conf \
        || fail "viviserv-proxy.conf no longer proxies /tv/ to 127.0.0.1:8080"
    for address in $LAN_ADDRESSES; do
        ip -4 -o addr show | grep -q " inet $address/" || fail "this host has no $address: Apache could not listen on it"
    done
    for port in 8086 8087 $CF $CC $CD; do
        port_free "$port" || fail "port $port is taken: $(ss -ltnpH "sport = :$port" 2>/dev/null | head -1)"
    done
    say "Front door: Apache's modules, the LAN addresses and the ports are ready"
    say "The fast suite (about 2 minutes)"
    as_vivian bash -c "cd '$TV_WORKTREE' && '$PY' -m pytest -q -m 'not slow and not api and not hardware and not resource' --ignore=test_integration.py" \
        > "$LOGDIR/fast-suite.log" 2>&1 || fail "the fast suite failed: $LOGDIR/fast-suite.log"
    tail -1 "$LOGDIR/fast-suite.log"
    say "The staged split check (about a minute, no hardware, scratch ports)"
    as_vivian env STAGE="$LOGDIR/staged-split" timeout 300 bash "$TV_WORKTREE/scripts/staged_split_check.sh" \
        > "$LOGDIR/staged-split.log" 2>&1 || fail "the staged split check failed: $LOGDIR/staged-split.log"
    tail -1 "$LOGDIR/staged-split.log"
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
gate() {  # name, url, stream_sanity_check args (--server-pid for each service)...
    local name=$1 url=$2; shift 2
    # A gate locks every lane. A recording cancelled just before lets go of its lock only on
    # the VCR's next tick (the headroom check's did not, 2026-10-25).
    wait_for 60 "Every lane is unlocked" locks_clear "$url" || { say "a lane is still locked at $url"; return 1; }
    say "Gate: $name"
    as_vivian "$PY" "$STAGE/stream_sanity_check.py" --url "$url" "$@" 2>&1 | tee "$LOGDIR/gate-$name.log"
    return "${PIPESTATUS[0]}"
}

roku_gate() {  # name, url, pids...: three navigations, ending on ESPN for the re-tune check
    local name=$1 url=$2; shift 2
    gate "$name" "$url" "$@" --roku-target youtube-tv:ESPN --roku-target youtube-tv:NBC --retunes 3
}

pids_of() {  # units... -> --server-pid for each: the gate measures their trees as one
    local unit
    for unit in "$@"; do printf -- '--server-pid %s ' "$(unit_pid "$unit")"; done
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
        curl -s --max-time 15 -o "$LOGDIR/relay-$stream.m3u8" "$url/stream/internet/$stream/index.m3u8"
        if head -1 "$LOGDIR/relay-$stream.m3u8" 2>/dev/null | grep -q '^#EXTM3U'; then
            say "relay $stream: playing"
        else
            say "relay $stream: no playlist (fine if it is not live upstream)"
        fi
    done
}

split_checks() {  # front door, feed port, control port, then the main site's /tv (optional)
    local door=$1 fport=$2 cport=$3 site=${4:-} base path code feed_epoch
    say "Split checks ($door)"
    loopback_only "$fport" "$cport" || { say "the feed (:$fport) or control (:$cport) listens beyond 127.0.0.1: $(ss -ltnH "( sport = :$fport or sport = :$cport )")"; return 1; }
    say "the feed (:$fport) and control (:$cport) listen on 127.0.0.1 only"
    for base in "$door" $site; do
        for path in internal/internet/pentulive/resolve feed/v1/lanes; do
            code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$base/$path")
            [ "$code" = 404 ] || { say "$base/$path answered $code: it must never be routed (D3)"; return 1; }
        done
    done
    say "/internal/ and /feed/v1/ are never routed (D3)"
    feed_epoch=$(json "http://127.0.0.1:$fport/feed/v1/lanes" "d['feed_epoch']")
    for base in "$door" $site; do
        [ -n "$feed_epoch" ] && [ "$(json "$base/api/v1/status" "d['feed_epoch']")" = "$feed_epoch" ] \
            || { say "$base's status is not on the feed's epoch $feed_epoch"; return 1; }
    done
    say "the API reports the feed's epoch $feed_epoch"
    # The relay asks control's /internal/ for a looked-up URL (Yle; never fails the gate).
    code=$(curl -s -o "$LOGDIR/resolve.json" -w '%{http_code}' --max-time 15 "http://127.0.0.1:$cport/internal/internet/pentulive/resolve")
    say "control's /internal/ resolve of pentulive: $code"
}

headroom_test() {  # name, url, units...
    local name=$1 url=$2 unit; shift 2
    local cgroups=()
    for unit in "$@"; do cgroups+=(--cgroup "/sys/fs/cgroup/system.slice/$unit.service"); done
    say "Task headroom: a Roku navigation during a digital recording ($name)"
    as_vivian "$PY" "$STAGE/scripts/task_headroom_check.py" --url "$url" "${cgroups[@]}" 2>&1 | tee "$LOGDIR/headroom-$name.log"
    [ "${PIPESTATUS[0]}" = 0 ] || return 1
    no_refusals "$@"
}

no_refusals() {  # units...
    local unit
    for unit in "$@"; do
        [ "$(refused "$unit")" = 0 ] || { say "$unit: TasksMax refused $(refused "$unit") thread(s) or process(es)"; return 1; }
    done
    say "TasksMax refused nothing ($*)"
}

drill() {  # name, base url, restart_drill args... (as root: its --command runs systemctl)
    local name=$1 base=$2; shift 2
    say "Drill: $name"
    "$PY" "$TV_WORKTREE/scripts/restart_drill.py" --tv "$base/" "$@" 2>&1 | tee "$LOGDIR/drill-$name.log"
    return "${PIPESTATUS[0]}"
}

# --- the candidate ----------------------------------------------------------------------------
stop_candidate() {
    systemctl stop "$CAND_APACHE.service" "$CAND_CONTROL.service" "$CAND_FEED.service" 2>/dev/null
}

start_candidate() {
    say "Staging the candidate in $STAGE"
    as_vivian mkdir -p "$STAGE/streams" "$STAGE/recordings" "$STAGE/state" "$STAGE/apache"
    # Production's tree as it runs (its uncommitted changes, Roku calibration and lineup state
    # included), then the branch's files over it: what promotion will leave running.
    as_vivian rsync -a --exclude /.git --exclude /streams --exclude /state --exclude __pycache__ "$TV/" "$STAGE/"
    git_v -C "$TV" diff --name-only --diff-filter=d HEAD "$TV_BRANCH" > "$LOGDIR/branch-files.txt"
    git_v -C "$TV" diff --name-only --diff-filter=D HEAD "$TV_BRANCH" > "$LOGDIR/branch-deleted.txt"
    chown vivian:vivian "$LOGDIR"/branch-*.txt
    as_vivian rsync -a --files-from="$LOGDIR/branch-files.txt" "$TV_WORKTREE/" "$STAGE/" || fail "could not stage the branch"
    (cd "$STAGE" && xargs -r -d '\n' rm -f -- < "$LOGDIR/branch-deleted.txt")
    systemctl reset-failed 'tv-thing-candidate*' 2>/dev/null
    # Both services share the candidate's dirs and UDP ports, as production's share theirs. No PSIP
    # guide sweeps: one would take the tuner mid-gate.
    local cand_env=(--setenv=TV_THING_STREAMS_DIR="$STAGE/streams" --setenv=TV_THING_RECORDINGS_DIR="$STAGE/recordings"
               --setenv=TV_THING_STATE_DIR="$STAGE/state" --setenv=TV_THING_EPG=0
               --setenv=TV_THING_TV_UDP_PORT=18420 --setenv=TV_THING_RECORD_TAP_PORT=18484
               --setenv=TV_THING_ROKU_TAP_PORT=18470 --setenv=TV_THING_RECORD_MIN_FREE_GB=1
               --setenv=PYTHONUNBUFFERED=1)
    # The feed: deploy/systemd/tv-thing-feed.service's limits, and a hard one-hour cap.
    systemd-run --unit="$CAND_FEED" --collect --uid=vivian --gid=vivian \
        -p WorkingDirectory="$STAGE" -p MemoryMax=1800M -p CPUQuota=800% -p TasksMax=192 \
        -p LimitFSIZE=16M:16G -p OOMPolicy=continue -p RuntimeMaxSec=3600 -p TimeoutStopSec=5 \
        "${cand_env[@]}" --setenv=PORT=$CF --setenv=TV_THING_CONTROL_URL=http://127.0.0.1:$CC \
        --setenv=TV_THING_VIDEO_ENCODER=cpu --setenv=TV_THING_MAX_GPU_TEMP_C=90 \
        "$PY" "$STAGE/feed_server.py" || fail "could not start the candidate's feed"
    # Control: deploy/systemd/tv-thing.service's limits. It comes back from a kill (the drill), as
    # the real unit does. RuntimeMaxSec would count as a failure and restart it every hour, so the
    # cap is timeout's, whose 124 is never restarted.
    systemd-run --unit="$CAND_CONTROL" --collect --uid=vivian --gid=vivian \
        -p WorkingDirectory="$STAGE" -p MemoryMax=1200M -p CPUQuota=400% -p TasksMax=160 \
        -p LimitFSIZE=16M:16G -p OOMPolicy=stop -p TimeoutStopSec=5 \
        -p Restart=on-failure -p RestartSec=2 -p RestartPreventExitStatus=124 \
        "${cand_env[@]}" --setenv=TV_THING_ROLE=control --setenv=PORT=$CC \
        --setenv=TV_THING_FEED_URL=http://127.0.0.1:$CF/ \
        timeout 3600 "$PY" "$STAGE/server.py" || fail "could not start the candidate's control"
    # Its front door: the production routes in a private Apache, as vivian, in the foreground
    # under its own capped unit.
    local modules=/usr/lib/apache2/modules
    as_vivian tee "$STAGE/apache/httpd.conf" >/dev/null <<CONF
ServerRoot "$STAGE/apache"
DefaultRuntimeDir "$STAGE/apache"
PidFile "$STAGE/apache/httpd.pid"
ErrorLog "$STAGE/apache/error.log"
LogLevel warn
LoadModule mpm_event_module $modules/mod_mpm_event.so
LoadModule authz_core_module $modules/mod_authz_core.so
LoadModule mime_module $modules/mod_mime.so
LoadModule alias_module $modules/mod_alias.so
LoadModule headers_module $modules/mod_headers.so
LoadModule proxy_module $modules/mod_proxy.so
LoadModule proxy_http_module $modules/mod_proxy_http.so
TypesConfig /etc/mime.types
ServerName localhost
Listen 127.0.0.1:$CD
Define TV_THING_FEED_PORT $CF
Define TV_THING_CONTROL_PORT $CC
<VirtualHost 127.0.0.1:$CD>
    Include "$STAGE/deploy/apache/tv-thing-routes.conf"
</VirtualHost>
CONF
    systemd-run --unit="$CAND_APACHE" --collect --uid=vivian --gid=vivian \
        -p RuntimeMaxSec=3600 -p TimeoutStopSec=5 \
        /usr/sbin/apache2 -d "$STAGE/apache" -f "$STAGE/apache/httpd.conf" -DFOREGROUND \
        || fail "could not start the candidate's Apache"
    wait_for 60 "The candidate's front door answers on :$CD" curl -sf --max-time 2 -o /dev/null "$C/api/v1" \
        || fail "the candidate's front door does not answer: $(cat "$STAGE/apache/error.log" 2>/dev/null)"
}

# --- the cut-over and its undoing -------------------------------------------------------------
cut_over() {
    say "Cut-over: installing the units and the front door (what they replace: $LOGDIR/before)"
    mkdir -p "$LOGDIR/before"
    cp -p "$UNITS/tv-thing.service" /etc/apache2/conf-available/viviserv-proxy.conf "$LOGDIR/before/"
    CUT=1
    install -m 644 "$TV/deploy/systemd/tv-thing-feed.service" "$TV/deploy/systemd/tv-thing.service" "$UNITS/" \
        || fail "could not install the units"
    install -D -m 644 "$TV/deploy/apache/apache2-front-door.conf" "$APACHE_DROPIN" || fail "could not install Apache's drop-in"
    install -m 644 "$TV/deploy/apache/tv-thing-front-door.conf" "$APACHE_SITE" || fail "could not install the front-door site"
    a2ensite -q tv-thing-front-door || fail "a2ensite tv-thing-front-door failed"
    apache2ctl configtest > "$LOGDIR/apache-configtest.log" 2>&1 \
        || fail "Apache's config test failed with the front door: $(cat "$LOGDIR/apache-configtest.log")"
    say "Apache's config test passed"
    systemctl daemon-reload
    port_free_but_tailscale || fail ":8080 is still held: $(ss -ltnpH 'sport = :8080')"
    systemctl enable -q tv-thing-feed.service
    systemctl start tv-thing-feed.service || fail "tv-thing-feed did not start"
    wait_for 30 "The feed answers" curl -sf --max-time 2 -o /dev/null http://127.0.0.1:8086/feed/v1/lanes || fail "the feed does not answer"
    systemctl start tv-thing.service || fail "tv-thing (control) did not start"
    APACHE_CUT=1
    systemctl restart apache2.service || fail "Apache did not restart with the front door"
    say "The feed, control and the front door are up"
}

undo_cut_over() {  # back to one process's units and Apache without the front door
    say "Taking the front door and the split units out"
    systemctl stop tv-thing.service tv-thing-feed.service
    systemctl disable -q tv-thing-feed.service 2>/dev/null
    a2dissite -q tv-thing-front-door 2>/dev/null
    rm -f "$APACHE_SITE" "$APACHE_DROPIN" "$UNITS/tv-thing-feed.service"
    rmdir "$(dirname "$APACHE_DROPIN")" 2>/dev/null
    install -m 644 "$TV/tv-thing.service" "$UNITS/tv-thing.service"
    systemctl daemon-reload
    if [ "$APACHE_CUT" = 1 ] || [ "$MODE" = rollback ]; then
        apache2ctl configtest > "$LOGDIR/apache-configtest-rollback.log" 2>&1 \
            || { say "APACHE'S CONFIG TEST FAILS after the rollback: $LOGDIR/apache-configtest-rollback.log"; return 1; }
        systemctl restart apache2.service || { say "APACHE DID NOT RESTART: systemctl status apache2"; return 1; }
    fi
    wait_for 15 ":8080 is free for the one process" port_free_but_tailscale || { say ":8080 is still held: $(ss -ltnpH 'sport = :8080')"; return 1; }
}
# tailscale0's own :8080 (another service, IPv4 and IPv6) is never TV Thing's.
port_free_but_tailscale() { [ -z "$(ss -ltnH 'sport = :8080' | awk '{print $4}' | grep -v '%tailscale0:')" ]; }

start_one_process() {
    hdpvr_answers >/dev/null 2>&1 || say "THE HD PVR DOES NOT ANSWER: the Roku lane stays down until it is reset: $(hdpvr_reset)"
    systemctl start tv-thing.service
    wait_for 120 "TV Thing is back" lane_ready "$P" digital || say "TV Thing did not come back; check journalctl -u tv-thing"
    wait_for 60 "TV Thing's Roku capture is back" lane_ready "$P" roku || say "TV Thing's Roku capture did not come back: reset the HD PVR: $(hdpvr_reset)"
    wait_for 60 "TV Thing listens on every LAN address" lan_listening || say "TV Thing is not listening on the LAN: ss -ltnp 'sport = :8080'"
}

# --- rollback ---------------------------------------------------------------------------------
rollback() {
    say "ROLLING BACK"
    local restart=1
    for unit in "$CAND_FEED" "$CAND_CONTROL" "$CAND_APACHE"; do
        journalctl -u "$unit" --no-pager > "$LOGDIR/$unit.journal" 2>&1
    done
    stop_candidate
    if [ "$CUT" = 1 ]; then
        journalctl -u tv-thing-feed -u tv-thing --since "-30min" --no-pager > "$LOGDIR/production.journal" 2>&1
        undo_cut_over || { restart=0; say "tv-thing stays STOPPED: fix Apache, then sudo systemctl start tv-thing"; }
    else
        systemctl stop tv-thing.service
    fi
    if [ "$TV_PROMOTED" = 1 ]; then
        # --keep, never --hard: it restores only the branch's files and keeps roku_app/'s uncommitted work.
        if as_vivian git -C "$TV" reset -q --keep "$TV_BASE"; then
            say "Production's checkout is back at $TV_BASE, its uncommitted work kept"
        else
            restart=0
            say "git reset --keep $TV_BASE refused: tv-thing stays STOPPED rather than run the failed build."
            say "Check git -C $TV status, reset it to $TV_BASE, then: sudo systemctl start tv-thing"
        fi
    fi
    [ "$restart" = 1 ] && start_one_process
    start_vizelvision
    restore_air
    say "Rolled back to $TV_BASE, one process. Logs: $LOGDIR"
}

# ---------------------------------------------------------------------------
if [ "$MODE" = rollback ]; then
    # Later, by hand: the one process on today's code (it reads what the feed wrote: the journal
    # stays in state/). Every stream drops once. VizelVision is left as it is: check it after.
    [ "$(id -u)" = 0 ] || { echo "Run with sudo."; exit 1; }
    trap '' HUP PIPE
    exec > >(tee --output-error=warn -a "$LOGDIR/rollback.log") 2>&1
    [ -e "$UNITS/tv-thing-feed.service" ] || [ -e "$APACHE_SITE" ] || { say "The split is not deployed: nothing to roll back"; exit 1; }
    undo_cut_over || exit 1
    start_one_process
    say "Back to one process ($(git_v -C "$TV" rev-parse --short HEAD)). Check VizelVision's master. Logs: $LOGDIR"
    exit 0
fi

precheck
[ "$MODE" = check ] && { say "Prechecks only: nothing was stopped. Logs: $LOGDIR"; exit 0; }
[ "$(id -u)" = 0 ] || { echo "Run with sudo (the units, Apache and the candidate need root)."; exit 1; }
# A dropped SSH session must not stop a deploy halfway: on 2026-10-26 it died mid-gate,
# promoted and never rolled back. Ignore the hang-up, and keep logging to the file once
# the terminal has gone (tee would otherwise exit, and the next line written kill us).
trap '' HUP PIPE
trap 'stop_candidate >/dev/null 2>&1; chown -R vivian:vivian "$LOGDIR" 2>/dev/null' EXIT
exec > >(tee --output-error=warn -a "$LOGDIR/deploy.log") 2>&1
say "Logs in $LOGDIR (it carries on if this terminal goes away: tail -f $LOGDIR/deploy.log)"

# --- 2-3. VizelVision and production stopped --------------------------------------------------
stop_vizelvision
say "Stopping production TV Thing"
systemctl stop tv-thing.service || fail "could not stop tv-thing"

# --- 4. the candidate -------------------------------------------------------------------------
start_candidate
wait_for 90 "The candidate's digital lane is ready (through its front door)" lane_ready "$C" digital || fail "candidate digital lane"
wait_for 60 "The candidate's Roku capture is ready" lane_ready "$C" roku || fail "candidate Roku capture"
sleep 20
journalctl -u "$CAND_FEED" -u "$CAND_CONTROL" --no-pager | grep -E 'Reaped|Traceback|FeedConfigError' \
    && say "(the candidate's start logged the lines above)" || say "The candidate started clean"
PIDS=$(pids_of "$CAND_FEED" "$CAND_CONTROL")
# shellcheck disable=SC2086  # PIDS is the --server-pid flags, split on purpose
gate candidate-digital "$C" $PIDS --retunes 3 || fail "candidate digital gate"
headroom_test candidate "$C" "$CAND_FEED" "$CAND_CONTROL" || fail "candidate task headroom"
# shellcheck disable=SC2086
roku_gate candidate-roku "$C" $PIDS || fail "candidate Roku gate"
phase1_checks "$C" "$STAGE/state/feed/recordings-journal.jsonl" 1 || fail "candidate Phase 1 checks"
split_checks "$C" "$CF" "$CC" || fail "candidate split checks"
no_refusals "$CAND_FEED" "$CAND_CONTROL" || fail "the candidate's TasksMax refused"
# The proof, rehearsed: control restarted, then killed, and nothing the feed plays moves.
drill candidate-A1 "$C" --vizelvision '' --seconds 60 --command "systemctl restart $CAND_CONTROL.service" \
    || fail "the candidate's drill A1: restarting control moved a stream"
wait_for 60 "The candidate's control answers again" lane_ready "$C" digital || fail "the candidate's control did not come back"
drill candidate-A2 "$C" --vizelvision '' --seconds 60 --command "systemctl kill -s KILL $CAND_CONTROL.service" \
    || fail "the candidate's drill A2: killing control moved a stream"
wait_for 60 "The candidate's control answers again" lane_ready "$C" digital || fail "the candidate's control did not come back"
journalctl -u "$CAND_FEED" --no-pager > "$LOGDIR/candidate-feed.journal" 2>&1
journalctl -u "$CAND_CONTROL" --no-pager > "$LOGDIR/candidate-control.journal" 2>&1
cp -p "$STAGE/state/feed/recordings-journal.jsonl" "$LOGDIR/candidate-journal.jsonl" 2>/dev/null
stop_candidate
pgrep -u vivian -f "$STAGE/" >/dev/null && fail "the candidate left processes behind: $(pgrep -u vivian -af "$STAGE/")"
hdpvr_answers >/dev/null 2>&1 || fail "the HD PVR stopped answering after the candidate: reset it before deploying again: $(hdpvr_reset)"

# --- 5. promote, cut over, and gate production ------------------------------------------------
say "Promoting TV Thing"
TV_PROMOTED=1
as_vivian git -C "$TV" merge -q --ff-only "$TV_BRANCH" || fail "could not fast-forward $TV"
cut_over
wait_for 120 "Production's digital lane is ready" lane_ready "$P" digital || fail "production digital lane"
wait_for 60 "Production's Roku capture is ready" lane_ready "$P" roku || fail "production Roku capture"
wait_for 60 "The front door listens on every LAN address" lan_listening || fail "the front door is not listening on the LAN"
sleep 20
journalctl -u tv-thing-feed -u tv-thing --since "-3min" --no-pager | grep -E 'Reaped|Traceback|FeedConfigError' \
    && say "(production's start logged the lines above)" || say "Production started clean"
PIDS=$(pids_of tv-thing-feed tv-thing)
# shellcheck disable=SC2086
gate production-digital "$P" $PIDS --retunes 3 || fail "production digital gate"
# shellcheck disable=SC2086
roku_gate production-roku "$P" $PIDS || fail "production Roku gate"
no_refusals tv-thing-feed tv-thing || fail "production's TasksMax refused"
phase1_checks "$P" "" 0 || fail "production Phase 1 checks"
split_checks "$P" 8086 8087 "$P_TV" || fail "production split checks"

# --- 6. VizelVision back, and the proof -------------------------------------------------------
say "Starting VizelVision"
start_vizelvision
VV_ARGS=()
restore_air || { VV_ARGS=(--vizelvision ''); say "VizelVision did not settle: the drills watch TV Thing alone"; }
STOPPED=0          # past the gates: from here nothing rolls back by itself
PROOF=ok
drill production-A1 "$P" "${VV_ARGS[@]}" --seconds 90 --command "systemctl restart tv-thing.service" || PROOF=failed
wait_for 60 "Control answers again" lane_ready "$P" digital || PROOF=failed
if [ "$PROOF" = ok ]; then
    drill production-A2 "$P" "${VV_ARGS[@]}" --seconds 90 --command "systemctl kill -s KILL tv-thing.service" || PROOF=failed
    wait_for 60 "Control answers again" lane_ready "$P" digital || PROOF=failed
fi

as_vivian rm -rf "$STAGE/streams" "$STAGE/recordings"
if [ "$PROOF" = ok ]; then
    say "DONE. Production runs $TV_BRANCH ($(git_v -C "$TV" rev-parse --short HEAD)) as two services; a control restart drops nothing. Logs: $LOGDIR"
else
    say "DEPLOYED, BUT THE PROOF FAILED: a restart or kill of control moved something ($LOGDIR/drill-production-A*.log)."
    say "The split passed its gates and stays. To go back to one process: sudo bash $TV/scripts/deploy-feed-split.sh --rollback"
    exit 1
fi
exit
}

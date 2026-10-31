#!/bin/bash
# Deploy the host-config branch: this host's settings (host_config.py), the Config page, and the
# tests in tests/. It changes what the feed runs (feed.json as this host has it, the digital
# lane's adapter, the encoder and recordings constants, the Roku capture's start), so AGENTS.md's
# gate applies, though on viviserv every one of those resolves as before. The split is already
# in: the units and the front door are not touched.
#
#   bash /home/vivian/tv_thing-host-config/scripts/deploy-host-config.sh --check        # prechecks, no sudo
#   sudo bash /home/vivian/tv_thing-host-config/scripts/deploy-host-config.sh           # deploy
#   sudo bash /home/vivian/tv_thing-host-config/scripts/deploy-host-config.sh --on-air  # see below
#
# A dropped SSH session does not stop it. It stops VizelVision's web and master for about 40-60
# minutes, and TV Thing's feed and control for 25-40 of them. With something on air it refuses,
# unless you pass --on-air; whatever was on air is put back at the end either way.
#
#   1. Prechecks: the fast suite; production on master, fast-forwarding to the branch, its
#      uncommitted work clear of the branch's files; nothing recording or due within the hour;
#      the HD PVR answering; the installed units the repo's; the staged split check; Cairo Radio
#      from a staged web-basic for ten minutes (scripts/web_stream_check.py); the drill.
#   2. Note what VizelVision has on air, stop its web and master, and wait for TV Thing's locks.
#   3. Stop production's control and feed (web-basic keeps playing).
#   4. The candidate, as deploy-feed-split.sh builds one: production's tree with the branch's
#      files over it, a feed (:8091) and a control (:8092) with the units' limits, behind a
#      private Apache on :8098. Through :8098: 3 digital retunes; a Roku navigation during a
#      recording; 3 Roku navigations; the Phase 1 checks; the split checks; the Config checks
#      (primary, Roku on, adapter 0); the drill restarting its control.
#   5. Promote: fast-forward master, start the feed and control, restart web-basic. The same
#      gates on production.
#   6. VizelVision back, what was on air put back, Up next resumed. Then the drill restarting
#      control on air, and Cairo Radio from production's web-basic for ten minutes.
# Any failure in 2-5 rolls back: `git reset --keep` (never --hard, so roku_app/'s uncommitted
# work survives), and the services started on the old build. A failure in 6 does not roll back:
# the build has passed its gates. It says what failed.
set -uo pipefail
# Read whole before running: editing a running script made bash resume mid-command (2026-10-06).
{

TV=/home/vivian/tv_thing
TV_WORKTREE=/home/vivian/tv_thing-host-config
TV_BRANCH=host-config
PY=/home/vivian/miniconda3/bin/python3
DEVICE=/dev/v4l/by-id/usb-AMBA_Hauppauge_HD_PVR_00A4E5AC-video-index0
P=http://127.0.0.1:8080
P_TV=http://127.0.0.1/tv          # the main site's /tv/ (viviserv-proxy.conf)
CF=8091 CC=8092 CD=8098           # the candidate's feed, control and front door
CW=18185                          # the staged web-basic's
C=http://127.0.0.1:$CD
VV=http://127.0.0.1:6970
VV_CONFIG=/home/vivian/vizelVision/config.master.json
UNITS=/etc/systemd/system
STAMP=$(date +%Y%m%d-%H%M%S)
LOGDIR=/home/vivian/host-config-$STAMP
STAGE=/home/vivian/tv_thing-candidate.host-config-$STAMP
CAND_FEED=tv-thing-candidate-feed-$STAMP
CAND_CONTROL=tv-thing-candidate-control-$STAMP
CAND_APACHE=tv-thing-candidate-apache-$STAMP
MODE=deploy
ON_AIR_OK=0
for arg in "$@"; do
    case "$arg" in
        --check) MODE=check ;;
        --on-air) ON_AIR_OK=1 ;;
        *) echo "usage: [sudo] bash $0 [--check] [--on-air]"; exit 2 ;;
    esac
done

TV_PROMOTED=0
WEB_RESTARTED=0
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
# Selecting the HD PVR's input answers at once; when the device has hung, it blocks until killed
# (2026-10-25). --get-input is no test: the driver answers it from memory.
hdpvr_answers() { timeout -k 2 5 v4l2-ctl --device "$DEVICE" --set-input=0; }
hdpvr_port() {
    local d
    for d in /sys/bus/usb/devices/*; do
        [ "$(cat "$d/idVendor" 2>/dev/null):$(cat "$d/idProduct" 2>/dev/null)" = "2040:4902" ] && { basename "$d"; return; }
    done
    echo "PORT"
}
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

web_stream_check() {  # name, base, status url, pid
    say "Cairo Radio from $2 for ten minutes ($1)"
    as_vivian timeout 1200 "$PY" "$TV_WORKTREE/scripts/web_stream_check.py" cairo-radio --base "$2" \
        --status "$3" --pid "$4" > "$LOGDIR/web-stream-$1.log" 2>&1
    local status=$?
    tail -3 "$LOGDIR/web-stream-$1.log"
    return $status
}

staged_web_basic_check() {  # the branch's web-basic, as vivian, on its own port, folder and pid
    local script=$TV_WORKTREE/web_basic.py pid status
    as_vivian mkdir -p "$LOGDIR/web-basic-streams"
    (cd "$TV_WORKTREE" && as_vivian env PORT=$CW TV_THING_WEB_STREAMS_DIR="$LOGDIR/web-basic-streams" \
        TV_THING_CONTROL_URL=http://127.0.0.1:8087 PYTHONUNBUFFERED=1 \
        timeout 1500 "$PY" "$script" >> "$LOGDIR/web-basic-staged.log" 2>&1) &
    wait_for 20 "The staged web-basic answers on :$CW" curl -sf --max-time 2 -o /dev/null "http://127.0.0.1:$CW/web/v1/status" \
        || { pkill -u vivian -f "^$PY $script$"; say "the staged web-basic did not start: $LOGDIR/web-basic-staged.log"; return 1; }
    # Its own path picks it out: production's web-basic runs $TV/web_basic.py.
    pid=$(pgrep -u vivian -f "^$PY $script$" | head -1)
    web_stream_check staged "http://127.0.0.1:$CW" "http://127.0.0.1:$CW/web/v1/status" "$pid"
    status=$?
    pkill -u vivian -f "^$PY $script$"; sleep 1
    return $status
}

precheck() {
    say "Prechecks"
    [ "$(git_v -C "$TV_WORKTREE" rev-parse --abbrev-ref HEAD)" = "$TV_BRANCH" ] || fail "$TV_WORKTREE is not on $TV_BRANCH"
    [ -z "$(git_v -C "$TV_WORKTREE" status --porcelain)" ] || fail "$TV_WORKTREE has uncommitted changes"
    [ "$(git_v -C "$TV" rev-parse --abbrev-ref HEAD)" = master ] || fail "$TV is not on master"
    git_v -C "$TV" merge-base --is-ancestor HEAD "$TV_BRANCH" || fail "$TV_BRANCH does not descend from master ($TV_BASE)"
    local dirty overlap recording due on_air prepared port unit
    dirty=$(git_v -C "$TV" status --porcelain | awk '{print $NF}' | sort)
    overlap=$(comm -12 <(echo "$dirty") <(git_v -C "$TV" diff --name-only --no-renames HEAD "$TV_BRANCH" | sort))
    [ -z "$overlap" ] || fail "$TV has uncommitted changes to files the branch changes: $overlap"
    [ -z "$dirty" ] || say "Production's $(echo "$dirty" | wc -l) other uncommitted path(s) stay, and are staged with the candidate"
    git_v -C "$TV" diff --quiet HEAD "$TV_BRANCH" -- deploy/ || fail "the branch changes deploy/: this script does not install units or routes"
    for unit in tv-thing-feed tv-thing tv-thing-web-basic; do
        cmp -s "$UNITS/$unit.service" "$TV/deploy/systemd/$unit.service" || fail "the installed $unit.service is not the repo's"
    done
    [ -e /home/vivian/.config/tv-thing/host.json ] \
        && say "NOTE: viviserv has a settings file, which the new build will read: $(cat /home/vivian/.config/tv-thing/host.json)"
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
    for port in $CF $CC $CD $CW; do
        port_free "$port" || fail "port $port is taken: $(ss -ltnpH "sport = :$port" 2>/dev/null | head -1)"
    done
    say "The fast suite (about 2 minutes)"
    as_vivian bash -c "cd '$TV_WORKTREE' && '$PY' -m pytest -q -m 'not slow and not api and not hardware and not resource' --ignore=tests/test_integration.py" \
        > "$LOGDIR/fast-suite.log" 2>&1 || fail "the fast suite failed: $LOGDIR/fast-suite.log"
    tail -1 "$LOGDIR/fast-suite.log"
    say "The staged split check (about a minute, no hardware, scratch ports)"
    as_vivian env STAGE="$LOGDIR/staged-split" timeout 300 bash "$TV_WORKTREE/scripts/staged_split_check.sh" \
        > "$LOGDIR/staged-split.log" 2>&1 || fail "the staged split check failed: $LOGDIR/staged-split.log"
    tail -1 "$LOGDIR/staged-split.log"
    staged_web_basic_check || fail "Cairo Radio from the branch's web-basic: $LOGDIR/web-stream-staged.log"
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
    wait_for 300 "$before is on air again" air_is "$before" \
        || { say "$before is not back on air after 5 minutes: check VizelVision"; return 1; }
}

# --- gates ------------------------------------------------------------------------------------
gate() {  # name, url, stream_sanity_check args (--server-pid for each service)...
    local name=$1 url=$2; shift 2
    wait_for 60 "Every lane is unlocked" locks_clear "$url" || { say "a lane is still locked at $url"; return 1; }
    say "Gate: $name"
    as_vivian "$PY" "$STAGE/scripts/stream_sanity_check.py" --url "$url" "$@" 2>&1 | tee "$LOGDIR/gate-$name.log"
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
}

config_checks() {  # url: viviserv is the primary, with feed.json's adapter and its Roku lane
    local url=$1 got
    say "Config checks ($url)"
    got=$(json "$url/api/v1/config" "(lambda s: (d['node'], d['node_role'], s['roku'], s['digital_adapter'], s['video_encoder']))({i['id']: i['effective'] for i in d['settings']})")
    [ "$got" = "('viviserv', 'primary', 'on', 0, 'cpu')" ] || { say "the Config API says $got"; return 1; }
    [ "$(json "$url/api/v1/status" "d['lanes']['roku'].get('available')")" = "True" ] || { say "the Roku lane is not available"; return 1; }
    curl -sf --max-time 5 "$url/config.html" | grep -q '<title>Config · TV Thing</title>' || { say "config.html is not served"; return 1; }
    say "Config: viviserv, primary, Roku on, adapter 0, cpu; the Roku lane available; config.html served"
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
    # included), then the branch's files over it, and its removals: what promotion leaves running.
    as_vivian rsync -a --exclude /.git --exclude /streams --exclude /state --exclude __pycache__ "$TV/" "$STAGE/"
    git_v -C "$TV" diff --name-only --no-renames --diff-filter=d HEAD "$TV_BRANCH" > "$LOGDIR/branch-files.txt"
    git_v -C "$TV" diff --name-only --no-renames --diff-filter=D HEAD "$TV_BRANCH" > "$LOGDIR/branch-deleted.txt"
    chown vivian:vivian "$LOGDIR"/branch-*.txt
    as_vivian rsync -a --files-from="$LOGDIR/branch-files.txt" "$TV_WORKTREE/" "$STAGE/" || fail "could not stage the branch"
    (cd "$STAGE" && xargs -r -d '\n' rm -f -- < "$LOGDIR/branch-deleted.txt")
    systemctl reset-failed 'tv-thing-candidate*' 2>/dev/null
    local cand_env=(--setenv=TV_THING_STREAMS_DIR="$STAGE/streams" --setenv=TV_THING_RECORDINGS_DIR="$STAGE/recordings"
               --setenv=TV_THING_STATE_DIR="$STAGE/state" --setenv=TV_THING_EPG=0
               --setenv=TV_THING_TV_UDP_PORT=18420 --setenv=TV_THING_RECORD_TAP_PORT=18484
               --setenv=TV_THING_ROKU_TAP_PORT=18470 --setenv=TV_THING_RECORD_MIN_FREE_GB=1
               --setenv=PYTHONUNBUFFERED=1)
    systemd-run --unit="$CAND_FEED" --collect --uid=vivian --gid=vivian \
        -p WorkingDirectory="$STAGE" -p MemoryMax=1800M -p CPUQuota=800% -p TasksMax=192 \
        -p LimitFSIZE=16M:16G -p OOMPolicy=continue -p RuntimeMaxSec=3600 -p TimeoutStopSec=5 \
        "${cand_env[@]}" --setenv=PORT=$CF --setenv=TV_THING_CONTROL_URL=http://127.0.0.1:$CC \
        --setenv=TV_THING_VIDEO_ENCODER=cpu --setenv=TV_THING_MAX_GPU_TEMP_C=90 \
        "$PY" "$STAGE/feed_server.py" || fail "could not start the candidate's feed"
    systemd-run --unit="$CAND_CONTROL" --collect --uid=vivian --gid=vivian \
        -p WorkingDirectory="$STAGE" -p MemoryMax=1200M -p CPUQuota=400% -p TasksMax=160 \
        -p LimitFSIZE=16M:16G -p OOMPolicy=stop -p TimeoutStopSec=5 \
        -p Restart=on-failure -p RestartSec=2 -p RestartPreventExitStatus=124 \
        "${cand_env[@]}" --setenv=TV_THING_ROLE=control --setenv=PORT=$CC \
        --setenv=TV_THING_FEED_URL=http://127.0.0.1:$CF/ \
        timeout 3600 "$PY" "$STAGE/server.py" || fail "could not start the candidate's control"
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

# --- production -------------------------------------------------------------------------------
start_production() {  # the feed, then control
    hdpvr_answers >/dev/null 2>&1 || say "THE HD PVR DOES NOT ANSWER: the Roku lane stays down until it is reset: $(hdpvr_reset)"
    systemctl start tv-thing-feed.service || return 1
    wait_for 30 "The feed answers" curl -sf --max-time 2 -o /dev/null http://127.0.0.1:8086/feed/v1/lanes || return 1
    systemctl start tv-thing.service || return 1
    wait_for 30 "Control answers" curl -sf --max-time 2 -o /dev/null http://127.0.0.1:8087/api/v1 || return 1
}

rollback() {
    say "ROLLING BACK"
    local restart=1 unit
    for unit in "$CAND_FEED" "$CAND_CONTROL" "$CAND_APACHE"; do
        journalctl -u "$unit" --no-pager > "$LOGDIR/$unit.journal" 2>&1
    done
    stop_candidate
    journalctl -u tv-thing-feed -u tv-thing -u tv-thing-web-basic --since "-90min" --no-pager > "$LOGDIR/production.journal" 2>&1
    systemctl stop tv-thing.service tv-thing-feed.service
    if [ "$TV_PROMOTED" = 1 ]; then
        # --keep, never --hard: it restores only the branch's files and keeps roku_app/'s uncommitted work.
        if as_vivian git -C "$TV" reset -q --keep "$TV_BASE"; then
            say "Production's checkout is back at $TV_BASE, its uncommitted work kept"
        else
            restart=0
            say "git reset --keep $TV_BASE refused: the feed and control stay STOPPED rather than run the failed build."
            say "Check git -C $TV status, reset it to $TV_BASE, then: sudo systemctl start tv-thing-feed tv-thing; sudo systemctl restart tv-thing-web-basic"
        fi
    fi
    if [ "$restart" = 1 ]; then
        start_production || say "TV Thing did not come back: journalctl -u tv-thing-feed -u tv-thing"
        [ "$WEB_RESTARTED" = 1 ] && systemctl restart tv-thing-web-basic.service
        wait_for 120 "TV Thing is back" lane_ready "$P" digital || say "TV Thing's digital lane did not come back"
        wait_for 60 "TV Thing's Roku capture is back" lane_ready "$P" roku || say "the Roku capture did not come back: reset the HD PVR: $(hdpvr_reset)"
    fi
    start_vizelvision
    restore_air
    say "Rolled back to $TV_BASE. Logs: $LOGDIR"
}

# ---------------------------------------------------------------------------
precheck
[ "$MODE" = check ] && { say "Prechecks only: nothing was stopped. Logs: $LOGDIR"; exit 0; }
[ "$(id -u)" = 0 ] || { echo "Run with sudo (the units and the candidate need root)."; exit 1; }
# A dropped SSH session must not stop a deploy halfway (2026-10-26): ignore the hang-up, and keep
# logging to the file once the terminal has gone.
trap '' HUP PIPE
trap 'stop_candidate >/dev/null 2>&1; chown -R vivian:vivian "$LOGDIR" 2>/dev/null' EXIT
exec > >(tee --output-error=warn -a "$LOGDIR/deploy.log") 2>&1
say "Logs in $LOGDIR (it carries on if this terminal goes away: tail -f $LOGDIR/deploy.log)"

# --- 2-3. VizelVision and production stopped --------------------------------------------------
stop_vizelvision
say "Stopping production TV Thing's control and feed"
systemctl stop tv-thing.service tv-thing-feed.service || fail "could not stop TV Thing"

# --- 4. the candidate -------------------------------------------------------------------------
start_candidate
wait_for 90 "The candidate's digital lane is ready (through its front door)" lane_ready "$C" digital || fail "candidate digital lane"
wait_for 60 "The candidate's Roku capture is ready" lane_ready "$C" roku || fail "candidate Roku capture"
sleep 20
journalctl -u "$CAND_FEED" -u "$CAND_CONTROL" --no-pager | grep -E 'Reaped|Traceback|FeedConfigError|Roku lane off' \
    && say "(the candidate's start logged the lines above)" || say "The candidate started clean"
PIDS=$(pids_of "$CAND_FEED" "$CAND_CONTROL")
# shellcheck disable=SC2086  # PIDS is the --server-pid flags, split on purpose
gate candidate-digital "$C" $PIDS --retunes 3 || fail "candidate digital gate"
headroom_test candidate "$C" "$CAND_FEED" "$CAND_CONTROL" || fail "candidate task headroom"
# shellcheck disable=SC2086
roku_gate candidate-roku "$C" $PIDS || fail "candidate Roku gate"
phase1_checks "$C" "$STAGE/state/feed/recordings-journal.jsonl" 1 || fail "candidate Phase 1 checks"
split_checks "$C" "$CF" "$CC" || fail "candidate split checks"
config_checks "$C" || fail "candidate Config checks"
no_refusals "$CAND_FEED" "$CAND_CONTROL" || fail "the candidate's TasksMax refused"
drill candidate-A1 "$C" --vizelvision '' --seconds 60 --command "systemctl restart $CAND_CONTROL.service" \
    || fail "the candidate's drill A1: restarting control moved a stream"
wait_for 60 "The candidate's control answers again" lane_ready "$C" digital || fail "the candidate's control did not come back"
journalctl -u "$CAND_FEED" --no-pager > "$LOGDIR/candidate-feed.journal" 2>&1
journalctl -u "$CAND_CONTROL" --no-pager > "$LOGDIR/candidate-control.journal" 2>&1
cp -p "$STAGE/state/feed/recordings-journal.jsonl" "$LOGDIR/candidate-journal.jsonl" 2>/dev/null
stop_candidate
sleep 2
pgrep -u vivian -f "$STAGE/" >/dev/null && fail "the candidate left processes behind: $(pgrep -u vivian -af "$STAGE/")"
hdpvr_answers >/dev/null 2>&1 || fail "the HD PVR stopped answering after the candidate: reset it before deploying again: $(hdpvr_reset)"

# --- 5. promote, and gate production ----------------------------------------------------------
say "Promoting TV Thing"
TV_PROMOTED=1
as_vivian git -C "$TV" merge -q --ff-only "$TV_BRANCH" || fail "could not fast-forward $TV"
say "master is now $(git_v -C "$TV" rev-parse --short HEAD)"
start_production || fail "production did not start"
WEB_RESTARTED=1
systemctl restart tv-thing-web-basic.service || fail "tv-thing-web-basic did not restart"
wait_for 120 "Production's digital lane is ready" lane_ready "$P" digital || fail "production digital lane"
wait_for 60 "Production's Roku capture is ready" lane_ready "$P" roku || fail "production Roku capture"
wait_for 60 "The front door listens on every LAN address" lan_listening || fail "the front door is not listening on the LAN"
sleep 20
journalctl -u tv-thing-feed -u tv-thing -u tv-thing-web-basic --since "-3min" --no-pager | grep -E 'Reaped|Traceback|FeedConfigError|Roku lane off' \
    && say "(production's start logged the lines above)" || say "Production started clean"
PIDS=$(pids_of tv-thing-feed tv-thing)
# shellcheck disable=SC2086
gate production-digital "$P" $PIDS --retunes 3 || fail "production digital gate"
# shellcheck disable=SC2086
roku_gate production-roku "$P" $PIDS || fail "production Roku gate"
no_refusals tv-thing-feed tv-thing || fail "production's TasksMax refused"
phase1_checks "$P" "" 0 || fail "production Phase 1 checks"
split_checks "$P" 8086 8087 "$P_TV" || fail "production split checks"
config_checks "$P" || fail "production Config checks"
config_checks "$P_TV" || fail "the main site's /tv/ Config checks"

# --- 6. VizelVision back, and the proof -------------------------------------------------------
say "Starting VizelVision"
start_vizelvision
VV_ARGS=()
restore_air || { VV_ARGS=(--vizelvision ''); say "VizelVision did not settle: the drill watches TV Thing alone"; }
STOPPED=0          # past the gates: from here nothing rolls back by itself
PROOF=ok
drill production-A1 "$P" "${VV_ARGS[@]}" --seconds 90 --command "systemctl restart tv-thing.service" || PROOF=failed
wait_for 60 "Control answers again" lane_ready "$P" digital || PROOF=failed
web_stream_check production "$P" "http://127.0.0.1:8085/web/v1/status" "$(unit_pid tv-thing-web-basic)" || PROOF=failed

as_vivian rm -rf "$STAGE/streams" "$STAGE/recordings"
if [ "$PROOF" = ok ]; then
    say "DONE. Production runs $TV_BRANCH ($(git_v -C "$TV" rev-parse --short HEAD)); master is not pushed. Logs: $LOGDIR"
else
    say "DEPLOYED, BUT A LAST CHECK FAILED (the drill or Cairo Radio): $LOGDIR/drill-production-A1.log, $LOGDIR/web-stream-production.log"
    say "The build passed its gates and stays. To go back: stop VizelVision's master and web, then"
    say "  sudo systemctl stop tv-thing tv-thing-feed; git -C $TV reset --keep $TV_BASE; sudo systemctl start tv-thing-feed tv-thing; sudo systemctl restart tv-thing-web-basic"
    exit 1
fi
exit
}

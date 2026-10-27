#!/bin/bash
# The feed/control split, staged on scratch ports with no hardware (INVARIANTS.md rows A2, C2,
# C3 and 8, as far as they go without a tuner). Always-on, guide reads and the Roku lane are
# off, so neither process can touch the tuner, the HD PVR or the Roku; production is never
# asked anything. About a minute; it exits non-zero on any failure.
#
#   timeout 300 scripts/staged_split_check.sh            # STAGE=/some/dir to keep the logs there
set -u
CODE=$(cd "$(dirname "$0")/.." && pwd)
PY=${PY:-/home/vivian/miniconda3/bin/python3}
STAGE=${STAGE:-$(mktemp -d /tmp/tv-thing-split.XXXXXX)}
mkdir -p "$STAGE/streams" "$STAGE/state" "$STAGE/recordings"
echo "Staging in $STAGE"
F=http://127.0.0.1:18086 C=http://127.0.0.1:18087
COMMON=(TV_THING_STREAMS_DIR="$STAGE/streams" TV_THING_STATE_DIR="$STAGE/state"
        TV_THING_RECORDINGS_DIR="$STAGE/recordings" TV_THING_ROKU_CONFIG="$STAGE/no-roku.json"
        TV_THING_EPG=0 TV_THING_YTTV_GUIDE=0 TV_THING_ALWAYS_ON=0 TV_THING_RECORD_MIN_FREE_GB=1
        TV_THING_TV_UDP_PORT=18620 TV_THING_RECORD_TAP_PORT=18684 TV_THING_ROKU_TAP_PORT=18670
        PYTHONUNBUFFERED=1)
fails=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; fails=$((fails + 1)); }
j() { curl -s --max-time "${3:-4}" "$1" | "$PY" -c "import json,sys; d=json.load(sys.stdin); print($2)" 2>/dev/null; }
wait_url() { local deadline=$((SECONDS + $2)); until curl -s -o /dev/null --max-time 2 "$1"; do [ $SECONDS -ge $deadline ] && return 1; sleep 0.5; done; }
start_feed() { (cd "$CODE" && exec env "${COMMON[@]}" PORT=18086 TV_THING_CONTROL_URL=$C "$PY" feed_server.py >> "$STAGE/feed.log" 2>&1) & FEED=$!; }
start_control() { (cd "$CODE" && exec env "${COMMON[@]}" TV_THING_ROLE=control PORT=18087 TV_THING_FEED_URL=$F/ "$PY" server.py >> "$STAGE/control.log" 2>&1) & CONTROL=$!; }
cleanup() { kill -CONT "${FEED:-0}" 2>/dev/null; kill "${CONTROL:-0}" "${FEED:-0}" 2>/dev/null; sleep 1; kill -9 "${CONTROL:-0}" "${FEED:-0}" 2>/dev/null; }
trap cleanup EXIT

start_feed; wait_url "$F/feed/v1/lanes" 30 && pass "the feed is up" || { fail "the feed did not start"; tail -20 "$STAGE/feed.log"; exit 1; }
FPID=$FEED
start_control; wait_url "$C/api/v1/status" 30 && pass "control is up" || { fail "control did not start"; tail -20 "$STAGE/control.log"; exit 1; }

epoch=$(j "$F/feed/v1/lanes" "d['feed_epoch']")
[ "$(j "$C/api/v1/status" "d['feed_epoch']")" = "$epoch" ] && pass "control reports the feed's epoch $epoch" || fail "epoch mismatch"
[ "$(j "$C/api/v1/status" "sorted(l['lifecycle'] for l in d['lanes'].values())")" = "['idle', 'idle', 'idle']" ] \
    && pass "control sees three idle lanes through the feed" || fail "lanes: $(j "$C/api/v1/status" "{n: l['lifecycle'] for n, l in d['lanes'].items()}")"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$F/api/v1/status")" = 404 ] && pass "the feed refuses control's API" || fail "the feed served /api/"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$C/stream/digital.m3u8")" = 404 ] && pass "control leaves /stream/ to the feed" || fail "control served /stream/"
ss -ltnH | awk '{print $4}' | grep -E ':1808[67]$' | grep -vq '^127\.0\.0\.1:' && fail "a split process listens beyond loopback" || pass "both listen on 127.0.0.1 only"

relay() { curl -s --max-time 8 "$F/stream/internet/cabride/index.m3u8" | head -1; }
[ "$(relay)" = "#EXTM3U" ] && pass "the feed relays an internet stream" || fail "relay: $(relay)"

kill -9 "$CONTROL"; sleep 0.5
ok=0; for _ in 1 2 3; do [ "$(relay)" = "#EXTM3U" ] && ok=$((ok + 1)); sleep 1; done
[ $ok = 3 ] && pass "control killed: the relay keeps playing (3/3)" || fail "relay during control's death: $ok/3"
[ "$(j "$F/feed/v1/lanes" "d['feed_epoch']")" = "$epoch" ] && pass "control killed: the feed's epoch is unchanged" || fail "the feed restarted"
start_control; wait_url "$C/api/v1/status" 30
[ "$(j "$C/api/v1/status" "d['feed_epoch']")" = "$epoch" ] && pass "control back: same feed, same epoch" || fail "after control's restart the epoch moved"

kill "$FEED"; wait "$FEED" 2>/dev/null
sleep 1      # past RemoteFeed's half-second cache: the spec is unknown within 2 s (INVARIANTS.md C2)
t0=$SECONDS; lanes=$(j "$C/api/v1/status" "sorted(l['lifecycle'] for l in d['lanes'].values())" 6)
[ "$lanes" = "['unknown', 'unknown', 'unknown']" ] && pass "feed stopped: control reports unknown in $((SECONDS - t0)) s" || fail "feed stopped: lanes $lanes"
[ "$(j "$C/api/v1/status" "all(f['busy'] for f in d['frontends'])" 6)" = True ] && pass "feed stopped: no tuner looks free" || fail "a tuner looks free"
code=$(curl -s -o "$STAGE/tune.json" -w '%{http_code}' --max-time 10 -X POST -H 'Content-Type: application/json' \
    -d '{"source": "digital", "channel_id": "wrc_hd_4_1"}' "$C/api/v1/tune")
[ "$code" = 503 ] && pass "feed stopped: a tune is refused with 503" || fail "feed stopped: tune answered $code $(head -c 200 "$STAGE/tune.json")"

start_feed; wait_url "$F/feed/v1/lanes" 30
FPID=$FEED
sleep 1
new=$(j "$C/api/v1/status" "d['feed_epoch']" 6)
[ -n "$new" ] && [ "$new" != "$epoch" ] && pass "feed back: control reads its new epoch" || fail "feed back: epoch $new"

kill -STOP "$FPID"
sleep 1
t0=$SECONDS; lanes=$(j "$C/api/v1/status" "sorted(l['lifecycle'] for l in d['lanes'].values())" 8)
[ "$lanes" = "['unknown', 'unknown', 'unknown']" ] && [ $((SECONDS - t0)) -le 4 ] \
    && pass "feed frozen (SIGSTOP): control answers in $((SECONDS - t0)) s, lanes unknown" || fail "feed frozen: $lanes after $((SECONDS - t0)) s"
kill -CONT "$FPID"; sleep 1
[ "$(j "$C/api/v1/status" "d['lanes']['digital']['lifecycle']" 6)" = idle ] && pass "feed thawed: control reads it again" || fail "after SIGCONT"

devices=0; for fd in /proc/"$FPID"/fd/*; do case "$(readlink "$fd")" in /dev/dvb*|/dev/video*|/dev/v4l*) devices=$((devices + 1)) ;; esac; done
[ "$devices" = 0 ] && pass "the staged feed holds no tuner or capture device" || fail "the staged feed has $devices device(s) open"
echo "---"; echo "$fails failed"; grep -iE 'traceback|error' "$STAGE"/*.log | grep -v 'HTTP\]' | head -5
exit $fails

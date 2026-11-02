#!/bin/bash
# Two cities, staged on one host with no hardware (Phase B: markets, API.md). Two copies of
# this tree, each one process on a scratch port and each the other's peer:
#   * "dc", the primary, with the tracked plan;
#   * "lansing", a second node in another market, with its own plan (a fixture: WLNS 6.1 on
#     RF 36, the RF WTTG 5.1 has in DC, and WTTG itself, which must not become a second channel).
# Each has adapter 9 (none here), no host.json, no Roku, no guide reads and no always-on, so
# nothing opens a tuner or asks production anything. Checks: the plan each serves, the dial
# both show (the same, with Lansing's channel once, numbered by Lansing), and where the pool
# sends a tune (WLNS from dc goes to lansing; a DC channel never does). About 30 s.
#
#   timeout 300 scripts/staged_market_check.sh            # STAGE=/some/dir to keep the logs
set -u
CODE=$(cd "$(dirname "$0")/.." && pwd)
PY=${PY:-/home/vivian/miniconda3/bin/python3}
STAGE=${STAGE:-$(mktemp -d /tmp/tv-thing-markets.XXXXXX)}
echo "Staging in $STAGE"
A=http://127.0.0.1:18187 B=http://127.0.0.1:18287
fails=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; fails=$((fails + 1)); }
j() { curl -s --max-time "${3:-5}" "$1" | "$PY" -c "import json,sys; d=json.load(sys.stdin); print($2)" 2>/dev/null; }
wait_for() {  # seconds, python expression on the JSON of url
    local deadline=$((SECONDS + $1)) url=$2 expr=$3
    until [ "$(j "$url" "$expr")" = True ]; do [ $SECONDS -ge $deadline ] && return 1; sleep 1; done
}
for node in dc lansing; do
    mkdir -p "$STAGE/$node/state" "$STAGE/$node/streams" "$STAGE/$node/recordings"
    rsync -a --exclude /.git --exclude /streams --exclude /state --exclude __pycache__ \
        --exclude /lineup_state.json --exclude /roku_state "$CODE/" "$STAGE/$node/code/"
done
"$PY" - "$STAGE" <<'EOF'
import json, sys
stage = sys.argv[1]
plan = [{"id": "wlns_6_1", "number": "6.1", "name": "WLNS", "frequency": 605000000, "rf_channel": 36,
         "tsid": 1234, "service_id": 1, "video_pid": 49, "audio_pid": 52, "modulation": "VSB_8"},
        {"id": "wttg_5_1", "number": "5.1", "name": "WTTG", "frequency": 605000000, "rf_channel": 36,
         "tsid": 531, "service_id": 3, "video_pid": 49, "audio_pid": 52, "modulation": "VSB_8"}]
json.dump(plan, open(f"{stage}/lansing/state/channels.json", "w"), indent=2)
json.dump({"node": "lansing-sim", "rf_channels": [36], "tsids": {"36": 1234}},
          open(f"{stage}/lansing/state/reception.json", "w"))
EOF
start() {  # name, port, peer port, role, market, node name
    (cd "$STAGE/$1/code" && exec env TV_THING_STREAMS_DIR="$STAGE/$1/streams" TV_THING_STATE_DIR="$STAGE/$1/state" \
        TV_THING_RECORDINGS_DIR="$STAGE/$1/recordings" TV_THING_HOST_CONFIG="$STAGE/$1/no-host.json" \
        TV_THING_ROKU=off TV_THING_ROKU_CONFIG="$STAGE/no-roku.json" TV_THING_EPG=0 TV_THING_YTTV_GUIDE=0 \
        TV_THING_ALWAYS_ON=0 TV_THING_RECORD_MIN_FREE_GB=1 TV_THING_DIGITAL_ADAPTER=9 TV_THING_TUNER_B=off \
        TV_THING_TV_UDP_PORT=$((18000 + $2 % 1000)) TV_THING_RECORD_TAP_PORT=$((19000 + $2 % 1000)) \
        TV_THING_ROKU_TAP_PORT=$((17000 + $2 % 1000)) TV_THING_BIND=127.0.0.1 PORT=$2 \
        TV_THING_PEER_HOST=127.0.0.1:$3 TV_THING_NODE_ROLE=$4 TV_THING_MARKET=$5 TV_THING_NODE=$6 \
        PYTHONUNBUFFERED=1 timeout 280 "$PY" server.py >> "$STAGE/$1.log" 2>&1) &
    PIDS+=($!)
}
PIDS=()
# timeout passes the TERM on to its server.
cleanup() { kill "${PIDS[@]}" 2>/dev/null; sleep 2; kill -9 "${PIDS[@]}" 2>/dev/null; }
trap cleanup EXIT
start dc 18187 18287 primary dc viviserv
start lansing 18287 18187 second lansing lansing-sim
wait_for 40 "$A/api/v1/plan" "d['market'] == 'dc'" && wait_for 40 "$B/api/v1/plan" "d['market'] == 'lansing'" \
    && pass "both nodes are up" || { fail "a node did not start"; tail -20 "$STAGE/dc.log" "$STAGE/lansing.log"; exit 1; }

# -- the plans -------------------------------------------------------------------------------
[ "$(j "$B/api/v1/plan" "(d['primary'], [(r['key'], r['id'], r['tv_number']) for r in d['rows']])")" = \
  "(False, [('6.1', 'wlns_6_1', 400), ('5.1', 'wttg_5_1', 401)])" ] \
    && pass "lansing serves its own plan, numbered in the other cities' section" \
    || fail "lansing's plan: $(j "$B/api/v1/plan" "[(r['key'], r['id'], r.get('tv_number')) for r in d['rows']]")"
[ "$(j "$A/api/v1/plan" "(d['primary'], len(d['rows']) > 60)")" = "(True, True)" ] \
    && pass "dc serves the tracked plan" || fail "dc's plan: $(j "$A/api/v1/plan" "d['primary'], len(d['rows'])")"

# -- the dial --------------------------------------------------------------------------------
dial="sorted((c['id'], c['number'], c['channel_id'], c['market'] if c['id'].startswith('digital@') else None) for c in d['channels'] if c['source_id'] == 'digital')"
wait_for 20 "$A/api/v1/channels" "any(c['id'] == 'digital@lansing/6.1' for c in d['channels'])" \
    && pass "dc's dial has lansing's channel" || fail "dc's dial lacks digital@lansing/6.1"
wait_for 20 "$B/api/v1/channels" "any(c['id'] == 'digital/4.1' for c in d['channels'])" \
    && pass "lansing's dial has the home market's channels" || fail "lansing's dial lacks digital/4.1"
[ "$(j "$A/api/v1/channels" "[(c['channel_id'], c['number'], c['node']) for c in d['channels'] if c['id'] == 'digital@lansing/6.1']")" = \
  "[('lansing/wlns_6_1', 400, 'lansing-sim')]" ] && pass "on dc, WLNS is lansing/wlns_6_1 at 400, lansing's" \
    || fail "WLNS on dc: $(j "$A/api/v1/channels" "[c for c in d['channels'] if c['id'].startswith('digital@')]")"
[ "$(j "$A/api/v1/channels" "sum(1 for c in d['channels'] if c['source_id'] == 'digital' and c['detail'].get('rf_channel') == 36 and 'wttg' in (c['channel_id'] or ''))")" = 1 ] \
    && pass "WTTG, in both plans, is one channel" || fail "WTTG is on dc's dial more than once"
if [ "$(j "$A/api/v1/channels" "$dial")" = "$(j "$B/api/v1/channels" "$dial")" ]; then
    pass "both dials are the same ($(j "$A/api/v1/channels" "len($dial)") antenna channels)"
else
    fail "the dials differ"; diff <(j "$A/api/v1/channels" "'\n'.join(map(str, $dial))") \
                                 <(j "$B/api/v1/channels" "'\n'.join(map(str, $dial))") | head -10
fi
[ "$(j "$A/api/v1/tuners" "sorted((t['id'], t['market']) for t in d['tuners'])")" = \
  "[('lansing-sim:digital', 'lansing'), ('viviserv:digital', 'dc')]" ] \
    && pass "each tuner carries its market" || fail "tuners: $(j "$A/api/v1/tuners" "[(t['id'], t.get('market')) for t in d['tuners']]")"

# -- where a tune goes (adapter 9 is no tuner: each tune fails where it lands, which is the point)
tune() { curl -s --max-time 30 -X POST -H 'Content-Type: application/json' -d "{\"source\": \"digital\", \"channel_id\": \"$2\"}" "$1/api/v1/tune" >/dev/null; }
before=$(grep -c 'POST /api/v1/tune' "$STAGE/lansing.log")
tune "$A" lansing/wlns_6_1
[ "$(grep -c 'POST /api/v1/tune' "$STAGE/lansing.log")" -gt "$before" ] \
    && pass "a tune of WLNS on dc goes to lansing's tuner" || fail "dc did not send WLNS to lansing"
before=$(grep -c 'POST /api/v1/tune' "$STAGE/lansing.log")
tune "$A" wrc_hd_4_1
[ "$(grep -c 'POST /api/v1/tune' "$STAGE/lansing.log")" = "$before" ] \
    && pass "a tune of a DC channel on dc never goes to lansing" || fail "dc sent WRC to lansing"
before=$(grep -c 'POST /api/v1/tune' "$STAGE/dc.log")
tune "$B" wrc_hd_4_1
[ "$(grep -c 'POST /api/v1/tune' "$STAGE/dc.log")" -gt "$before" ] \
    && pass "a tune of a DC channel on lansing goes to dc's tuner" || fail "lansing did not send WRC to dc"
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 -X POST -H 'Content-Type: application/json' \
    -d '{"source": "digital", "channel_id": "wrc_hd_4_1", "tuner": "lansing-sim:digital"}' "$B/api/v1/tune")
[ "$code" = 404 ] && pass "lansing's own tuner refuses a DC channel (404)" || fail "lansing's tuner answered $code for WRC"
grep -q Traceback "$STAGE/dc.log" "$STAGE/lansing.log" && fail "a traceback: $(grep -h -A3 Traceback "$STAGE"/*.log | head -8)"

echo "---"
echo "$fails failed"
exit $((fails > 0))

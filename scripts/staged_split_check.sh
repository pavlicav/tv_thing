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
APACHE_DIR=$STAGE/apache D=http://127.0.0.1:18080 T=http://127.0.0.1:18081
apache() { /usr/sbin/apache2 -d "$APACHE_DIR" -f "$APACHE_DIR/httpd.conf" -k "$1"; }
cleanup() {
    [ -f "$APACHE_DIR/httpd.pid" ] && apache stop 2>/dev/null
    kill -CONT "${FEED:-0}" 2>/dev/null; kill "${CONTROL:-0}" "${FEED:-0}" 2>/dev/null; sleep 1
    kill -9 "${CONTROL:-0}" "${FEED:-0}" 2>/dev/null
}
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
# The VCR's recorder, started the way control starts one: through /feed/v1/, from the thread
# answering that request. It must outlive the request (its death signal once followed that
# thread). Its UDP tap gets no packets, so ffmpeg only waits; the feed ends it at `until`.
until=$(( $(date +%s) + 20 ))
rec=$(curl -s --max-time 8 -X POST -H 'Content-Type: application/json' \
    -d "{\"kind\": \"tap\", \"part\": \"$STAGE/recordings/staged.ts.part\", \"until\": $until}" \
    "$F/feed/v1/recorders" | "$PY" -c "import json,sys; print(json.load(sys.stdin)['id'])" 2>/dev/null)
sleep 3
[ -n "$rec" ] && [ "$(j "$F/feed/v1/recorders/$rec" "d['alive']")" = True ] \
    && pass "a recorder started through /feed/v1/ outlives the request that started it" \
    || fail "the recorder 3 s on: $(j "$F/feed/v1/recorders/${rec:-none}" "d.get('ended'), d.get('returncode')")"
[ -n "$rec" ] && curl -s -o /dev/null --max-time 8 -X POST -H 'Content-Type: application/json' \
    -d '{"timeout": 2}' "$F/feed/v1/recorders/$rec/stop"
# A stream whose URL is looked up (Yle Areena): the feed asks control's /internal/ for it.
# Skipped, not failed, when Yle has nothing live.
yle() { curl -s --max-time 20 "$F/stream/internet/pentulive/index.m3u8" | head -1; }
asked() { grep -c 'GET /internal/internet/pentulive/resolve' "$STAGE/control.log"; }
YLE=0
if [ "$(curl -s -o "$STAGE/resolve.json" -w '%{http_code}' --max-time 15 "$C/internal/internet/pentulive/resolve")" = 200 ]; then
    YLE=1; before=$(asked)
    [ "$(yle)" = "#EXTM3U" ] && [ "$(asked)" -gt "$before" ] \
        && pass "the feed relays a looked-up stream, asking control's /internal/ for its URL" \
        || fail "pentulive: $(yle), control asked $(asked) time(s)"
else
    echo "SKIP  pentulive: Yle Areena answered $(head -c 120 "$STAGE/resolve.json")"
fi

# --- the front door: a private Apache running the very routes production includes ----------
mkdir -p "$APACHE_DIR"; M=/usr/lib/apache2/modules
# /tv/ as the main site has it: viviserv-proxy.conf's own lines, aimed at this front door.
grep -E '^(RedirectMatch \^/tv\$|ProxyPass(Reverse)? +/tv/ )' "${VIVISERV_PROXY:-/etc/apache2/conf-available/viviserv-proxy.conf}" \
    > "$APACHE_DIR/tv-lines.conf"
[ "$(wc -l < "$APACHE_DIR/tv-lines.conf")" = 3 ] || { fail "viviserv-proxy.conf's three /tv lines not found"; exit 1; }
cat > "$APACHE_DIR/httpd.conf" <<CONF
ServerRoot "$APACHE_DIR"
DefaultRuntimeDir "$APACHE_DIR"
PidFile "$APACHE_DIR/httpd.pid"
ErrorLog "$APACHE_DIR/error.log"
LogLevel warn
LoadModule mpm_event_module $M/mod_mpm_event.so
LoadModule authz_core_module $M/mod_authz_core.so
LoadModule mime_module $M/mod_mime.so
LoadModule alias_module $M/mod_alias.so
LoadModule headers_module $M/mod_headers.so
LoadModule proxy_module $M/mod_proxy.so
LoadModule proxy_http_module $M/mod_proxy_http.so
TypesConfig /etc/mime.types
ServerName localhost
Listen 127.0.0.1:18080
Listen 127.0.0.1:18081
Define TV_THING_FEED_PORT 18086
Define TV_THING_CONTROL_PORT 18087
<VirtualHost 127.0.0.1:18080>
    Include "$CODE/deploy/apache/tv-thing-routes.conf"
</VirtualHost>
<VirtualHost 127.0.0.1:18081>
    ProxyPreserveHost On
$(sed 's/:8080/:18080/' "$APACHE_DIR/tv-lines.conf")
</VirtualHost>
CONF
apache start && wait_url "$D/api/v1/status" 15 && pass "the front door is up (the production routes, private Apache)" \
    || { fail "the front door did not start"; cat "$APACHE_DIR/error.log"; exit 1; }
[ "$(j "$D/api/v1/status" "d['feed_epoch']")" = "$epoch" ] && pass "front door: the API reaches control" || fail "front door: no API"
[ "$(curl -s --max-time 8 "$D/stream/internet/cabride/index.m3u8" | head -1)" = "#EXTM3U" ] \
    && pass "front door: /stream/ reaches the feed" || fail "front door: no stream"
master=$(curl -s --max-time 8 "$D/stream/internet/cabride/index.m3u8")
media=$master; base=$D/stream/internet/cabride
variant=$(echo "$master" | grep -v '^#' | head -1)
echo "$master" | grep -q 'EXT-X-STREAM-INF' && media=$(curl -s --max-time 8 "$base/$variant")
segment=$(echo "$media" | grep -v '^#' | grep . | tail -1)
[ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$base/$segment")" = 200 ] \
    && pass "front door: a relayed segment plays ($segment)" || fail "front door: segment $segment"
printf '%s\n%s\n' "$master" "$media" | grep -qE ':1808[67]|127\.0\.0\.1|localhost' \
    && fail "a playlist names a backend (D4)" || pass "no playlist names a backend (D4)"
codes=""
for url in "$D/internal/internet/pentulive/resolve" "$D/feed/v1/lanes" "$T/tv/internal/internet/pentulive/resolve" "$T/tv/feed/v1/lanes"; do
    codes="$codes $(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url")"
done
[ "$codes" = " 404 404 404 404" ] && pass "/internal/ and /feed/v1/ are never routed (D3)" || fail "D3:$codes"
[ "$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' "$T/tv")" = "302 $T/tv/" ] \
    && pass "/tv redirects to /tv/" || fail "/tv: $(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' "$T/tv")"
[ "$(j "$T/tv/api/v1/status" "d['feed_epoch']")" = "$epoch" ] && [ "$(curl -s --max-time 8 "$T/tv/stream/internet/cabride/index.m3u8" | head -1)" = "#EXTM3U" ] \
    && pass "/tv/ (the main site's own lines): the API reaches control and /tv/stream/ the feed" || fail "/tv/ routes"
( misses=0; for _ in $(seq 30); do [ "$(curl -s --max-time 5 "$D/stream/internet/cabride/index.m3u8" | head -1)" = "#EXTM3U" ] \
      || misses=$((misses + 1)); sleep 0.1; done; echo "$misses" > "$STAGE/graceful.misses" ) &
LOOP=$!
sleep 0.5; apache graceful; sleep 1; apache graceful; wait "$LOOP"
[ "$(cat "$STAGE/graceful.misses")" = 0 ] && pass "two graceful reloads mid-playback: no stream request failed (D1)" \
    || fail "D1: $(cat "$STAGE/graceful.misses")/30 stream requests failed across graceful reloads"

kill -9 "$CONTROL"; sleep 0.5
ok=0; for _ in 1 2 3; do [ "$(relay)" = "#EXTM3U" ] && ok=$((ok + 1)); sleep 1; done
[ $ok = 3 ] && pass "control killed: the relay keeps playing (3/3)" || fail "relay during control's death: $ok/3"
[ "$YLE" = 1 ] && { [ "$(yle)" = "#EXTM3U" ] && pass "control killed: the looked-up stream plays on the URL in hand" \
    || fail "control killed: pentulive $(yle)"; }
answer=$(curl -s -o "$STAGE/d2.json" -w '%{http_code} %{content_type}' --max-time 5 "$D/api/v1/status")
[ "$answer" = "503 application/json" ] && "$PY" -c "import json,sys; json.load(open(sys.argv[1]))" "$STAGE/d2.json" \
    && pass "control killed: /api/ through the front door is a JSON 503 (D2)" || fail "D2: $answer $(head -c 120 "$STAGE/d2.json")"
[ "$(curl -s --max-time 8 "$D/stream/internet/cabride/index.m3u8" | head -1)" = "#EXTM3U" ] \
    && pass "control killed: the relay plays through the front door" || fail "front-door relay with control down"
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

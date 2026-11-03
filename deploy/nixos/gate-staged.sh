#!/usr/bin/env bash
# The real-stream gate on viviserv2 (AGENTS.md): a staged candidate with both tuners on VA-API (the
# UHD 630), on its own port, streams and state, time-boxed; tuner A, tuner B, then tuner B beside
# tuner A. Never joins the pool. Production tv-thing and tv-thing-feed must be stopped. Stage the
# candidate first:
#   rsync -a --delete --exclude /.git --exclude /streams --exclude /state --exclude __pycache__ \
#       <candidate checkout>/ ~/tv_thing-gate-b/
# nvidia-smi reads nothing here, so the package temperature (the iGPU is on the CPU die) is
# sampled alongside.
set -u
C=$HOME/tv_thing-gate-b
S=$HOME/tv_thing-scratch/gate-igpu-$(date +%Y%m%d-%H%M%S)
URL=http://127.0.0.1:8099
# The system iHD driver: hardware.graphics in /etc/nixos/tv-thing.nix.
if systemctl is-active -q tv-thing-feed; then echo "Stop production first: sudo systemctl stop tv-thing tv-thing-feed"; exit 1; fi
mkdir -p "$S/streams" "$S/state" "$S/recordings"
cd "$C"
env TV_THING_ROKU=off TV_THING_HOST_CONFIG="$S/no-host.json" TV_THING_NODE_ROLE=second TV_THING_NODE=viviserv2 \
    TV_THING_PEER_HOST=off TV_THING_TUNER_B=1 TV_THING_EXTRA_TUNER_IDLE_SECONDS=60 \
    PORT=8099 TV_THING_BIND=127.0.0.1 TV_THING_TV_UDP_PORT=18420 TV_THING_STREAMS_DIR="$S/streams" \
    TV_THING_STATE_DIR="$S/state" TV_THING_RECORDINGS_DIR="$S/recordings" TV_THING_ALWAYS_ON=0 TV_THING_EPG=0 \
    TV_THING_YTTV_GUIDE=0 TV_THING_VIDEO_ENCODER=vaapi LIBVA_DRIVER_NAME=iHD \
    PYTHONUNBUFFERED=1 timeout 2400 python3 -u server.py > "$S/server.log" 2>&1 &
TPID=$!
( while sleep 1; do echo "$(date +%s) $(cat /sys/class/thermal/thermal_zone2/temp)"; done ) > "$S/pkg-temp.log" &
TEMP=$!
sleep 8
PID=$(pgrep -P "$TPID" | head -1)
[ -n "$PID" ] || { echo "the staged server did not start"; tail -20 "$S/server.log"; kill $TEMP; exit 1; }
trap "kill $PID $TPID $TEMP 2>/dev/null; sleep 2; pkill -f \"$C/dvb_source.py\" 2>/dev/null" EXIT
grep -E "running on|Extra tuners|channels|Video encoder" "$S/server.log"
gate() { echo "=== $1"; shift; timeout 900 python3 scripts/stream_sanity_check.py --url $URL --server-pid "$PID" --retunes 3 --require-encoder h264_vaapi "$@" 2>&1 | grep -vE "^PASS" | tail -30; echo "exit ${PIPESTATUS[0]}"; }
stop() { curl -s -X POST $URL/api/v1/stop -H "Content-Type: application/json" -d "{\"lane\":\"$1\"}" | head -c 80; echo; sleep 3; }
gate "tuner A alone, 3 retunes"
stop digital
gate "tuner B alone, 3 retunes" --lane digital-b
stop digital-b
echo "=== both at once: tuner A held on WETA 26.1 while tuner B is gated"
curl -s -X POST $URL/api/v1/tune -H "Content-Type: application/json" \
    -d "{\"source\":\"digital\",\"channel_id\":\"weta_hd_26_1\",\"quality\":\"medium\",\"tuner\":\"viviserv2:digital\"}" | head -c 120; echo
sleep 5
gate "tuner B with tuner A streaming" --lane digital-b --channel-id wjla_7_1 --channel-id wrc_hd_4_1 --max-dvb-sources 2
grep -E "Video encoder|unavailable|trip|Traceback" "$S/server.log" | sort | uniq -c | head
awk "{print \$2/1000}" "$S/pkg-temp.log" | sort -n | awk "{a[NR]=\$1} END {print \"package temp C: min\", a[1], \"max\", a[NR], \"samples\", NR}"
echo "logs: $S"

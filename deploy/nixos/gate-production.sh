#!/usr/bin/env bash
# The production gate on viviserv2 (AGENTS.md), through the front door: tuner A, then tuner B beside it.
cd ~/tv_thing || exit 1
pids() { echo --server-pid "$(systemctl show -p MainPID --value tv-thing-feed)" --server-pid "$(systemctl show -p MainPID --value tv-thing)"; }
# Production starts tuner A on its last channel, which may be the gate's first (32.1): a tune to
# what already plays reuses its encoder (watch-along), and the gate fails that round. Park it first.
echo "=== parking tuner A on 4.1 WRC, a channel the gate does not tune"
curl -s -X POST http://127.0.0.1:8080/api/v1/tune -H "Content-Type: application/json" \
    -d '{"source":"digital","channel_id":"wrc_hd_4_1","quality":"medium","tuner":"viviserv2:digital"}' | head -c 100; echo
sleep 8
echo "=== tuner A, 3 retunes (production, front door)"
timeout 900 python3 scripts/stream_sanity_check.py --url http://127.0.0.1:8080 --retunes 3 $(pids)
echo "exit $?"
echo "=== tuner B, 3 retunes, beside tuner A (production, front door)"
timeout 900 python3 scripts/stream_sanity_check.py --url http://127.0.0.1:8080 --retunes 3 --lane digital-b --max-dvb-sources 2 $(pids)
echo "exit $?"
echo "=== tuners after"
curl -s http://127.0.0.1:8080/api/v1/tuners | python3 -c 'import json,sys; [print(t["id"], t["state"], (t.get("selection") or {}).get("number"), t["available"]) for t in json.load(sys.stdin)["tuners"]]'
journalctl -u tv-thing-feed --since "-20min" --no-pager | grep -i -E "error|trip|reached|traceback" | tail -5

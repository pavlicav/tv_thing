#!/bin/bash
# Deploy the markets branch on viviserv: Phase B (markets, plan sharing, one dial across cities)
# and signal grades (a tune prefers a tuner that plays the station well). Only control runs any
# of it (AGENTS.md: a change only control runs is deployed by restarting control under the
# drill); the feed, web-basic, the units and the front door are not touched, and nothing goes
# off air.
#
#   bash /home/vivian/tv_thing-markets/scripts/deploy-markets.sh --check    # prechecks, no sudo
#   sudo bash /home/vivian/tv_thing-markets/scripts/deploy-markets.sh       # deploy
#
#   1. Prechecks: the branch clean and fast-forwarding production's master, production's
#      uncommitted work clear of the branch's files, nothing changed outside control's code;
#      the fast suite; the staged split check; the two-city check; the drill on today's build.
#   2. The dial now (every antenna channel's id and number), for after.
#   3. Promote: fast-forward master. Then the drill restarts control (tv-thing) and proves no
#      stream, recording, relay or VizelVision moved.
#   4. After: the same dial; /api/v1/plan; each tuner's market and signal grade; the market
#      on the Config API; config.html, index.html and inputs.html served; no traceback.
# A failure in 3 or 4 rolls back: `git reset --keep` (never --hard, so roku_app/'s uncommitted
# work survives) and control restarted on the old build.
set -uo pipefail
# Read whole before running: editing a running script made bash resume mid-command (2026-10-06).
{

TV=/home/vivian/tv_thing
TV_WORKTREE=/home/vivian/tv_thing-markets
TV_BRANCH=markets
PY=/home/vivian/miniconda3/bin/python3
P=http://127.0.0.1:8080
STAMP=$(date +%Y%m%d-%H%M%S)
LOGDIR=/home/vivian/markets-$STAMP
MODE=deploy
for arg in "$@"; do
    case "$arg" in
        --check) MODE=check ;;
        *) echo "usage: [sudo] bash $0 [--check]"; exit 2 ;;
    esac
done

as_vivian() {
    if [ "$(id -un)" = vivian ]; then env XDG_RUNTIME_DIR=/run/user/1000 "$@"
    else sudo -u vivian -H env XDG_RUNTIME_DIR=/run/user/1000 "$@"; fi
}
git_v() { as_vivian git "$@"; }
say() { echo "[$(date +%H:%M:%S)] $*"; }
json() { curl -s --max-time 10 "$1" | "$PY" -c "import json,sys; d=json.load(sys.stdin); print($2)"; }
PROMOTED=0
TV_BASE=$(git_v -C "$TV" rev-parse --short HEAD)
as_vivian mkdir -p "$LOGDIR"

rollback() {
    say "ROLLING BACK to $TV_BASE"
    if git_v -C "$TV" reset -q --keep "$TV_BASE"; then
        systemctl restart tv-thing.service && say "Control is back on $TV_BASE" \
            || say "CONTROL DID NOT RESTART: journalctl -u tv-thing"
    else
        say "git reset --keep $TV_BASE refused: check git -C $TV status, reset it, then sudo systemctl restart tv-thing"
    fi
}
fail() { say "FAILED: $*"; [ "$PROMOTED" = 1 ] && rollback; say "Logs: $LOGDIR"; exit 1; }

dial() {  # the antenna channels on the dial: id, number and the channel they tune
    json "$P/api/v1/channels" "'\n'.join(f\"{c['id']} {c['number']} {c['channel_id']}\" for c in d['channels'] if c['source_id'] == 'digital')"
}

# --- 1. prechecks -----------------------------------------------------------------------------
say "Prechecks"
[ "$(git_v -C "$TV_WORKTREE" rev-parse --abbrev-ref HEAD)" = "$TV_BRANCH" ] || fail "$TV_WORKTREE is not on $TV_BRANCH"
[ -z "$(git_v -C "$TV_WORKTREE" status --porcelain)" ] || fail "$TV_WORKTREE has uncommitted changes"
[ "$(git_v -C "$TV" rev-parse --abbrev-ref HEAD)" = master ] || fail "$TV is not on master"
git_v -C "$TV" merge-base --is-ancestor HEAD "$TV_BRANCH" || fail "$TV_BRANCH does not descend from master ($TV_BASE)"
dirty=$(git_v -C "$TV" status --porcelain | awk '{print $NF}' | sort)
overlap=$(comm -12 <(echo "$dirty") <(git_v -C "$TV" diff --name-only --no-renames HEAD "$TV_BRANCH" | sort))
[ -z "$overlap" ] || fail "$TV has uncommitted changes to files the branch changes: $overlap"
outside=$(git_v -C "$TV" diff --name-only HEAD "$TV_BRANCH" -- feed_node.py feed_server.py feed_api.py dvb_source.py \
    tuner.py analog_tuner.py recorder.py web_basic.py deploy/)
[ -z "$outside" ] || fail "the branch changes what this script does not restart: $outside"
lane=$(json "$P/api/v1/status" "d['lanes']['digital']['stream'].get('ready')") || fail "TV Thing is not answering"
say "The fast suite (about 2 minutes)"
as_vivian bash -c "cd '$TV_WORKTREE' && '$PY' -m pytest -q -p no:cacheprovider -m 'not slow and not api and not hardware and not resource' --ignore=tests/test_integration.py" \
    > "$LOGDIR/fast-suite.log" 2>&1 || fail "the fast suite failed: $LOGDIR/fast-suite.log"
tail -1 "$LOGDIR/fast-suite.log"
say "The staged split check (about a minute, no hardware)"
as_vivian env STAGE="$LOGDIR/staged-split" timeout 300 bash "$TV_WORKTREE/scripts/staged_split_check.sh" \
    > "$LOGDIR/staged-split.log" 2>&1 || fail "the staged split check failed: $LOGDIR/staged-split.log"
tail -1 "$LOGDIR/staged-split.log"
say "The two-city check (about 30 s, no hardware)"
as_vivian env STAGE="$LOGDIR/staged-markets" timeout 300 bash "$TV_WORKTREE/scripts/staged_market_check.sh" \
    > "$LOGDIR/staged-markets.log" 2>&1 || fail "the two-city check failed: $LOGDIR/staged-markets.log"
tail -1 "$LOGDIR/staged-markets.log"
say "The drill on today's build (30 s, read-only)"
as_vivian "$PY" "$TV_WORKTREE/scripts/restart_drill.py" --watch-only --seconds 30 > "$LOGDIR/drill-before.log" 2>&1 \
    || fail "the drill already fails on today's build: $LOGDIR/drill-before.log"
say "Prechecks passed (master $TV_BASE -> $TV_BRANCH $(git_v -C "$TV" rev-parse --short "$TV_BRANCH"))"
[ "$MODE" = check ] && { say "Prechecks only: nothing was changed. Logs: $LOGDIR"; exit 0; }
[ "$(id -u)" = 0 ] || { echo "Run with sudo (the drill restarts tv-thing)."; exit 1; }
trap '' HUP PIPE
exec > >(tee --output-error=warn -a "$LOGDIR/deploy.log") 2>&1
say "Logs in $LOGDIR"

# --- 2. the dial now --------------------------------------------------------------------------
dial > "$LOGDIR/dial-before.txt" || fail "could not read the dial"
say "The dial now: $(wc -l < "$LOGDIR/dial-before.txt") antenna channels"

# --- 3. promote, and the drill restarts control -------------------------------------------------
PROMOTED=1
git_v -C "$TV" merge -q --ff-only "$TV_BRANCH" || fail "could not fast-forward $TV"
say "master is now $(git_v -C "$TV" rev-parse --short HEAD)"
say "The drill: restarting control must move nothing"
"$PY" "$TV_WORKTREE/scripts/restart_drill.py" --tv "$P/" --seconds 90 --command "systemctl restart tv-thing.service" \
    2>&1 | tee "$LOGDIR/drill-restart.log"
[ "${PIPESTATUS[0]}" = 0 ] || fail "the drill: restarting control moved something ($LOGDIR/drill-restart.log)"

# --- 4. after ---------------------------------------------------------------------------------
for _ in $(seq 30); do curl -sf --max-time 2 -o /dev/null "$P/api/v1/plan" && break; sleep 2; done
dial > "$LOGDIR/dial-after.txt" || fail "could not read the dial after"
diff "$LOGDIR/dial-before.txt" "$LOGDIR/dial-after.txt" > "$LOGDIR/dial.diff" \
    && say "The dial is the same: $(wc -l < "$LOGDIR/dial-after.txt") antenna channels" \
    || fail "the dial changed: $(head -10 "$LOGDIR/dial.diff")"
got=$(json "$P/api/v1/plan" "(d['market'], d['primary'], len(d['rows']))") || fail "no /api/v1/plan"
[ "$(json "$P/api/v1/plan" "d['market'] == 'dc' and d['primary'] and len(d['rows']) > 60")" = True ] \
    || fail "the plan is $got"
say "Plan: $got"
got=$(json "$P/api/v1/tuners" "[(t['id'], t.get('market'), 'health' in t and 'signal' in t) for t in d['tuners'] if t['local']]")
[ "$got" = "[('viviserv:digital', 'dc', True)]" ] || fail "this node's tuners: $got"
say "Tuners: $got; the peer's: $(json "$P/api/v1/tuners" "[(t['id'], t.get('market')) for t in d['tuners'] if not t['local']]")"
got=$(json "$P/api/v1/config" "{i['id']: i['effective'] for i in d['settings']}.get('market')")
[ "$got" = dc ] || fail "the Config API's market is $got"
for page in index.html inputs.html config.html; do
    curl -sf --max-time 5 -o /dev/null "$P/$page" || fail "$page is not served"
done
say "Config: market dc; the Watch, Inputs and Config pages are served"
journalctl -u tv-thing --since "-3min" --no-pager | grep -E "Traceback" | head -5 \
    && fail "control logged the lines above" || say "Control started clean"
journalctl -u tv-thing --since "-3min" --no-pager | grep "Signal health: could not" | tail -2
chown -R vivian:vivian "$LOGDIR"
say "DONE. Production runs $TV_BRANCH ($(git_v -C "$TV" rev-parse --short HEAD)); master is not pushed. Logs: $LOGDIR"
exit
}

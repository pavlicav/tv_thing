#!/bin/bash
# Deploy a branch whose running change is web-basic's alone (AGENTS.md: a change to web-basic needs
# the fast suite and scripts/web_stream_check.py for each stream it touches, against a staged
# web-basic first, then against production after `sudo systemctl restart tv-thing-web-basic`).
# The feed and control are not restarted, and the branch may change nothing they run.
#
#   bash scripts/deploy-web-basic.sh BRANCH STREAM...          # prechecks, no sudo
#   sudo bash scripts/deploy-web-basic.sh --deploy BRANCH STREAM...
#
#   e.g.  sudo bash scripts/deploy-web-basic.sh --deploy unify sports-guide
#
#   1. Prechecks: the branch's worktree clean and fast-forwarding production's master, production's
#      uncommitted work clear of its files, nothing outside web-basic's code, tests, docs and
#      deploy/nixos changed; nothing of web-basic's on air; the fast suite; each STREAM ten
#      minutes from the branch's own web-basic on a scratch port and folder.
#   2. Promote: fast-forward master, restart tv-thing-web-basic (its web streams drop and restart
#      when next watched; the feed and control are untouched).
#   3. Each STREAM ten minutes from production's web-basic.
# A failure in 2 or 3 rolls back: `git reset --keep` (never --hard: roku_app/'s uncommitted work
# survives) and web-basic restarted on the old build. master is not pushed.
set -uo pipefail
# Read whole before running: editing a running script made bash resume mid-command (2026-10-06).
{

TV=/home/vivian/tv_thing
PY=/home/vivian/miniconda3/bin/python3
P=http://127.0.0.1:8080
WEB_STATUS=http://127.0.0.1:8085/web/v1/status
VV=http://127.0.0.1:6970
CW=18185                          # the staged web-basic's port
MODE=check
[ "${1:-}" = --deploy ] && { MODE=deploy; shift; }
BRANCH=${1:-}; shift || true
STREAMS=("$@")
[ -n "$BRANCH" ] && [ "${#STREAMS[@]}" -gt 0 ] || { echo "usage: [sudo] bash $0 [--deploy] BRANCH STREAM..."; exit 2; }
STAMP=$(date +%Y%m%d-%H%M%S)
LOGDIR=/home/vivian/web-basic-$BRANCH-$STAMP

as_vivian() {
    if [ "$(id -un)" = vivian ]; then "$@"
    else sudo -u vivian -H "$@"; fi
}
git_v() { as_vivian git "$@"; }
say() { echo "[$(date +%H:%M:%S)] $*"; }
PROMOTED=0
TV_BASE=$(git_v -C "$TV" rev-parse --short HEAD)
WORKTREE=$(git_v -C "$TV" worktree list --porcelain | awk -v b="refs/heads/$BRANCH" '$1 == "worktree" {w = $2} $1 == "branch" && $2 == b {print w}')
as_vivian mkdir -p "$LOGDIR"

rollback() {
    say "ROLLING BACK to $TV_BASE"
    if git_v -C "$TV" reset -q --keep "$TV_BASE"; then
        systemctl restart tv-thing-web-basic.service && say "web-basic is back on $TV_BASE" \
            || say "WEB-BASIC DID NOT RESTART: journalctl -u tv-thing-web-basic"
    else
        say "git reset --keep $TV_BASE refused: check git -C $TV status, reset it, then sudo systemctl restart tv-thing-web-basic"
    fi
}
fail() { say "FAILED: $*"; [ "$PROMOTED" = 1 ] && rollback; say "Logs: $LOGDIR"; exit 1; }

stream_check() {  # label, stream, base, status url, pid
    say "$2 for ten minutes from $1's web-basic"
    as_vivian timeout 1200 "$PY" "$TV/scripts/web_stream_check.py" "$2" --base "$3" --status "$4" --pid "$5" \
        > "$LOGDIR/web-stream-$1-$2.log" 2>&1
    local status=$?
    tail -3 "$LOGDIR/web-stream-$1-$2.log"
    return $status
}

staged_check() {  # stream: the branch's web-basic, on its own port, folder and pid
    local script=$WORKTREE/web_basic.py pid status
    as_vivian mkdir -p "$LOGDIR/web-basic-streams"
    (cd "$WORKTREE" && as_vivian env PORT=$CW TV_THING_WEB_STREAMS_DIR="$LOGDIR/web-basic-streams" \
        TV_THING_CONTROL_URL=http://127.0.0.1:8087 PYTHONUNBUFFERED=1 \
        timeout 1500 "$PY" "$script" >> "$LOGDIR/web-basic-staged.log" 2>&1) &
    for _ in $(seq 20); do curl -sf --max-time 2 -o /dev/null "http://127.0.0.1:$CW/web/v1/status" && break; sleep 1; done
    # Its own path picks it out: production's web-basic runs $TV/web_basic.py.
    pid=$(pgrep -u vivian -f "^$PY $script$" | head -1)
    [ -n "$pid" ] || { say "the staged web-basic did not start: $LOGDIR/web-basic-staged.log"; return 1; }
    stream_check staged "$1" "http://127.0.0.1:$CW" "http://127.0.0.1:$CW/web/v1/status" "$pid"
    status=$?
    pkill -u vivian -f "^$PY $script$"; sleep 1
    return $status
}

# --- 1. prechecks -----------------------------------------------------------------------------
say "Prechecks: $BRANCH (${STREAMS[*]})"
[ -n "$WORKTREE" ] || fail "no worktree has $BRANCH checked out"
[ -z "$(git_v -C "$WORKTREE" status --porcelain)" ] || fail "$WORKTREE has uncommitted changes"
[ "$(git_v -C "$TV" rev-parse --abbrev-ref HEAD)" = master ] || fail "$TV is not on master"
git_v -C "$TV" merge-base --is-ancestor HEAD "$BRANCH" || fail "$BRANCH does not descend from master ($TV_BASE)"
dirty=$(git_v -C "$TV" status --porcelain | awk '{print $NF}' | sort)
overlap=$(comm -12 <(echo "$dirty") <(git_v -C "$TV" diff --name-only --no-renames HEAD "$BRANCH" | sort))
[ -z "$overlap" ] || fail "$TV has uncommitted changes to files the branch changes: $overlap"
outside=$(git_v -C "$TV" diff --name-only HEAD "$BRANCH" | grep -v -E '^(web_basic\.py|tests/|scripts/|deploy/nixos/|[A-Za-z0-9_]+\.md$)')
[ -z "$outside" ] || fail "the branch changes what only a feed or control deploy may: $outside"
on_air=$(curl -s --max-time 5 "$VV/v1/master" | "$PY" -c "import json,sys; a=json.load(sys.stdin).get('on_air') or {}; print(a.get('source_id') or '')" 2>/dev/null)
case "$on_air" in
    vizelvision|internet|web*) fail "VizelVision has a web-basic stream on air ($on_air): run after it" ;;
esac
say "The fast suite (about 2 minutes)"
as_vivian bash -c "cd '$WORKTREE' && '$PY' -m pytest -q -p no:cacheprovider -m 'not slow and not api and not hardware and not resource' --ignore=tests/test_integration.py" \
    > "$LOGDIR/fast-suite.log" 2>&1 || fail "the fast suite failed: $LOGDIR/fast-suite.log"
tail -1 "$LOGDIR/fast-suite.log"
for stream in "${STREAMS[@]}"; do
    staged_check "$stream" || fail "$stream from the branch's web-basic: $LOGDIR/web-stream-staged-$stream.log"
done
say "Prechecks passed (master $TV_BASE -> $BRANCH $(git_v -C "$TV" rev-parse --short "$BRANCH"))"
[ "$MODE" = deploy ] || { say "Prechecks only: nothing was changed. Logs: $LOGDIR"; exit 0; }
[ "$(id -u)" = 0 ] || { echo "Run with sudo to deploy (it restarts tv-thing-web-basic)."; exit 1; }

# --- 2. promote -------------------------------------------------------------------------------
trap '' HUP PIPE
exec > >(tee --output-error=warn -a "$LOGDIR/deploy.log") 2>&1
PROMOTED=1
git_v -C "$TV" merge -q --ff-only "$BRANCH" || fail "could not fast-forward $TV"
say "master is now $(git_v -C "$TV" rev-parse --short HEAD)"
systemctl restart tv-thing-web-basic.service || fail "tv-thing-web-basic did not restart"
for _ in $(seq 20); do curl -sf --max-time 2 -o /dev/null "$WEB_STATUS" && break; sleep 1; done

# --- 3. production ----------------------------------------------------------------------------
for stream in "${STREAMS[@]}"; do
    stream_check production "$stream" "$P" "$WEB_STATUS" "$(systemctl show -p MainPID --value tv-thing-web-basic)" \
        || fail "$stream from production's web-basic: $LOGDIR/web-stream-production-$stream.log"
done
chown -R vivian:vivian "$LOGDIR"
say "DONE. Production runs $BRANCH ($(git_v -C "$TV" rev-parse --short HEAD)); master is not pushed. Logs: $LOGDIR"
exit
}

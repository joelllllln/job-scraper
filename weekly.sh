#!/usr/bin/env bash
# weekly.sh — the whole pipeline, once a week, unattended.
#
#   ./weekly.sh                normal weekly run
#   ./weekly.sh --full         also re-sniff every firm's ATS (monthly is enough)
#   ./weekly.sh --resend-all   re-send the digest covering every open role, from
#                              the database as it already stands. Collects
#                              nothing, verifies nothing, records nothing —
#                              seconds rather than half an hour. For when a
#                              digest went missing or you want the whole list.
#
# Design rules for an unattended job:
#   - never run twice at once (flock)
#   - never start with a broken config (validate.py gate)
#   - never lose the database (backup first, WAL, checkpointed writes)
#   - never let one dead source cost the whole run (per-stage isolation)
#   - never hang forever (per-stage timeout)
#   - never claim success when stages failed (exit summary + non-zero exit)

set -uo pipefail
cd "$(dirname "$0")" || exit 1

PYTHON=${PYTHON:-python3}
STAGE_TIMEOUT=${STAGE_TIMEOUT:-2400}      # 40 min ceiling per stage

FULL=""
RESEND=""
EXPAND=""
for arg in "$@"; do
  case "$arg" in
    --full)         FULL=1 ;;
    --resend-all)   RESEND=1 ;;
    --expand-firms) EXPAND=1 ;;
    *) echo "unknown option: $arg" >&2
       echo "usage: weekly.sh [--full] [--resend-all] [--expand-firms]" >&2
       exit 2 ;;
  esac
done

mkdir -p logs backups
LOG="logs/$(date +%F).log"
LOCK="/tmp/jobscraper.lock"

# --- one run at a time -------------------------------------------------------
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "another run is in progress (lock: $LOCK) — exiting" | tee -a "$LOG"
  exit 0
fi

FAILED=()
STARTED=$(date +%s)

log() { echo "$*" | tee -a "$LOG"; }

run() {
  local name="$1"; shift
  log ""
  log "=== $name · $(date +%H:%M:%S) ==="
  local start; start=$(date +%s)
  if timeout --signal=INT --kill-after=60 "$STAGE_TIMEOUT" "$@" 2>&1 | tee -a "$LOG"; then
    log "--- $name ok ($(( $(date +%s) - start ))s)"
  else
    local rc=${PIPESTATUS[0]}
    if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
      log "!! $name TIMED OUT after ${STAGE_TIMEOUT}s — continuing"
    else
      log "!! $name failed (exit $rc) — continuing"
    fi
    FAILED+=("$name")
  fi
}

trap 'log ""; log "!! interrupted at $(date +%H:%M:%S) — partial results are saved"; exit 130' INT TERM

log "### weekly run $(date -u +%FT%TZ)"
log "### python: $($PYTHON --version 2>&1), host: $(hostname)"

# --- gate: config must be valid before anything expensive --------------------
if ! $PYTHON validate.py 2>&1 | tee -a "$LOG"; then
  log ""
  log "!! config invalid — aborting before any scraping. Nothing was changed."
  exit 1
fi

# --- gate: the code itself must still behave ---------------------------------
if ! $PYTHON selftest.py >>"$LOG" 2>&1; then
  log "!! selftest failed — aborting. Run 'python3 selftest.py' to see what broke."
  exit 1
fi
log "selftest: pass"

# --- backup before we write anything -----------------------------------------
$PYTHON -c "import store; p = store.backup(); print(f'backup: {p}' if p else 'backup: no db yet')" \
  2>&1 | tee -a "$LOG"

if [ -z "$RESEND" ]; then

  # --- widen the registry from the official register --------------------------
  # Opt-in and one-off-ish: pulls every active London finance and energy company
  # from Companies House and appends the new ones to firms.csv with a BLANK
  # domain. discover.py picks them up on the same run.
  if [ -n "$EXPAND" ]; then
    run "expand firms" $PYTHON companies_house.py --append
  fi

  # --- discovery (slow, slow-changing) ---------------------------------------
  # sniff reads every firm's own careers page. Slow, and which ATS a firm uses
  # changes about never, so only on --full or the very first run.
  if [ -n "$FULL" ] || [ ! -f sniffed.csv ]; then
    run "sniff" $PYTHON sniff.py
  fi
  # discover is incremental — it probes only firms with no answer recorded yet,
  # so it costs nothing on a steady week and picks up newly added firms by
  # itself. That is what makes adding thousands of firms a one-off.
  if [ -n "$FULL" ]; then
    run "discover" $PYTHON discover.py --recheck
  else
    run "discover" $PYTHON discover.py
  fi

  # --- collection --------------------------------------------------------------
  # Order matters: direct ATS first, so when the same job also turns up on an
  # aggregator the stored link is already the direct one.
  run "ats endpoints" $PYTHON scrape.py
  run "workday"       $PYTHON workday.py
  run "reed+bullhorn" $PYTHON feeds.py --all
  run "job boards"    $PYTHON boards.py --hours 192
  run "efinancial"    $PYTHON efc.py --limit 200

  # --- verification -------------------------------------------------------------
  run "verify" $PYTHON verify.py

else
  log ""
  log "### resend: reporting the database as it stands — no collection, no verification"
fi

# --- keep derived fields in step with the current parsers ------------------------
# Offline and quick. Rows verified under an older parser keep its answers
# otherwise, and years_required now decides whether a role is shown at all.
run "reparse" $PYTHON verify.py --reparse

# --- ranking --------------------------------------------------------------------
# Only stamp the run if most stages worked. Recording a mostly-failed run would
# silently swallow a week of new jobs from the next digest.
if [ -n "$RESEND" ]; then
  # every open role, not just the new ones — and deliberately not recorded, so a
  # resend can't move the clock and hide next week's genuinely new jobs
  run "score" $PYTHON score.py
elif [ ${#FAILED[@]} -lt 5 ]; then
  run "score" $PYTHON score.py --new-only --record
else
  log "!! ${#FAILED[@]} stages failed — scoring without --record so next week still sees these"
  run "score" $PYTHON score.py --new-only
fi

run "notify" $PYTHON notify.py

# --- summary ---------------------------------------------------------------------
ELAPSED=$(( $(date +%s) - STARTED ))
log ""
log "### done in $((ELAPSED / 60))m$((ELAPSED % 60))s"
if [ ${#FAILED[@]} -gt 0 ]; then
  log "### ${#FAILED[@]} stage(s) failed: ${FAILED[*]}"
  log "### full log: $LOG"
fi
$PYTHON -c "
import http_client
b = http_client.breaker_report()
print('### hosts circuit-broken this run: ' + (', '.join(b) if b else 'none'))" 2>>"$LOG" | tee -a "$LOG"

find logs -name '*.log' -mtime +90 -delete 2>/dev/null || true
find backups -name 'jobs-*.db' -mtime +60 -delete 2>/dev/null || true

[ ${#FAILED[@]} -gt 0 ] && exit 1
exit 0

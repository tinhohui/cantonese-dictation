#!/usr/bin/env bash
# One-button "fix dictation" action, triggered from the Hammerspoon menu
# (Tinho, 2026-08-03: pressing this IS the explicit, in-person joint-test
# go-ahead -- a deliberate button click, at a moment of his choosing).
#
# ORDER (Tinho, 2026-08-03, corrected -- restart comes first, not last):
#   1. Grab just the pid + thread count (near-instant, <0.1s) -- the only
#      two facts that are actually destroyed by a restart.
#   2. Restart the live service IMMEDIATELY -- the thing that has given
#      real relief every single time tonight, regardless of root cause.
#      This is the priority; nothing should delay it.
#   3. Only THEN write the full diagnostic bundle (recent logs, deployed
#      commit, incident trend, watchdog log) -- log FILES are unaffected
#      by the restart (append-only, on disk already), so capturing them
#      after costs nothing diagnostically and no longer delays the fix.
#
# What this does NOT do: it does not diagnose the root cause or write a
# code fix. That step needs real investigation each time -- today's
# incidents (a thread leak in two different places) each took real
# reading of code and log history to find and fix correctly; a script
# that guessed and auto-patched live server.py would be far more
# dangerous than the failure it was trying to cure. The bundle this
# writes is what makes that next investigation fast instead of starting
# from zero -- point Claude at $BUNDLE next time and say "fix dictation".
set -uo pipefail

LIVE="$HOME/dictation"
REPO="$HOME/tinho-dictation"
BUNDLE="$LIVE/last-fix-bundle.txt"
LABEL="com.huitinho.dictation"

OLD_PID=$(launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | awk '/pid =/{print $3}')
OLD_THREAD_COUNT="n/a"
if [ -n "${OLD_PID:-}" ]; then
  OLD_THREAD_COUNT=$(ps -M -p "$OLD_PID" 2>/dev/null | wc -l | tr -d ' ')
fi

launchctl kickstart -k "gui/$(id -u)/$LABEL"
sleep 3

NEW_STATE=$(launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | awk '/state =/{print $3; exit}')

{
  echo "=== fix_dictation_now.sh run: $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
  echo
  echo "--- pre-restart snapshot ---"
  echo "old pid: ${OLD_PID:-n/a}, thread count: $OLD_THREAD_COUNT"
  echo
  echo "--- launchd state (after restart) ---"
  launchctl print "gui/$(id -u)/$LABEL" 2>&1 | grep -E "state|pid"
  echo
  echo "--- deployed commit ---"
  cat "$LIVE/.deployed_commit" 2>/dev/null || echo "(no marker)"
  echo
  echo "--- repo HEAD ---"
  git -C "$REPO" rev-parse HEAD 2>/dev/null
  echo
  echo "--- incident trend (7d/30d/24h) ---"
  ( cd "$LIVE" && ./venv/bin/python3 dictation_incident_report.py 2>&1 )
  echo
  echo "--- last 60 lines: /tmp/dictation.log ---"
  tail -60 /tmp/dictation.log 2>&1
  echo
  echo "--- last 40 lines: server.log ---"
  tail -40 "$LIVE/server.log" 2>&1
  echo
  echo "--- last 20 lines: watchdog-external.log ---"
  tail -20 "$LIVE/watchdog-external.log" 2>&1
} > "$BUNDLE" 2>&1

if [ "$NEW_STATE" = "running" ]; then
  echo "OK: restarted, bundle at $BUNDLE"
  exit 0
else
  echo "WARNING: state after restart is '$NEW_STATE', not 'running' -- check $BUNDLE"
  exit 1
fi

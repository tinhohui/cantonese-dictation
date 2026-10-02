#!/usr/bin/env python3
"""STOP-returns-zero-characters guard (2026-07-26, incident response — item 3
of the same brief as content_guard.py).

Tonight's live server.log shows this, for a real 2-second take:

    2026-07-25T22:48:22 transcribed 2s tail + 0 segments: asr=0.370s
    settle=0.083s polish=0.001s total=0.453s budget_ms=500 budget=PASS
    llm=False provisional_segments=0 raw=0 out=0 chars dropouts=0
    audio=20260725-224822.wav

Nobody was told. Two things make this invisible to everything else that
already exists:

  1. server.py's log_history() SKIPS the entry outright when both `text`
     and `raw` are empty (`if not text and not raw: return None`) — so a
     genuine zero-output STOP never reaches history.jsonl at all. It is
     invisible even to content_guard.py, which can only ever look at takes
     that DID make it into history.jsonl.
  2. watchdog.py's latency selftest only ever proves the model is FAST on
     canned audio — a STOP that returns instantly with nothing (raw=0
     out=0, total=0.453s here, comfortably inside budget) looks like a
     SUCCESS to every latency-shaped check that exists.

The only place this incident is visible at all is server.log's own
per-STOP diagnostic line (server.py's `log(f"transcribed {tail_seconds}s
tail + {n_segments} segments: ... raw={len(raw)} out={len(text)} chars ...
audio={name}")`), written unconditionally on every live STOP. This module
tails that line (read-only — no import of server.py, no mic/ollama/GPU,
`DICTATION_LIVE_GUARD=1` safe) and trips on it.

DESIGN NOTE — why NOT "two consecutive out==0 STOPs" as literally briefed:
MEASURED against the live ~/dictation/server.log (2026-07-22 through
2026-07-26, 1413 parsed STOP lines; 1107 in-scope per the SCOPE note
below): out==0 happens 24 times in-window, but 22 of those 24 are
raw=1-4/out=0 — a brief accidental hotkey tap where ASR heard a faint
fragment (1-4 chars) that polish/dedup correctly reduced to nothing. These
cluster in short back-to-back runs — a literal "2 consecutive out==0"
rule, applied to the same in-scope records this module actually evaluates,
would have STARTED a trip 5 separate times in four days on pure noise
(e.g. 2026-07-24T20:24:01-20:24:03), exactly the cries-wolf pattern this
repo has already had to retune away from once (watchdog.py's
SELFTEST_SAMPLES history) and D63 (a guard must match the ACTION that
causes the harm, not a correlated-but-ambiguous signal).

The two REAL incidents (2026-07-25 20:45:21 and 22:48:22) are both raw=0,
not just out=0 — ASR itself decoded LITERALLY NOTHING from 2-3 seconds of
real audio, categorically different from a benign 1-4-char fragment. That
signal (raw==0 for a tail long enough that silence is implausible) has a
measured base rate of 2/1107 = 0.18% in-scope — both true incidents, ZERO
false positives — and needs no "two in a row" to be meaningful: a single
raw==0 take for >=1s of tail is already exactly what happened tonight.
So SILENT_STOP below trips on ONE such take, immediately — "instantly", as
briefed — while the literal "two consecutive out==0" wording is
reinterpreted as IMPLAUSIBLE_RUN (below): >=2 consecutive takes with
output implausibly low FOR THEIR DURATION (a chars-per-second floor
measured from the same log: p1=1.33, p5=2.19 chars/s across 1368 healthy
non-zero-duration takes; 1.0 chosen with headroom below p1). MEASURED: 0
IMPLAUSIBLE_RUN trips across the whole log (6 isolated single takes dip
below the 1.0 floor — a slow/quiet moment, not a bug — but never twice in
a row), so this condition has a 0% measured false-positive rate too; it is
a dormant safety net for a degradation pattern that has not yet occurred,
not a duplicate of SILENT_STOP. See PR body for the full measured table.

Scope match with content_guard.py's v1 SCOPE decision: only single-segment
(segments==0) takes with a real audio file (audio != None) are evaluated —
a multi-segment take's `out` covers segments this line's own tail duration
does not, so a length/duration ratio computed against it would be
meaningless (and, since it can only ever look artificially HIGH, never
LOW, it can never cause a false trip either way — excluded for clarity,
not safety).

Wiring: called from watchdog.py's selftest_worker loop next to
content_guard.maybe_run(), gated by the SAME busy_fn (never contends with
a real recording), using the SAME notify_fyi() local macOS banner (never a
URL/dashboard action — tinho-os D10a). Cheaper
than content_guard.py by design: no offline decode, no model load — just
regex over whatever bytes were appended to server.log since the last
check (byte-offset tailing, not a full re-read), so it can run every poll
(30s) rather than on a 30-minute cadence and still catch the catastrophic
case within one poll interval.
"""
import datetime
import json
import os
import re
import time

HERE = os.path.dirname(os.path.abspath(__file__))

LOG_PATH = os.environ.get(
    "DICTATION_STOP_INTEGRITY_LOG_PATH", os.path.join(HERE, "server.log"))
STATE_PATH = os.path.join(HERE, ".stop_integrity_state")




RECORDINGS_DIR = os.environ.get(
    "DICTATION_STOP_INTEGRITY_RECORDINGS_DIR",
    os.environ.get("DICTATION_CONTENT_GUARD_RECORDINGS_DIR",
                   os.path.join(HERE, "recordings")))










STOP_LINE_RE = re.compile(
    r"^(?P<ts>\S+) transcribed (?P<dur>\d+)s tail \+ (?P<segs>\d+) segments: "
    r".*?raw=(?P<raw>\d+) out=(?P<out>\d+) chars .*?audio=(?P<audio>\S+)$")




MIN_SILENT_TAIL_SECONDS = float(
    os.environ.get("DICTATION_STOP_INTEGRITY_MIN_SILENT_TAIL_SECONDS", "1"))
CHARS_PER_SEC_FLOOR = float(
    os.environ.get("DICTATION_STOP_INTEGRITY_CHARS_PER_SEC_FLOOR", "1.0"))
MIN_TAIL_SECONDS_FOR_RATIO = float(
    os.environ.get("DICTATION_STOP_INTEGRITY_MIN_TAIL_SECONDS_FOR_RATIO", "1"))
IMPLAUSIBLE_RUN_LENGTH = int(
    os.environ.get("DICTATION_STOP_INTEGRITY_RUN_LENGTH", "2"))
RECENT_WINDOW = 10
COOLDOWN_SECONDS = int(
    os.environ.get("DICTATION_STOP_INTEGRITY_COOLDOWN_MINUTES", "15")) * 60


def _state():
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(state):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
    except OSError:
        pass


def _in_scope(rec):
    """Single-segment takes with a real saved wav only — see module
    docstring's "Scope match with content_guard.py" note."""
    return rec["segs"] == 0 and rec["audio"] not in (None, "None")


def parse_new_lines(log_path=LOG_PATH, state=None):
    """Read only the bytes appended to `log_path` since the stored byte
    offset, parse STOP_LINE_RE matches. Returns (records, new_offset),
    oldest first. Never raises — an unreadable log is treated as "nothing
    new" (offset unchanged), matching content_guard.run_once()'s fail-open
    posture: a canary that fails on its OWN I/O errors is more dangerous
    than one that just skips a cycle. Detects truncation/rotation (stored
    offset past current end-of-file) and resets to 0 rather than seeking
    past EOF."""
    state = state if state is not None else _state()
    offset = state.get("offset", 0)
    try:
        size = os.path.getsize(log_path)
    except OSError:
        return [], offset
    if size < offset:
        offset = 0
    records = []
    try:
        with open(log_path, encoding="utf-8", errors="ignore") as fh:
            fh.seek(offset)
            for line in fh:
                line = line.rstrip("\n")
                m = STOP_LINE_RE.match(line)
                if not m:
                    continue
                d = m.groupdict()
                records.append({
                    "ts": d["ts"],
                    "dur": int(d["dur"]),
                    "segs": int(d["segs"]),
                    "raw": int(d["raw"]),
                    "out": int(d["out"]),
                    "audio": d["audio"],
                })
            new_offset = fh.tell()
    except OSError:
        return [], offset
    return records, new_offset


def evaluate_records(records, recent_before=None,
                     min_silent_tail=MIN_SILENT_TAIL_SECONDS,
                     chars_per_sec_floor=CHARS_PER_SEC_FLOOR,
                     min_tail_for_ratio=MIN_TAIL_SECONDS_FOR_RATIO,
                     run_length=IMPLAUSIBLE_RUN_LENGTH):
    """Pure decision function — no I/O, fully unit-testable.

    `records`: newly-parsed STOP records, oldest first.
    `recent_before`: the tail of previously-seen in-scope records (so a
    run that spans a poll boundary — e.g. 1 low-ratio take last cycle, 1
    more this cycle — is still detected).

    Returns (tripped, reason_or_None, updated_recent_window,
    tripped_record_or_None) — the window is always returned (capped to
    RECENT_WINDOW) so callers persist it regardless of whether anything
    tripped; `tripped_record` is the specific record responsible (its
    `audio` field is what a caller needs to quarantine the right wav) —
    the record that trips SILENT_STOP, or the latest record completing an
    IMPLAUSIBLE_RUN.
    """
    window = list(recent_before or [])
    reason = None
    tripped_record = None
    for rec in records:
        if not _in_scope(rec):
            window = []
            continue
        window.append(rec)
        if reason is not None:
            continue



        if rec["raw"] == 0 and rec["dur"] >= min_silent_tail:
            reason = (f"stop-integrity: SILENT STOP — {rec['dur']}s of audio "
                      f"({rec['audio']}) produced raw=0 out=0 chars")
            tripped_record = rec
            continue



        if rec["out"] > 0 and rec["dur"] >= min_tail_for_ratio:
            ratio = rec["out"] / rec["dur"]
            if ratio < chars_per_sec_floor:
                run = 0
                for r2 in reversed(window):
                    if r2["dur"] < min_tail_for_ratio:
                        break
                    if r2["raw"] == 0:
                        break
                    r2_ratio = r2["out"] / r2["dur"] if r2["dur"] else float("inf")
                    if r2_ratio >= chars_per_sec_floor:
                        break
                    run += 1
                if run >= run_length:
                    reason = (f"stop-integrity: {run} consecutive takes under "
                              f"{chars_per_sec_floor:.1f} chars/s (latest "
                              f"{ratio:.2f} chars/s over {rec['dur']}s, "
                              f"{rec['audio']})")
                    tripped_record = rec
    window = window[-RECENT_WINDOW:]
    return reason is not None, reason, window, tripped_record


def maybe_run(log=print, notify_fn=None, busy_fn=None, log_path=LOG_PATH,
             recordings_dir=RECORDINGS_DIR):
    """Entry point for watchdog.py's cadence loop. No `due()` gate — this
    is cheap (byte-offset tail read + regex over a handful of new lines),
    so it runs every poll rather than on content_guard.py's 30-minute
    cadence, catching a genuine STOP failure within one poll interval
    (SELFTEST_POLL_SECONDS, 30s) instead of waiting for the next scheduled
    check. Rate-limited notification only (COOLDOWN_SECONDS) so an ongoing
    episode doesn't spam — the state itself (offset, recent window) always
    advances regardless of the notification cooldown, so no STOP is ever
    re-evaluated twice."""
    if busy_fn is None:
        import recording_gate
        busy_fn = recording_gate.busy
    try:
        if busy_fn():
            return None
    except Exception:
        return None
    state = _state()
    try:
        records, new_offset = parse_new_lines(log_path, state)
    except Exception as exc:
        log(f"stop-integrity: scan errored (ignored, no trip): {exc}")
        return None
    if not records:
        return None
    tripped, reason, updated_recent, tripped_record = evaluate_records(
        records, state.get("recent", []))
    now = time.time()
    last_trip = state.get("last_trip", 0.0)
    renotify = tripped and (now - last_trip > COOLDOWN_SECONDS)
    _write_state({
        "offset": new_offset,
        "recent": updated_recent,
        "last_trip": now if renotify else last_trip,
    })
    if tripped:






        if tripped_record is not None and tripped_record.get("audio"):
            try:
                import incident_quarantine
                wav_path = os.path.join(
                    recordings_dir, tripped_record["audio"])
                incident_quarantine.quarantine(
                    wav_path, "stop_integrity", tripped_record,
                    recordings_dir=recordings_dir, log=log)
            except Exception as exc:
                log(f"stop-integrity: quarantine failed (ignored, trip "
                    f"already logged): {exc}")
        if not renotify:
            log(f"stop-integrity: TRIPPED (within cooldown, not re-notifying) "
                f"— {reason}")
            return True, reason
        log(f"stop-integrity: TRIPPED — {reason}")
        if notify_fn is not None:
            try:
                notify_fn(f"⚠️ dictation stop-integrity: {reason}", log)
            except Exception as exc:
                log(f"stop-integrity: notify failed: {exc}")
        return True, reason
    return False, None

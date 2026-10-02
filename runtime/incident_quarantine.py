#!/usr/bin/env python3
"""Incident quarantine (2026-07-26) — preserve canary evidence past
server.py's KEEP_RECORDINGS prune.

server.py's save_recording() keeps only the newest KEEP_RECORDINGS=10 wavs
in recordings/, unconditionally — it has no idea a canary ever looked at
any of them. Both real 2026-07-25 incidents' own wavs had already rotated
out by the time this repo could re-examine them (see tinho-dictation PR
#24's DEVIATIONS #2/#3): the evidence that PROVED the bug was gone within
minutes, before it even finished being diagnosed. With KEEP_RECORDINGS=10
and Tinho dictating actively, that window can be under two minutes (10
files at multiple takes/minute). Every future trip would lose its own
evidence the same way unless something copies it out of the prune's reach
BEFORE the next STOP rotates it away.

Fix: server.py's prune only calls `os.listdir(RECORDINGS_DIR)` (never
recursive) and only unlinks entries whose name sorts before the newest 10
— a subdirectory named "incidents" sorts AFTER every "YYYYMMDD-HHMMSS.wav"
filename (ASCII: any digit < 'i'), so it always lands in the "keep newest"
tail of `sorted(os.listdir(...))` and is never enumerated for deletion,
regardless of how many wavs accumulate. Confirmed by reading server.py's
save_recording() (never modified — this needs no server.py change, per
the brief). recordings/incidents/ therefore survives the prune by
construction, not by racing it.

Called by content_guard.py / stop_integrity_guard.py ONLY on an actual
TRIP (not on every check — both guards measure a 0% false-positive rate,
so quarantine is rare by construction, not a routine cost). Copies
(never moves — Tinho's original file and the live prune's own bookkeeping
are both left completely alone) the tripped take's wav plus a small JSON
sidecar (guard name, its own metrics, and the history.jsonl row if one
exists — log_history() skips zero-output entries, so a SILENT_STOP
incident has no history row and the sidecar simply omits it) into
RECORDINGS_DIR/incidents/.

PRIVACY (Tinho's audio is his own private speech): this never uploads
anything, never sends audio or transcribed text over the network, and
never writes transcribed content into server.log or an ntfy body — the
`log()` calls here only ever mention filenames and counts. The sidecar
JSON already only contains what history.jsonl itself already stores (no
new content exposure) plus purely numeric guard metrics.

BOUNDED (so this directory cannot grow without limit either): keeps the
newest KEEP_INCIDENTS (default 30) quarantined takes, evicting the oldest
whenever a new one is quarantined. Sizing: both guards' measured trip rate
on the real ~/dictation log (2026-07-22 to 2026-07-26, ~4 days) is 2 real
incidents total — roughly 1 every 2 days. 30 kept incidents covers ~2
months of history even at a 5x-elevated rate (1/day), while costing at
most ~30 * ~2MB (the observed per-wav size for a real take) =~60MB —
trivial, and nowhere near enough to matter against Tinho's disk. Every
eviction removes both the wav and its sidecar together (matched by stem),
never leaving an orphaned sidecar or wav behind.
"""
import datetime
import json
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))

INCIDENTS_DIR_NAME = "incidents"
DEFAULT_RECORDINGS_DIR = os.environ.get(
    "DICTATION_CONTENT_GUARD_RECORDINGS_DIR", os.path.join(HERE, "recordings"))
KEEP_INCIDENTS = int(os.environ.get("DICTATION_KEEP_INCIDENTS", "30"))


def incidents_dir(recordings_dir=None):
    recordings_dir = recordings_dir or DEFAULT_RECORDINGS_DIR
    return os.path.join(recordings_dir, INCIDENTS_DIR_NAME)


def _stem(filename):
    return os.path.splitext(filename)[0]


def _prune(idir, keep=KEEP_INCIDENTS, log=print):
    """Keep the newest `keep` quarantined wavs (by mtime), evicting the
    oldest ones — wav + its sidecar json together, matched by stem. Never
    raises: an eviction failure just leaves one extra file around for next
    time, never blocks the quarantine that triggered it."""
    try:
        wavs = [f for f in os.listdir(idir) if f.endswith(".wav")]
    except OSError:
        return
    wavs.sort(key=lambda f: os.path.getmtime(os.path.join(idir, f)))
    stale = wavs[:-keep] if keep > 0 else wavs
    for name in stale:
        stem = _stem(name)
        for candidate in (name, stem + ".json"):
            path = os.path.join(idir, candidate)
            try:
                if os.path.isfile(path):
                    os.unlink(path)
            except OSError as exc:
                log(f"incident-quarantine: evict {candidate} failed "
                    f"(ignored): {exc}")


def quarantine(wav_path, guard_name, metrics, history_entry=None,
              recordings_dir=None, keep=KEEP_INCIDENTS, log=print):
    """Copy `wav_path` (must already exist on disk) plus a metadata-only
    JSON sidecar into RECORDINGS_DIR/incidents/, then prune that directory
    to the newest `keep`. Never raises — a quarantine failure must never
    turn an already-detected, already-notified trip into a crash; the trip
    itself and its ntfy notification already happened in the caller before
    this runs. Idempotent: re-quarantining the same filename just
    overwrites its own copy (shutil.copy2), never duplicates.

    Returns the path the wav was copied to, or None if quarantine could
    not happen (source missing, directory uncreatable, etc.) — callers
    should treat None as "logged already, nothing more to do", never as an
    error to propagate.
    """
    try:
        if not wav_path or not os.path.isfile(wav_path):
            log(f"incident-quarantine: source wav missing, skipping "
                f"({wav_path})")
            return None
        idir = incidents_dir(recordings_dir)
        os.makedirs(idir, exist_ok=True)
        name = os.path.basename(wav_path)
        dest_wav = os.path.join(idir, name)
        shutil.copy2(wav_path, dest_wav)
        sidecar = {
            "quarantined_at": datetime.datetime.now().isoformat(
                timespec="seconds"),
            "guard": guard_name,
            "metrics": metrics or {},
        }
        if history_entry is not None:
            sidecar["history_entry"] = history_entry
        dest_json = os.path.join(idir, _stem(name) + ".json")
        with open(dest_json, "w", encoding="utf-8") as fh:
            json.dump(sidecar, fh, ensure_ascii=False, indent=2)
        log(f"incident-quarantine: preserved {name} ({guard_name} trip) "
            f"-> {dest_wav}")
        _prune(idir, keep=keep, log=log)
        return dest_wav
    except Exception as exc:
        log(f"incident-quarantine: failed (ignored, trip already logged "
            f"separately): {exc}")
        return None

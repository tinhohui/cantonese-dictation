#!/usr/bin/env python3
"""Single source of truth for "is Tinho recording right now (or just
finished)?" — shared by server.py, watchdog.py and editor.py.

Dictation is Tinho's #1 tool and #1 frustration (his universal input). No
background job — the periodic self-test, a shadow-batch flush that calls the
local qwen model, dictionary/consolidation housekeeping, or the GitHub sync
push — may compete with his OWN recording/transcribe/polish for CPU/GPU,
ever (hard requirement, 2026-07-23).

Design: server.py owns the real signals (Recorder.recording, the segment
worker's busy flag, the per-request lock) because they live inside main()'s
closures. Rather than duplicating that state, server.py REGISTERS one or more
zero-arg callables via register_signal(); this module ORs them together and
adds a hold-window after the last time any signal was true, so a background
job never starts crowding in during the few seconds of tail ASR/polish/paste
that follow a STOP. Modules that can't see server.py's internals at all
(editor.py, watchdog.py) just call busy() / wait_until_idle() — they need
know nothing about recorders, sockets or locks.

busy() is deliberately cheap (a handful of attribute reads/lock probes) so
polling it in a tight loop is fine.
"""
import os
import threading
import time





HOLD_SECONDS = float(os.environ.get("DICTATION_RECORDING_HOLD_SECONDS", "10"))

_lock = threading.Lock()
_signals = []
_last_active = 0.0
recording_active = threading.Event()


def set_recording_active(active):
    """Publish the foreground lifecycle without sharing request_lock.

    START sets this before touching the request lock and STOP clears it only
    after transcription/polish has completed.  Maintenance observes this
    event; foreground code never waits for maintenance.
    """
    global _last_active
    if active:
        recording_active.set()
        with _lock:
            _last_active = time.monotonic()
    else:
        recording_active.clear()


def register_signal(fn):
    """Register one more "am I busy right now" probe. Safe to call more than
    once (e.g. tests re-registering); all registered signals are OR'd."""
    with _lock:
        _signals.append(fn)


def clear_signals():
    """Test-only: reset registrations between test cases."""
    global _last_active
    with _lock:
        _signals.clear()
        _last_active = 0.0
    recording_active.clear()


def _raw_active():
    if recording_active.is_set():
        return True
    for fn in list(_signals):
        try:
            if fn():
                return True
        except Exception:
            continue
    return False


def busy(hold_seconds=None):
    """True if a recording (or the request/segment work it triggers) is in
    progress right now, or was within `hold_seconds` (default HOLD_SECONDS)."""
    global _last_active
    hold_seconds = HOLD_SECONDS if hold_seconds is None else hold_seconds
    now = time.monotonic()
    if _raw_active():
        with _lock:
            _last_active = now
        return True
    with _lock:
        since = now - _last_active
    return since < hold_seconds


def wait_until_idle(poll_seconds=5, max_wait=None, log=None, hold_seconds=None):
    """Block (via short sleeps, not a spin loop) until busy() is False.

    Housekeeping (shadow-batch flush, consolidation, sync) has no deadline of
    its own, so the default (max_wait=None) waits as long as it takes —
    recording always wins. Pass a bound only for callers that must eventually
    proceed (mirrors ab_harness.py's own wait_for_idle doctrine). Returns True
    if idle was reached, False if max_wait elapsed first (caller proceeds
    "carefully", per that same doctrine).
    """
    t0 = time.monotonic()
    logged = False
    while busy(hold_seconds=hold_seconds):
        if max_wait is not None and time.monotonic() - t0 > max_wait:
            if log:
                log("recording_gate: max_wait exceeded, proceeding carefully")
            return False
        if log and not logged:
            log("recording_gate: recording active — deferring background work")
            logged = True
        time.sleep(poll_seconds)
    return True

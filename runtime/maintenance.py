#!/usr/bin/env python3
"""Serialized, foreground-aware lane for dictation housekeeping.

Only this worker executes best-effort network/model maintenance.  Foreground
recording and transcription never use this queue or its lock.
"""
import os
import queue
import subprocess
import threading

import recording_gate

HERE = os.path.dirname(os.path.abspath(__file__))


_NTFY_TOPIC_FILE = os.path.join(HERE, ".ntfy_topic")


















DEFAULT_ESCALATE_EVERY = 3


def _resolve_ntfy_topic():
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if topic:
        return topic
    try:
        with open(_NTFY_TOPIC_FILE, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def default_notify(name, streak, exc):
    """Bounded FYI-level ntfy push for a maintenance job that has failed
    `streak` times in a row. Priority stays low/default; this NEVER posts to
    the tinho-os urgent topic — D10a reserves that for stop-breach /
    circuit-breaker-trip / system-down, and a stuck background job is none of
    those. A no-op (returns False) when no NTFY_TOPIC env var / .ntfy_topic
    file is configured — MaintenanceLane already logs every failure via
    `log`, so a missing topic loses the push, not the record."""
    topic = _resolve_ntfy_topic()
    if not topic:
        return False
    body = (f"dictation maintenance job '{name}' has failed {streak} times "
            f"in a row: {exc}")
    try:
        subprocess.run(
            ["curl", "-sS", "--max-time", "10",
             "-H", "Title: dictation maintenance stalled",
             "-H", "Priority: low", "-H", "Tags: warning",
             "-d", body, f"https://ntfy.sh/{topic}"],
            capture_output=True, timeout=15)
        return True
    except Exception:
        return False


class MaintenanceLane:
    def __init__(self, log=None, notify=default_notify,
                escalate_every=DEFAULT_ESCALATE_EVERY):
        self._log = log or (lambda *_args: None)
        self._notify = notify
        self._escalate_every = max(1, escalate_every)
        self._queue = queue.Queue()
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._started = False
        self._pending = set()
        self._pending_lock = threading.Lock()
        self._fail_streaks = {}
        self._fail_streaks_lock = threading.Lock()

    @property
    def lock(self):
        """The maintenance-only lock (exposed for regression tests)."""
        return self._lock

    def failure_streak(self, name):
        """Current consecutive-failure count for `name` (0 if it last
        succeeded, or has never run). Regression-test / diagnostic hook."""
        with self._fail_streaks_lock:
            return self._fail_streaks.get(name, 0)

    def start(self):
        with self._start_lock:
            if self._started:
                return
            self._started = True
            threading.Thread(
                target=self._run, name="dictation-maintenance", daemon=True
            ).start()

    def submit(self, name, fn, *args, dedupe=False, **kwargs):
        """Queue work and return immediately; optionally coalesce by name."""
        self.start()
        if dedupe:
            with self._pending_lock:
                if name in self._pending:
                    return False
                self._pending.add(name)
        self._queue.put((name, fn, args, kwargs, dedupe))
        return True

    def _run(self):
        while True:
            name, fn, args, kwargs, dedupe = self._queue.get()
            try:
                while True:



                    recording_gate.wait_until_idle(
                        poll_seconds=0.1, log=self._log)
                    with self._lock:
                        if recording_gate.busy():
                            continue
                        fn(*args, **kwargs)
                        break
            except Exception as exc:
                self._log(f"maintenance {name} failed (ignored): {exc}")
                self._on_failure(name, exc)
            else:
                self._on_success(name)
            finally:
                if dedupe:
                    with self._pending_lock:
                        self._pending.discard(name)
                self._queue.task_done()

    def _on_success(self, name):
        with self._fail_streaks_lock:
            self._fail_streaks.pop(name, None)

    def _on_failure(self, name, exc):
        with self._fail_streaks_lock:
            streak = self._fail_streaks.get(name, 0) + 1
            self._fail_streaks[name] = streak
        if self._notify and streak % self._escalate_every == 0:
            try:
                self._notify(name, streak, exc)
            except Exception as notify_exc:
                self._log(f"maintenance {name} escalation notify failed: "
                          f"{notify_exc}")

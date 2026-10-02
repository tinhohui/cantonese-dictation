#!/usr/bin/env python3
"""SenseVoice dictation server.

Keeps the model resident and listens on a Unix socket so each dictation
is just a socket round-trip instead of a cold model load.

Protocol (newline-terminated commands):
  START        begin recording from the default input device
  STOP         stop, transcribe, reply with one line of text
  LANG <code>  set language: auto | yue | zh | en | ja | ko
  PING         reply "pong"
"""

import collections
import datetime
import difflib
import functools
import json
import os
import re
import socket
import sys
from contextlib import contextmanager
import threading
import time
import traceback

import numpy as np
import sherpa_onnx
import sounddevice as sd
from opencc import OpenCC

import paragraph as paragraph_mod
import polish as polish_mod
import output_language_gate
import recording_gate

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.path.join(HERE, "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17")
SOCKET_PATH = "/tmp/dictation.sock"
SAMPLE_RATE = 16000


TAG_RE = re.compile(r"<\|[^|]*\|>")



cc = OpenCC("s2hk")


HISTORY_PATH = os.path.join(HERE, "history.jsonl")
LOCK_PATH = os.path.join(HERE, ".server.lock")
LOG_PATH = os.path.join(HERE, "server.log")


def log(msg):
    """Timestamped diagnostics. Hammerspoon discards our stdout, so a file is
    the only way to see per-stage timings when someone asks "why was it slow"."""
    line = f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def acquire_singleton():
    """Refuse to start a second instance.

    Two servers race for the unix socket and the microphone: the newer one wins
    the socket while the older keeps the HTTP port, so dictation silently
    records nothing. Hammerspoon respawns the server on every reload, which
    made this easy to hit.
    """
    import fcntl
    fh = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another server instance is already running — exiting",
              file=sys.stderr, flush=True)
        raise SystemExit(0)
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def log_history(text, raw=None, audio=None):
    """Append every transcription so nothing is lost if a paste misses.

    Pasting depends on whichever app has focus at release time; if focus moved,
    the text would otherwise be gone. `raw` (pre-polish ASR text) and `audio`
    (recording filename) make a bad result retryable and diagnosable.
    """
    if not text and not raw:
        return None
    try:
        entry = {"time": datetime.datetime.now().isoformat(timespec="seconds"),
                 "text": text}
        if raw and raw != text:
            entry["raw"] = raw
        if audio:
            entry["audio"] = audio
        with open(HISTORY_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry["time"]
    except OSError:
        return None


def log_history_correction(audio_name, text):
    """Append a post-STOP correction record once a grace-bounded background
    rewrite (item 2, 2026-07-26: "STOP throws away GPU work it already paid
    for") lands its polished text after the take was already pasted.

    D1 / Tinho's explicit ruling: what he already pasted must NEVER change
    under his cursor — he rejected any auto-rewrite-after-paste outright.
    So this NEVER touches the original history.jsonl record (whose `text`
    is exactly what got pasted) and NEVER re-pastes anything. It only
    appends a new, separate record linked by `correction_of` (the earlier
    record's `audio` filename) so the polished version is not silently
    thrown away — record and pasted text are allowed to differ; that is
    the accepted trade Tinho approved.

    Deliberately append-only, same safety contract as log_history(): a
    blind read-modify-write of a JSONL file other threads (and the
    SIGTERM emergency-flush handler) also append to concurrently would be
    its own hazard, so this never rewrites the original line in place.
    Never raises, never blocks a dictation over a history-file problem.
    """
    if not text or not audio_name:
        return None
    try:
        text, _, _ = output_language_gate.enforce(text, log=log)
        entry = {
            "time": datetime.datetime.now().isoformat(timespec="seconds"),
            "text": text,
            "correction_of": audio_name,
        }
        with open(HISTORY_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry["time"]
    except OSError:
        return None









SEGMENT_SECONDS = int(os.environ.get("DICTATION_SEGMENT_SECONDS", "15"))
SEGMENT_HARD_SECONDS = SEGMENT_SECONDS * 1.5






















SEGMENT_OVERLAP_SECONDS = float(
    os.environ.get("DICTATION_SEGMENT_OVERLAP_SECONDS", "0.5"))











TAIL_DECODE_SECONDS = float(os.environ.get("DICTATION_TAIL_DECODE_SECONDS", "1"))
STOP_BUDGET_SECONDS = float(os.environ.get("DICTATION_STOP_BUDGET_SECONDS", "0.5"))



























STOP_CAPTURE_FLUSH_FLOOR_SECONDS = float(
    os.environ.get("DICTATION_STOP_CAPTURE_FLUSH_FLOOR_SECONDS", "0.6"))
















STOP_REWRITE_GRACE_SECONDS = float(
    os.environ.get("DICTATION_STOP_REWRITE_GRACE_SECONDS", "3.0"))


REWRITE_INPUT_CHAR_CAP = int(os.environ.get("DICTATION_REWRITE_INPUT_CHAR_CAP", "4000"))
SEAM_CHAR_CAP = int(os.environ.get("DICTATION_SEAM_CHAR_CAP", "160"))
QUIET_RMS = 0.005
QUIET_RUN_SECONDS = 0.3










































FOREGROUND_LLM = os.environ.get("DICTATION_FOREGROUND_LLM", "1") not in (
    "0", "false", "off", "no")





















ASR_TARGET_RMS = float(os.environ.get("DICTATION_ASR_TARGET_RMS", "0.08"))
ASR_MAX_GAIN = float(os.environ.get("DICTATION_ASR_MAX_GAIN", "8.0"))
ASR_PEAK_CEILING = float(os.environ.get("DICTATION_ASR_PEAK_CEILING", "0.95"))














ASR_CLEAN_SNR = float(os.environ.get("DICTATION_ASR_CLEAN_SNR", "12.0"))
ASR_MIN_TARGET_FRACTION = float(
    os.environ.get("DICTATION_ASR_MIN_TARGET_FRACTION", "0.45"))
_NOISE_FRAME = 400



def estimate_snr(samples, frame=_NOISE_FRAME):
    """Ratio of overall energy to the 10th-percentile frame energy.

    Returns None when the buffer is too short to have gaps worth measuring, so
    callers fall back to treating the take as clean rather than guessing."""
    if samples is None or len(samples) < frame * 4:
        return None
    x = samples.astype(np.float32)
    usable = (len(x) // frame) * frame
    frames = x[:usable].reshape(-1, frame)
    energies = np.sqrt((frames ** 2).mean(axis=1))
    floor = float(np.percentile(energies, 10))
    rms = float(np.sqrt((x ** 2).mean()))
    if floor <= 0 or rms <= 0:
        return None
    return rms / floor


def normalise_for_asr(samples, target_rms=None, max_gain=None,
                      peak_ceiling=None):
    """Scale quiet audio up toward target_rms without ever clipping, lifting a
    NOISY take less than a clean one.

    Pure and total: returns the input unchanged when it is silent, already
    loud enough, or when any bound would be violated. Never attenuates."""
    target_rms = ASR_TARGET_RMS if target_rms is None else target_rms
    max_gain = ASR_MAX_GAIN if max_gain is None else max_gain
    peak_ceiling = ASR_PEAK_CEILING if peak_ceiling is None else peak_ceiling
    if samples is None or not len(samples) or target_rms <= 0:
        return samples
    rms = float(np.sqrt((samples.astype(np.float32) ** 2).mean()))
    peak = float(np.abs(samples).max())
    if rms <= 0 or peak <= 0:
        return samples
    snr = estimate_snr(samples)
    if snr is not None and snr < ASR_CLEAN_SNR:

        fraction = max(ASR_MIN_TARGET_FRACTION, snr / ASR_CLEAN_SNR)
        target_rms *= fraction
    gain = min(target_rms / rms, max_gain, peak_ceiling / peak)
    if gain <= 1.0:
        return samples
    return (samples.astype(np.float32) * gain).astype(samples.dtype)


STOP_TAIL_GRACE_SECONDS = float(
    os.environ.get("DICTATION_STOP_TAIL_GRACE_SECONDS", "0.2"))
ASR_TAIL_PAD_SECONDS = float(
    os.environ.get("DICTATION_ASR_TAIL_PAD_SECONDS", "0.5"))
SAFETY_MAX_SEGMENTS = 240








































HARD_PORTAUDIO_TIMEOUT = float(
    os.environ.get("DICTATION_HARD_PORTAUDIO_TIMEOUT", "3"))



PORTAUDIO_CALLBACK_VERIFY_TIMEOUT = float(
    os.environ.get("DICTATION_PORTAUDIO_CALLBACK_VERIFY_TIMEOUT", "1"))






























STREAM_WARMUP_SECONDS = float(
    os.environ.get("DICTATION_STREAM_WARMUP_SECONDS", "2.0"))




PORTAUDIO_AUDIO_VERIFY_TIMEOUT = float(
    os.environ.get("DICTATION_PORTAUDIO_AUDIO_VERIFY_TIMEOUT", "2.0"))









HARD_ASR_TIMEOUT = float(os.environ.get("DICTATION_HARD_ASR_TIMEOUT", "20"))




FLUSH_THREAD_CEILING = int(os.environ.get("DICTATION_FLUSH_THREAD_CEILING", "8"))












REQUEST_LOCK_WATCHER_CEILING = int(
    os.environ.get("DICTATION_REQUEST_LOCK_WATCHER_CEILING", "8"))
HARD_POLISH_TIMEOUT = float(os.environ.get("DICTATION_HARD_POLISH_TIMEOUT", "32"))



HARD_POLISH_TIMEOUT_FULL = float(
    os.environ.get("DICTATION_HARD_POLISH_TIMEOUT_FULL", "75"))


HARD_PORTAUDIO_TIMEOUT = float(
    os.environ.get("DICTATION_HARD_PORTAUDIO_TIMEOUT", "3"))


MAX_LOCK_SECONDS = float(os.environ.get("DICTATION_MAX_LOCK_SECONDS", "45"))
LOCK_MONITOR_POLL_SECONDS = float(
    os.environ.get("DICTATION_LOCK_MONITOR_POLL_SECONDS", "5"))
REQUEST_LOCK_ACQUIRE_TIMEOUT = float(
    os.environ.get("DICTATION_LOCK_ACQUIRE_TIMEOUT", "3"))
SELFTEST_LOCK_ACQUIRE_TIMEOUT = float(
    os.environ.get("DICTATION_SELFTEST_LOCK_TIMEOUT", "15"))
BUSY_REPLY = "busy/recovering, try again"


class RequestLockBusy(RuntimeError):
    """The #63 request-lock acquire ceiling elapsed."""


class RecoverableLock:
    """Serializes handle() requests without any clock-based recovery.

    Each acquisition creates a lease owned by the current thread. A daemon
    watcher joins that owner thread; if it dies without releasing, the lock
    is cleared and waiters are notified immediately. Live holders are never
    stolen, regardless of how long they legitimately run.
    """

    class Lease:
        def __init__(self, owner_thread):
            self.owner_thread = owner_thread
            self.acquired_at = time.monotonic()
            self.released = False

    def __init__(self):
        self._cond = threading.Condition()
        self._lease = None
        self._local = threading.local()
        self._watcher_count_lock = threading.Lock()
        self._watchers_outstanding = 0

    def is_held(self):
        """True while some thread holds this lock. Cheap and non-blocking —
        used by the sleep-assertion refresher to tell "idle" apart from
        "between recording and pasting", which is_recording alone cannot."""
        with self._cond:
            return self._lease is not None

    def _watch_lease(self, lease):
        try:
            lease.owner_thread.join()
            with self._cond:
                if self._lease is lease:
                    self._lease = None
                    self._cond.notify_all()
        finally:
            with self._watcher_count_lock:
                self._watchers_outstanding -= 1

    def acquire_current(self, timeout=-1):
        current = threading.current_thread()
        deadline = None if timeout is None or timeout < 0 else (
            time.monotonic() + timeout)
        with self._cond:
            while True:
                if self._lease is None:
                    lease = self.Lease(current)
                    self._lease = lease
                    with self._watcher_count_lock:
                        spawn_watcher = (
                            self._watchers_outstanding
                            < REQUEST_LOCK_WATCHER_CEILING)
                        if spawn_watcher:
                            self._watchers_outstanding += 1
                    if spawn_watcher:
                        threading.Thread(
                            target=self._watch_lease,
                            args=(lease,),
                            daemon=True,
                            name="request-lock-watcher").start()
                    return lease, True
                if not self._lease.owner_thread.is_alive():
                    self._lease = None
                    self._cond.notify_all()
                    continue
                if deadline is None:
                    self._cond.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None, False
                self._cond.wait(remaining)

    @contextmanager
    def hold(self):
        lease, acquired = self.acquire_current(
            timeout=REQUEST_LOCK_ACQUIRE_TIMEOUT)
        if not acquired:
            raise RequestLockBusy(BUSY_REPLY)
        try:
            yield lease
        finally:
            self.release_current(lease)

    def __enter__(self):
        lease, acquired = self.acquire_current()
        if not acquired:
            raise RuntimeError("failed to acquire recoverable lock")
        self._local.lease = lease
        return lease

    def __exit__(self, exc_type, exc, tb):
        lease = getattr(self._local, "lease", None)
        if lease is not None:
            self.release_current(lease)
        self._local.lease = None
        return False

    def release_current(self, lease):
        with self._cond:
            if self._lease is lease:
                lease.released = True
                self._lease = None
                self._cond.notify_all()

    def is_held(self):
        with self._cond:
            return self._lease is not None

    def held_seconds(self):
        with self._cond:
            if self._lease is None:
                return 0.0
            return max(0.0, time.monotonic() - self._lease.acquired_at)

    def force_recover(self):
        """Abandon the current lock and let future requests proceed.

        The holder keeps its own lease object, so a later release is harmless;
        this is the #63 last-resort recovery for a live thread blocked in C.
        """
        with self._cond:
            lease = self._lease
            if lease is None:
                return None
            held = max(0.0, time.monotonic() - lease.acquired_at)
            self._lease = None
            self._cond.notify_all()
            return held

























def _parse_bool_env(raw, default=False):
    if raw is None:
        return default
    return raw.strip().lower() not in ("", "0", "false", "no")


HOLD_SLEEP_ASSERTION = _parse_bool_env(
    os.environ.get("DICTATION_HOLD_SLEEP_ASSERTION"), default=False)
SLEEP_REFRESH_SECONDS = int(
    os.environ.get("DICTATION_SLEEP_REFRESH_MINUTES", "20")) * 60


def should_refresh_sleep_assertion(hold_assertion, is_recording,
                                   is_busy=False):
    """Pure decision: is now safe/appropriate to close+reopen the idle mic
    stream to keep the sleep-prevention assertion from ever going stale?

    Never while actively recording — reopen() briefly drops audio capture,
    which must never happen mid-dictation.

    Never while a take is still IN FLIGHT either (is_busy), and that second
    condition is why this signature changed on 2026-08-07. is_recording goes
    False the instant Tinho releases the key, but the take is not finished —
    ASR and polish still have to run. The refresher fired in exactly that
    window and rebuilt the InputStream underneath a decode in progress:

      23:27:11  recorded wall=1.1s          (recording ENDS, is_recording False)
      23:27:21  sleep-assertion refresher: reopened idle mic stream
      23:27:24  transcribed 1s tail: asr=8.430s

    8.4 seconds to decode ONE second of audio, with the machine near idle.
    Tinho's own diagnosis was right and the CPU-contention theory was wrong:
    this fires on a 20-minute timer, so it hits at random regardless of how
    busy the day has been, which is exactly how he described it.

    Skipping costs nothing — the loop simply refreshes on its next tick."""
    return (not hold_assertion) and (not is_recording) and (not is_busy)


class StreamCallbackVerificationError(RuntimeError):
    """A started InputStream never proved it could deliver a callback."""


class StreamSilentError(RuntimeError):
    """A started InputStream delivered callbacks, but every sample in them
    was exactly zero — digital silence, not a quiet room.

    This is the AirPods/Bluetooth failure class (2026-07-29 investigation):
    CoreAudio keeps calling back on schedule while the HFP link delivers
    nothing, so `stream.start()` succeeding and
    StreamCallbackVerificationError not firing both mean nothing. Raised only
    by _open_stream(require_audio=True) — i.e. the FIRST rebuild attempt in
    reopen(), so it lands in reopen()'s existing rescan-and-retry-once path
    (sd._terminate()/_initialize(), the only thing that makes PortAudio
    re-resolve the default input device). The retry never raises it: leaving
    the Recorder with no stream at all would be strictly worse than keeping a
    silent one.
    """


RECORDINGS_DIR = os.path.join(HERE, "recordings")






INCIDENTS_DIR_NAME = "incidents"





































_KEEP_RECORDINGS_OVERRIDDEN = "DICTATION_KEEP_RECORDINGS" in os.environ
KEEP_RECORDINGS = int(os.environ.get("DICTATION_KEEP_RECORDINGS", "10"))






RECORDINGS_MAX_BYTES = int(
    os.environ.get("DICTATION_RECORDINGS_MAX_MB", "2048")) * 1024 * 1024

CHECKED_REGISTRY_PATH = os.path.join(HERE, ".recordings_checked.json")
_checked_lock = threading.RLock()


def _load_checked_registry():
    try:
        with open(CHECKED_REGISTRY_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_checked_registry(registry):
    try:
        with open(CHECKED_REGISTRY_PATH, "w", encoding="utf-8") as fh:
            json.dump(registry, fh)
    except OSError:
        pass


def mark_recording_checked(name):
    """Record that `name` (a RECORDINGS_DIR wav filename) has been verified
    CLEAN by content_guard.py's content-loss canary, so it has served its
    "verification not yet performed" purpose and is now eligible for
    eviction under the disk-size backstop (see _prune_recordings). Called by
    content_guard.run_once() on every check that does NOT trip -- a FLAGGED
    take's evidence purpose is instead served by incident_quarantine.py's
    copy into recordings/incidents/, which this module's prune never
    touches, so a tripped take is deliberately never marked here and stays
    ineligible (kept) in its original location too.

    Never raises: a failed mark just leaves the take ineligible (kept) one
    cycle longer, which is always the safe direction per D1."""
    if not name:
        return
    with _checked_lock:
        registry = _load_checked_registry()
        registry[name] = datetime.datetime.now().isoformat(timespec="seconds")



        try:
            on_disk = set(os.listdir(RECORDINGS_DIR))
        except OSError:
            on_disk = None
        if on_disk is not None:
            registry = {k: v for k, v in registry.items() if k in on_disk}
        _write_checked_registry(registry)


def _is_checked_clean(name, registry):
    return name in registry


def _list_recording_wavs():
    """Every plain file directly under RECORDINGS_DIR, oldest-first by name
    (filenames are YYYYMMDD-HHMMSS.wav, so a name sort IS a time sort).
    Never includes RECORDINGS_DIR/incidents/ itself or anything inside it."""
    try:
        entries = os.listdir(RECORDINGS_DIR)
    except OSError:
        return []
    return sorted(
        f for f in entries
        if f != INCIDENTS_DIR_NAME
        and os.path.isfile(os.path.join(RECORDINGS_DIR, f)))


def _prune_recordings():
    """Evict stale wavs from RECORDINGS_DIR -- see the retention comment
    block above KEEP_RECORDINGS for the full rationale. Two modes:

    - DICTATION_KEEP_RECORDINGS override set: reproduces the exact OLD
      count-based prune (PR #31) against the raw, UNFILTERED directory
      listing, byte for byte -- this is the one path that must match prior
      behaviour exactly, including its historical quirk that
      KEEP_RECORDINGS=0 prunes nothing (`old[:-0]` is `old[:0]`, an empty
      slice) rather than everything.
    - Default: purpose-driven, bounded by RECORDINGS_MAX_BYTES -- see the
      comment block above."""
    try:
        raw_entries = sorted(os.listdir(RECORDINGS_DIR))
    except OSError:
        return

    if _KEEP_RECORDINGS_OVERRIDDEN:
        for stale in raw_entries[:-KEEP_RECORDINGS]:
            os.unlink(os.path.join(RECORDINGS_DIR, stale))
        return

    wavs = [f for f in raw_entries
           if f != INCIDENTS_DIR_NAME
           and os.path.isfile(os.path.join(RECORDINGS_DIR, f))]
    sizes = {}
    total_bytes = 0
    for name in wavs:
        try:
            sizes[name] = os.path.getsize(os.path.join(RECORDINGS_DIR, name))
        except OSError:
            sizes[name] = 0
        total_bytes += sizes[name]

    if total_bytes <= RECORDINGS_MAX_BYTES:
        return

    registry = _load_checked_registry()
    eligible = [f for f in wavs if _is_checked_clean(f, registry)]
    for stale in eligible:
        if total_bytes <= RECORDINGS_MAX_BYTES:
            break
        try:
            os.unlink(os.path.join(RECORDINGS_DIR, stale))
            total_bytes -= sizes.get(stale, 0)
        except OSError:
            pass


def save_recording(samples, rate):
    """Persist the raw audio so a bad transcription can be retried and a lost
    one diagnosed, then run retention (_prune_recordings — purpose-driven by
    default; DICTATION_KEEP_RECORDINGS reproduces the old fixed-count
    behaviour — see the comment block above KEEP_RECORDINGS)."""
    try:
        os.makedirs(RECORDINGS_DIR, exist_ok=True)
        name = datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + ".wav"
        path = os.path.join(RECORDINGS_DIR, name)
        import wave
        with wave.open(path, "w") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes((samples * 32767).astype(np.int16).tobytes())
        _prune_recordings()
        return name
    except OSError:
        return None


class Recorder:
    """Holds the microphone stream open for the whole process lifetime.

    Opening the stream per dictation looked cleaner, but an idle app-bundle
    process gets App Nap'd by macOS, and a napped process's audio callback is
    serviced late enough that CoreAudio silently discards chunks mid-recording
    (measured: ~4s missing from a 20s take, dropouts=0 — no overflow flag, the
    frames simply never arrived). A process actively doing audio I/O is exempt
    from App Nap, so the stream stays open and a flag decides whether frames
    are kept. Bonus: recording starts instantly — no device spin-up on START,
    so the first syllable is never clipped.
    """

    def __init__(self):
        self.frames = []
        self.total = 0
        self.level = 0.0
        self.recording = False
        self.dropouts = 0
        self.segment_count = 0
        self.pending_segments = []
        self.tail_epoch = 0
        self.partial_getter = lambda _epoch: ""
        self.segment_event = threading.Event()


        self.lock = RecoverableLock()
        self.stream = None





        self._stream_generation = 0
        self._stream_callback_seen = threading.Event()




        self._stream_audio_seen = threading.Event()





        self._warm_until = 0.0
        self._warm_confirmed = False




        self.stream_delivering_silence = False




        self.last_wall = 0.0
        self._open_stream()

    def _callback(self, generation, indata, frames, time_info, status):
        if generation != self._stream_generation:
            return
        self._stream_callback_seen.set()







        if indata.size and indata.any():
            self._warm_confirmed = True
            self._stream_audio_seen.set()
        if not self.recording:
            return

        import time as _t
        now = _t.time()
        if not self._warm_confirmed and now < self._warm_until:




            return
        if self.last_cb and now - self.last_cb > 0.2:
            self.gaps.append((round(now - self.t0, 1), round(now - self.last_cb, 2)))
        self.last_cb = now
        if status and status.input_overflow:
            self.dropouts += 1
        self.frames.append(indata.copy())
        self.total += indata.shape[0]


        rms = float(np.sqrt((indata[:, 0] ** 2).mean())) if indata.size else 0.0
        self.level = rms
        if rms < QUIET_RMS:
            self.quiet_samples += indata.shape[0]
        else:
            self.quiet_samples = 0



        due = self.total >= SEGMENT_SECONDS * self.capture_rate
        at_pause = self.quiet_samples >= QUIET_RUN_SECONDS * self.capture_rate
        overdue = self.total >= SEGMENT_HARD_SECONDS * self.capture_rate
        if due and (at_pause or overdue):
            self.segment_count += 1
            if self.segment_count <= SAFETY_MAX_SEGMENTS:
                epoch = self.tail_epoch
                fallback = self.partial_getter(epoch)
                self.pending_segments.append({
                    "frames": self.frames,
                    "epoch": epoch,
                    "entry": {
                        "segment_id": epoch,
                        "raw": fallback,
                        "text": fallback,
                        "ready": False,
                        "provisional": True,
                        "final": False,
                        "ready_event": threading.Event(),
                    },
                })
                self.segment_event.set()
                self.tail_epoch += 1







                self.frames, self.total = self._trailing_frames(
                    int(SEGMENT_OVERLAP_SECONDS * self.capture_rate))
            else:
                self.recording = False
                self.frames = []
                self.total = 0
            self.quiet_samples = 0

    def _retire_stream(self, stream, label):
        """Prove PortAudio can no longer invoke `stream`'s Python callback
        BEFORE the last reference to it is dropped (see CALLBACK
        USE-AFTER-FREE HARDENING further down this file).

        Advances _stream_generation first, so any callback that fires during
        the stop/close window is already rejected by _callback()'s existing
        generation guard and can neither satisfy a new stream's verification
        nor append to the next take's buffer. Then stops and closes the
        stream, each bounded by the same guarded_call/HARD_PORTAUDIO_TIMEOUT
        ceiling reopen() has always used. Returns True if either call
        wedged, in which case the stream is parked in _ABANDONED_STREAMS
        forever instead of being collected. Never raises: retirement runs on
        recovery paths, where an exception would be worse than a leak."""
        if stream is None:
            return False
        self._stream_generation += 1
        wedged = False
        try:
            _, stop_timed_out = guarded_call(
                stream.stop, HARD_PORTAUDIO_TIMEOUT, "portaudio stream.stop")
        except Exception:
            stop_timed_out = False
        if stop_timed_out:
            wedged = True
            _note_portaudio_wedge("stop")
        else:
            try:
                _, close_timed_out = guarded_call(
                    stream.close, HARD_PORTAUDIO_TIMEOUT,
                    "portaudio stream.close")
            except Exception:
                close_timed_out = False
            if close_timed_out:
                wedged = True
                _note_portaudio_wedge("close")
        if wedged:
            _abandon_stream_forever(stream, label)
        return wedged

    def _open_stream(self, force_portaudio_reinit=False, require_audio=False,
                     attempt_label="initial open", device=None):









































        if self.stream is not None:
            retiring, self.stream = self.stream, None
            self._retire_stream(retiring, attempt_label)
        if force_portaudio_reinit:














            try:
                _, terminate_timed_out = guarded_call(
                    sd._terminate, HARD_PORTAUDIO_TIMEOUT, "portaudio _terminate")
                if terminate_timed_out:
                    _note_portaudio_wedge("_terminate")
                _, initialize_timed_out = guarded_call(
                    sd._initialize, HARD_PORTAUDIO_TIMEOUT, "portaudio _initialize")
                if initialize_timed_out:
                    _note_portaudio_wedge("_initialize")
            except Exception as exc:
                log(f"PortAudio re-init (sd._terminate/_initialize) raised "
                    f"(continuing to attempt opening the stream anyway): {exc}")
        try:
            if device is not None:
                self.capture_rate = int(
                    sd.query_devices(device)["default_samplerate"])
            else:
                self.capture_rate = int(
                    sd.query_devices(kind="input")["default_samplerate"])
        except Exception:
            self.capture_rate = 48000
        self._stream_generation += 1
        generation = self._stream_generation
        self._stream_callback_seen.clear()
        self._stream_audio_seen.clear()
        self._warm_confirmed = False

        def current_stream_callback(indata, frames, time_info, status):
            self._callback(generation, indata, frames, time_info, status)




        stream_kwargs = dict(
            samplerate=self.capture_rate, channels=1, dtype="float32",
            callback=current_stream_callback)
        if device is not None:
            stream_kwargs["device"] = device
        self.stream = sd.InputStream(**stream_kwargs)
        self._warm_until = time.time() + STREAM_WARMUP_SECONDS






        _, start_timed_out = guarded_call(
            self.stream.start, HARD_PORTAUDIO_TIMEOUT, "portaudio stream.start")
        if start_timed_out:
            _note_portaudio_wedge("start")
            raise TimeoutError(
                f"stream.start() did not return within "
                f"{HARD_PORTAUDIO_TIMEOUT:.0f}s")
        if not self._stream_callback_seen.wait(PORTAUDIO_CALLBACK_VERIFY_TIMEOUT):
            raise StreamCallbackVerificationError(
                f"fresh InputStream produced no callback within "
                f"{PORTAUDIO_CALLBACK_VERIFY_TIMEOUT:.1f}s")






        if self._stream_audio_seen.wait(PORTAUDIO_AUDIO_VERIFY_TIMEOUT):
            self.stream_delivering_silence = False
            return






        _note_silent_stream(attempt_label, will_retry=require_audio)
        if require_audio:
            raise StreamSilentError(
                f"fresh InputStream delivered only digital silence within "
                f"{PORTAUDIO_AUDIO_VERIFY_TIMEOUT:.1f}s")
        self.stream_delivering_silence = True

    def reopen(self, force_reinit=False):
        """A persistent stream is pinned to the device it opened on; if the
        default input changed (AirPods came/went) it can go silent, or the
        reopen itself can fail outright with PaErrorCode -9986 (stale
        PortAudio device table — see _open_stream). force_reinit=True skips
        straight to the full PortAudio rescan on the first attempt (used by
        transcribe() when it has already detected a dead-stream zero-capture,
        so there's no point trying the same stale table again first)."""
        with self.lock:
            self.recording = False
            old_stream = self.stream
            wedged = False
















            if old_stream is not None:





                self.stream = None
                wedged = self._retire_stream(old_stream, "reopen")
            try:




                self._open_stream(
                    force_portaudio_reinit=(force_reinit or wedged),







                    require_audio=True,
                    attempt_label="first rebuild")
            except Exception as exc:
                if isinstance(exc, StreamCallbackVerificationError):
                    _note_portaudio_rebuild_failure("first rebuild", will_retry=True)




                log(f"mic stream open failed ({exc}) — forcing a full "
                    f"PortAudio device rescan (stale device table, "
                    f"PaErrorCode -9986 cause) and retrying once")
                try:






                    self._open_stream(force_portaudio_reinit=True,
                                      attempt_label="rescan retry")













                    if self.stream_delivering_silence:
                        self._fallback_to_other_input_device()
                except Exception as exc2:










                    dead, self.stream = self.stream, None
                    self._retire_stream(dead, "failed rebuild")
                    if isinstance(exc2, StreamCallbackVerificationError):
                        _note_portaudio_rebuild_failure("rescan retry", will_retry=False)
                    log(f"MIC STREAM DEAD — could not reopen after "
                        f"PortAudio re-init: {exc2}")
                    raise

    def _fallback_to_other_input_device(self):
        """Called only from reopen(), only after the default input device's
        own rescan-and-retry has already come back silent (self.stream is
        open and stream_delivering_silence is True at this point). Tries the
        other input devices PortAudio's own (just-rescanned) device table
        knows about, in _rank_fallback_input_devices() preference order, and
        adopts the first one that proves non-silent by opening a stream
        directly on its device index (device= in _open_stream) — this never
        touches macOS's system-wide default input, only which device THIS
        process's PortAudio stream is bound to.

        On total failure (every candidate device also silent, or no other
        input devices exist) the invariant is unchanged: a silent-but-live
        stream still beats no stream. self.stream_delivering_silence is
        left exactly as the caller's rescan retry set it (True), and
        self.stream holds a live stream — the last candidate that got as
        far as starting, or, if not one candidate even reached that point,
        a fresh stream rebuilt on the default device by the surrender path
        at the end of this method. self.stream is never left None here.
        """
        try:
            failed = sd.query_devices(kind="input")
            failed_index = failed.get("index")
            failed_name = failed.get("name", "?")
        except Exception:
            failed_index, failed_name = None, "?"

        for idx, name in _rank_fallback_input_devices(exclude_index=failed_index):
            try:
                self._open_stream(
                    force_portaudio_reinit=False,
                    require_audio=True,
                    attempt_label=f"device fallback (device {idx} {name!r})",
                    device=idx)
            except Exception:
                continue
            log(f"mic-fallback: input device {failed_name!r} "
                f"(index {failed_index}) was silent even after a full "
                f"PortAudio rescan — switched capture to device {idx} "
                f"({name!r}), which delivered non-silent audio")
            return








        if self.stream is None:
            try:
                self._open_stream(force_portaudio_reinit=False,
                                  attempt_label="fallback surrender rebuild")
            except Exception as exc:
                log(f"mic-fallback: could not rebuild any stream after every "
                    f"candidate device failed outright: {exc}")
        log(f"mic-fallback: input device {failed_name!r} (index "
            f"{failed_index}) was silent even after rescan, and no other "
            f"candidate input device delivered non-silent audio either — "
            f"keeping the silent stream on {failed_name!r}")

    def start(self):
        with self.lock:
            if self.recording:
                return
            import time as _t
            self.frames = []
            self.total = 0
            self.quiet_samples = 0
            self.dropouts = 0
            self.segment_count = 0
            self.pending_segments = []
            self.tail_epoch += 1
            self.gaps = []
            self.last_cb = None
            self.t0 = _t.time()
            self.recording = True

    def stop(self):
        import time as _t



        if STOP_TAIL_GRACE_SECONDS > 0 and self.recording:
            _t.sleep(STOP_TAIL_GRACE_SECONDS)
        with self.lock:
            if not self.recording:
                return np.zeros(0, dtype=np.float32)
            self.recording = False
            wall = _t.time() - self.t0
            self.last_wall = wall
            if self.gaps:
                log(f"callback gaps (at_s, gap_s): {self.gaps[:20]}")
            log(f"recorded wall={wall:.1f}s")
            if not self.frames:
                return np.zeros(0, dtype=np.float32)
            return np.concatenate(self.frames, axis=0).flatten()

    def _trailing_frames(self, n_samples):
        """Return (frames, samples) copying up to the last n_samples of
        self.frames, oldest-first -- used by _callback's SEGMENT-BOUNDARY
        WORD LOSS FIX to seed the next segment's buffer with a copy of the
        outgoing segment's own trailing overlap window (see
        SEGMENT_OVERLAP_SECONDS). Called only from inside the audio
        callback, same as everything else that touches self.frames there --
        no separate lock needed. n_samples<=0 (or an empty buffer) returns
        ([], 0), i.e. overlap is opt-out via
        DICTATION_SEGMENT_OVERLAP_SECONDS=0, matching pre-fix behaviour
        exactly."""
        if n_samples <= 0 or not self.frames:
            return [], 0
        out = []
        remaining = n_samples
        for block in reversed(self.frames):
            if remaining <= 0:
                break
            take = min(remaining, block.shape[0])
            out.append(block[-take:].copy())
            remaining -= take
        out.reverse()
        samples = sum(b.shape[0] for b in out)
        return out, samples

    def tail_snapshot(self):
        """Copy the current bounded tail for background ASR."""
        with self.lock:
            epoch = self.tail_epoch
            frames = list(self.frames)
            rate = self.capture_rate
        samples = (np.concatenate(frames, axis=0).flatten()
                   if frames else np.zeros(0, dtype=np.float32))
        return epoch, samples, rate

    def force_recover_stream(self):
        """#63: recover the deeper stream lock and rebuild its live stream."""
        recovered = self.lock.force_recover()
        if recovered is None:
            return None, False
        self.recording = False
        self.frames = []
        self.total = 0
        self.quiet_samples = 0
        try:
            self._open_stream()
            return recovered, True
        except Exception:
            return recovered, False


def build_recognizer(language):
    return sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=os.path.join(MODEL_DIR, "model.int8.onnx"),
        tokens=os.path.join(MODEL_DIR, "tokens.txt"),
        use_itn=True,
        language=language,
        num_threads=4,
        debug=False,
    )


























































REPAIR_MIN_FRAGMENT_LEN = int(
    os.environ.get("DICTATION_REPAIR_MIN_FRAGMENT_LEN", "6"))
REPAIR_MIN_RATIO = float(os.environ.get("DICTATION_REPAIR_MIN_RATIO", "0.85"))
REPAIR_MIN_MARGIN = float(os.environ.get("DICTATION_REPAIR_MIN_MARGIN", "0.12"))







REPAIR_HISTORY_VOCAB_CAP = int(
    os.environ.get("DICTATION_REPAIR_HISTORY_VOCAB_CAP", "100"))












REPAIR_VOCAB_PERSIST_PATH = os.path.join(HERE, "repair_vocab.json")







REPAIR_MAX_TEXT_CHARS = int(
    os.environ.get("DICTATION_REPAIR_MAX_TEXT_CHARS", "2000"))

_LATIN_RUN_RE = re.compile(r"[A-Za-z][A-Za-z' -]*[A-Za-z]|[A-Za-z]")
_LATIN_TERM_RE = re.compile(r"[A-Za-z][A-Za-z0-9 .'-]*")


def _collapsed(s):
    return re.sub(r"[\s\-]", "", s).lower()


@functools.lru_cache(maxsize=1)
def _common_english_words():
    """Best-effort system word list for conservative single-token gating."""
    words = set()
    for path in (
        "/usr/share/dict/words",
        "/usr/share/dict/web2a",
        "/usr/share/dict/web2",
    ):
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    word = line.strip().lower()
                    if word and word.isalpha():
                        words.add(word)
        except OSError:
            continue
    return words


def _is_common_english_word(word):
    w = word.lower()
    words = _common_english_words()
    if w in words:
        return True
    if len(w) > 3 and w.endswith("s") and w[:-1] in words:
        return True
    if len(w) > 4 and w.endswith("es") and w[:-2] in words:
        return True
    if len(w) > 4 and w.endswith("ed") and w[:-2] in words:
        return True
    if len(w) > 5 and w.endswith("ing") and w[:-3] in words:
        return True
    return False













_REPAIR_STOPWORDS = frozenset("""
    a an the is are was were be been being do does did doing not no yes
    to of in on at for with without and or but if then so because as
    i you he she it we they my your his her its our their me him them
    this that these those what who how why when where which will would
    can could should shall may might must have has had let's lets
""".split())


def dictionary_only_repair_vocabulary(dictionary_terms=None, max_phrase_len=30):
    """The curated subset of build_repair_vocabulary(): dictionary.json's
    term NAMES only, never history-mined phrases. Exposed separately
    because it doubles as repair_latin_fragments()'s FUZZY vocabulary (see
    that function's docstring for why fuzzy matching is restricted to this
    curated set) while the full build_repair_vocabulary() output remains
    available for EXACT matching, which has no such restriction."""
    vocab = set()
    for term in (dictionary_terms or {}):
        term = term.strip()
        if term and _LATIN_TERM_RE.fullmatch(term) and len(term) <= max_phrase_len:
            vocab.add(term)
    return sorted(vocab)


def _load_persisted_repair_counts(path):
    """Fail-safe read of the durable {phrase: count} mining state written by
    a PREVIOUS process's build_repair_vocabulary() call -- same
    missing/corrupt-is-fine contract as the history scan below. Any error
    (missing file, corrupt JSON, wrong shape) yields an empty dict, never an
    exception. Read only once, at process start, by build_repair_vocabulary()
    -- never per-take (D71)."""
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        counts = data.get("counts")
        if not isinstance(counts, dict):
            return {}
        return {str(k): int(v) for k, v in counts.items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def _save_persisted_repair_counts(path, counts):
    """Best-effort write of the already-`cap`-bounded {phrase: count} state
    so a future history.jsonl prune (prune_served_history(), Tinho's
    use-then-delete ruling) can never again erase vocabulary this process
    has already earned. Atomic (write-then-rename) so a crash mid-write
    can't corrupt the file the next restart reads. Failure is silent --
    losing this write only costs the durability gain for this one restart,
    never a crash; the in-memory vocabulary this call already computed is
    unaffected either way."""
    if not path:
        return
    try:
        tmp_path = f"{path}.tmp-{os.getpid()}"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump({"schema": 1,
                       "updated": datetime.datetime.now().isoformat(timespec="seconds"),
                       "counts": counts}, fh, ensure_ascii=False)
        os.replace(tmp_path, path)
    except OSError:
        pass


def build_repair_vocabulary(dictionary_terms=None, history_path=None,
                             cap=REPAIR_HISTORY_VOCAB_CAP,
                             min_len=REPAIR_MIN_FRAGMENT_LEN,
                             max_words=3, max_phrase_len=30,
                             persist_path=None):
    """Bounded vocabulary of known-good English/tech phrases for
    repair_latin_fragments(). Three read-only sources, all fail-safe to
    contributing nothing on any error (history is a convenience, same
    contract as log_history()):

      - dictionary.json's already-curated term NAMES (the correct spelling,
        e.g. "Codex", "Supabase", "IBKR" -- not polish.py's mangled-variant
        match patterns, which are a different, larger list this never reads).
        Always included in full -- this list is itself small and curated, so
        it does not need the D71 cap.
      - 2-3 word n-grams mined from the most frequent bounded phrases in
        history.jsonl's already-POLISHED `text` field (deliberately NOT
        `raw` -- a ONE-TIME scan when this is called, callers must call it
        once at process start, never per interval/per STOP). A candidate
        must appear at least twice (habitual usage, not a one-off) and
        contain no all-stopword boundary word, to keep generic English
        ("I do", "to use") out of a vocabulary meant to represent Tinho's
        own recurring technical terms.

        Mining from `text` rather than `raw` is a measured fix, not a
        stylistic choice: an earlier version mined `raw` and self-poisoned
        -- "when" occasionally mis-decodes as "w e n", and once that
        appeared twice in RAW history it entered the vocabulary as a
        "known" phrase, which then WRONGLY replaced a later, correctly
        recognised "when" with "w e n" (measured against real
        history.jsonl: this exact corruption fired repeatedly). `text` has
        already been through polish.py's dictionary substitution (and, for
        longer takes, LLM correction), so it is far less likely to contain
        the very ASR garbage this function exists to fix.
      - `persist_path` (2026-08-20 INCIDENT: history.jsonl's use-then-delete
        ruling, prune_served_history() in editor.py, was silently erasing
        this vocabulary every time a mined phrase's rows aged out of
        history.jsonl -- measured 191/92 on 2026-08-16 down to 138/89 by
        2026-08-20 as history.jsonl collapsed 4648 -> 304 rows under that
        SAME ruling). When given, a small persisted {phrase: count} file
        from a previous run is UNIONED with whatever history.jsonl offers
        today -- per phrase, the MAX of the two counts, never a sum, so
        repeated restarts re-mining the same still-present history can't
        inflate a count without bound -- and the merged, still-`cap`-bounded
        result is written back. A phrase mined once now survives forever,
        independent of how much of history.jsonl remains on disk. Read and
        written exactly ONCE here, at process start (main()'s only call site
        passes persist_path) -- same D71 contract as the history scan.
        Defaults to None (no persistence, no disk I/O beyond history_path)
        so every existing caller/test that builds a vocabulary from a
        hand-built history_path and expects ONLY that content is completely
        unaffected.
    """
    vocab = set(dictionary_only_repair_vocabulary(dictionary_terms, max_phrase_len))
    counter = collections.Counter()
    if history_path and os.path.isfile(history_path):
        try:
            with open(history_path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        entry = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    polished = entry.get("text") or ""
                    for run in _LATIN_RUN_RE.findall(polished):
                        words = run.split()
                        for n in range(2, max_words + 1):
                            for i in range(len(words) - n + 1):
                                gram = words[i:i + n]
                                if (gram[0].lower() in _REPAIR_STOPWORDS
                                        or gram[-1].lower() in _REPAIR_STOPWORDS):
                                    continue
                                phrase = " ".join(gram)
                                if not (min_len <= len(phrase) <= max_phrase_len):
                                    continue
                                counter[phrase.lower()] += 1
        except OSError:
            pass

    merged = _load_persisted_repair_counts(persist_path)
    for phrase, count in counter.items():
        if count > merged.get(phrase, 0):
            merged[phrase] = count
    ranked = sorted(merged.items(), key=lambda kv: kv[1], reverse=True)[:cap]

    for phrase, count in ranked:
        if count >= 2:
            vocab.add(phrase)
    if persist_path:
        _save_persisted_repair_counts(persist_path, dict(ranked))
    return sorted(vocab)


_LATIN_WORD_RE = re.compile(r"[A-Za-z]+")



REPAIR_MAX_WINDOW_WORDS = int(os.environ.get("DICTATION_REPAIR_MAX_WINDOW_WORDS", "8"))













_REPAIR_BOUNDARY_STOPWORDS = frozenset("""
    i a an the is are was were to of in on and or but so do does did not
    we you he she it they my your his her our their him them its
    this that what how why when where which will would can could should
    must may might have has had be been being
""".split())


def _best_vocabulary_match(fragment, vocabulary, fuzzy_vocabulary, min_len,
                            min_ratio, min_margin, boundary_tokens=None):
    """Shared scoring for one candidate window. Returns (claimed, replacement):

      - (True, text) -- an exact collapsed match was found (checked against
        the FULL `vocabulary`, dictionary + history-mined) and differs from
        `fragment` (e.g. casing) -> caller applies `text`.
      - (True, None) -- an exact collapsed match was found and IS
        `fragment` already -> caller consumes the window with NO text
        change, and — critically — does NOT fall through to try shorter
        sub-windows inside it. Without this, a window that is already
        exactly correct (e.g. "Claude Code") could fail the "already
        correct, no-op" check, fall through to a SHORTER window ("Code"
        alone), and get wrongly fuzzy-matched against an unrelated
        vocabulary entry ("Codex") purely because it happens to look
        similar in isolation -- caught by this function's own test suite
        (test_exact_match_that_is_already_correct_is_untouched).
      - (False, None) -- nothing confidently matches at this window size;
        caller should try a shorter window.

    FUZZY matching is checked only against `fuzzy_vocabulary` (the curated
    dictionary.json subset), never the full history-mined `vocabulary` --
    measured against real history.jsonl (see PR body / repair_latin_
    fragments docstring): fuzzy-matching generic history-mined phrases
    produced repeated, semantically-wrong corrections ("me for permission"
    -> "more permission", dropping the real word "me" and changing the
    sentence's meaning) whose SequenceMatcher ratio (0.87-0.90) was NOT
    separable by any threshold from the genuine win this feature exists for
    ("talkingken" -> "talking token", ratio 0.909) -- three rounds of
    tightening (boundary stopwords, a higher length floor, mining from
    `text` instead of `raw`) each closed one failure mode and left this one
    open, because it is not a bug, it is a property of ratio-based
    similarity on ordinary, non-curated English: two different SHORT
    prefixes sharing one LONG common suffix ("permission") always score
    high regardless of how unrelated the prefixes are. Exact matching has
    no such failure mode (identity is identity), so it stays scoped to the
    full vocabulary; only fuzzy is restricted.
    """
    fc = _collapsed(fragment)
    curated = frozenset(fuzzy_vocabulary)
    exact = next((p for p in vocabulary if _collapsed(p) == fc), None)
    if exact is not None:





        if boundary_tokens is None and (
                exact not in curated or _is_common_english_word(fragment)):
            return False, None
        return True, (exact if exact != fragment else None)
    if len(fragment) < min_len:
        return False, None











    if boundary_tokens and any(
            any(_collapsed(p) == _collapsed(tok) for p in vocabulary)
            for tok in boundary_tokens):
        return False, None
    best_ratio, second_ratio, best_phrase = 0.0, 0.0, None
    for phrase in fuzzy_vocabulary:
        if (boundary_tokens is None and " " not in phrase
                and _is_common_english_word(fragment)):
            continue
        ratio = difflib.SequenceMatcher(None, fc, _collapsed(phrase)).ratio()
        if ratio > best_ratio:
            best_ratio, second_ratio, best_phrase = ratio, best_ratio, phrase
        elif ratio > second_ratio:
            second_ratio = ratio
    if (best_phrase is not None and best_ratio >= min_ratio
            and best_ratio - second_ratio >= min_margin
            and best_phrase.lower() != fragment.lower()):
        return True, best_phrase
    return False, None



































SHORT_FRAGMENT_MIN_LEN = 2


def _flanked_by_cjk(text, start, end):
    """True if text[start:end] sits between two CJK characters, tolerating
    at most one intervening space per side -- see the module note above for
    the measured real-evidence spacing this accounts for."""
    i = start
    if i >= 1 and text[i - 1] == " ":
        i -= 1
    before = i >= 1 and bool(polish_mod.CJK_RE.match(text[i - 1]))
    j = end
    if j < len(text) and text[j] == " ":
        j += 1
    after = j < len(text) and bool(polish_mod.CJK_RE.match(text[j]))
    return before and after


def _short_fragment_context_match(fragment, fuzzy_vocabulary):
    """Prefix/suffix containment for a fragment below the ratio floor.
    Returns the single matching curated phrase, or None on zero or multiple
    candidates -- ambiguity is refused, never guessed (D1). See the module
    note above for why candidates are restricted to single-word, purely
    alphabetic curated terms."""
    if _is_common_english_word(fragment):
        return None
    fl = fragment.lower()
    hit = None
    for phrase in fuzzy_vocabulary:
        if " " in phrase or not phrase.isalpha():
            continue
        pl = phrase.lower()
        if len(pl) <= len(fl):
            continue
        if pl.startswith(fl) or pl.endswith(fl):
            if hit is not None and hit != phrase:
                return None
            hit = phrase
    return hit


def repair_latin_fragments(text, vocabulary, fuzzy_vocabulary=None,
                            min_len=REPAIR_MIN_FRAGMENT_LEN,
                            min_ratio=REPAIR_MIN_RATIO,
                            min_margin=REPAIR_MIN_MARGIN,
                            max_window=REPAIR_MAX_WINDOW_WORDS, log_fn=None):
    """D1-conservative post-ASR repair for embedded English/tech phrases
    SenseVoice mangles inside Cantonese speech (INCIDENT 2026-07-26).

    Operates on individual Latin WORD tokens, never on a whole contiguous
    Latin run. An earlier version matched (and replaced) the entire run as
    one unit; measured against the real ~/dictation/history.jsonl (1087 RAW
    entries) that silently DELETED adjacent real words whenever a run mixed
    ordinary English with one garbled/merged token -- e.g. "I always want"
    (three real, correctly-recognised words) became "always want", because
    the whole run fuzzy-matched a shorter vocabulary phrase and the word "I"
    was swallowed with it. That is exactly the kind of loss D1 forbids.

    Fix: slide a window of 1..max_window CONSECUTIVE Latin word tokens
    (tokens separated by nothing but a single space in the original text --
    a run broken by CJK, punctuation, or extra spacing never forms one
    window), trying the LONGEST window first at each position so a genuine
    multi-token garble ("c l a u d e" -> "Claude", "face me" -> "FaceTime",
    "chat g p t" -> "ChatGPT") is caught before its individual tokens are
    tried alone. A window is replaced ONLY when it is unambiguously close to
    exactly one vocabulary phrase (SequenceMatcher ratio on the collapsed
    forms >= min_ratio, beating the runner-up by >= min_margin) -- otherwise
    every token in it is left completely untouched, including tokens
    adjacent to a match that fell outside the matched window. An exact
    collapsed match (e.g. "super base" vs "Supabase") always replaces
    regardless of length, checked against the FULL `vocabulary`. D1: when
    uncertain, keep what the ASR produced.

    `fuzzy_vocabulary` (defaults to `vocabulary` if not given, for direct
    unit testing of the matching mechanism in isolation) scopes FUZZY
    matching specifically -- production wiring (main(), see build_
    recognizer()'s asr_raw()) always passes dictionary_only_repair_
    vocabulary()'s curated output here, NOT the full history-mined
    vocabulary. This is a measured safety boundary, not a style choice:
    fuzzy-matching against generic history-mined phrases produced repeated
    semantically-wrong corrections ("me for permission" -> "more
    permission", changing what Tinho actually said) whose similarity score
    was indistinguishable, by ratio, margin, OR edit distance, from the
    genuine win this feature exists for ("talkingken" -> "talking token").
    See _best_vocabulary_match()'s docstring for the full measurement.
    Exact matching has no such failure mode (identity is identity) and
    stays scoped to the full vocabulary regardless.

    This cannot recover a word the acoustic model never decoded at all
    (INCIDENT evidence: "back testing" -> "back" -- "testing" left no trace
    in the text). Only better upstream decoding could close that gap, and
    sherpa-onnx 1.13.4's SenseVoice recognizer has no hotwords/contextual-
    biasing hook to drive that with (see the investigation above
    build_recognizer()) -- this function is the fallback that investigation
    concluded was still worth shipping, in the safety-scoped form above.
    """
    fuzzy_vocabulary = vocabulary if fuzzy_vocabulary is None else fuzzy_vocabulary
    if not text or not vocabulary:
        return text
    if len(text) > REPAIR_MAX_TEXT_CHARS:
        return text

    words = list(_LATIN_WORD_RE.finditer(text))
    if not words:
        return text





    adjacent_to_next = [
        (words[i + 1].start() - words[i].end() == 1
         and text[words[i].end():words[i + 1].start()] == " ")
        for i in range(len(words) - 1)
    ]













    windows = {}
    consumed = set()
    i = 0
    while i < len(words):
        if i in consumed:
            i += 1
            continue
        matched = False
        max_reach = 1
        while (i + max_reach - 1 < len(words) - 1
               and max_reach < max_window
               and adjacent_to_next[i + max_reach - 1]):
            max_reach += 1
        for window in range(min(max_window, max_reach), 0, -1):
            if i + window > len(words):
                continue
            span = words[i:i + window]












            if window > 3 and not all(len(w.group(0)) == 1 for w in span):
                continue














            all_single_letter = all(len(w.group(0)) == 1 for w in span)
            if window > 1 and not all_single_letter and (
                    span[0].group(0).lower() in _REPAIR_BOUNDARY_STOPWORDS
                    or span[-1].group(0).lower() in _REPAIR_BOUNDARY_STOPWORDS):
                continue
            fragment = text[span[0].start():span[-1].end()]
            boundary_tokens = (
                (span[0].group(0), span[-1].group(0)) if window > 1 else None)
            claimed, replacement = _best_vocabulary_match(
                fragment, vocabulary, fuzzy_vocabulary, min_len, min_ratio,
                min_margin, boundary_tokens)






            if (not claimed and window == 1
                    and SHORT_FRAGMENT_MIN_LEN <= len(fragment) < min_len
                    and _flanked_by_cjk(text, span[0].start(), span[-1].end())):
                short_match = _short_fragment_context_match(
                    fragment, fuzzy_vocabulary)
                if short_match is not None:
                    claimed, replacement = True, short_match
            if claimed:
                if replacement is not None and log_fn:
                    log_fn(f"latin-repair: {fragment!r} -> {replacement!r} "
                           f"(window={window})")
                windows[i] = (i + window - 1, replacement)





                consumed.update(range(i + 1, i + window))
                i += window
                matched = True
                break
        if not matched:
            i += 1

    out = []
    pos = 0
    idx = 0
    n = len(words)
    while idx < n:
        if idx in windows:
            last_idx, replacement = windows[idx]
            start_word, end_word = words[idx], words[last_idx]
            out.append(text[pos:start_word.start()])




            out.append(replacement if replacement is not None
                        else text[start_word.start():end_word.end()])
            pos = end_word.end()
            idx = last_idx + 1
        else:
            idx += 1
    out.append(text[pos:])
    return "".join(out)


def guarded_call(fn, timeout, label, *args, **kwargs):
    """Run fn under an unconditional wall-clock ceiling (see the STUCK-LOCK
    AUTO-RECOVERY comment above SAFETY_MAX_SEGMENTS for the incident this
    exists to close). On timeout, logs the required
    `lock-recovered: aborted stuck transcription after Ns` line and returns
    (None, True) instead of hanging the caller — the caller then falls back
    to raw/punctuated text for that one take and, critically, still reaches
    its own `finally: request_lock.release()` on schedule.

    Module-level (not a main() closure) so it's independently unit-testable
    and shared verbatim by transcribe()/segment_worker()/retry_audio().
    Any exception fn raises before the deadline propagates normally — only
    a genuine timeout is turned into the (None, True) sentinel.
    """
    result, timed_out = polish_mod.run_with_hard_timeout(
        fn, timeout, *args, **kwargs)
    if timed_out:
        log(f"lock-recovered: aborted stuck transcription after "
            f"{timeout:.0f}s ({label}) — falling back for this take")
    return result, timed_out


def recover_portaudio_wedge(request_lock, recorder, log, reason):
    """#63 recovery pass: unblock both request and PortAudio stream locks."""
    findings = []
    recovered = request_lock.force_recover()
    if recovered is not None:
        msg = (f"lock-recovered: {reason} force-released stuck request_lock "
               f"after {recovered:.0f}s")
        log(msg)
        findings.append(msg)
    if recorder is not None and hasattr(recorder, "force_recover_stream"):
        try:
            stream_recovered, rebuilt = recorder.force_recover_stream()
            if stream_recovered is not None:
                msg = (f"lock-recovered: {reason} force-released stuck "
                       f"PortAudio lock after {stream_recovered:.0f}s")
                msg += " and rebuilt the stream" if rebuilt else " (rebuild failed)"
                log(msg)
                findings.append(msg)
        except Exception as exc:
            msg = f"PortAudio recovery failed ({reason}): {exc}"
            log(msg)
            findings.append(msg)
    return findings


def dispatch_request(data, request_lock, recorder, state, recognizer_for,
                     transcribe, retry_audio, seg_lock, seg_texts, log,
                     request_lock_timeout=REQUEST_LOCK_ACQUIRE_TIMEOUT):
    """#63 request dispatch with bounded lock acquisition."""
    if data == "PING":
        return "pong"
    lock, acquired = request_lock.acquire_current(timeout=request_lock_timeout)
    if not acquired:
        log("request_lock timeout — a prior transcription is stuck "
            f"(waited {request_lock_timeout:.0f}s; returning '{BUSY_REPLY}')")
        return BUSY_REPLY
    try:
        if data == "START":
            with seg_lock:
                seg_texts.clear()
            recorder.start()
            return "ok"
        if data == "STOP":
            return transcribe(recorder.stop(), recorder.dropouts)
        if data.startswith("LANG "):
            lang = data.split(None, 1)[1].strip()
            state["lang"] = lang
            recognizer_for(lang)
            return f"lang={lang}"
        if data.startswith("POLISH "):
            state["polish"] = data.split(None, 1)[1].strip() == "on"
            return f"polish={'on' if state['polish'] else 'off'}"
        if data.startswith("RETRY "):
            return retry_audio(data.split(None, 1)[1].strip())
        return "?"
    finally:
        request_lock.release_current(lock)











_portaudio_wedge_count = 0
_portaudio_wedge_count_lock = threading.Lock()
_portaudio_wedge_observer = None











LOW_GAIN_RMS_THRESHOLD = float(
    os.environ.get("DICTATION_LOW_GAIN_RMS_THRESHOLD", "0.05"))
INPUT_VOLUME_MIN = int(os.environ.get("DICTATION_INPUT_VOLUME_MIN", "50"))
INPUT_VOLUME_TARGET = int(os.environ.get("DICTATION_INPUT_VOLUME_TARGET", "85"))
INPUT_VOLUME_COOLDOWN_SECONDS = float(
    os.environ.get("DICTATION_INPUT_VOLUME_COOLDOWN_SECONDS", "600"))
_input_volume_last_raise = 0.0
_input_volume_lock = threading.Lock()

















CAPTURE_PROBE_WINDOW_SECONDS = float(
    os.environ.get("DICTATION_CAPTURE_PROBE_WINDOW_SECONDS", "1.0"))
CAPTURE_PROBE_MAX_BLOCKS = int(
    os.environ.get("DICTATION_CAPTURE_PROBE_MAX_BLOCKS", "512"))


CAPTURE_PROBE_MIN_SECONDS = float(
    os.environ.get("DICTATION_CAPTURE_PROBE_MIN_SECONDS", "0.2"))

























def capture_signal_state(recorder, window_seconds=None, max_blocks=None,
                         threshold=None, min_seconds=None):
    """Classify what the microphone has actually delivered so far.

    Returns ``(state, rms, peak, n_samples)`` where state is one of:

      ``none``   - not recording, or not one frame has arrived yet.  This is
                   the pre-existing dead-InputStream case and it keeps its
                   exact old meaning.
      ``silent`` - frames ARE arriving and every sample in the sampled window
                   is exactly 0.0.  This is the wedged-Bluetooth failure: the
                   stream is healthy at the PortAudio level and carries no
                   audio whatsoever.  A real microphone — including the
                   built-in one in a dead-quiet room — has a noise floor that
                   is never exactly zero, which is what makes this a proof
                   rather than a guess (same reasoning as
                   Recorder._stream_audio_seen).
      ``low``    - real, non-zero audio, but the LOUDEST sample in the window
                   is still below LOW_GAIN_RMS_THRESHOLD, so the window's rms
                   is necessarily below it too (rms <= peak): either he is not
                   speaking yet, or he is speaking far too quietly.  A single
                   ``low`` window cannot tell those apart and must never
                   produce an alert on its own — see the block comment above.
                   Sustained across several seconds it means the input is too
                   quiet, whose remedy (move closer / raise the input volume)
                   is completely different from ``silent``'s, so the client
                   must never collapse the two into one alert.
      ``ok``     - normal audio: something in this window is speech-loud.

    CONCURRENCY.  This runs on the PING lane while a take is in progress, so
    it takes NO lock at all: not ``request_lock`` (it exists precisely to
    answer while that lock is held by a STOP) and not ``recorder.lock``
    (``stop()`` holds that while it concatenates the whole take).  Every read
    here is a single attribute or a bounded slice of a list that only the
    audio callback appends to; the callback never mutates a block after
    appending it, and if it swaps ``self.frames`` wholesale at a segment
    boundary we simply keep the older list, which is still real recent audio.
    Nothing here can block, slow, or drop capture.
    """
    if window_seconds is None:
        window_seconds = CAPTURE_PROBE_WINDOW_SECONDS
    if max_blocks is None:
        max_blocks = CAPTURE_PROBE_MAX_BLOCKS
    if threshold is None:
        threshold = LOW_GAIN_RMS_THRESHOLD
    if min_seconds is None:
        min_seconds = CAPTURE_PROBE_MIN_SECONDS

    live = bool(getattr(recorder, "recording", False))
    total = int(getattr(recorder, "total", 0) or 0)


    if not live or total <= 0:
        return "none", 0.0, 0.0, 0
    frames = getattr(recorder, "frames", None) or []



    tail = frames[-max_blocks:] if max_blocks > 0 else list(frames)
    if not tail:
        return "none", 0.0, 0.0, 0
    rate = int(getattr(recorder, "capture_rate", 0) or 0) or 48000
    want = int(window_seconds * rate) if window_seconds > 0 else 0
    picked = []
    got = 0
    for block in reversed(tail):
        arr = np.asarray(block).reshape(-1)
        if arr.size == 0:
            continue
        picked.append(arr)
        got += arr.size
        if want and got >= want:
            break
    if not picked:
        return "none", 0.0, 0.0, 0
    picked.reverse()
    samples = picked[0] if len(picked) == 1 else np.concatenate(picked)
    if want and samples.size > want:
        samples = samples[-want:]
    if samples.size < max(1, int(min_seconds * rate)):
        return "none", 0.0, 0.0, int(samples.size)
    samples = samples.astype(np.float64, copy=False)
    peak = float(np.max(np.abs(samples)))
    rms = float(np.sqrt(np.mean(samples ** 2)))
    n = int(samples.size)
    if peak == 0.0:
        return "silent", rms, peak, n
    if peak < threshold:
        return "low", rms, peak, n
    return "ok", rms, peak, n


def capture_probe_reply(recorder):
    """Build the CAPTURING wire answer.

    WIRE COMPATIBILITY, both directions — this matters because server.py can
    go live on powerd's own restart cycle while ~/.hammerspoon has not been
    reloaded yet, and vice versa:

      * Field 1 is still the old bare ``yes``/``no``, so a client that has not
        been reloaded behaves EXACTLY as it did before this change.  In
        particular the "not one frame arrived" case still answers the bare
        string ``no``, byte for byte, which is the only value the old client
        ever alerted on.  The states that old clients had no alert for
        (silence, low gain) still do not trigger the old alert — no regression
        either way, just no new benefit until Hammerspoon is reloaded.
      * A reloaded client splits on TAB and acts on field 2.  Against an OLD
        server it receives a bare ``yes``/``no`` with no field 2 and falls
        straight back to the legacy behaviour.
      * The verb itself is unchanged (``CAPTURING``), so a new client never
        sends an unknown command to an old server — an unknown command would
        fall through to the request-lock path, which this probe must never
        touch.

    Fields: ``<yes|no>\\t<state>\\t<rms>\\t<peak>\\t<n_samples>``.
    """
    state, rms, peak, n = capture_signal_state(recorder)
    if state == "none":
        return "no"
    legacy = "no" if state == "silent" else "yes"
    return f"{legacy}\t{state}\t{rms:.5f}\t{peak:.5f}\t{n}"


def _maybe_raise_input_volume(reason):
    """Best-effort: read macOS's current input volume via osascript and, if
    it is below INPUT_VOLUME_MIN, raise it to INPUT_VOLUME_TARGET. Never
    raises — a failure here must not take down the take that triggered it.
    Cooldown-gated so a genuinely quiet room (Tinho deliberately lowered it)
    isn't fought every single take."""
    global _input_volume_last_raise
    import subprocess
    now = time.time()
    with _input_volume_lock:
        if now - _input_volume_last_raise < INPUT_VOLUME_COOLDOWN_SECONDS:
            return
        try:
            current = int(subprocess.run(
                ["osascript", "-e", "input volume of (get volume settings)"],
                capture_output=True, text=True, timeout=3
            ).stdout.strip())
        except Exception as exc:
            log(f"input-volume-heal: could not read current volume "
                f"(ignored): {exc}")
            return
        if current >= INPUT_VOLUME_MIN:
            return
        try:
            subprocess.run(
                ["osascript", "-e",
                 f"set volume input volume {INPUT_VOLUME_TARGET}"],
                capture_output=True, text=True, timeout=3, check=True)
            _input_volume_last_raise = now
            log(f"input-volume-heal: raised system input volume "
                f"{current} -> {INPUT_VOLUME_TARGET} (trigger: {reason})")
        except Exception as exc:
            log(f"input-volume-heal: could not raise volume (ignored): {exc}")































_ABANDONED_STREAMS = []
_abandoned_streams_lock = threading.Lock()


def _abandon_stream_forever(stream, why):
    """Park a stream PortAudio may still be calling back into. Never freed:
    the cffi callback closure it owns must outlive any in-flight
    AudioIOProc. Growth is bounded in practice by _note_portaudio_wedge()'s
    own escalation — a process wedging streams repeatedly is restarted long
    before this list is large."""
    if stream is None:
        return
    with _abandoned_streams_lock:
        _ABANDONED_STREAMS.append(stream)
        parked = len(_ABANDONED_STREAMS)
    log(f"portaudio-abandon: keeping a permanent reference to the {why} "
        f"stream so PortAudio can never invoke a freed callback closure "
        f"(parked={parked})")


def _note_portaudio_wedge(op):
    """Log + count a PortAudio call that hit HARD_PORTAUDIO_TIMEOUT without
    returning. Always emits the grep-able `portaudio-wedge:` marker line
    (count included) regardless of whether an observer is wired up."""
    global _portaudio_wedge_count
    with _portaudio_wedge_count_lock:
        _portaudio_wedge_count += 1
        count = _portaudio_wedge_count
    log(f"portaudio-wedge: stream.{op}() did not return within "
        f"{HARD_PORTAUDIO_TIMEOUT:.0f}s — abandoning the old stream "
        f"(leaked, never joined) and rebuilding a fresh InputStream "
        f"(count={count})")
    if _portaudio_wedge_observer is not None:
        try:
            _portaudio_wedge_observer(op)
        except Exception as exc:
            log(f"portaudio-wedge observer error (ignored): {exc}")


def _note_portaudio_rebuild_failure(attempt, will_retry):
    """Make a started-but-silent replacement observable to the watchdog.

    `InputStream.start()` has already returned at this point, so this is not
    a stop/start wedge.  It is nevertheless the same actionable device
    failure class: if the bounded retry also cannot receive a callback, the
    existing PortAudio watchdog's second event emits its low-priority ntfy
    alert instead of leaving the daemon silently unable to record.
    """
    outcome = ("forcing one PortAudio rescan retry" if will_retry
               else "MIC STREAM DEAD after the bounded retry")
    log(f"portaudio-rebuild-failed: {attempt} fresh InputStream produced no "
        f"callback within {PORTAUDIO_CALLBACK_VERIFY_TIMEOUT:.1f}s; {outcome}")
    if _portaudio_wedge_observer is not None:
        try:
            _portaudio_wedge_observer("callback")
        except Exception as exc:
            log(f"portaudio-rebuild-failed observer error (ignored): {exc}")


def _current_input_device_name():
    """Best-effort name of the input device PortAudio currently considers
    default. Purely for the log line below — never raises, never blocks the
    caller on a device query that misbehaves."""
    try:
        return str(sd.query_devices(kind="input").get("name", "?"))
    except Exception:
        return "?"












BUILTIN_MIC_NAME = "MacBook Air Microphone"










_VIRTUAL_INPUT_DEVICE_MARKERS = (
    "teams audio",
    "iphone microphone",
    "virtual",
    "loopback",
    "blackhole",
    "continuity camera",
)


def _is_virtual_input_device_name(name):
    lowered = str(name).lower()
    return any(marker in lowered for marker in _VIRTUAL_INPUT_DEVICE_MARKERS)


def _rank_fallback_input_devices(exclude_index=None):
    """Real PortAudio input devices, in fallback-preference order, for use
    after the default input has already proven silent (see
    Recorder._fallback_to_other_input_device).

    Reuses sd.query_devices()'s device table directly — the caller has
    already forced a PortAudio rescan (force_portaudio_reinit=True) before
    reaching this point, so no extra rescan or shelling out is needed here.

    Order:
      1. other real (non-virtual, non-built-in) input devices, in PortAudio
         index order — an intentionally-connected device (USB mic, etc.) is
         tried before quietly falling back to the laptop's own mic.
      2. the built-in mic (BUILTIN_MIC_NAME) — the last-resort known-good.
      3. virtual/loopback devices (_VIRTUAL_INPUT_DEVICE_MARKERS) — tried
         only if nothing else is left, since routing dictation through a
         virtual device would silently misdirect it.

    `exclude_index` drops the device already proven silent this pass (the
    caller's own default input) so it is never retried in the same fallback
    pass. Returns [] on any unexpected shape from sd.query_devices() (e.g.
    under a test double that doesn't model the full device list) rather than
    raising — a fallback that can't enumerate devices is simply a no-op, not
    a crash.
    """
    try:
        devices = sd.query_devices()
    except Exception:
        return []
    if not isinstance(devices, (list, tuple)):
        return []

    real, builtin, virtual = [], [], []
    for idx, entry in enumerate(devices):
        try:
            if not isinstance(entry, dict):
                continue
            if entry.get("max_input_channels", 0) <= 0:
                continue
            if idx == exclude_index:
                continue
            name = str(entry.get("name", ""))
        except Exception:
            continue
        if _is_virtual_input_device_name(name):
            virtual.append((idx, name))
        elif name.strip() == BUILTIN_MIC_NAME:
            builtin.append((idx, name))
        else:
            real.append((idx, name))
    return real + builtin + virtual


def _note_silent_stream(attempt, will_retry):
    """Make a started-but-DIGITALLY-SILENT stream observable (2026-07-29).

    Distinct from _note_portaudio_rebuild_failure above: there the stream
    produced no callback at all; here callbacks arrive perfectly on schedule
    and every sample in them is exactly 0.0. That is the AirPods/Bluetooth
    signature, and until now it passed every health check the daemon had —
    `stream.start()` returns, the callback-verification event fires (an
    all-zeros buffer sets it just as well as real audio), the warm-up window
    elapses, and the silent stream is kept as the live one indefinitely.
    The six `recording was silent` events in server.log — including an 8.5s
    take that was zeros end to end — were all invisible except as a failed
    dictation Tinho noticed by hand.

    Escalation rides the SAME muted channel the wedge watchdog already uses
    (_portaudio_wedge_observer -> watchdog.announce_portaudio_wedge ->
    changelog + fyi topic). Per D10a this must NEVER push to the urgent ntfy
    topic: a mic that already attempted its own rescan is not a stop-breach.
    """
    outcome = ("forcing one PortAudio rescan retry" if will_retry
               else "keeping the silent stream (a stream-less Recorder would "
                    "be worse) and escalating")
    log(f"portaudio-silent-stream: {attempt} fresh InputStream delivered "
        f"only digital silence (every sample exactly 0.0) within "
        f"{PORTAUDIO_AUDIO_VERIFY_TIMEOUT:.1f}s on input device "
        f"{_current_input_device_name()!r}; {outcome}")
    if _portaudio_wedge_observer is not None:
        try:
            _portaudio_wedge_observer("silence")
        except Exception as exc:
            log(f"portaudio-silent-stream observer error (ignored): {exc}")


def _trailing_quiet_samples(samples, rate, quiet_rms=QUIET_RMS,
                            block_seconds=0.01):
    """How many samples at the END of `samples` are part of a continuous
    quiet run (block-RMS < quiet_rms), scanning backward from the end.
    0 if the very last block already has real signal.

    2026-07-26 (trailing-filler hallucination root-cause fix — see
    StreamingTail.decode_once()'s docstring for why this exists): reuses
    QUIET_RMS/QUIET_RUN_SECONDS verbatim — the SAME threshold
    Recorder._callback already uses in production to decide a safe
    segment-cut pause — rather than inventing a new number for a
    superficially different purpose that is actually the same underlying
    question ("is this a real pause"). block_seconds is a scan-granularity
    parameter (how finely to walk backward), not a content-affecting
    threshold — 10ms is fine-grained enough that it cannot itself hide a
    syllable inside one block."""
    block = max(1, int(rate * block_seconds))
    n = samples.size
    quiet = 0
    i = n
    while i > 0:
        start = max(0, i - block)
        chunk = samples[start:i]
        rms = float(np.sqrt((chunk.astype(np.float64) ** 2).mean())) \
            if chunk.size else 0.0
        if rms >= quiet_rms:
            break
        quiet += chunk.size
        i = start
    return quiet


class StreamingTail:
    """Continuously decode the bounded live tail without touching an LLM.

    SLIDING-WINDOW DECODE (2026-07-26, FOURTH revision, live incident,
    十分嚴重, reported five times): every earlier revision this round argued
    about how long STOP may wait for the tail flush. All of them were the
    same mistake in different clothes — they assumed a meaningful amount of
    undecoded audio genuinely exists at STOP and then negotiated how to
    absorb it. It does not have to exist at all.

    ROOT CAUSE, confirmed by direct code + log inspection: decode_once()
    used to call self.snapshot_fn() (Recorder.tail_snapshot(), server.py) —
    the WHOLE bounded tail buffer since the last segment cut, up to
    SEGMENT_HARD_SECONDS (22.5s) — and re-decode it FROM SCRATCH on every
    call (sherpa_onnx.OfflineRecognizer has no incremental/streaming decode
    API — verified via dir(), documented in flush()'s docstring: create_
    stream/decode_stream/accept_waveform/result/get_option/set_option/has_
    option, nothing else). That is D71's exact shape (「每 15 秒只重寫嗰
    15 秒」's inverse: EVERY tick re-decoding the WHOLE growing window,
    not just its own new slice) hiding in the one path in front of Tinho —
    confirmed against server.log: both known-bad takes
    (20260726-091108.wav wall=8.2s, 20260726-091239.wav wall=13.3s) were
    "0 segments" takes, i.e. the ENTIRE take was one continuously-growing
    buffer, and the longer it grew the slower each tick's full re-decode
    got (asr=0.668s-0.675s for the ~13s cases vs. 0.193s average over 40
    mixed-length takes) — exactly the "longer takes lose the final word,
    short ones are fine" pattern Tinho reported from the start.

    FIX: decode ONLY the audio that is new since the last successful
    decode for this epoch, then STITCH it onto the previously decoded text
    using repair_bounded_seam() — the exact function and pattern D71's own
    PR #16 already proved for segment rewrites (每 15 秒只重寫嗰 15 秒),
    reused verbatim rather than reimplemented. Per-tick and per-STOP cost
    is now CONSTANT regardless of how long Tinho has been speaking; the
    residual undecoded audio at STOP is bounded by one decode interval
    (typically well under a second, self-pacing — see _run()), not by the
    whole take's length. This is what makes run-to-completion
    (StreamingTail.flush()) almost never load-bearing rather than the
    primary defense: there is almost nothing left to wait for.

    The decode window is anchored to the pause-bounded current utterance
    itself: Recorder already cuts the tail at a quiet gap, so the
    recognizer gets the real acoustic context it needs without a separate
    clock-based overlap number."""

    def __init__(self, snapshot_fn, decode_fn, cadence=TAIL_DECODE_SECONDS):
        self.snapshot_fn = snapshot_fn
        self.decode_fn = decode_fn
        self.cadence = cadence
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._stop = threading.Event()
        self._epoch = None
        self._latest = ""
        self._by_epoch = {}
        self._thread = None










        self._decode_lock = threading.Lock()
        self._decoded_epoch = None
        self._decoded_samples = 0
        self._text_so_far = ""















        self._flush_threads_outstanding = 0
        self._flush_count_lock = threading.Lock()

    def start(self):
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="streaming-tail-asr")
        self._thread.start()

    def wake(self):
        self._event.set()

    def _publish(self, epoch, hypothesis):
        with self._lock:
            if epoch != self._epoch:
                if self._epoch is not None:
                    self._by_epoch[self._epoch] = self._latest
                self._epoch = epoch
                self._latest = ""
            self._latest = hypothesis
            self._by_epoch[epoch] = hypothesis

    def hypothesis(self, epoch):
        with self._lock:
            if epoch == self._epoch:
                return self._latest
            return self._by_epoch.get(epoch, "")

    def decode_once(self, trim_trailing_quiet=True):
        """Decode the current utterance window and publish the latest
        whole-utterance hypothesis for the current epoch.

        trim_trailing_quiet (2026-07-26, trailing-filler-hallucination root
        cause): SenseVoice is an offline recognizer trained on complete
        utterances; handed a window that ENDS in non-speech (breath, mouth
        click, plain silence — exactly what a growing tail window very
        often does at an arbitrary decode tick), it does what such models
        do at a boundary with no more acoustic evidence: emit a plausible
        filler token. A blocklist-based text strip was tried and removed
        the same day this file was written (see polish.py's long removed-
        mechanism comment) — it worked BLIND, from text plus one energy
        number, after the model had already committed to a word, and
        turned out unable to tell a hallucinated "okay"/"I" from Tinho
        genuinely saying "okay"/"I" (measured 2 of 6 tokens were real-word
        false positives). This is the layer that comment itself named as
        correct instead: give the recognizer a cleaner boundary so it has
        nothing to hallucinate FROM, never delete what it already produced.

        SAFE BY CONSTRUCTION, not by threshold-tuning: trimming here never
        DISCARDS audio. It only decides not to decode the trailing quiet
        run *yet* — self._decoded_samples advances only up to the trim
        point, so the deferred tail is simply "new" again on the NEXT
        decode_once() call (more speech continuing, or genuine silence that
        stays quiet and keeps deferring harmlessly). Reuses QUIET_RMS /
        QUIET_RUN_SECONDS verbatim (Recorder._callback's own already-
        production-proven "is this a safe pause" signal — see
        _trailing_quiet_samples) rather than inventing a new threshold.

        The decode window is anchored to the current utterance start, which
        is already a real speech-pause boundary because Recorder only starts
        a fresh tail after a segment cut at a quiet gap. That gives the
        recognizer the actual acoustic context it needs without inventing a
        separate timing window, and means each decode result is the
        authoritative hypothesis for the current window rather than a text
        seam that needs to be stitched onto prior output.

        trim_trailing_quiet=False (flush() uses this) decodes everything
        captured, unconditionally — D1: at STOP there is no "next call" to
        defer to, so deferring here would mean never decoding it at all.
        The one place this fix does NOT protect is a take that happens to
        end in genuine trailing silence right at STOP; that is a smaller,
        rarer window than the one this fix removes (every ordinary mid-
        recording tick), and D1 (never lose a word) outranks eliminating
        100% of a cosmetic artifact.

        Returns True if a decode actually ran (there was >= 0.25s of NEW,
        post-trim tail audio to decode since last time), False if it
        early-exited on too little (the ordinary idle/just-caught-up/still-
        quiet state). The return value is what lets _run() below pace
        itself by REAL WORK rather than a fixed clock — see its docstring.
        The same 0.25s (rate // 4) threshold already used elsewhere in this
        file for "is there enough audio to bother" naturally caps how tiny
        a decoded sliver can be — reused, not a new number — and, combined
        with _run()'s back-to-back loop, naturally throttles decode
        attempts to roughly 4/s during active recording without a separate
        invented cadence."""
        with self._decode_lock:
            epoch, samples, rate = self.snapshot_fn()
            if epoch != self._decoded_epoch:


                self._decoded_epoch = epoch
                self._decoded_samples = 0
                self._text_so_far = ""
            new_samples = samples[self._decoded_samples:]
            decode_samples = samples
            if trim_trailing_quiet and new_samples.size > 0:
                quiet = _trailing_quiet_samples(new_samples, rate)
                if quiet >= QUIET_RUN_SECONDS * rate:
                    decode_samples = decode_samples[:decode_samples.size - quiet]
            if decode_samples.size < rate // 4 or decode_samples.size <= self._decoded_samples:
                return False
            new_text = self.decode_fn(decode_samples, rate) or ""
            if new_text:
                self._text_so_far = new_text
            self._decoded_samples = decode_samples.size
            self._publish(epoch, self._text_so_far)
        return True

    def flush(self, timeout=HARD_ASR_TIMEOUT):
        """Wait for one last update to land — RUNS TO COMPLETION; `timeout`
        is a pathological-hang backstop, not a latency budget.

        Returns True if decode_once() actually finished and published
        (so hypothesis() below is reading the take's REAL final result),
        False only if `timeout` itself elapsed first.

        HISTORY (2026-07-26, THIRD revision, live incident): rev 1 used a
        budget-derived timeout that could hit 0.0s (INCIDENT 2026-07-25).
        Rev 2 added a fixed 0.6s floor, derived from a sample whose max was
        0.437s — live data disproved it within a day (asr=0.668s/0.675s on
        two real takes, both tail_decode_complete=False:
        20260726-091108.wav, 20260726-091239.wav). Tinho's ruling, stated
        for this exact case: 「我是不喜歡硬性數字的」 — no fixed latency
        number will ever be safe, because there will always be a real
        decode slower than whatever was measured. So this rev removes the
        latency bound entirely: decoding audio Tinho has ALREADY spoken
        must never be cut off just because it is taking a while.

        `timeout` therefore exists ONLY to catch a decoder that is
        genuinely STUCK, not merely slow — and default is HARD_ASR_TIMEOUT
        (the repo's existing pathological-ASR-hang constant, reused
        deliberately rather than inventing a new one; see its own
        definition). Tinho asked, correctly, whether even THIS bound can
        avoid being a number, by watching for LACK OF PROGRESS instead of
        elapsed time — a hung decoder produces nothing, a merely-slow one
        keeps producing. Investigated against the actual API in use here
        (`sherpa_onnx.OfflineRecognizer`/`OfflineStream`, verified via
        `dir()`: create_stream, decode_stream, decode_streams,
        accept_waveform, get_option, has_option, result, set_option) —
        `decode_stream()` is ONE opaque blocking native call with no
        progress callback, partial-result hook, or monotonic counter
        exposed to Python during decoding; `stream.result` is populated
        only once decode_stream() returns. There is nothing to observe
        mid-decode. That is a property of this offline (batch, not
        streaming) recognizer, not a gap in how server.py calls it — a
        genuinely different recognizer family (an online/streaming one)
        would expose incremental state, but swapping recognizer families
        is a materially different, unverified change, not attempted here.
        So this falls back exactly as instructed: a clock-based guard,
        explicitly a hang guard of last resort, sized far above any
        plausible real decode (HARD_ASR_TIMEOUT=20s vs. observed real
        decodes under 1s even for a 13s+ take) rather than anywhere near
        it — the opposite of how the retired 0.6s floor was sized.

        See StreamingTail._run() for the actual fix to the backlog that
        made this bound matter at all in the first place: this backstop is
        now meant to be almost never load-bearing, not the primary
        defense.

        Calls decode_once(trim_trailing_quiet=False): D1 — at STOP there is
        no future decode_once() call to defer a trimmed tail to, so this
        one must decode everything captured, unconditionally. See
        decode_once()'s docstring."""
        with self._flush_count_lock:
            if self._flush_threads_outstanding >= FLUSH_THREAD_CEILING:
                log(f"tail-stop-flush: {self._flush_threads_outstanding} "
                    f"already outstanding (ceiling {FLUSH_THREAD_CEILING}) "
                    "-- not spawning another, same as a timed-out flush")
                return False
            self._flush_threads_outstanding += 1

        done = threading.Event()

        def work():
            try:
                self.decode_once(trim_trailing_quiet=False)
            finally:
                done.set()
                with self._flush_count_lock:
                    self._flush_threads_outstanding -= 1

        threading.Thread(target=work, daemon=True, name="tail-stop-flush").start()
        return done.wait(max(0.0, timeout))

    def _run(self):
        """Decode BACK-TO-BACK with no added wait whenever there is real
        tail audio to decode; fall back to the idle poll (`cadence`) only
        when there genuinely was nothing to decode this pass.

        ROOT CAUSE FIX (2026-07-26, live incident): decode_once() re-decodes
        the WHOLE current-segment tail buffer every call (offline recognizer,
        no incremental/streaming decode API — see flush()'s docstring), so
        its cost GROWS as the tail grows within a segment window. The OLD
        loop (`wait(cadence); decode_once()`, serial) stacked a FIXED extra
        wait on top of that growing cost every single cycle: as decode got
        slower, the gap between successive published results grew too,
        which let MORE new audio accumulate before the next attempt, which
        made THAT decode slower still — a compounding backlog, confirmed
        against server.log for the two known-bad takes (20260726-091108.wav,
        wall=8.2s, 0 segments — i.e. the ENTIRE take was one continuously
        growing tail buffer the whole time; 20260726-091239.wav, wall=13.3s,
        same shape) and against the take immediately following the first
        incident (20260726-091116.wav, wall=1.3s but asr=0.529s — far slower
        than a 1.3s clip should take, consistent with its decode_once() call
        blocking on asr_lock behind the PRIOR take's abandoned, still-
        running flush() thread; asr_raw()'s lock is shared between the tail
        and segment_worker's background ASR).

        Removing the fixed inter-decode wait (replaced by "wait only when
        idle") turns this from a vicious cycle into a self-correcting one:
        more frequent decoding means less new audio accumulates per call,
        which keeps each call's own cost down, which allows even more
        frequent decoding. It does not require the recognizer to support
        true incremental decoding (a materially larger, unverified change)
        and it does not touch asr_lock's scope (the sherpa recognizer is
        not documented thread-safe, so serializing tail/segment ASR is left
        alone rather than risking a same-night, unverified concurrency
        change) — both flagged as follow-up, not attempted here.

        `cadence` (TAIL_DECODE_SECONDS) is now purely an idle-poll interval
        — how often to check whether recording has resumed — never a bound
        on how promptly a REAL, in-progress take gets decoded."""
        while not self._stop.is_set():
            try:
                decoded = self.decode_once()
            except Exception as exc:
                log(f"streaming tail ASR error (ignored): {exc}")
                decoded = False
            if not decoded:
                self._event.wait(self.cadence)
                self._event.clear()


class CoalescingRewriteWorker:
    """Run at most one rewrite, with at most one aggregate waiting behind it.

    ``submit`` never creates an individual rewrite job.  Items arriving while
    ``process_batch`` is in flight accumulate in ``_next_batch`` and are
    claimed together by the next call.  Queue depth is therefore bounded at
    one active plus one pending rewrite regardless of segment count.
    """

    def __init__(self, process_batch):
        self.process_batch = process_batch
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._next_batch = []
        self._discarded_batches = []
        self._active = False
        self._active_batch = None
        self._active_done = None
        self._thread = None

    def start(self):
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="coalesced-segment-rewrite")
        self._thread.start()

    def submit(self, item):
        with self._lock:
            self._next_batch.append(item)
            self._event.set()

    def clear_pending(self):
        """Discard only work that has not started; never wait for active work."""
        with self._lock:
            pending = self._next_batch
            count = len(pending)
            self._next_batch = []
            if pending:



                self._discarded_batches.append(pending)
                self._event.set()
            return count

    def queue_depth(self):
        """Number of rewrite calls represented, not number of merged segments."""
        with self._lock:
            return int(self._active) + int(bool(self._next_batch))

    def snapshot_active(self):
        """Return (batch, done_event) for whatever is ACTIVE right now, or
        (None, None) if the worker is idle.

        D1 GRACE FIX (item 2, 2026-07-26): STOP needs to know, without
        waiting, whether a full rewrite is already in flight and — if so —
        get a stable handle to be notified when THAT SPECIFIC batch finishes,
        so a bounded background watcher can let it land instead of the old
        behaviour (cancel_inflight() killing it outright, throwing away GPU
        work already paid for). ``done_event`` is a fresh Event created only
        for this batch (see _run below): a caller holding a reference to it
        is immune to confusion from a LATER batch reusing the same worker —
        that later batch gets its own new Event, this one only ever fires
        for the batch it was handed out for.
        """
        with self._lock:
            if not self._active:
                return None, None
            return self._active_batch, self._active_done

    def _run(self):
        while True:
            self._event.wait()
            with self._lock:
                if not self._next_batch:
                    discarded = self._discarded_batches
                    self._discarded_batches = []
                    self._event.clear()
                    batch = None
                else:
                    discarded = self._discarded_batches
                    self._discarded_batches = []
                    batch = self._next_batch
                    self._next_batch = []
                    self._active = True
                    self._active_batch = batch
                    self._active_done = threading.Event()
                    self._event.clear()


            discarded.clear()
            if batch is None:
                continue
            done_event = self._active_done
            try:
                self.process_batch(batch)
            except Exception as exc:
                log(f"coalesced segment rewrite error: {exc}")
            finally:
                with self._lock:
                    self._active = False
                    self._active_batch = None
                    if self._next_batch:
                        self._event.set()





                done_event.set()


def bounded_rewrite_input(raw, input_cap=REWRITE_INPUT_CHAR_CAP):
    """Return this interval's own new text, capped to a constant size.

    D71: bytes sent to the rewrite model per interval must stay constant
    regardless of how long the recording has run — never proportional to
    session length. There is no history/join argument here on purpose:
    polish() (see polish.py) takes no such parameter, so a "previous tail"
    value would never actually reach the model — it would be decorative.
    Overflow beyond the cap is still appended verbatim by the caller (see
    rewrite_segment_batch) as the lossless D1 fallback; the cap bounds GPU
    work, never content.
    """
    return raw[:input_cap]


def repair_bounded_seam(previous, current, seam_cap=SEAM_CHAR_CAP):
    """Repair only the bounded join; bytes outside it are never touched."""
    if not previous or not current:
        return previous, current
    prefix, left = previous[:-seam_cap], previous[-seam_cap:]
    right, suffix = current[:seam_cap], current[seam_cap:]








    stripped_right = right.lstrip()
    if left[-1:].isalpha() and left[-1:].isascii() \
            and stripped_right != right \
            and stripped_right[:1].isalpha() and stripped_right[:1].isascii():
        right = stripped_right

    punctuation = "，。！？；：,.!?;:"
    if left[-1:] in punctuation and right[:1] == left[-1:]:
        right = right[1:]

    if left.endswith("-") and right.startswith("-") \
            and left[-2:-1].isascii() and right[1:2].isascii():
        left = left[:-1]
        right = right[1:]
    return prefix + left, right + suffix
























_SEAM_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:'[A-Za-z0-9]+)*|[^\sA-Za-z0-9]")
SEAM_DEDUP_MAX_WINDOW = 8


def dedupe_seam_words(previous, current, max_window=SEAM_DEDUP_MAX_WINDOW):
    """Remove the duplicate word(s)/character(s) two adjacent segments both
    recognised from the shared SEGMENT_OVERLAP_SECONDS of audio at their
    cut. Returns (previous, current) with `current`'s (or, in the fragment
    case, `previous`'s) duplicate content stripped; an exact-silence cut
    with no real overlap content passes through unchanged, matching
    today's behaviour."""
    if not previous or not current:
        return previous, current
    prev_tokens = list(_SEAM_TOKEN_RE.finditer(previous))
    cur_tokens = list(_SEAM_TOKEN_RE.finditer(current))
    if not prev_tokens or not cur_tokens:
        return previous, current

    def norm(match):
        return match.group(0).lower()



    limit = min(max_window, len(prev_tokens), len(cur_tokens))
    for k in range(limit, 0, -1):
        if [norm(t) for t in prev_tokens[-k:]] == [norm(t) for t in cur_tokens[:k]]:
            current = current[cur_tokens[k - 1].end():].lstrip()
            return previous, current






    last_tok, first_tok = prev_tokens[-1], cur_tokens[0]
    last_s, first_s = norm(last_tok), norm(first_tok)
    if (last_s.isalpha() and first_s.isalpha()
            and len(last_s) < len(first_s) and first_s.startswith(last_s)):
        previous = previous[:last_tok.start()].rstrip()
        return previous, current

    return previous, current


def join_with_seam_dedup(fragments):
    """Space-join segment/tail text fragments in order, applying
    dedupe_seam_words() at every adjacent pair — the drop-in replacement for
    the plain `" ".join(f for f in fragments if f and f.strip())` this
    codebase used before the 2026-08-19 segment-boundary word-loss fix.
    Empty/whitespace-only fragments are dropped, same as before."""
    parts = [f.strip() for f in fragments if f and f.strip()]
    if not parts:
        return ""
    result = parts[0]
    for frag in parts[1:]:
        result, frag = dedupe_seam_words(result, frag)
        result = f"{result} {frag}".strip() if frag else result
    return result







SEGMENT_REWRITE_MAX_LOAD = float(
    os.environ.get("DICTATION_SEGMENT_REWRITE_MAX_LOAD", "4.0"))


def _load1():
    """1-minute load average, 0.0 where the platform has none. Factored out
    of perform_segment_rewrite so every load-gated path reads it the same way
    and so tests can inject a load without touching the real machine (the
    old inline os.getloadavg() made these tests silently load-dependent:
    they only passed on an idle box)."""
    try:
        return os.getloadavg()[0]
    except OSError:
        return 0.0


def perform_segment_rewrite(batch, rewrite_state, seg_lock, delivered_segment_ids,
                            recorder, polish_enabled, polish_fn,
                            guarded_call_fn=guarded_call,
                            timeout=None, log_fn=log,
                            load_fn=None, defer_fn=None):
    """Rewrite only this batch's own new text (D71); coalesce only when an
    earlier call overran its interval.

    Module-level and every collaborator passed in explicitly (recorder,
    polish, locks, the delivered-ids set) so this — the actual wired path
    the background rewrite worker calls — is unit-testable without spinning
    up main()'s full server, matching wait_for_segments_settled /
    snapshot_live_segments above.
    """
    if not recorder.recording or not polish_enabled:
        return
































    load1 = (load_fn or _load1)()
    if SEGMENT_REWRITE_MAX_LOAD > 0 and load1 > SEGMENT_REWRITE_MAX_LOAD:
        if defer_fn is None:
            log_fn(f"segment rewrite skipped: 1-min load {load1:.2f} > "
                   f"{SEGMENT_REWRITE_MAX_LOAD:.2f} — dictation capture has "
                   f"priority (D25); segment keeps its D1 text")
            return
        depth = defer_fn(batch)
        log_fn(f"segment rewrite deferred: 1-min load {load1:.2f} > "
               f"{SEGMENT_REWRITE_MAX_LOAD:.2f} — dictation capture has "
               f"priority (D25); queued for a later rewrite "
               f"({depth} in queue)")
        return
    raw = "\n\n".join(item["raw"] for item in batch if item["raw"])
    if not raw:
        return
    previous_entry = rewrite_state["previous_entry"]
    new_text = bounded_rewrite_input(raw)
    t0 = time.time()
    result, timed_out = guarded_call_fn(
        polish_fn, timeout,
        "sliding-window background segment rewrite", new_text,
        use_llm=True, full=True, cancellable=True)
    if timed_out or not result:
        return


    polished = result[0] + raw[len(new_text):]
    with seg_lock:
        if previous_entry is not None \
                and previous_entry["segment_id"] not in delivered_segment_ids:
            previous_entry["text"], polished = repair_bounded_seam(
                previous_entry["text"], polished)
        batch[0]["entry"]["text"] = polished
        for item in batch[1:]:
            item["entry"]["text"] = ""
        rewrite_state["previous_entry"] = batch[0]["entry"]
    log_fn(f"sliding-window segment rewrite: segments={len(batch)} "
        f"input_chars={len(new_text)} polish={time.time()-t0:.1f}s")


def apply_grace_rewrite_to_parts(parts, batch, head_text=None):
    """Item 2 (2026-07-26, "STOP throws away GPU work it already paid
    for"): once a grace-bounded background rewrite lands (see
    STOP_REWRITE_GRACE_SECONDS / CoalescingRewriteWorker.snapshot_active()),
    compute what THAT TAKE's history.jsonl record should say instead of its
    original assembly — WITHOUT mutating `parts` (the already-pasted
    snapshot) and without assuming the rewrite belongs to this take at all.

    `batch` is the exact list snapshot_active() handed back at STOP time —
    each item is `{"entry": entry, "raw": raw}`, where `entry` is the SAME
    mutable dict object perform_segment_rewrite() writes `text` into on
    completion (batch[0]'s entry gets the polished text; batch[1:]'s
    entries are absorbed and their own `text` cleared — the exact
    coalescing perform_segment_rewrite() itself performs, mirrored here
    read-only for history purposes only).

    Returns a NEW list of parts with the rewrite folded in, or None if
    NONE of the batch's segment_ids appear in `parts` at all — e.g. the
    rewrite belonged to a PREVIOUS take that was still in flight when this
    take's STOP fired, or a straggler for a take that hasn't reached this
    watcher yet. That guard is what makes it safe to call from a bounded
    background thread days after `parts` was captured: a wrong-take
    rewrite is silently ignored rather than corrupting an unrelated take's
    history record.
    """
    part_ids = {p["segment_id"] for p in parts}
    batch_ids = [item["entry"]["segment_id"] for item in batch]
    if not any(seg_id in part_ids for seg_id in batch_ids):
        return None
    head_id = batch_ids[0]
    absorbed_ids = set(batch_ids[1:])




    if head_text is None:
        head_text = batch[0]["entry"].get("text", "")
    merged = []
    for part in parts:
        if part["segment_id"] in absorbed_ids:
            continue
        if part["segment_id"] == head_id:
            part = dict(part)
            part["text"] = head_text
        merged.append(part)
    return merged


def watch_grace_rewrite(active_batch, active_done, parts, tail_text,
                         original_text, audio_name,
                         grace_seconds=None, log_fn=log,
                         cancel_inflight_fn=None):
    """Item 2 background watcher (2026-07-26): give an already-ACTIVE
    background segment rewrite up to `grace_seconds` to land after STOP has
    already returned, instead of the old behaviour of killing it outright.

    STOP itself never waits on this — transcribe() spawns this as a daemon
    thread and returns immediately, exactly as before the fix.

    If the rewrite lands within the grace window, its polished text is
    folded into a NEW history.jsonl correction record via
    log_history_correction() — NEVER back into the text already pasted
    (see that function's docstring for the D1/Tinho ruling). If the grace
    window closes first, the rewrite is cancelled exactly as STOP used to
    do immediately, just later — bounding how long a NEW take's own
    segment rewrites can queue behind this one on the single coalescing
    worker to `grace_seconds`, not "however long the generation takes".

    apply_grace_rewrite_to_parts() already guards against a rewrite that
    belongs to a DIFFERENT (earlier or later) take; this function adds no
    further identity check beyond that.
    """
    grace_seconds = STOP_REWRITE_GRACE_SECONDS if grace_seconds is None else grace_seconds
    cancel_inflight_fn = cancel_inflight_fn or polish_mod.cancel_inflight
    landed = active_done.wait(grace_seconds)
    if not landed:
        try:
            cancel_inflight_fn()
        except Exception as exc:
            log_fn(f"grace-rewrite: cancel_inflight error (ignored): {exc}")
        return
    if not audio_name:
        return
    try:
        merged_parts = apply_grace_rewrite_to_parts(parts, active_batch)
        if merged_parts is None:
            return
        joined = join_with_seam_dedup(
            [p["text"] for p in merged_parts] + [tail_text])
        corrected = paragraph_mod.paragraph_break(joined) if joined else joined
        if corrected and corrected != original_text:
            log_history_correction(audio_name, corrected)
            log_fn(f"grace-rewrite: landed within {grace_seconds:.0f}s, "
                   f"wrote history correction for {audio_name}")
    except Exception as exc:
        log_fn(f"grace-rewrite: apply error (ignored): {exc}")











































DEFERRED_REWRITE_QUEUE_CAP = int(
    os.environ.get("DICTATION_DEFERRED_REWRITE_QUEUE_CAP", "12"))





DEFERRED_REWRITE_IDLE_SECONDS = float(
    os.environ.get("DICTATION_DEFERRED_REWRITE_IDLE_SECONDS", "60"))
DEFERRED_REWRITE_POLL_SECONDS = float(
    os.environ.get("DICTATION_DEFERRED_REWRITE_POLL_SECONDS", "15"))



DEFERRED_REWRITE_MAX_AGE_SECONDS = float(
    os.environ.get("DICTATION_DEFERRED_REWRITE_MAX_AGE_SECONDS", "3600"))





DEFERRED_REWRITE_YIELD_POLL_SECONDS = float(
    os.environ.get("DICTATION_DEFERRED_REWRITE_YIELD_POLL_SECONDS", "0.05"))


def _join_take_text(part_texts, tail_text):
    """Join a take's segment texts plus its tail exactly the way this file's
    own STOP/grace paths do. Resolved through globals() on purpose: the
    seam-dedup join (join_with_seam_dedup, 2026-08-19 segment-boundary
    word-loss fix) is landing on a separate branch, so this helper is
    byte-identical in both revisions and upgrades itself once that merges,
    instead of forcing a conflict or a divergent mirror."""
    fragments = list(part_texts) + [tail_text]
    joiner = globals().get("join_with_seam_dedup")
    if joiner is not None:
        return joiner(fragments)
    return " ".join(s.strip() for s in fragments if s and s.strip())


class DeferredRewriteQueue:
    """Bounded hold-pen for segment rewrites the load gate turned away.

    Two-phase on purpose. `defer(batch)` is called from inside the rewrite
    worker DURING a recording, when the take has no identity yet (its audio
    filename is only created at STOP, see transcribe()). `bind_take()` is
    called from STOP once `parts`/`tail_text`/`text`/`audio_name` all exist,
    and attaches that context to every queued batch belonging to the take —
    reusing the same segment_id-intersection identity test
    apply_grace_rewrite_to_parts() already uses, so a batch that belongs to
    a different take is never mis-attributed.

    Only bound items are drainable. An item that never gets bound (its take
    crashed, or STOP produced no parts) simply ages out — it is never run
    against a guessed take.
    """

    def __init__(self, cap=None, max_age_seconds=None, log_fn=log,
                 now_fn=time.time):
        self.cap = DEFERRED_REWRITE_QUEUE_CAP if cap is None else cap
        self.max_age_seconds = (DEFERRED_REWRITE_MAX_AGE_SECONDS
                                if max_age_seconds is None else max_age_seconds)
        self._log = log_fn
        self._now = now_fn
        self._lock = threading.Lock()
        self._items = []

    def defer(self, batch):
        """Queue one gated rewrite call. Returns the resulting queue depth.

        Bounded: beyond `cap`, the OLDEST entry is dropped, so a long noisy
        stretch can never grow this without limit (D71). Dropping the oldest
        rather than refusing the newest is deliberate — the newest segments
        are the ones Tinho reported as worst ("towards the end").
        """
        with self._lock:
            self._items.append({"batch": batch, "queued_at": self._now(),
                                "take": None})
            dropped = 0
            while len(self._items) > self.cap:
                self._items.pop(0)
                dropped += 1
            depth = len(self._items)
        if dropped:
            self._log(f"deferred segment rewrite queue full (cap={self.cap}): "
                      f"dropped {dropped} oldest entry(ies); those segments "
                      f"keep their D1 text")
        return depth

    def bind_take(self, parts, tail_text, original_text, audio_name):
        """Attach this take's served context to its own queued batches.

        Returns how many queued batches were bound. All batches of one take
        share ONE take dict by identity, so a second deferred rewrite for
        the same take compounds on the first one's result instead of
        silently reverting it (see drain_deferred_rewrites).
        """
        if not parts or not audio_name:
            return 0
        part_ids = {p["segment_id"] for p in parts}
        take = {"parts": list(parts), "tail_text": tail_text or "",
                "original_text": original_text, "audio_name": audio_name}
        bound = 0
        with self._lock:
            for item in self._items:
                if item["take"] is not None:
                    continue
                batch_ids = [b["entry"]["segment_id"] for b in item["batch"]]
                if any(seg_id in part_ids for seg_id in batch_ids):
                    item["take"] = take
                    bound += 1
        return bound

    def prune(self):
        """Drop entries older than max_age_seconds. Returns how many went."""
        cutoff = self._now() - self.max_age_seconds
        with self._lock:
            keep = [i for i in self._items if i["queued_at"] >= cutoff]
            dropped = len(self._items) - len(keep)
            self._items = keep
        if dropped:
            self._log(f"deferred segment rewrite: dropped {dropped} entry(ies) "
                      f"older than {self.max_age_seconds:.0f}s unrun")
        return dropped

    def requeue(self, item):
        """Put a claimed item BACK after a capture-yield. Returns True if it
        was re-queued, False if the queue was full and it was dropped.

        Keeps the item's ORIGINAL `queued_at`, so a rewrite that keeps
        losing the race to Tinho's microphone still ages out on its own
        clock (DEFERRED_REWRITE_MAX_AGE_SECONDS) instead of being renewed
        forever by its own retries. Re-inserted at the front (its rightful
        oldest-first position) so the next quiet tick tries it again.

        Dropping when full is deliberate and matches defer()'s cap rule:
        losing ONE deferred rewrite is acceptable and preferred (D25) —
        those segments simply keep their D1 text.
        """
        with self._lock:
            if len(self._items) >= self.cap:
                return False
            self._items.insert(0, item)
            return True

    def claim_ready(self):
        """Pop and return the oldest BOUND entry, or None."""
        with self._lock:
            for index, item in enumerate(self._items):
                if item["take"] is not None:
                    return self._items.pop(index)
        return None

    def depth(self):
        with self._lock:
            return len(self._items)

    def ready_depth(self):
        with self._lock:
            return sum(1 for i in self._items if i["take"] is not None)


def _cancel_rewrite_if_recording_starts(
        done_event, recording_active_fn, cancel_inflight_fn, log_fn=log,
        poll_seconds=DEFERRED_REWRITE_YIELD_POLL_SECONDS, yielded_event=None):
    """Watch a deferred rewrite and kill it the moment Tinho touches the key.

    The drain below only starts when the machine has been quiet for
    DEFERRED_REWRITE_IDLE_SECONDS, but a rewrite costs seconds of CPU/GPU
    and he can press the key at any point inside that window. D25 does not
    end at the decision to start: capture outranks this pass mid-flight too,
    which is exactly what the PortAudio leak ratchet punishes if it does not.

    `recording_active_fn` is recording_gate.recording_active, which server.py's
    handle() sets for BOTH START and STOP (see `foreground_work` there), so
    this fires on either edge of the key — press or release.

    yielded_event (2026-08-21): set BEFORE the cancel, and never cleared. The
    cancel only aborts the ollama generation registered at that instant;
    polish()'s classify fallback would then start a fresh, uncancellable one
    (measured worst case 5.3s) right on top of his STOP. The drain hands this
    event to polish() as `should_abort`, so the yield stops the WORK, not
    just the current socket. It is also how the drain knows to re-queue the
    item instead of writing a half-finished correction.
    """
    while not done_event.wait(poll_seconds):
        try:
            active = recording_active_fn()
        except Exception:
            active = False
        if active:



            if yielded_event is not None:
                yielded_event.set()
            try:
                cancel_inflight_fn()
            except Exception as exc:
                log_fn(f"deferred segment rewrite: cancel error (ignored): {exc}")
            log_fn("deferred segment rewrite yielded: capture started — "
                   "dictation capture has priority (D25)")
            return


def drain_deferred_rewrites(queue, polish_fn, guarded_call_fn=guarded_call,
                            timeout=None, load_fn=None, busy_fn=None,
                            correction_fn=None, cancel_inflight_fn=None,
                            recording_active_fn=None, paragraph_fn=None,
                            log_fn=log, now_fn=time.time,
                            max_load=None):
    """Run AT MOST ONE queued rewrite, and only on a genuinely quiet machine.

    Returns a short status string ("empty" / "busy" / "load" / "unbound" /
    "yielded" / "timeout" / "unchanged" / "landed") so the policy is
    unit-testable without a mic, a model or a real load average.

    One per call is the D71 bound: the caller ticks on a fixed interval, so
    the cost of draining a 12-deep backlog is spread over 12 ticks rather
    than becoming one long GPU burst next to the microphone.
    """
    load_fn = load_fn or _load1
    busy_fn = busy_fn or (lambda: recording_gate.busy(
        hold_seconds=DEFERRED_REWRITE_IDLE_SECONDS))
    correction_fn = correction_fn or log_history_correction
    cancel_inflight_fn = cancel_inflight_fn or polish_mod.cancel_inflight
    recording_active_fn = (recording_active_fn
                           or recording_gate.recording_active.is_set)
    paragraph_fn = paragraph_fn or paragraph_mod.paragraph_break
    max_load = SEGMENT_REWRITE_MAX_LOAD if max_load is None else max_load

    queue.prune()
    if not queue.ready_depth():
        return "empty"


    if busy_fn():
        return "busy"
    load1 = load_fn()
    if max_load > 0 and load1 > max_load:
        return "load"
    item = queue.claim_ready()
    if item is None:
        return "unbound"
    batch, take = item["batch"], item["take"]
    raw = "\n\n".join(b["raw"] for b in batch if b["raw"])
    if not raw:
        return "unchanged"
    new_text = bounded_rewrite_input(raw)
    done = threading.Event()
    yielded = threading.Event()
    threading.Thread(
        target=_cancel_rewrite_if_recording_starts,
        args=(done, recording_active_fn, cancel_inflight_fn, log_fn,
              DEFERRED_REWRITE_YIELD_POLL_SECONDS, yielded),
        daemon=True, name="deferred-rewrite-capture-guard").start()
    t0 = now_fn()
    try:
        result, timed_out = guarded_call_fn(
            polish_fn, timeout, "deferred segment rewrite", new_text,
            use_llm=True, full=True, cancellable=True,
            should_abort=yielded.is_set)
    finally:
        done.set()










    if yielded.is_set():
        kept = queue.requeue(item)
        log_fn(f"deferred segment rewrite yielded to capture: "
               f"segments={len(batch)} after {now_fn()-t0:.2f}s — "
               + ("re-queued for a later quiet tick"
                  if kept else "queue full, dropped; segments keep their "
                               "D1 text"))
        return "yielded"
    if timed_out or not result:
        log_fn(f"deferred segment rewrite abandoned: segments={len(batch)} "
               f"input_chars={len(new_text)} — segment keeps its D1 text")
        return "timeout"


    polished = result[0] + raw[len(new_text):]
    try:
        merged = apply_grace_rewrite_to_parts(take["parts"], batch,
                                              head_text=polished)
        if merged is None:
            return "unchanged"
        joined = _join_take_text([p["text"] for p in merged], take["tail_text"])
        corrected = paragraph_fn(joined) if joined else joined
        if not corrected or corrected == take["original_text"]:
            take["parts"] = merged
            return "unchanged"
        correction_fn(take["audio_name"], corrected)


        take["parts"] = merged
        take["original_text"] = corrected
    except Exception as exc:
        log_fn(f"deferred segment rewrite apply error (ignored): {exc}")
        return "unchanged"
    log_fn(f"deferred segment rewrite landed: segments={len(batch)} "
           f"input_chars={len(new_text)} polish={now_fn()-t0:.1f}s "
           f"waited={now_fn()-item['queued_at']:.0f}s load={load1:.2f} — "
           f"wrote history correction for {take['audio_name']}")
    return "landed"



def wait_for_segments_settled(recorder, seg_lock, seg_texts, seg_busy,
                               deadline_seconds=120, poll_seconds=0.05,
                               sleep_fn=None, now_fn=None):
    """Bounded wait for background rolling segments to become safe to read
    (STOP-RACE FIX, 2026-07-23, long-recording ≤3s latency priority).

    transcribe() used to synchronously wait (up to 120s) for every background
    segment to reach FULL completion — including its LLM polish/classify step
    — before it could assemble the final text and let STOP return. That LLM
    step is exactly the slow, sometimes-cancelled-by-STOP-itself part (a
    full-rewrite can run ~12-25s typically, up to SEGMENT_TIMEOUT=60s; a
    classify fallback after a cancelled rewrite could run up to
    STRUCT_TIMEOUT's 30s/45s-hard budget — see polish._polish's matching
    STOP-RACE comment). Measured residual before this fix (Local Dictation
    System.md, stop-race-cancel note): "STOP fired at 2s -> text settled in
    6.6s" — already over the 3s target, and that was the FAST case.

    Fix: segment_worker() now publishes a segment's raw ASR text plus a
    deterministic (no-LLM, millisecond, lossless) punctuated fallback into its
    seg_texts entry — and marks entry["ready"]=True — the MOMENT its ASR
    finishes, *before* attempting the (possibly slow) LLM polish. This
    function only waits for every claimed segment to reach "ready" (its raw
    content is safe — ASR itself cannot be skipped without losing words, but
    it is fast and independently bounded by HARD_ASR_TIMEOUT), not for the
    LLM upgrade to finish. In the common case (the last segment's ASR already
    completed before its full-rewrite got cancelled by cancel_inflight()) this
    returns on the very first check, because "ready" is published before the
    LLM attempt even starts.

    Returns nothing; the caller reads seg_texts itself afterwards (and is
    expected to call polish_mod.cancel_inflight() again once this returns, to
    abandon whatever LLM work it just decided not to wait for). deadline_seconds
    is a pathological backstop only (e.g. a genuinely stuck ASR call not yet
    caught by its own HARD_ASR_TIMEOUT) — the ready-based exit is what
    actually bounds the common case now.

    Module-level (not a main() closure) so it's independently unit-testable —
    see test_stop_race_latency.py — with sleep_fn/now_fn injected so tests
    don't pay real wall-clock time. transcribe() calls this with the real
    recorder/seg_lock/seg_texts/seg_busy objects; behaviour is otherwise
    identical to the inline loop it replaces.
    """
    sleep_fn = sleep_fn or time.sleep
    now_fn = now_fn or time.time
    deadline = now_fn() + deadline_seconds
    while now_fn() < deadline:
        with seg_lock:
            segments_ready = all(e.get("ready") for e in seg_texts)
        if not recorder.pending_segments and not seg_busy.is_set():
            return
        if not recorder.pending_segments and segments_ready:
            return
        sleep_fn(poll_seconds)


def snapshot_live_segments(recorder, seg_lock, seg_texts, delivered_ids,
                           deadline, now_fn=None, floor_seconds=0.0):
    """Wait responsively until the STOP deadline, then copy live entries once.

    The worker mutates entry objects in place. Keeping references until after
    the readiness wait is essential: copying before waiting loses an ASR result
    that arrives during the budget. Entries that miss the deadline retain
    ``provisional=True`` and ``final=False``.

    ``floor_seconds`` (D1 guaranteed floor, INCIDENT 2026-07-25): if any entry
    is STILL not ready once ``deadline`` (the STOP_BUDGET_SECONDS target) has
    passed — e.g. settle-wait alone consumed the whole budget — it gets one
    further shared grace window of up to ``floor_seconds`` for its real ASR to
    land, rather than falling back immediately to the punctuated streaming
    partial. Entries that still miss THAT deadline retain
    ``provisional=True``/``final=False`` exactly as before; this is a second
    chance, not a change to the fallback contract. Default 0.0 preserves the
    old no-grace-window behaviour (used by callers/tests that pass a deadline
    that is already exact).
    """
    now_fn = now_fn or time.monotonic
    with seg_lock:
        live = list(seg_texts)
        live.extend(
            pending["entry"] for pending in list(recorder.pending_segments)
            if pending.get("entry") is not None)

    unique = []
    seen = set()
    for entry in live:
        segment_id = entry["segment_id"]
        if segment_id not in seen:
            seen.add(segment_id)
            unique.append(entry)

    for entry in unique:
        if entry.get("ready"):
            continue
        remaining = deadline - now_fn()
        if remaining <= 0:
            break
        event = entry.get("ready_event")
        if event is not None:
            event.wait(remaining)

    if floor_seconds > 0 and any(not entry.get("ready") for entry in unique):
        floor_deadline = now_fn() + floor_seconds
        for entry in unique:
            if entry.get("ready"):
                continue
            remaining = floor_deadline - now_fn()
            if remaining <= 0:
                break
            event = entry.get("ready_event")
            if event is not None:
                event.wait(remaining)

    with seg_lock:
        parts = []
        for entry in unique:
            snap = {
                key: value for key, value in entry.items()
                if key != "ready_event"
            }
            snap["final"] = bool(
                entry.get("ready") and not entry.get("provisional"))
            parts.append(snap)
        delivered_ids.update(seen)
        seg_texts[:] = [
            entry for entry in seg_texts
            if entry.get("segment_id") not in seen
        ]
    return parts


def main():
    global _lock
    _lock = acquire_singleton()









    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    server.listen(4)




















    state = {"lang": os.environ.get("DICTATION_DEFAULT_LANG", "yue"),
             "polish": True}
    recognizers = {"auto": build_recognizer("auto")}
    recorder = Recorder()










    try:
        dictionary_terms = polish_mod.load_dictionary()
    except Exception as exc:
        log(f"latin-repair: dictionary load failed (continuing dict-less): {exc}")
        dictionary_terms = {}
    repair_vocabulary = build_repair_vocabulary(
        dictionary_terms, HISTORY_PATH, persist_path=REPAIR_VOCAB_PERSIST_PATH)




    repair_fuzzy_vocabulary = dictionary_only_repair_vocabulary(dictionary_terms)
    log(f"latin-repair: vocabulary built ({len(repair_vocabulary)} phrases, "
        f"{len(repair_fuzzy_vocabulary)} fuzzy-eligible)")




    def sleep_assertion_refresher():
        if HOLD_SLEEP_ASSERTION:
            log("sleep-assertion refresher disabled "
                "(DICTATION_HOLD_SLEEP_ASSERTION=1) — holding the mic-stream "
                "power assertion for the process lifetime")
            return
        log(f"sleep-assertion refresher active — reopening the idle mic "
            f"stream every {SLEEP_REFRESH_SECONDS // 60}min so it never "
            f"becomes long-lived enough for powerd to reap it (Amphetamine "
            f"covers system wake instead; see INCIDENT note below)")












        consecutive_failures = 0
        MIC_DEAD_THRESHOLD = 3
        while True:
            time.sleep(SLEEP_REFRESH_SECONDS)
            try:
                if should_refresh_sleep_assertion(
                        HOLD_SLEEP_ASSERTION, recorder.recording,
                        is_busy=request_lock.is_held()):
                    recorder.reopen()
                    consecutive_failures = 0
                    log("sleep-assertion refresher: reopened idle mic stream")
                else:
                    log("sleep-assertion refresher: recording in progress, "
                        "skipped this cycle")
            except Exception as exc:
                consecutive_failures += 1
                try:
                    recorder.reopen(force_reinit=True)
                    consecutive_failures = 0
                    log("sleep-assertion refresher: recovered mic stream "
                        "after a forced PortAudio re-init retry")
                except Exception as exc2:
                    log(f"sleep-assertion refresher error "
                        f"({consecutive_failures} consecutive failure(s), "
                        f"forced PortAudio re-init retry also failed): {exc2}")
                    if consecutive_failures >= MIC_DEAD_THRESHOLD:






                        log(f"MIC DEAD — mic InputStream could not be "
                            f"reopened after {consecutive_failures} "
                            f"consecutive sleep-assertion refresh cycles "
                            f"(~{SLEEP_REFRESH_SECONDS * consecutive_failures // 60}"
                            f"min), even after forced PortAudio re-init. "
                            f"Original error: {exc}")
                        try:
                            notify_lock_recovery(
                                f"MIC DEAD: mic InputStream unrecoverable "
                                f"after {consecutive_failures} consecutive "
                                f"refresh cycles (PaErrorCode -9986 / stale "
                                f"PortAudio device table suspected). "
                                f"Original error: {exc}")
                        except Exception as exc3:
                            log(f"MIC DEAD notify failed (ignored): {exc3}")




































                        try:
                            busy = bool(getattr(recorder, "recording", False)) \
                                or request_lock.is_held()
                        except Exception:
                            busy = False
                        if busy:




                            log("MIC DEAD — restart deferred, a recording or "
                                "request is still in flight; will exit on the "
                                "next refresh cycle if it is still dead")
                        else:
                            log("MIC DEAD — exiting so launchd starts a clean "
                                "process with a fresh PortAudio context")
                            try:
                                sys.stdout.flush()
                                sys.stderr.flush()
                            except Exception:
                                pass
                            os._exit(70)

    import time
    threading.Thread(target=sleep_assertion_refresher, daemon=True).start()

    editor = None
    try:
        import editor
        editor.start_background()
    except Exception as exc:
        editor = None
        print(f"editor unavailable: {exc}", file=sys.stderr, flush=True)




    watchdog_mod, watchdog_state = None, None
    try:
        import watchdog as watchdog_mod
        watchdog_state = watchdog_mod.LatencyWatchdog()
    except Exception as exc:
        watchdog_mod = None
        print(f"watchdog unavailable: {exc}", file=sys.stderr, flush=True)

    def watchdog_observe(elapsed):
        """Feed one tail polish latency; fire the async self-heal when the
        trigger trips. Never raises, never blocks the paste path."""
        if watchdog_mod is None or editor is None or not state["polish"]:
            return
        try:




            if elapsed > watchdog_mod.SOFT_REGRESSION_THRESHOLD_SECONDS:
                log(f"watchdog: soft regression — foreground stop-to-text "
                    f"{elapsed:.1f}s > "
                    f"{watchdog_mod.SOFT_REGRESSION_THRESHOLD_SECONDS:.1f}s "
                    "(log only, no self-heal)")
            reason = watchdog_state.record(elapsed)
            if reason:
                watchdog_mod.start_heal_async(
                    reason, watchdog_state.healed_recently_when_tripped,
                    polish_mod, editor, log)
        except Exception as exc:
            log(f"watchdog error (ignored): {exc}")













    portaudio_wedge_state = (
        watchdog_mod.PortAudioWedgeWatchdog() if watchdog_mod else None)

    def portaudio_wedge_observe(op):
        if watchdog_mod is None or portaudio_wedge_state is None:
            return
        try:
            reason = portaudio_wedge_state.record(op)
            if reason:
                watchdog_mod.announce_portaudio_wedge(reason, editor, log)
        except Exception as exc:
            log(f"portaudio-wedge watchdog error (ignored): {exc}")

    global _portaudio_wedge_observer
    _portaudio_wedge_observer = portaudio_wedge_observe

    def recognizer_for(lang):
        if lang not in recognizers:
            recognizers[lang] = build_recognizer(lang)
        return recognizers[lang]

    asr_lock = threading.Lock()

    def asr_raw(samples, rate):


        with asr_lock:
            rec = recognizer_for(state["lang"])
            stream = rec.create_stream()



            samples = normalise_for_asr(samples)
            if ASR_TAIL_PAD_SECONDS > 0 and samples is not None and len(samples):
                samples = np.concatenate([
                    samples,
                    np.zeros(int(rate * ASR_TAIL_PAD_SECONDS), dtype=samples.dtype),
                ])
            stream.accept_waveform(rate, samples)
            rec.decode_stream(stream)
            decoded = cc.convert(TAG_RE.sub("", stream.result.text).strip())





        return repair_latin_fragments(
            decoded, repair_vocabulary, repair_fuzzy_vocabulary, log_fn=log)




    tail_stream = StreamingTail(recorder.tail_snapshot, asr_raw)
    recorder.partial_getter = tail_stream.hypothesis
    tail_stream.start()



    seg_texts = []
    seg_lock = threading.Lock()
    seg_busy = threading.Event()
    delivered_segment_ids = set()
    rewrite_state = {"previous_entry": None}

    deferred_rewrites = DeferredRewriteQueue(log_fn=log)

    def rewrite_segment_batch(batch):
        """Bind perform_segment_rewrite to this server's live collaborators."""
        perform_segment_rewrite(
            batch, rewrite_state, seg_lock, delivered_segment_ids,
            recorder, state["polish"], polish_mod.polish,
            guarded_call_fn=guarded_call, timeout=HARD_POLISH_TIMEOUT_FULL,
            log_fn=log, defer_fn=deferred_rewrites.defer)

    rewrite_worker = CoalescingRewriteWorker(rewrite_segment_batch)
    rewrite_worker.start()

    def deferred_rewrite_worker():
        """Tick the deferred-rewrite drain on a fixed interval (see
        DeferredRewriteQueue above). One rewrite per tick at most, and only
        while nothing has recorded for DEFERRED_REWRITE_IDLE_SECONDS — this
        thread must never be the thing competing with his microphone."""
        while True:
            time.sleep(DEFERRED_REWRITE_POLL_SECONDS)
            if not state["polish"]:
                continue
            try:
                drain_deferred_rewrites(
                    deferred_rewrites, polish_mod.polish,
                    guarded_call_fn=guarded_call,
                    timeout=HARD_POLISH_TIMEOUT_FULL, log_fn=log)
            except Exception as exc:
                log(f"deferred rewrite worker error (ignored): {exc}")

    threading.Thread(target=deferred_rewrite_worker, daemon=True,
                     name="deferred-segment-rewrite").start()




















    def _emergency_flush(signum, _frame):
        try:
            log(f"signal {signum} received — flushing in-memory audio before exit "
                "(see INCIDENT 2026-07-22: macOS powerd periodically SIGTERMs this "
                "process for holding a long-lived mic 'prevent idle sleep' assertion)")
            if recorder.recording:
                with recorder.lock:
                    frames = list(recorder.frames)
                if frames:
                    samples = np.concatenate(frames, axis=0).flatten()
                    if samples.size >= recorder.capture_rate // 4:
                        name = save_recording(samples, recorder.capture_rate)
                        log_history(
                            "", raw="[emergency save — server was killed mid-recording, "
                                    "never transcribed; use dashboard RETRY]",
                            audio=name)
                        log(f"emergency-saved {samples.size/recorder.capture_rate:.1f}s "
                            f"in-progress recording as {name}")
            for pending in list(recorder.pending_segments):
                frames = pending.get("frames", pending)
                if not frames:
                    continue
                samples = np.concatenate(frames, axis=0).flatten()
                if samples.size >= recorder.capture_rate // 4:
                    name = save_recording(samples, recorder.capture_rate)
                    log_history(
                        "", raw="[emergency save — pending segment, never transcribed; "
                                "use dashboard RETRY]",
                        audio=name)
                    log(f"emergency-saved pending segment as {name}")
            with seg_lock:
                parts = list(seg_texts)
            for part in parts:
                log_history(part.get("text", ""), raw=part.get("raw", ""), audio=None)
            if parts:
                log(f"emergency-flushed {len(parts)} already-processed segment(s) to history")
        except Exception as exc:
            log(f"emergency shutdown flush failed: {exc}")
        finally:
            import signal as _signal
            _signal.signal(signum, _signal.SIG_DFL)
            os.kill(os.getpid(), signum)

    import signal
    signal.signal(signal.SIGTERM, _emergency_flush)
    signal.signal(signal.SIGINT, _emergency_flush)

    def segment_worker():
        import time as _time
        while True:
            recorder.segment_event.wait()
            recorder.segment_event.clear()
            while recorder.pending_segments:
                seg_busy.set()
                pending = recorder.pending_segments[0]
                frames = pending.get("frames", pending)










                entry = pending.get(
                    "entry", {
                        "segment_id": pending["epoch"],
                        "raw": "", "text": "", "ready": False,
                        "provisional": True, "final": False,
                        "ready_event": threading.Event(),
                    })
                with seg_lock:
                    if entry["segment_id"] not in delivered_segment_ids and not any(
                            item["segment_id"] == entry["segment_id"]
                            for item in seg_texts):
                        seg_texts.append(entry)
                recorder.pending_segments.pop(0)
                try:
                    samples = np.concatenate(frames, axis=0).flatten()
                    rate = recorder.capture_rate
                    name = save_recording(samples, rate)
                    t0 = _time.time()




                    asr_result, asr_timed_out = guarded_call(
                        asr_raw, HARD_ASR_TIMEOUT, "background segment ASR",
                        samples, rate)
                    raw = "" if asr_timed_out else (asr_result or "")
                    t_asr = _time.time() - t0





                    fallback_text, _ = polish_mod.polish(raw, use_llm=False) if raw else ("", False)
                    with seg_lock:
                        entry["raw"] = raw
                        entry["text"] = fallback_text
                        entry["ready"] = True
                        entry["provisional"] = False
                        entry["final"] = True
                        entry["ready_event"].set()




                    if raw and recorder.recording:
                        rewrite_worker.submit({"entry": entry, "raw": raw})
                    log(f"segment ASR done in background: {samples.size/rate:.0f}s "
                        f"asr={t_asr:.1f}s {len(raw)} chars audio={name} "
                        f"rewrite_queue={rewrite_worker.queue_depth()}")
                except Exception as exc:
                    log(f"segment worker error: {exc}")
                    with seg_lock:
                        entry["ready"] = True
                        entry["ready_event"].set()
                finally:


                    with seg_lock:
                        delivered_segment_ids.discard(entry["segment_id"])
            seg_busy.clear()

    threading.Thread(target=segment_worker, daemon=True).start()

    def level_writer():
        """Publish the live mic level ~12x/s while recording, so the on-screen
        waveform moves with the voice — visual proof the mic is really hearing."""
        import time as _time
        while True:
            if recorder.recording:
                try:
                    with open("/tmp/dictation.level", "w") as fh:
                        fh.write(f"{recorder.level:.4f}")
                except OSError:
                    pass
                _time.sleep(0.08)
            else:
                _time.sleep(0.25)

    threading.Thread(target=level_writer, daemon=True).start()

    def transcribe(samples, dropouts=0):
        """Returns (text, audio_name) -- audio_name (2026-07-26) is the
        saved wav's filename, or None if nothing was saved for this take
        (an early misfire/dead-stream/silent-input return, all before
        save_recording() is ever called). Callers needing a retry-capable
        identifier (the STOP handler, so a Hammerspoon client can recover a
        failed take with RETRY <name> instead of guessing) use audio_name;
        callers that only want the text (none currently) can ignore it.

        Why this exists: "return the most recent transcription" was
        considered and REJECTED (2026-07-26) -- save_recording() is called
        from four places (this function's own tail save, segment_worker's
        per-segment background saves, and two SIGTERM emergency-flush
        paths), so no mtime/recency heuristic can reliably say which .wav
        belongs to which take; measured against 198 consecutive real takes,
        33% are under 30s apart (shortest gap 3s), so a "most recent"
        lookup would often grab a NEIGHBOURING take and paste confidently
        wrong text -- worse than pasting nothing. Returning THIS take's own
        audio_name has no such ambiguity: it names exactly the file this
        transcribe() call itself just produced (or None, honestly, if it
        produced none)."""
        import time as _time
        stop_deadline = _time.monotonic() + STOP_BUDGET_SECONDS
        rate = recorder.capture_rate















        audio_name = None
        raw = ""
        text = ""
        parts = []
        active_batch = None
        active_done = None
        output_gate_tripped = False
        tail_text = None

        try:
            n_pending_rewrites = rewrite_worker.clear_pending()
            if n_pending_rewrites:
                log(f"stop-race: discarded one coalesced pending rewrite "
                    f"covering {n_pending_rewrites} segment(s)")



















            t_race0 = _time.time()
            active_batch, active_done = rewrite_worker.snapshot_active()
            settle_elapsed = _time.time() - t_race0
            if active_batch or settle_elapsed > 0.3:
                log(f"stop-race: settled in {settle_elapsed:.1f}s "
                    f"(active_rewrite_grace={'pending' if active_batch else 'none'})")







            parts = snapshot_live_segments(
                recorder, seg_lock, seg_texts, delivered_segment_ids,
                stop_deadline, floor_seconds=STOP_CAPTURE_FLUSH_FLOOR_SECONDS)
            segment_floor_used = _time.monotonic() > stop_deadline



            for part in parts:
                if part.get("provisional") and part.get("raw"):
                    part["text"], _ = polish_mod.polish(
                        part["raw"], use_llm=False)
            if samples.size < rate // 4 and not parts:














                wall = getattr(recorder, "last_wall", 0.0)
                if samples.size == 0 and wall > 1.0:
                    log(f"recorded wall={wall:.1f}s but captured ZERO "
                        f"frames — mic InputStream is dead (stale "
                        f"PortAudio device table); reopening")
                    try:
                        recorder.reopen(force_reinit=True)
                    except Exception as exc:
                        log(f"mic reopen after zero-frame dead-stream "
                            f"failed: {exc}")
                return "", audio_name










            if not parts and float(np.abs(samples).max()) < 1e-4:

















                log("recording was silent — rebuilding mic stream with a "
                    "full PortAudio device rescan (re-resolves the current "
                    "default input device)")
                try:
                    recorder.reopen(force_reinit=True)
                except Exception as exc:
                    log(f"mic reopen failed: {exc}")
                _maybe_raise_input_volume("silent take")
                return "", audio_name






            elif not parts and samples.size:
                take_rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
                if take_rms < LOW_GAIN_RMS_THRESHOLD:
                    log(f"low-gain take: rms={take_rms:.4f} < "
                        f"{LOW_GAIN_RMS_THRESHOLD:.4f} threshold")
                    _maybe_raise_input_volume(f"low-gain take (rms={take_rms:.4f})")






            audio_name = save_recording(samples, rate) if samples.size >= rate // 4 else None
            t0 = _time.time()
            tail_raw = ""




            tail_decode_complete = True
            if samples.size >= rate // 4:

























                tail_decode_complete = tail_stream.flush(HARD_ASR_TIMEOUT)
                tail_raw = tail_stream.hypothesis(recorder.tail_epoch)
            t_asr = _time.time() - t0




            raw = join_with_seam_dedup([p["raw"] for p in parts] + [tail_raw])
            t0 = _time.time()
            used_llm = False
            try:
                if parts:






























                    if tail_raw:
                        tail_text, used_llm = polish_mod.polish(
                            tail_raw, use_llm=state["polish"] and FOREGROUND_LLM,
                            tail_samples=samples, tail_rate=rate,
                            audio_duration_seconds=samples.size / rate)
                    else:
                        tail_text, used_llm = "", False












                    joined = join_with_seam_dedup(
                        [p["text"] for p in parts] + [tail_text])
                    text = paragraph_mod.paragraph_break(joined) if joined else joined
                else:








                    text, used_llm = polish_mod.polish(
                        raw, use_llm=state["polish"] and FOREGROUND_LLM,
                        tail_samples=samples, tail_rate=rate,
                        audio_duration_seconds=samples.size / rate)
                    text = paragraph_mod.paragraph_break(text) if text else text
            except Exception as exc:




                log(f"polish failed, falling back to raw text: {exc}")
                text = raw
            t_polish = _time.time() - t0
            provisional_segments = sum(
                bool(part.get("provisional")) for part in parts)





            t_total = t_asr + settle_elapsed + t_polish
            budget_ok = t_total <= STOP_BUDGET_SECONDS








            floor_used = segment_floor_used
            log(f"transcribed {samples.size/rate:.0f}s tail + {len(parts)} segments: "
                f"asr={t_asr:.3f}s settle={settle_elapsed:.3f}s "
                f"polish={t_polish:.3f}s total={t_total:.3f}s "
                f"budget_ms={STOP_BUDGET_SECONDS * 1000:.0f} "
                f"budget={'PASS' if budget_ok else 'FAIL'} llm={used_llm} "
                f"provisional_segments={provisional_segments} "
                f"floor_used={floor_used} tail_decode_complete={tail_decode_complete} "
                f"raw={len(raw)} out={len(text)} chars dropouts={dropouts} "
                f"audio={audio_name}")
            if not tail_decode_complete:









                log(f"TAIL DECODE HUNG past HARD_ASR_TIMEOUT="
                    f"{HARD_ASR_TIMEOUT:.0f}s — tail_raw is almost certainly "
                    f"incomplete, audio={audio_name} — this is a stuck "
                    f"decoder, investigate, do not just re-run")









            if len(raw) > polish_mod.ULTRA_SHORT_CHARS:
                watchdog_observe(t_total)
        except Exception as exc:





            log(f"transcribe() unhandled error (audio saved as {audio_name}): {exc}")
            if not text:
                text = raw
        finally:
            text, output_gate_tripped, _ = output_language_gate.enforce(
                text, log=log)








            if active_batch and parts and tail_text is not None and audio_name:
                threading.Thread(
                    target=watch_grace_rewrite,
                    args=(active_batch, active_done, parts, tail_text, text,
                          audio_name),
                    daemon=True, name="stop-rewrite-grace-watcher").start()





            if audio_name and parts:
                try:
                    n_deferred = deferred_rewrites.bind_take(
                        parts, tail_text or "", text, audio_name)
                    if n_deferred:
                        log(f"deferred segment rewrite: bound {n_deferred} "
                            f"load-skipped rewrite(s) to {audio_name}; "
                            f"queue depth {deferred_rewrites.depth()}")
                except Exception as exc:
                    log(f"deferred rewrite bind error (ignored): {exc}")
            thist = log_history(text, raw=raw, audio=audio_name)





            if editor is not None and (text or raw) and not output_gate_tripped:
                try:
                    if thist:
                        editor.shadow_improve_async(thist, raw, text)
                    editor.capture_spellouts_async(raw)
                except Exception as exc:
                    log(f"post-dictation hooks error: {exc}")
        return text, audio_name

    def retry_audio(name):
        """Re-run the full pipeline on a saved recording (dashboard 重跑掣).

        The audio already exists on disk (that's the whole point of RETRY),
        so the lose-nothing concern here is narrower than in transcribe() —
        but ASR/polish are guarded the same way so a bad re-run degrades to
        raw text (or a logged, empty-but-diagnosable no-op) instead of an
        uncaught exception disappearing into handle()'s catch-all.
        """
        import wave as _wave
        path = os.path.join(RECORDINGS_DIR, os.path.basename(name))
        if not os.path.exists(path):
            return ""
        try:
            with _wave.open(path) as w:
                rate = w.getframerate()
                samples = np.frombuffer(
                    w.readframes(w.getnframes()), dtype=np.int16
                ).astype(np.float32) / 32768




            asr_result, asr_timed_out = guarded_call(
                asr_raw, HARD_ASR_TIMEOUT, "retry ASR", samples, rate)
            if asr_timed_out:
                return ""
            raw = asr_result or ""
        except Exception as exc:
            log(f"retry {os.path.basename(name)}: ASR failed: {exc}")
            return ""
        try:
            polish_result, polish_timed_out = guarded_call(
                polish_mod.polish, HARD_POLISH_TIMEOUT, "retry polish",
                raw, use_llm=state["polish"])
            text, used_llm = (raw, False) if polish_timed_out else polish_result
        except Exception as exc:
            log(f"retry {os.path.basename(name)}: polish failed, using raw: {exc}")
            text, used_llm = raw, False
        text, _, _ = output_language_gate.enforce(text, log=log)
        log(f"retry {os.path.basename(name)}: llm={used_llm} "
            f"raw={len(raw)} out={len(text)} chars")
        log_history(text, raw=raw, audio=os.path.basename(name))
        return text




    print(f"listening on {SOCKET_PATH}", flush=True)







    request_lock = RecoverableLock()

    def notify_lock_recovery(body):
        """Record a stuck-lock episode + self-heal so Tinho can see it
        happened (2026-07-23 requirement) — WITHOUT paging him. Correction
        (2026-07-23, same day): no ntfy for this. Tinho wants push
        notifications kept minimal so a truly urgent one never gets buried
        in routine self-heal noise — a stuck lock that already
        auto-recovered is, by definition, no longer urgent by the time
        anyone would read the push. Instead this rides the same changelog
        channel watchdog.py's latency self-heals already use
        (editor.log_watchdog_event → the GitHub sync Tinho reads / the
        dashboard), which is always logged to server.log regardless."""
        if editor is None:
            return
        try:
            editor.log_watchdog_event(body)
        except Exception as exc:
            log(f"lock-recovery changelog append failed (ignored): {exc}")

    def lock_monitor():
        """#63 last-resort recovery for a holder stuck outside guarded_call."""
        while True:
            time.sleep(LOCK_MONITOR_POLL_SECONDS)
            try:
                if request_lock.held_seconds() > MAX_LOCK_SECONDS:
                    findings = recover_portaudio_wedge(
                        request_lock, recorder, log, "monitor")
                    if any("request_lock" in finding for finding in findings):
                        notify_lock_recovery(
                            "慢速自癒：request_lock was stuck and the "
                            "PortAudio stream lock was rebuilt — dictation "
                            "should be working again.")
            except Exception as exc:
                log(f"lock monitor error (ignored): {exc}")

    threading.Thread(target=lock_monitor, daemon=True,
                     name="lock-monitor").start()














    recording_gate.register_signal(
        lambda: recorder.recording or seg_busy.is_set()
        or (0 < request_lock.held_seconds() <= MAX_LOCK_SECONDS))

    def handle(conn):
        data = "<not yet received>"
        keep_recording_active = False
        try:
            conn.settimeout(5.0)
            data = conn.recv(4096).decode("utf-8").strip()


            if data == "PING":
                conn.sendall(b"pong\n")
                return





















            if data == "CAPTURING":
                conn.sendall(capture_probe_reply(recorder).encode("utf-8")
                             + b"\n")
                return
            foreground_work = (data in ("START", "STOP")
                               or data.startswith("RETRY "))
            if foreground_work:



                recording_gate.set_recording_active(True)


            with request_lock.hold():
                if data == "START":
                    with seg_lock:
                        seg_texts.clear()
                    recorder.start()
                    keep_recording_active = True
                    reply = "ok"
                elif data == "STOP":
















                    stop_text, stop_audio_name = transcribe(
                        recorder.stop(), recorder.dropouts)
                    reply = f"{stop_audio_name or '-'}\t{stop_text}"
                elif data.startswith("LANG "):
                    lang = data.split(None, 1)[1].strip()
                    state["lang"] = lang
                    recognizer_for(lang)
                    reply = f"lang={lang}"
                elif data.startswith("POLISH "):
                    state["polish"] = data.split(None, 1)[1].strip() == "on"
                    reply = f"polish={'on' if state['polish'] else 'off'}"
                elif data.startswith("RETRY "):
                    reply = retry_audio(data.split(None, 1)[1].strip())
                elif data == "PING":
                    reply = "pong"
                else:
                    reply = "?"
            conn.sendall((reply + "\n").encode("utf-8"))
        except Exception as exc:







            if isinstance(exc, RequestLockBusy):
                log(f"request_lock timeout — returning '{BUSY_REPLY}'")
            else:
                log(f"unhandled request error ({data!r} truncated): {exc}\n"
                    f"{traceback.format_exc()}")
            try:
                conn.sendall(((BUSY_REPLY if isinstance(exc, RequestLockBusy)
                               else "") + "\n").encode("utf-8"))
            except OSError:
                pass
        finally:
            if not keep_recording_active and (
                    data in ("START", "STOP") or data.startswith("RETRY ")):
                recording_gate.set_recording_active(False)
            conn.close()

    def selftest_lock_probe():
        """Prove the request_lock + transcription path is genuinely alive —
        not just that polish() is fast when called directly, which is all
        the plain latency samples below ever prove (they never touch
        request_lock at all, so they cannot see a stuck lock). Actually
        acquires request_lock and runs a real polish() call through it, the
        same as a live STOP would. Returns (ok, elapsed, detail)."""
        t0 = time.time()
        probe_lock, acquired = request_lock.acquire_current(
            timeout=SELFTEST_LOCK_ACQUIRE_TIMEOUT)
        if not acquired:
            return False, time.time() - t0, (
                "could not acquire request_lock within "
                f"{SELFTEST_LOCK_ACQUIRE_TIMEOUT:.0f}s")
        try:
            _, timed_out = polish_mod.run_with_hard_timeout(
                polish_mod.polish, HARD_POLISH_TIMEOUT, "多謝晒",
                use_llm=True, full=False, cancellable=False)
            if timed_out:
                return False, time.time() - t0, (
                    "polish() did not complete within "
                    f"{HARD_POLISH_TIMEOUT:.0f}s even after acquiring "
                    "request_lock")
            return True, time.time() - t0, "ok"
        finally:
            request_lock.release_current(probe_lock)

    def selftest_force_recover():
        findings = recover_portaudio_wedge(
            request_lock, recorder, log, "selftest")
        if any("request_lock" in finding for finding in findings):
            notify_lock_recovery(
                "慢速自癒：selftest detected a stuck request_lock and the "
                "PortAudio stream lock was rebuilt — dictation should be "
                "working again.")
        return findings







    if watchdog_mod is not None and editor is not None:
        def selftest_busy():





            return recording_gate.busy()
        watchdog_mod.start_selftest_async(
            polish_mod, editor, selftest_busy, log,
            lock_probe=selftest_lock_probe,
            force_recover=selftest_force_recover)







    def accept_loop():
        while True:
            conn, _ = server.accept()
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    threading.Thread(target=accept_loop, daemon=True).start()









    try:
        warm_ok = polish_mod.warm_up()
    except Exception as exc:
        warm_ok = False
        log(f"model warm-up raised: {exc}")
    log("model warm-up " + ("ok" if warm_ok else
        "incomplete — proceeding; dictation will still wait up to "
        "DICTATION_WARMUP_WAIT_MAX before giving up on it"))
    print("model loaded", flush=True)




    threading.Event().wait()


if __name__ == "__main__":
    main()

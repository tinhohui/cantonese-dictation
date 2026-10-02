#!/usr/bin/env python3
"""Content-loss canary (2026-07-25, incident response).

Why this exists — two real bugs hit Tinho's live dictation on the night of
2026-07-25 and the EXISTING self-test (watchdog.py's SELFTEST_SAMPLES, run
every DICTATION_SELFTEST_MINUTES) reported PASS through both of them:

  1. a PortAudio stream stop wedged, holding request_lock, so every
     dictation returned empty text for ~50 minutes;
  2. STOP dropped the final words of a take —
     recordings/20260725-230355.wav (15.4s) produced 40 chars live, ending
     WITHOUT the final word, while decoding the SAME file offline with the
     SAME SenseVoice model yields 46 chars ending "...现在差不多三十秒收工。"

The existing self-test feeds CANNED audio straight into polish() and times
it — it never exercises the mic, STOP, or the tail flush, and it never
looks at what a take actually PRODUCED. It measures latency on the one path
that was not broken. This module adds the check the incident actually
needed: did a REAL take's live output contain (most of) what was actually
said?

See also stop_integrity_guard.py (2026-07-26) — a cheaper, faster,
complementary check for the more catastrophic version of the same failure
class: a STOP that returns literally ZERO characters (invisible even to
this module, since server.py's log_history() never writes an empty-output
entry to history.jsonl at all, so this canary never gets a chance to look
at it). That module is the floor beneath this one, not a duplicate of it.

Design, per D1 (zero-content-loss is the top priority) and D71 (the rewrite
path is out of scope here — untouched):

  1. Pick a recent real take: a history.jsonl entry whose "audio" file is
     still on disk (recordings/ only keeps the newest KEEP_RECORDINGS=10 —
     see server.py).
  2. Re-decode that wav OFFLINE with the exact SAME SenseVoice recognizer
     config server.py's build_recognizer() uses (model, tokens, use_itn,
     language="auto", num_threads) — CPU sherpa-onnx, never the mic, never
     ollama, never the GPU.
  3. Compare the offline full-decode text against the LIVE text the
     pipeline actually produced for that take (history.jsonl's "text"
     field — what was actually pasted). A literal string compare is
     useless: live text is punctuated/polished by an LLM pass and the
     offline decode is raw ASR, and the live path emits Traditional
     (OpenCC s2hk, same as server.py's asr_raw()) while a naive offline
     decode would still be Simplified. `_normalize()` strips punctuation/
     whitespace AND runs the same s2hk conversion on both sides so neither
     of those is ever mistaken for content loss.
  4. Trip on SHORTFALL, not wording. Two measured signals, both computed on
     normalized text:
       - char_shortfall_ratio: overall (offline_len - live_len) / offline_len
       - tail_missing_fraction: how much of the offline decode's OWN tail
         window fails to appear anywhere in the live text (SequenceMatcher
         alignment) — this is the STOP-drops-the-ending signature
         specifically, independent of the overall ratio.
     Thresholds are set from a measured healthy distribution — see
     CHAR_SHORTFALL_THRESHOLD / TAIL_MISSING_THRESHOLD below and the PR body
     for the actual numbers sampled.

Wiring (item 4 of the brief): `maybe_run()` is called from watchdog.py's
existing selftest_worker() loop, inside the SAME busy_fn-gated branch the
latency self-test already uses (server.py's selftest_busy() ==
recording_gate.busy()) — so this NEVER runs while a recording is in flight
or within recording_gate's post-STOP hold window, using the mechanism that
already exists rather than a new one. `maybe_run()` also re-checks
recording_gate.busy() itself (belt-and-braces, cheap) so it stays safe even
if ever called from a different cadence in the future.

Notification: on trip, this calls the SAME watchdog.notify_fyi() local macOS
banner the latency watchdog already uses. It has no URL/action and does not
steal focus (tinho-os D10a reserves disruptive surfaces for stop-breach /
circuit-breaker / system-down; a content-loss canary trip is neither).

Wired into watchdog.py's selftest_worker loop (called every poll, its own
due()/INTERVAL cadence gates the actual work) — see the PR body for the
measured healthy-shortfall distribution and the false-positive check
against real recent takes. This module never touches server.py.
"""
import datetime
import difflib
import json
import os
import re
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))





MODEL_DIR = os.environ.get(
    "DICTATION_CONTENT_GUARD_MODEL_DIR",
    os.path.join(HERE, "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"))
HISTORY_PATH = os.environ.get(
    "DICTATION_CONTENT_GUARD_HISTORY_PATH", os.path.join(HERE, "history.jsonl"))
RECORDINGS_DIR = os.environ.get(
    "DICTATION_CONTENT_GUARD_RECORDINGS_DIR", os.path.join(HERE, "recordings"))



TAG_RE = re.compile(r"<\|[^|]*\|>")

STATE_PATH = os.path.join(HERE, ".content_guard_state")
INTERVAL = int(os.environ.get("DICTATION_CONTENT_GUARD_MINUTES", "30")) * 60
































CANDIDATE_LOOKBACK = 20


MIN_NORMALIZED_CHARS = 15




















SEGMENT_SECONDS = int(os.environ.get("DICTATION_SEGMENT_SECONDS", "15"))







































CHAR_SHORTFALL_THRESHOLD = float(
    os.environ.get("DICTATION_CONTENT_GUARD_SHORTFALL", "0.08"))
TAIL_WINDOW_CHARS = int(
    os.environ.get("DICTATION_CONTENT_GUARD_TAIL_CHARS", "12"))
TAIL_MISSING_THRESHOLD = float(
    os.environ.get("DICTATION_CONTENT_GUARD_TAIL_MISSING", "0.50"))

_CC = None


def _cc():
    """Lazy import — OpenCC is a native extension; keep it off the import
    path for callers that only want the pure-string helpers (tests)."""
    global _CC
    if _CC is None:
        from opencc import OpenCC
        _CC = OpenCC("s2hk")
    return _CC


def _normalize(text):
    """Strip ASR tags, ALL whitespace and punctuation (Unicode categories P*
    and Z*, covering both ASCII and CJK punctuation), then canonicalize
    script via the same s2hk conversion server.py's asr_raw() already
    applies. Two texts that differ only by punctuation, spacing, or
    Simplified-vs-Traditional script normalize to the same string — neither
    is ever mistaken for content loss."""
    if not text:
        return ""
    text = TAG_RE.sub("", text)
    text = "".join(
        ch for ch in text
        if not unicodedata.category(ch).startswith(("P", "Z", "C")))
    return _cc().convert(text)


def char_shortfall_ratio(live_text, offline_text):
    """(offline_len - live_len) / offline_len on normalized text. 0.0 when
    live has at least as much content as the offline decode (never negative
    — a live take that's LONGER than the offline decode, e.g. because
    polish spelled out a number, is not a loss). Both inputs may be raw
    (un-normalized) live/offline text; normalization happens here."""
    live_n = _normalize(live_text)
    off_n = _normalize(offline_text)
    if not off_n:
        return 0.0
    return max(0.0, (len(off_n) - len(live_n)) / len(off_n))


def tail_missing_fraction(live_text, offline_text,
                          tail_chars=TAIL_WINDOW_CHARS):
    """How much of the offline decode's OWN final `tail_chars` (after
    normalization) fails to appear anywhere in the live text, via
    SequenceMatcher alignment. 0.0 = the whole tail is accounted for
    somewhere in live; 1.0 = none of it is. This is the STOP-drops-the-
    ending signature specifically — a take that lost a middle phrase but
    kept its ending scores low here even if char_shortfall_ratio is high,
    and vice versa; the canary trips on either."""
    live_n = _normalize(live_text)
    off_n = _normalize(offline_text)
    if not off_n:
        return 0.0
    tail = off_n[-tail_chars:] if len(off_n) > tail_chars else off_n
    if not tail:
        return 0.0
    if not live_n:
        return 1.0
    matcher = difflib.SequenceMatcher(None, live_n, tail, autojunk=False)
    covered = sum(block.size for block in matcher.get_matching_blocks())
    return max(0.0, min(1.0, 1.0 - covered / len(tail)))


def evaluate(live_text, offline_text,
             char_shortfall_threshold=CHAR_SHORTFALL_THRESHOLD,
             tail_missing_threshold=TAIL_MISSING_THRESHOLD,
             tail_chars=TAIL_WINDOW_CHARS):
    """Pure decision function — no I/O, fully unit-testable. Returns
    (tripped, reason_or_None, metrics_dict)."""
    shortfall = char_shortfall_ratio(live_text, offline_text)
    tail_missing = tail_missing_fraction(live_text, offline_text, tail_chars)
    metrics = {"char_shortfall_ratio": shortfall,
               "tail_missing_fraction": tail_missing}
    if shortfall > char_shortfall_threshold:
        return True, (f"content-guard: {shortfall:.0%} char shortfall vs "
                      f"offline decode (threshold {char_shortfall_threshold:.0%})"), metrics
    if tail_missing > tail_missing_threshold:
        return True, (f"content-guard: {tail_missing:.0%} of the offline "
                      f"decode's tail is missing from live output "
                      f"(threshold {tail_missing_threshold:.0%})"), metrics
    return False, None, metrics




def _read_history_tail(history_path=HISTORY_PATH, lookback=CANDIDATE_LOOKBACK):
    """Last `lookback` parseable entries from history.jsonl, oldest first is
    NOT assumed — returned newest-last exactly as the file has them, so
    callers reverse for "most recent first"."""
    try:
        with open(history_path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    out = []
    for line in lines[-lookback:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def pick_recent_take(history_path=HISTORY_PATH, recordings_dir=RECORDINGS_DIR,
                     lookback=CANDIDATE_LOOKBACK,
                     min_chars=MIN_NORMALIZED_CHARS, evaluated=None):
    """Most recent history.jsonl entry whose audio file is still on disk,
    whose live text is long enough (normalized) for the ratios to be
    meaningful, whose take was never cut into segments — see the v1 SCOPE
    comment above SEGMENT_SECONDS: a "\\n\\n" in live text means at least one
    segment was assembled separately from the tail, so the saved wav
    (tail-only) can't be validly compared against the whole live text — AND
    whose audio filename is not already in `evaluated` (2026-09-30 fix: see
    the "EVALUATE-ONCE" note above INTERVAL — a take this canary has already
    checked, tripped or not, is never picked again). Returns (entry_dict,
    wav_path) or (None, None) if nothing suitable is found in the lookback
    window. Older-but-unevaluated entries are still considered even when the
    single newest entry has already been evaluated — this only skips a
    SPECIFIC already-seen filename, it does not stop looking further back."""
    evaluated = evaluated or ()
    entries = _read_history_tail(history_path, lookback)
    try:
        on_disk = set(os.listdir(recordings_dir))
    except OSError:
        return None, None
    for entry in reversed(entries):
        audio = entry.get("audio")
        text = entry.get("text") or ""
        if not audio or audio not in on_disk:
            continue
        if audio in evaluated:
            continue
        if "\n\n" in text:
            continue
        if len(_normalize(text)) < min_chars:
            continue
        return entry, os.path.join(recordings_dir, audio)
    return None, None




def build_recognizer(language="auto", model_dir=None, num_threads=4):
    """Mirrors server.py's build_recognizer() exactly (same model file, same
    use_itn, same language default) — the whole point of this canary is
    comparing against what the SAME config would have produced, not a
    different one. Duplicated (not imported from server.py) deliberately:
    this module must stay importable and safe to exercise even while
    server.py is mid-edit elsewhere, and must never trigger any of
    server.py's module-level state."""
    import sherpa_onnx
    model_dir = model_dir or MODEL_DIR
    return sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=os.path.join(model_dir, "model.int8.onnx"),
        tokens=os.path.join(model_dir, "tokens.txt"),
        use_itn=True,
        language=language,
        num_threads=num_threads,
        debug=False,
    )


def decode_wav_offline(wav_path, recognizer=None, model_dir=None):
    """Full, non-streaming decode of one saved wav — CPU sherpa-onnx only,
    same tag-strip + s2hk conversion as server.py's asr_raw() so the offline
    text is directly comparable (normalize() still runs on both sides
    regardless, as a second line of defense). Never touches the mic or
    ollama."""
    import wave

    import numpy as np

    with wave.open(wav_path, "rb") as w:
        rate = w.getframerate()
        n_frames = w.getnframes()
        raw = w.readframes(n_frames)
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

    rec = recognizer or build_recognizer(model_dir=model_dir)
    stream = rec.create_stream()
    stream.accept_waveform(rate, samples)
    rec.decode_stream(stream)

    from opencc import OpenCC
    cc = OpenCC("s2hk")
    return cc.convert(TAG_RE.sub("", stream.result.text).strip())




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


def due(state=None, interval=INTERVAL):
    state = state if state is not None else _state()
    last = state.get("last")
    if not last:
        return True
    try:
        dt = datetime.datetime.fromisoformat(last)
    except (TypeError, ValueError):
        return True
    return (datetime.datetime.now() - dt).total_seconds() > interval


def run_once(log=print, history_path=HISTORY_PATH, recordings_dir=RECORDINGS_DIR,
            model_dir=None, evaluated=None):
    """Pick a take, decode it offline, evaluate. Never raises — any failure
    (missing model dir, unreadable wav, decode error) is logged and treated
    as "nothing to report" for this cycle, not as a trip; a canary that
    fails open on its OWN errors is safer than one that manufactures false
    trips out of infrastructure hiccups. `evaluated` (optional set of audio
    filenames already checked in a prior cycle) is forwarded straight to
    pick_recent_take so an already-seen recording is never picked again —
    see the 2026-09-30 EVALUATE-ONCE note above INTERVAL. Returns
    (tripped, reason, metrics) or (False, None, None) if no suitable take
    was found or an error occurred. When a take WAS found and checked,
    metrics also carries metrics["audio"] = that take's filename, so a
    caller (maybe_run) can record it as evaluated without this function's
    return shape changing."""
    try:
        entry, wav_path = pick_recent_take(history_path, recordings_dir,
                                           evaluated=evaluated)
        if entry is None:
            log("content-guard: no suitable recent take found — skipping")
            return False, None, None
        live_text = entry.get("text") or ""
        offline_text = decode_wav_offline(wav_path, model_dir=model_dir)
        tripped, reason, metrics = evaluate(live_text, offline_text)
        metrics["audio"] = entry.get("audio")
        log(f"content-guard: checked {entry.get('audio')} "
            f"shortfall={metrics['char_shortfall_ratio']:.0%} "
            f"tail_missing={metrics['tail_missing_fraction']:.0%}")
        if tripped:









            try:
                import incident_quarantine
                incident_quarantine.quarantine(
                    wav_path, "content_guard", metrics, history_entry=entry,
                    recordings_dir=recordings_dir, log=log)
            except Exception as exc:
                log(f"content-guard: quarantine failed (ignored, trip "
                    f"already logged): {exc}")
        else:








            try:
                import server
                server.mark_recording_checked(entry.get("audio"))
            except Exception as exc:
                log(f"content-guard: mark-checked failed (ignored, check "
                    f"already logged): {exc}")
        return tripped, reason, metrics
    except Exception as exc:
        log(f"content-guard: check errored (ignored, no trip): {exc}")
        return False, None, None


def maybe_run(log=print, notify_fn=None, busy_fn=None, force=False):
    """Entry point for watchdog.py's cadence loop. Runs at most once per
    INTERVAL (or `force=True` for tests/manual runs), and only when nothing
    reports busy. `busy_fn` defaults to recording_gate.busy — passed
    explicitly here (rather than hardcoded) so this stays unit-testable
    without recording_gate's global state leaking between tests, and so a
    caller that already has its own busy check (watchdog.py's
    selftest_worker) doesn't pay for two redundant probes silently
    disagreeing; the default still makes this module safe to call standalone.
    """
    if busy_fn is None:
        import recording_gate
        busy_fn = recording_gate.busy
    try:
        if busy_fn():
            return None
    except Exception:
        return None
    if not force and not due():
        return None



    state = _state()
    evaluated = set(state.get("evaluated") or [])
    tripped, reason, metrics = run_once(log=log, evaluated=evaluated)
    if metrics and metrics.get("audio"):
        evaluated.add(metrics["audio"])





    try:
        still_on_disk = set(os.listdir(RECORDINGS_DIR))
        incidents_dir = os.path.join(RECORDINGS_DIR, "incidents")
        if os.path.isdir(incidents_dir):
            still_on_disk |= set(os.listdir(incidents_dir))
        evaluated = {name for name in evaluated if name in still_on_disk}
    except OSError:
        pass
    state["evaluated"] = sorted(evaluated)
    state["last"] = datetime.datetime.now().isoformat(timespec="seconds")
    _write_state(state)
    if tripped:
        log(f"content-guard: TRIPPED — {reason}")
        if notify_fn is not None:
            try:
                notify_fn(f"⚠️ dictation content-guard: {reason}", log)
            except Exception as exc:
                log(f"content-guard: notify failed: {exc}")
    return tripped, reason, metrics

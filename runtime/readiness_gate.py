#!/usr/bin/env python3
"""readiness_gate.py — general-purpose readiness mechanism for deferred work.

Tinho, 2026-07-26, his own words: 「如果沒有足夠樣本而不值得執行，就會有一個判
斷機制去判斷它值不值得執行；如果不值得執行就不執行。但是如果有一天真的已經足
夠樣本執行的時候，便會自動執行。」

Today, work that cannot yet be evaluated (not enough real data to measure it
against) gets deferred into a human's memory — which means it is forgotten,
or re-litigated from scratch months later. This module is the general fix:

  1. A REGISTRY (see REGISTRY below) of deferred work. Each entry records
     WHAT is deferred, WHY (the measurement that failed), a READINESS
     CONDITION expressed as something checkable against real data, its
     DERIVATION (never a picked round number — see each item's comment for
     what the number is actually tied to), and WHAT TO RUN once ready.
  2. A cheap PERIODIC CHECK (`maybe_run` / `run_gate`) that evaluates every
     registered condition. Genuinely cheap: filesystem reads, regex, plain
     numpy on already-saved audio — no model call, no ollama, no GPU, no
     mic, ever. Reuses `recording_gate` (the EXISTING single source of
     truth for "is Tinho recording right now") rather than inventing a
     second busy-detection mechanism — this never runs, and never even
     checks, while a recording (or its tail transcribe/polish/paste) is in
     flight.
  3. When a condition is met, the registered evaluation runs and its result
     is appended to READINESS_STATE_PATH. If the result is still not a
     clean "ship it", the item re-defers with the NEW numbers recorded —
     never silently retried forever, and never silently dropped either.
     Because each check re-reads real data (recordings/, history.jsonl,
     server.log) rather than a cached verdict, the loop is bounded by data
     arrival, not by a hidden retry counter: it cannot spin, because
     nothing changes between two checks unless Tinho has actually recorded
     more.

D1 (zero content loss) applies throughout: every function below is
READ-ONLY against recordings/, history.jsonl and server.log. Nothing here
ever writes to, deletes, reorders, or overwrites Tinho's own data or output
text — it only observes and schedules. Two other files it never touches:
server.py and polish.py own the LIVE prosody/filler-strip code paths; this
module only re-derives measurements from already-saved, already-produced
data, entirely outside those files.
"""
import datetime
import glob
import json
import os
import re
import wave

import numpy as np

import recording_gate

HERE = os.path.dirname(os.path.abspath(__file__))
RECORDINGS_DIR = os.environ.get(
    "DICTATION_READINESS_RECORDINGS_DIR", os.path.join(HERE, "recordings"))
HISTORY_PATH = os.environ.get(
    "DICTATION_READINESS_HISTORY_PATH", os.path.join(HERE, "history.jsonl"))
SERVER_LOG_PATH = os.environ.get(
    "DICTATION_READINESS_SERVER_LOG", os.path.join(HERE, "server.log"))
READINESS_STATE_PATH = os.environ.get(
    "DICTATION_READINESS_STATE", os.path.join(HERE, "readiness_state.jsonl"))
DUE_STATE_PATH = os.environ.get(
    "DICTATION_READINESS_DUE_STATE", os.path.join(HERE, ".readiness_gate_state"))







INTERVAL = int(os.environ.get("DICTATION_READINESS_GATE_MINUTES", "30")) * 60






class ReadinessItem:
    """One deferred piece of work registered with the gate.

    `what`/`why_deferred`/`derivation` are prose for humans reading the
    registry; `check_fn`/`run_fn` are the code that makes the condition and
    the action real rather than a promise (D59/D67: a rule that must hold
    every time lives in code, not prose)."""

    def __init__(self, item_id, what, why_deferred, derivation, check_fn, run_fn):
        self.id = item_id
        self.what = what
        self.why_deferred = why_deferred
        self.derivation = derivation
        self.check_fn = check_fn
        self.run_fn = run_fn


def _append_state(record, path=None):
    path = path or READINESS_STATE_PATH
    record = dict(record)
    record["ts"] = datetime.datetime.now().isoformat(timespec="seconds")
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return record


def evaluate_item(item, state_path=None):
    """Run just the cheap check for one item. Never touches audio/models."""
    result = item.check_fn()
    return _append_state({"id": item.id, "phase": "check", **result}, path=state_path)


def run_gate(registry, max_wait=None, log=None, state_path=None):
    """The periodic entry point: check every registered item, and run the
    ones that are ready. Reuses `recording_gate` — the existing single
    source of truth for "is Tinho recording right now" — instead of a new
    busy-detection mechanism; never checks or runs while a recording (or
    its tail transcribe/polish/paste) is in flight.

    Returns a list of per-item outcomes ({"id", "ready": False, ...} for
    items not yet ready, {"id", "ready": True, "ran": True, ...} for items
    evaluated this call).
    """
    if not recording_gate.wait_until_idle(max_wait=max_wait, log=log):
        if log:
            log("readiness_gate: still busy after max_wait, skipping this cycle")
        return []

    outcomes = []
    for item in registry:
        try:
            check = evaluate_item(item, state_path=state_path)
        except Exception as exc:
            if log:
                log(f"readiness_gate: {item.id} check errored (ignored): {exc}")
            continue
        if not check.get("ready"):
            outcomes.append(check)
            continue


        if not recording_gate.wait_until_idle(max_wait=max_wait, log=log):
            outcomes.append(check)
            continue
        try:
            result = item.run_fn()
        except Exception as exc:
            if log:
                log(f"readiness_gate: {item.id} run errored (ignored): {exc}")
            outcomes.append(check)
            continue
        record = _append_state(
            {"id": item.id, "phase": "run", "ready": True, **result}, path=state_path)
        if log:
            log(f"readiness_gate: {item.id} ran — {record.get('verdict', record)}")
        outcomes.append(record)
    return outcomes






def _due_state():
    try:
        with open(DUE_STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _write_due_state(state):
    try:
        with open(DUE_STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
    except OSError:
        pass


def due(state=None, interval=INTERVAL):
    state = state if state is not None else _due_state()
    last = state.get("last")
    if not last:
        return True
    try:
        dt = datetime.datetime.fromisoformat(last)
    except (TypeError, ValueError):
        return True
    return (datetime.datetime.now() - dt).total_seconds() > interval


def maybe_run(log=print, busy_fn=None, force=False, registry=None):
    """Entry point for watchdog.py's cadence loop (mirrors content_guard.
    maybe_run's contract exactly). Runs at most once per INTERVAL (or
    `force=True` for tests/manual runs), and only when nothing reports
    busy. `busy_fn` defaults to recording_gate.busy — passed explicitly so
    a caller that already has its own busy check (watchdog.py's
    selftest_worker) doesn't pay for a second redundant probe, while this
    module stays safe to call standalone."""
    if busy_fn is None:
        busy_fn = recording_gate.busy
    try:
        if busy_fn():
            return None
    except Exception:
        return None
    if not force and not due():
        return None
    outcomes = run_gate(registry if registry is not None else REGISTRY, log=log)
    _write_due_state({"last": datetime.datetime.now().isoformat(timespec="seconds")})
    return outcomes






def _retained_recordings():
    try:
        return sorted(glob.glob(os.path.join(RECORDINGS_DIR, "*.wav")))
    except OSError:
        return []


def _stem_to_dt(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    try:
        return datetime.datetime.strptime(stem, "%Y%m%d-%H%M%S")
    except ValueError:
        return None

















_KEEP_RECORDINGS = int(os.environ.get("DICTATION_KEEP_RECORDINGS", "10"))
HISTORY_LOOKBACK = max(50, _KEEP_RECORDINGS * 10)


def _read_history_rows(lookback=None):
    lookback = HISTORY_LOOKBACK if lookback is None else lookback
    rows = []
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return rows
    for line in lines[-lookback:]:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _history_text(entry):
    for key in ("text", "final_text", "output", "polished"):
        v = entry.get(key)
        if isinstance(v, str) and v:
            return v
    return ""


def _history_timestamp(entry):
    for key in ("ts", "timestamp", "time"):
        v = entry.get(key)
        if v:
            return str(v)
    return ""


def _parsed_history_rows():
    parsed = []
    for r in _read_history_rows():
        ts_raw = _history_timestamp(r)
        dt = None
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.datetime.strptime(ts_raw[:19], fmt)
                break
            except ValueError:
                continue
        if dt is not None:
            parsed.append((dt, _history_text(r)))
    return parsed


def _match_recordings_to_text(max_skew_seconds=30):
    """Read-only pairing of currently-retained wavs to their history.jsonl
    transcript by nearest timestamp (the wav filename IS a capture
    timestamp; history rows carry their own). A generous but bounded skew
    window: wide enough to survive normal queueing/rewrite latency, tight
    enough that two different takes in the same session don't collide. No
    audio decode here — this is the CHEAP check path, reused by both
    registered items' check_fn."""
    parsed_rows = _parsed_history_rows()
    pairs = []
    for wav in _retained_recordings():
        wav_dt = _stem_to_dt(wav)
        if wav_dt is None or not parsed_rows:
            continue
        best_dt, best_text = min(
            parsed_rows, key=lambda pr: abs((pr[0] - wav_dt).total_seconds()))
        if abs((best_dt - wav_dt).total_seconds()) <= max_skew_seconds:
            pairs.append((wav, best_text))
    return pairs





















PROSODY_READY_N = 10








PROSODY_TERMINAL_WINDOW_SECONDS = float(
    os.environ.get("DICTATION_PROSODY_TERMINAL_WINDOW_SECONDS", "0.6"))
PROSODY_MIN_VOICED_FRAMES = 3
PROSODY_RISE_SEMITONES = 1.5
PROSODY_FRAME_MS = 30
PROSODY_HOP_MS = 15
PROSODY_RATE_ANALYSIS_SECONDS = 5.0
PROSODY_PITCH_FMIN = 70.0
PROSODY_PITCH_FMAX = 400.0
PROSODY_MIN_AUDIO_SECONDS = 0.4


def _frame_pitch_track(samples, rate, fmin=PROSODY_PITCH_FMIN, fmax=PROSODY_PITCH_FMAX):
    frame_n = max(1, int(rate * PROSODY_FRAME_MS / 1000))
    hop_n = max(1, int(rate * PROSODY_HOP_MS / 1000))
    if samples.size < frame_n:
        return np.array([])
    window = np.hanning(frame_n)
    min_lag = max(1, int(rate / fmax))
    max_lag = min(frame_n - 1, int(rate / fmin))
    if min_lag >= max_lag:
        return np.array([])
    pitches = []
    for start in range(0, samples.size - frame_n + 1, hop_n):
        frame = samples[start:start + frame_n].astype(np.float64)
        frame = frame - frame.mean()
        if not np.any(frame):
            pitches.append(0.0)
            continue
        windowed = frame * window
        ac = np.correlate(windowed, windowed, mode="full")
        ac = ac[ac.size // 2:]
        if ac[0] <= 0:
            pitches.append(0.0)
            continue
        segment = ac[min_lag:max_lag]
        if segment.size == 0:
            pitches.append(0.0)
            continue
        peak_idx = int(np.argmax(segment)) + min_lag
        peak_val = ac[peak_idx]
        if peak_val <= 0 or peak_val < 0.3 * ac[0]:
            pitches.append(0.0)
            continue
        pitches.append(rate / peak_idx)
    return np.array(pitches)


def _estimate_speaking_rate(samples, rate):
    if samples.size < int(rate * 0.2):
        return None
    frame_n = max(1, int(rate * 0.02))
    n_frames = samples.size // frame_n
    if n_frames < 2:
        return None
    trimmed = samples[:n_frames * frame_n].reshape(n_frames, frame_n)
    env = np.sqrt(np.mean(np.square(trimmed), axis=1))
    if env.max() <= 1e-9:
        return 0.0
    above = env > (env.max() * 0.35)
    peaks = int(np.sum(above[1:] & ~above[:-1])) + int(above[0])
    duration = samples.size / rate
    return peaks / duration if duration > 0 else None


def extract_prosody_features(samples, rate):
    """Bounded prosodic summary of one take's own audio: terminal pitch
    rise/fall, an energy-envelope summary, and speaking rate. Returns None
    when the audio says nothing reliable — absence is always safe (D1
    tie-break: uncertain -> no signal). Verbatim port of PR #30's
    server.py:extract_prosody_features(); see the module docstring above
    for why it lives here instead of being imported."""
    try:
        if samples is None or rate is None or rate <= 0:
            return None
        samples = np.asarray(samples).reshape(-1)
        if samples.size < int(rate * PROSODY_MIN_AUDIO_SECONDS):
            return None
        term_n = min(int(rate * PROSODY_TERMINAL_WINDOW_SECONDS), samples.size)
        terminal = samples[-term_n:]
        ref_start = max(0, samples.size - 2 * term_n)
        reference = samples[ref_start: samples.size - term_n]
        if reference.size < int(rate * 0.2):
            reference = samples[: max(1, samples.size - term_n)]

        term_voiced = _frame_pitch_track(terminal, rate)
        term_voiced = term_voiced[term_voiced > 0]
        ref_voiced = _frame_pitch_track(reference, rate)
        ref_voiced = ref_voiced[ref_voiced > 0]

        terminal_rise = None
        semitone_delta = None
        if term_voiced.size >= PROSODY_MIN_VOICED_FRAMES \
                and ref_voiced.size >= PROSODY_MIN_VOICED_FRAMES:
            f_term = float(np.median(term_voiced))
            f_ref = float(np.median(ref_voiced))
            if f_term > 0 and f_ref > 0:
                semitone_delta = 12.0 * float(np.log2(f_term / f_ref))
                terminal_rise = bool(semitone_delta >= PROSODY_RISE_SEMITONES)

        rate_n = min(samples.size, int(rate * PROSODY_RATE_ANALYSIS_SECONDS))
        reference_slice = samples[:rate_n]
        rms_terminal = float(np.sqrt(np.mean(np.square(terminal)))) if terminal.size else 0.0
        rms_ref = float(np.sqrt(np.mean(np.square(reference_slice)))) if reference_slice.size else 0.0
        energy_ratio = (rms_terminal / rms_ref) if rms_ref > 1e-9 else None

        speaking_rate = _estimate_speaking_rate(reference_slice, rate)

        return {
            "terminal_rise": terminal_rise,
            "semitone_delta": semitone_delta,
            "energy_ratio": energy_ratio,
            "speaking_rate": speaking_rate,
        }
    except Exception:
        return None










_QUESTION_TAIL_MARKERS = ("啊", "呀", "咧", "呢", "嘛", "未", "冇", "好唔好",
                          "得唔得", "係咪", "點樣", "可唔可以", "幾時")
_QUESTION_RE = re.compile(
    r"[？?]\s*$"
    r"|(?:" + "|".join(_QUESTION_TAIL_MARKERS) + r")[，,]?\s*$"
    r"|\b(?:what|why|how|who|when|where|is it|can you|could you|do you|does it)\b",
    re.IGNORECASE)


def _looks_like_genuine_question(text):
    return bool(text) and bool(_QUESTION_RE.search(text.strip()))


def check_prosody_readiness():
    pairs = _match_recordings_to_text()
    questions = [p for p in pairs if _looks_like_genuine_question(p[1])]
    ready = len(questions) >= PROSODY_READY_N
    return {
        "ready": ready,
        "have": len(questions),
        "need": PROSODY_READY_N,
        "total_retained": len(pairs),
        "detail": (f"{len(questions)}/{PROSODY_READY_N} genuine-question "
                    f"takes retained (of {len(pairs)} matched takes total)"),
    }


def _load_wav(path):
    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        n = w.getnframes()
        raw = w.readframes(n)
        width = w.getsampwidth()
    if width == 2:
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
    else:
        samples = np.frombuffer(raw, dtype=np.uint8).astype(np.float64)
    return samples, rate


def run_prosody_reevaluation():
    """RUN step — only called by run_gate() once check_prosody_readiness()
    reports ready. Re-runs PR #30's exact measurement against however many
    genuine-question takes are now retained: statements feed the same
    false-positive check PR #30 reported, and questions feed a recall
    check against the no-prosody (text-only) baseline already recorded in
    history.jsonl for that same take. No fresh ASR decode (history.jsonl's
    own transcript is reused instead of re-transcribing), no GPU, no
    ollama, no mic — CPU-only numpy on already-saved wavs, same budget PR
    #30's own measurement used.
    """
    pairs = _match_recordings_to_text()
    rows = []
    for wav_path, text in pairs:
        try:
            samples, rate = _load_wav(wav_path)
        except (OSError, wave.Error):
            continue
        features = extract_prosody_features(samples, rate)
        is_question = _looks_like_genuine_question(text)
        baseline_flagged = bool(re.search(r"[？?]\s*$", text.strip()))
        rows.append({
            "wav": os.path.basename(wav_path),
            "is_genuine_question": is_question,
            "baseline_already_flagged": baseline_flagged,
            "terminal_rise": None if features is None else features.get("terminal_rise"),
            "semitone_delta": None if features is None else features.get("semitone_delta"),
        })

    statements = [r for r in rows if not r["is_genuine_question"]]
    questions = [r for r in rows if r["is_genuine_question"]]
    false_positives = [r for r in statements if r["terminal_rise"] is True]




    recall_opportunities = [r for r in questions if not r["baseline_already_flagged"]]
    recovered = [r for r in recall_opportunities if r["terminal_rise"] is True]

    fp_rate = (len(false_positives) / len(statements)) if statements else None
    recall = (len(recovered) / len(recall_opportunities)) if recall_opportunities else None
    beats_baseline = (recall is not None and recall > 0.0) if recall is not None else None

    if not recall_opportunities:
        verdict = (f"re-deferred: {len(questions)} genuine-question take(s) "
                    "matched but the text-only baseline already caught all of "
                    "them (0 misses), so recall still has no denominator")
    else:
        verdict = (f"fp_rate={fp_rate:.0%} (n={len(statements)}), "
                    f"recall={recall:.0%} (n={len(recall_opportunities)}), "
                    f"beats_no_prosody_baseline={beats_baseline}")

    return {
        "ran": True,
        "n_statements": len(statements),
        "n_questions": len(questions),
        "n_recall_opportunities": len(recall_opportunities),
        "false_positive_rate": fp_rate,
        "recall": recall,
        "beats_baseline": beats_baseline,
        "verdict": verdict,
    }






















FILLER_STRIP_READY_N = 1

_FILLER_STRIP_LINE_RE = re.compile(
    r"^(?P<ts>\S+) filler-strip token=(?P<token>'[^']*'|\"[^\"]*\") "
    r"decision=(?P<decision>\S+) tail_rms=(?P<rms>[\d.eE+-]+) "
    r"threshold=(?P<threshold>[\d.eE+-]+) window_s=(?P<window>[\d.eE+-]+) "
    r"take_id=(?P<take_id>\S+)")


def _read_filler_strip_events():
    events = []
    try:
        with open(SERVER_LOG_PATH, "r", encoding="utf-8") as fh:
            for line in fh:
                m = _FILLER_STRIP_LINE_RE.search(line)
                if m:
                    events.append(m.groupdict())
    except OSError:
        pass
    return events


def check_filler_strip_readiness():
    events = _read_filler_strip_events()
    stripped = [e for e in events if e["decision"] == "stripped"]
    ready = len(stripped) >= FILLER_STRIP_READY_N
    return {
        "ready": ready,
        "have": len(stripped),
        "need": FILLER_STRIP_READY_N,
        "total_events_logged": len(events),
        "detail": f"{len(stripped)}/{FILLER_STRIP_READY_N} decision=stripped events logged",
    }


def run_filler_strip_reevaluation():
    """RUN step — only called once check_filler_strip_readiness() reports
    ready (at least one decision=stripped event exists). Surfaces every
    stripped event, matched to its source wav when still retained (by
    `take_id` if the caller supplied one, else by nearest capture
    timestamp — PR #35 notes take_id has no current caller, so timestamp
    matching is the practical path today), for HUMAN confirmation.

    "Genuine non-speech, correctly stripped" vs "real content, false
    strip" is a by-ear judgment this module does not invent an automated
    verdict for (D1: uncertain -> no automated verdict, never guess) — it
    reports the evidence a human needs to confirm each one, exactly what
    PR #35 itself asked for ("pull the FIRST real decision=stripped line
    once one shows up and confirm by ear").
    """
    events = _read_filler_strip_events()
    stripped = [e for e in events if e["decision"] == "stripped"]
    retained_by_stem = {_stem_to_dt(p): p for p in _retained_recordings()}

    rows = []
    for e in stripped:
        take_id = e["take_id"]
        wav_path = None
        if take_id and take_id != "n/a":
            for p in _retained_recordings():
                if os.path.splitext(os.path.basename(p))[0] == take_id:
                    wav_path = p
                    break
        if wav_path is None:
            try:
                ev_dt = datetime.datetime.strptime(e["ts"][:19], "%Y-%m-%dT%H:%M:%S")
            except ValueError:
                ev_dt = None
            if ev_dt is not None and retained_by_stem:
                best_dt = min(retained_by_stem, key=lambda d: abs((d - ev_dt).total_seconds())
                              if d else float("inf"))
                if best_dt is not None and abs((best_dt - ev_dt).total_seconds()) <= 30:
                    wav_path = retained_by_stem[best_dt]
        rows.append({
            "ts": e["ts"],
            "token": e["token"].strip("'\""),
            "tail_rms": float(e["rms"]),
            "threshold": float(e["threshold"]),
            "take_id": take_id,
            "matched_wav": os.path.basename(wav_path) if wav_path else None,
        })

    confirmable = [r for r in rows if r["matched_wav"]]
    verdict = (
        f"{len(rows)} stripped event(s) logged; {len(confirmable)} still "
        "have a source wav retained for by-ear confirmation. False-strip "
        "rate = confirmed-false / total-stripped is computable by hand from "
        "this report once each row is confirmed — not computed here "
        "(D1: uncertain -> no automated verdict)."
    )
    return {
        "ran": True,
        "n_stripped_total": len(rows),
        "n_confirmable_now": len(confirmable),
        "rows": rows,
        "verdict": verdict,
    }






REGISTRY = [
    ReadinessItem(
        item_id="pr30-prosody-recall",
        what=("PR #30 'prosody to the polish layer' — terminal-pitch-rise "
              "signal that upgrades a trailing 。/. to ？/? in polish.py — "
              "open, unmerged."),
        why_deferred=("2026-07-26 measurement: 40% false-positive rate "
                       "(4/10) on real statement takes; recall unmeasurable "
                       "— 0 of the 10 available takes were genuine "
                       "questions."),
        derivation=("PROSODY_READY_N=10 matches the sample size PR #30's "
                     "own false-positive measurement already used, so "
                     "recall is estimated at the same granularity as the "
                     "rate it will be compared against — see the constant's "
                     "own comment above."),
        check_fn=check_prosody_readiness,
        run_fn=run_prosody_reevaluation,
    ),
    ReadinessItem(
        item_id="pr35-filler-strip-false-rate",
        what=("strip_trailing_filler()'s trailing-VAD gate in polish.py "
              "(TRAILING_VAD_RMS=0.001, sub-frame-max aggregation, shipped "
              "in PR #35) — its false-strip rate."),
        why_deferred=("PR #35 (merged): tuned the gate on measured evidence "
                       "but could not compute a false-strip RATE — no "
                       "decision=stripped event had ever been logged "
                       "(0/0)."),
        derivation=("FILLER_STRIP_READY_N=1 is the mathematical minimum for "
                     "the rate's denominator to exist at all, not a chosen "
                     "sample size — deliberately not matched to item 1's "
                     "n=10, which is tied to a different measurement's "
                     "granularity need. See the constant's own comment "
                     "above."),
        check_fn=check_filler_strip_readiness,
        run_fn=run_filler_strip_reevaluation,
    ),
]


if __name__ == "__main__":
    import sys
    force = "--force" in sys.argv
    out = maybe_run(force=force)
    if out is None:
        print("readiness_gate: not due (or busy) — nothing to do this cycle")
    else:
        for o in out:
            print(json.dumps(o, ensure_ascii=False))

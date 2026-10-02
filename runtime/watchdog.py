#!/usr/bin/env python3
"""Latency watchdog with auto-remediation (standing order, 2026-07-22).

Tinho: "if it loads more than 10s, find some way to improve it so the
slow-transcribe problem never repeats; do it auto and remember it."

The recurring slowness class has three known root causes, all of which have
struck at least once:

  1. the live qwen model gets evicted from memory (keep_alive lapse, another
     model loaded during benchmarks, ollama restart) — the next dictation
     pays a ~10s cold reload;
  2. the injected-notes prompt snapshot is missing/churning, so every
     dictation is an ollama prompt-cache miss (p95 18s class, SLOWNESS
     INCIDENT 2026-07-22);
  3. style.notes / style.subs regrow past the consolidation thresholds and
     inflate the prompt (same incident, second wave).

So the watchdog watches the one symptom all three share — tail-path polish
latency — and when it trips, runs a self-heal that addresses exactly those
three causes, logs what it found/did to server.log (`watchdog:` lines) AND
to the dictionary changelog (the GitHub-sync channel Tinho already reads),
and — only if a heal didn't stick and it trips AGAIN shortly after — shows a
short local macOS banner so Tinho knows the system saw the slowness and what
it tried. Quiet rule respected: one banner per persistent episode, never per
trigger.

Trigger (pure logic, unit-tested in test_watchdog.py):
  - p95 of the last 10 recorded polish latencies > LATENCY_TARGET (3s)
  - OR 2 consecutive dictations > 6s                     (fast path for a
    sudden cliff — cold model — without waiting for 10 samples)
  - gated by a 15-min cooldown so a healing pass gets time to take effect.

RETUNED 2026-07-22 (stability task, second pass): the original standing order
above was literally ">10s"; Tinho's later 3s TARGET
(DICTATION_LATENCY_TARGET, default 3.0) supersedes it as "the 3s target
class" — 2 consecutive >6s or p95>3s over the last 10 — consistent with the
same-day root-cause fix (INCIDENT: powerd SIGTERM cycle from the mic's
sleep-prevention assertion, see server.py) and the periodic self-test below
that proves the target is met even between real dictations.

Only latencies where the LLM was actually in play are recorded (polish
toggled off / ultra-short deterministic takes tell us nothing about model
health). Timeouts DO count — they arrive as ~18s measurements.

The heal itself runs on a daemon thread — never on the paste path.

CONTENT-LOSS CANARY (2026-07-25/26, additive): the self-test below only ever
proves LATENCY on canned audio — it never proves that a real take's live
output actually contains what was said (see the incident in
content_guard.py's docstring: STOP dropped a take's final words and this
self-test reported PASS throughout). `content_guard.maybe_run()` is called
from `selftest_worker`'s own loop below, gated by the same busy_fn() that
loop already uses, so it inherits the exact same "never while recording"
guarantee without a new mechanism — see content_guard.py for the check
itself.
"""
import datetime
import json
import os
import statistics
import subprocess
import threading
import time
import urllib.request
from collections import deque

import content_guard
import readiness_gate
import recording_gate
import stop_integrity_guard

HERE = os.path.dirname(os.path.abspath(__file__))





LATENCY_TARGET = float(os.environ.get("DICTATION_LATENCY_TARGET", "3.0"))
WINDOW = 10
P95_LIMIT = LATENCY_TARGET
P95_MIN_SAMPLES = 5
CONSEC_LIMIT = 6.0
CONSEC_N = 2
COOLDOWN = 15 * 60
ESCALATE_WINDOW = 45 * 60











SOFT_REGRESSION_THRESHOLD_SECONDS = float(
    os.environ.get("DICTATION_SOFT_REGRESSION_THRESHOLD_SECONDS", "4.5"))

OLLAMA_PS_URL = "http://localhost:11434/api/ps"


class LatencyWatchdog:
    """Pure trigger logic — no I/O, injectable clock, fully unit-testable."""

    def __init__(self, window=WINDOW, p95_limit=P95_LIMIT,
                 min_samples=P95_MIN_SAMPLES, consec_limit=CONSEC_LIMIT,
                 consec_n=CONSEC_N, cooldown=COOLDOWN,
                 escalate_window=ESCALATE_WINDOW, clock=time.time):
        self.buf = deque(maxlen=window)
        self.p95_limit = p95_limit
        self.min_samples = min_samples
        self.consec_limit = consec_limit
        self.consec_n = consec_n
        self.cooldown = cooldown
        self.escalate_window = escalate_window
        self.clock = clock
        self.consec = 0
        self.last_heal = 0.0
        self.healed_recently_when_tripped = False

    def p95(self):
        if not self.buf:
            return 0.0
        vals = sorted(self.buf)
        idx = max(0, int(round(0.95 * len(vals))) - 1)
        return vals[idx]

    def record(self, elapsed):
        """Feed one tail-path polish latency. Returns a reason string when a
        self-heal should fire NOW, else None."""
        self.buf.append(float(elapsed))
        if elapsed > self.consec_limit:
            self.consec += 1
        else:
            self.consec = 0
        reason = None
        if self.consec >= self.consec_n:
            reason = (f"{self.consec} consecutive dictations "
                      f">{self.consec_limit:.0f}s (last {elapsed:.1f}s)")
        elif len(self.buf) >= self.min_samples and self.p95() > self.p95_limit:
            reason = (f"p95 {self.p95():.1f}s over last {len(self.buf)} "
                      f"dictations >{self.p95_limit:.0f}s")
        if reason is None:
            return None
        now = self.clock()
        if now - self.last_heal < self.cooldown:
            return None

        self.healed_recently_when_tripped = (
            self.last_heal > 0 and now - self.last_heal < self.escalate_window)
        self.last_heal = now
        self.consec = 0
        return reason




def _model_resident(model):
    """Is `model` currently loaded in ollama? None = ollama unreachable."""
    try:
        with urllib.request.urlopen(OLLAMA_PS_URL, timeout=5) as resp:
            models = json.load(resp).get("models") or []
        return any(m.get("name") == model or m.get("model") == model
                   for m in models)
    except Exception:
        return None


def _rewarm_one(polish_mod, model):
    payload = {"model": model, "prompt": "ok", "stream": False,
               "keep_alive": "8h", "options": {"num_predict": 1}}
    req = urllib.request.Request(
        polish_mod.OLLAMA_URL, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=180) as resp:
        resp.read()
    return time.time() - t0


def _hybrid_models(polish_mod):
    """The distinct models the live server actually uses — MODEL always, plus
    FAST_MODEL when the hybrid is on (FAST_MODEL != MODEL). De-duplicated and
    ordered (fast first: it's the foreground path Tinho waits on)."""
    fast = getattr(polish_mod, "FAST_MODEL", polish_mod.MODEL)
    return [fast] + ([polish_mod.MODEL] if polish_mod.MODEL != fast else [])


def _rewarm(polish_mod):
    """Reload + re-pin the live model(s) exactly the way the server does.
    With the hybrid on this re-warms BOTH qwen2.5:3b (foreground) and
    qwen2.5:7b (background), so a self-heal can't leave either cold."""
    return sum(_rewarm_one(polish_mod, m) for m in _hybrid_models(polish_mod))











































































def _last_real_dictation_dt(history_path=None):
    """Timestamp of the most recent real dictation, from history.jsonl's own
    mtime — the exact same read `real_usage_since()` already performs, not a
    second usage signal. None if there has never been one / the file is
    unreadable."""
    mtime = _history_mtime(history_path)
    if mtime is None:
        return None
    return datetime.datetime.fromtimestamp(mtime)


def plausibly_about_to_dictate(now=None, history_path=None):
    """Usage-driven proxy for "is Tinho plausibly about to dictate again
    soon": true iff a real dictation happened within
    SELFTEST_IDLE_BACKSTOP_SECONDS of `now`. There is no way to observe the
    future, so — exactly like selftest_should_run()'s own usage gate — this
    proxies "about to" with "recently did", and reuses the SAME idle
    boundary selftest_backstop_due() already established (2026-07-26,
    derived from the measured GPU-cost tradeoff in that constant's own
    comment above) rather than inventing a second "is he active" number."""
    now = now or datetime.datetime.now()
    last = _last_real_dictation_dt(history_path)
    if last is None:
        return False
    return (now - last).total_seconds() <= SELFTEST_IDLE_BACKSTOP_SECONDS


def _release_one(polish_mod, model):
    """Explicit "not needed right now" — keep_alive=0 unloads `model`
    immediately regardless of whatever keep_alive value pinned it before;
    ollama evaluates keep_alive per-request, so this is never in conflict
    with polish.py's own keep_alive="8h" on its next real call (editor.py's
    _shadow_propose_local already establishes this exact keep_alive=0
    pattern for a different model). Empty prompt: nothing to generate, this
    call exists purely to change residency."""
    payload = {"model": model, "prompt": "", "stream": False, "keep_alive": 0}
    req = urllib.request.Request(
        polish_mod.OLLAMA_URL, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        resp.read()


def ensure_models_resident(polish_mod, log=print, busy_fn=None,
                           now=None, history_path=None, force=False):
    """Proactive half. No-op unless plausibly_about_to_dictate(): only pays
    the re-warm cost when there has been recent real usage. Never contends
    with foreground transcription (D58) — busy_fn (defaults to
    recording_gate.busy) gates every call, checked before touching ollama
    at all. Cheap by design: the residency CHECK is a handful of bytes over
    localhost (/api/ps, no GPU, no generation); only an ACTUALLY evicted
    model pays the compute cost of a real re-warm."""
    if busy_fn is None:
        busy_fn = recording_gate.busy
    try:
        if busy_fn():
            return []
    except Exception:
        return []
    if not force and not plausibly_about_to_dictate(
            now=now, history_path=history_path):
        return []
    healed = []
    for model in _hybrid_models(polish_mod):
        resident = _model_resident(model)
        if resident is not False:
            continue
        try:
            t = _rewarm_one(polish_mod, model)
            healed.append((model, t))
            log(f"model-residency: {model} was evicted between polls — "
                f"re-warmed in {t:.1f}s ahead of the next real dictation")
        except Exception as exc:
            log(f"model-residency: re-warm of {model} failed: {exc}")
    return healed


def release_models_if_idle(polish_mod, log=print, busy_fn=None,
                           now=None, history_path=None):
    """Complementary half. No-op while plausibly_about_to_dictate() — only
    acts once that SAME window has elapsed with no real dictation. Same
    busy_fn/D58 gate as ensure_models_resident()."""
    if busy_fn is None:
        busy_fn = recording_gate.busy
    try:
        if busy_fn():
            return []
    except Exception:
        return []
    if plausibly_about_to_dictate(now=now, history_path=history_path):
        return []
    released = []
    for model in _hybrid_models(polish_mod):
        resident = _model_resident(model)
        if resident is not True:
            continue
        try:
            _release_one(polish_mod, model)
            released.append(model)
            log(f"model-residency: released {model} — idle "
                f"{SELFTEST_IDLE_BACKSTOP_MINUTES}min+ since the last real "
                "dictation, freed now rather than left to the 8h clock")
        except Exception as exc:
            log(f"model-residency: release of {model} failed: {exc}")
    return released


def notify_fyi(body, log):
    """Show one quiet, local macOS banner for a persistent failure.

    This deliberately has no URL/action and never invokes a browser.  The
    incident and changelog have already been written by the caller; this is
    only the lightweight surface that lets Tinho notice them without losing
    the page or app he is currently using.
    """
    try:
        proc = subprocess.run(
            ["osascript", "-e",
             "on run argv\n"
             "display notification (item 1 of argv) with title \"Dictation\"\n"
             "end run",
             body[:500]],
            capture_output=True, text=True, timeout=10)
        if proc.returncode == 0:
            return True
        log(f"watchdog: local notification failed rc={proc.returncode}")
        return False
    except Exception as exc:
        log(f"watchdog: local notification failed: {exc}")
        return False


def self_heal(reason, escalate, polish_mod, editor_mod, log,
              recovery_hook=None):
    """The remediation pass. Idempotent; every step is individually guarded
    so one failure never blocks the rest. Returns the findings list.

    Recording-absolute-priority guarantee (2026-07-23): this can be triggered
    by a real dictation's OWN latency (watchdog_observe), i.e. right when a
    recording may just have ended — wait (briefly; this is meant to be a fast
    fix, not indefinite housekeeping) for recording_gate to clear first."""
    recording_gate.wait_until_idle(max_wait=30, log=log)
    findings = []

    if recovery_hook is not None:
        try:
            recovered = recovery_hook()
            if recovered:
                findings.extend(recovered)
        except Exception as exc:
            findings.append(f"recovery hook failed: {exc}")

















    for model in _hybrid_models(polish_mod):
        resident = _model_resident(model)
        if resident is None:
            findings.append(f"ollama unreachable on /api/ps (checking {model})")

            break
        if resident is True:
            continue
        findings.append(f"{model} was EVICTED from memory")
        try:
            t = _rewarm_one(polish_mod, model)
            findings.append(f"re-warmed {model} in {t:.1f}s (keep_alive 8h) "
                            "— eviction was the likely cause")
        except Exception as exc:
            findings.append(f"re-warm of {model} FAILED: {exc}")


    try:
        if not os.path.exists(polish_mod.NOTES_SNAPSHOT_PATH):
            editor_mod._ensure_notes_snapshot()
            findings.append("notes snapshot was MISSING — reseeded "
                            "(every dictation was a prompt-cache miss)")
        else:
            findings.append("notes snapshot present (prompt cache stable)")
    except Exception as exc:
        findings.append(f"snapshot check failed: {exc}")




    try:
        doc = editor_mod.load_doc()
        notes = len((doc.get("style") or {}).get("notes", []))
        subs = len((doc.get("style") or {}).get("subs", []))
        editor_mod._maybe_consolidate_for_load(doc)
        editor_mod._maybe_consolidate_subs_for_load(doc)
        findings.append(f"consolidation checked (notes={notes} subs={subs})")
    except Exception as exc:
        findings.append(f"consolidation check failed: {exc}")

    summary = "; ".join(findings)
    log(f"watchdog: TRIPPED ({reason}) -> {summary}")


    try:
        editor_mod.log_watchdog_event(f"慢速自癒：{reason} → {summary}")
    except Exception as exc:
        log(f"watchdog: changelog append failed: {exc}")


    if escalate:
        notify_fyi(f"fyi: dictation polish still slow after self-heal. "
                   f"Trigger: {reason}. Tried: {summary}", log)
    return findings


def start_heal_async(reason, escalate, polish_mod, editor_mod, log,
                     recovery_hook=None):
    t = threading.Thread(
        target=lambda: _safe_heal(reason, escalate, polish_mod, editor_mod,
                                  log, recovery_hook),
        daemon=True, name="watchdog-heal")
    t.start()
    return t


def _safe_heal(reason, escalate, polish_mod, editor_mod, log,
               recovery_hook=None):
    try:
        self_heal(reason, escalate, polish_mod, editor_mod, log,
                  recovery_hook=recovery_hook)
    except Exception as exc:
        try:
            log(f"watchdog: self-heal crashed: {exc}")
        except Exception:
            pass


















PORTAUDIO_WEDGE_TRIP_COUNT = int(
    os.environ.get("DICTATION_PORTAUDIO_WEDGE_TRIP_COUNT", "2"))
PORTAUDIO_WEDGE_WINDOW_SECONDS = float(
    os.environ.get("DICTATION_PORTAUDIO_WEDGE_WINDOW_MINUTES", "30")) * 60
PORTAUDIO_WEDGE_COOLDOWN_SECONDS = float(
    os.environ.get("DICTATION_PORTAUDIO_WEDGE_COOLDOWN_MINUTES", "15")) * 60


class PortAudioWedgeWatchdog:
    """Counts PortAudio stream wedges and callback-dead rebuilds (fed by
    server.py's recovery observer) and trips once
    PORTAUDIO_WEDGE_TRIP_COUNT of them land within
    PORTAUDIO_WEDGE_WINDOW_SECONDS of each other. Cooldown-gated the same
    way LatencyWatchdog is, so a flapping device announces itself once per
    episode rather than on every single wedge."""

    def __init__(self, trip_count=PORTAUDIO_WEDGE_TRIP_COUNT,
                 window=PORTAUDIO_WEDGE_WINDOW_SECONDS,
                 cooldown=PORTAUDIO_WEDGE_COOLDOWN_SECONDS, clock=time.time):
        self.trip_count = trip_count
        self.window = window
        self.cooldown = cooldown
        self.clock = clock
        self.events = deque()
        self.last_trip = 0.0

    def record(self, op):
        """Feed one wedge occurrence (op = 'start'/'stop'/'close'). Returns
        a reason string when the trip condition is met, else None."""
        now = self.clock()
        self.events.append(now)
        while self.events and now - self.events[0] > self.window:
            self.events.popleft()
        if len(self.events) < self.trip_count:
            return None
        if now - self.last_trip < self.cooldown:
            return None
        self.last_trip = now
        return (f"{len(self.events)} PortAudio stream failures (latest: "
                f"{op}) within {self.window / 60:.0f}min")


def announce_portaudio_wedge(reason, editor_mod, log):
    """Make recurring PortAudio recovery failures visible through the existing
    muted channels: server.log (always),
    the changelog (editor.log_watchdog_event, same GitHub-sync channel the
    latency watchdog and the stuck-lock recovery already use), and a
    local macOS banner — same notify_fyi() surface the latency watchdog uses,
    never a URL or dashboard action (D10a)."""
    log(f"watchdog: TRIPPED (portaudio-wedge: {reason})")
    if editor_mod is not None:
        try:
            editor_mod.log_watchdog_event(
                f"PortAudio 串流失敗：{reason} — Recorder.reopen() 已嘗試 "
                "重建並驗證新串流；重複出現代表裝置可能不穩定，值得留意。")
        except Exception as exc:
            log(f"watchdog: changelog append failed: {exc}")
    notify_fyi(
        f"fyi: dictation's mic stream has failed {reason}. Recovery retried "
        "and verified a replacement where possible; the audio device may be "
        "flaky and needs attention.", log)


































SELFTEST_STATE_PATH = os.path.join(HERE, ".selftest_state")
SELFTEST_INTERVAL = int(os.environ.get("DICTATION_SELFTEST_MINUTES", "5")) * 60
SELFTEST_POLL_SECONDS = 30
SELFTEST_ESCALATE_AFTER = 2



























SELFTEST_IDLE_BACKSTOP_MINUTES = int(
    os.environ.get("DICTATION_SELFTEST_IDLE_BACKSTOP_MINUTES", "60"))
SELFTEST_IDLE_BACKSTOP_SECONDS = SELFTEST_IDLE_BACKSTOP_MINUTES * 60
HISTORY_PATH = os.path.join(HERE, "history.jsonl")






SELFTEST_SAMPLES = {
    "short": {
        "text": "多謝晒幫我搞掂呢件事", "full": False,

        "target": 1.5,
    },
    "medium": {
        "text": ("今日開會傾咗個新功能嘅設計同埋下一步嘅計劃"
                  "希望下星期可以出到第一個版本俾大家睇下"),
        "full": False,



















        "target": float(os.environ.get("DICTATION_SELFTEST_MEDIUM_TARGET",
                                        "9.0")),
    },
    "long": {
        "text": ("今日同個客開會傾咗成個鐘頭，主要係傾緊個新項目嘅時間表同埋預算，"
                  "佢哋想我哋下個月之前交第一版嘅設計文件，同埋要約埋個技術團隊"
                  "傾下個架構應該點樣搭先至撐得住咁大嘅流量"),
        "full": True,



        "target": float(os.environ.get("DICTATION_SELFTEST_LONG_TARGET",
                                        "20.0")),
    },
}


def _selftest_state():
    try:
        with open(SELFTEST_STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _write_selftest_state(state):
    try:
        with open(SELFTEST_STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
    except OSError:
        pass


def selftest_due(state=None):
    """Pure-enough to unit test by passing `state` explicitly; reads the
    state file itself in production (mirrors editor._rule_audit_due)."""
    state = state if state is not None else _selftest_state()
    last = state.get("last")
    if not last:
        return True
    try:
        dt = datetime.datetime.fromisoformat(last)
    except (TypeError, ValueError):
        return True
    return (datetime.datetime.now() - dt).total_seconds() > SELFTEST_INTERVAL


def _history_mtime(history_path=None):





    if history_path is None:
        history_path = HISTORY_PATH
    try:
        return os.path.getmtime(history_path)
    except OSError:
        return None


def real_usage_since(last_run_iso, history_path=None):
    """True if history.jsonl has been modified (a real dictation appended a
    new entry) more recently than `last_run_iso` (the selftest's own last
    recorded run time). No prior run (`last_run_iso` falsy/unparseable)
    counts as "usage present" so the very first selftest of a fresh process
    still runs once selftest_due() fires — it never gates on usage before it
    has ever run. Read-only: only stats history.jsonl's mtime, never opens
    or parses it, and never touches server.py."""
    if not last_run_iso:
        return True
    mtime = _history_mtime(history_path)
    if mtime is None:
        return False
    try:
        last_dt = datetime.datetime.fromisoformat(last_run_iso)
    except (TypeError, ValueError):
        return True
    return datetime.datetime.fromtimestamp(mtime) > last_dt


def selftest_backstop_due(state=None):
    """True once SELFTEST_IDLE_BACKSTOP_SECONDS have elapsed since the last
    selftest run, independent of usage — the overnight safety net so a
    genuinely broken model is still caught even across a stretch with zero
    real dictations. A cycle that fires via the backstop also runs the
    "long" sample (see selftest_worker) for a full deep check, not just
    short/medium."""
    state = state if state is not None else _selftest_state()
    last = state.get("last")
    if not last:
        return True
    try:
        dt = datetime.datetime.fromisoformat(last)
    except (TypeError, ValueError):
        return True
    return (datetime.datetime.now() - dt).total_seconds() > SELFTEST_IDLE_BACKSTOP_SECONDS


def selftest_should_run(state=None):
    """Pure gating decision for a due cycle (item 5's usage gate): run only
    if there has been real usage since the selftest's own last run, OR the
    idle backstop has elapsed. Separated from selftest_worker's loop body
    so it's directly unit-testable, same pattern as SelfTestState.evaluate
    / selftest_due above."""
    state = state if state is not None else _selftest_state()
    return selftest_backstop_due(state) or real_usage_since(state.get("last"))


def selftest_should_run_long(state=None, consec_trips=0,
                             escalate_after=SELFTEST_ESCALATE_AFTER):
    """Pure gating decision for whether THIS cycle's run should include the
    "long" sample: the idle-backstop deep check, or the trip counter has
    already reached escalate_after (self-heal already failed that many
    times) — the escalation-probe behaviour described in SELFTEST_SAMPLES'
    comment above."""
    state = state if state is not None else _selftest_state()
    return selftest_backstop_due(state) or consec_trips >= escalate_after


def _sample_cfg(entry):
    """Normalise one SELFTEST_SAMPLES entry: either the {"text","full",
    "target"} dict form (current), or a bare string (legacy / test
    convenience) — text only, full=False, no per-sample target."""
    if isinstance(entry, str):
        return entry, False, None
    return entry.get("text", ""), bool(entry.get("full")), entry.get("target")


def _default_targets():
    """{sample_name: target_seconds} built from SELFTEST_SAMPLES' own "target"
    entries (falling back to the flat LATENCY_TARGET for legacy bare-string
    samples that carry no target of their own)."""
    targets = {}
    for name, entry in SELFTEST_SAMPLES.items():
        _, _, target = _sample_cfg(entry)
        targets[name] = LATENCY_TARGET if target is None else target
    return targets


class SelfTestState:
    """Pure trigger logic for the self-test's own escalation counter —
    separate from LatencyWatchdog's time-based cooldown/escalate-window
    because the self-test runs on a fixed cadence, not per-dictation.

    HONEST PER-SAMPLE TARGETS (2026-07-23): each sample can have ITS OWN
    target (`targets={name: seconds}`) instead of one flat number, because
    "medium" (tail full-rewrite, gates paste latency) and "long" (background
    segment full-rewrite, hidden latency) are not the same latency class —
    see SELFTEST_SAMPLES' comment. A run "trips" when the WORST OFFENDER
    (the sample furthest over its OWN target, by ratio — not just the
    biggest raw number, since "long" is naturally bigger than "medium" even
    when both are perfectly healthy) exceeds its target. This means a trip
    is always a genuine regression relative to what that sample's class can
    actually achieve, never the inherent cost of a longer input.

    `target=` (a single flat number) is still accepted for callers that want
    the old uniform-bar behaviour (and for tests) — it's exactly the
    `targets=None` + a single number applied to every sample. Consecutive
    trips (with no clean run between them) are counted; escalate goes True
    once `escalate_after` self-heals have already run and it STILL trips
    again — i.e. "escalate only if self-heal doesn't recover after 2 tries."
    """

    def __init__(self, target=None, targets=None,
                 escalate_after=SELFTEST_ESCALATE_AFTER):
        if targets is not None:
            self.targets = dict(targets)
            self._flat_target = None
        elif target is not None:
            self.targets = None
            self._flat_target = target
        else:
            self.targets = _default_targets()
            self._flat_target = None
        self.escalate_after = escalate_after
        self.consec_trips = 0

    def _target_for(self, name):
        if self.targets is not None:
            return self.targets.get(name, LATENCY_TARGET)
        return self._flat_target if self._flat_target is not None else LATENCY_TARGET

    def evaluate(self, latencies):
        """latencies: {sample_name: elapsed_seconds}. Returns
        (tripped, escalate, reason_or_None)."""
        if not latencies:
            return False, False, None
        worst_name = worst_elapsed = worst_target = None
        worst_ratio = 0.0
        for name, elapsed in latencies.items():
            target = self._target_for(name)
            ratio = elapsed / target if target else float("inf")
            if worst_name is None or ratio > worst_ratio:
                worst_name, worst_ratio = name, ratio
                worst_elapsed, worst_target = elapsed, target
        if worst_ratio <= 1.0:
            self.consec_trips = 0
            return False, False, None
        self.consec_trips += 1
        escalate = self.consec_trips > self.escalate_after
        reason = (f"selftest {worst_name} sample {worst_elapsed:.1f}s > "
                  f"target {worst_target:.1f}s (consecutive trip {self.consec_trips})")
        return True, escalate, reason


def _run_selftest_once(polish_mod, log, include_long=True):
    """Time the polish path on each canned sample. Never raises — a sample
    that errors counts as infinitely slow (trips immediately) rather than
    silently skipping the check. Each sample's own `full` flag (dict form)
    picks the same code path production actually uses for that length class
    (tail short-rewrite vs background segment-rewrite) — see SELFTEST_SAMPLES.

    `include_long=False` (2026-07-26 GPU-cost fix) skips the "long" sample
    entirely — it is not run and does not appear in the returned dict at
    all, so its 8.6s-measured GPU cost is only ever paid when the caller
    actually wants it (idle-backstop deep check or trip escalation — see
    selftest_worker). SelfTestState.evaluate() only ever judges the samples
    actually present in its input, so omitting "long" here never counts as
    a pass or a fail for it — it simply wasn't checked this cycle."""
    out = {}
    for name, entry in SELFTEST_SAMPLES.items():
        if name == "long" and not include_long:
            continue
        text, full, _target = _sample_cfg(entry)
        t0 = time.time()
        try:
            polish_mod.polish(text, use_llm=True, full=full, cancellable=False)
            out[name] = time.time() - t0
        except Exception as exc:
            log(f"selftest: {name} sample raised: {exc}")
            out[name] = float("inf")
    return out


def selftest_worker(polish_mod, editor_mod, busy_fn, log, lock_probe=None,
                    force_recover=None):
    """Daemon-thread loop: every SELFTEST_INTERVAL, off the hot path, prove
    the 3s TARGET still holds; self-heal (and, after repeated failure,
    escalate) exactly like the passive watchdog if it doesn't.

    STUCK-LOCK DETECTION (2026-07-23, incident postmortem — see server.py's
    RecoverableLock docstring): the latency samples below call polish()
    directly, which never touches server.py's request_lock at all — they
    prove the model is fast, never that a transcription can actually
    complete end-to-end through the real serialization path. `lock_probe`
    (a zero-arg callable returning (ok, elapsed, detail)) is server.py's way
    of handing this worker a real acquire-run-release probe without this
    module needing to know anything about sockets or locks. When it reports
    NOT ok, that IS the stuck-lock condition this whole feature exists to
    catch — call `force_recover` immediately (don't wait for the passive
    monitor) and skip that cycle's latency samples, since measuring
    polish() speed against a lock that just proved broken tells us nothing
    useful."""
    state = SelfTestState()
    targets_str = ", ".join(f"{k}={v:.1f}s" for k, v in
                            sorted(state.targets.items()))
    log(f"selftest: active, every {SELFTEST_INTERVAL // 60}min "
        f"(usage-gated, idle backstop {SELFTEST_IDLE_BACKSTOP_MINUTES}min), "
        f"targets: {targets_str}")
    while True:
        try:


















            content_guard.maybe_run(log=log, notify_fn=notify_fyi,
                                    busy_fn=busy_fn)











            readiness_gate.maybe_run(log=log, busy_fn=busy_fn)









            stop_integrity_guard.maybe_run(log=log, notify_fn=notify_fyi,
                                           busy_fn=busy_fn)
            if busy_fn():
                pass
            elif selftest_due():







                current_state = _selftest_state()
                backstop = selftest_backstop_due(current_state)
                if not selftest_should_run(current_state):
                    time.sleep(SELFTEST_POLL_SECONDS)
                    continue
                if lock_probe is not None:
                    ok, elapsed, detail = lock_probe()
                    if not ok:
                        log(f"selftest: STUCK-LOCK detected ({detail}, probe "
                            f"waited {elapsed:.1f}s)")
                        if force_recover is not None:
                            try:
                                force_recover()
                            except Exception as exc:
                                log(f"selftest: force_recover failed: {exc}")
                        _write_selftest_state(
                            {"last": datetime.datetime.now()
                             .isoformat(timespec="seconds")})
                        time.sleep(SELFTEST_POLL_SECONDS)
                        continue





                ensure_models_resident(
                    polish_mod, log=log, busy_fn=busy_fn, force=backstop)







                run_long = selftest_should_run_long(
                    current_state, state.consec_trips, state.escalate_after)
                latencies = _run_selftest_once(
                    polish_mod, log, include_long=run_long)
                summary = ", ".join(f"{k}={v:.1f}s" for k, v in latencies.items())
                log(f"selftest: ran ({summary})"
                    f"{' [backstop]' if backstop else ''}"
                    f"{' [long: escalation probe]' if run_long and not backstop else ''}")
                _write_selftest_state(
                    {"last": datetime.datetime.now().isoformat(timespec="seconds")})
                tripped, escalate, reason = state.evaluate(latencies)
                if tripped:
                    self_heal(reason, escalate, polish_mod, editor_mod, log)
                release_models_if_idle(polish_mod, log=log, busy_fn=busy_fn)
            else:



                ensure_models_resident(polish_mod, log=log, busy_fn=busy_fn)
                release_models_if_idle(polish_mod, log=log, busy_fn=busy_fn)
        except Exception as exc:
            log(f"selftest worker error (ignored): {exc}")
        time.sleep(SELFTEST_POLL_SECONDS)


def start_selftest_async(polish_mod, editor_mod, busy_fn, log, lock_probe=None,
                         force_recover=None):
    t = threading.Thread(
        target=selftest_worker,
        args=(polish_mod, editor_mod, busy_fn, log, lock_probe, force_recover),
        daemon=True, name="dictation-selftest")
    t.start()
    return t

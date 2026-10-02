#!/usr/bin/env python3
"""Judgment gate for learned dictionary corrections (2026-07-23).

PROBLEM this replaces: editor.py used to take a user correction (either the
post-paste Accessibility diff, or a manual "改正" edit) and hand it straight
to a cloud-Sonnet gatekeeper that could commit a dict/style/format rule the
same round-trip (learn_from_edit -> judge_with_ai -> apply_judgement, all
before the next dictation). That conflates two very different situations
that LOOK identical as a text diff:

  1. a true homophone mishear — the ASR picked the wrong word for something
     that sounds the same ("tell scale" -> "Tailscale") — a legitimate,
     reusable dictionary rule;
  2. a rephrase / meaning change — Tinho said one thing, then edited it to
     mean something else, or simply changed his mind — NOT a mishearing, and
     turning it into a global substitution rule would silently rewrite every
     future occurrence of a word he used on purpose.

A single edit is weak evidence either way. This module removes the
auto-promote step entirely: every observed correction is durably queued
(never dictionary.json), then reviewed in a batch pass, off the paste path,
by a LOCAL qwen classifier whose only job is "does this look like the same
sound misheard, or a different meaning?". Only pairs classified as a mishear
on >= PROMOTE_THRESHOLD independent occasions are ever handed to the caller's
promote_fn (which still goes through the existing admission-gate / rejected-
memory / single-Han protections in editor.py — this module only decides WHEN
something is evidence-worthy, never HOW dictionary.json is safe to mutate).

UPDATE (2026-07-26): pronunciation is now the primary discriminator, not the
sole vote of a local-qwen classifier. Tinho's own design ruling, after a
measured misclassification ("coat"->"code" called "rephrase" when it's a
genuine near-homophone mishear): same/near-same SOUND between "wrong" and
"right" is itself sufficient evidence of a mishearing, independent of what
an LLM guesses about "meaning". See phonetics.py for the language-agnostic
phonetic-key/distance interface and classify_correction_combined() below for
how it's layered ON TOP of (not instead of) the original classify_correction
LLM call -- phonetic closeness is a sufficient trigger for "mishear"; when
it isn't close (or isn't comparable -- a cross-script pair), the original
LLM classifier still runs and decides, exactly as before.

Also as of 2026-07-26, promotion no longer waits for repeated recurrence as
its only safety net ("wait for recurrence"): PROMOTE_THRESHOLD defaults to 1
(promote on the first confident "mishear" evidence) and every promotion is
instead gated on replay_verify() -- simulating the candidate rule against
every other already-approved correction and a bounded sample of raw
dictation history, promoting only if it makes nothing worse ("promote
cheaply, verify immediately", replacing counting with evidence). See
replay_verify() below.

Nothing here ever runs on, or blocks, the live transcription/paste path:
  - enqueue() is a pure file append (no network, no LLM) — cheap enough to
    call straight from an HTTP handler.
  - process_queue() (the only thing that calls the classifier) is meant to be
    invoked from a background worker only, and itself waits on
    recording_gate before making any ollama call, exactly like the other
    off-hot-path jobs in this codebase (shadow batch flush, sync push,
    consolidation).

Pure-ish module: the only I/O is the queue/state files and one local ollama
call; no dependency on editor.py's dictionary schema, so it's testable in
isolation with a stubbed classifier (see test_review_gate.py).
"""
import datetime
import json
import os
import threading
import urllib.request

import phonetics
import recording_gate
import rule_audit

HERE = os.path.dirname(os.path.abspath(__file__))




QUEUE_PATH = os.path.join(HERE, "review_queue.jsonl")


STATE_PATH = os.path.join(HERE, ".review_state.json")



HISTORY_PATH = os.path.join(HERE, "history.jsonl")

OLLAMA_URL = os.environ.get("DICTATION_OLLAMA_URL", "http://localhost:11434/api/generate")
CLASSIFY_MODEL = os.environ.get("DICTATION_REVIEW_MODEL", "qwen2.5:7b")
CLASSIFY_TIMEOUT = float(os.environ.get("DICTATION_REVIEW_TIMEOUT", "30"))








PROMOTE_THRESHOLD = int(os.environ.get("DICT_PROMOTE_N", "1"))





REPLAY_HISTORY_SAMPLE = int(os.environ.get("DICT_REPLAY_HISTORY_SAMPLE", "300"))




PHONETIC_THRESHOLD = float(os.environ.get("DICT_PHONETIC_THRESHOLD",
                                          str(phonetics.DEFAULT_THRESHOLD)))

_queue_lock = threading.Lock()

CLASSIFY_PROMPT = """你係語音轉文字字典嘅守門員。用家啱啱將轉寫出嚟嘅字「wrong」改成「right」。判斷呢個改動屬於邊一種：

- "mishear"：真.同音／近音聽錯 —— 語音辨識揀錯咗字，「wrong」同「right」讀音相同或者好接近（例如專有名詞、人名、工具名嘅串法錯咗）。呢種先至值得變成一條可以重用嘅字典規則。
- "rephrase"：用家改咗意思、改咗講法、或者改變咗主意 —— 「wrong」同「right」讀音唔同，只係用字或者句子結構唔同，唔係聽錯。呢種絕對唔可以變成全局替換規則，否則會誤傷第啲句子。

只輸出 JSON：{"verdict": "mishear" 或 "rephrase", "reason": "一句解釋"}，唔好有其他文字。

輸入：
"""


def enqueue(wrong, right, source, extra=None):
    """Append one raw correction observation to the durable review queue.

    Pure file append — no network, no LLM, safe to call directly from a
    request handler on the paste-adjacent (but not paste-blocking) path.
    Returns True if the entry was written, False on a no-op (identical
    strings, blank input) or an I/O failure.
    """
    wrong, right = (wrong or "").strip(), (right or "").strip()
    if not wrong or not right or wrong == right:
        return False
    entry = {"time": datetime.datetime.now().isoformat(timespec="seconds"),
             "wrong": wrong, "right": right, "source": source}
    if extra:
        entry.update(extra)
    with _queue_lock:
        try:
            with open(QUEUE_PATH, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return True
        except OSError:
            return False


def _read_queue():
    if not os.path.exists(QUEUE_PATH):
        return []
    rows = []
    with open(QUEUE_PATH, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _read_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            state = json.load(fh)
        if not isinstance(state, dict):
            return {"cursor": 0, "items": {}}
        state.setdefault("cursor", 0)
        state.setdefault("items", {})
        return state
    except (OSError, json.JSONDecodeError):
        return {"cursor": 0, "items": {}}


def _write_state(state):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _key(wrong, right):
    return f"{wrong}→{right}"


def _ollama_classify(wrong, right, model=None, timeout=None):
    """One local-qwen classification call. Returns the parsed response JSON
    (a dict), or None on ANY failure (network, timeout, malformed JSON) — the
    caller treats None as "undecided, try again next pass", never a crash.
    Only ever called from process_queue(), which itself is only ever called
    from a background worker — never from a request handler and never on the
    paste path."""
    prompt = CLASSIFY_PROMPT + json.dumps({"wrong": wrong, "right": right},
                                          ensure_ascii=False)
    payload = {
        "model": model or CLASSIFY_MODEL, "prompt": prompt, "stream": False,
        "format": "json", "keep_alive": "8h",
        "options": {"temperature": 0.1, "top_p": 0.9},
    }
    req = urllib.request.Request(
        OLLAMA_URL, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout or CLASSIFY_TIMEOUT) as resp:
            obj = json.loads(resp.read().decode("utf-8"))
        return json.loads(obj.get("response", "") or "{}")
    except Exception:
        return None


def classify_correction(wrong, right, model=None, timeout=None):
    """"mishear" / "rephrase" / None (undecided). Never raises.

    This is the ORIGINAL (pre-2026-07-26) classifier: a single local-qwen
    call, nothing else. Left unchanged and still directly usable/testable on
    its own (see test_classify_correction_parses_and_never_raises) — the
    phonetic layer added below calls INTO this, it does not replace it."""
    out = _ollama_classify(wrong, right, model=model, timeout=timeout)
    if not isinstance(out, dict):
        return None
    verdict = out.get("verdict")
    return verdict if verdict in ("mishear", "rephrase") else None


def phonetic_verdict(wrong, right, threshold=None):
    """"mishear" / "rephrase" / None (not comparable — blank input, or a
    cross-script pair with no single-language phonetic comparison; see
    phonetics.is_phonetically_close). Pure, offline, no network — safe to
    call unconditionally, including from the paste-adjacent path if ever
    needed later."""
    close = phonetics.is_phonetically_close(
        wrong, right, threshold=PHONETIC_THRESHOLD if threshold is None else threshold)
    if close is None:
        return None
    return "mishear" if close else "rephrase"


def classify_correction_combined(wrong, right, model=None, timeout=None,
                                 threshold=None, llm_classify=None):
    """The classifier process_queue actually uses by default as of
    2026-07-26. Tinho's design ruling: phonetic closeness between "wrong"
    and "right" is BY ITSELF sufficient evidence of a same-sound mishearing
    — it doesn't need an LLM's agreement, because a near-homophone pair
    (e.g. "coat"/"code") is a mishear no matter what an LLM guesses about
    "meaning" from the text alone (this is the exact bug being fixed: the
    old LLM-only classifier called that pair "rephrase").

    When phonetics is NOT close (or isn't comparable at all — a cross-script
    pair, or blank input), the ORIGINAL classify_correction local-qwen call
    still runs and decides, completely unchanged — "layer on top of the
    existing logic, don't delete it": phonetics only ever ADDS a new
    "mishear" trigger, it never suppresses the old signal's ability to still
    catch a mishearing phonetics' simplified consonant-skeleton misses (e.g.
    a multi-word ASR compression like "Ch jobb" -> "Cron job", where the
    two sides' spelled-out letters diverge more than the threshold even
    though the whole phrase truly was misheard).
    """
    llm_classify = llm_classify or classify_correction
    phon = phonetic_verdict(wrong, right, threshold=threshold)
    if phon == "mishear":
        return "mishear"
    return llm_classify(wrong, right, model=model, timeout=timeout)


def _read_history(limit=None):
    """Raw dictation takes, oldest-first, read-only. Missing file -> []."""
    if not os.path.exists(HISTORY_PATH):
        return []
    rows = []
    with open(HISTORY_PATH, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if limit:
        rows = rows[-limit:]
    return rows


def replay_verify(wrong, right, queue_rows=None, history_rows=None,
                  history_sample=None):
    """"promote cheaply, verify immediately" (2026-07-26). The OLD gate's
    only defense against learning a wrong rule was waiting for repeated
    "mishear" classifications — slow, and still not actual evidence the rule
    is SAFE, just that the classifier keeps agreeing with itself. Tinho's
    ruling: the fear was "learning something wrong", and that fear is
    answered by REPLAY, not a counter.

    Before a candidate promotes, simulate applying it against:
      (a) every OTHER already-approved correction in review_queue.jsonl —
          via rule_audit.replay_against_queue, which treats each approved
          correction's "right" text as a probe: if the candidate's "wrong"
          pattern shows up verbatim inside a DIFFERENT, already-approved
          correction with no evidence it was itself corrected there, the
          candidate would clobber legitimate approved text;
      (b) a bounded sample of recent raw dictation takes (history.jsonl) —
          via rule_audit.history_evidence, the exact same kept/fixed
          evidence scorer the weekly rule-QA audit already uses.

    Promotes only if neither probe finds real regression evidence — i.e.
    only if it makes nothing worse (never required to prove it makes
    anything BETTER; a brand-new, never-before-corrected candidate with no
    evidence either way already passes). See regression_check()'s docstring
    for the 2026-07-26 correction to what raw-history "kept" evidence
    actually means for a first-time candidate. Returns (ok: bool, detail:
    dict) so every decision is inspectable, never silent.

    rule_audit.py already IS the evidence-scoring "shared brain" (its own
    docstring's words) for exactly this kind of kept-vs-fixed judgement —
    this reuses it rather than re-implementing it here.

    This is a thin single-candidate wrapper over regression_check() (below)
    — the "verify at promotion time" call site. regression_check() itself
    is the reusable, many-candidates-at-once version that a standing
    regression suite calls (see test_regression_corrections.py): same
    evidence, same logic, one implementation.
    """
    queue_rows = _read_queue() if queue_rows is None else queue_rows
    history_rows = (_read_history(limit=history_sample or REPLAY_HISTORY_SAMPLE)
                    if history_rows is None else history_rows)
    report = regression_check([(wrong, right)], queue_rows=queue_rows,
                              history_rows=history_rows)
    row = report[0]
    detail = {"queue_kept": row["queue_kept"], "queue_fixed": row["queue_fixed"],
              "history_kept": row["history_kept"], "history_fixed": row["history_fixed"]}
    return (row["ok"], detail)


def regression_check(candidates, queue_rows=None, history_rows=None,
                     history_sample=None):
    """The DURABLE, many-candidates-at-once version of replay_verify's
    single-pair check (2026-07-26) — item 2 of the phonetics/gate rebuild:
    replay_verify answers "is THIS ONE candidate safe to promote right now";
    this answers "replay an arbitrary list of (wrong, right) candidates
    against the approved-corrections corpus and a history sample, and tell
    me which ones would make something worse" — usable from a one-off
    script (e.g. auditing every rule currently live in dictionary.json's
    terms) or, more importantly, from a standing pytest regression suite
    (see test_regression_corrections.py) that any future change to the
    learning/polish path runs against automatically. A change that makes a
    previously-safe candidate start failing here is exactly the regression
    this exists to catch — one evidence implementation
    (rule_audit.replay_against_queue / rule_audit.history_evidence) shared
    by both call sites, so there is only ever one answer to "would this rule
    make something worse".

    `candidates` is any iterable of (wrong, right) pairs. Returns a list of
    dicts, one per candidate, in input order: {wrong, right, ok, queue_kept,
    queue_fixed, history_kept, history_fixed}.

    NO-REGRESSION, NOT IMPROVEMENT (2026-07-26 correction — the "31 corrections
    -> 0 promoted rules wearing a new mask" bug). This function was ALREADY,
    structurally, a no-regression-only check before this note: `bad` never
    required `fixed > 0` anywhere — a brand-new candidate with zero evidence
    either way (kept=0, fixed=0) always passed. The actual defect was one
    level down, in what `ev_hist["kept"]` (rule_audit.history_evidence's
    "kept" count against RAW, never-curated history.jsonl text) means for a
    candidate that has NEVER been corrected by anyone: every one of its
    occurrences is, BY CONSTRUCTION, "uncorrected" (no rule exists yet to
    have fixed it), so `kept` was structurally guaranteed > 0 for any
    genuinely new mishearing with 1+ occurrences — indistinguishable from a
    common word's real, legitimate, unrelated uses. Confirmed case:
    "talkingken" -> "talking token" (10 occurrences, 4 rows, 2026-07-21
    through 2026-07-25, never once corrected — even the cloud-Sonnet shadow
    pass on 2 of the 4 rows still shows the uncorrected form) scored
    `history_kept=1` and failed, purely because nothing has ever fixed it —
    which is exactly the population a first-time term exists to serve, not
    evidence against it.

    The fix distinguishes what `ev_hist["kept"]` actually means by reusing
    the EXISTING common-word admission signal (rule_audit.is_common_wrong,
    the same predicate editor.py's admission gate already applies): a common
    English/Cantonese word genuinely DOES have many legitimate, unrelated
    uses, so an uncorrected occurrence of one really is regression risk
    (this is rule_audit.history_evidence's original, correct purpose for the
    weekly audit of an already-live COMMON-word rule — e.g. "教育"->"trading"
    zombie detection, still exercised unchanged by test_rule_audit.py). A
    specific, non-common wrong-key (already confirmed unusual enough to have
    passed that same predicate) essentially cannot have "many unrelated
    legitimate meanings" the way a common word can — repeated raw,
    uncorrected occurrences of it are overwhelmingly more likely to be more
    instances of the SAME not-yet-fixed mishearing than to be diverse
    legitimate content. So: for a common wrong-key, ev_hist["kept"] still
    blocks (protection preserved, unchanged in effect). For a non-common
    wrong-key, it no longer blocks on its own.

    rule_audit.history_evidence() ITSELF is deliberately left unmodified —
    it is the shared evidence scorer the weekly self-audit (rule_audit.
    classify_term / audit_doc) also depends on for exactly the common-word
    case above, and changing its general semantics would risk that
    consumer's already-tested behaviour. Only THIS caller's interpretation
    of the raw-history signal changes.

    ev_queue["kept"] (regression against an OTHER, ALREADY-APPROVED
    correction in review_queue.jsonl) is UNCHANGED and stays an unconditional
    hard blocker regardless of common/non-common — that signal comes from
    Tinho-approved text, a materially stronger evidentiary basis than raw,
    never-reviewed history, and loosening it was never the ask.
    """
    queue_rows = _read_queue() if queue_rows is None else queue_rows
    history_rows = (_read_history(limit=history_sample or REPLAY_HISTORY_SAMPLE)
                    if history_rows is None else history_rows)
    report = []
    for wrong, right in candidates:
        ev_queue = rule_audit.replay_against_queue(queue_rows, wrong, right)
        ev_hist = rule_audit.history_evidence(history_rows, wrong, right)
        hist_risk = ev_hist["kept"] > 0 and rule_audit.is_common_wrong(wrong, right)
        bad = ev_queue["kept"] > 0 or hist_risk
        report.append({"wrong": wrong, "right": right, "ok": not bad,
                       "queue_kept": ev_queue["kept"], "queue_fixed": ev_queue["fixed"],
                       "history_kept": ev_hist["kept"], "history_fixed": ev_hist["fixed"]})
    return report


def classify_corpus(rows, classify_fn=None, model=None):
    """Run a classifier over a fixed corpus of {wrong, right} pairs (no
    queue/state I/O, no promotion) and return each pair's verdict. The other
    half of item 2's standing regression suite: regression_check asks "does
    a candidate rule make something worse"; this asks "does the classifier
    still call this pair what it used to call it" — together they're what
    test_regression_corrections.py runs on every test invocation against the
    31 real corrections in testdata/corrections_31.jsonl, so a future change
    to the phonetic/classification logic that silently flips a
    previously-correct call is caught instead of shipped quietly."""
    classify = classify_fn or classify_correction_combined
    out = []
    for row in rows or []:
        wrong, right = row.get("wrong"), row.get("right")
        if not wrong or not right:
            continue
        out.append({"wrong": wrong, "right": right,
                    "verdict": classify(wrong, right, model=model)})
    return out


def _find_reverted_promotion(items, row_wrong, row_right):
    """If (row_wrong, row_right) is the exact reverse of an already-promoted,
    not-yet-quarantined pair, return that pair's (wrong, right); else None.

    This is how "if Tinho later manually edits that same substitution back,
    the rule auto-returns to quarantine" is detected: a promoted rule W->R
    means dictionary.json now auto-rewrites W to R. If Tinho then corrects a
    freshly-rewritten R back to W, that correction is observed here as a new
    review_queue.jsonl row with wrong=R, right=W — the exact reverse of the
    promoted pair's own key."""
    ent = items.get(_key(row_right, row_wrong))
    if ent and ent.get("promoted") and not ent.get("quarantined"):
        return ent["wrong"], ent["right"]
    return None


def process_queue(promote_fn, log=None, model=None, threshold=None,
                  classify_fn=None, gate=True, verify=True, replay_fn=None,
                  quarantine_fn=None):
    """The review pass — the ONLY place this module ever calls the classifier.

    Classifies every newly-queued correction (since the last saved cursor)
    with `classify_fn` (default: classify_correction_combined — phonetic
    closeness first, the original local-qwen call as fallback; see that
    function's docstring). A pair reaching `threshold` independent "mishear"
    classifications (default PROMOTE_THRESHOLD=1, i.e. "promote cheaply" —
    see the module docstring's 2026-07-26 update) is, unless `verify=False`,
    first run through `replay_fn` (default: replay_verify — "verify
    immediately") and only handed to `promote_fn(wrong, right, mishear_count)`
    if that replay finds no evidence the rule would clobber legitimate text.

    `promote_fn` returns True/False — this module never touches
    dictionary.json itself, so every existing safety invariant (admission
    gate, rejected memory, single-Han protection) lives entirely in the
    caller and is applied identically regardless of who proposed the rule.

    Independently of classification, every new row is also checked against
    already-promoted pairs for a REVERSAL (see _find_reverted_promotion): if
    Tinho corrects a freshly-autocorrected "right" back to the original
    "wrong", that promoted pair is misbehaving live evidence, not a new
    candidate — `quarantine_fn(wrong, right, reason)` is called (if given)
    and the pair's state flips out of "promoted" so it can never re-fire
    from this module again. `quarantine_fn` is None by default (a safe
    no-op — the reversal is still detected, logged, and marked internally
    even with no callback wired) since the actual dictionary.json quarantine
    mutation (editor.quarantine_rule) lives outside this module's ownership;
    see the PR body for the exact adapter to wire it in.

    `gate=True` (the production default) blocks on recording_gate before the
    first classifier call, so a review pass can never contend with a live
    dictation for the model/GPU — mirrors every other background job in this
    codebase (shadow batch flush, sync push, consolidation, watchdog heal).
    Tests pass gate=False since there's no real recording to wait on and no
    real ollama call to protect.

    Returns a summary dict — queued/new/classified/promoted/deferred/
    undecided/quarantined — so every decision is inspectable; nothing here
    is silent (the caller is expected to funnel `log` into its own log file
    too). A candidate that clears the mishear threshold but fails replay
    verification is folded into `deferred` (same bucket as a promote_fn
    decline — both mean "evidence said promote, a safety check said no").
    """
    log = log or (lambda *_a, **_k: None)
    classify = classify_fn or classify_correction_combined
    replay = replay_fn or replay_verify
    threshold = PROMOTE_THRESHOLD if threshold is None else threshold
    rows = _read_queue()
    state = _read_state()
    cursor = int(state.get("cursor", 0))
    items = state.setdefault("items", {})
    new_rows = rows[cursor:]
    summary = {"queued": len(rows), "new": len(new_rows), "classified": [],
              "promoted": [], "deferred": [], "undecided": [],
              "quarantined": []}
    if not new_rows:
        return summary
    if gate:
        recording_gate.wait_until_idle(log=log)
    now = datetime.datetime.now().isoformat(timespec="seconds")
    for row in new_rows:
        wrong, right = row.get("wrong"), row.get("right")
        if not wrong or not right:
            continue





        reverted = _find_reverted_promotion(items, wrong, right)
        if reverted:
            rev_wrong, rev_right = reverted
            rev_ent = items[_key(rev_wrong, rev_right)]
            rev_ent["promoted"] = False
            rev_ent["quarantined"] = True
            summary["quarantined"].append([rev_wrong, rev_right])
            reason = (f"Tinho manually reverted the promoted rule "
                     f"(observed correction {wrong}→{right})")
            log(f"review_gate: {rev_wrong}→{rev_right} reverted by Tinho — "
                "auto-quarantined")
            if quarantine_fn:
                try:
                    quarantine_fn(rev_wrong, rev_right, reason)
                except Exception as exc:
                    log(f"review_gate: quarantine_fn raised for "
                        f"{rev_wrong}→{rev_right}: {exc}")
            continue

        key = _key(wrong, right)
        ent = items.setdefault(key, {"wrong": wrong, "right": right,
                                     "mishear": 0, "rephrase": 0,
                                     "promoted": False, "first": now})
        if ent.get("promoted") or ent.get("quarantined"):
            continue
        verdict = classify(wrong, right, model=model)
        ent["last"] = now
        if verdict == "mishear":
            ent["mishear"] = int(ent.get("mishear", 0)) + 1
        elif verdict == "rephrase":
            ent["rephrase"] = int(ent.get("rephrase", 0)) + 1
        else:
            summary["undecided"].append([wrong, right])
            log(f"review_gate: undecided {wrong}→{right} (classifier failed)")
            continue
        summary["classified"].append([wrong, right, verdict])
        log(f"review_gate: classified {wrong}→{right} as {verdict} "
            f"(mishear={ent['mishear']} rephrase={ent['rephrase']})")
        if ent["mishear"] >= threshold:
            if verify:
                ok_to_try, detail = replay(wrong, right)
                if not ok_to_try:
                    summary["deferred"].append([wrong, right])
                    log(f"review_gate: {wrong}→{right} reached threshold but "
                        f"replay verification found risk (would worsen a "
                        f"past correction): {detail}")
                    continue
            try:
                ok = bool(promote_fn(wrong, right, ent["mishear"]))
            except Exception as exc:
                ok = False
                log(f"review_gate: promote_fn raised for {wrong}→{right}: {exc}")
            if ok:
                ent["promoted"] = True
                summary["promoted"].append([wrong, right])
                log(f"review_gate: PROMOTED {wrong}→{right} "
                    f"({ent['mishear']} mishear classifications >= {threshold}"
                    f"{', replay-verified' if verify else ''})")
            else:
                summary["deferred"].append([wrong, right])
                log(f"review_gate: {wrong}→{right} reached threshold but "
                    "promote_fn declined (gate/rejected/common-word)")
    state["cursor"] = len(rows)
    _write_state(state)
    return summary

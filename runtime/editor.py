#!/usr/bin/env python3
"""Local dictionary editor.

Serves a small page on 127.0.0.1 for viewing, editing and live-testing the
dictation dictionary. Loopback only — nothing is exposed off the machine.
"""

import base64
import datetime
import difflib
import hashlib
import json
import os
import shutil
import random
import re
import socket
import subprocess
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import content_guard
import polish as polish_mod
from maintenance import MaintenanceLane
import recording_gate
import review_gate
import rule_audit

HERE = os.path.dirname(os.path.abspath(__file__))
DICT_PATH = os.path.join(HERE, "dictionary.json")
HISTORY_PATH = os.path.join(HERE, "history.jsonl")
SOCKET_PATH = "/tmp/dictation.sock"
PORT = 8765

_maintenance = MaintenanceLane(log=lambda msg: _log(msg))


def submit_maintenance(name, fn, *args, dedupe=False, **kwargs):
    """Fire-and-forget entry point for every network/model housekeeping job."""
    return _maintenance.submit(name, fn, *args, dedupe=dedupe, **kwargs)


def ask_server(cmd, timeout=5):
    """Forward a command to the dictation server over its unix socket."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(SOCKET_PATH)
        s.sendall(cmd.encode("utf-8"))

        chunks = []
        while True:
            b = s.recv(4096)
            if not b:
                break
            chunks.append(b)
        s.close()
        return b"".join(chunks).decode("utf-8").strip()
    except OSError:
        return None


HOTKEY_PATH = os.path.join(HERE, "hotkey.json")


def read_hotkey():
    try:
        with open(HOTKEY_PATH, encoding="utf-8") as fh:
            return json.load(fh).get("triggers", [])
    except (OSError, json.JSONDecodeError):
        return [{"type": "modifier", "keycode": 54, "label": "右 ⌘"}]


def usage_stats():
    """Summarise how dictation is actually being used, from the history log."""
    rows = []
    if os.path.exists(HISTORY_PATH):
        with open(HISTORY_PATH, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    by_day, total_chars, longest = {}, 0, 0
    for r in rows:
        day = (r.get("time") or "")[:10]
        by_day[day] = by_day.get(day, 0) + 1
        n = len(r.get("text") or "")
        total_chars += n
        longest = max(longest, n)
    days = sorted(by_day.items())[-14:]
    return {
        "total": len(rows),
        "chars": total_chars,
        "avg": round(total_chars / len(rows)) if rows else 0,
        "longest": longest,
        "days": days,
    }


_CONTENT = re.compile(r"[A-Za-z0-9一-鿿]")



_TOKEN_RE = re.compile(r"[A-Za-z0-9']+|[一-鿿]")


def extract_corrections(old, new):
    """Diff a transcription against the user's hand-corrected version and pull
    out word-level substitutions — these are exactly the mishearings a
    dictionary rule can fix next time. Returns [(variant, correct), ...]."""
    to = [(m.group(), m.start(), m.end()) for m in _TOKEN_RE.finditer(old)]
    tn = [(m.group(), m.start(), m.end()) for m in _TOKEN_RE.finditer(new)]
    sm = difflib.SequenceMatcher(None, [t[0] for t in to], [t[0] for t in tn],
                                 autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "replace":
            continue


        o = old[to[i1][1]:to[i2 - 1][2]]
        n = new[tn[j1][1]:tn[j2 - 1][2]]
        if not o or not n or o == n:
            continue
        if len(o) > 25 or len(n) > 25:
            continue




        if not _CONTENT.search(o) or not _CONTENT.search(n):
            continue
        out.append((o, n))
    return out


def learn_corrections(old, new):
    """Queue diffed corrections for judgment-gate review (review_gate.py) —
    nothing ever goes straight into dictionary.json from a single edit
    anymore (GATEKEEPER REWRITE, 2026-07-23: a one-off correction is often
    not a mishearing at all — Tinho rephrased, or changed his mind — and the
    old immediate cloud-Sonnet judge couldn't tell the difference from a
    single sample). Each candidate is durably appended to review_queue.jsonl;
    a background pass (review_queue_worker) classifies it with a LOCAL qwen
    model and only promotes a pair into dictionary.json once it has been
    independently seen as a same-sound mishear DICT_PROMOTE_N times (default
    3). This function itself is a pure file append — no network, no LLM — so
    it's safe to call straight from the /api/correct handler.

    Returns the list of [variant, correct] pairs that were newly queued (for
    the dashboard's "已儲存" status line — it no longer means "added to
    dictionary.json", just "queued for review")."""
    cands = extract_corrections(old, new)
    if not cands:
        return []
    added = []
    for variant, correct in cands:
        if review_gate.enqueue(variant, correct, "correct"):
            added.append([variant, correct])
    if added:
        _log(f"review_gate: queued {len(added)} candidate(s) from a manual "
             f"correction: {added}")
    return added


LOG_PATH = os.path.join(HERE, "server.log")
CLAUDE_BIN = os.path.expanduser("~/.local/bin/claude")
REREVIEW_TIMEOUT_S = 20
_rereview_guard = threading.Lock()

JUDGE_PROMPT = """你係語音轉文字字典嘅守門員。用家啱啱喺轉寫文字度改正咗一啲字眼。以下每一對係 {"i": 編號, "wrong": 聽錯寫法, "right": 用家改成}。

將每一對分類做以下其中一種：

- "dict"：真.聽錯 — 專有名詞／工具名／人名／固定串法（例如 "tell scale"→"Tailscale"、"克勞德"→"Claude"）。會變成一條普通字詞替換規則。
- "style"：語氣／用詞偏好（例如粵語 比→畀、個→嘅、地→哋、口語字加減口字邊、大細楷習慣）。你必須提供一個「有上下文保護」嘅正則替換：{"i": 編號, "pattern": "...", "replace": "..."}。pattern 絕對唔可以係淨一個中文字（會誤傷全部句子）— 一定要帶上下文（前後瞻 lookahead/lookbehind 或者 ≥2 個字），例如 "比(?=你|我|佢|人|大家)" → "畀"。pattern 一定要包含 wrong 嗰個字。
- "format"：排版偏好（point form、編號 第一點/1.、分段、標點習慣）。提供 {"i": 編號, "note": "一句中文指示"}。
- "reject"：一次性嘅上下文改寫，或者 wrong 本身係常用詞（自動替換會誤傷第啲句子，例如 "how"→"now"）。

只輸出 JSON：{"dict": [編號...], "style": [{"i","pattern","replace"}...], "format": [{"i","note"}...], "reject": [編號...]}，唔好有其他文字。

"""

FORMAT_CAPTURE_PROMPT = """比較同一段說話「貼出嚟嘅版本」同「用家改成嘅版本」嘅版面結構（分行、點列、編號、段落、標點習慣）。如果 edited 反映咗一個「可以重用」嘅格式偏好，返回 {"note": "一句中文指示"}；如果淨係一次性改動，返回 {"note": null}。只輸出 JSON，唔好有其他文字。

"""

REREVIEW_PROMPT = """你係語音轉文字字典嘅守門員。Tinho 唔接受以下呢條 rule／想重新考慮佢。請重新判斷，你可以：
- 刪除佢（一次性／太危險）：{"action": "delete"}
- 收窄佢（加上下文保護，減少誤傷）：{"action": "narrow", "pattern": "...", "replace": "..."}（style_sub），或者 {"action": "narrow", "variant": "更精確嘅正則"}（term）
- 轉類型：term→style：{"action": "convert", "to": "style", "pattern": "...", "replace": "..."}；style→dict：{"action": "convert", "to": "dict", "wrong": "...", "right": "..."}
- 保留原樣：{"action": "keep"}

只輸出 JSON，唔好有其他文字。呢條 rule 係：
"""


def _log(msg):
    line = f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        return False
    return True













REPO = os.environ.get("DICT_REPO", "tinhohui/tinho-os")
DICT_FILE = os.environ.get("DICT_FILE", "dictation/dictionary.json")
SYNC_STATE_PATH = os.path.join(HERE, ".dict_sync_state")














SYNC_ENABLED = REPO.strip().lower() not in ("", "off")












































GH_API_HANG_BACKSTOP_S = 120


def _gh_api_run(args, **kwargs):
    """subprocess.run wrapper for every `gh api` call this module makes.
    Callers no longer pass their own `timeout` -- GH_API_HANG_BACKSTOP_S is
    applied here, uniformly, as the hang-recovery backstop described above
    (never a per-call tuning knob)."""
    kwargs.setdefault("timeout", GH_API_HANG_BACKSTOP_S)
    return subprocess.run(args, **kwargs)

_dict_lock = threading.RLock()
_dirty = threading.Event()
_push_now = threading.Event()


def load_doc():
    with _dict_lock:
        try:
            with open(DICT_PATH, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {"terms": {}, "pending": {}}





    migrate_style_notes_to_rules(doc)
    return doc


def _write_doc(doc):
    with _dict_lock:
        with open(DICT_PATH, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)


def save_doc(doc):
    """Persist a local change and schedule a push to the GitHub canonical copy."""
    _write_doc(doc)
    _dirty.set()
    _push_now.set()
    _maybe_consolidate_for_load(doc)
    _maybe_consolidate_subs_for_load(doc)


def append_changelog(doc, kind, desc, source):
    """Newest-first feed of every accept/remove, capped at 100."""
    cl = doc.setdefault("changelog", [])
    cl.insert(0, {"time": datetime.datetime.now().isoformat(timespec="seconds"),
                  "kind": kind, "desc": desc, "source": source})
    del cl[100:]








REJECTED_CAP = 200


def _rej_key(kind, wrong, right):
    """Canonical key for a dict-type candidate: the wrong->right pair."""
    return f"{wrong}→{right}"


def add_rejected(doc, kind, key, pattern=None):
    """Record a rejected rule (dedup by kind+key+pattern, cap REJECTED_CAP)."""
    rej = doc.setdefault("rejected", [])
    sig = (kind, key, pattern)
    if any((r.get("kind"), r.get("key"), r.get("pattern")) == sig for r in rej):
        return
    rej.insert(0, {"kind": kind, "key": key, "pattern": pattern,
                   "time": datetime.datetime.now().isoformat(timespec="seconds")})
    del rej[REJECTED_CAP:]


def is_rejected(rejected, kind, key):
    """True if a candidate of this kind/key was previously rejected."""
    return any(r.get("kind") == kind and r.get("key") == key for r in (rejected or []))


def _rejected_from_rule(rule):
    """Map a live-rule descriptor (as the dashboard/rereview pass it) to the
    (kind, key, pattern) it should be remembered as."""
    kind = rule.get("kind")
    if kind == "term":
        return "dict", _rej_key("dict", rule.get("variant"), rule.get("term")), None
    if kind == "style_sub":
        return "style", rule.get("pattern"), rule.get("pattern")
    if kind == "style_note":
        return "format", rule.get("note"), None
    return None


def _is_single_han(s):
    """A lone CJK char — far too dangerous as a bare global replace rule."""
    return len(s) == 1 and not s.isascii()


def _is_context_safe(pattern):
    """A style pattern must carry context: literal content within a
    lookaround-bounded match, or >=2 bare literal chars with no lookaround.

    2026-08-04 incident: the old check accepted ANY pattern containing a
    lookaround token, without checking whether anything was left to match
    once the lookarounds were stripped out. A PURE zero-width pattern like
    "(?<=決)(?=定)" (all lookaround, zero literal match span) passed as
    "safe" -- but re.sub on a zero-width match INSERTS at that position
    rather than replacing a span, so this promoted 12 live rules that each
    silently inserted garbage into every occurrence of a common word/
    phrase (決定 -> 決咁定, 完全 -> 完全都全, 是不 -> 是唔不, ...) and left
    400+ more of the same dangerous shape sitting in shadow_candidates.
    A lookaround only makes a match safer if there is still an actual
    matched span for it to bound -- requiring >=1 literal char after
    stripping the lookarounds enforces that."""
    stripped = re.sub(
        r"\(\?<=[^)]*\)|\(\?<![^)]*\)|\(\?=[^)]*\)|\(\?![^)]*\)", "", pattern)
    has_lookaround = stripped != pattern
    literal = re.sub(r"[\\().?*+|\[\]{}^$]", "", stripped)
    if has_lookaround:
        return len(literal) >= 1
    return len(literal) >= 2


def _valid_style_pattern(pattern, wrong):
    if not pattern or not isinstance(pattern, str):
        return False
    try:
        re.compile(pattern)
    except re.error:
        return False
    if wrong not in pattern:
        return False
    return _is_context_safe(pattern)


def judge_with_ai(cands):
    """The gatekeeper — cloud-only Sonnet via the Claude CLI (slow is fine, the
    whole path is async). Returns the parsed verdict dict
    {"dict":[i], "style":[{i,pattern,replace}], "format":[{i,note}], "reject":[i]}
    or None on any failure (caller sends everything to pending).

    2026-09-30 incident: a non-JSON CLI reply (e.g. an auth/CLI-level error
    printed as plain text instead of the model ever running) made
    `re.search(...).group()` raise AttributeError on the None match — caught
    by the blanket `except Exception` below same as any other failure, but
    logged only as the opaque "'NoneType' object has no attribute 'group'",
    with no hint that the CLI never reached the model at all. Measured: 44
    days / 1882 occurrences (2026-08-19 through this fix), ~every 10 min,
    24/7 — the gatekeeper was silently dead the whole time (dictionary
    corrections stuck in pending forever) and nothing surfaced it. Mirrors
    _parse_shadow_json's existing None-check so a non-JSON reply degrades to
    a normal, diagnosable "unavailable" — logging the actual reply text —
    instead of reporting a Python internals string as the reason."""
    items = [{"i": i, "wrong": v, "right": c} for i, (v, c) in enumerate(cands)]
    prompt = JUDGE_PROMPT + json.dumps(items, ensure_ascii=False)
    try:
        out = subprocess.run(
            [CLAUDE_BIN, "-p", prompt, "--model", "sonnet"],
            capture_output=True, text=True, timeout=90).stdout
        m = re.search(r"\{.*\}", out, re.S)
        if not m:
            detail = out.strip()[:200] or "(empty reply)"
            _write_judge_state(False, detail)
            _log(f"gatekeeper sonnet unavailable: no JSON in reply: {detail!r}")
            return None
        verdict = json.loads(m.group())
        for k in ("dict", "style", "format", "reject"):
            verdict.setdefault(k, [])
        _write_judge_state(True)
        _log(f"gatekeeper sonnet: dict={verdict['dict']} "
             f"style={[s.get('i') for s in verdict['style'] if isinstance(s, dict)]} "
             f"format={[f.get('i') for f in verdict['format'] if isinstance(f, dict)]} "
             f"reject={verdict['reject']}")
        return verdict
    except Exception as exc:
        _write_judge_state(False, str(exc))
        _log(f"gatekeeper sonnet unavailable: {exc}")
        return None


JUDGE_STATE_PATH = os.path.join(HERE, ".judge_state")


def _judge_state_read():
    try:
        with open(JUDGE_STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _write_judge_state(ok, detail=""):
    """Record EVERY judge_with_ai attempt (success or failure) to a small
    dashboard-readable state file, so a chronic failure streak is visible on
    disk instead of scrolling past in server.log — the exact gap that let
    the gatekeeper stay silently dead for 44 days before anyone noticed.
    Mirrors _write_consolidate_state's shape (same doctrine, same file
    format), applied to the gatekeeper judge instead of consolidation.

    Deliberately writes the true state on every call with no threshold of
    its own — "is this bad enough to matter" is a judgement for whatever
    reads this file (dashboard, a human, a future check), not a magic
    number baked in here (see the codebase's own no-hard-number-thresholds
    doctrine)."""
    prev = _judge_state_read()
    streak = 0 if ok else prev.get("consecutive_failures", 0) + 1
    now = datetime.datetime.now().isoformat(timespec="seconds")
    state = {
        "last_attempt": now,
        "last_ok": ok,
        "consecutive_failures": streak,
        "last_error": "" if ok else detail,
        "last_success": now if ok else prev.get("last_success"),
    }
    try:
        with open(JUDGE_STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)
    except OSError:
        pass
    return streak


def apply_judgement(doc, cands, verdict, source):
    """Mutate doc according to a judge verdict. dict→terms rule (never a bare
    single-Han rule), style→context-bounded sub, format→style note, reject→drop,
    anything else / invalid → pending. Returns a summary."""





    migrate_style_notes_to_rules(doc)
    terms = doc.setdefault("terms", {})
    pending = doc.setdefault("pending", {})
    style = doc.setdefault("style", {})
    subs = style.setdefault("subs", [])
    notes = style.setdefault("notes", [])
    rejected = doc.get("rejected", [])
    summary = {"dict": [], "style": [], "format": [], "reject": [],
               "pending": [], "dropped": []}

    def to_pending(o, c):
        if o not in pending.setdefault(c, []):
            pending[c].append(o)
            summary["pending"].append([o, c])

    dict_idx = {i for i in verdict.get("dict", []) if isinstance(i, int)}
    reject_idx = {i for i in verdict.get("reject", []) if isinstance(i, int)}
    style_map = {s.get("i"): s for s in verdict.get("style", []) if isinstance(s, dict)}
    format_map = {f.get("i"): f for f in verdict.get("format", []) if isinstance(f, dict)}

    handled = set()
    for i, (o, c) in enumerate(cands):
        if i in dict_idx:
            handled.add(i)


            if _is_single_han(o) and _is_single_han(c):
                to_pending(o, c)
                continue
            if is_rejected(rejected, "dict", _rej_key("dict", o, c)):
                summary["dropped"].append(["dict", f"{o}→{c}"])
                continue







            if rule_audit.is_unsafe_wrong(o, c):
                add_rejected(doc, "dict", _rej_key("dict", o, c))
                append_changelog(doc, "removed",
                                 f"gate 自動拒絕常用詞 rule：{o}→{c}", source)
                summary["reject"].append([o, c])
                _log(f"admission gate rejected common-word rule: {o}→{c}")
                continue
            esc = re.escape(o)


            vs = terms.setdefault(rule_audit.existing_canonical(terms, c), [])
            if esc not in vs and o not in vs:
                vs.append(esc)
                append_changelog(doc, "dict", f"{o}→{c}", source)
                summary["dict"].append([o, c])
        elif i in style_map:
            handled.add(i)
            s = style_map[i]
            pat, rep = s.get("pattern"), s.get("replace")
            if is_rejected(rejected, "style", pat):
                summary["dropped"].append(["style", pat])
                continue
            if _valid_style_pattern(pat, o) and isinstance(rep, str):
                if not any(x.get("pattern") == pat for x in subs):
                    subs.append({"pattern": pat, "replace": rep,
                                 "note": f"{o}→{rep}"})
                    append_changelog(doc, "style", f"{pat} → {rep}", source)
                    summary["style"].append([pat, rep])
            else:
                to_pending(o, c)
        elif i in format_map:
            handled.add(i)
            note = (format_map[i].get("note") or "").strip()
            if note and is_rejected(rejected, "format", note):
                summary["dropped"].append(["format", note])
                continue










            if note and not _add_style_note(style, note):
                summary["dropped"].append(["format", note])
                continue
            if note:
                append_changelog(doc, "format", note, source)
                summary["format"].append(note)
            else:
                to_pending(o, c)
        elif i in reject_idx:
            handled.add(i)




            add_rejected(doc, "dict", _rej_key("dict", o, c))
            summary["reject"].append([o, c])
    for i, (o, c) in enumerate(cands):
        if i not in handled:
            to_pending(o, c)
    return summary










def _promote_mishear(wrong, right, count):
    """promote_fn for review_gate.process_queue(). Returns True iff the pair
    actually landed in dictionary.json as a live terms rule."""
    with _dict_lock:
        doc = load_doc()
        terms = doc.setdefault("terms", {})
        rejected = doc.get("rejected", [])






        if _is_single_han(wrong) and _is_single_han(right):
            _log(f"review_gate: {wrong}→{right} is a bare single-Han pair — "
                 "never auto-promoted, needs a manual/contextual rule")
            return False
        if is_rejected(rejected, "dict", _rej_key("dict", wrong, right)):
            _log(f"review_gate: {wrong}→{right} is in rejected memory — skipped")
            return False
        if rule_audit.is_unsafe_wrong(wrong, right):
            add_rejected(doc, "dict", _rej_key("dict", wrong, right))
            append_changelog(doc, "removed",
                             f"gate 自動拒絕常用詞 rule：{wrong}→{right}", "review_gate")
            save_doc(doc)
            _log(f"review_gate: admission gate rejected common-word rule: "
                 f"{wrong}→{right}")
            return False
        esc = re.escape(wrong)
        vs = terms.setdefault(rule_audit.existing_canonical(terms, right), [])
        if esc in vs or wrong in vs:
            return True
        vs.append(esc)
        append_changelog(doc, "dict",
                         f"{wrong}→{right}（review_gate，{count} 次聽錯判斷）",
                         "review_gate")
        save_doc(doc)
    _log(f"review_gate: promoted {wrong}→{right} after {count} mishear "
         "classifications")
    return True


REVIEW_QUEUE_INTERVAL_S = int(os.environ.get("DICTATION_REVIEW_INTERVAL_MIN", "10")) * 60


def review_queue_worker():
    """Background pass over review_queue.jsonl (see review_gate.py). Runs on
    its own cadence (default every 10 min, DICTATION_REVIEW_INTERVAL_MIN),
    same pattern as pending_retry_worker — never on the paste path, and
    review_gate.process_queue() itself waits on recording_gate before making
    any local-qwen call, so a live dictation always wins."""
    while True:
        time.sleep(REVIEW_QUEUE_INTERVAL_S)
        submit_maintenance("review-queue", _review_queue_pass, dedupe=True)


def _review_queue_pass():
    summary = review_gate.process_queue(_promote_mishear, log=_log, gate=False)
    if summary["new"]:
        _log(f"review_gate: pass complete — {summary}")


def _read_sync_sha():
    try:
        with open(SYNC_STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh).get("sha")
    except (OSError, json.JSONDecodeError):
        return None


def _write_sync_sha(sha):
    try:
        with open(SYNC_STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump({"sha": sha}, fh)
    except OSError:
        return False
    return True


def _github_get_file(path):
    """Raw bytes + sha for `path` in REPO, or (None, None) offline/failure.
    The generic GitHub Contents API primitive github_get() (dictionary.json,
    below) and push_review_queue_backup() (review_queue.jsonl, same repo/
    directory) both build on -- one sync mechanism, two callers, rather than
    a second copy of this subprocess/error-handling shape."""
    if not SYNC_ENABLED:
        return None, None
    try:
        proc = _gh_api_run(["gh", "api", f"repos/{REPO}/contents/{path}"],
                           capture_output=True, text=True)
        if proc.returncode != 0:
            _log(f"_github_get_file({path}) rc={proc.returncode}: "
                 f"{proc.stderr.strip()[:120]}")
            return None, None
        j = json.loads(proc.stdout)
        return base64.b64decode(j["content"]), j["sha"]
    except Exception as exc:
        _log(f"_github_get_file({path}) failed: {exc}")
        return None, None


def _github_put_file(path, content_bytes, sha, message):
    """PUT raw bytes at `path` in REPO; returns (new_sha, error). error
    non-None on failure (a 409/422 sha conflict shows up here — caller
    re-GETs and retries, same contract github_put() already had). sha=None
    is valid here (first-ever push of a path GitHub has never seen);
    github_put() itself still requires one, per D58 rule 5 (GET the sha
    immediately before PUT, never a blind/stale write to the dictionary)."""
    if not SYNC_ENABLED:
        return None, "github sync disabled (DICT_REPO=off)"
    body = {"message": message, "branch": "main",
            "content": base64.b64encode(content_bytes).decode("ascii")}
    if sha:
        body["sha"] = sha
    try:
        proc = _gh_api_run(
            ["gh", "api", "-X", "PUT", f"repos/{REPO}/contents/{path}",
             "--input", "-"],
            input=json.dumps(body), capture_output=True, text=True)
        if proc.returncode != 0:
            return None, proc.stderr.strip()[:200]
        return json.loads(proc.stdout)["content"]["sha"], None
    except Exception as exc:
        return None, str(exc)


def github_get():
    """(doc, sha) from the canonical repo, or (None, None) offline/failure."""
    content, sha = _github_get_file(DICT_FILE)
    if content is None:
        return None, None
    try:
        return json.loads(content.decode("utf-8")), sha
    except Exception as exc:
        _log(f"github_get decode failed: {exc}")
        return None, None


def github_put(doc, sha, message):
    """PUT the merged file; returns (new_sha, error). error non-None on failure
    (a 409 sha conflict shows up here — caller re-GETs, merges and retries)."""
    if not sha:
        return None, "missing sha (GET must succeed immediately before PUT)"
    content = json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8")
    return _github_put_file(DICT_FILE, content, sha, message)


def _is_sha_conflict(error):
    return bool(re.search(r"(?:HTTP\s*)?(?:409|422)\b", error or ""))













REVIEW_QUEUE_BACKUP_FILE = os.path.join(
    os.path.dirname(DICT_FILE), "review_queue.jsonl")
REVIEW_QUEUE_BACKUP_STATE_PATH = os.path.join(
    HERE, ".review_queue_backup_state")


def _review_queue_backup_pending():
    """(content_bytes, sha256) if the local review_queue.jsonl differs from
    what was last successfully backed up, else (None, None). Content-hash
    compare, not mtime — a touch/rewrite that doesn't change the actual
    corrections must never trigger a network call."""
    try:
        with open(review_gate.QUEUE_PATH, "rb") as fh:
            content = fh.read()
    except OSError:
        return None, None
    digest = hashlib.sha256(content).hexdigest()
    try:
        with open(REVIEW_QUEUE_BACKUP_STATE_PATH, encoding="utf-8") as fh:
            last = json.load(fh).get("sha256")
    except (OSError, json.JSONDecodeError):
        last = None
    if digest == last:
        return None, None
    return content, digest


def push_review_queue_backup():
    """Push review_queue.jsonl to the vault if its content has changed since
    the last successful backup. Best-effort, like every other maintenance
    job on this lane (D58: never on the foreground path, never blocks
    dictation) — a failed backup is logged and simply retried on the next
    poll, since the local file (review_gate's own durable, append-only log)
    remains the working copy regardless."""
    if not SYNC_ENABLED:
        return True
    content, digest = _review_queue_backup_pending()
    if content is None:
        return True
    _, get_sha = _github_get_file(REVIEW_QUEUE_BACKUP_FILE)
    new_sha, error = _github_put_file(
        REVIEW_QUEUE_BACKUP_FILE, content, get_sha,
        "dictation: backup review_queue.jsonl (hand-made correction labels)")
    if error:
        _log(f"review_queue backup failed: {error}")
        return False
    try:
        with open(REVIEW_QUEUE_BACKUP_STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump({"sha256": digest, "github_sha": new_sha,
                      "last": datetime.datetime.now().isoformat(timespec="seconds")}, fh)
    except OSError:
        pass
    _log(f"review_queue backup: {len(content)} bytes -> {REVIEW_QUEUE_BACKUP_FILE}")
    return True




def merge_dicts(a, b):
    """Union-merge b INTO a; a wins on scalar conflicts. Local edits never lost.
    terms/pending: union variant lists per key. style.subs: union by pattern.
    style.rules: union by identity (structured, 2026-07-26 -- see
    _upsert_style_rule; style.notes stays the regenerated view). changelog:
    merge, dedupe, sort by time desc, cap 100."""
    out = json.loads(json.dumps(a))
    for section in ("terms", "pending"):
        dst = out.setdefault(section, {})
        for k, vs in (b.get(section) or {}).items():
            cur = dst.setdefault(k, [])
            for v in vs:
                if v not in cur:
                    cur.append(v)
    astyle = out.setdefault("style", {})
    subs = astyle.setdefault("subs", [])
    seen_pat = {s.get("pattern") for s in subs}
    for s in (b.get("style") or {}).get("subs", []):
        if s.get("pattern") not in seen_pat:
            subs.append(s)
            seen_pat.add(s.get("pattern"))



    migrate_style_notes_to_rules(out)
    bstyle = b.get("style") or {}
    b_rules = bstyle.get("rules")
    b_clauses = ([r.get("text") for r in b_rules if isinstance(r, dict) and r.get("text")]
                if b_rules else bstyle.get("notes") or [])
    for clause_or_note in b_clauses:
        for clause in _note_clauses(clause_or_note) or [clause_or_note.strip()]:
            if clause:
                _upsert_style_rule(astyle, clause)
    astyle["notes"] = _rules_to_notes(astyle.get("rules", []))
    combined = list(out.get("changelog", [])) + list(b.get("changelog") or [])
    seen_e, merged = set(), []
    for e in sorted(combined, key=lambda e: e.get("time", ""), reverse=True):
        key = (e.get("time"), e.get("kind"), e.get("desc"), e.get("source"))
        if key in seen_e:
            continue
        seen_e.add(key)
        merged.append(e)
    if merged:
        out["changelog"] = merged[:100]


    combined_r = list(out.get("rejected", [])) + list(b.get("rejected") or [])
    seen_r, merged_r = set(), []
    for r in sorted(combined_r, key=lambda r: r.get("time", ""), reverse=True):
        key = (r.get("kind"), r.get("key"), r.get("pattern"))
        if key in seen_r:
            continue
        seen_r.add(key)
        merged_r.append(r)
    if merged_r:
        out["rejected"] = merged_r[:REJECTED_CAP]




    b_sc = b.get("shadow_candidates") or {}
    if b_sc or out.get("shadow_candidates"):
        sc = out.setdefault("shadow_candidates", {})
        for k, e in b_sc.items():
            if not isinstance(e, dict):
                continue
            cur = sc.get(k)
            if cur is None:
                sc[k] = json.loads(json.dumps(e))
            else:
                cur["count"] = max(int(cur.get("count", 0)), int(e.get("count", 0)))
                cur["first"] = min(cur.get("first") or "", e.get("first") or "") \
                    or cur.get("first") or e.get("first")
                cur["last"] = max(cur.get("last") or "", e.get("last") or "")






    b_scp = b.get("shadow_candidate_progress") or {}
    if b_scp or out.get("shadow_candidate_progress"):
        scp = out.setdefault("shadow_candidate_progress", {})
        for k, e in b_scp.items():
            if not isinstance(e, dict):
                continue
            cur = scp.get(k)
            if cur is None:
                scp[k] = json.loads(json.dumps(e))
            else:
                cur["count"] = max(int(cur.get("count", 0)), int(e.get("count", 0)))


    b_fl = b.get("flagged_subs") or {}
    if b_fl or out.get("flagged_subs"):
        fl = out.setdefault("flagged_subs", {})
        for pat, n in b_fl.items():
            fl[pat] = max(int(fl.get(pat, 0)), int(n) if isinstance(n, int) else 0)




    b_q = b.get("quarantine") or []
    if b_q or out.get("quarantine"):
        q = out.setdefault("quarantine", [])
        seen_q = {_quarantine_key(e) for e in q}
        for e in b_q:
            if not isinstance(e, dict):
                continue
            k = _quarantine_key(e)
            if k in seen_q:
                for cur in q:
                    if _quarantine_key(cur) == k:
                        cur["since"] = min(cur.get("since") or "",
                                           e.get("since") or "") \
                            or cur.get("since") or e.get("since")
                continue
            q.append(json.loads(json.dumps(e)))
            seen_q.add(k)



    b_qr = b.get("quarantine_resolved") or {}
    if b_qr or out.get("quarantine_resolved"):
        qr = out.setdefault("quarantine_resolved", {})
        for k, e in b_qr.items():
            cur = qr.get(k)
            if cur is None or (e.get("time") or "") > (cur.get("time") or ""):
                qr[k] = json.loads(json.dumps(e))








    consolidate_style_subs(out)



    _prune_shadow_candidates(out)
    _prune_flagged_subs(out)


    _prune_pending(out)





    _prune_rejected_style_rules(out)


    _prune_quarantined(out)
    return out


def _ci_fold(s):
    """Case/whitespace-fold a string for quarantine identity comparisons.
    strip() absorbs incidental surrounding whitespace; casefold() absorbs
    ASCII/Latin case (NTFY == ntfy). CJK characters have no case distinction,
    so casefold() is a documented no-op on them (confirmed: 'NTFY'.casefold()
    == 'ntfy' but '用家哋'.casefold() == '用家哋') -- this cannot collapse two
    genuinely different CJK terms, only different-case spellings of the same
    Latin/ASCII one. Non-strings pass through unchanged."""
    return s.strip().casefold() if isinstance(s, str) else s


def _quarantine_key(entry):
    """Stable identity of a quarantine entry, case/whitespace-folded (see
    _ci_fold) so a rule quarantined as "NTFY" and later re-quarantined as
    "ntfy" are recognised as the SAME rule -- they name the same live term,
    since apply_dictionary matches IGNORECASE (2026-08-09 quarantine entries
    document this explicitly: "apply_dictionary is IGNORECASE and the later
    key silently wins"). Bug fixed 2026-08-18: this identity used to be
    exact-string, so a quarantine entry for "NTFY" never matched the live
    term key "ntfy" and the corrupting rule kept firing for 9 days after
    Tinho believed it was killed."""
    if entry.get("kind") == "style_sub":
        return ("style_sub", entry.get("pattern"))
    return ("term", _ci_fold(entry.get("term")), _ci_fold(entry.get("variant")))


def _qkey_str(entry):
    """String form of _quarantine_key, for the quarantine_resolved map."""
    return "|".join(str(p) for p in _quarantine_key(entry))


def _qkey_str_legacy(entry):
    """Pre-fix (non-case-folded) form of _qkey_str. quarantine_resolved
    entries written before the 2026-08-18 case-fold fix are keyed this way;
    kept so those historical resolutions are still honoured (matched, not
    orphaned) once _qkey_str itself starts folding case. Never used to WRITE
    new entries -- _mark_quarantine_resolved always writes the current
    (folded) form -- only to read old ones."""
    if entry.get("kind") == "style_sub":
        parts = ("style_sub", entry.get("pattern"))
    else:
        parts = ("term", entry.get("term"), entry.get("variant"))
    return "|".join(str(p) for p in parts)


QUARANTINE_RESOLVED_CAP = 100


def _prune_quarantined(doc):
    """Merge-site/deterministic collapse (same doctrine as _prune_pending):

    1. a quarantine entry whose rule has been RESOLVED (restored or deleted,
       recorded in quarantine_resolved) is dropped — a stale peer's
       quarantine list can't re-open a settled case, and in particular can't
       re-kill a restored rule;
    2. a rule still sitting in quarantine may NOT also exist in the live
       terms/subs — quarantine means observe-only, and a union-merge against
       a stale peer that still applies the rule would otherwise resurrect it
       into the live doc the moment it was quarantined locally.

    Idempotent; quarantine and quarantine_resolved themselves union across
    devices, so a quarantining/resolution anywhere prunes everywhere.

    Case fix (2026-08-18): a quarantined TERM rule may share its live term
    key with a different-case spelling ("NTFY" quarantined, live key
    "ntfy") -- apply_dictionary matches IGNORECASE, so they are the same
    rule to the user even though they are different dict keys. Pruning
    below therefore matches live term keys AND variants case-insensitively
    (see _ci_fold), not just by exact key. The live term KEY itself is left
    exactly as spelled (never renamed/merged) -- only the matched variant is
    removed, and the key is dropped only if that empties it out."""
    q = doc.get("quarantine") or []
    if not q:
        return
    resolved = doc.get("quarantine_resolved") or {}
    q = [e for e in q
         if _qkey_str(e) not in resolved and _qkey_str_legacy(e) not in resolved]
    if q:
        doc["quarantine"] = q
    else:
        doc.pop("quarantine", None)
        return
    terms = doc.get("terms") or {}
    for e in q:
        if e.get("kind") == "term":
            qterm, qvariant = e.get("term"), e.get("variant")
            for key in _ci_matching_keys(terms, qterm):
                vs = terms.get(key)
                if not vs:
                    continue
                if qvariant in vs:
                    vs.remove(qvariant)
                else:
                    nv = _ci_fold(qvariant)
                    match = next((x for x in vs if _ci_fold(x) == nv), None)
                    if match is not None:
                        vs.remove(match)
                if not vs:
                    del terms[key]
        elif e.get("kind") == "style_sub":
            subs = (doc.get("style") or {}).get("subs")
            if subs:
                doc["style"]["subs"] = [
                    s for s in subs if s.get("pattern") != e.get("pattern")]


def _ci_matching_keys(terms, term):
    """All keys in `terms` naming the same term as `term`, case/whitespace-
    insensitively: the exact key first if present, then any other key whose
    folded form matches (there can be more than one live key for the same
    term across different case spellings -- the "case-rival canonical" class
    documented in several 2026-08-09 quarantine entries -- and a quarantine
    of one spelling must prune ALL of them, not just whichever happens to be
    found first)."""
    if term is None:
        return []
    keys = [term] if term in terms else []
    norm = _ci_fold(term)
    keys.extend(k for k in terms if k != term and _ci_fold(k) == norm)
    return keys


def _mark_quarantine_resolved(doc, entry, action):
    qr = doc.setdefault("quarantine_resolved", {})
    qr[_qkey_str(entry)] = {
        "action": action,
        "time": datetime.datetime.now().isoformat(timespec="seconds")}
    if len(qr) > QUARANTINE_RESOLVED_CAP:
        for k in sorted(qr, key=lambda k: qr[k].get("time") or "")[
                :len(qr) - QUARANTINE_RESOLVED_CAP]:
            del qr[k]


def quarantine_rule(doc, rule, reason, source):
    """Move a live rule into shadow-observation: it stops applying, but
    polish logs every would-be hit to .quarantine_hits.jsonl so the weekly
    audit can decide on evidence whether to restore or delete it. Mutates
    doc; the caller pushes via push_authoritative(_quarantine_shrink(...))."""
    kind = rule.get("kind")
    q = doc.setdefault("quarantine", [])
    now = datetime.datetime.now().isoformat(timespec="seconds")
    if kind == "term":
        entry = {"kind": "term", "term": rule.get("term"),
                 "variant": rule.get("variant"), "since": now,
                 "reason": reason}
    elif kind == "style_sub":
        sub = rule.get("sub") or {}
        entry = {"kind": "style_sub", "pattern": rule.get("pattern"),
                 "replace": sub.get("replace"), "note": sub.get("note"),
                 "since": now, "reason": reason}
    else:
        return None
    if not any(_quarantine_key(e) == _quarantine_key(entry) for e in q):
        q.append(entry)
    _prune_quarantined(doc)
    append_changelog(doc, "quarantine",
                     f"隔離觀察：{rule_desc(rule)}（{reason}）", source)
    return entry


def _quarantine_shrink(entries):
    """push_authoritative callback for quarantining: ensure each entry exists
    in quarantine and is absent from the live sections, on every merge
    attempt. Idempotent."""
    payload = json.loads(json.dumps(entries))

    def shrink(doc):
        q = doc.setdefault("quarantine", [])
        seen = {_quarantine_key(e) for e in q}
        for e in payload:
            if _quarantine_key(e) not in seen:
                q.append(json.loads(json.dumps(e)))
                seen.add(_quarantine_key(e))
        _prune_quarantined(doc)
    return shrink


def restore_quarantined(doc, entry, source):
    """A quarantined rule earned its way back (observation showed it fixing
    real mishearings): put it back live, drop the quarantine entry, and mark
    the case resolved so a stale peer's quarantine list can't re-kill it."""
    q = doc.get("quarantine") or []
    doc["quarantine"] = [e for e in q
                         if _quarantine_key(e) != _quarantine_key(entry)]
    if not doc["quarantine"]:
        doc.pop("quarantine", None)
    _mark_quarantine_resolved(doc, entry, "restore")
    if entry.get("kind") == "term":
        vs = doc.setdefault("terms", {}).setdefault(entry.get("term"), [])
        if entry.get("variant") not in vs:
            vs.append(entry.get("variant"))
        desc = f"{entry.get('variant')}→{entry.get('term')}"
    else:
        subs = doc.setdefault("style", {}).setdefault("subs", [])
        if not any(s.get("pattern") == entry.get("pattern") for s in subs):
            subs.append({"pattern": entry.get("pattern"),
                         "replace": entry.get("replace"),
                         "note": entry.get("note") or ""})
        desc = entry.get("pattern")
    append_changelog(doc, "restore", f"隔離期滿恢復：{desc}", source)


def discard_quarantined(doc, entry, source):
    """A quarantined rule failed observation: drop it for good, with rejected
    memory so it can never be re-proposed, and a resolved marker so a stale
    peer's quarantine list can't re-open the case."""
    q = doc.get("quarantine") or []
    doc["quarantine"] = [e for e in q
                         if _quarantine_key(e) != _quarantine_key(entry)]
    if not doc["quarantine"]:
        doc.pop("quarantine", None)
    _mark_quarantine_resolved(doc, entry, "delete")
    if entry.get("kind") == "term":
        wrong = rule_audit.unescape(entry.get("variant"))
        add_rejected(doc, "dict",
                     _rej_key("dict", entry.get("variant"), entry.get("term")))
        if wrong != entry.get("variant"):
            add_rejected(doc, "dict", _rej_key("dict", wrong, entry.get("term")))
        desc = f"{entry.get('variant')}→{entry.get('term')}"
    else:
        add_rejected(doc, "style", entry.get("pattern"),
                     entry.get("pattern"))
        desc = entry.get("pattern")
    append_changelog(doc, "removed", f"隔離判定刪除：{desc}", source)


def _prune_pending(doc):
    """Merge-site/deterministic collapse for pending (same doctrine as
    _prune_shadow_candidates): drop any pending candidate that was rejected —
    manual ✗ or a gatekeeper reject, both land in rejected memory — or that
    has already been consumed into a live terms rule. Without this, a
    union-merge against a stale remote resurrects the pending entry the
    moment it is removed locally (the zombie class root-caused 2026-07-22).
    Idempotent; rejected memory itself unions across devices, so a rejection
    anywhere prunes everywhere."""
    pending = doc.get("pending") or {}
    rejected = doc.get("rejected", [])
    terms = doc.get("terms") or {}
    for c in list(pending):
        live = terms.get(c, [])
        keep = [v for v in pending[c]
                if not is_rejected(rejected, "dict", _rej_key("dict", v, c))
                and v not in live and re.escape(v) not in live]
        if keep:
            pending[c] = keep
        else:
            del pending[c]


def _prune_rejected_style_rules(doc):
    """Merge-site/deterministic collapse for style.rules (same doctrine as
    _prune_pending above, and _prune_quarantined for terms/style_sub): a
    style rule Tinho deleted — one at a time via delete_rule (kind=
    style_note, which already records it in rejected memory as kind=
    "format"), or in bulk via a consolidation/authoritative-restore shrink
    (see _shrink_style_rules) — must not survive a union-merge against a
    stale peer that still carries it.

    Before this existed, merge_dicts's style.rules union (_upsert_style_rule,
    identity-based, with no visibility into rejected memory) had no
    equivalent guard: terms/style_sub deletions are protected by quarantine
    + _prune_quarantined, and pending candidates by rejected + _prune_pending,
    but a deleted style rule had NOTHING pruning it back out after the union
    re-added it. This is exactly the gap that let tinho-os PR #1006's
    84->28 style.rules cut regrow to 60 within hours (measured live
    2026-08-18, tinho-os PR #1012 follow-up) — the splitter that caused the
    original corruption was already retired by then, so the union-merge
    resurrecting old entries from a stale peer was the only remaining path
    back in.

    Idempotent; rejected memory itself unions across devices (see
    merge_dicts), so a deletion anywhere prunes everywhere."""
    style = doc.get("style") or {}
    rules = style.get("rules")
    if not rules:
        return
    rejected_texts = {r.get("key") for r in (doc.get("rejected") or [])
                      if r.get("kind") == "format"}
    if not rejected_texts:
        return
    kept = [r for r in rules
           if not (isinstance(r, dict) and r.get("text") in rejected_texts)]
    if len(kept) != len(rules):
        style["rules"] = kept
        style["notes"] = _rules_to_notes(kept)


def sync_pull():
    """Pull remote; if its sha moved, union-merge it into local. Marks dirty
    only when the merge actually added something remote didn't have."""
    remote, sha = github_get()
    if remote is None:
        return False
    if sha == _read_sync_sha():
        return True
    with _dict_lock:
        local = load_doc()
        merged = merge_dicts(local, remote)
        _write_doc(merged)
        added = json.dumps(merged, ensure_ascii=False, sort_keys=True) != \
            json.dumps(remote, ensure_ascii=False, sort_keys=True)
    _write_sync_sha(sha)
    _log(f"sync pull: merged remote sha={sha[:7]} local_ahead={added}")
    if added:
        _dirty.set()
        _push_now.set()
    return True


def sync_push():
    """Push local up if dirty. Merges remote-only content first so nothing the
    repo has is dropped; on sha conflict re-GET, merge, retry once."""
    if not _dirty.is_set():
        return True
    remote, sha = github_get()
    if remote is None or not sha:
        _log("sync push deferred: could not refresh remote sha")
        return False
    with _dict_lock:
        local = load_doc()
        if remote is not None:
            local = merge_dicts(local, remote)
            _write_doc(local)
    msg = "dictation: sync dictionary from Mac"
    new_sha, err = github_put(local, sha, msg)
    if new_sha is None and _is_sha_conflict(err):
        remote2, sha2 = github_get()
        if remote2 is not None:
            with _dict_lock:
                local = merge_dicts(load_doc(), remote2)
                _write_doc(local)
            new_sha, err = github_put(local, sha2, msg)
    if new_sha:
        _write_sync_sha(new_sha)
        _dirty.clear()
        _log(f"sync push ok sha={new_sha[:7]}")
        return True
    else:
        _log(f"sync push failed (stays dirty): {err}")
        return False


def push_authoritative(shrink, message, max_attempts=2):
    """Push a doc that carries an INTENTIONAL local shrink — a consolidation
    that replaced style.notes wholesale, or a ✕/re-review delete that removed
    one live rule. Plain sync_push() always union-merges remote in before
    pushing, so it can only ever grow the doc: a shrink pushed that way gets
    silently undone the moment remote still holds the old, bigger copy (this
    is exactly what happened to the first, unpushed consolidation attempt
    tonight — it had to be repaired with a hand-run bypass push).

    `shrink(doc)` mutates doc in place to (re)apply exactly the intentional
    change; it MUST be idempotent — it is re-run after every fetch, including
    retries, since the retry may see different remote content.

    Per attempt: fetch remote+sha fresh, union-merge it into local (so a
    genuinely concurrent addition from another device — a new term, a new
    note, a new rejected entry — is never lost), THEN re-apply `shrink` on
    top of that merge so the union can never resurrect the exact thing being
    shrunk, then PUT with the sha just fetched. On a sha conflict (someone
    pushed in between — the flaky-hotspot scenario the diagnosis hit) the
    whole cycle repeats from a fresh github_get(): it re-fetches the current
    sha rather than reusing the stale one, and `shrink` runs again so the
    retry can't resurrect the shrink either.

    On success, .dict_sync_state is updated to the new sha, so the next
    periodic sync_pull() sees sha == recorded sha and skips its merge
    entirely — there is nothing stale left to re-merge in later.

    Returns True once a push is confirmed; False if every attempt failed
    (offline / GitHub outage) — the doc stays marked dirty so the ordinary
    background sync_worker keeps retrying (with the ordinary, less careful
    merge — a shrink could in principle be resurrected there, but only in
    this fully-offline fallback, never in the normal path)."""




    if not SYNC_ENABLED:



        with _dict_lock:
            local = load_doc()
            shrink(local)
            lstyle = local.setdefault("style", {})
            if "notes" in lstyle:
                _set_style_notes(lstyle, lstyle["notes"])
            _write_doc(local)
        _dirty.clear()
        return True
    attempts = min(max(1, max_attempts), 2)
    err = None
    for attempt in range(1, attempts + 1):
        remote, sha = github_get()
        if remote is None or not sha:
            err = "could not refresh remote sha"
            _log(f"authoritative push deferred: {err}")
            break
        with _dict_lock:
            local = load_doc()
            merged = merge_dicts(local, remote) if remote is not None else local
            shrink(merged)











            mstyle = merged.setdefault("style", {})
            if "notes" in mstyle:
                _set_style_notes(mstyle, mstyle["notes"])
            _write_doc(merged)
            _dirty.set()
        new_sha, err = github_put(merged, sha, message)
        if new_sha:
            _write_sync_sha(new_sha)
            _dirty.clear()
            _log(f"authoritative push ok sha={new_sha[:7]} attempt={attempt}")
            return True
        _log(f"authoritative push attempt {attempt} failed: {err}")
        if not _is_sha_conflict(err) or attempt >= attempts:
            break
    _log(f"authoritative push failed, stays dirty: {err}")
    return False


def first_sync():
    """On startup the local file is richer than the repo snapshot: union in
    anything remote-only, push local up, verify with a fresh GET."""
    remote, sha = github_get()
    if remote is None:




        _dirty.set()
        _log("first sync: github_get failed, deferring push "
             "to background sync")
        return False
    with _dict_lock:
        local = load_doc()
        local = merge_dicts(local, remote)
        _write_doc(local)
    _write_sync_sha(sha)

    if json.dumps(local, ensure_ascii=False, sort_keys=True) == \
            json.dumps(remote, ensure_ascii=False, sort_keys=True):
        _dirty.clear()
        _log("first sync: local already matches remote, nothing to push")
        return True
    new_sha, err = github_put(local, sha, "dictation: initial sync from Mac")
    if new_sha is None and _is_sha_conflict(err):
        remote2, sha2 = github_get()
        if remote2 is not None and sha2:
            with _dict_lock:
                local = merge_dicts(load_doc(), remote2)
                _write_doc(local)
            new_sha, err = github_put(
                local, sha2, "dictation: initial sync from Mac")
    if new_sha:
        _write_sync_sha(new_sha)
        _dirty.clear()
        verify, vsha = github_get()
        ok = verify is not None and vsha == new_sha
        _log(f"first sync push ok sha={new_sha[:7]} verified={ok}")
    else:
        _log(f"first sync push failed (stays dirty): {err}")
        _dirty.set()
        return False
    return True


_sync_retry_seconds = 5
_SYNC_RETRY_MAX_SECONDS = 300


def _sync_cycle(initial=False):
    """One best-effort sync attempt, always executed on the maintenance lane.

    RAISES on failure (2026-07-26) after scheduling its own retry -- this is
    new. Previously every failure was swallowed here (log + reschedule,
    `return`, never propagate), which meant MaintenanceLane's generic
    per-job failure-streak tracking (maintenance.py's _on_failure /
    DEFAULT_ESCALATE_EVERY, the PR #20 mechanism) never saw a "github-sync"
    failure no matter how long the sync had been broken -- the same
    invisible-chronic-failure shape PR #28 already fixed for
    consolidate_notes, just not yet fixed here. The retry-with-backoff
    behaviour is unchanged (still scheduled below, still runs, independent
    of the raise); the raise ONLY feeds the existing escalation path so a
    permanently-broken sync eventually surfaces as a bounded ntfy nudge
    instead of retrying forever in total silence."""
    global _sync_retry_seconds
    try:
        ok = first_sync() if initial else (sync_pull() and sync_push())
    except Exception as exc:
        _log(f"sync cycle error: {exc}")
        ok = False
    if ok:
        _sync_retry_seconds = 5
        return
    delay = _sync_retry_seconds
    _sync_retry_seconds = min(delay * 2, _SYNC_RETRY_MAX_SECONDS)
    _log(f"sync deferred; fire-and-forget retry in {delay}s")
    threading.Timer(delay, _push_now.set).start()
    raise RuntimeError(f"github-sync failed; retrying in {delay}s")


def sync_worker():


    if not SYNC_ENABLED:
        _log("github sync disabled (DICT_REPO=off) — dictionary.json "
             "stays local-only")
        return
    submit_maintenance("github-sync", _sync_cycle, True, dedupe=True)
    while True:
        _push_now.wait(timeout=900)
        _push_now.clear()
        submit_maintenance("github-sync", _sync_cycle, dedupe=True)


def pending_retry_worker():
    """Re-judge everything in pending every 10 min (only when non-empty)."""
    while True:
        time.sleep(600)
        submit_maintenance("pending-review", retry_pending, dedupe=True)


def retry_pending():
    with _dict_lock:
        doc = load_doc()
        pending = doc.get("pending", {})
        cands = [(v, c) for c, vs in pending.items() for v in list(vs)]
    if not cands:
        return
    verdict = judge_with_ai(cands)
    if verdict is None:
        _log("pending retry: judge unavailable, all stay pending")
        return
    with _dict_lock:
        doc = load_doc()
        pending = doc.setdefault("pending", {})
        for v, c in cands:
            if v in pending.get(c, []):
                pending[c].remove(v)
                if not pending[c]:
                    del pending[c]
        summary = apply_judgement(doc, cands, verdict, "retry")
        save_doc(doc)





    still = {tuple(x) for x in summary["pending"]}
    gone = [[v, c] for v, c in cands if (v, c) not in still]
    if gone:
        submit_maintenance(
            "push-pending-shrink", push_authoritative, _pending_shrink(gone),
            "dictation: judge pending candidates")
    _log(f"pending retry: accepted_dict={summary['dict']} style={summary['style']} "
         f"format={summary['format']} rejected={summary['reject']} "
         f"still_pending={summary['pending']}")


def _rereview_backend():
    """Return the configured judge backend, defaulting safely to Sonnet."""
    backend = os.environ.get("REREVIEW_BACKEND", "sonnet").strip().lower()
    if backend not in ("sonnet", "codex", "local"):
        _log(f"rereview unknown backend {backend!r}; using sonnet")
        return "sonnet"
    return backend


def _run_rereview_judge(prompt):
    """Run one judge with the existing prompt/JSON contract and a hard limit."""
    backend = _rereview_backend()
    if backend == "local":


        out = polish_mod._ollama(
            prompt, timeout=REREVIEW_TIMEOUT_S, force_json=True,
            model=SHADOW_LOCAL_MODEL)
    else:
        command = ([CLAUDE_BIN, "-p", prompt, "--model", "sonnet"]
                   if backend == "sonnet" else
                   ["codex", "exec", "--skip-git-repo-check", prompt])
        out = subprocess.run(
            command, capture_output=True, text=True,
            timeout=REREVIEW_TIMEOUT_S).stdout
    return json.loads(re.search(r"\{.*\}", out, re.S).group())


def rereview_rule(rule):
    """Queue a rereview without ever joining, waiting, or touching the live
    transcription request_lock. At most one judge is admitted at a time."""
    if os.environ.get("REREVIEW_PAUSED") == "1":
        _log("rereview skipped (paused)")
        return False
    if not _rereview_guard.acquire(blocking=False):
        _log("rereview skipped (already in flight)")
        return False
    try:
        submit_maintenance("rereview", _rereview_rule_worker, rule)
    except Exception:
        _rereview_guard.release()
        raise
    return True


def _rereview_rule_worker(rule):
    """Worker-side judge and mutation. Always releases the admission slot."""
    prompt = REREVIEW_PROMPT + json.dumps(rule, ensure_ascii=False)
    try:
        verdict = _run_rereview_judge(prompt)
    except (subprocess.TimeoutExpired, TimeoutError):
        _log("rereview skipped (timeout)")
        return
    except Exception as exc:
        _log(f"rereview judge unavailable: {exc}")
        return
    finally:


        _rereview_guard.release()
    _apply_rereview_verdict(rule, verdict)


def _apply_rereview_verdict(rule, verdict):
    action = verdict.get("action")
    kind = rule.get("kind")
    with _dict_lock:
        doc = load_doc()
        terms = doc.setdefault("terms", {})
        style = doc.setdefault("style", {})
        subs = style.setdefault("subs", [])
        changed = True





        shrink, push_msg = None, None
        if action == "delete":
            delete_rule(doc, rule, "rereview")
            shrink, push_msg = _rule_shrink(rule), "dictation: rereview delete rule"
        elif action == "keep":
            append_changelog(doc, rule_kind_tag(kind), f"保留：{rule_desc(rule)}",
                             "rereview")
        elif action == "narrow" and kind == "term":
            nv = verdict.get("variant")
            c, v = rule.get("term"), rule.get("variant")
            vs = terms.get(c, [])
            if nv and v in vs:
                vs[vs.index(v)] = nv


                add_rejected(doc, "dict", _rej_key("dict", v, c), v)
                append_changelog(doc, "dict", f"收窄 {v}→{nv} ({c})", "rereview")
                shrink = _replace_rule_shrink(rule, new_term=(c, nv))
                push_msg = "dictation: rereview narrow rule"
            else:
                changed = False
        elif action == "narrow" and kind == "style_sub":
            pat, rep = verdict.get("pattern"), verdict.get("replace")
            old = rule.get("pattern")
            hit = next((s for s in subs if s.get("pattern") == old), None)
            if hit and _valid_style_pattern(pat, "") is not False and pat:
                hit["pattern"], hit["replace"] = pat, rep if rep is not None else hit["replace"]
                add_rejected(doc, "style", old, old)
                append_changelog(doc, "style", f"收窄 {old} → {pat}", "rereview")
                shrink = _replace_rule_shrink(rule, new_sub=dict(hit))
                push_msg = "dictation: rereview narrow rule"
            else:
                changed = False
        elif action == "convert" and verdict.get("to") == "style" and kind == "term":
            pat, rep = verdict.get("pattern"), verdict.get("replace")
            c, v = rule.get("term"), rule.get("variant")
            if _valid_style_pattern(pat, "") is not False and pat and isinstance(rep, str):
                if v in terms.get(c, []):
                    terms[c].remove(v)
                    if not terms[c]:
                        del terms[c]
                new_sub = {"pattern": pat, "replace": rep, "note": f"→{rep}"}
                if not any(s.get("pattern") == pat for s in subs):
                    subs.append(dict(new_sub))
                add_rejected(doc, "dict", _rej_key("dict", v, c), v)
                append_changelog(doc, "style", f"轉為語氣規則 {pat} → {rep}", "rereview")
                shrink = _replace_rule_shrink(rule, new_sub=new_sub)
                push_msg = "dictation: rereview convert rule"
            else:
                changed = False
        elif action == "convert" and verdict.get("to") == "dict" and kind == "style_sub":
            wrong, right = verdict.get("wrong"), verdict.get("right")
            old = rule.get("pattern")
            if wrong and right and not _is_single_han(wrong):
                doc["style"]["subs"] = [s for s in subs if s.get("pattern") != old]
                vs = terms.setdefault(rule_audit.existing_canonical(terms, right), [])
                esc = re.escape(wrong)
                if esc not in vs:
                    vs.append(esc)
                add_rejected(doc, "style", old, old)
                append_changelog(doc, "dict", f"轉為字詞規則 {wrong}→{right}", "rereview")
                shrink = _replace_rule_shrink(rule, new_term=(right, esc))
                push_msg = "dictation: rereview convert rule"
            else:
                changed = False
        else:
            changed = False
        if changed:
            save_doc(doc)
    if changed and shrink is not None:
        push_authoritative(shrink, push_msg)
    _log(f"rereview {kind} action={action} applied={changed}")


def rule_kind_tag(kind):
    return {"term": "dict", "style_sub": "style", "style_note": "format"}.get(kind, "dict")


def rule_desc(rule):
    if rule.get("kind") == "term":
        return f"{rule.get('variant')}→{rule.get('term')}"
    if rule.get("kind") == "style_sub":
        return f"{rule.get('pattern')}"
    return f"{rule.get('note')}"


def delete_rule(doc, rule, source):
    """Remove a live rule immediately, changelog it, and remember it as rejected
    so the learning loop never re-proposes the same rule."""
    kind = rule.get("kind")
    rej = _rejected_from_rule(rule)
    if rej:
        add_rejected(doc, *rej)
    if kind == "term":
        c, v = rule.get("term"), rule.get("variant")
        if v in doc.get("terms", {}).get(c, []):
            doc["terms"][c].remove(v)
            if not doc["terms"][c]:
                del doc["terms"][c]
            append_changelog(doc, "removed", f"{v}→{c}", source)
    elif kind == "style_sub":
        p = rule.get("pattern")
        subs = doc.get("style", {}).get("subs", [])
        doc.setdefault("style", {})["subs"] = [s for s in subs if s.get("pattern") != p]
        append_changelog(doc, "removed", f"語氣 {p}", source)
    elif kind == "style_note":
        n = rule.get("note")
        notes = doc.get("style", {}).get("notes", [])
        if n in notes:
            notes.remove(n)



        _snapshot_remove_note(n)
        append_changelog(doc, "removed", f"格式 {n}", source)


def _rule_shrink(rule):
    """push_authoritative callback for a ✕/re-review delete: strip exactly
    this one rule back out if a union-merge brought it back from a remote
    that hadn't caught up with the delete yet. Pure content removal, no
    changelog/rejected bookkeeping — that already happened once, in
    delete_rule, against the doc written to disk before this ever runs.
    Idempotent: safe to re-apply on every retry."""
    kind = rule.get("kind")

    def shrink(doc):
        if kind == "term":
            c, v = rule.get("term"), rule.get("variant")
            vs = doc.get("terms", {}).get(c)
            if vs and v in vs:
                vs.remove(v)
                if not vs:
                    del doc["terms"][c]
        elif kind == "style_sub":
            p = rule.get("pattern")
            subs = doc.get("style", {}).get("subs")
            if subs:
                doc["style"]["subs"] = [s for s in subs if s.get("pattern") != p]
        elif kind == "style_note":
            n = rule.get("note")
            notes = doc.get("style", {}).get("notes")
            if notes and n in notes:
                notes.remove(n)

    return shrink


def _replace_rule_shrink(old_rule, new_sub=None, new_term=None):
    """push_authoritative callback for a re-review narrow/convert: strip the
    OLD form of the rule (same removal as _rule_shrink) and re-ensure the NEW
    form, so a union-merge against a stale remote can neither resurrect the
    old broad rule nor drop its replacement. new_sub is a complete style.subs
    entry; new_term is a (term, variant) pair. Idempotent."""
    base = _rule_shrink(old_rule)

    def shrink(doc):
        base(doc)
        if new_sub is not None:
            subs = doc.setdefault("style", {}).setdefault("subs", [])
            if not any(s.get("pattern") == new_sub.get("pattern") for s in subs):
                subs.append(json.loads(json.dumps(new_sub)))
        if new_term is not None:
            c, v = new_term
            vs = doc.setdefault("terms", {}).setdefault(c, [])
            if v not in vs:
                vs.append(v)
    return shrink


def _shrink_style_rules(doc, target_notes):
    """Force style.rules/notes to exactly `target_notes` (a bulk replace —
    consolidation, or an authoritative restore), and remember every clause
    the replace drops as rejected (kind="format", same record delete_rule
    already writes for a single ✕ style-rule delete) so the merge-site guard
    (_prune_rejected_style_rules, called from merge_dicts) can keep it out of
    every future union-merge — not just this one push_authoritative call.

    Without the reject records, a bulk cut is only protected for the
    duration of ITS OWN push_authoritative attempts (which re-apply the
    shrink on each retry); once that push succeeds, an ordinary later
    sync_pull()/sync_push() against a stale peer has nothing telling it the
    dropped clauses were deliberate, and re-adds them via the plain
    style.rules union. This is exactly the gap that let tinho-os PR #1006's
    84->28 cut regrow to 60 within hours (measured live 2026-08-18).

    Computed against the doc's CURRENT style.notes at call time, so it stays
    correct even on a push_authoritative retry whose union brought back
    different stale content than the previous attempt. Idempotent:
    add_rejected dedupes, and _set_style_notes is already idempotent.

    Call this INSIDE a push_authoritative shrink callback (it mutates `doc`
    in place); never call it against the live doc directly."""
    style = doc.setdefault("style", {})
    target = list(target_notes)
    target_set = set(target)
    for text in (style.get("notes") or []):
        if isinstance(text, str) and text not in target_set:
            add_rejected(doc, "format", text, None)
    _set_style_notes(style, target)


def _unlearn_shrink(removed):
    """push_authoritative callback for an auto-unlearn (Tinho's edit reverted
    a learned rule's output): strip each reverted (pattern → term) pair back
    out if a union-merge brought it back from a remote that hadn't caught up
    with the removal yet. Same contract as _rule_shrink: pure content
    removal, idempotent, safe to re-apply on every retry. The rejected-memory
    entry recorded at unlearn time rides along in the doc itself (rejected
    unions, never shrinks), so the pair also stays blocked from re-landing."""
    def shrink(doc):
        terms = doc.get("terms", {})
        for pat, var in removed:
            vs = terms.get(var)
            if vs and pat in vs:
                vs.remove(pat)
                if not vs:
                    del terms[var]
    return shrink


def _pending_shrink(removed):
    """push_authoritative callback for pending-candidate removals (a manual ✗
    reject, or a gatekeeper cycle that consumed/rejected candidates): keep
    each removed (variant, correct) pair out of pending after every merge
    attempt, so a stale remote can never resurrect it. Idempotent. Belt and
    braces with _prune_pending, which already drops rejected/consumed
    candidates at the merge site — this also covers candidates that landed
    as style/format rules (whose pending entry has no terms-side marker)."""
    def shrink(doc):
        pending = doc.get("pending") or {}
        for v, c in removed:
            vs = pending.get(c)
            if vs and v in vs:
                vs.remove(v)
                if not vs:
                    del pending[c]
    return shrink


def capture_format_edit(pasted, edited):
    """If a paste vs edit differs in LINE STRUCTURE, ask Sonnet whether there is
    a reusable formatting preference; non-null → append to style.notes. Async."""
    if not _line_structure_changed(pasted, edited):
        return
    prompt = FORMAT_CAPTURE_PROMPT + json.dumps(
        {"pasted": pasted[:1200], "edited": edited[:1200]}, ensure_ascii=False)
    try:
        out = subprocess.run([CLAUDE_BIN, "-p", prompt, "--model", "sonnet"],
                             capture_output=True, text=True, timeout=90).stdout
        note = json.loads(re.search(r"\{.*\}", out, re.S).group()).get("note")
    except Exception as exc:
        _log(f"format capture failed: {exc}")
        return
    if not note or not isinstance(note, str) or note.strip().lower() == "null":
        return
    note = note.strip()
    with _dict_lock:
        doc = load_doc()
        style = doc.setdefault("style", {})
        if not _add_style_note(style, note):
            return
        append_changelog(doc, "format", note, "auto")
        save_doc(doc)
    _log(f"format capture learned: {note}")


_BULLET_RE = re.compile(r"^\s*[-•*]\s")
_NUMBER_RE = re.compile(r"^\s*(\d+[.)]|第[一二三四五六七八九十]+點)")


def _line_structure_changed(a, b):
    def sig(t):
        lines = (t or "").split("\n")
        return (len([l for l in lines if l.strip()]),
                sum(1 for l in lines if _BULLET_RE.match(l)),
                sum(1 for l in lines if _NUMBER_RE.match(l)))
    return sig(a) != sig(b)













NOTE_SIMILARITY_THRESHOLD = 0.65














_CONFLICTING_MARKER_PAIRS = (("全形", "半形"), ("中文", "英文"), ("有", "冇"))
_NEGATION_PARTICLES = ("唔", "不", "非")
_DIGIT_RE = re.compile(r"\d+")
_QUOTED_SYMBOL_RE = re.compile(r"「([^」]*)」")


def _has_conflicting_markers(x, y):









    for a, b in _CONFLICTING_MARKER_PAIRS:
        if (a in x and b in y and a not in y) or (b in x and a in y and b not in y):
            return True
    for particle in _NEGATION_PARTICLES:
        if (particle in x) != (particle in y):
            return True
    dx, dy = _DIGIT_RE.findall(x), _DIGIT_RE.findall(y)
    if (dx or dy) and dx != dy:
        return True










    qx, qy = _QUOTED_SYMBOL_RE.findall(x), _QUOTED_SYMBOL_RE.findall(y)
    if qx and qy and qx[-1] != qy[-1]:
        return True
    return False


def _similar(x, y):
    if x == y:
        return True
    if _has_conflicting_markers(x, y):
        return False
    return difflib.SequenceMatcher(None, x, y).ratio() > NOTE_SIMILARITY_THRESHOLD


def unlearn_reverted(cands, terms):
    """If the user changed a rule's OUTPUT back into one of its variants, the
    rule itself was wrong — remove that variant. Returns (survivors, removed)."""
    survivors, removed = [], []
    for variant, correct in cands:
        hit = False
        if variant in terms:
            for pat in list(terms[variant]):
                try:
                    if re.fullmatch(pat.replace(r"\b", ""), correct, re.IGNORECASE):
                        terms[variant].remove(pat)
                        removed.append([pat, variant])
                        hit = True
                except re.error:
                    continue
        if not hit:
            survivors.append((variant, correct))
    return survivors, removed


def learn_from_edit(pasted, edited):
    """The whole auto-learn pipeline for one observed post-paste edit.

    GATEKEEPER REWRITE (2026-07-23): this used to hand every diffed candidate
    straight to the cloud-Sonnet judge and commit its verdict the same
    round-trip (accepted/style/format could all go live off ONE edit). That
    is exactly the "auto-promote" behaviour that turned out to be wrong more
    often than it looked: Tinho editing a pasted dictation is just as likely
    to be a rephrase or a changed mind as it is a genuine mishearing, and a
    single sample can't tell those apart. Promotion is now entirely the
    review_gate's job — see review_gate.py's module docstring — so this
    function no longer calls judge_with_ai/apply_judgement at all; it only
    (a) removes a rule Tinho just reverted (a deletion is safe on its own,
    unrelated to the promote-on-weak-evidence problem) and (b) durably queues
    every candidate for later, off-paste-path, local-qwen review. Nothing in
    here calls an LLM — it's cheap enough to run straight off Hammerspoon's
    Accessibility watcher."""
    result = {"queued": [], "removed": []}
    cands = extract_corrections(pasted, edited)
    if not cands:
        return result
    with _dict_lock:
        doc = load_doc()
        terms = doc.setdefault("terms", {})
        cands, removed = unlearn_reverted(cands, terms)
        for pat, var in removed:
            append_changelog(doc, "removed", f"{var} rule 撤回 ({pat})", "auto")

            add_rejected(doc, "dict", _rej_key("dict", pat, var), pat)




            raw = re.sub(r"\\(.)", r"\1", pat)
            if raw != pat:
                add_rejected(doc, "dict", _rej_key("dict", raw, var))
        if removed:
            save_doc(doc)
    if removed:




        submit_maintenance(
            "push-unlearn", push_authoritative, _unlearn_shrink(removed),
            "dictation: unlearn reverted rule")
    result["removed"] = removed
    for variant, correct in cands:
        if review_gate.enqueue(variant, correct, "learn"):
            result["queued"].append([variant, correct])
    _log(f"learn: {result}")
    return result































SHADOW_FLUSH_MAX_LOAD = float(
    os.environ.get("DICTATION_SHADOW_FLUSH_MAX_LOAD", "4.0"))

SHADOW_JUDGE_MODEL = os.environ.get("SHADOW_JUDGE_MODEL", "local")
SHADOW_LOCAL_MODEL = "qwen2.5:7b"
SHADOW_LOCAL_TIMEOUT_S = 60
SHADOW_CLOUD_TIMEOUT_S = 120
SHADOW_MIN_CHARS = 20
_hist_lock = threading.Lock()







CLAUDE_TEACHER_BATCH_N = 4
CLAUDE_TEACHER_PAIR_CHARS = 160
CLAUDE_TEACHER_KNOWN_WRONG_MAX = 6
CLAUDE_TEACHER_KNOWN_WRONG_CHARS = 24
CLAUDE_TEACHER_TIMEOUT_S = SHADOW_CLOUD_TIMEOUT_S










SHADOW_PROMPT = """你係 Tinho 語音轉文字系統嘅事後校對員。以下 JSON 有一次聽寫嘅「raw」（原始語音辨識）同「final」（已經潤色貼咗出嚟嘅版本）。

任務一 — 搵出聽錯／用錯字，分三類輸出候選規則（每項都要自己帶埋錯／啱一對）：
- "dict"：真.聽錯嘅專有名詞／工具名／人名／固定串法，格式 [{"wrong":"聽錯寫法","right":"正確寫法"}...]。
- "style"：粵語語氣／用字偏好（例如 比→畀、地→哋、大細楷習慣），格式 [{"pattern":"帶上下文保護嘅正則","replace":"..."}...]。pattern 絕對唔可以淨係一個中文字，一定要帶上下文（lookahead/lookbehind 或者 ≥2 個字），例如 "比(?=你|我|佢|人|大家)"。
- "format"：可重用嘅排版偏好或者標點習慣。如果你留意到一個「重複出現、值得學」嘅標點習慣（例如某類問句成日漏咗問號、長句成日冇逗號、半形句號應轉全形），就寫成一句中文指示放入呢度。格式 ["一句中文指示"...]。

任務二 — "improved"：一個完整、盡量修正咗上文下理嘅版本：
- 可以修正你推斷到嘅聽錯字、修正分段。
- 一定要順手改好標點位置：問句用「？」、長句喺自然停頓加逗號、完整意思收「。」、並列用頓號「、」、半形標點轉全形。
- 絕對唔好翻譯、絕對唔好摘要或者縮短、絕對唔好加前言標題或者解釋。
- 長度要同 final 相差唔超過 15%。

只輸出 JSON：{"dict":[...],"style":[...],"format":[...],"improved":"..."}，唔好有其他文字。

"""






CLAUDE_TEACHER_PROMPT = """你係 Tinho 語音轉文字系統嘅 Claude teacher。只可閱讀以下文字 JSON：每項係原始 ASR `raw` 同已由現有流程校對過嘅 `final`；絕對冇音訊，唔好聲稱聽過音訊。

根據 raw/final 差異，為每項找值得重複學習嘅 ASR 更正候選。`known_wrong` 係本批文字中已知規則，唔好重提。寧可留空，唔好猜測。
- `dict`: 專有名詞、工具名、人名或固定串法，`[{"wrong":"...","right":"..."}]`。
- `style`: 只限有上下文保護嘅正則，`[{"pattern":"...","replace":"..."}]`；唔好單一中文字。
- `format`: 可重用排版偏好，`["..."]`。
每項最多兩個候選，候選文字最多 40 個字。唔好改寫全文；`improved` 永遠填空字串。
只輸出 JSON：`{"items":[{"i":0,"dict":[],"style":[],"format":[],"improved":""}]}`，每個輸入 i 最多一項，唔好輸出其他文字。

"""


def _parse_shadow_json(out):
    """Extract+parse the {"dict":...,"style":...,"format":...,"improved":...}
    object from a proposer's raw stdout/response text. Returns None on any
    failure — every caller already treats None as "no proposal this round",
    never raises."""
    if not out:
        return None
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group())
    except json.JSONDecodeError:
        return None


def _clip_teacher_text(text, limit):
    """Keep a teacher prompt bounded even when one dictation is unusually long."""
    return (text or "").strip()[:limit]


def _teacher_known_wrong_patterns(items):
    """Return only live dictionary patterns relevant to this text batch.

    This is intentionally a relevance filter, not a dictionary dump: teacher
    batches should know what they are seeing without paying for unrelated
    vocabulary.  The normal downstream dedupe remains authoritative.
    """
    text = "\n".join((item.get("raw") or "") + "\n" +
                     (item.get("final") or "") for item in items)
    if not text:
        return []
    known = []
    try:
        terms = (load_doc().get("terms") or {})
        for right, wrongs in terms.items():
            for wrong in wrongs or []:
                if not isinstance(wrong, str):
                    continue
                try:
                    matches = re.search(wrong, text)
                except re.error:
                    matches = False
                if not matches:
                    continue
                entry = {"wrong": _clip_teacher_text(
                    wrong, CLAUDE_TEACHER_KNOWN_WRONG_CHARS),
                    "right": _clip_teacher_text(
                    str(right), CLAUDE_TEACHER_KNOWN_WRONG_CHARS)}
                known.append(entry)
                if len(known) >= CLAUDE_TEACHER_KNOWN_WRONG_MAX:
                    return known
    except Exception as exc:
        _log(f"claude teacher known-pattern read failed: {exc}")
    return known


def _parse_claude_teacher_json(out, item_count):
    """Parse one batched teacher response into normal per-item shadow data."""
    obj = _parse_shadow_json(out)
    entries = obj.get("items") if isinstance(obj, dict) else None
    if not isinstance(entries, list):
        return {}
    result = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("i"), int):
            continue
        i = entry["i"]
        if i < 0 or i >= item_count or i in result:
            continue


        result[i] = {"dict": entry.get("dict") or [],
                     "style": entry.get("style") or [],
                     "format": entry.get("format") or [],
                     "improved": entry.get("improved") or ""}
    return result


def claude_teacher_propose_batch(items):
    """Ask Sonnet for candidates from a capped, text-only batch.

    Returned values are *only* proposal objects.  This function never reads
    audio and never writes dictionary.json; callers must route each result
    through _apply_shadow_result/apply_shadow_candidates like local qwen does.
    """
    items = list(items[:CLAUDE_TEACHER_BATCH_N])
    payload = {"items": [
        {"i": i,
         "raw": _clip_teacher_text(item.get("raw"), CLAUDE_TEACHER_PAIR_CHARS),
         "final": _clip_teacher_text(item.get("final"), CLAUDE_TEACHER_PAIR_CHARS)}
        for i, item in enumerate(items)],
        "known_wrong": _teacher_known_wrong_patterns(items)}
    prompt = CLAUDE_TEACHER_PROMPT + json.dumps(payload, ensure_ascii=False,
                                                 separators=(",", ":"))
    try:
        out = subprocess.run([CLAUDE_BIN, "-p", prompt, "--model", "sonnet"],
                             capture_output=True, text=True,
                             timeout=CLAUDE_TEACHER_TIMEOUT_S).stdout
    except Exception as exc:
        _log(f"claude teacher unavailable: {exc}")
        return {}
    return _parse_claude_teacher_json(out, len(items))


def _shadow_propose_cloud(raw, final):
    """Cloud Sonnet proposer call — unchanged shape/prompt from the original
    per-dictation implementation. Used in production when
    SHADOW_JUDGE_MODEL=cloud, and ALWAYS as the weekly QA's ground truth
    (see weekly_shadow_qa) regardless of the live routing, since the QA's
    whole point is checking local qwen's recall against cloud Sonnet."""
    prompt = SHADOW_PROMPT + json.dumps(
        {"raw": raw or "", "final": final}, ensure_ascii=False)
    try:
        out = subprocess.run([CLAUDE_BIN, "-p", prompt, "--model", "sonnet"],
                             capture_output=True, text=True,
                             timeout=SHADOW_CLOUD_TIMEOUT_S).stdout
    except Exception as exc:
        _log(f"shadow sonnet unavailable: {exc}")
        return None
    return _parse_shadow_json(out)


def _shadow_propose_local(raw, final):
    """Local ollama qwen2.5:7b proposer call (D19 #23). Same prompt/JSON
    contract as the cloud call — this model only PROPOSES candidates, it
    never judges/writes; apply_shadow_candidates' existing local guards
    (anti-re-proposal filter, admission gate, recurrence threshold) and the
    cloud-only gatekeeper (judge_with_ai) are completely unaffected by which
    model produced the proposal. This background model must not remain
    resident beside the latency-critical live-polish model: keep_alive=0
    tells Ollama to unload it immediately after the response."""
    prompt = SHADOW_PROMPT + json.dumps(
        {"raw": raw or "", "final": final}, ensure_ascii=False)
    payload = {
        "model": SHADOW_LOCAL_MODEL, "prompt": prompt, "stream": False,
        "keep_alive": 0, "format": "json",
        "options": {"temperature": 0.1, "top_p": 0.9},
    }
    req = urllib.request.Request(
        polish_mod.OLLAMA_URL, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    resp = None
    try:
        resp = urllib.request.urlopen(req, timeout=SHADOW_LOCAL_TIMEOUT_S)



        with polish_mod._inflight_lock:
            polish_mod._inflight_resps.add(resp)
        with resp:
            obj = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        _log(f"shadow local qwen unavailable: {exc}")
        return None
    finally:
        if resp is not None:
            with polish_mod._inflight_lock:
                polish_mod._inflight_resps.discard(resp)
    return _parse_shadow_json(obj.get("response", ""))


def shadow_propose(raw, final, model=None):
    """Route one proposer call to local qwen or cloud Sonnet per
    SHADOW_JUDGE_MODEL (or an explicit `model` override — used by the weekly
    QA to force "cloud" for ground truth regardless of the live setting).
    Returns the parsed {"dict","style","format","improved"} dict, or None."""
    which = model or SHADOW_JUDGE_MODEL
    if which == "cloud":
        return _shadow_propose_cloud(raw, final)
    return _shadow_propose_local(raw, final)


def set_history_field(time_key, field, value):
    """Attach a field to the history entry matched by time. Serialised so
    concurrent shadow threads (one per dictation) never clobber the log."""
    with _hist_lock:
        if not os.path.exists(HISTORY_PATH):
            return False
        entries, hit = [], False
        with open(HISTORY_PATH, encoding="utf-8") as fh:
            for line in fh:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        for e in entries:
            if e.get("time") == time_key and not hit:
                e[field] = value
                hit = True
        if not hit:
            return False
        with open(HISTORY_PATH, "w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
        return True












SHADOW_RECURRENCE_MIN = 2









SHADOW_CANDIDATES_CAP = 100


def _bump_shadow_candidate(doc, kind, key, fields):
    """Count one shadow proposal of (kind, key). Returns True once the count
    reaches SHADOW_RECURRENCE_MIN — the caller then commits the rule; the
    entry is removed here (its job is done).

    RECURRENCE COUNT SURVIVES CAP EVICTION (2026-07-26, audit finding): the
    OLD behaviour dropped a candidate's accumulated `count` the moment
    SHADOW_CANDIDATES_CAP evicted its full entry — so a correction that
    genuinely recurs, just more than SHADOW_CANDIDATES_CAP proposals apart,
    could NEVER reach SHADOW_RECURRENCE_MIN: eviction always reset it to 0
    before it could accumulate again. That is a structural dead end, the
    same shape as the replay_verify blind spot, not a threshold-tuning
    problem — raising the cap only delays it. `shadow_candidate_progress` is
    a second, minimal store (kind + the same identifying fields + count
    only — no timestamps, no extra context) updated on EVERY bump regardless
    of whether the full entry gets evicted this cycle. It is never
    destructively capped by size; instead it is pruned the exact same way
    _prune_shadow_candidates already prunes shadow_candidates itself (once
    the rule is live or rejected, progress for it is stale and dropped) —
    its size tracks the number of genuinely-still-open recurring
    corrections, a purpose-bounded quantity, not a chosen number. format
    candidates match by _similar() so a slightly reworded re-proposal of the
    same preference still counts as recurrence rather than starting a fresh
    counter — now checked against BOTH stores so a reworded re-proposal
    still finds progress even after the original full entry was evicted."""
    store = doc.setdefault("shadow_candidates", {})
    progress = doc.setdefault("shadow_candidate_progress", {})
    full_key = f"{kind}:{key}"
    if full_key not in store and kind == "format":
        for k in store:
            if k.startswith("format:") and store[k].get("kind") == "format" \
                    and _similar(key, k[len("format:"):]):
                full_key = k
                break
    if full_key not in store and full_key not in progress and kind == "format":
        for k in progress:
            if k.startswith("format:") and progress[k].get("kind") == "format" \
                    and _similar(key, k[len("format:"):]):
                full_key = k
                break
    now = datetime.datetime.now().isoformat(timespec="seconds")
    ent = store.get(full_key)
    if ent is None:
        prior = progress.get(full_key)
        seed_count = int(prior.get("count", 0)) if prior else 0
        ent = store[full_key] = dict(fields, kind=kind, count=seed_count, first=now)
    ent["count"] = int(ent.get("count", 0)) + 1
    ent["last"] = now
    progress[full_key] = dict(fields, kind=kind, count=ent["count"])
    if ent["count"] >= SHADOW_RECURRENCE_MIN:
        del store[full_key]
        progress.pop(full_key, None)
        if not progress:
            doc.pop("shadow_candidate_progress", None)
        return True
    if len(store) > SHADOW_CANDIDATES_CAP:
        for k in sorted(store, key=lambda k: store[k].get("last", ""))[
                :len(store) - SHADOW_CANDIDATES_CAP]:
            del store[k]
    return False


def _shadow_entry_is_resolved(e, terms, subs, notes, rejected):
    """True once (kind, identifying fields) in `e` is already live or
    rejected — shared by _prune_shadow_candidates for BOTH shadow_candidates
    (full entries) and shadow_candidate_progress (count-only entries, same
    kind + identifying fields, added 2026-07-26): a candidate that has
    already landed or been rejected has served its purpose in either store."""
    e = e if isinstance(e, dict) else {}
    kind = e.get("kind")
    if kind == "dict":
        w, r = e.get("wrong") or "", e.get("right") or ""
        vs = terms.get(r, [])
        return (w in terms or w in vs or re.escape(w) in vs
                or is_rejected(rejected, "dict", _rej_key("dict", w, r)))
    if kind == "style":
        p = e.get("pattern")
        return p in subs or is_rejected(rejected, "style", p)
    if kind == "format":
        n = e.get("note") or ""
        return any(_similar(n, x) for x in notes) \
            or is_rejected(rejected, "format", n)
    return True


def _prune_shadow_candidates(doc):
    """Deterministic merge-site collapse (same doctrine as
    consolidate_style_subs): drop tracked shadow candidates (both the full
    shadow_candidates store and the count-only shadow_candidate_progress
    store, 2026-07-26) whose rule is already live or rejected. Committing a
    candidate removes its entry — a shrink — and a union-merge against a
    stale remote would otherwise bring the entry straight back; since the
    committed rule is by then live in the merged doc, this prune makes that
    resurrection impossible. Idempotent."""
    terms = doc.get("terms") or {}
    subs = {s.get("pattern") for s in (doc.get("style") or {}).get("subs", [])}
    notes = (doc.get("style") or {}).get("notes", [])
    rejected = doc.get("rejected", [])

    store = doc.get("shadow_candidates")
    if store:
        for k in list(store):
            if _shadow_entry_is_resolved(store[k], terms, subs, notes, rejected):
                del store[k]
    if store is not None and not store:
        doc.pop("shadow_candidates", None)

    progress = doc.get("shadow_candidate_progress")
    if progress:
        for k in list(progress):
            if _shadow_entry_is_resolved(progress[k], terms, subs, notes, rejected):
                del progress[k]
    if progress is not None and not progress:
        doc.pop("shadow_candidate_progress", None)


def apply_shadow_candidates(data, source):
    """Route shadow-discovered errors through the SAME safety invariants as
    apply_judgement: no bare single-Han terms rule, context-safe style patterns
    only; a single-Han swap goes to pending for the Sonnet retry to
    contextualise. On top of that, two shadow-specific gates:

    - LOCAL anti-re-proposal filter (replaces the prompt digest removed
      2026-07-22): candidates that duplicate a live rule, target a known
      canonical term spelling, or match rejected memory are dropped by
      set-membership here — the model no longer needs to be told what exists.
    - Recurrence threshold: a surviving candidate is only committed once it
      has been proposed SHADOW_RECURRENCE_MIN times (see above)."""
    dicts = [d for d in (data.get("dict") or []) if isinstance(d, dict)]
    styles = [s for s in (data.get("style") or []) if isinstance(s, dict)]
    formats = [f for f in (data.get("format") or []) if isinstance(f, str)]
    if not (dicts or styles or formats):
        return {"dict": [], "style": [], "format": [], "pending": []}
    acc = {"dict": [], "style": [], "format": [], "pending": [],
           "dropped": [], "deferred": []}
    with _dict_lock:
        doc = load_doc()
        terms = doc.setdefault("terms", {})
        pending = doc.setdefault("pending", {})
        style = doc.setdefault("style", {})
        subs = style.setdefault("subs", [])
        notes = style.setdefault("notes", [])
        rejected = doc.get("rejected", [])
        lower_terms = {k.lower() for k in terms}
        changed = False
        for d in dicts:
            wrong = (d.get("wrong") or "").strip()
            right = (d.get("right") or "").strip()
            if not wrong or not right or wrong == right:
                continue
            if not _CONTENT.search(wrong) or not _CONTENT.search(right):
                continue


            if wrong in terms or wrong.lower() in lower_terms:
                acc["dropped"].append(["dict", f"{wrong}→{right} (已知正確詞)"])
                continue
            if _is_single_han(wrong) and _is_single_han(right):
                if wrong not in pending.setdefault(right, []):
                    pending[right].append(wrong)
                    acc["pending"].append([wrong, right])
                    changed = True
                continue
            if is_rejected(rejected, "dict", _rej_key("dict", wrong, right)):
                acc["dropped"].append(["dict", f"{wrong}→{right}"])
                continue



            if rule_audit.is_unsafe_wrong(wrong, right):
                add_rejected(doc, "dict", _rej_key("dict", wrong, right))
                append_changelog(doc, "removed",
                                 f"gate 自動拒絕常用詞 rule：{wrong}→{right}",
                                 source)
                acc["dropped"].append(["dict", f"{wrong}→{right} (常用詞)"])
                changed = True
                _log(f"admission gate rejected common-word shadow rule: "
                     f"{wrong}→{right}")
                continue
            esc = re.escape(wrong)
            vs = terms.get(right, [])
            if esc in vs or wrong in vs:
                continue
            changed = True
            if not _bump_shadow_candidate(doc, "dict", f"{wrong}→{right}",
                                          {"wrong": wrong, "right": right}):
                acc["deferred"].append(["dict", f"{wrong}→{right}"])
                continue
            terms.setdefault(
                rule_audit.existing_canonical(terms, right), []).append(esc)
            append_changelog(doc, "dict", f"{wrong}→{right}", source)
            acc["dict"].append([wrong, right])
        for s in styles:
            pat, rep = s.get("pattern"), s.get("replace")
            if not isinstance(pat, str) or not isinstance(rep, str):
                continue
            try:
                re.compile(pat)
            except re.error:
                continue
            if not _is_context_safe(pat):
                _log(f"shadow style rejected (no context): {pat}")
                continue
            if is_rejected(rejected, "style", pat):
                acc["dropped"].append(["style", pat])
                continue
            if any(x.get("pattern") == pat for x in subs):
                continue
            changed = True
            if not _bump_shadow_candidate(doc, "style", pat,
                                          {"pattern": pat, "replace": rep}):
                acc["deferred"].append(["style", pat])
                continue
            subs.append({"pattern": pat, "replace": rep, "note": f"→{rep} (shadow)"})
            append_changelog(doc, "style", f"{pat} → {rep}", source)
            acc["style"].append([pat, rep])
        for note in formats:
            note = note.strip()
            if not note:
                continue
            if is_rejected(rejected, "format", note):
                acc["dropped"].append(["format", note])
                continue
            changed = True
            if not _bump_shadow_candidate(doc, "format", note, {"note": note}):
                acc["deferred"].append(["format", note])
                continue





            if not _add_style_note(style, note):
                continue
            append_changelog(doc, "format", note, source)
            acc["format"].append(note)
        if changed:
            save_doc(doc)
    _log(f"shadow candidates: {acc}")
    return acc


def _apply_shadow_result(time_key, final, data, source="shadow"):
    """Apply one proposer result through the one shared shadow validation path.

    Both local-qwen and Claude-teacher results arrive here.  Keeping the write
    call in this one helper is intentional: a teacher candidate has no route
    around apply_shadow_candidates' admission, rejection, dedupe, and
    recurrence gates.
    """
    if not data:
        return
    improved = data.get("improved")
    if isinstance(improved, str):
        improved = improved.strip()
        lo, hi = len(final) * 0.85, len(final) * 1.15
        if improved and lo <= len(improved) <= hi:
            set_history_field(time_key, "shadow", improved)
            _log(f"shadow stored improved ({len(improved)} chars) for {time_key}")
        else:
            _log(f"shadow improved rejected (len "
                 f"{len(improved) if improved else 0} vs final {len(final)})")
    return apply_shadow_candidates(data, source)


def shadow_improve(time_key, raw, final):
    """One proposer pass for one completed dictation (called from
    flush_shadow_queue, per dictation, during a batch flush — see below).
    Routes to local qwen or cloud Sonnet per shadow_propose/SHADOW_JUDGE_MODEL.
    Stores a context-repaired 'shadow' on the history entry and files any
    discovered corrections through the unchanged gatekeeper/guard path."""
    if not final or len(final) < SHADOW_MIN_CHARS:
        return
    data = shadow_propose(raw, final)
    if data is None:
        return
    return _apply_shadow_result(time_key, final, data)










SHADOW_QUEUE_PATH = os.path.join(HERE, ".shadow_queue.jsonl")
SHADOW_QUEUE_STATE_PATH = os.path.join(HERE, ".shadow_queue_state")
SHADOW_BATCH_N = 10
SHADOW_BATCH_INTERVAL_S = 30 * 60
_shadow_queue_lock = threading.Lock()


def _shadow_queue_state():
    try:
        with open(SHADOW_QUEUE_STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_shadow_queue_state(state):
    try:
        with open(SHADOW_QUEUE_STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)
    except OSError as exc:
        _log(f"shadow queue state save failed: {exc}")


def _shadow_queue_len():
    if not os.path.exists(SHADOW_QUEUE_PATH):
        return 0
    try:
        with open(SHADOW_QUEUE_PATH, encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())
    except OSError:
        return 0


def _shadow_batch_due(queue_len, queue_started_at, now=None):
    """Pure due-check (mirrors _consolidation_due's time-gate-OR-count-gate
    shape): fires once `queue_len` reaches SHADOW_BATCH_N, or
    SHADOW_BATCH_INTERVAL_S has elapsed since `queue_started_at` (the
    timestamp the currently-queued batch STARTED, i.e. when it went from
    empty to 1 item — not "last flush"), whichever comes first."""
    if queue_len <= 0:
        return False
    if queue_len >= SHADOW_BATCH_N:
        return True
    if not queue_started_at:
        return False
    try:
        started = datetime.datetime.fromisoformat(queue_started_at)
    except ValueError:
        return False
    now = now or datetime.datetime.now()
    return (now - started).total_seconds() >= SHADOW_BATCH_INTERVAL_S


def enqueue_shadow_item(time_key, raw, final):
    """Append one dictation to the batch queue; seeds queue_started_at the
    moment the queue goes 0 -> 1 so the 30-min time gate is measured from
    "oldest item still waiting", not from the last flush. Returns the queue
    length after the append (0 on a write failure, e.g. disk full — logged,
    never raises; a dropped shadow proposal is low-stakes internal
    rule-mining fuel, never the dictation itself)."""
    with _shadow_queue_lock:
        was_empty = _shadow_queue_len() == 0
        try:
            with open(SHADOW_QUEUE_PATH, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(
                    {"time": time_key, "raw": raw or "", "final": final},
                    ensure_ascii=False) + "\n")
        except OSError as exc:
            _log(f"shadow queue enqueue failed: {exc}")
            return _shadow_queue_len()
        if was_empty:
            _save_shadow_queue_state({
                "queue_started_at": datetime.datetime.now()
                .isoformat(timespec="seconds")})
        return _shadow_queue_len()


def _flush_claude_teacher_items(items):
    """Run queued work in capped teacher rounds, then use the normal gate.

    `items` remains the same durable queue used by local qwen.  A result is
    applied once per source item only after the single Claude batch call has
    returned; no proposal writes directly to dictionary.json.
    """
    for start in range(0, len(items), CLAUDE_TEACHER_BATCH_N):
        batch = items[start:start + CLAUDE_TEACHER_BATCH_N]
        recording_gate.wait_until_idle(log=_log)
        try:
            proposed = claude_teacher_propose_batch(batch)
        except Exception as exc:
            _log(f"claude teacher batch failed (non-fatal): {exc}")
            continue
        for i, item in enumerate(batch):
            data = proposed.get(i)
            if data is None:
                continue
            try:
                _apply_shadow_result(item.get("time"), item.get("final"), data,
                                     source="claude-teacher")
            except Exception as exc:
                _log(f"claude teacher item failed (non-fatal): {exc}")


def flush_shadow_queue():
    """Process every currently-queued dictation through shadow_improve (in
    turn — proposer calls are NOT parallelised, mirroring the "one Sonnet/
    ollama call at a time" spirit of the rest of this file's background
    work), then clear the queue. A no-op on an empty queue, so both the
    load-triggered call site (shadow_improve_async) and the periodic
    backstop poll (consolidation_worker) can call it unconditionally after
    their own due-check."""
    with _shadow_queue_lock:
        items = []
        if os.path.exists(SHADOW_QUEUE_PATH):
            try:
                with open(SHADOW_QUEUE_PATH, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            items.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            except OSError:
                pass
        if not items:
            return
        try:
            os.remove(SHADOW_QUEUE_PATH)
        except OSError as exc:









            _log(f"shadow queue clear failed, state left intact for retry: {exc}")
            return
        _save_shadow_queue_state({"queue_started_at": None})







    recording_gate.wait_until_idle(log=_log)























    try:
        load1 = os.getloadavg()[0]
    except OSError:
        load1 = 0.0
    if SHADOW_FLUSH_MAX_LOAD > 0 and load1 > SHADOW_FLUSH_MAX_LOAD:
        _log(f"shadow batch flush deferred: 1-min load {load1:.2f} > "
             f"{SHADOW_FLUSH_MAX_LOAD:.2f} — dictation capture has priority "
             f"(D25); {len(items)} item(s) stay queued")
        return
    _log(f"shadow batch flush: {len(items)} queued dictation(s), "
         f"model={SHADOW_JUDGE_MODEL}")
    if SHADOW_JUDGE_MODEL == "claude-teacher":
        _flush_claude_teacher_items(items)
        return
    for item in items:
        recording_gate.wait_until_idle(log=_log)
        try:
            shadow_improve(item.get("time"), item.get("raw"), item.get("final"))
        except Exception as exc:
            _log(f"shadow batch item failed (non-fatal): {exc}")


def flush_shadow_queue_async():
    submit_maintenance("shadow-batch", flush_shadow_queue, dedupe=True)


def shadow_improve_async(time_key, raw, final):
    """Enqueue one completed dictation for the batched shadow pass (D19 #23:
    every SHADOW_BATCH_N dictations or SHADOW_BATCH_INTERVAL_S, whichever
    first — replaces the old immediate per-dictation call). Same call
    signature as before this change, so server.py's call site
    (`editor.shadow_improve_async(thist, raw, text)`) needed no change.
    Fires the flush the INSTANT the batch becomes due (load-triggered,
    mirroring _maybe_consolidate_for_load) — consolidation_worker's periodic
    poll is only the backstop for the time-gate case at low dictation
    volume."""
    if not final or len(final) < SHADOW_MIN_CHARS:
        return
    n = enqueue_shadow_item(time_key, raw, final)
    state = _shadow_queue_state()
    if _shadow_batch_due(n, state.get("queue_started_at")):
        flush_shadow_queue_async()


def capture_spellouts(raw):
    """Submit each spell-out (mis-rendered word → assembled word) through the
    async Sonnet gatekeeper, exactly like an observed edit."""
    caps = polish_mod.detect_spellouts(raw or "")
    if not caps:
        return
    for tok, word in caps:
        _log(f"spellout capture: {tok} → {word}")
    cands = [(tok, word) for tok, word in caps]
    verdict = judge_with_ai(cands)
    with _dict_lock:
        doc = load_doc()
        if verdict is None:
            pending = doc.setdefault("pending", {})
            for tok, word in cands:
                if tok not in pending.setdefault(word, []):
                    pending[word].append(tok)
        else:
            apply_judgement(doc, cands, verdict, "spellout")
        save_doc(doc)


def capture_spellouts_async(raw):
    submit_maintenance("spellout-capture", capture_spellouts, raw)
















CONSOLIDATE_STATE_PATH = os.path.join(HERE, ".consolidate_state")
CONSOLIDATE_INTERVAL = 24 * 3600
CONSOLIDATE_MAX_NOTES = 12
CONSOLIDATE_LOAD_THRESHOLD = 18
CONSOLIDATE_MIN_SPACING = 2 * 3600









CONSOLIDATE_FAIL_ESCALATE_EVERY = 3
_consolidating = threading.Event()

CONSOLIDATE_PROMPT = """你係 Tinho 語音轉文字系統嘅風格規則整理員。以下 JSON 有：
- "notes"：而家所有格式／用詞／標點偏好規則（style.notes）。
- "changelog"：最近嘅改動記錄。
- "rejected"：Tinho 拒絕過嘅規則（唔可以再出現喺結果）。
- "subs"：而家生效嘅上下文替換規則 pattern（你淨係可以評論，唔可以改）。

任務：
1. "notes"：整合成一份精簡、唔互相矛盾嘅規則清單，最多 12 條，全部用廣東話，每條一句、清晰。合併重複、刪走互相矛盾同過時嘅、保留 Tinho 真正嘅偏好。唔好包含 rejected 入面拒絕咗嘅規則。
2. "bad_subs"：如果有邊條 subs pattern 睇落有問題（會誤傷、太闊、或者同 notes 矛盾），列出佢嘅 pattern 字串（淨係評論，我唔會自動改）。冇就 []。

只輸出 JSON：{"notes":[...],"bad_subs":[...]}，唔好有其他文字。

"""


def _consolidate_last():
    try:
        with open(CONSOLIDATE_STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh).get("last")
    except (OSError, json.JSONDecodeError):
        return None


def _consolidate_state_read():
    try:
        with open(CONSOLIDATE_STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _write_consolidate_state(status="ok"):
    """Record EVERY attempt (success or failure), not just successes — see the
    BUG note above CONSOLIDATE_FAIL_ESCALATE_EVERY. Also tracks a consecutive-
    failure streak, reset to 0 on "ok", so a chronic failure can be surfaced
    (D72: a permanently-skipped job must not stay invisible) instead of
    scrolling past in a log nobody reads. Returns the streak count."""
    prev = _consolidate_state_read()
    streak = 0 if status == "ok" else prev.get("consecutive_failures", 0) + 1
    try:
        with open(CONSOLIDATE_STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump({
                "last": datetime.datetime.now().isoformat(timespec="seconds"),
                "last_status": status,
                "consecutive_failures": streak,
            }, fh)
    except OSError:
        pass
    return streak


_CONSOLIDATE_NTFY_TOPIC_FILE = os.path.join(HERE, ".ntfy_topic")



def _consolidate_escalation_notify(streak, status):
    """Bounded FYI-level nudge once consolidation has failed
    CONSOLIDATE_FAIL_ESCALATE_EVERY times in a row (and every further
    multiple, so a chronic failure doesn't go silent again after the first
    nudge scrolls past). Reuses the existing ntfy path (NTFY_TOPIC env or
    .ntfy_topic file) — no new channel, Priority stays low/default, never the
    tinho-os urgent topic (D10a reserves that for stop-breach / circuit-
    breaker / system-down, and a stalled style-notes merge is none of those)."""
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        try:
            with open(_CONSOLIDATE_NTFY_TOPIC_FILE, encoding="utf-8") as fh:
                topic = fh.read().strip()
        except OSError:
            topic = ""
    if not topic:
        _log("consolidate: escalation ntfy skipped (no NTFY_TOPIC env / "
             ".ntfy_topic file)")
        return False
    body = (f"聽寫 consolidation 連續 {streak} 次失敗（{status}）—— "
            "style.notes 已經冇再整合，check ~/dictation/server.log")
    try:
        subprocess.run(
            ["curl", "-sS", "--max-time", "10",
             "-H", "Title: dictation consolidation stalled",
             "-H", "Priority: low", "-H", "Tags: warning",
             "-d", body, f"https://ntfy.sh/{topic}"],
            capture_output=True, timeout=15)
        return True
    except Exception as exc:
        _log(f"consolidate: escalation ntfy failed: {exc}")
        return False


def _consolidation_due(notes_count=None):
    """True on the ordinary >24h cadence, OR — if notes_count is given — as
    soon as style.notes has grown past CONSOLIDATE_LOAD_THRESHOLD, provided at
    least CONSOLIDATE_MIN_SPACING has passed since the last run. The min
    spacing is the only thing standing between fast shadow-learning growth and
    thrashing: without it, a burst of accepted notes right after a
    consolidation could fire another pass immediately."""
    last = _consolidate_last()
    if not last:
        return True
    try:
        dt = datetime.datetime.fromisoformat(last)
    except ValueError:
        return True
    elapsed = (datetime.datetime.now() - dt).total_seconds()
    if elapsed > CONSOLIDATE_INTERVAL:
        return True
    if notes_count is not None and notes_count > CONSOLIDATE_LOAD_THRESHOLD \
            and elapsed > CONSOLIDATE_MIN_SPACING:
        return True
    return False


def _maybe_consolidate_for_load(doc):
    """Called after every save_doc(): if style.notes just crossed the load
    threshold, kick a consolidation pass in the background. This is the
    load-triggered half of the fix — save_doc is the single choke point every
    note-adding path (gatekeeper accept, format-edit capture, shadow
    candidates) already funnels through, so wiring it here catches growth the
    instant it happens instead of waiting up to an hour for the periodic
    worker. Non-blocking: save_doc is called from request-handling and
    background threads alike. The actual pass is serialized on the maintenance
    lane and never invokes Claude."""
    try:
        notes_count = len((doc.get("style") or {}).get("notes", []))
    except AttributeError:
        return
    if notes_count <= CONSOLIDATE_LOAD_THRESHOLD:
        return
    if _consolidating.is_set():
        return
    if not _consolidation_due(notes_count):
        return
    _consolidating.set()

    def _run():
        try:
            _log(f"consolidation load-triggered: {notes_count} notes "
                 f"> {CONSOLIDATE_LOAD_THRESHOLD}")
            consolidate_notes()
        except Exception as exc:
            _log(f"load-triggered consolidate error: {exc}")
        finally:
            _consolidating.clear()

    submit_maintenance("consolidate-notes", _run, dedupe=True)








def write_notes_snapshot(notes):
    """Replace the injected-notes snapshot wholesale (consolidation boundary)."""
    try:
        with open(polish_mod.NOTES_SNAPSHOT_PATH, "w", encoding="utf-8") as fh:
            json.dump(list(notes), fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        _log(f"notes snapshot write failed: {exc}")


def _snapshot_remove_note(note):
    """Drop ONE note from the snapshot (✕ delete / re-review delete) without
    promoting any not-yet-consolidated notes into the prompt."""
    try:
        with open(polish_mod.NOTES_SNAPSHOT_PATH, encoding="utf-8") as fh:
            snap = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(snap, list) or note not in snap:
        return
    write_notes_snapshot([n for n in snap if n != note])


def _ensure_notes_snapshot():
    """Seed the snapshot from the live notes on first startup after this
    feature ships, so behaviour is unchanged until the next consolidation."""
    if os.path.exists(polish_mod.NOTES_SNAPSHOT_PATH):
        return
    write_notes_snapshot((load_doc().get("style") or {}).get("notes", []))












FLAGGED_SUB_REREVIEW_MIN = 2


def _record_flagged_subs(doc, bad_subs):
    """Bump the flag counter for each consolidation-flagged LIVE sub. Returns
    the patterns now due for re-review (count reached the threshold); their
    counters are cleared here — the queue entry is consumed by the dispatch,
    and a rule the re-review decides to keep starts again from zero rather
    than being re-reviewed on every future flag."""
    flagged = doc.setdefault("flagged_subs", {})
    live = {s.get("pattern") for s in (doc.get("style") or {}).get("subs", [])}
    due = []
    for pat in bad_subs:
        if not isinstance(pat, str) or pat not in live:
            continue
        n = int(flagged.get(pat, 0)) + 1
        if n >= FLAGGED_SUB_REREVIEW_MIN:
            due.append(pat)
            flagged.pop(pat, None)
        else:
            flagged[pat] = n
    _prune_flagged_subs(doc)
    return due


def _prune_flagged_subs(doc):
    """Merge-site/deterministic cleanup: drop flag counters for patterns that
    are no longer live subs (deleted, narrowed, or family-merged away).
    Idempotent; removes the section entirely when empty so synced docs stay
    minimal."""
    flagged = doc.get("flagged_subs")
    if flagged is None:
        return
    live = {s.get("pattern") for s in (doc.get("style") or {}).get("subs", [])}
    for pat in list(flagged):
        if pat not in live:
            del flagged[pat]
    if not flagged:
        doc.pop("flagged_subs", None)




















RULE_AUDIT_STATE_PATH = os.path.join(HERE, ".rule_audit_state")
RULE_AUDIT_INTERVAL = 12 * 3600

RULE_AUDIT_HISTORY_ROWS = 600
RULE_AUDIT_SUSPECT_CAP = 5
RULE_AUDIT_RULING_TTL = 30 * 86400
QUARANTINE_DAYS = 30




























































HISTORY_RECENCY_FLOOR = max(
    content_guard.CANDIDATE_LOOKBACK,
    review_gate.REPLAY_HISTORY_SAMPLE,
    RULE_AUDIT_HISTORY_ROWS,
)
HISTORY_ROW_BACKSTOP = 50 * HISTORY_RECENCY_FLOOR


def _history_row_still_needed(entry, rows_from_end, recordings_dir):
    """True if `entry` (rows_from_end positions from the end of the file,
    0 = newest) could still legitimately be read by some real consumer.

    D1 while a row is alive: this is deliberately permissive -- any
    uncertainty (e.g. an unparseable `time`/`audio` field) keeps the row
    rather than risking a real, unserved purpose being cut short."""
    if rows_from_end < HISTORY_RECENCY_FLOOR:
        return True
    audio = entry.get("audio") if isinstance(entry, dict) else None
    if audio:
        try:
            if audio in os.listdir(recordings_dir):
                return True
        except OSError:
            return True
    return False


def prune_served_history(recordings_dir=None):
    """Delete rows from history.jsonl once every real consumer has had them
    (see the module note above) — no archive, per Tinho's ruling: a served
    row is simply gone, not relocated. Returns a report dict (rows_before,
    rows_deleted, rows_after) for callers/tests; production callers ignore
    it. Uses the same _hist_lock every other history.jsonl read-modify-
    write in this module already serialises on."""
    recordings_dir = recordings_dir or os.path.join(HERE, "recordings")
    with _hist_lock:
        try:
            with open(HISTORY_PATH, encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            return None
        total = len(lines)
        if total <= HISTORY_RECENCY_FLOOR:
            return {"rows_before": total, "rows_deleted": 0, "rows_after": total}

        def _parsed(line):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                return {}






        kept = []
        for i, line in enumerate(lines):
            rows_from_end = total - 1 - i
            entry = _parsed(line)
            if rows_from_end >= HISTORY_ROW_BACKSTOP:
                continue
            if _history_row_still_needed(entry, rows_from_end, recordings_dir):
                kept.append(line)
        deleted = total - len(kept)
        if deleted == 0:
            return {"rows_before": total, "rows_deleted": 0, "rows_after": total}
        try:
            with open(HISTORY_PATH, "w", encoding="utf-8") as fh:
                fh.writelines(kept)
        except OSError as exc:
            _log(f"history prune failed: {exc}")
            return None
    _log(f"history pruned: {deleted} served rows deleted, {len(kept)} kept live")
    return {"rows_before": total, "rows_deleted": deleted, "rows_after": len(kept)}

AUDIT_REREVIEW_PROMPT = """你係語音轉文字字典嘅稽核員。以下一條自動學習返嚟嘅替換規則，個 "wrong" key 係常用詞，全局替換有機會誤傷正常句子。JSON 入面有規則本身、佢嘅出處（provenance）、同埋歷史證據：kept＝個詞喺歷史文字度以正常用法出現過幾多次（規則會誤傷）、fixed＝有證據佢真係修正過聽錯幾多次、contexts＝實際上下文片段。

根據證據判斷：
- 誤傷風險高、修正證據弱 → {"action": "delete"}
- 證據混合、唔確定 → {"action": "quarantine"}（規則暫停生效，只記錄本應觸發嘅位置，遲啲再判）
- 上下文顯示佢淨係喺專門場景出現、修正證據強 → {"action": "keep"}

只輸出 JSON，唔好有其他文字。資料：
"""


def _rule_audit_state():
    try:
        with open(RULE_AUDIT_STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _write_rule_audit_state(state):
    try:
        with open(RULE_AUDIT_STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)
    except OSError:
        return False
    return True


def self_test():
    """Quick deterministic smoke test for editor helpers."""
    assert _log("self-test probe")
    assert _write_sync_sha("sha-test")
    assert _write_rule_audit_state({"last": "2026-07-26T00:00:00"})
    return True


def _rule_audit_due():
    state = _rule_audit_state()
    last = state.get("last")
    if not last:
        return True
    try:
        dt = datetime.datetime.fromisoformat(last)
    except ValueError:
        return True
    return (datetime.datetime.now() - dt).total_seconds() > RULE_AUDIT_INTERVAL


def _load_history_rows(limit=RULE_AUDIT_HISTORY_ROWS):
    rows = []
    try:
        with open(HISTORY_PATH, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return rows[-limit:]


def audit_rereview(finding):
    """Sonnet arbitration for a SUSPECT finding, with the evidence attached.
    Returns 'keep' / 'delete' / 'quarantine' (defaults to keep on failure —
    the audit must never delete without a positive verdict)."""
    payload = {"rule": {"wrong": finding["wrong"], "right": finding["right"],
                        "kind": finding["kind"]},
               "provenance": finding["provenance"],
               "kept": finding["kept"], "fixed": finding["fixed"],
               "contexts": finding["contexts"]}
    if os.environ.get("REREVIEW_PAUSED") == "1":
        _log("rereview skipped (paused)")
        return "keep"
    if not _rereview_guard.acquire(blocking=False):
        _log("rereview skipped (already in flight)")
        return "keep"
    try:
        verdict = _run_rereview_judge(
            AUDIT_REREVIEW_PROMPT + json.dumps(payload, ensure_ascii=False))
        action = verdict.get("action")
        if action in ("keep", "delete", "quarantine"):
            return action
    except (subprocess.TimeoutExpired, TimeoutError):
        _log("rereview skipped (timeout)")
    except Exception as exc:
        _log(f"audit rereview unavailable: {exc}")
    finally:
        _rereview_guard.release()
    return "keep"


def _quarantine_review(doc, rows, now):
    """Decide the fate of each quarantined rule from fresh evidence gathered
    while it was under observation. Returns (restores, discards) entries."""
    restores, discards = [], []
    for entry in list(doc.get("quarantine") or []):
        since = entry.get("since") or ""
        recent = [r for r in rows if (r.get("time") or "") >= since]
        if entry.get("kind") == "term":
            wrong = rule_audit.unescape(entry.get("variant"))
            right = entry.get("term")
        else:
            wrong, right = entry.get("pattern"), entry.get("replace")
        ev = rule_audit.history_evidence(recent, wrong, right or "")
        if ev["fixed"] >= 2 and ev["kept"] == 0:
            restores.append(entry)
        elif ev["kept"] >= 2:
            discards.append(entry)
        else:
            try:
                dt = datetime.datetime.fromisoformat(since)
                expired = (now - dt).total_seconds() > QUARANTINE_DAYS * 86400
            except ValueError:
                expired = True
            if expired:
                discards.append(entry)
    return restores, discards


def weekly_rule_audit():
    """The scheduled pass. Cheap (no LLM except capped SUSPECT arbitration);
    every shrink rides push_authoritative + rejected memory, per doctrine."""
    now = datetime.datetime.now()
    rows = _load_history_rows()
    state = _rule_audit_state()
    ruled = state.get("ruled") or {}





    for k in list(ruled):
        if ruled[k].get("by") == "human":
            continue
        try:
            age = (now - datetime.datetime.fromisoformat(
                ruled[k].get("time") or "")).total_seconds()
        except ValueError:
            age = RULE_AUDIT_RULING_TTL + 1
        if age > RULE_AUDIT_RULING_TTL:
            del ruled[k]

    with _dict_lock:
        doc = load_doc()
        findings = rule_audit.audit_doc(doc, rows, _is_context_safe)
    deletions, quarantines, suspects = [], [], []
    for f in findings:
        key = f"{f['wrong']}→{f['right']}" if f["kind"] == "term" \
            else f["variant"]
        if key in ruled:
            continue
        if f["verdict"] == "DELETE":
            deletions.append(f)
        elif f["verdict"] == "QUARANTINE":
            quarantines.append(f)
        elif f["verdict"] == "SUSPECT":
            suspects.append(f)

    for f in suspects[:RULE_AUDIT_SUSPECT_CAP]:
        action = audit_rereview(f)
        key = f"{f['wrong']}→{f['right']}" if f["kind"] == "term" \
            else f["variant"]
        if action == "delete":
            f["reason"] += " [sonnet: delete]"
            deletions.append(f)
        elif action == "quarantine":
            f["reason"] += " [sonnet: quarantine]"
            quarantines.append(f)
        else:
            ruled[key] = {"verdict": "keep", "by": "sonnet",
                          "time": now.isoformat(timespec="seconds")}

    q_entries = []
    with _dict_lock:
        doc = load_doc()
        restores, discards = _quarantine_review(doc, rows, now)
        for f in deletions:
            rule = {"kind": "term", "term": f["right"],
                    "variant": f["variant"]} if f["kind"] == "term" else \
                {"kind": "style_sub", "pattern": f["variant"]}
            delete_rule(doc, rule, "audit")


            if f["kind"] == "term" and f["wrong"] != f["variant"]:
                add_rejected(doc, "dict",
                             _rej_key("dict", f["wrong"], f["right"]))
        for f in quarantines:
            rule = {"kind": "term", "term": f["right"],
                    "variant": f["variant"]} if f["kind"] == "term" else \
                {"kind": "style_sub", "pattern": f["variant"],
                 "sub": next((s for s in doc.get("style", {}).get("subs", [])
                              if s.get("pattern") == f["variant"]), {})}
            e = quarantine_rule(doc, rule, f["reason"], "audit")
            if e:
                q_entries.append(e)
        for e in restores:
            restore_quarantined(doc, e, "audit")
        for e in discards:
            discard_quarantined(doc, e, "audit")
        changed = bool(deletions or q_entries or restores or discards)
        if changed:
            append_changelog(
                doc, "audit",
                f"週審計：檢查 {len(findings)} 條，刪 {len(deletions)}，"
                f"隔離 {len(q_entries)}，恢復 {len(restores)}，"
                f"隔離判定刪除 {len(discards)}", "audit")
            _write_doc(doc)
            _dirty.set()

        del_rules = [{"kind": "term", "term": f["right"],
                      "variant": f["variant"]} if f["kind"] == "term" else
                     {"kind": "style_sub", "pattern": f["variant"]}
                     for f in deletions]
        resolved_snapshot = json.loads(json.dumps(
            doc.get("quarantine_resolved") or {}))
        restored_snapshot = json.loads(json.dumps(restores))

    state["last"] = now.isoformat(timespec="seconds")
    state["ruled"] = ruled
    _write_rule_audit_state(state)
    if not changed:
        _log(f"weekly rule audit: {len(findings)} rules checked, no action")
        return

    def _shrink(d):
        for rule in del_rules:
            _rule_shrink(rule)(d)
        _quarantine_shrink(q_entries)(d)


        if resolved_snapshot:
            qr = d.setdefault("quarantine_resolved", {})
            for k, e in resolved_snapshot.items():
                qr.setdefault(k, e)
        for e in restored_snapshot:
            if e.get("kind") == "term":
                vs = d.setdefault("terms", {}).setdefault(e.get("term"), [])
                if e.get("variant") not in vs:
                    vs.append(e.get("variant"))
            else:
                subs = d.setdefault("style", {}).setdefault("subs", [])
                if not any(s.get("pattern") == e.get("pattern") for s in subs):
                    subs.append({"pattern": e.get("pattern"),
                                 "replace": e.get("replace"),
                                 "note": e.get("note") or ""})
        _prune_quarantined(d)

    ok = push_authoritative(_shrink, "dictation: weekly rule audit")
    _log(f"weekly rule audit: checked={len(findings)} deleted={len(del_rules)} "
         f"quarantined={len(q_entries)} restored={len(restored_snapshot)} "
         f"discarded={len(discards)} push_ok={ok}")







def log_watchdog_event(desc):
    with _dict_lock:
        doc = load_doc()
        append_changelog(doc, "watchdog", desc, "watchdog")
        _write_doc(doc)
    _dirty.set()













USAGE_STATE_PATH = os.path.join(HERE, ".usage_summary_state")
USAGE_INTERVAL = 7 * 86400
RCLONE_REMOTE = "r2:tinho-personal"


def _usage_summary_due():
    try:
        with open(USAGE_STATE_PATH, encoding="utf-8") as fh:
            last = json.load(fh).get("last")
        dt = datetime.datetime.fromisoformat(last)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return True
    return (datetime.datetime.now() - dt).total_seconds() > USAGE_INTERVAL


def _rclone_size(path):
    """{"count": n, "bytes": b} via `rclone size --json`, or None."""
    try:
        out = subprocess.run(["rclone", "size", path, "--json"],
                             capture_output=True, text=True, timeout=120)
        if out.returncode != 0:
            return None
        return json.loads(out.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def weekly_usage_summary():
    """One changelog line per week: R2 total + inbox-audio proxy for Modal
    usage. State is only written on success, so a failed rclone call just
    retries on the next poll instead of losing a week."""
    total = _rclone_size(RCLONE_REMOTE)
    inbox = _rclone_size(RCLONE_REMOTE + "/inbox-audio")
    if total is None and inbox is None:
        _log("usage summary: rclone unavailable, will retry next poll")
        return False
    def _fmt(b):
        return f"{b / 1e9:.2f} GB" if b >= 1e9 else f"{b / 1e6:.1f} MB"

    parts = []
    if total:
        parts.append(f"R2 全桶 {_fmt(total.get('bytes', 0))}"
                     f"／{total.get('count', 0)} 檔")
    if inbox:
        parts.append(f"inbox-audio {inbox.get('count', 0)} 檔"
                     f"（{_fmt(inbox.get('bytes', 0))}，7 日 retention）")
    desc = ("週用量：" + "；".join(parts)
            + "。Modal 金額 CLI 冇 API（1.x 冇 billing endpoint），"
              "要睇 modal.com/settings/usage")
    with _dict_lock:
        doc = load_doc()
        append_changelog(doc, "usage", desc, "usage")
        _write_doc(doc)
    _dirty.set()
    try:
        with open(USAGE_STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump({"last": datetime.datetime.now()
                      .isoformat(timespec="seconds")}, fh)
    except OSError:
        pass
    _log(f"usage summary appended: {desc}")
    return True










SHADOW_QA_STATE_PATH = os.path.join(HERE, ".shadow_qa_state")
SHADOW_QA_INTERVAL_S = 7 * 86400
SHADOW_QA_SAMPLE_N = 20
SHADOW_QA_HISTORY_ROWS = 600
SHADOW_QA_DECLINE_STREAK = 2
_QA_NTFY_TOPIC_FILE = os.path.join(HERE, ".ntfy_topic")


def _shadow_qa_ntfy_topic():
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if topic:
        return topic
    try:
        with open(_QA_NTFY_TOPIC_FILE, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _shadow_qa_notify(body):
    """fyi-level ntfy push — only fires on SHADOW_QA_DECLINE_STREAK
    consecutive weekly recall declines, so this is rare by design."""
    topic = _shadow_qa_ntfy_topic()
    if not topic:
        _log("shadow QA: ntfy skipped (no NTFY_TOPIC env / .ntfy_topic file)")
        return False
    try:
        subprocess.run(
            ["curl", "-sS", "--max-time", "10",
             "-H", "Title: dictation shadow QA", "-H", "Priority: low",
             "-H", "Tags: chart_with_downwards_trend",
             "-d", body, f"https://ntfy.sh/{topic}"],
            capture_output=True, timeout=15)
        return True
    except Exception as exc:
        _log(f"shadow QA: ntfy failed: {exc}")
        return False


def _shadow_qa_due():
    if SHADOW_JUDGE_MODEL != "local":
        return False
    try:
        with open(SHADOW_QA_STATE_PATH, encoding="utf-8") as fh:
            last = json.load(fh).get("last")
        dt = datetime.datetime.fromisoformat(last)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return True
    return (datetime.datetime.now() - dt).total_seconds() > SHADOW_QA_INTERVAL_S


def _shadow_qa_sample(rows, n=SHADOW_QA_SAMPLE_N, rng=random):
    """Rows carrying both a non-trivial raw AND final text — the same shape
    shadow_improve itself requires. Pure/injectable rng for tests."""
    candidates = [r for r in rows
                 if (r.get("raw") or "").strip()
                 and len(r.get("text") or "") >= SHADOW_MIN_CHARS]
    if len(candidates) <= n:
        return candidates
    return rng.sample(candidates, n)


def weekly_shadow_qa():
    """Sample SHADOW_QA_SAMPLE_N recent dictations, run BOTH proposers (local
    qwen and cloud Sonnet, forced via shadow_propose(..., model=...)) on each,
    and measure qwen's recall of Sonnet's dict/style candidates on identical
    input. Any dict candidate Sonnet found but qwen missed is filed straight
    into the existing `pending` queue — the SAME queue the gatekeeper's 10-min
    retry (retry_pending) already drains via judge_with_ai, so this re-review
    queue has a real consumption mechanism from day one (closing the "flag
    logged but nothing picks it up" gap noted in Local Dictation System.md).
    Recall history persists in SHADOW_QA_STATE_PATH; SHADOW_QA_DECLINE_STREAK
    consecutive weekly declines fires an ntfy nudge to consider
    SHADOW_JUDGE_MODEL=cloud. State (including recall_history) is only
    written on a run that actually completes, so a failed pass just retries
    on the next poll rather than corrupting the streak."""
    if SHADOW_JUDGE_MODEL != "local":
        return None
    rows = _load_history_rows(SHADOW_QA_HISTORY_ROWS)
    sample = _shadow_qa_sample(rows)
    if len(sample) < 5:
        _log("shadow QA: not enough history rows with raw+final text, skipping")
        return None
    sonnet_total, sonnet_caught_by_qwen, missed_dict = 0, 0, []
    judged = 0
    for row in sample:
        raw, final = row.get("raw", ""), row.get("text", "")
        sonnet_data = shadow_propose(raw, final, model="cloud")
        if sonnet_data is None:
            continue
        judged += 1
        qwen_data = shadow_propose(raw, final, model="local") or {}
        s_dict = {(d.get("wrong"), d.get("right"))
                 for d in (sonnet_data.get("dict") or []) if isinstance(d, dict)}
        q_dict = {(d.get("wrong"), d.get("right"))
                 for d in (qwen_data.get("dict") or []) if isinstance(d, dict)}
        s_style = {s.get("pattern") for s in (sonnet_data.get("style") or [])
                  if isinstance(s, dict) and s.get("pattern")}
        q_style = {s.get("pattern") for s in (qwen_data.get("style") or [])
                  if isinstance(s, dict) and s.get("pattern")}
        sonnet_total += len(s_dict) + len(s_style)
        sonnet_caught_by_qwen += len(s_dict & q_dict) + len(s_style & q_style)
        for wrong, right in s_dict - q_dict:
            if wrong and right:
                missed_dict.append((wrong, right))
    if judged == 0:
        _log("shadow QA: cloud Sonnet unavailable for the whole sample, "
             "will retry next poll (state not advanced)")
        return None
    recall = (sonnet_caught_by_qwen / sonnet_total) if sonnet_total else 1.0


    if missed_dict:
        with _dict_lock:
            doc = load_doc()
            pending = doc.setdefault("pending", {})
            terms = doc.get("terms", {})
            added = 0
            for wrong, right in missed_dict:
                if wrong in pending.get(right, []) or wrong in terms.get(right, []):
                    continue
                pending.setdefault(right, []).append(wrong)
                added += 1
            if added:
                append_changelog(
                    doc, "shadow_qa",
                    f"週度 QA：qwen 漏咗 {added} 個 Sonnet 揪出嘅 dict candidate，"
                    "已入 pending 等 gatekeeper 覆核", "shadow_qa")
                save_doc(doc)
    state = _shadow_qa_state_read()
    history = state.get("recall_history", []) + [recall]
    history = history[-8:]
    declining = (len(history) > SHADOW_QA_DECLINE_STREAK
                and all(history[-i - 1] < history[-i - 2]
                        for i in range(SHADOW_QA_DECLINE_STREAK)))
    if declining:
        _shadow_qa_notify(
            f"聽寫 shadow-learning 本地 qwen recall 連續 "
            f"{SHADOW_QA_DECLINE_STREAK} 週下跌（{history[-3:]}）—— "
            "考慮將 SHADOW_JUDGE_MODEL 設返 cloud")
    try:
        with open(SHADOW_QA_STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump({
                "last": datetime.datetime.now().isoformat(timespec="seconds"),
                "recall_history": history,
            }, fh, ensure_ascii=False)
    except OSError as exc:
        _log(f"shadow QA state save failed: {exc}")
    _log(f"shadow QA: sample={len(sample)} judged={judged} recall={recall:.0%} "
         f"missed_dict={len(missed_dict)} declining={declining}")
    return {"sample": len(sample), "judged": judged, "recall": recall,
           "missed_dict": len(missed_dict), "declining": declining}


def _shadow_qa_state_read():
    try:
        with open(SHADOW_QA_STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


_NOTE_CLAUSE_TRAIL_RE = re.compile(r"[。，、,.\s]+$")

















_NOTE_CLAUSE_SEPS = frozenset()
_NOTE_CLAUSE_BRACKETS = {"「": "」", "『": "』", "（": "）", "(": ")", "“": "”"}


def _note_clauses(note):
    """Return `note` as its single atomic clause (list of one).

    Historically split a style note into comma/dun-separated "clauses" --
    see _NOTE_CLAUSE_SEPS' 2026-08-18 retirement note for why that was
    destructive and was disabled. The bracket-tracking body is kept as-is
    (dead weight with an empty _NOTE_CLAUSE_SEPS, but every call site --
    _add_style_note, _set_style_notes, migrate_style_notes_to_rules,
    _collapse_near_duplicate_notes -- already treats this function as "the
    clause splitter" and iterates its result, so keeping the same shape
    means none of them needed to change)."""
    clauses, buf, stack = [], [], []
    for ch in note:
        closer = _NOTE_CLAUSE_BRACKETS.get(ch)
        if closer:
            stack.append(closer)
            buf.append(ch)
        elif stack and ch == stack[-1]:
            stack.pop()
            buf.append(ch)
        elif not stack and ch in _NOTE_CLAUSE_SEPS:
            piece = "".join(buf).strip()
            if piece:
                clauses.append(piece)
            buf = []
        else:
            buf.append(ch)
    piece = "".join(buf).strip()
    if piece:
        clauses.append(piece)
    return clauses


def _note_clause_key(clause):
    """Dedup key for a clause: strip whitespace + trailing terminal
    punctuation so '問句用「？」' and '問句用「？」。' collapse to the same
    key without losing either's actual wording in the output."""
    return _NOTE_CLAUSE_TRAIL_RE.sub("", "".join(clause.split()))
















def _extract_rule_fields(clause):
    """Best-effort decomposition of one clause into (governs, condition,
    action). Grounded in the two shapes actually observed in real style
    notes, not invented:
      - '...「X」' -- names a concrete target symbol X. Every observed
        punctuation-target rule marks its symbol this way. action=X,
        governs=the clause with that bracket removed.
      - '...時...' with no bracket -- a conditional ("when X, Y") shape,
        e.g. '英文詞緊接標點時唔留空格'. condition=text up to and
        including 時, action=the remainder.
    Neither shape found -> (None, None, None): this clause is PASSTHROUGH --
    its full text is still stored verbatim as the rule's `text` field (see
    _upsert_style_rule), only these three descriptive sub-fields stay empty.
    Never a reason to drop the clause itself."""
    brackets = list(_QUOTED_SYMBOL_RE.finditer(clause))
    if brackets:
        m = brackets[-1]
        action = m.group(1)
        governs = (clause[:m.start()] + clause[m.end():]).strip("，,、；; ")
        return (governs or None), None, action
    idx = clause.find("時")
    if 0 < idx < len(clause) - 1:
        condition = clause[:idx + 1].strip()
        action = clause[idx + 1:].strip("，, ")
        if action:
            return None, condition, action
    return None, None, None















_COMPLAINT_MARKER_RE = re.compile(
    "改好|請你|麻煩你|唔該你|儘快|盡快|"
    "同佢|同他|同她|跟佢|跟他|跟她|確認|確經|搞掂|處理"
)


def _is_dictated_complaint(text):
    """True if `text` reads as Tinho addressing the assistant/a person
    rather than specifying a style/format preference -- see the admission
    guard note above _COMPLAINT_MARKER_RE."""
    if _QUOTED_SYMBOL_RE.search(text):
        return False
    return bool(_COMPLAINT_MARKER_RE.search(text))


def _upsert_style_rule(style, text):
    """Insert `text` (one already-atomic clause) as a style rule, or fold it
    into an existing rule with the same identity instead of appending a new
    record. Returns:
      "added"     - new identity, a new record was created.
      "widened"   - matched an existing identity AND `text` was a more
                    complete wording of it, so the canonical text/fields
                    were updated (a real, if modest, change).
      "unchanged" - matched an existing identity (verbatim, or a shorter/
                    equal-length restatement) with nothing to update --
                    this IS the duplicate-prevention outcome: the caller
                    should treat it exactly like "already covered", not
                    like a new format note landing.
      "rejected"  - `text` reads as a dictated complaint/direct address
                    rather than a style preference (_is_dictated_complaint);
                    not stored at all.

    The canonical `text` kept for a rule is the LONGEST wording seen for
    that identity (ties broken by first-seen) -- the more complete phrasing
    of the same preference, never a semantic rewrite (which would need a
    model)."""
    rules = style.setdefault("rules", [])
    if _is_dictated_complaint(text):
        _log(f"style rule admission: rejected complaint-like text: {text!r}")
        return "rejected"
    for rule in rules:
        if _similar(text, rule["text"]):
            if len(text) > len(rule["text"]):
                rule["text"] = text
                governs, condition, action = _extract_rule_fields(text)
                rule["governs"], rule["condition"], rule["action"] = (
                    governs, condition, action)
                return "widened"
            return "unchanged"
    governs, condition, action = _extract_rule_fields(text)
    rules.append({"text": text, "governs": governs, "condition": condition,
                 "action": action})
    return "added"


def _add_style_note(style, note):
    """Public entry point every note-append call site now uses in place of
    `notes.append(note)`: splits a (possibly compound) note into its atomic
    clauses and upserts each one by identity. Returns True iff this changed
    style.rules (i.e. at least one clause was new or widened an existing
    rule's wording) -- False means the note was already fully covered,
    which callers use the same way they used to use "already in notes"."""
    changed = False
    for clause in _note_clauses(note) or [note.strip()]:
        if not clause:
            continue
        if _upsert_style_rule(style, clause) in ("added", "widened"):
            changed = True
    style["notes"] = _rules_to_notes(style.get("rules", []))
    return changed


def _rules_to_notes(rules):
    """The backward-compat VIEW: every existing reader of style.notes
    (polish.load_style, watchdog's notes-count check, editor's own notes
    snapshot) keeps working unmodified against a flat list of strings --
    this is what keeps that list in sync with the structured rules that are
    now the source of truth."""
    return [r["text"] for r in rules if isinstance(r, dict) and r.get("text")]


def _set_style_notes(style, notes_list):
    """Replace style's entire rule set from a flat list of note/clause
    strings (e.g. consolidate_notes()'s output), rebuilding style.rules
    from scratch via the same identity upsert every other write path uses
    -- so a bulk replace can never leave rules/notes out of sync with each
    other the way a direct `style["notes"] = notes_list` write used to."""
    style["rules"] = []
    for note in notes_list:
        if not isinstance(note, str) or not note.strip():
            continue
        for clause in _note_clauses(note) or [note.strip()]:
            if clause:
                _upsert_style_rule(style, clause)
    style["notes"] = _rules_to_notes(style["rules"])


def migrate_style_notes_to_rules(doc):
    """One-time (idempotent) migration: legacy free-text style.notes ->
    structured style.rules. Safe to call on every load_doc() -- a no-op
    once style.rules already exists (rules is authoritative from then on;
    notes is always regenerated FROM it, never re-migrated from a stale
    notes list that could re-diverge).

    D1: every distinct preference in the legacy notes survives. This does
    NOT truncate to any cap -- every clause that doesn't fold into an
    existing identity gets its own rule, however many that is. Returns a
    report dict for callers that want the before/after numbers (the PR
    body / tests use this; production callers ignore it)."""
    style = doc.setdefault("style", {})
    if "rules" in style:
        return None
    legacy_notes = style.get("notes") or []
    style["rules"] = []
    passthrough = 0
    for note in legacy_notes:
        if not isinstance(note, str) or not note.strip():
            continue
        for clause in _note_clauses(note) or [note.strip()]:
            if not clause:
                continue
            action = _upsert_style_rule(style, clause)
            if action == "added" and _extract_rule_fields(clause) == (None, None, None):
                passthrough += 1
    style["notes"] = _rules_to_notes(style["rules"])
    return {
        "notes_before": len(legacy_notes),
        "rules_after": len(style["rules"]),
        "passthrough": passthrough,
    }


def _collapse_near_duplicate_notes(notes):
    """Collapse near-duplicate WHOLE notes (_similar(), same threshold as the
    new-note rejection gate) into one merged note per cluster, deterministically
    and with NO model.

    D1 (must preserve every distinct preference): a cluster is never reduced
    to a single "best" member picked by length or recency -- that can silently
    drop a distinct clause a shorter/older member carried (e.g. one member
    saying '...並列用頓號「、」' and another saying '...完整意思收「。」' are
    both ~50% similar to a shared '問句...' prefix but each carries a clause
    the other lacks). Instead every member's clauses are UNIONED, deduped
    only by exact match (via _note_clause_key, punctuation/whitespace
    insensitive) -- strictly additive, so nothing is ever dropped, only
    literal repeats across near-duplicate members are removed once.

    Whole-note clustering (rather than clustering raw clause fragments) is
    deliberate: clause fragments are short enough that edit-distance/ratio
    similarity gives false positives on OPPOSITE short phrases (e.g. '中文句
    用全形標點' vs '純英文句用半形標點' differ by one word but mean opposite
    things) -- measured empirically while building this function. Clustering
    whole notes first, then only fragmenting within an already-confirmed
    near-duplicate cluster, avoids that failure mode; see
    NOTE_SIMILARITY_THRESHOLD for the threshold's own calibration.

    Proven offline against the real style.notes + changelog near-duplicate
    corpus (39 real notes, incl. the 15-member "問句" cluster this was
    written for): 39 -> 22 notes, 0 clauses lost (verified by comparing the
    clause-key set of the input against the output)."""
    n = len(notes)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for i in range(n):
        for j in range(i + 1, n):
            if _similar(notes[i], notes[j]):
                union(i, j)

    clusters = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)

    merged = []
    for idxs in clusters.values():
        members = [notes[i] for i in idxs]
        if len(members) == 1:
            merged.append(members[0])
            continue
        seen, ordered = set(), []
        for member in members:
            for clause in _note_clauses(member):
                key = _note_clause_key(clause)
                if key and key not in seen:
                    seen.add(key)
                    ordered.append(clause.rstrip("。，、,. "))
        merged.append("，".join(ordered) + "。")
    return merged


def _deterministic_consolidation(payload):
    """Lossless exact/whitespace dedupe, then near-duplicate collapse (see
    _collapse_near_duplicate_notes). ALWAYS succeeds (2026-07-26) -- style
    notes are now identity-based structured rules (see _upsert_style_rule /
    _add_style_note), so duplication can no longer accumulate by
    construction; this remains only as a safety net for whatever legacy
    free text hasn't been migrated yet or reached style.notes some other
    way. It used to return None (forcing a model-tier call) once the count
    exceeded CONSOLIDATE_MAX_NOTES -- removed: a genuinely large set of
    DISTINCT preferences cannot be shrunk further without a model actually
    understanding and re-summarising them, which is exactly what D1 (never
    silently drop a distinct preference) forbids doing automatically. Since
    duplication is structurally prevented at the point of insertion, this
    path no longer has a reason to ever fail, which is also why
    _run_consolidator (below) now tries this FIRST rather than a tier."""
    notes, seen = [], set()
    rejected = set(payload.get("rejected") or [])
    for value in payload.get("notes") or []:
        if not isinstance(value, str):
            continue
        note = " ".join(value.split()).strip()
        key = note.casefold()
        if not note or key in seen or f"format:{note}" in rejected:
            continue
        seen.add(key)
        notes.append(note)
    notes = _collapse_near_duplicate_notes(notes)
    return {"notes": notes, "bad_subs": []}


def _run_consolidator(prompt, payload):
    """Deterministic first, a tier only as an unreachable-in-practice
    safety net; never Claude/Ollama.

    REORDERED 2026-07-26 (D72: the cheapest capable tier must be chosen,
    never defaulted into — start at the cheapest tier, escalate only after
    a cheaper one has ACTUALLY failed). This used to try a paid/free tier
    FIRST and fall back to _deterministic_consolidation only on failure —
    backwards, since deterministic is strictly cheaper (free, instant, no
    external call) and, now that style notes are identity-based structured
    rules (_upsert_style_rule), _deterministic_consolidation ALWAYS
    succeeds: duplication can no longer accumulate by construction, so
    there is nothing left that only a model's semantic judgement could
    resolve. The tier-invocation branch below is kept as defense-in-depth
    (never deleted, still tested) but is, by this same fact, unreachable in
    normal operation — which is the literal sense in which this "removes
    the last reason that path ever calls out to a tier."

    Returns (result_or_None, status). status is one of:
      "ok"             - the deterministic path (or, if ever reached, a
                          tier) produced a usable
                          {"notes":[...],"bad_subs":[...]}.
      "bad_contract"   - a tier actually ran and exited 0, but its stdout was
                          not the documented JSON object.
      "dedupe_insufficient" - no runnable tier AND (unreachable today, see
                          above) the deterministic path failed too.
    """
    result = _deterministic_consolidation(payload)
    if result is not None:
        return result, "ok"
    script = os.path.join(HERE, "scripts", "free_tier_run.sh")
    command = None
    if os.path.isfile(script) and os.access(script, os.X_OK):
        command = [script, CONSOLIDATE_PROMPT]
    elif shutil.which("codex"):
        command = ["codex", "exec", "--skip-git-repo-check", prompt]
    if command:
        try:
            kwargs = {"capture_output": True, "text": True, "timeout": 60}
            if command[0] == script:
                kwargs["input"] = json.dumps(payload, ensure_ascii=False)
            proc = subprocess.run(command, **kwargs)
            if proc.returncode == 0:
                match = re.search(r"\{.*\}", proc.stdout, re.S)
                if not match:
                    _log("consolidate: tier ran (rc=0) but returned no JSON "
                         f"object — bad contract; stdout head="
                         f"{proc.stdout[:160]!r}")
                    return None, "bad_contract"
                try:
                    return json.loads(match.group()), "ok"
                except json.JSONDecodeError as exc:
                    _log(f"consolidate: tier ran (rc=0) but JSON did not "
                         f"parse — bad contract: {exc}; stdout head="
                         f"{proc.stdout[:160]!r}")
                    return None, "bad_contract"
            _log(f"consolidate cheap tier unavailable rc={proc.returncode}: "
                 f"{proc.stderr.strip()[:200]}")
        except Exception as exc:
            _log(f"consolidate cheap tier unavailable: {exc}")
    else:
        _log("consolidate: no tier configured (no scripts/free_tier_run.sh, "
             "no codex on PATH)")
    _log("consolidate: deterministic path failed AND no tier available — "
         "this should not be reachable now that style.notes is structured; "
         "see _deterministic_consolidation")
    return None, "dedupe_insufficient"


CONSOLIDATE_REJECTED_LIMIT = 30









def consolidate_notes(dry_run=False, doc_override=None):
    """Consolidate style.notes without Claude, preserving the JSON contract."""
    doc = doc_override if doc_override is not None else load_doc()
    style = doc.get("style", {}) or {}
    payload = {
        "notes": list(style.get("notes", [])),
        "changelog": [{"kind": e.get("kind"), "desc": e.get("desc")}
                      for e in (doc.get("changelog") or [])[:30]],
        "rejected": [f"{r.get('kind')}:{r.get('key')}"
                     for r in (doc.get("rejected") or [])[:CONSOLIDATE_REJECTED_LIMIT]
                     if r.get("key")],
        "subs": [s.get("pattern") for s in style.get("subs", []) if s.get("pattern")],
    }
    prompt = CONSOLIDATE_PROMPT + json.dumps(payload, ensure_ascii=False)
    data, status = _run_consolidator(prompt, payload)
    if not isinstance(data, dict) or status != "ok":
        if not dry_run:




            streak = _write_consolidate_state(status)
            if streak and streak % CONSOLIDATE_FAIL_ESCALATE_EVERY == 0:
                _log(f"consolidate: {streak} consecutive failures "
                     f"(status={status}) — escalating")
                _consolidate_escalation_notify(streak, status)
        return None






    new_notes = [n.strip() for n in data.get("notes", [])
                 if isinstance(n, str) and n.strip()]
    bad_subs = [s for s in data.get("bad_subs", []) if isinstance(s, str) and s]
    result = {"notes": new_notes, "bad_subs": bad_subs}
    if dry_run:
        return result




    if bad_subs:
        with _dict_lock:
            d = load_doc()
            due = _record_flagged_subs(d, bad_subs)
            for pat in due:
                append_changelog(
                    d, "style",
                    f"被 flag ≥{FLAGGED_SUB_REREVIEW_MIN} 次，自動重審：{pat}",
                    "consolidate")
            _write_doc(d)
            _dirty.set()
            _push_now.set()
        _log(f"consolidate: cheap tier flagged subs {bad_subs}; "
             f"due for re-review: {due}")
        for pat in due:
            rereview_rule({"kind": "style_sub", "pattern": pat})
    if not new_notes:
        _log("consolidate: returned no notes — skipped, notes unchanged")
        _write_consolidate_state()
        return result
    with _dict_lock:
        doc = load_doc()
        before = len(doc.get("style", {}).get("notes", []))
        _set_style_notes(doc.setdefault("style", {}), new_notes)
        append_changelog(doc, "consolidate",
                         f"整合 style.notes：{before} → {len(new_notes)} 條",
                         "consolidate")
        _write_doc(doc)
        _dirty.set()













    write_notes_snapshot(new_notes)




    flag_state = load_doc().get("flagged_subs")

    def _shrink(d):
        _shrink_style_rules(d, new_notes)
        if flag_state:
            d["flagged_subs"] = json.loads(json.dumps(flag_state))
        else:
            d.pop("flagged_subs", None)
        _prune_flagged_subs(d)
    ok = push_authoritative(
        _shrink, f"dictation: consolidate style.notes ({before} -> {len(new_notes)})")
    _write_consolidate_state()
    if not ok:
        _log("consolidate: authoritative push failed after retries — stays "
             "dirty; background sync may transiently resurrect old notes "
             "until it eventually succeeds")
    _log(f"consolidate applied: {before} → {len(new_notes)} notes")
    return result


def _maintenance_poll():
    """Run due housekeeping serially on the maintenance-only worker."""
    try:
        doc = load_doc()
        notes_count = len((doc.get("style") or {}).get("notes", []))
        if _consolidation_due(notes_count):
            consolidate_notes()
        _maybe_consolidate_subs_for_load(doc)
    except Exception as exc:
        _log(f"consolidation worker error: {exc}")
    try:
        if _rule_audit_due():
            weekly_rule_audit()
    except Exception as exc:
        _log(f"weekly rule audit error: {exc}")
    try:
        if _usage_summary_due():
            weekly_usage_summary()
    except Exception as exc:
        _log(f"usage summary error: {exc}")
    try:
        qlen = _shadow_queue_len()
        if _shadow_batch_due(qlen, _shadow_queue_state().get("queue_started_at")):
            flush_shadow_queue()
    except Exception as exc:
        _log(f"shadow batch flush error: {exc}")
    try:
        if _shadow_qa_due():
            weekly_shadow_qa()
    except Exception as exc:
        _log(f"shadow QA error: {exc}")
    try:
        push_review_queue_backup()
    except Exception as exc:
        _log(f"review_queue backup error: {exc}")
    try:
        prune_served_history()
    except Exception as exc:
        _log(f"history prune error: {exc}")


def consolidation_worker():
    """Backstop for the load-triggered check in save_doc() (see
    _maybe_consolidate_for_load): runs a consolidation pass on the ordinary
    >24h cadence, or sooner if style.notes is over CONSOLIDATE_LOAD_THRESHOLD
    and CONSOLIDATE_MIN_SPACING allows it. save_doc already fires the instant
    notes crosses the threshold, so in practice this poll should rarely be
    the one that trips — it exists to catch any doc mutation that somehow
    bypasses save_doc. Checks every 10 min (was hourly; too slow relative to
    how fast shadow learning can regrow notes)."""
    while True:
        submit_maintenance("maintenance-poll", _maintenance_poll, dedupe=True)
        time.sleep(600)
































SUBS_FAMILY_MERGE_MIN = 2


_SUB_LITERAL_RE = re.compile(r"^[^\\().?*+|\[\]{}^$]+$")

_SUB_FAMILY_RE = re.compile(
    r"^(?P<prefix>[^\\().?*+|\[\]{}^$]+)\(\?=(?P<la>[^()]+)\)$")
_consolidating_subs = threading.Event()


def _split_alternation(la):
    """Split a lookahead body on top-level '|', respecting [...] classes and
    backslash escapes. The family regex already guarantees no parens."""
    parts, cur, i, in_class = [], [], 0, False
    while i < len(la):
        c = la[i]
        if c == "\\" and i + 1 < len(la):
            cur.append(la[i:i + 2])
            i += 2
            continue
        if c == "[":
            in_class = True
        elif c == "]":
            in_class = False
        if c == "|" and not in_class:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    parts.append("".join(cur))
    return [p for p in parts if p]


def _parse_family_sub(sub):
    """(prefix, core_replace, branches) for a mergeable LITERAL(?=...) sub,
    else None. core_replace strips a duplicated lookahead literal off the
    replace (系(?=同)→係同 really means →係: the lookahead consumes nothing,
    so a replace ending with the looked-ahead text would paste it twice)."""
    pat, rep = sub.get("pattern"), sub.get("replace")
    if not isinstance(pat, str) or not isinstance(rep, str):
        return None
    m = _SUB_FAMILY_RE.match(pat)
    if not m:
        return None
    branches = _split_alternation(m.group("la"))
    if not branches:
        return None
    core = rep
    if len(branches) == 1:
        b = branches[0]
        if _SUB_LITERAL_RE.match(b) and len(rep) > len(b) and rep.endswith(b):
            core = rep[:-len(b)]
    if not core:
        return None
    return m.group("prefix"), core, branches


def _dedupe_branches(branches):
    """Drop exact duplicates, then drop any literal branch that has a shorter
    literal branch as its prefix — in a lookahead, (?=更) already matches
    everywhere (?=更新) does."""
    seen = []
    for b in branches:
        if b not in seen:
            seen.append(b)
    literals = {b for b in seen if _SUB_LITERAL_RE.match(b)}
    out = []
    for b in seen:
        if b in literals and any(o != b and b.startswith(o) for o in literals):
            continue
        out.append(b)
    return out


def _consolidated_subs(subs):
    """Pure function: (new_subs, merged) where merged is
    [(prefix, core, n_members)] for every family that actually collapsed.
    Original order is preserved; a merged rule sits where the family's first
    member sat. Never merges across different replacements, and keeps any
    entry it cannot prove equivalent."""
    parsed = [_parse_family_sub(s) for s in subs]
    fam_count = {}
    for p in parsed:
        if p:
            fam_count[(p[0], p[1])] = fam_count.get((p[0], p[1]), 0) + 1

    fam_branches, fam_first, out, merged = {}, {}, [], []
    for idx, (sub, p) in enumerate(zip(subs, parsed)):
        if not p or fam_count[(p[0], p[1])] < SUBS_FAMILY_MERGE_MIN:
            out.append(sub)
            continue
        key = (p[0], p[1])
        if key not in fam_branches:
            fam_branches[key] = []
            fam_first[key] = len(out)
            out.append(None)
        fam_branches[key].extend(p[2])

    for key, branches in fam_branches.items():
        prefix, core = key
        branches = _dedupe_branches(branches)
        pattern = f"{prefix}(?={'|'.join(branches)})"
        try:
            re.compile(pattern)
        except re.error:
            pattern = None
        if pattern is None or not _is_context_safe(pattern):



            out[fam_first[key]] = [s for s, p in zip(subs, parsed)
                                   if p and (p[0], p[1]) == key]
            continue
        out[fam_first[key]] = {
            "pattern": pattern, "replace": core,
            "note": f"{prefix}→{core}（整合 {fam_count[key]} 條）"}
        merged.append((prefix, core, fam_count[key]))
    flat = []
    for s in out:
        if s is None:
            continue
        flat.extend(s) if isinstance(s, list) else flat.append(s)
    return flat, merged


def consolidate_style_subs(doc):
    """Mutate doc in place, collapsing style.subs pattern families. Returns
    (before_count, after_count) when something changed, else None. Idempotent
    — safe to re-run on every push_authoritative retry."""
    style = doc.setdefault("style", {})
    subs = style.get("subs", [])
    new, merged = _consolidated_subs(subs)
    if not merged or len(new) == len(subs):
        return None
    style["subs"] = new
    return len(subs), len(new)


def _maybe_consolidate_subs_for_load(doc):
    """Called after every save_doc(): if any style.subs pattern family has
    grown to SUBS_FAMILY_MERGE_MIN members, collapse it in the background.
    Deterministic (no LLM), so no 24h/min-spacing gates are needed — after a
    merge there is nothing left to merge, so this cannot thrash; it fires
    again only when learning has grown a family back to the threshold."""
    try:
        subs = (doc.get("style") or {}).get("subs", [])
    except AttributeError:
        return
    new, merged = _consolidated_subs(subs)
    if not merged or len(new) == len(subs):
        return
    if _consolidating_subs.is_set():
        return
    _consolidating_subs.set()

    def _run():
        try:
            with _dict_lock:
                d = load_doc()
                res = consolidate_style_subs(d)
                if res:
                    before, after = res
                    append_changelog(
                        d, "consolidate",
                        f"整合 style.subs：{before} → {after} 條", "consolidate")
                    _write_doc(d)
                    _dirty.set()
            if res:
                _log(f"subs consolidation load-triggered: {before} → {after}")




                ok = push_authoritative(
                    lambda d: consolidate_style_subs(d),
                    f"dictation: consolidate style.subs ({before} -> {after})")
                if not ok:
                    _log("subs consolidation: authoritative push failed — "
                         "stays dirty; background sync may transiently "
                         "resurrect old subs until a later pass succeeds")
        except Exception as exc:
            _log(f"subs consolidation error: {exc}")
        finally:
            _consolidating_subs.clear()

    submit_maintenance("consolidate-subs", _run, dedupe=True)


def apply_correction(time_key, corrected):
    """Rewrite one history entry with the user's fix; return the old text."""
    if not os.path.exists(HISTORY_PATH):
        return None
    entries, old_text = [], None
    with open(HISTORY_PATH, encoding="utf-8") as fh:
        for line in fh:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    for e in entries:
        if e.get("time") == time_key and old_text is None:
            old_text = e.get("text") or ""
            e["text"] = corrected
            e["edited"] = True
    if old_text is None:
        return None
    with open(HISTORY_PATH, "w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    return old_text


def read_history(limit=100):
    if not os.path.exists(HISTORY_PATH):
        return []
    out = []
    with open(HISTORY_PATH, encoding="utf-8") as fh:
        for line in fh:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out[-limit:][::-1]

PAGE = """<!doctype html>
<html lang="zh-HK"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>語音輸入台</title>
<style>
:root{--bg:#fbfbfd;--fg:#1a1a1c;--card:#fff;--line:#e3e3e8;--accent:#c0392b;
--muted:#6b6b73;--chip:#f0f0f4}
@media(prefers-color-scheme:dark){:root{--bg:#141416;--fg:#ececed;--card:#1d1d20;
--line:#2e2e33;--accent:#ff6b5a;--muted:#9a9aa2;--chip:#27272c}}
*{box-sizing:border-box}
body{margin:0;padding:32px 20px 80px;background:var(--bg);color:var(--fg);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Helvetica Neue",sans-serif}
.wrap{max-width:860px;margin:0 auto}
h1{font-size:23px;margin:0 0 4px}
.sub{color:var(--muted);font-size:13px;margin-bottom:26px}
.term{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:14px 16px;margin-bottom:10px}
.term-head{display:flex;gap:10px;align-items:center;margin-bottom:10px}
.term-head input{flex:1;font-size:16px;font-weight:600;padding:7px 10px;
border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg)}
.arrow{color:var(--muted);font-size:12px;margin-bottom:7px}
.chips{display:flex;flex-wrap:wrap;gap:7px}
.chip{display:flex;align-items:center;gap:5px;background:var(--chip);
border-radius:7px;padding:3px 5px 3px 9px}
.chip input{border:0;background:transparent;color:var(--fg);font-size:13px;
font-family:ui-monospace,SFMono-Regular,Menlo,monospace;width:auto;min-width:60px;
field-sizing:content;outline:none}
button{cursor:pointer;font:inherit;border-radius:7px;border:1px solid var(--line);
background:var(--card);color:var(--fg);padding:5px 11px}
button:hover{border-color:var(--accent);color:var(--accent)}
.x{border:0;background:transparent;color:var(--muted);padding:0 4px;font-size:15px}
.x:hover{color:var(--accent)}
.add-chip{border-style:dashed;font-size:13px;padding:3px 10px}
.bar{position:fixed;left:0;right:0;bottom:0;background:var(--card);
border-top:1px solid var(--line);padding:12px 20px;display:flex;gap:10px;
align-items:center;justify-content:center}
.bar[hidden]{display:none}  /* display:flex above would otherwise beat [hidden] */
.primary{background:var(--accent);color:#fff;border-color:var(--accent);font-weight:600;
padding:8px 20px}
.primary:hover{color:#fff;opacity:.9}
#status{color:var(--muted);font-size:13px}
.test{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:14px 16px;margin:24px 0 10px}
.test input{width:100%;padding:9px 11px;border:1px solid var(--line);border-radius:8px;
background:var(--bg);color:var(--fg);font-size:14px;margin-bottom:9px}
.out{font-size:14px;min-height:22px}
.out b{color:var(--accent)}
.tabs{display:flex;gap:8px;margin-bottom:18px}
.tab{padding:7px 15px}
.tab.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.hrow{cursor:pointer}
.hrow:hover{border-color:var(--accent)}
</style></head><body><div class="wrap">
<h1>語音輸入台</h1>
<div class="sub">撳住右⌘ 講嘢 · 輕撳一下 = 長段落模式 · <span id="stat">檢查緊…</span> <span id="gstat"></span></div>

<div class="tabs">
  <button class="tab on" onclick="go('hist',this)">🕘 最近轉寫</button>
  <button class="tab" onclick="go('dict',this)">📖 字典</button>
  <button class="tab" onclick="go('key',this)">⌨️ 快捷鍵</button>
  <button class="tab" onclick="go('set',this)">⚙️ 設定</button>
</div>

<div id="key" class="pane" hidden>
  <div class="term">
    <div class="arrow">而家綁咗（全部同時生效）</div>
    <div id="curkey" style="margin:8px 0 14px"></div>
    <button class="primary" onclick="capture()">＋ 加多一個掣</button>
    <div style="font-size:13px;color:var(--muted);margin-top:10px">
      撳咗之後，喺 4 秒內撳你想加嘅掣：修飾鍵（⌘ ⌥ ⌃ ⇧，左右分開）或者滑鼠側掣。<br>
      MX Master 3S 嘅拇指掣、手勢掣、前後掣都收得到 — <b>唔使經 Logi Options+</b>。
    </div>
  </div>
  <div class="term">
    <div class="arrow">點用</div>
    <div style="font-size:14px;line-height:1.9">
      <b>撳住</b> → 講嘢 → <b>放手</b> → 出字（短訊息用呢個）<br>
      <b>輕撳一下</b> → 開始錄 → 講幾耐都得 → <b>再撳一下</b> → 出字（長段落用呢個）<br>
      錄緊音嗰陣螢幕底會有紅點提示。
    </div>
  </div>
  <div class="term">
    <div class="arrow">你嘅使用情況</div>
    <div id="stats" style="font-size:14px">…</div>
  </div>
</div>

<div id="hist" class="pane">
  <div class="sub">撳任何一條就複製，跟住 ⌘V 貼去邊都得。</div>
  <div id="hlist"></div>
</div>

<div id="set" class="pane" hidden>
  <div class="term"><div class="arrow">辨識語言</div>
    <div style="font-size:14px">而家識：廣東話＋英文（自動辨識）</div>
    <div style="font-size:13px;color:var(--muted);margin-top:6px">
    冇手動掣㗎喇 — SenseVoice 淨係用 auto 模式聽（廣東話夾英文碼轉碼），
    唔使揀語言。想加多種語言／改行為，直接同 AI 講就得，唔使喺呢度撳掣。</div></div>
  <div class="term"><div class="arrow">潤色</div>
    <div style="font-size:14px">恆常開，混合式 — 前台（撳停嗰刻等緊嘅嗰段、短句、長段落分類）用 qwen2.5:3b 求快；
    背景（你仲講緊嘢嗰陣做嘅長段落全文重寫）用 qwen2.5:7b 求靚，唔使等</div>
    <div style="font-size:13px;color:var(--muted);margin-top:6px">
    冇開關掣 — 就算潤色本身用嘅 LLM 行唔到（例如 ollama 未起、逾時），
    標點模型都仲會保底出標點，絕對唔會出一嚿冇標點嘅文字。</div></div>
  <div class="term"><div class="arrow">說明 — 條 pipeline 點行</div>
    <div style="font-size:13px;color:var(--muted)">
    <b style="color:var(--fg)">本機（Mac 上面行，唔使網絡）：</b>
    <ol style="margin:6px 0 14px;padding-left:20px;line-height:1.9">
      <li><b>SenseVoice ASR</b>（sherpa-onnx，auto 語言）— 錄音轉做文字，~0.1–0.7 秒</li>
      <li><b>OpenCC s2hk</b> — 簡轉繁（香港字形），毫秒級</li>
      <li><b>字典／語氣規則</b>（dictionary.json 嘅 terms + style.subs）— 決定性字詞取代，毫秒級</li>
      <li><b>CT-Transformer 標點</b> — 加標點，3–6 毫秒，呢步保證一定有標點，就算 LLM 冧咗都唔影響</li>
      <li><b>qwen 潤色</b>（ollama，混合式）— 前台（撳停嗰刻、短句全文重寫、長段落分類：問號／分段／自我修正）用 qwen2.5:3b 求快，~2 秒內；背景滾動長段落全文重寫用 qwen2.5:7b 求靚，~5–15 秒；錄緊音嗰陣背景已經開始做，撳停淨係補尾段，所以出字好快</li>
      <li><b>貼上</b>（Hammerspoon）— 出字之後即刻貼去你嗰個輸入框</li>
    </ol>
    <b style="color:var(--fg)">雲端（背景做，唔阻住出字，離線都唔影響貼字）：</b>
    <ol style="margin:6px 0 0;padding-left:20px;line-height:1.9">
      <li><b>GitHub 字典同步</b> — 開機同每 15 分鐘 pull 一次，有改動即刻 push，離線就等下次</li>
      <li><b>本機 qwen review-queue 守門員</b>（review_gate.py，本機 ollama，唔使網絡）— 你手動改字／貼字後自己改嘅位，全部先入 review queue（唔會即刻入字典），背景用本機 qwen 判斷係「聽錯」定係「改咗意思／改變主意」；淨係見夠 3 次（DICT_PROMOTE_N 可以調）獨立判斷做「聽錯」，先會真正入字典 —— 一次性嘅改寫、改主意，永遠唔會變成規則</li>
      <li><b>Sonnet 守門員判斷</b>（<code>claude -p --model sonnet</code>）— 淨係處理拼字候選（讀出字母串個字）、排版偏好、shadow 分析提出嘅候選，通常幾秒到 90 秒（timeout 上限），離線就排隊等 10 分鐘後重試</li>
      <li><b>Shadow-Sonnet 分析</b> — 每次貼字 ≥20 字之後背景行一次，順手校對出「☁️ 修正版」＋提出新規則候選，唔阻住貼字，通常 1–2 分鐘內完成</li>
      <li><b>每日背景整合</b>（free tier／Codex／deterministic fallback；絕不使用 Claude）— 相隔 &gt;24 小時先行一次，將 style.notes 濃縮返 ≤12 條，避免規則愈積愈多拖慢潤色</li>
      <li><b>Modal 電話 pipeline</b> — iPhone 聽寫走 SenseVoice＋標點＋字典（冇 LLM，靠決定性規則），~6 秒（warm）</li>
    </ol>
    </div>
    <div style="font-size:13px;color:var(--muted);margin-top:14px">
    每次錄音都會保留原聲（最近 10 次）— 最近轉寫度可以 ▶ 聽返或者 🔄 重跑。</div></div>
</div>

<div id="dict" class="pane" hidden>
<div class="sub">字典自己會學。呢度淨係展示改動，同埋可以 ✕ 刪除或者 🔁 重新 review 每一條規則。</div>
<div id="pendbox"></div>
<div id="rulesbox"></div>
<div class="term">
  <div class="arrow">🕘 最近改動</div>
  <div id="changelog"></div>
</div>
</div>
</div>
<script>
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
// The dictionary now curates itself: the 字典 tab is read-only — it shows what
// changed and lets Tinho ✕ delete or 🔁 re-review any live rule.
let _rules=[];
const KIND={dict:'字詞',style:'語氣',format:'格式',removed:'刪除',consolidate:'整合'};
const SRC={auto:'自動',retry:'重試',rereview:'重審',manual:'手動',shadow:'雲校',spellout:'串字',consolidate:'整合'};
function loadRules(){
  fetch('/api/rules').then(r=>r.json()).then(d=>{
    _rules=[];
    let h='<div class="arrow">生效緊嘅規則 — ✕ 刪除／🔁 重新 review</div>';
    Object.keys(d.terms||{}).forEach(c=>(d.terms[c]||[]).forEach(v=>{
      h+=ruleRow({kind:'term',term:c,variant:v},`${esc(v)} → <b>${esc(c)}</b>`,'字詞');
    }));
    ((d.style||{}).subs||[]).forEach(s=>{
      h+=ruleRow({kind:'style_sub',pattern:s.pattern},
                 `${esc(s.pattern)} → <b>${esc(s.replace)}</b>`,'語氣');
    });
    ((d.style||{}).notes||[]).forEach(n=>{
      h+=ruleRow({kind:'style_note',note:n},esc(n),'格式');
    });
    if(!_rules.length)h+='<div style="color:var(--muted)">仲未有生效嘅規則</div>';
    document.getElementById('rulesbox').innerHTML='<div class="term">'+h+'</div>';
  });
}
function ruleRow(id,label,tag){
  const i=_rules.push(id)-1;
  return `<div style="display:flex;align-items:center;gap:8px;margin:6px 0">
    <span style="font-size:11px;color:var(--muted);width:34px;flex:none">${tag}</span>
    <span style="flex:1;word-break:break-all">${label}</span>
    <button class="x" onclick="ruleDel(${i})" title="刪除">✕</button>
    <button class="x" onclick="rereview(${i})" title="重新 review">🔁</button></div>`;
}
async function ruleDel(i){
  await fetch('/api/rule_delete',{method:'POST',body:JSON.stringify(_rules[i])});
  loadRules();loadChangelog();
}
async function rereview(i){
  await fetch('/api/rereview',{method:'POST',body:JSON.stringify(_rules[i])});
  const s=document.getElementById('stat');s.textContent='🔁 重新 review 緊（背景 Sonnet）…';
  setTimeout(()=>{loadRules();loadChangelog();status()},6000);
}
function loadChangelog(){
  fetch('/api/changelog').then(r=>r.json()).then(cl=>{
    const box=document.getElementById('changelog');
    if(!cl.length){box.innerHTML='<span style="color:var(--muted)">仲未有改動</span>';return}
    box.innerHTML=cl.map(e=>`<div style="margin:4px 0;font-size:13px">
      <span style="color:var(--muted)">${esc((e.time||'').replace('T',' ').slice(5,16))}</span>
      · ${esc(KIND[e.kind]||e.kind)} · ${esc(e.desc||'')}
      <span style="color:var(--muted);font-size:11px">${esc(SRC[e.source]||e.source||'')}</span></div>`).join('');
  });
}
function go(id,btn){
  ['hist','dict','key','set'].forEach(p=>document.getElementById(p).hidden=(p!==id));
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('on'));
  btn.classList.add('on');
  if(id==='hist')loadHist();
  if(id==='key')loadKey();
  if(id==='dict'){loadPending();loadRules();loadChangelog()}
}
async function loadKey(){
  const ks=await (await fetch('/api/hotkey')).json();
  document.getElementById('curkey').innerHTML=ks.map((k,i)=>
    `<span class="chip" style="font-size:16px;padding:7px 7px 7px 14px;margin:0 7px 7px 0">
       ${esc(k.label)}
       ${ks.length>1?`<button class="x" onclick="delKey(${i})" title="移除">✕</button>`:''}
     </span>`).join('');
  const s=await (await fetch('/api/stats')).json();
  const max=Math.max(1,...s.days.map(d=>d[1]));
  document.getElementById('stats').innerHTML=
    `總共 <b>${s.total}</b> 次轉寫 · 平均每次 <b>${s.avg}</b> 字 · 最長 <b>${s.longest}</b> 字<br>
     <div style="margin-top:12px">`+
    s.days.map(([d,n])=>`<div style="display:flex;align-items:center;gap:9px;margin:3px 0">
      <span style="color:var(--muted);font-size:12px;width:46px">${d.slice(5)}</span>
      <span style="background:var(--accent);height:11px;border-radius:3px;
        width:${Math.round(n/max*260)}px;min-width:3px"></span>
      <span style="font-size:12px;color:var(--muted)">${n}</span></div>`).join('')
    +'</div>';
}
async function delKey(i){
  await fetch('/api/hotkey/remove',{method:'POST',body:JSON.stringify({index:i})});
  setTimeout(loadKey,400);
}
async function capture(){
  const before=(await (await fetch('/api/hotkey')).json()).length;
  await fetch('/api/capture',{method:'POST',body:'{}'});
  document.getElementById('curkey').innerHTML=
    '<span style="font-size:16px">撳你想加嘅掣…</span>';
  // Hammerspoon writes the new binding to disk; poll until the list grows
  let tries=0;
  const iv=setInterval(async()=>{
    const ks=await (await fetch('/api/hotkey')).json();
    if(ks.length>before||++tries>10){clearInterval(iv);loadKey()}
  },700);
}
async function loadHist(){
  const r=await fetch('/api/history');const rows=await r.json();
  const box=document.getElementById('hlist');
  if(!rows.length){box.innerHTML='<div class="sub">仲未有轉寫記錄。撳住右⌘ 講句嘢試下。</div>';return}
  box.innerHTML=rows.map((e,i)=>`<div class="term hrow" id="h${i}" onclick="copy(${i})">
      <div class="arrow">${esc((e.time||'').replace('T',' '))}${e.edited?' · ✏️已修正':''}
        <span style="float:right">
          ${e.audio?`<button onclick="event.stopPropagation();playRow(${i})" title="聽返原錄音">▶</button>
          <button onclick="event.stopPropagation();retryRow(${i})" title="用原錄音由頭重跑一次（ASR＋潤色）">🔄 重跑</button>`:''}
          <button onclick="event.stopPropagation();editRow(${i})" title="改正錯字 — 系統會由你嘅修改學新詞">✏️ 改正</button>
        </span></div>
      <div class="htext" style="white-space:pre-wrap">${esc(e.text)}</div>
      ${e.raw?`<div style="font-size:12px;color:var(--muted);margin-top:6px">原始轉寫：${esc(e.raw)}</div>`:''}
      ${e.shadow?`<div style="font-size:13px;color:var(--muted);margin-top:6px;white-space:pre-wrap">☁️ 修正版：${esc(e.shadow)}</div>`:''}
      </div>`).join('');
  window._hist=rows;
}
function editRow(i){
  const row=document.getElementById('h'+i);
  row.onclick=null;
  row.querySelector('.htext').innerHTML=
    `<textarea style="width:100%;min-height:70px;font:inherit;background:var(--bg);
      color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:8px"
      onclick="event.stopPropagation()">${esc(window._hist[i].text)}</textarea>
    <div style="margin-top:6px">
      <button class="primary" onclick="event.stopPropagation();saveEdit(${i})">儲存＋學習</button>
      <button onclick="event.stopPropagation();loadHist()">取消</button></div>`;
}
async function saveEdit(i){
  const ta=document.querySelector('#h'+i+' textarea');
  const r=await fetch('/api/correct',{method:'POST',
    body:JSON.stringify({time:window._hist[i].time,text:ta.value})});
  const j=await r.json();
  const s=document.getElementById('stat');
  s.textContent=(j.added&&j.added.length)
    ?`✓ 已儲存，${j.added.length} 個候選已入 review queue — 見夠幾次先會自動入字典，唔使批准`
    :'✓ 已儲存（今次冇新候選）';
  loadHist();setTimeout(status,8000);
}
async function loadPending(){
  const p=await (await fetch('/api/pending')).json();
  const rows=[];
  Object.keys(p).forEach(c=>p[c].forEach(v=>rows.push([v,c])));
  window._pend=rows;
  const box=document.getElementById('pendbox');
  if(!rows.length){box.innerHTML='';return}
  box.innerHTML='<div class="term" style="border-color:var(--accent)">'+
    '<div class="arrow">🎓 由你嘅修改學到嘅候選 — Sonnet 會自動審核，唔使理</div>'+
    '<div style="font-size:12px;color:var(--muted);margin-bottom:8px">'+
    '每 10 分鐘自動送去 Sonnet 判 dict／style／format／拒絕，通過就會出現喺低下面「最近改動」。'+
    '如果你已經知道呢個唔啱，唔使等，即刻 ✕ 拒絕。</div>'+
    rows.map(([v,c],i)=>`<div style="margin:7px 0">
      「${esc(v)}」 → <b>「${esc(c)}」</b>
      <button onclick="pend(${i},'reject')">✕ 唔要（即刻拒絕，唔使等審核）</button></div>`).join('')+'</div>';
}
async function pend(i,action){
  const [v,c]=window._pend[i];
  await fetch('/api/pending',{method:'POST',
    body:JSON.stringify({action:action,correct:c,variant:v})});
  loadPending();loadRules();loadChangelog();
}
function playRow(i){
  if(window._player){window._player.pause();window._player=null}
  window._player=new Audio('/api/audio/'+encodeURIComponent(window._hist[i].audio));
  window._player.play();
}
async function retryRow(i){
  const s=document.getElementById('stat');s.textContent='重跑緊…（長段落可能要 10-30 秒）';
  try{
    const r=await fetch('/api/retry',{method:'POST',
      body:JSON.stringify({audio:window._hist[i].audio})});
    const j=await r.json();
    if(j.text){
      try{await navigator.clipboard.writeText(j.text)}catch(e){}
      s.textContent='✓ 重跑完成，新結果喺最頂（已複製）';
    }else s.textContent='✗ 重跑失敗';
  }catch(e){s.textContent='✗ 重跑失敗'}
  loadHist();setTimeout(status,5000);
}
async function copy(i){
  await navigator.clipboard.writeText(window._hist[i].text);
  const s=document.getElementById('stat');s.textContent='✓ 已複製，⌘V 貼上';
  setTimeout(status,2000);
}
async function status(){
  const r=await fetch('/api/status');const j=await r.json();
  document.getElementById('stat').textContent=j.up?'引擎運行中':'⚠️ 引擎未啟動';
  const g=document.getElementById('gstat');
  const n=j.judge_consecutive_failures||0;
  g.textContent=n>0?`⚠️ 字典守門員連續失敗 ${n} 次（上次成功：${j.judge_last_success||'從未'}）`:'';
}
loadHist();status();setInterval(status,15000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/":
            self._send(200, PAGE, "text/html")
        elif self.path == "/api/dict":
            with open(DICT_PATH, encoding="utf-8") as fh:
                self._send(200, json.dumps(json.load(fh)["terms"], ensure_ascii=False))
        elif self.path == "/api/history":
            self._send(200, json.dumps(read_history(), ensure_ascii=False))
        elif self.path == "/api/status":
            judge_state = _judge_state_read()
            self._send(200, json.dumps({
                "up": ask_server("PING") == "pong",
                "judge_consecutive_failures": judge_state.get(
                    "consecutive_failures", 0),
                "judge_last_success": judge_state.get("last_success"),
            }))
        elif self.path == "/api/hotkey":
            self._send(200, json.dumps(read_hotkey(), ensure_ascii=False))
        elif self.path == "/api/stats":
            self._send(200, json.dumps(usage_stats(), ensure_ascii=False))
        elif self.path == "/api/pending":
            self._send(200, json.dumps(load_doc().get("pending", {}),
                                       ensure_ascii=False))
        elif self.path == "/api/rules":
            doc = load_doc()
            self._send(200, json.dumps({
                "terms": doc.get("terms", {}),
                "style": doc.get("style", {"subs": [], "notes": []}),
                "pending": doc.get("pending", {}),
            }, ensure_ascii=False))
        elif self.path == "/api/changelog":
            self._send(200, json.dumps(load_doc().get("changelog", []),
                                       ensure_ascii=False))
        elif self.path.startswith("/api/audio/"):
            from urllib.parse import unquote
            name = os.path.basename(unquote(self.path[len("/api/audio/"):]))
            path = os.path.join(HERE, "recordings", name)
            if name.endswith(".wav") and os.path.exists(path):
                with open(path, "rb") as fh:
                    data = fh.read()
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self._send(404, "{}")
        else:
            self._send(404, "{}")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if self.path == "/api/dict":
            with _dict_lock:
                doc = load_doc()
                doc["terms"] = payload
                save_doc(doc)



            snap = json.loads(json.dumps(payload))

            def _terms_shrink(d, snap=snap):
                d["terms"] = json.loads(json.dumps(snap))
            submit_maintenance(
                "push-terms-replace", push_authoritative, _terms_shrink,
                "dictation: replace terms")
            self._send(200, '{"ok":true}')
        elif self.path == "/api/capture":

            rc = os.system("/opt/homebrew/bin/hs -c 'dictation.capture()' "
                           ">/dev/null 2>&1")
            self._send(200, json.dumps({"ok": rc == 0}))
        elif self.path == "/api/hotkey/remove":
            idx = int(payload["index"]) + 1
            rc = os.system(f"/opt/homebrew/bin/hs -c 'dictation.removeTrigger({idx})'"
                           " >/dev/null 2>&1")
            self._send(200, json.dumps({"ok": rc == 0}))
        elif self.path == "/api/setting":
            reply = ask_server(payload["cmd"])
            self._send(200, json.dumps({"reply": reply}, ensure_ascii=False))
        elif self.path == "/api/retry":
            name = os.path.basename(payload.get("audio", ""))
            reply = ask_server("RETRY " + name, timeout=120)
            self._send(200, json.dumps({"text": reply or ""}, ensure_ascii=False))
        elif self.path == "/api/learn":


            pasted, edited = payload.get("pasted") or "", payload.get("edited") or ""
            result = learn_from_edit(pasted, edited)


            submit_maintenance("format-capture", capture_format_edit,
                               pasted, edited)
            self._send(200, json.dumps(result, ensure_ascii=False))
        elif self.path == "/api/rule_delete":
            with _dict_lock:
                doc = load_doc()
                delete_rule(doc, payload, "manual")
                save_doc(doc)



            submit_maintenance(
                "push-rule-delete", push_authoritative, _rule_shrink(payload),
                "dictation: delete rule")
            self._send(200, '{"ok":true}')
        elif self.path == "/api/rereview":
            rereview_rule(payload)
            self._send(200, '{"ok":true}')
        elif self.path == "/api/correct":
            old_text = apply_correction(payload.get("time"),
                                        (payload.get("text") or "").strip())
            added = learn_corrections(old_text, payload.get("text") or "") \
                if old_text else []
            self._send(200, json.dumps({"ok": old_text is not None,
                                        "added": added}, ensure_ascii=False))
        elif self.path == "/api/pending":
            c, v = payload.get("correct", ""), payload.get("variant", "")
            action = payload.get("action")
            ok, reason = True, ""
            with _dict_lock:
                doc = load_doc()
                pending = doc.setdefault("pending", {})















                if action == "approve":
                    ok, reason = False, ("approve 停用 — 呢個候選會由 Sonnet 自動審核"
                                          "（通常 10 分鐘內），唔使手動收入字典；"
                                          "如果肯定唔啱可以 ✕ 即刻拒絕")
                elif action == "reject" and v in pending.get(c, []):
                    pending[c].remove(v)
                    if not pending[c]:
                        del pending[c]

                    add_rejected(doc, "dict", _rej_key("dict", v, c))
                    save_doc(doc)



                    submit_maintenance(
                        "push-pending-reject", push_authoritative,
                        _pending_shrink([[v, c]]),
                        "dictation: reject pending candidate")
            self._send(200, json.dumps({"ok": ok, "reason": reason},
                                       ensure_ascii=False))
        elif self.path == "/api/test":
            text = payload["text"]
            out = polish_mod.apply_dictionary(text, payload["terms"])

            html = out
            for correct in payload["terms"]:
                if correct in out and correct not in text:
                    html = html.replace(correct, f"<b>{correct}</b>")
            self._send(200, json.dumps({"changed": out != text, "html": html},
                                       ensure_ascii=False))
        else:
            self._send(404, "{}")


class ReusableHTTPServer(HTTPServer):

    allow_reuse_address = True
    daemon_threads = True


def serve():
    ReusableHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


def start_background():
    try:
        _ensure_notes_snapshot()
    except Exception as exc:
        _log(f"notes snapshot seed failed: {exc}")
    threading.Thread(target=serve, daemon=True).start()


    threading.Thread(target=sync_worker, daemon=True).start()
    threading.Thread(target=pending_retry_worker, daemon=True).start()


    threading.Thread(target=review_queue_worker, daemon=True).start()

    threading.Thread(target=consolidation_worker, daemon=True).start()


if __name__ == "__main__":
    print(f"字典編輯器: http://127.0.0.1:{PORT}")
    webbrowser.open(f"http://127.0.0.1:{PORT}")
    serve()

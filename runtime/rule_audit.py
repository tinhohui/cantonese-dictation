#!/usr/bin/env python3
"""Evidence-based QA heuristics for the dictation dictionary.

Born from the in→PWA zombie (2026-07-22): a learned rule whose "wrong" key is
a bare common word rewrites every legitimate use of that word. This module is
the shared brain for three consumers in editor.py:

  1. the ADMISSION GATE — apply_judgement / apply_shadow_candidates call
     is_common_wrong() before letting a dict candidate land: a common
     English/Cantonese "wrong" key with no context guard is auto-rejected
     (recorded in rejected memory, so it can never be re-proposed either);
  2. the WEEKLY SELF-AUDIT — audit_doc() re-scores every live terms variant
     and style sub against the history log and classifies KEEP / DELETE /
     QUARANTINE / SUSPECT (SUSPECT = uncertain, dispatched to a Sonnet
     re-review with the evidence attached);
  3. the one-off full audit of 2026-07-22 (scripted), which used the same
     scoring plus a human ruling on the contested rules.

Pure functions only — no editor import, no I/O beyond what callers pass in —
so editor.py can import this without cycles and the tests can run it cold.
"""

import re






COMMON_EN = frozenset("""
a about above add after again against ago all almost also always am an and any
anyone anything are around as ask at away back bad bass be because been before
behind being below beside besides best better between big bit both bring but
buy by call came can cannot cant case change check clean clear close cloth
cloud codec codecs come cool copy could cut day days did do does doing done
down download
each easy edit else end enough even ever every everything fast few file find
fine first fix for found free from full get give go goes going good got great
had has have having he hear heard help her here him his hold home hour hours
how however i if im in info inside into is it its just keep kept kind know
last later leather left less let life like lingo list listen little live long
look lot made make many may maybe me mean might mind mine more most move much
must my name near need never new next nice night no not note nothing now of
off often ok okay old on once one only open or other our out over owe own part
people phone place plan play please point poaching post put question quite
read ready real really right room run said same save saw say section see seem
seen send sent session set she should show side since small so some something
soon sound space speak stand start still stop storage store such super sure
take talk tell text than thank that the their them then there these they thing
things think this those though thought three through time times to today told
too took top try turn two under until up upon us use used using very view
voice wait want was watch way we week well went were what when where which
while who why will with within without word words work would write wrong yes
yesterday yet you your
""".split())



COMMON_YUE = frozenset("""
教育 交易 學習 學校 語言 語音 文字 文件 檔案 系統 問題 時間 時候 工作 功能
服務 資料 資訊 信息 訊息 儲存 空間 位置 地方 方式 方法 內容 電腦 電話 電源
網上 網絡 標點 符號 句子 段落 格式 規則 字典 錄音 聽寫 用家 用戶 有關 關於
然後 如果 但係 因為 所以 可以 唔係 係咪 點樣 咁樣 而家 已經 仲有 另外 例如
即係 真係 好似 覺得 認為 希望 需要 應該 一定 直接 自動 手動 開始 完成 繼續
刪除 清理 改善 更新 設定 安裝 使用 幫手 幫助 意思 意見 決定 選擇 重要 簡單
複雜 容易 困難 快啲 慢啲 之前 之後 依家 今日 聽日 尋日 早上 下晝 夜晚 每日
每次 全部 部分 其他 呢個 嗰個 呢啲 嗰啲 大家 自己 我哋 你哋 佢哋 乜嘢 咩嘢
點解 幾多 幾時 邊個 邊度 返工 收工 開會 傾偈 講嘢 做嘢 睇下 諗下 試下 等下
高校
""".split())




FUNC_HAN = set(
    "嘅咁噉又話有兩個係唔我你佢哋啲咗喺同就都要好冇乜嘢點樣呢嗰而家再先至得"
    "過咩啦喇囉㗎架吖啊呀咧添埋晒曬俾畀將被之的了是不在人上下中出入去來返翻"
    "做講睇聽寫一二三四五六七八九十百千萬幾多少大小新舊快慢高低長短前後左右"
    "內外面度處完成開關落起身心手口日月年時分等到向與及或但因所如果若無沒能"
    "會可用家們他她它個那這也還把讓給對想知道去加")



_PARTICLES = set("咧呢啦喇囉㗎架吖啊呀嘅噃喎啩嘛咩添嘞囖咯")

_LATIN_TOKEN = re.compile(r"[A-Za-z']+")
_HAN = re.compile(r"[一-鿿]")
_GUARD = re.compile(r"\(\?<?[=!]")


def has_guard(pattern):
    """True if a stored variant/pattern carries a lookaround context guard."""
    return bool(_GUARD.search(pattern or ""))


def _latin_tokens(s):
    return _LATIN_TOKEN.findall(s or "")


def is_common_english(s):
    """True when s is one common English word, or a phrase made ENTIRELY of
    common words ("owe him", "right there", "to the list") — either way a
    global replace keyed on it will eventually hit legitimate speech."""
    toks = _latin_tokens(s)
    if not toks:
        return False

    stripped = re.sub(r"[\s']", "", s)
    if not re.fullmatch(r"[A-Za-z]+", stripped):
        return False
    return all(t.lower() in COMMON_EN for t in toks)


def is_common_cantonese(s):
    """True when s is a common Cantonese/Chinese word, a common word wearing a
    sentence particle (用家咧), a common word plus one stray syllable (有關奧),
    or a generic function-character fragment (又話有兩個嘅)."""
    han = "".join(_HAN.findall(s or ""))
    if not han or han != re.sub(r"\s", "", s or ""):
        return False
    if han in COMMON_YUE:
        return True
    core = han
    while core and core[-1] in _PARTICLES:
        core = core[:-1]
    while core and core[0] in _PARTICLES:
        core = core[1:]
    if core != han and core in COMMON_YUE:
        return True

    if 2 < len(han) <= 3 and (han[:2] in COMMON_YUE or han[-2:] in COMMON_YUE):
        return True

    if len(han) >= 2 and all(c in FUNC_HAN for c in han):
        return True
    return False





















_LATIN_LETTER_ONLY = re.compile(r"^[A-Za-z]$")


def is_bare_single_token(wrong):
    """A one-character key — a single Latin letter or a single Han character —
    is never safe as a global replace. It has no word boundary worth the name
    in Han text, and in Latin text it is almost always a fragment of a
    spell-out rather than a word the speaker meant."""
    w = unescape((wrong or "").strip())
    if len(w) != 1:
        return False
    return bool(_LATIN_LETTER_ONLY.match(w) or _HAN.match(w))


def existing_canonical(terms, right):
    """apply_dictionary() substitutes with re.IGNORECASE, once per term, in
    dict order — so two canonicals differing only in case SILENTLY fight, and
    whichever sorts later in the dict wins. Measured on the live dictionary:
    Supabase/SupaBase, Codex/codex, ntfy/NTFY, Claude/claude all coexisted, and
    "Supabase" was being rewritten back into the wrong "SupaBase" on every
    take because that key happened to come second.

    Returns the canonical already present that case-matches `right`, so a new
    variant merges into it instead of creating a rival key. Returns `right`
    unchanged when there is no collision."""
    if right in terms:
        return right
    lowered = (right or "").lower()
    for existing in terms:
        if existing.lower() == lowered:
            return existing
    return right


_SYSTEM_WORDS_PATH = "/usr/share/dict/words"
_system_words = None


def _load_system_words():
    """The OS word list, not a list this repo maintains. Using it keeps the
    guard logic-driven: "the ASR misheard a word" almost always produces a
    NON-word ("clode", "supper base"), so a candidate whose `wrong` side is a
    REAL English word is far more likely to be speech the rewrite will corrupt
    than a mishearing it will fix."""
    global _system_words
    if _system_words is None:
        try:
            with open(_SYSTEM_WORDS_PATH, encoding="utf-8", errors="ignore") as fh:
                _system_words = {w.strip().lower() for w in fh if w.strip()}
        except OSError:
            _system_words = set()
    return _system_words


def is_real_english_word(wrong, min_len=3):
    """True when `wrong` is a single bare English word. Bare is the operative
    part: a context-bounded variant like `cloud(?= (code|ai|model))` carries
    its own guard and is deliberately NOT caught here — that is exactly how a
    real word is allowed to be a rule when it has been made specific enough.

    KNOWN LIMIT: /usr/share/dict/words is Webster's 2nd (1934), so it has
    "plot" but not "download" — the live dictionary had BOTH promoted, and
    this catches only the first. The complete fix is evidence-based:
    history_evidence()'s `kept` count already measures whether the word has
    survived un-corrected in Tinho's own accepted output, which is the
    logic-driven signal that needs no list at all. Wiring that into the
    admission path (it is currently only consulted at re-review) is the
    follow-up this comment exists to point at.

    min_len defaults to 3 because a 1-2 letter dictionary key is already
    handled by is_bare_single_token and is too short to judge here.
    polish.fix_comma_before_new_sentence lowers it to 2, where the two-
    letter words that matter are exactly the sentence openers it looks
    for -- Do, If, So, We, It."""
    w = (wrong or "").strip()
    if len(w) < min_len or not re.fullmatch(r"[A-Za-z]+", w):
        return False
    return w.lower() in _load_system_words()


def is_unsafe_wrong(wrong, right=None):
    """The full admission predicate: the word-list gate OR the structural
    ones. Call this, not is_common_wrong(), at every dictionary insert site."""
    if right is not None:
        if is_noop_variant(wrong, right):
            return True
        if (wrong or '').lower() == (right or '').lower():





            return is_casing_only_common_selfmatch(wrong, right)
    return (is_common_wrong(wrong, right)
            or is_bare_single_token(wrong)
            or is_real_english_word(wrong))


def is_common_wrong(wrong, right=None):
    """The admission-gate predicate: is this candidate's "wrong" key a common
    English word/phrase or common Cantonese word, i.e. dangerous as a bare
    global replace? Casing-only fixes (whatsapp→WhatsApp) are exempt — the
    replace changes capitalisation, not words."""
    wrong = (wrong or "").strip()
    if not wrong:
        return False
    if right is not None and wrong.lower() == (right or "").strip().lower():
        return False
    return is_common_english(wrong) or is_common_cantonese(wrong)




def unescape(pattern):
    """Best-effort raw text of a re.escape()d variant."""
    return re.sub(r"\\(.)", r"\1", pattern or "")


def history_evidence(rows, wrong, right, context_chars=30):
    """Score one wrong→right rule against the history log.

    kept   – entries whose FINAL text contains `wrong` un-corrected (the
             phrase survived Tinho's own eyes: evidence it can be legitimate
             text — or at least tolerated — so a global rewrite is risky);
    fixed  – entries where an edited/shadow version turned `wrong` into
             `right` (evidence the rule fires on genuine mishearings);
    contexts – up to 6 snippets for a human / Sonnet re-review to read."""



    probe = re.escape(wrong)
    if re.match(r"[A-Za-z]", wrong):
        probe = r"\b" + probe
    if re.search(r"[A-Za-z]$", wrong):
        probe = probe + r"\b"
    probe_re = re.compile(probe, re.IGNORECASE)
    rl = (right or "").lower()
    kept = fixed = 0
    contexts = []
    for r in rows or []:
        text = r.get("text") or ""
        m = probe_re.search(text)
        if not m:
            continue
        corrected = False
        for f in ("edited", "shadow"):
            v = r.get(f)
            if isinstance(v, str) and not probe_re.search(v) \
                    and rl and rl in v.lower():
                corrected = True
        if corrected:
            fixed += 1
        else:
            kept += 1
        if len(contexts) < 6:
            lo = max(0, m.start() - context_chars)
            snippet = text[lo:m.end() + context_chars].replace("\n", " ")
            contexts.append({"time": r.get("time"), "text": snippet,
                             "corrected": corrected})
    return {"kept": kept, "fixed": fixed, "contexts": contexts}


def replay_against_queue(queue_rows, wrong, right, context_chars=30):
    """Replay a candidate wrong->right rule against every OTHER observed
    correction in review_gate's review_queue.jsonl (2026-07-26, "promote
    cheaply, verify immediately" — see review_gate.replay_verify).

    review_queue.jsonl rows are `{wrong, right, ...}` observed corrections,
    not transcripts, so they don't fit history_evidence's `{text, edited,
    shadow}` shape directly. Adapt them: each OTHER correction's own
    approved `right` text becomes a probe text with no correction fields —
    if the candidate's `wrong` pattern shows up verbatim inside it, that's
    an uncorrected ("kept") hit, i.e. text Tinho already approved that the
    candidate rule would clobber. Rows for this exact (wrong, right) pair
    itself are excluded — replaying a rule against its own evidence isn't
    "another correction", and a prefix/substring candidate (e.g. "to" ->
    "tocheckpoint and") would otherwise always self-match.

    Same return shape as history_evidence: {kept, fixed, contexts}. `fixed`
    is always 0 here (there's no edited/shadow field to show a correction
    happened) — kept is the only signal this probe can produce, which is
    exactly the "would this clobber something already approved" question
    replay_verify asks it."""
    probe_rows = [{"text": r.get("right") or ""}
                 for r in (queue_rows or [])
                 if not (r.get("wrong") == wrong and r.get("right") == right)]
    return history_evidence(probe_rows, wrong, right, context_chars=context_chars)


def provenance(doc, wrong, right):
    """shadow / auto / retry / spellout / manual / unknown, from the changelog.
    'manual' means Tinho added it by hand — trusted."""
    desc = f"{wrong}→{right}"
    for e in doc.get("changelog") or []:
        if e.get("kind") == "dict" and e.get("desc") == desc:
            return e.get("source") or "unknown"
    return "unknown"


def was_reverted(doc, wrong, right):
    """True if the changelog or rejected memory shows this pair was ever
    removed/rejected before (a resurrection is instant-delete evidence)."""
    desc = f"{wrong}→{right}"
    for e in doc.get("changelog") or []:
        if e.get("kind") == "removed" and (
                desc in (e.get("desc") or "")
                or f"{right} rule 撤回 ({wrong})" in (e.get("desc") or "")):
            return True
    for r in doc.get("rejected") or []:
        if r.get("kind") == "dict" and r.get("key") == desc:
            return True
    return False

















_DUP_LOOKAHEAD = re.compile(r'\(\?=((?:[^()\\]|\\.)*)\)\s*$')


def is_duplication_sub(pattern, replace):
    """The 因為為 bug (因(?=為|爲)→因為, fired on 194 takes / 7.4%): a trailing
    lookahead matches WITHOUT consuming what follows it, so replacing the
    literal core with a string that already ends in one of the lookahead's
    own alternatives duplicates that text in the output every single time.
    Structural -- true regardless of any history evidence, same as the other
    structural guards in this file."""
    m = _DUP_LOOKAHEAD.search(pattern or "")
    if not m or not replace:
        return False
    alts = [a for a in m.group(1).split("|") if a]
    return any(replace.endswith(a) for a in alts)


def is_casing_only_common_selfmatch(wrong, right):
    """whatsapp→WhatsApp is a legitimate casing fix on a proper noun;
    "is it better"→"Is it better" is the same SHAPE (wrong.lower() ==
    right.lower()) but wrong is an ordinary common phrase, so applying it
    mid-sentence corrupts real speech by capitalising it. is_common_wrong()
    deliberately exempts every casing-only pair -- correct for its own
    narrower question -- so this is the separate, stricter check the audit
    needs: is the phrase ITSELF common, independent of the casing exemption."""
    wrong = (wrong or "").strip()
    right = (right or "").strip()
    if not wrong or wrong == right or wrong.lower() != right.lower():
        return False
    return is_common_english(wrong) or is_common_cantonese(wrong)


def is_noop_variant(wrong, right):
    """A variant identical (not just case-identical) to its own canonical
    does nothing, ever -- e.g. terms["VPS"] containing the literal variant
    "VPS", live today. Not dangerous, just dead weight worth a human glance
    in case it is hiding a typo'd intent."""
    wrong = (wrong or "").strip()
    right = (right or "").strip()
    return bool(wrong) and wrong == right


def raw_evidence(rows, kind, wrong_or_pattern, right_or_replace):
    """Replay ONE rule (a terms variant or a style.subs pattern) over the
    genuine `raw` ASR field of every take that has one. server.py's
    log_history() only stores `raw` when it differs from the final `text` --
    a missing `raw` means that take's own live ruleset made no change at all
    and has nothing to compare (history_evidence() above already covers the
    text/edited/shadow signal for those rows). Uses the SAME substitution
    code the live server runs (polish.apply_dictionary / polish.apply_style),
    imported lazily to avoid polish's numpy/opencc weight and the
    polish<->rule_audit import cycle (polish imports this module), so the
    measurement matches production behaviour exactly rather than
    approximating it.

    fires        - the rule matched the take's pre-polish text
    repairs      - applying it in isolation reproduces EXACTLY the take's own
                   final `text` -- confirms this rule is what turned that
                   mishearing into the right output
    corruptions  - applying it changes text the take's own live pipeline left
                   untouched (real content sitting exactly where the rule
                   would strike), or the pattern carries the duplication
                   signature (is_duplication_sub) -- either way, hard
                   evidence against the rule, not merely absence of evidence
                   for it
    """
    import polish
    fires = repairs = corruptions = 0
    contexts = []
    is_dup = kind == "style_sub" and is_duplication_sub(
        wrong_or_pattern, right_or_replace)
    for r in rows or []:
        base = r.get("raw")
        if not base:
            continue
        final = r.get("text") or ""
        try:
            if kind == "term":
                simulated = polish.apply_dictionary(
                    base, terms={right_or_replace: [wrong_or_pattern]})
            else:
                simulated = polish.apply_style(
                    base, subs=[{"pattern": wrong_or_pattern,
                                 "replace": right_or_replace}])
        except re.error:
            continue
        if simulated == base:
            continue
        fires += 1
        corrupt_hit = is_dup or base == final
        if simulated == final:
            repairs += 1
        if corrupt_hit:
            corruptions += 1
        if len(contexts) < 6:
            contexts.append({"time": r.get("time"), "raw": base,
                             "simulated": simulated, "final": final})
    return {"fires": fires, "repairs": repairs, "corruptions": corruptions,
            "contexts": contexts, "duplication_signature": is_dup}











def classify_term(doc, rows, right, variant):
    wrong = unescape(variant)
    guarded = has_guard(variant)
    common = is_common_wrong(wrong, right)
    casing_only = bool(wrong) and wrong != right and wrong.lower() == right.lower()
    casing_flag = is_casing_only_common_selfmatch(wrong, right)









    structural = not casing_only and (
        is_bare_single_token(wrong) or is_real_english_word(wrong))
    src = provenance(doc, wrong, right)
    ev = history_evidence(rows, wrong, right)
    raw_ev = raw_evidence(rows, "term", variant, right)
    ev = {"kept": ev["kept"] + raw_ev["corruptions"],
          "fixed": ev["fixed"] + raw_ev["repairs"],
          "contexts": (ev["contexts"] + raw_ev["contexts"])[:6]}
    dangerous = (common and not guarded) or casing_flag or structural

    if was_reverted(doc, wrong, right):
        return _f("term", right, variant, wrong, "DELETE", src, ev,
                  "zombie: previously removed/rejected yet live again")
    if is_noop_variant(wrong, right):
        return _f("term", right, variant, wrong, "REVIEW", src, ev,
                  "no-op: variant is identical to its own canonical, has "
                  "zero effect")
    if not dangerous:






        if guarded and ev["kept"] >= 2 and ev["fixed"] == 0:
            return _f("term", right, variant, wrong, "SUSPECT", src, ev,
                      f"context-guarded but {ev['kept']} measured fires "
                      "with zero confirmed repairs — guard may be too "
                      "loose")
        why = "context-guarded" if (common and guarded) else \
            "specific wrong key (not a common word)"
        return _f("term", right, variant, wrong, "KEEP", src, ev, why)
    if common:
        label = "common word"
    elif casing_flag:
        label = ("casing-only self-match on a common phrase (capitalises "
                 "ordinary mid-sentence text)")
    elif is_bare_single_token(wrong):
        label = "bare single-character key"
    else:
        label = "real English dictionary word"
    if src == "manual":
        return _f("term", right, variant, wrong, "KEEP", src, ev,
                  f"{label} but manually added (trusted)")

    if ev["kept"] >= 1 and ev["fixed"] == 0:
        return _f("term", right, variant, wrong, "DELETE", src, ev,
                  f"{label}; {ev['kept']} legitimate uses in history, "
                  "zero evidence it ever fixed anything")
    if ev["kept"] == 0 and ev["fixed"] == 0:
        return _f("term", right, variant, wrong, "DELETE", src, ev,
                  f"{label}; no history evidence it ever fired on a real "
                  "mishearing")
    if ev["kept"] >= 1 and ev["fixed"] >= 1:
        return _f("term", right, variant, wrong, "QUARANTINE", src, ev,
                  f"{label} with mixed evidence ({ev['fixed']} fixed / "
                  f"{ev['kept']} legitimate)")
    return _f("term", right, variant, wrong, "SUSPECT", src, ev,
              f"{label}; only fix-evidence ({ev['fixed']}) — needs review")


def classify_sub(doc, rows, sub, context_safe_fn):
    pat = sub.get("pattern") or ""
    rep = sub.get("replace")
    ev = {"kept": 0, "fixed": 0, "contexts": []}
    try:
        re.compile(pat)
    except re.error:
        return _f("style_sub", rep, pat, pat, "DELETE", "unknown", ev,
                  "pattern does not compile")
    for r in doc.get("rejected") or []:
        if r.get("kind") == "style" and r.get("key") == pat:
            return _f("style_sub", rep, pat, pat, "DELETE", "unknown", ev,
                      "zombie: pattern in rejected memory yet live again")
    if not context_safe_fn(pat):
        return _f("style_sub", rep, pat, pat, "DELETE", "unknown", ev,
                  "no context guard on a style sub (bare pattern)")
    if is_duplication_sub(pat, rep):
        return _f("style_sub", rep, pat, pat, "DELETE", "unknown", ev,
                  "duplication signature: replacement re-includes text its "
                  "own lookahead already peeked at without consuming it "
                  "(the 因為為 bug)")




    raw_ev = raw_evidence(rows, "style_sub", pat, rep)
    ev = {"kept": raw_ev["corruptions"], "fixed": raw_ev["repairs"],
          "contexts": raw_ev["contexts"]}
    if raw_ev["fires"] == 0:
        return _f("style_sub", rep, pat, pat, "KEEP", "unknown", ev,
                  "context-safe pattern, no measured fires yet")
    if raw_ev["corruptions"] >= 1 and raw_ev["repairs"] == 0:
        return _f("style_sub", rep, pat, pat, "DELETE", "unknown", ev,
                  f"guarded but {raw_ev['fires']} measured fires corrupt "
                  f"real speech ({raw_ev['corruptions']} corrupted), zero "
                  "confirmed repairs")
    if raw_ev["corruptions"] >= 1:
        return _f("style_sub", rep, pat, pat, "QUARANTINE", "unknown", ev,
                  f"mixed evidence ({raw_ev['repairs']} confirmed repairs / "
                  f"{raw_ev['corruptions']} corrupted)")
    return _f("style_sub", rep, pat, pat, "KEEP", "unknown", ev,
              f"context-safe pattern, {raw_ev['repairs']} confirmed "
              "repairs, no measured corruption")


def _f(kind, right, variant, wrong, verdict, src, ev, reason):
    return {"kind": kind, "right": right, "variant": variant, "wrong": wrong,
            "verdict": verdict, "provenance": src,
            "kept": ev["kept"], "fixed": ev["fixed"],
            "contexts": ev["contexts"], "reason": reason}


def audit_doc(doc, rows, context_safe_fn, overrides=None):
    """Score EVERY terms variant and style sub. `overrides` maps
    "wrong→right" (terms) or the sub pattern to a (verdict, reason) pair — the
    one-off audit uses it to encode human rulings on contested rules."""
    overrides = overrides or {}
    findings = []
    for right, variants in (doc.get("terms") or {}).items():
        for v in variants:
            f = classify_term(doc, rows, right, v)
            key = f"{f['wrong']}→{right}"
            if key in overrides:
                f["verdict"], f["reason"] = overrides[key]
                f["reason"] += " [ruled]"
            findings.append(f)
    for sub in (doc.get("style") or {}).get("subs", []):
        f = classify_sub(doc, rows, sub, context_safe_fn)
        if f["variant"] in overrides:
            f["verdict"], f["reason"] = overrides[f["variant"]]
            f["reason"] += " [ruled]"
        findings.append(f)
    return findings

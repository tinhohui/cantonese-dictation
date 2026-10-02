#!/usr/bin/env python3
"""Post-processing layer for raw ASR output.

Three stages:
  1. dictionary substitution — fixes proper nouns the recogniser mangles
  2. punctuation — a CT-Transformer model (milliseconds, deterministic, lossless)
  3. a local LLM pass (ollama) — question marks, paragraphing, self-corrections

Stage 3 never rewrites long text wholesale: qwen2.5:7b generates ~12 tok/s on
this M1, so regenerating an 800-char dictation costs ~60s — which is what made
dictations time out and vanish. Instead:
  - short input (≤ SHORT_CHARS) gets the full LLM rewrite (small, so fast)
  - long input keeps the punctuated text verbatim and the LLM only *classifies*:
    which sentences are questions, where paragraphs break, which sentences
    contain a self-correction (only those get rewritten, one at a time)
Worst case the LLM fails entirely → the text still carries stage-2 punctuation.
"""

import concurrent.futures
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request

import numpy as np
from opencc import OpenCC

import rule_audit




_cc = OpenCC("s2hk")

HERE = os.path.dirname(os.path.abspath(__file__))
DICT_PATH = os.path.join(HERE, "dictionary.json")
PUNCT_MODEL = os.path.join(
    HERE, "sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12-int8",
    "model.int8.onnx")
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = os.environ.get("DICTATION_LLM", "qwen2.5:7b")


















FAST_MODEL = os.environ.get("DICTATION_LLM_FAST", MODEL)
TIMEOUT = 18
STRUCT_TIMEOUT = 30














































SHORT_CHARS = 60





















ULTRA_SHORT_CHARS = int(os.environ.get("DICTATION_ULTRA_SHORT_CHARS", "12"))




FULL_MAX_CHARS = 600








SEGMENT_TIMEOUT = int(os.environ.get("DICTATION_SEGMENT_TIMEOUT", "20"))







































FG_HARD_CEILING_SECONDS = float(
    os.environ.get("DICTATION_FG_HARD_CEILING_SECONDS", "6.0"))
FG_ASR_ALLOWANCE_SECONDS = float(
    os.environ.get("DICTATION_FG_ASR_ALLOWANCE_SECONDS", "0.7"))
FG_HARD_BUFFER = float(os.environ.get("DICTATION_FG_HARD_BUFFER", "0.3"))
_FG_CALL_DEFAULT = str(max(
    0.5, FG_HARD_CEILING_SECONDS - FG_ASR_ALLOWANCE_SECONDS - FG_HARD_BUFFER))
FG_TIMEOUT = float(os.environ.get("DICTATION_FG_TIMEOUT", _FG_CALL_DEFAULT))
FG_STRUCT_TIMEOUT = float(os.environ.get("DICTATION_FG_STRUCT_TIMEOUT", _FG_CALL_DEFAULT))






















SHORT_AUDIO_SKIP_SECONDS = float(
    os.environ.get("DICTATION_SHORT_AUDIO_SKIP_SECONDS", "3.0"))












WARM_WAIT_MAX = float(os.environ.get("DICTATION_WARMUP_WAIT_MAX", "60"))


class WarmGate:
    """threading.Event wrapper the whole server agrees on for "is qwen warm"."""

    def __init__(self):
        self._event = threading.Event()

    def is_warm(self):
        return self._event.is_set()

    def mark_warm(self):
        self._event.set()

    def wait(self, timeout=None):
        """Block until warm or until timeout elapses. Returns True if warm,
        False if the bound was hit first (caller proceeds unwarmed anyway)."""
        return self._event.wait(timeout=timeout)


WARM_GATE = WarmGate()

SYSTEM_PROMPT = """你係一個廣東話語音轉文字嘅後期編輯器。輸入係語音辨識嘅原始輸出，可能夾雜英文。

規則：
1. 標點符號要放得準：
   - 問句用「？」。判斷準則：句尾有 啊／呀／咧／呢／嘛／未／冇／好唔好／得唔得／係咪／點樣／可唔可以／幾時 等收尾，或者有明顯疑問語氣，就係問句。
   - 陳述長句要喺自然停頓位（例如「然之後」「如果」「除非」「同埋」「跟住」等語氣詞前後）斷開加逗號；唔好成句冇標點，亦都唔好逗號太密。
   - 一個完整意思講完就收「。」。
   - 並列列舉嘅詞語之間用頓號「、」。
   - 淨係直接引述或者特定名稱先至用引號「」；平時唔好亂加引號。
   - 感嘆先至用感嘆號。
2. 絕對唔好改動用詞、唔好翻譯、唔好將廣東話改成書面語。保持原本口語風格。
3. 如果講者自我修正（例如「唔係，我意思係…」「即係…」「講錯咗…」），只保留修正後嘅版本，刪走講錯果段同修正提示詞。
4. 所有英文字保留原樣，唔好翻譯，唔好改大小寫串法。
5. 如果講者讀出一個字之後逐個字母串出嚟（例如「我串俾你聽」「串法係」之後跟住一串單獨字母），只輸出組合出嚟嘅字本身，刪走啲字母同串字提示語。
6. 輸出繁體中文。
7. 只輸出處理後嘅文字本身，唔好加任何解釋、前言、標題或者引號。
8. 如果需要打英文字（例如自我修正之後要重新打某個英文詞），一律用英式串法，唔係美式串法（例如 colour 唔係 color、organise 唔係 organize、realise 唔係 realize、centre 唔係 center）。講者本身講出嚟嘅英文字保持原樣（見規則 4），呢條規則淨係管你自己要打字嗰啲情況。
9. 讀出標點符號名稱要轉做符號，但名詞用法要保留原字：如果講者喺分句或者成句結尾讀出標點符號嘅名稱（廣東話：問號、逗號、句號、感嘆號；英文：question mark、comma、full stop、exclamation mark），要刪走嗰個詞，改用返實際符號（？，。！）。但如果嗰個詞喺句入面做緊名詞（即係講緊個符號本身，例如「個問號好奇怪」「呢個 comma 用得啱唔啱」），要保持原字，唔好轉做符號。

例子一：
輸入：你今日得唔得閒幫我睇下個 VCP pattern
輸出：你今日得唔得閒幫我睇下個 VCP pattern？

例子二（自我修正，只留修正後版本）：
輸入：聽日三點開會唔係我意思係四點開會記得叫埋 Chloe
輸出：聽日四點開會，記得叫埋 Chloe。

例子三（自我修正嘅另一種講法）：
輸入：呢個 quarter 個 revenue 唔係我講錯咗係上個 quarter 個 revenue 升咗三成
輸出：上個 quarter 個 revenue 升咗三成。

例子四（串字：只輸出組合後嘅字）：
輸入：呢個工具叫 GitHub 我串俾你聽 G I T H U B 你幫我裝好佢
輸出：呢個工具叫 GitHub，你幫我裝好佢。

例子五（句尾讀出標點名稱 → 轉做符號）：
輸入：聽日得唔得閒同我食飯問號
輸出：聽日得唔得閒同我食飯？

例子六（英文標點名稱喺句尾 → 轉做符號）：
輸入：Do you have time tomorrow question mark
輸出：Do you have time tomorrow?

例子七（標點名稱做名詞用，唔喺句尾當指令 → 保持原字）：
輸入：呢句最尾個問號好似打多咗
輸出：呢句最尾個問號好似打多咗。

例子八（英文標點名稱做名詞用 → 保持原字）：
輸入：I think that comma is in the wrong place
輸出：I think that comma is in the wrong place."""

STRUCT_PROMPT = """你係語音轉文字嘅後期分析器。輸入係一段已經加咗標點、逐句編號嘅說話。你唔使重寫，只需要分析，然後輸出一個 JSON object，包含以下欄位：

"q": 語氣係問嘢嘅句子編號，佢哋嘅句號會改做問號「？」。判斷準則：句尾有 啊／呀／咧／呢／嘛／未／冇／好唔好／得唔得／係咪／點樣／可唔可以／幾時 等收尾，或者有明顯疑問語氣，就當係問句。淨係真.疑問句先算，陳述句（例如「除非…」開頭）唔好當問句。冇就 []。
"para": 應該開始新段落嘅句子編號。內容轉咗話題、或者係列舉嘅下一點，先至分段。內容短或者得一個主題就 []。第 1 句唔使列。
"bullets": true 或 false。內容係清晰嘅多點列舉（例如「第一…第二…第三…」）先至 true，否則 false。
"fix": key 係句子編號（字串），value 係改寫後嘅嗰句，喺以下兩種情況先至列出（其他情況絕對唔好改寫）：
  (a) 自我修正（例如「唔係，我意思係…」「講錯咗，係…」）：value 只保留修正後版本。
  (b) 句尾或分句尾讀出咗標點符號名稱（廣東話：問號、逗號、句號、感嘆號；英文：question mark、comma、full stop、exclamation mark）：value 係刪走嗰個詞、改用返實際符號（？，。！）之後嘅句子。但如果嗰個詞喺句入面做緊名詞（即係講緊個符號本身，例如「個問號好奇怪」），唔算，唔好列入 fix。
  冇符合就 {}。如果修正後嘅句子入面要打英文字，一律用英式串法（colour、organise、realise、centre 咁），唔係美式串法。

只輸出 JSON，唔好有其他文字。

例子一：
輸入：
1. 第一件事，我今日要開會討論個 budget。
2. 第二件事，聽日三點唔係，我意思係四點，要 send 個 email 俾 Chloe。
3. 呢個安排得唔得。
輸出：
{"q": [3], "para": [2, 3], "bullets": true, "fix": {"2": "第二件事，聽日四點，要 send 個 email 俾 Chloe。"}}

例子二（句尾讀出標點名稱 → 用 fix 轉做符號）：
輸入：
1. 你聽日得唔得閒去睇戲問號
輸出：
{"q": [], "para": [], "bullets": false, "fix": {"1": "你聽日得唔得閒去睇戲？"}}

例子三（標點名稱做名詞用 → 唔轉，唔列入 fix）：
輸入：
1. 呢句最尾個問號好似打多咗。
輸出：
{"q": [], "para": [], "bullets": false, "fix": {}}"""

_punct = None






PUNCT_ENGLISH_SKIP_RATIO = float(
    os.environ.get("DICTATION_PUNCT_ENGLISH_SKIP_RATIO", "0.75"))


def _english_ratio(text):
    latin = len(re.findall(r"[A-Za-z]", text or ""))
    han = len(re.findall(r"[一-鿿]", text or ""))
    total = latin + han
    return (latin / total) if total else 0.0


def punctuate(text):
    """Deterministic punctuation via CT-Transformer — 3-6ms, cannot lose words."""
    global _punct
    if not text:
        return text
    if _punct is None:
        import sherpa_onnx
        _punct = sherpa_onnx.OfflinePunctuation(
            sherpa_onnx.OfflinePunctuationConfig(
                model=sherpa_onnx.OfflinePunctuationModelConfig(
                    ct_transformer=PUNCT_MODEL)))
















    if _english_ratio(text) >= PUNCT_ENGLISH_SKIP_RATIO:
        return fix_spacing(text)


    bare = re.sub(r"[，。！？、,.!?;；]", "", text)
    out = fix_spacing(_punct.add_punctuation(bare))
    return restore_asr_english_stops(text, out)






















_ASR_ENGLISH_STOP_RE = re.compile(r"([a-z0-9])\.\s+([A-Z][a-z])")


def restore_asr_english_stops(original, punctuated):
    """Put back full stops the ASR found between two English sentences."""
    if not original or not punctuated:
        return punctuated
    for m in _ASR_ENGLISH_STOP_RE.finditer(original):
        joined = m.group(1) + " " + m.group(2)
        if joined in punctuated:
            punctuated = punctuated.replace(
                joined, m.group(1) + ". " + m.group(2), 1)
    return punctuated


def warm_up():
    """Load the models at startup so the first dictation isn't slow.

    Warms BOTH hybrid models (FAST_MODEL for the foreground tail/single-take
    path, MODEL for the background segment rewrites) so neither Tinho's first
    short take NOR his first long recording ever pays a cold load. When the
    hybrid is off (FAST_MODEL == MODEL) the second warm is a cheap no-op on
    the already-resident model. keep_alive="8h" (via _ollama) pins them.

    Always flips WARM_GATE when done, success or failure (see its docstring
    just above SYSTEM_PROMPT) — that gate, not this function's return value,
    is what the rest of the server actually queues on."""
    try:
        punctuate("測試")
        llm_polish("測試", model=FAST_MODEL)
        if MODEL != FAST_MODEL:
            llm_polish("測試", model=MODEL)
        return True
    except Exception:
        return False
    finally:
        WARM_GATE.mark_warm()


def load_dictionary():
    try:
        with open(DICT_PATH, encoding="utf-8") as fh:
            return json.load(fh).get("terms", {})
    except (OSError, json.JSONDecodeError):
        return {}


def load_style():
    """Style layer: context-bounded regex subs (register/word-choice fixes) and
    free-text formatting/wording notes injected into the LLM prompts."""
    try:
        with open(DICT_PATH, encoding="utf-8") as fh:
            style = json.load(fh).get("style", {})
        return style.get("subs", []), style.get("notes", [])
    except (OSError, json.JSONDecodeError):
        return [], []


def apply_style(text, subs=None):
    """Deterministic context-bounded substitutions, applied right after terms.
    Patterns always carry context (never a bare single Han char), so this is
    safe and fast enough for the paste path."""
    if subs is None:
        subs, _ = load_style()
    for sub in subs:
        pat, rep = sub.get("pattern"), sub.get("replace")
        if not pat or rep is None:
            continue
        try:
            text = re.sub(pat, rep, text)
        except re.error:
            continue
    return text












































































_SPELL_ELIGIBLE_CHAR = "[A-Zb-z]"
_SPELL_JOIN_RUN_RE = re.compile(
    rf"(?<![A-Za-z0-9]){_SPELL_ELIGIBLE_CHAR}"
    rf"(?:[\s,，、.．\-]+{_SPELL_ELIGIBLE_CHAR}){{2,}}"
    rf"(?![A-Za-z0-9])(?!\s*[0-9])")






_SPELL_JOIN_CUE_ZH_RE = re.compile(
    r"(?:我)?串(?:俾你聽|你聽|俾你|法係|法|出嚟|字)?[，,、。.\s]*$")
_SPELL_JOIN_CUE_EN_RE = re.compile(
    r"(?i)spell(?:ed|\s+it)?(?:\s+out)?[\s,，、]*$")


def _split_repeated_run(assembled):
    """If `assembled` is an exact repetition of a shorter substring (e.g.
    "VCPVCP" = "VCP" x2), return the repeated units as a list. Otherwise
    return [assembled] unchanged. Only considers proper (smaller) units, so
    a genuine non-repeating acronym is never split."""
    n = len(assembled)
    for unit_len in range(1, n):
        if n % unit_len == 0 and assembled == assembled[:unit_len] * (n // unit_len):
            return [assembled[:unit_len]] * (n // unit_len)
    return [assembled]


def join_spelled_out_letters(text, terms=None, mark_spans=False):
    """Join a spelled-out run of 3+ single letters into one word, resolved
    against the curated dictionary for casing. See the long module comment
    above for the full rationale, the "a" exclusion rule, repeat-splitting,
    and cue-phrase stripping.

    CONFIRMATION SPELL-OUT (matches SYSTEM_PROMPT's own worked example,
    "呢個工具叫 GitHub 我串俾你聽 G I T H U B 你幫我裝好佢" ->
    "呢個工具叫 GitHub，你幫我裝好佢。"): when a cue phrase immediately
    precedes the run AND the word right before that cue phrase already
    equals the resolved word (case-insensitively), the spell-out is just
    Tinho confirming a word he already said correctly — the cue phrase and
    letters are dropped entirely, not duplicated. Without a cue phrase
    (no explicit "let me spell it" signal), or when the preceding word
    differs, the resolved word is inserted as normal — this function only
    ever fixes the CURRENT run, it never reaches back to rewrite an earlier
    mishearing (that correction belongs to detect_spellouts() feeding the
    dictionary-learning pipeline, not a live inline rewrite).

    mark_spans=False (default, every existing/direct caller) returns plain
    text, byte-identical to before this parameter existed. mark_spans=True
    (only _polish()'s pipeline uses this) wraps each freshly-assembled
    token in the apply_dictionary() protected-span markers instead of
    inserting it bare, so a following apply_dictionary() call can tell a
    JUST-joined acronym apart from ordinary text and refuse to re-match it
    — see apply_dictionary()'s module comment for the full rationale
    (the ROQ/GROQ and OAUTH/IBK collisions this exists to prevent)."""
    if not text or not _SPELL_JOIN_RUN_RE.search(text):
        return text
    if terms is None:
        terms = load_dictionary()
    vocab = {k.upper(): k for k in terms}

    out = []
    pos = 0
    for m in _SPELL_JOIN_RUN_RE.finditer(text):
        start, end = m.start(), m.end()
        if start < pos:
            continue
        letter_matches = list(re.finditer(r"[A-Za-z]", m.group()))











        after = text[end:]
        if (len(letter_matches) >= 4
                and letter_matches[-1].group().lower() == "i"
                and re.match(r"\s*[a-z]", after)):
            end = start + letter_matches[-2].end()
            letter_matches = letter_matches[:-1]
        if len(letter_matches) < 3:
            continue
        letters = [lm.group() for lm in letter_matches]
        assembled = "".join(letters).upper()
        units = _split_repeated_run(assembled)
        resolved = " ".join(vocab.get(u, u) for u in units)

        prefix = text[pos:start]
        no_cue = _SPELL_JOIN_CUE_ZH_RE.sub("", prefix)
        no_cue = _SPELL_JOIN_CUE_EN_RE.sub("", no_cue)
        had_cue = no_cue != prefix
        out.append(no_cue)
        if had_cue:
            preceding_tokens = _SPELL_TOKEN_RE.findall(no_cue)
            preceding = preceding_tokens[-1] if preceding_tokens else ""
            if preceding.lower() == resolved.replace(" ", "").lower():
                pos = end
                continue
        out.append(_PROTECT_START + resolved + _PROTECT_END if mark_spans else resolved)
        pos = end
    out.append(text[pos:])






    return re.sub(r" {2,}", " ", "".join(out))

























































_CN_NUM_CHARS = "零一二三四五六七八九十百千萬兩"
_CN_DIGIT_MAP = {"零": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4,
                 "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT_MAP = {"十": 10, "百": 100, "千": 1000}
_CN_BIG_UNIT_MAP = {"萬": 10000}
_CN_PERCENT_AFTER_RE = re.compile(
    rf"([{_CN_NUM_CHARS}]+)\s*(?:percent|%|趴)")
_BAI_FEN_ZHI_RE = re.compile(rf"百分之([{_CN_NUM_CHARS}]+)")
_CN_NUMERAL_RUN_RE = re.compile(rf"[{_CN_NUM_CHARS}]+")


def _cn_numeral_to_int(s):
    """Parse a run of Chinese numeral characters (十/百/千/萬 positional
    system) into an int. Returns None if `s` contains anything outside the
    numeral character set, or contains 零 (see the module comment: a digit-
    string reading like a year or phone number can't be told apart from a
    genuine positional zero, so this bails rather than guess) — the caller
    then leaves the original text untouched rather than risk a silent
    truncation."""
    if not s or "零" in s:
        return None
    if "萬" in s:
        idx = s.index("萬")
        before, after = s[:idx], s[idx + 1:]
        if "萬" in after:
            return None
        before_val = _cn_numeral_to_int(before) if before else 1
        after_val = _cn_numeral_to_int(after) if after else 0
        if before_val is None or after_val is None:
            return None
        return before_val * 10000 + after_val
    total = 0
    section = 0
    num = 0
    for ch in s:
        if ch in _CN_DIGIT_MAP:
            num = _CN_DIGIT_MAP[ch]
        elif ch in _CN_UNIT_MAP:
            unit = _CN_UNIT_MAP[ch]
            section += (num or 1) * unit
            num = 0
        else:
            return None
    section += num
    total += section
    return total


def convert_cn_percent_numerals(text):
    """Chinese numeral -> Arabic digits, ONLY in the two percent contexts
    above. See the module comment for the scope boundary."""
    if not text:
        return text

    def _repl(m):
        val = _cn_numeral_to_int(m.group(1))
        return f"{val}%" if val is not None else m.group(0)

    text = _BAI_FEN_ZHI_RE.sub(_repl, text)
    text = _CN_PERCENT_AFTER_RE.sub(_repl, text)
    return text


def convert_cn_numerals_general(text):
    """Chinese numeral -> Arabic digits, the BROADENED (non-percent-only)
    rule. See the long module comment above for the full scope and the
    evidence behind each exclusion. Runs AFTER convert_cn_percent_numerals
    in the pipeline, so anything already converted there has no remaining
    Chinese-numeral characters left for this function to touch."""
    if not text or not _CN_NUMERAL_RUN_RE.search(text):
        return text

    out = []
    pos = 0
    for m in _CN_NUMERAL_RUN_RE.finditer(text):
        start, end = m.start(), m.end()
        run = m.group()
        preceding = text[start - 1] if start > 0 else ""
        following = text[end] if end < len(text) else ""

        if preceding == "第":
            continue
        if run == "十" and following == "分" and text[end:end + 2] not in ("分鐘", "分鍾"):
            continue
        if run == "一" and following in ("定",):
            continue
        if run == "一" and following in ("齊", "齐"):
            continue
        if len(run) < 2 and run not in _CN_UNIT_MAP and run not in _CN_BIG_UNIT_MAP:
            continue

        val = _cn_numeral_to_int(run)
        if val is None:
            continue

        out.append(text[pos:start])
        out.append(str(val))
        pos = end
    out.append(text[pos:])
    return "".join(out)







































_TERMINAL_PUNCT_CHARS = "。！？!?"
_ZH_TRAILING_PARTICLES_RE = re.compile(r"[啊呀喇啦囉嘅嘞㗎嘛呢囖噃喎]+$")
_COMPLETE_ANSWER_WORDS_ZH = frozenset({
    "好", "得", "得嘅", "係", "系", "唔係", "唔系", "岩", "啱", "冇問題",
    "明白", "知道", "收到", "多謝", "唔該", "唔使", "唔洗", "唔駛", "對",
    "掂", "搞掂", "可以", "行", "冇問題啦",
})
_COMPLETE_ANSWER_WORDS_EN = frozenset({
    "yes", "no", "ok", "okay", "sure", "yeah", "yep", "nope", "right",
    "correct", "agreed", "agree", "thanks", "thank you", "got it", "done",
    "noted", "understood", "fine", "great", "perfect",
})
_SHORT_ANSWER_MAX_CHARS = 12


def apply_terminal_punctuation_for_short_answers(text):
    """Append a language-appropriate terminal mark to a standalone complete
    short answer; leave a mid-sentence fragment untouched. See the long
    module comment above for the closed word list, its honest limits, and
    what real-data check was and wasn't possible."""
    if not text:
        return text
    stripped = text.rstrip()
    if not stripped:
        return text
    if stripped[-1] in _TERMINAL_PUNCT_CHARS:
        return text
    if len(stripped) > _SHORT_ANSWER_MAX_CHARS:
        return text
    core = _ZH_TRAILING_PARTICLES_RE.sub("", stripped)
    if core not in _COMPLETE_ANSWER_WORDS_ZH \
            and stripped.lower() not in _COMPLETE_ANSWER_WORDS_EN:
        return text
    mark = "。" if CJK_RE.search(stripped) else "."
    return stripped + mark












STYLE_NOTES_PROMPT_CAP = 20











NOTES_SNAPSHOT_PATH = os.path.join(HERE, ".style_notes_snapshot")


def load_prompt_notes():
    """The style notes injected into LLM prompts: the consolidation-boundary
    snapshot when one exists, else (first run, before any consolidation) the
    live notes list."""
    try:
        with open(NOTES_SNAPSHOT_PATH, encoding="utf-8") as fh:
            notes = json.load(fh)
        if isinstance(notes, list):
            return [n for n in notes if isinstance(n, str)]
    except (OSError, json.JSONDecodeError):
        return load_style()[1]
    _, notes = load_style()
    return notes


def _style_notes_block(notes):
    """Render the user's formatting/wording preferences as a prompt suffix."""
    notes = [n for n in (notes or []) if n and n.strip()]
    if not notes:
        return ""
    if len(notes) > STYLE_NOTES_PROMPT_CAP:
        notes = notes[:STYLE_NOTES_PROMPT_CAP]
    lines = "\n".join(f"- {n.strip()}" for n in notes)
    return ("\n\n用家嘅格式／用詞偏好（請盡量遵守，但唔好因此改動用詞或者刪內容）：\n"
            + lines)





























_PROTECT_START = "\x00"
_PROTECT_END = "\x01"


def _protected_ranges(text):
    """(start, end) index pairs -- inclusive of the marker characters
    themselves -- for every well-formed _PROTECT_START/_PROTECT_END pair
    currently in `text`. Re-scanned fresh before each term's substitution
    pass inside apply_dictionary, so it stays correct even though earlier
    passes may have already changed everything after a given span -- the
    markers travel embedded in the text itself instead of being tracked as
    external offsets, so there is nothing that can drift out of sync."""
    ranges = []
    i = 0
    while True:
        s = text.find(_PROTECT_START, i)
        if s == -1:
            break
        e = text.find(_PROTECT_END, s)
        if e == -1:
            break
        ranges.append((s, e))
        i = e + 1
    return ranges











_LETTERS_ONLY_RE = re.compile(r"[^0-9a-z]+")


def _same_letters(a, b):
    return (_LETTERS_ONLY_RE.sub("", (a or "").lower())
            == _LETTERS_ONLY_RE.sub("", (b or "").lower()))


def apply_dictionary(text, terms=None):
    if terms is None:
        terms = load_dictionary()
    for correct, variants in terms.items():




        usable = []
        for variant in sorted(variants, key=len, reverse=True):
            try:
                re.compile(variant)
            except re.error:
                continue
            usable.append(variant)
        if not usable:
            continue



        usable.insert(0, re.escape(correct))


        bounded = []
        for v in usable:
            if re.match(r"^[A-Za-z]", v):
                v = r"\b" + v
            if re.search(r"[A-Za-z]$", v):
                v = v + r"\b"
            bounded.append(v)
        pattern = "|".join(f"(?:{v})" for v in bounded)




        protected = _protected_ranges(text) if _PROTECT_START in text else None

        def _repl(m, _correct=correct, _protected=protected):
            if _protected:
                for s, e in _protected:
                    if s <= m.start() and m.end() <= e:



















                        if (m.start() == s + len(_PROTECT_START)
                                and m.end() == e
                                and _same_letters(m.group(0), _correct)):
                            break
                        return m.group(0)
            return _correct

        try:
            text = re.sub(pattern, _repl, text, flags=re.IGNORECASE)
        except re.error:
            continue
    if _PROTECT_START in text:
        text = text.replace(_PROTECT_START, "").replace(_PROTECT_END, "")
    return text








_inflight_lock = threading.Lock()
_inflight_resps = set()


def cancel_inflight():
    """Abort any in-flight cancellable ollama generation. Returns how many were
    cancelled. Safe to call when nothing is in flight (returns 0)."""
    with _inflight_lock:
        resps = list(_inflight_resps)
        _inflight_resps.clear()
    n = 0
    for resp in resps:


        try:
            sock = resp.fp.raw._sock
            sock.shutdown(socket.SHUT_RDWR)
            n += 1
        except Exception:
            pass
        try:
            resp.close()
        except Exception:
            pass
    return n
















HARD_TIMEOUT_BUFFER = 15


def run_with_hard_timeout(fn, hard_timeout, *args, **kwargs):
    """Run fn(*args, **kwargs) with an unconditional wall-clock ceiling that
    does NOT depend on fn's own internal timeout(s) firing correctly.

    The ceiling parameter is named `hard_timeout`, not `timeout` — several
    callers (llm_polish, in particular) have their OWN `timeout=` keyword
    that must pass through untouched via **kwargs; naming this parameter
    `timeout` too caused a `got multiple values for argument 'timeout'`
    TypeError at every call site that forwarded one (caught by the callers'
    blanket `except Exception`, so it silently fell back to the lossless
    path instead of ever calling the LLM — found by test_quick_wins.py's
    test_above_threshold_still_uses_llm).

    fn runs on its own daemon thread; the CALLER never waits past
    `hard_timeout` seconds, full stop. If fn is still running when the
    deadline hits, it is abandoned — Python cannot safely kill a thread
    stuck inside a C call or a blocked socket read — so it keeps running in
    the background and its eventual result (or exception) is discarded.
    That's exactly what lets the CALLER move on and, in server.py, reach
    its own `finally: release the lock` on schedule instead of never
    reaching it.

    Returns (result, timed_out). On timed_out=True, result is None. Any
    exception fn raises is re-raised here (not swallowed) if fn finishes
    within the deadline — only a genuine timeout is turned into a
    (None, True) sentinel instead of an exception.
    """
    box = {}

    def _run():
        try:
            box["value"] = fn(*args, **kwargs)
        except BaseException as exc:
            box["error"] = exc

    t = threading.Thread(target=_run, daemon=True, name="polish-hard-timeout")
    t.start()
    t.join(hard_timeout)
    if t.is_alive():
        return None, True
    if "error" in box:
        raise box["error"]
    return box.get("value"), False


def _ollama(prompt, timeout, force_json=False, cancellable=False, model=None):
    payload = {
        "model": model or MODEL,
        "prompt": prompt,






        "stream": True,


        "keep_alive": "8h",
        "options": {"temperature": 0.1, "top_p": 0.9},
    }
    if force_json:
        payload["format"] = "json"
    req = urllib.request.Request(
        OLLAMA_URL, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=timeout)
    if cancellable:
        with _inflight_lock:
            _inflight_resps.add(resp)
    deadline = time.time() + timeout
    chunks = []
    try:
        for line in resp:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            frag = obj.get("response")
            if frag:
                chunks.append(frag)
            if obj.get("done"):
                break
            if time.time() > deadline:
                raise TimeoutError("ollama generation exceeded timeout")
        return "".join(chunks).strip()
    finally:
        if cancellable:
            with _inflight_lock:
                _inflight_resps.discard(resp)
        try:
            resp.close()
        except Exception:
            pass


def llm_polish(text, timeout=TIMEOUT, cancellable=False, model=None):
    notes = load_prompt_notes()
    out = _ollama(f"{SYSTEM_PROMPT}{_style_notes_block(notes)}"
                  f"\n\n原始輸出：\n{text}\n\n處理後：", timeout,
                  cancellable=cancellable, model=model)

    if len(out) > 1 and out[0] in "「\"'" and out[-1] in "」\"'":
        out = out[1:-1].strip()
    return _cc.convert(out)




























SENT_SPLIT_RE = re.compile(
    r"(?<=[。！？!?])"
    r"|(?<=[a-z0-9)\]]\.)(?=\s+[A-Z一-鿿])"
)





_LATIN_EDGE_RE = re.compile(r"[A-Za-z0-9,.;:!?'\"\)\]]$")


def split_sentences(text):
    return [s for s in (p.strip() for p in SENT_SPLIT_RE.split(text)) if s]


def join_sentences(parts):
    """Re-join split sentences losslessly: a space between two Latin-script
    neighbours, nothing between CJK ones."""
    out = ""
    for part in parts:
        if out and _LATIN_EDGE_RE.search(out) and re.match(r"[A-Za-z0-9]", part):
            out += " "
        out += part
    return out


def llm_structure(sentences, model=None, timeout=STRUCT_TIMEOUT, cancellable=False):
    """Ask the LLM to classify, not rewrite: tiny output, so fast even at 12 tok/s.

    timeout is the ollama call ceiling — foreground (full=False) passes the
    tight FG_STRUCT_TIMEOUT so a stall falls back to punctuated text in seconds
    instead of holding request_lock for the full STRUCT_TIMEOUT.

    cancellable=True registers the call so a STOP racing a background segment
    (see _polish's stop-race-fallback comment) can abort it via
    cancel_inflight() instead of waiting it out."""
    numbered = "\n".join(f"{i+1}. {s}" for i, s in enumerate(sentences))
    notes = load_prompt_notes()
    out = _ollama(f"{STRUCT_PROMPT}{_style_notes_block(notes)}"
                  f"\n\n輸入：\n{numbered}\n輸出：",
                  timeout, force_json=True, cancellable=cancellable, model=model)
    return json.loads(out)


CJK_RE = re.compile(r"[一-鿿]")
LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")


def dropped_latin(original, candidate):
    """English words the model silently deleted.

    Rule says keep English verbatim, but a small model will sometimes drop a
    token mid-sentence ("project schedule" -> "project"), which reads fluently
    enough that the loss is easy to miss.
    """


    before = {w.lower() for w in LATIN_RE.findall(original) if len(w) > 1}
    after = {w.lower() for w in LATIN_RE.findall(candidate)}



    return sorted(before - after)





_HOMOPHONE_CLASSES = ["係系繫喺", "嘅既", "咗左", "嚟黎", "嘢野", "畀俾", "噉咁", "哋地"]
_HOMOPHONE_MAP = {c: cls[0] for cls in _HOMOPHONE_CLASSES for c in cls}


def _canon_chars(chars):
    return {_HOMOPHONE_MAP.get(c, c) for c in chars}


def novel_cjk(original, candidate):
    """Han characters the model invented — it may only add punctuation or delete.

    A small model will occasionally swap a correct character for a visually or
    phonetically similar one (畀 -> 畤), which is worse than leaving the raw
    text alone because it looks plausible.
    """
    return _canon_chars(CJK_RE.findall(candidate)) - _canon_chars(CJK_RE.findall(original))


def fix_spacing(text):
    """Restore the space between Han characters and Latin words.

    Both the punctuation model and the LLM reliably strip it
    ("呢個 quarter 個" -> "呢個quarter個"), so this is repaired mechanically.
    """
    text = re.sub(r"([一-鿿])([A-Za-z0-9])", r"\1 \2", text)
    text = re.sub(r"([A-Za-z0-9])([一-鿿])", r"\1 \2", text)
    return re.sub(r" {2,}", " ", text)








_HAN_CHAR_RE = re.compile(r"[一-鿿]")
_LATIN_LETTER_RE = re.compile(r"[A-Za-z]")
_FULL_TO_ASCII_PUNCT = {"，": ",", "。": ".", "？": "?", "！": "!", "：": ":", "；": ";"}
_PUNCT_SPLIT_RE = re.compile(
    "([" + "".join(_FULL_TO_ASCII_PUNCT.keys())
    + "".join(_FULL_TO_ASCII_PUNCT.values()) + "])")
_ASCII_PUNCT_CLASS = "".join(re.escape(c) for c in _FULL_TO_ASCII_PUNCT.values())


def fix_english_punctuation(text):
    """Convert full-width ，。？！：； to ASCII ,.?!:; inside clauses that are
    predominantly ASCII/English, leaving Cantonese and mixed code-switching
    clauses (any clause containing at least one Han character) on full-width
    punctuation.

    A "clause" is the text run between two punctuation marks (either width),
    so a single line can convert some delimiters and keep others — e.g. an
    English sentence followed by a Chinese one. Spacing is normalised around
    the ASCII marks only (no space before, exactly one space after, none at
    the very end of the string); full-width spacing is left untouched.
    """
    if not text:
        return text
    parts = _PUNCT_SPLIT_RE.split(text)
    out = []
    n = len(parts)
    for i in range(0, n, 2):
        clause = parts[i]
        delim = parts[i + 1] if i + 1 < n else None
        if delim is None:
            out.append(clause)
            break
        han = len(_HAN_CHAR_RE.findall(clause))
        latin = len(_LATIN_LETTER_RE.findall(clause))
        predominantly_english = latin > 0 and han == 0
        if predominantly_english and delim in _FULL_TO_ASCII_PUNCT:
            out.append(clause.rstrip(" "))
            out.append(_FULL_TO_ASCII_PUNCT[delim])
        else:
            out.append(clause)
            out.append(delim)
    result = "".join(out)
    result = re.sub(rf"\s+([{_ASCII_PUNCT_CLASS}])", r"\1", result)
    result = re.sub(rf"([{_ASCII_PUNCT_CLASS}])(?=\S)", r"\1 ", result)
    return result.rstrip(" ")

















































































_CONTENT_CHANGE_LOG_PATH = os.path.join(HERE, "server.log")


def _log_content_change(operation, decision, detail=None, take_id=None):
    def _write():
        try:
            ts = time.strftime("%Y-%m-%dT%H:%M:%S")
            line = (f"{ts} content-change op={operation} decision={decision}")
            if detail:
                line += f" {detail}"
            line += f" take_id={take_id if take_id else 'n/a'}"
            with open(_CONTENT_CHANGE_LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
    try:
        threading.Thread(target=_write, daemon=True).start()
    except Exception:
        pass


def _safe_fix(original, candidate):
    """Accept a per-sentence self-correction rewrite only if it is plausible.

    Damage is bounded to one sentence, but still refuse invented Han characters,
    vanished English words, and implausible shrink/growth.
    """
    candidate = _cc.convert(candidate.strip())
    if not candidate:
        return None
    if novel_cjk(original, candidate) or dropped_latin(original, candidate):
        return None
    if len(candidate) < len(original) * 0.2 or len(candidate) > len(original) * 2:
        return None
    return candidate
































_Q_TAIL_RE = re.compile(
    r"(?:係咪|係唔係|(?:好|得|要|使|駛|洗)唔(?:好|得|要|使|駛|洗)"
    r"|可唔可以|得唔得閒|有冇|對嗎|係嗎|真嗎|嗎)\s*[。\.]$")
_Q_NE_RE = re.compile(
    r"(?:咩|乜|點|邊|幾|如何|怎樣|什麼|甚麼|為什麼|點樣).*呢\s*[。\.]$")



















_CONTRACTIONS = {
    "Ill": "I'll", "Im": "I'm", "Ive": "I've", "Id": "I'd",
    "dont": "don't", "doesnt": "doesn't", "didnt": "didn't",
    "cant": "can't", "wont": "won't",
    "wouldnt": "wouldn't", "couldnt": "couldn't", "shouldnt": "shouldn't",
    "isnt": "isn't", "arent": "aren't", "wasnt": "wasn't", "werent": "weren't",
    "havent": "haven't", "hasnt": "hasn't", "hadnt": "hadn't",
    "youre": "you're", "youve": "you've", "youll": "you'll",
    "theyre": "they're", "theyve": "they've", "theyll": "they'll",
    "thats": "that's", "whats": "what's", "theres": "there's",
    "heres": "here's", "hes": "he's", "shes": "she's",
    "wheres": "where's", "hows": "how's", "whos": "who's",
}



_CAPS_ONLY = ("Ill", "Im", "Ive", "Id")
_CONTRACTION_RE = re.compile(
    r"\b(" + "|".join(
        w if w in _CAPS_ONLY else f"{w}|{w.capitalize()}"
        for w in sorted(_CONTRACTIONS, key=len, reverse=True)
    ) + r")\b")


def restore_english_contractions(text):
    """Put back the apostrophe SenseVoice drops. Word-bounded, so a listed
    form can never fire inside a longer word."""
    def _sub(m):
        w = m.group(1)
        fixed = _CONTRACTIONS.get(w)
        if fixed is not None:
            return fixed
        fixed = _CONTRACTIONS.get(w[0].lower() + w[1:])
        return fixed[0].upper() + fixed[1:] if fixed else w
    return _CONTRACTION_RE.sub(_sub, text)
































COHESIVE_MIN_WHOLE = int(os.environ.get("DICTATION_COHESIVE_MIN_WHOLE", "20"))
COHESIVE_MAX_SPLIT = int(os.environ.get("DICTATION_COHESIVE_MAX_SPLIT", "2"))
COHESIVE_MAX_TAKES = int(os.environ.get("DICTATION_COHESIVE_MAX_TAKES", "4000"))
HISTORY_PATH = os.path.join(HERE, "history.jsonl")

_MIDWORD_CUT_RE = re.compile(r"([一-鿿])。[ \t]*(?=([一-鿿]))")
_cohesive = None


def _cohesive_bigrams(path=None):
    """Bigrams that Tinho's own accepted output says are single words.

    Built once per process — reading the whole history is milliseconds and
    happens off the dictation path, at first use. Returns an empty set on any
    read/parse problem so a missing or corrupt history degrades to "no repair",
    never to an exception on the paste path."""
    global _cohesive
    if _cohesive is not None:
        return _cohesive
    whole, split = {}, {}
    try:
        with open(path or HISTORY_PATH, encoding="utf-8") as fh:
            lines = fh.readlines()[-COHESIVE_MAX_TAKES:]
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            text = (rec.get("final") or rec.get("output")
                    or rec.get("text") or "")
            if not text:
                continue


            for run in re.split(r"[^一-鿿]+", text):
                for i in range(len(run) - 1):
                    b = run[i:i + 2]
                    whole[b] = whole.get(b, 0) + 1
            for m in re.finditer(r"([一-鿿])。\s*([一-鿿])", text):
                b = m.group(1) + m.group(2)
                split[b] = split.get(b, 0) + 1
    except OSError:
        _cohesive = frozenset()
        return _cohesive
    _cohesive = frozenset(
        b for b, n in whole.items()
        if n >= COHESIVE_MIN_WHOLE and split.get(b, 0) <= COHESIVE_MAX_SPLIT)
    return _cohesive


def repair_midword_sentence_cuts(text, cohesive=None):
    """Drop a 。 that CT-Transformer planted inside a cohesive word."""
    if not text:
        return text
    known = _cohesive_bigrams() if cohesive is None else cohesive
    if not known:
        return text

    def _sub(m):
        pair = m.group(1) + m.group(2)
        return m.group(1) if pair in known else m.group(0)

    return _MIDWORD_CUT_RE.sub(_sub, text)





























_COMMA_BEFORE_SENTENCE_RE = re.compile(r"([a-z0-9)\]])\s*,\s+([A-Z][a-z]+)\b")


def fix_comma_before_new_sentence(text, terms=None):
    """Promote a comma to a full stop when it is really a sentence break."""
    if not text or "," not in text:
        return text
    canonicals = {k.lower() for k in (terms if terms is not None
                                      else load_dictionary())}

    def _sub(m):
        word = m.group(2)
        if word.lower() in canonicals:
            return m.group(0)
        if not rule_audit.is_real_english_word(word, min_len=2):
            return m.group(0)
        return f"{m.group(1)}. {word}"

    return _COMMA_BEFORE_SENTENCE_RE.sub(_sub, text)


































TRAILING_INTERJECTIONS = ("yeah", "yes", "okay", "ok", "uh", "um",
                          "ah", "oh", "mm", "hmm", "i")
_TRAILING_FILLER_RE = re.compile(
    r"^(?P<body>.*?[。．.!?！？])\s*(?P<filler>[A-Za-z]+)\s*[.。]\s*$", re.S)
TRAILING_FILLER_MIN_BODY = int(
    os.environ.get("DICTATION_TRAILING_FILLER_MIN_BODY", "20"))


def strip_hallucinated_trailing_interjection(text, log_fn=None):
    """Drop a standalone trailing interjection that follows finished content."""
    if not text:
        return text
    m = _TRAILING_FILLER_RE.match(text)
    if not m:
        return text
    if m.group("filler").lower() not in TRAILING_INTERJECTIONS:
        return text
    body = m.group("body").strip()
    if len(body) < TRAILING_FILLER_MIN_BODY:
        return text
    if log_fn:
        log_fn("trailing-filler stripped: %r after %d chars of content"
               % (m.group("filler"), len(body)))
    return body



















_CJK_DECIMAL_RE = re.compile(r"(?<=\d)\s*點\s*(?=\d)")
_EN_NUMBER_WORD = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}
_EN_DIGIT_TOKEN = r"(?:\d|" + "|".join(_EN_NUMBER_WORD) + r")"


_EN_DECIMAL_RE = re.compile(
    r"\b(\d+|" + "|".join(_EN_NUMBER_WORD) + r")\s+point\s+"
    r"(" + _EN_DIGIT_TOKEN + r"(?:\s+" + _EN_DIGIT_TOKEN + r")*)\b",
    re.IGNORECASE)


def _en_digits(chunk):
    out = []
    for tok in chunk.split():
        tok = tok.strip().lower()
        if tok.isdigit():
            out.append(tok)
        elif tok in _EN_NUMBER_WORD:
            out.append(_EN_NUMBER_WORD[tok])
        else:
            return None
    return "".join(out)


def fix_spoken_decimals(text):
    """Join a spoken decimal into a real number, in either script."""
    if not text:
        return text
    text = _CJK_DECIMAL_RE.sub(".", text)

    def _en(m):
        head = m.group(1).lower()
        head = head if head.isdigit() else _EN_NUMBER_WORD.get(head)
        tail = _en_digits(m.group(2))
        if head is None or tail is None:
            return m.group(0)
        return f"{head}.{tail}"

    return _EN_DECIMAL_RE.sub(_en, text)


























_ASR_CONNECTIVES = (
    "so", "and", "but", "or", "otherwise", "anyway", "then", "because",
    "however", "though", "although", "yet", "plus", "also",
)








_UNBELIEVED_STOP_MAX_WORDS = int(
    os.environ.get("DICTATION_UNBELIEVED_STOP_MAX_WORDS", "4"))
_UNBELIEVED_STOP_RE = re.compile(
    r"(?:^|(?<=[.!?])\s)([^.!?]*?[a-z0-9])\.\s+([a-z]{2,})\b")



















_FALSE_STOP_NEGATED_AUX_RE = re.compile(
    r"(?<=[a-z])\.(\s+)(Does|Do|Is|Are|Was|Were|Has|Have|Had|Did|Can|Could"
    r"|Will|Would|Should)(\s+not\b)")














_SEAM_FUSE_MIN_TAIL = 3
_SEAM_FUSE_RE = re.compile(r"\b[A-Za-z]{6,}\b")


def _english_lexicon(path="/usr/share/dict/words"):
    global _ENGLISH_LEXICON
    try:
        return _ENGLISH_LEXICON
    except NameError:
        pass
    try:
        with open(path) as fh:
            _ENGLISH_LEXICON = {w.strip().lower() for w in fh if w.strip()}
    except Exception:
        _ENGLISH_LEXICON = set()
    return _ENGLISH_LEXICON


def repair_seam_fused_words(text, lexicon=None):
    """Collapse a word the segment overlap made the recogniser say twice."""
    if not text:
        return text
    words = _english_lexicon() if lexicon is None else lexicon
    if not words:
        return text

    def _sub(m):
        w = m.group(0)
        lw = w.lower()
        if lw in words:
            return w
        for cut in range(len(lw) - _SEAM_FUSE_MIN_TAIL, 2, -1):
            head, tail = lw[:cut], lw[cut:]
            if head in words and head.endswith(tail):
                return w[:cut]
        return w

    return _SEAM_FUSE_RE.sub(_sub, text)


def repair_false_stop_before_negated_auxiliary(text):
    """Turn an ASR-invented stop before "<Aux> not ..." back into a comma.

    Runs AFTER punctuation restoration and BEFORE mark_english_questions, so
    the false sentence start never reaches the question heuristic."""
    if not text:
        return text

    def _sub(m):
        return "," + m.group(1) + m.group(2).lower() + m.group(3)

    return _FALSE_STOP_NEGATED_AUX_RE.sub(_sub, text)


def drop_unbelieved_english_stops(text):
    """Repair a full stop the ASR followed with a lowercase word."""
    if not text:
        return text

    def _sub(m):
        clause, word = m.group(1), m.group(2)
        whole = m.group(0)
        if word in _ASR_CONNECTIVES:
            return whole.replace(clause + ".", clause + ",", 1)
        if len(clause.split()) <= _UNBELIEVED_STOP_MAX_WORDS:
            return whole.replace(clause + ".", clause, 1)
        return whole

    return _UNBELIEVED_STOP_RE.sub(_sub, text)


















_EN_WH = r"(?:what|why|how|when|where|who|which|whose|whom)"
_EN_AUX = (r"(?:do|does|did|is|are|was|were|can|could|will|would|should|shall"
           r"|have|has|had|am|may|might|must)")






























































_EN_QUESTION_RE = re.compile(
    r"(?:^|(?<=[.!?])\s+)((?:" + _EN_WH + r"|" + _EN_AUX +
    r")\b[^.!?]{2,200})"
    r"(?:\.(?=\s*$|\s+(?-i:[A-Z]))|(?=\s*$))", re.IGNORECASE)
_EN_IMPERATIVE_RE = re.compile(
    r"^do\s+(?:it|this|that|so|them|these|those)\b", re.IGNORECASE)
_EN_SUBORDINATE_RE = re.compile(
    r"^(?:when|where|while|whenever|wherever)\b(?!\s+" + _EN_AUX + r"\b)"
    r"[^,]{0,80}(?:,|$)", re.IGNORECASE)



_EN_CLAUSE_CUT_RE = re.compile(
    r",\s*(?=(?:and|or|but|so|then)\b"
    r"|(?:" + _EN_WH + r"|" + _EN_AUX + r")\b"
    r"|[a-z]+\s+(?:it|this|that|so|them|these|those|him|her|us|me)\b)",
    re.IGNORECASE)


def mark_english_questions(text):
    """Add the "?" an English question needs, from sentence shape alone."""
    if not text:
        return text

    def _sub(m):
        body = m.group(1)
        stripped = body.strip()
        if _EN_IMPERATIVE_RE.match(stripped) or _EN_SUBORDINATE_RE.match(stripped):
            return m.group(0)
        cut = _EN_CLAUSE_CUT_RE.search(body)
        if cut:
            head, tail = body[:cut.start()], body[cut.end():]



            trailing = m.string[m.end(1):m.end(1) + 1]
            tail = mark_english_questions(tail)
            if trailing != '.' or tail.rstrip().endswith(('.', '!', '?')):
                trailing = ''
            return f"{head.rstrip()}? {tail}{trailing}"
        return f"{body.rstrip()}?"

    return _EN_QUESTION_RE.sub(_sub, text)


def mark_cantonese_questions(text):
    """Turn a sentence-final 。/. into ？ when the clause ends unambiguously
    interrogatively. Length-preserving per sentence — one character is swapped,
    never added or removed, so this cannot lose content."""
    out = []
    for sent in SENT_SPLIT_RE.split(text):
        stripped = sent.rstrip()
        if stripped.endswith(("。", ".")) and (
                _Q_TAIL_RE.search(stripped) or _Q_NE_RE.search(stripped)):
            tail = sent[len(stripped):]
            sent = stripped[:-1] + "？" + tail
        out.append(sent)
    return "".join(out)


def _apply_structure(sentences, plan):
    """Turn the classification JSON into final text — mechanically, losslessly."""
    n = len(sentences)
    qs = {i for i in plan.get("q", []) if isinstance(i, int) and 1 <= i <= n}
    paras = {i for i in plan.get("para", []) if isinstance(i, int) and 1 < i <= n}
    bullets = bool(plan.get("bullets")) and len(paras) >= 2
    fixes = plan.get("fix", {}) or {}

    out_sents = list(sentences)
    for key, replacement in fixes.items():
        try:
            i = int(key)
        except (TypeError, ValueError):
            continue
        if not (1 <= i <= n) or not isinstance(replacement, str):
            continue
        original = out_sents[i - 1]
        fixed = _safe_fix(original, replacement)
        detail = f"sentence={i} original_len={len(original)} candidate_len={len(replacement)}"
        if fixed:
            _log_content_change("self-correction-fix", "applied", detail)
            out_sents[i - 1] = fixed
        else:
            _log_content_change("self-correction-fix", "rejected", detail)
    for i in qs:
        s = out_sents[i - 1]
        if not s.endswith(("。", ".")):
            continue


        mark = "？" if s.endswith("。") else "?"
        out_sents[i - 1] = s[:-1] + mark


    groups, cur = [], []
    for idx, s in enumerate(out_sents, start=1):
        if idx in paras and cur:
            groups.append(cur)
            cur = []
        cur.append(s)
    if cur:
        groups.append(cur)

    if bullets and len(groups) >= 3:
        return "\n".join("- " + join_sentences(g) for g in groups)
    return "\n\n".join(join_sentences(g) for g in groups)


def _full_rewrite(text, terms, style_subs, timeout=TIMEOUT, cancellable=False,
                  model=None, hard_buffer=HARD_TIMEOUT_BUFFER):
    """Full qwen rewrite with every guard (no invented Han, no dropped English,
    length-loss floor). Returns cleaned final text, or None if no candidate
    survived — the caller then falls back to a lossless path.

    cancellable=True registers the ollama call so a STOP (cancel_inflight) can
    abandon it mid-generation; the abort surfaces as one of the caught errors,
    so the caller falls through to the fast classify path.

    model selects which ollama model runs this rewrite (hybrid routing — see
    FAST_MODEL); None means MODEL. The guards below are model-agnostic, so a
    rougher fast model is policed identically to the quality model."""
    out = ""
    for _ in range(2):
        try:
            candidate, timed_out = run_with_hard_timeout(
                llm_polish, timeout + hard_buffer, text, timeout=timeout,
                cancellable=cancellable, model=model)
            if timed_out:



                return None
        except Exception:




            return None
        if (candidate
                and not novel_cjk(text, candidate)
                and not dropped_latin(text, candidate)):
            out = candidate
            break
    if not out:
        return None
    if len(out) > max(60, len(text) * 2.5):
        return None
    if len(text) > 40 and len(out) < len(text) * 0.55:
        return None
    return fix_spacing(apply_style(apply_dictionary(out, terms), style_subs))


def polish(text, use_llm=True, full=False, cancellable=False,
           audio_duration_seconds=None, tail_samples=None, tail_rate=None,
           should_abort=None):
    """Return (final_text, used_llm) — see _polish() for the real logic.

    should_abort (optional): a zero-arg predicate polled at the two points
    where this function is about to spend LLM time. When it returns True the
    call gives up IMMEDIATELY and returns the deterministic punctuated text
    (lossless — D1 is untouched, only the LLM upgrade is forfeited). Default
    None means "never abort", i.e. byte-identical to every existing caller.

    Added 2026-08-21 for the deferred segment-rewrite drain (server.py's
    drain_deferred_rewrites): cancel_inflight() aborts the ollama call that
    is in flight AT THAT INSTANT, but a cancelled full rewrite then falls
    through to the classify fallback below and starts a SECOND generation
    that nothing cancels — up to FG_STRUCT_TIMEOUT+FG_HARD_BUFFER = 5.3s of
    GPU sitting directly on top of Tinho's key release. This predicate is
    what makes a capture-yield actually stop the work instead of merely
    restarting it one call later (D25: capture outranks quality catch-up).

    Wraps _polish() so every return path (fast, full-rewrite, classify, or a
    guard falling back to the punctuated text) gets the deterministic
    English-punctuation fix as the last step before the text is handed back
    for paste.

    audio_duration_seconds (optional): seconds of RECORDED AUDIO this text
    came from, if the caller knows it — see SHORT_AUDIO_SKIP_SECONDS above.
    None (the default) means "unknown/not applicable" and never triggers the
    duration gate, so every existing caller that doesn't pass this is
    byte-identical to before.

    tail_samples/tail_rate: ACCEPTED BUT UNUSED (2026-07-26). These used to
    feed strip_trailing_filler()'s VAD gate; that mechanism was removed
    entirely the same day it landed — see the "TRIED AND REMOVED" comment
    near the top of this file (where FILLER_TOKENS used to live) and the PR
    body of its removal for the full reasoning: an unmeasured, unbounded
    content-loss risk cannot be justified by a one-keystroke convenience
    benefit under D1. Kept as accepted-but-ignored parameters ONLY because
    server.py's transcribe() (owned by another worker this round, out of
    file-ownership scope to edit here) still passes them at its two call
    sites — removing them from this signature would raise a live TypeError
    on every dictation. Safe to delete once whoever owns server.py cleans up
    those two call sites; nothing here reads them."""
    out, used_llm = _polish(text, use_llm=use_llm, full=full, cancellable=cancellable,
                            audio_duration_seconds=audio_duration_seconds,
                            should_abort=should_abort)
    out = fix_english_punctuation(out)
    log_quarantine_hits_async(out)
    return out, used_llm









QUARANTINE_HITS_PATH = os.path.join(HERE, ".quarantine_hits.jsonl")


def load_quarantine():
    try:
        with open(DICT_PATH, encoding="utf-8") as fh:
            q = json.load(fh).get("quarantine", [])
        return q if isinstance(q, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def log_quarantine_hits(text, quarantine=None):
    """Scan the final text for would-be hits of quarantined rules and append
    them to QUARANTINE_HITS_PATH. Returns the hits (for tests)."""
    if not text:
        return []
    entries = load_quarantine() if quarantine is None else quarantine
    if not entries:
        return []
    hits = []
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    for e in entries:
        if not isinstance(e, dict):
            continue
        pat = e.get("variant") if e.get("kind") == "term" else e.get("pattern")
        if not pat:
            continue
        if e.get("kind") == "term" and re.match(r"^[A-Za-z\\]", pat):
            probe = r"\b" + pat + (r"\b" if re.search(r"[A-Za-z]$", pat) else "")
        else:
            probe = pat
        try:
            for m in re.finditer(probe, text, re.IGNORECASE):
                lo = max(0, m.start() - 25)
                hits.append({
                    "time": now, "kind": e.get("kind"),
                    "key": pat, "term": e.get("term") or e.get("replace"),
                    "match": m.group()[:60],
                    "context": text[lo:m.end() + 25].replace("\n", " ")})
        except re.error:
            continue
    if hits:
        try:
            with open(QUARANTINE_HITS_PATH, "a", encoding="utf-8") as fh:
                for h in hits:
                    fh.write(json.dumps(h, ensure_ascii=False) + "\n")
        except OSError:
            pass
    return hits


def log_quarantine_hits_async(text):
    try:
        threading.Thread(target=log_quarantine_hits, args=(text,),
                         daemon=True).start()
    except Exception:
        return False
    return True


def _polish(text, use_llm=True, full=False, cancellable=False,
            audio_duration_seconds=None, should_abort=None):
    """Return (final_text, used_llm).

    full=True forces the full qwen rewrite (wording + within-segment
    self-correction cleanup, not just classification) for longer input too —
    used for rolling segments, whose polish latency is hidden while the speaker
    keeps talking. Every guard still applies, so a rewrite can only ever be as
    lossy as punctuation, never a summary.

    cancellable=True (background segment full rewrites, and — since the
    STOP-race fix below — the classify fallback that can follow one) lets a
    STOP abandon the in-flight qwen call via cancel_inflight(); the aborted
    call then falls through to the lossless classify path or the punctuated
    text, so stop-to-text stays fast.

    HYBRID MODEL ROUTING (2026-07-23): full=True is the background rolling-
    segment path (latency hidden while the speaker keeps talking) → quality
    MODEL. full=False is every FOREGROUND path Tinho actually waits on (the
    tail at STOP, a short single-take, the long-text classify pass) →
    FAST_MODEL. When DICTATION_LLM_FAST is unset FAST_MODEL == MODEL, so this
    is a no-op until the hybrid is switched on. See FAST_MODEL's comment.

    STOP-RACE FALLBACK FIX (2026-07-23, long-recording ≤3s priority): a
    full=True background segment whose full-rewrite attempt fails/times
    out/gets cancelled used to fall through to the classify path still
    carrying the GENEROUS background budget (STRUCT_TIMEOUT/HARD_TIMEOUT_BUFFER)
    purely because full=True — even though server.py's transcribe() blocks
    synchronously on exactly this segment when STOP races it (measured
    residual: "STOP fired at 2s -> text settled in 6.6s", already over the 3s
    target, and that was the fast case). Once a full-rewrite has genuinely been
    attempted and failed in THIS call, the remaining classify attempt now
    always uses the tight FG budget + FAST_MODEL + is cancellable, regardless
    of `full` — see the comment above `fg_for_struct` below.

    audio_duration_seconds (2026-07-23, short-audio LLM skip): optional
    seconds-of-recording, if the caller knows it. See SHORT_AUDIO_SKIP_SECONDS
    above for the gate this feeds and its accepted tradeoff.
    """
    if not text or not (CJK_RE.search(text) or LATIN_RE.search(text)):
        return "", False


    model = MODEL if full else FAST_MODEL



    fg = not full
    terms = load_dictionary()


















    text = join_spelled_out_letters(text, terms, mark_spans=True)
    text = apply_dictionary(text, terms)
    style_subs, _ = load_style()
    text = apply_style(text, style_subs)
    text = convert_cn_percent_numerals(text)
    text = convert_cn_numerals_general(text)



    try:
        punctuated = punctuate(text)
    except Exception:
        punctuated = text





    punctuated = apply_terminal_punctuation_for_short_answers(punctuated)
    punctuated = repair_midword_sentence_cuts(punctuated)
    punctuated = fix_comma_before_new_sentence(punctuated, terms)
    if _english_ratio(punctuated) >= PUNCT_ENGLISH_SKIP_RATIO:
        punctuated = drop_unbelieved_english_stops(punctuated)
        punctuated = repair_false_stop_before_negated_auxiliary(punctuated)
        punctuated = repair_seam_fused_words(punctuated)
    punctuated = fix_spoken_decimals(punctuated)
    punctuated = mark_cantonese_questions(punctuated)
    if _english_ratio(punctuated) >= PUNCT_ENGLISH_SKIP_RATIO:
        punctuated = mark_english_questions(punctuated)
    punctuated = restore_english_contractions(punctuated)
    punctuated = strip_hallucinated_trailing_interjection(
        punctuated, log_fn=_log_content_change and (
            lambda msg: _log_content_change('trailing-filler', 'applied', msg)))

    if not use_llm:
        return punctuated, False



    if len(text.strip()) <= ULTRA_SHORT_CHARS:
        return punctuated, False








    if (SHORT_AUDIO_SKIP_SECONDS > 0
            and audio_duration_seconds is not None
            and audio_duration_seconds < SHORT_AUDIO_SKIP_SECONDS):
        return punctuated, False





    if should_abort is not None and should_abort():
        return punctuated, False





    WARM_GATE.wait(timeout=WARM_WAIT_MAX)

    short = len(text) <= SHORT_CHARS






    attempted_full = False
    if (short or full) and len(text) <= FULL_MAX_CHARS:




        if fg:
            rewrite_timeout, hard_buffer = FG_TIMEOUT, FG_HARD_BUFFER
        else:
            rewrite_timeout = TIMEOUT if short else SEGMENT_TIMEOUT
            hard_buffer = HARD_TIMEOUT_BUFFER
        out = _full_rewrite(text, terms, style_subs,
                            timeout=rewrite_timeout, hard_buffer=hard_buffer,
                            cancellable=cancellable, model=model)
        if out is not None:
            return out, True
        if short:
            return punctuated, False
        attempted_full = True













        if should_abort is not None and should_abort():
            return punctuated, False





    if len(text) > 2800:
        return punctuated, False
    sentences = split_sentences(punctuated)
    if not sentences:
        return punctuated, False




































    fg_for_struct = fg or attempted_full
    struct_timeout = FG_STRUCT_TIMEOUT if fg_for_struct else STRUCT_TIMEOUT
    struct_hard = struct_timeout + (FG_HARD_BUFFER if fg_for_struct else HARD_TIMEOUT_BUFFER)
    struct_model = FAST_MODEL if fg_for_struct else model
    try:
        plan, timed_out = run_with_hard_timeout(
            llm_structure, struct_hard, sentences,
            model=struct_model, timeout=struct_timeout,
            cancellable=cancellable or attempted_full)
        if timed_out:
            return punctuated, False
        return fix_spacing(_apply_structure(sentences, plan)), True
    except Exception:
        return punctuated, False









_SPELL_SEQ_RE = re.compile(
    r"(?<![A-Za-z])[A-Za-z](?:[\s\-.．·]+[A-Za-z]){2,}(?![A-Za-z])")
_SPELL_CUE_RE = re.compile(r"(?:我)?串(?:俾你聽|你聽|俾你|法係|法|出嚟|字)?[，,、。.\s]*$")
_SPELL_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*|[一-鿿]+")


def detect_spellouts(text):
    """Return [(preceding_token, assembled_word), ...] for each spelled-out run
    whose immediately-preceding content token differs from the assembled word.
    Empty when there is nothing to learn (no spell-out, or already correct)."""
    if text is None:
        return []
    caps = []
    for m in _SPELL_SEQ_RE.finditer(text):
        letters = re.findall(r"[A-Za-z]", m.group())
        if len(letters) < 3:
            continue
        assembled = "".join(letters)
        prefix = text[:m.start()]


        while True:
            new = _SPELL_CUE_RE.sub("", prefix)
            new = re.sub(r"(?i)spell(?:ed|\s+it)?(?:\s+out)?[\s,，、]*$", "", new)
            new = new.rstrip(" ，,、.。\t")
            if new == prefix:
                break
            prefix = new
        toks = _SPELL_TOKEN_RE.findall(prefix)
        if not toks:
            continue
        token = toks[-1]
        if token.lower() == assembled.lower():
            continue
        caps.append((token, assembled))
    return caps


def self_test():
    """Quick deterministic smoke test for the module-level helpers."""
    assert detect_spellouts(None) == []
    assert detect_spellouts("") == []
    assert load_prompt_notes() is not None
    return True


if __name__ == "__main__":
    import sys
    import time
    raw = " ".join(sys.argv[1:]) or sys.stdin.read()
    t0 = time.time()
    result, used = polish(raw)
    print(f"[llm={used} {time.time()-t0:.1f}s]\n{result}")

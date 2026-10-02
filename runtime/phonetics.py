#!/usr/bin/env python3
"""Language-agnostic phonetic-similarity interface (2026-07-26).

WHY THIS EXISTS
----------------
review_gate.py's mishear/rephrase classifier used to be a single local-qwen
call with no notion of how the two sides of a correction actually SOUND.
Tinho's own design ruling: pronunciation is the discriminator. Same/near-same
sound between "wrong" and "right" means the ASR misheard something real
(worth learning); different sound means Tinho spoke a different word or
changed his mind (must never become a global substitution rule). Observed
proof this matters: "coat" -> "code" was classified "rephrase" (wrong -- these
are near-homophones and it's a genuine mishear) while "you" -> "i" correctly
classified "rephrase" (unrelated sounds) purely by chance, because nothing in
the old pipeline ever looked at pronunciation at all.

THE INTERFACE (the architecture decision Tinho asked for)
-----------------------------------------------------------
    phonetic_key(word, lang)        -> str
    key_distance(key_a, key_b)      -> float  (0 = identical, unbounded up)
    key_similarity(key_a, key_b)    -> float in [0, 1]  (1 = identical sound)

Adding a language means adding ONE mapping function and registering it in
_KEY_FUNCS below -- the distance/similarity logic never changes. Two mappings
ship today:

  - "en"  : a compact, pure-python, single-Metaphone-family consonant-skeleton
            algorithm (silent-letter handling, common digraphs, vowel
            dropping). Not the full Double Metaphone spec -- a deliberately
            small subset sufized for near-homophone detection in short
            dictation corrections, not a general spell-checker.
  - "yue" : a Cantonese/Chinese jyutping-romanisation key, via a hand-curated
            table of common characters (_JYUTPING, below). A character absent
            from the table has NO knowable pronunciation here, so it maps to
            an opaque, self-only symbol (see _jyutping_key) -- two unknown
            characters are always treated as phonetically UNRELATED. This is
            a deliberate conservative default: guessing a pronunciation we
            don't have data for is exactly the "learns the wrong thing" risk
            this whole system exists to kill. Extending Cantonese coverage is
            pure data entry (grow _JYUTPING) -- never a logic change.

LANGUAGE DETECTION IS NOT REAL LANGID
--------------------------------------
detect_script() is a Unicode-range check: does the token contain a CJK
ideograph, or Latin letters? That's it. It is NOT language identification
(it can't tell Cantonese from Mandarin from Hakka, and it would call
"Tailscale" written in Latin script "en" even though it's a product name).
It is exactly enough to route a token to the right key function, which is
all phonetic_key() needs. A cross-script pair (Chinese wrong -> English
right, or vice versa) has no meaningful single-language phonetic comparison
here, so is_phonetically_close() returns None (undecided) for those instead
of guessing -- the caller falls back to its other evidence (see
review_gate.classify_correction_combined).

No dependency added. Pure stdlib (re, unicodedata not even needed). If a
real Cantonese-romanisation library is ever added (e.g. a jyutping/pinyin
package), it drops in as one more branch in _KEY_FUNCS with zero change to
the distance/similarity code -- that swap-in-a-mapping property is the whole
point of the interface.
"""
import re



_HAN_RE = re.compile(r"[㐀-鿿豈-﫿]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def detect_script(word):
    """"han" / "latin" / "other". A Unicode code-point range check, nothing
    more -- see module docstring for why that's deliberately all it is."""
    word = word or ""
    if _HAN_RE.search(word):
        return "han"
    if _LATIN_RE.search(word):
        return "latin"
    return "other"


_SCRIPT_TO_LANG = {"han": "yue", "latin": "en"}


def _resolve_lang(word, lang):
    if lang:
        return lang
    return _SCRIPT_TO_LANG.get(detect_script(word))









_INITIAL_SILENT = (
    ("kn", "n"), ("gn", "n"), ("pn", "n"), ("wr", "r"), ("wh", "w"),
)
_DIGRAPHS = (
    ("tion", "shn"), ("sion", "shn"), ("ph", "f"), ("sh", "x"), ("ch", "x"),
    ("th", "0"), ("qu", "kw"), ("ck", "k"), ("gh", ""),
)


def _dedupe(s):
    out = []
    for c in s:
        if not out or out[-1] != c:
            out.append(c)
    return "".join(out)


def _metaphone_key(word):
    s = re.sub(r"[^a-z]", "", (word or "").lower())
    if not s:
        return ""
    s = _dedupe(s)
    for pat, rep in _INITIAL_SILENT:
        if s.startswith(pat):
            s = rep + s[len(pat):]
            break
    for pat, rep in _DIGRAPHS:
        s = s.replace(pat, rep)
    vowels = set("aeiouy")
    out = []
    for i, c in enumerate(s):
        if i == 0:


            out.append(c.upper())
            continue
        if c in vowels:
            continue
        if c == "h":
            continue
        m = {"c": "k", "g": "k", "s": "s", "z": "s", "v": "f", "x": "ks",
             "j": "j", "q": "k"}
        out.append(m.get(c, c).upper())
    return _dedupe("".join(out))






_JYUTPING = {
    "你": "nei5", "我": "ngo5", "佢": "keoi5", "哋": "dei6", "係": "hai6",
    "唔": "m4", "咩": "me1", "嘢": "je5", "呢": "ni1", "嗰": "go2",
    "個": "go3", "喺": "hai2", "咗": "zo2", "㗎": "gaa3", "架": "gaa3",
    "嘅": "ge3", "同": "tung4", "就": "zau6", "都": "dou1", "要": "jiu3",
    "好": "hou2", "冇": "mou5", "乜": "mat1", "點": "dim2", "樣": "joeng6",
    "再": "zoi3", "先": "sin1", "至": "zi3", "得": "dak1", "過": "gwo3",
    "會": "wui5", "可": "ho2", "用": "jung6", "家": "gaa1", "們": "mun4",
    "他": "taa1", "她": "taa1", "它": "taa1", "把": "baa2", "讓": "joeng6",
    "給": "kap1", "對": "deoi3", "想": "soeng2", "知": "zi1", "之": "zi1",
    "是": "si6", "十": "sap6", "世": "sai3", "細": "sai3", "四": "sei3",
    "試": "si3", "時": "si4", "事": "si6", "史": "si2", "師": "si1",
    "詩": "si1", "屎": "si2", "死": "sei2", "洗": "sai2", "駛": "sai2",
    "使": "sai2", "西": "sai1", "大": "daai6", "太": "taai3", "快": "faai3",
    "慢": "maan6", "一": "jat1", "二": "ji6", "三": "saam1", "五": "ng5",
    "六": "luk6", "七": "cat1", "八": "baat3", "九": "gau2", "教": "gaau3",
    "育": "juk6", "交": "gaau1", "學": "hok6", "校": "haau6",
    "語": "jyu5", "言": "jin4", "文": "man4", "字": "zi6", "件": "gin6",
    "自": "zi6", "現": "jin6", "在": "zoi6", "今": "gam1", "日": "jat6",
    "明": "ming4", "聽": "teng1", "工": "gung1", "作": "zok3", "服": "fuk6",
    "務": "mou6", "資": "zi1", "料": "liu2", "訊": "seon3", "息": "sik1",
    "信": "seon3", "儲": "cyu5", "存": "cyun4", "空": "hung1", "間": "gaan1",
    "位": "wai6", "置": "zi3", "地": "dei6", "方": "fong1", "式": "sik1",
    "法": "faat3", "內": "noi6", "容": "jung4", "電": "din6", "腦": "nou5",
    "話": "waa6", "網": "mong5", "上": "soeng5", "標": "biu1", "點": "dim2",
    "符": "fu4", "號": "hou6", "句": "geoi3", "子": "zi2", "段": "dyun6",
    "落": "lok6", "格": "gaak3", "式二": "sik1", "規": "kwai1", "則": "zak1",
    "典": "din2", "錄": "luk6", "音": "jam1", "用": "jung6", "戶": "wu6",
    "有": "jau5", "關": "gwaan1", "於": "jyu1", "然": "jin4", "後": "hau6",
    "如": "jyu4", "果": "gwo2", "但": "daan6", "因": "jan1", "為": "wai4",
    "所": "so2", "以": "ji5", "可以": "ho2ji5", "點樣": "dim2joeng6",
    "而": "ji4", "已": "ji5", "經": "ging1", "仲": "zung6", "另": "ling6",
    "外": "ngoi6", "例": "lai6", "如": "jyu4", "即": "zik1", "真": "zan1",
    "似": "ci5", "覺": "gok3", "得": "dak1", "認": "jing6", "為": "wai4",
    "希": "hei1", "望": "mong6", "需": "seoi1", "應": "jing1", "該": "goi1",
    "定": "ding6", "直": "zik6", "接": "zip3", "動": "dung6", "始": "ci2",
    "完": "jyun4", "成": "sing4", "繼": "gai3", "續": "zuk6", "刪": "saan1",
    "除": "ceoi4", "清": "cing1", "理": "lei5", "改": "goi2", "善": "sin6",
    "更": "gang3", "新": "san1", "設": "cit3", "安": "on1", "裝": "zong1",
    "使": "sai2", "幫": "bong1", "手": "sau2", "助": "zo6", "意": "ji3",
    "思": "si1", "見": "gin3", "決": "kyut3", "選": "syun2", "擇": "zaak6",
    "重": "zung6", "要": "jiu3", "簡": "gaan2", "單": "daan1", "複": "fuk1",
    "雜": "zaap6", "容": "jung4", "易": "ji6", "困": "kwan3", "難": "naan4",
}


def _jyutping_key(word):
    han = [c for c in (word or "") if _HAN_RE.match(c)]
    if not han:
        return ""
    parts = []
    for c in han:
        syl = _JYUTPING.get(c)





        parts.append(syl if syl is not None else c)
    return "".join(parts)


_KEY_FUNCS = {"en": _metaphone_key, "yue": _jyutping_key}


def phonetic_key(word, lang=None):
    """The pluggable interface: word + language -> a comparable string key.
    `lang` picked from script when not given (see _resolve_lang / detect_script).
    Adding a language = adding one function to _KEY_FUNCS; the rest of this
    module (distance/similarity) never has to change."""
    resolved = _resolve_lang(word, lang)
    fn = _KEY_FUNCS.get(resolved)
    if fn is None:
        return (word or "").strip().lower()
    return fn(word)


def _levenshtein(a, b):
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[-1]


def key_distance(key_a, key_b):
    """Raw Levenshtein edit distance between two phonetic keys."""
    return _levenshtein(key_a or "", key_b or "")


def key_similarity(key_a, key_b):
    """1.0 = identical sound, 0.0 = maximally different, given the two keys'
    own lengths (normalised edit distance). Empty-vs-empty is defined as 1.0
    (nothing to compare); empty-vs-nonempty is 0.0."""
    a, b = key_a or "", key_b or ""
    if not a and not b:
        return 1.0
    dist = _levenshtein(a, b)
    return 1.0 - dist / max(len(a), len(b))


DEFAULT_THRESHOLD = 0.5


def is_phonetically_close(word_a, word_b, lang=None, threshold=DEFAULT_THRESHOLD):
    """True/False/None. None means "can't compare" -- either input is blank,
    or the two words are in different detected scripts (a Chinese wrong ->
    English right pair has no single-language phonetic comparison here; the
    caller should fall back to its other evidence rather than get a guess
    dressed up as a phonetic verdict)."""
    word_a, word_b = (word_a or "").strip(), (word_b or "").strip()
    if not word_a or not word_b:
        return None
    if lang is None:
        sa, sb = detect_script(word_a), detect_script(word_b)
        if sa != sb and "other" not in (sa, sb):
            return None
    ka, kb = phonetic_key(word_a, lang), phonetic_key(word_b, lang)
    return key_similarity(ka, kb) >= threshold

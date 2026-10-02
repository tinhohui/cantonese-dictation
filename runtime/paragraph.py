#!/usr/bin/env python3
"""Content-driven paragraph breaking.

The defect this replaces: server.py used to build the final text as
``"\\n\\n".join(segment_texts)`` — every ~15s AUDIO CUT became a paragraph
break, regardless of whether the sentence at that point had even finished.
Tinho's complaint: short, related sentences scattered across their own
paragraphs, and sentences that clearly belong together got split apart —
including the killer case of pausing mid-sentence to think of a word.

Ruling (binding, see the PR body / commit message for the full quote):
  - an audio cut must NEVER create a paragraph — silence/cut timing decides
    only where audio is chunked for ASR/rewrite, never where a paragraph
    starts;
  - length may only ever be a last-resort TIE-BREAKER, never the rule
    itself ("using length to impersonate content") — this module does not
    implement a length-based break at all, see the module docstring's
    "ceiling" section below;
  - enumerations (第一點/第二點/第三點, 首先/其次/最後, 一、二、三...) must
    get their own paragraph even when each item is a single short sentence;
  - a sentence that is clearly unfinished (ends on 然後/但係/所以/而, or has
    no terminal punctuation at all) must never be split from what follows.

2026-07-26 CORRECTION — topic-shift connectives (另外/講返/至於/總之)
REMOVED from the signal set. They shipped initially and were pulled the
same night: Tinho reported (his words) 「有時我在同一個句子裏面說『另外』，
然後他也會分段了」— 另外 is also an ordinary mid-sentence discourse
particle, not a reliable topic-shift marker, so any list built on words
like this has the same failure mode by construction. Real replay against
~/dictation/history.jsonl at the time of removal: 181 breaks were coming
from this signal (175 另外, 1 至於, 1 總之, 0 講返— 講返 never once
appeared as a paragraph-starting token in the whole corpus). Text-level
inspection of a large sample read as mostly legitimate topic changes, but
that read cannot be trusted over Tinho's own live-heard doctrine, and the
COST IS ASYMMETRIC — Tinho's own framing: a missing break costs one Enter
keystroke; a wrong break costs re-reading scattered text and rejoining it
by hand, and he had already reported this exact failure three times in one
night before asking for the fix. Under that weighting, losing many
"probably-good" breaks to guarantee zero more of the reported bad ones is
the correct trade even though it loses more breaks in raw count than it
saves. See the "HONEST CEILING" section below — this is UNDER-segmentation
by deliberate design, not a temporary gap to "improve" back into a bigger
connective list in a future round.

paragraph_break() is the only entry point. It is a pure function of TEXT —
it does not take, and must never be given, any audio-cut/segment-boundary
information. Feed it the fully assembled take (segments joined by anything —
whitespace is normalised away before any decision is made) and it returns
the same text with paragraph breaks placed purely from the lexical signals
above.

D1 (zero content loss): paragraph_break() only ever inserts/removes
WHITESPACE between sentence units. It never adds, drops, or reorders a
single non-whitespace character. See test_paragraph.py's
test_d1_property_real_history for the property test asserting this over a
real sample of Tinho's dictation history.

THE HONEST CEILING (read this before trusting the output blindly): the only
remaining signal is enumeration — a long run-on monologue that drifts from
one sub-topic to another WITHOUT ever using an enumeration marker will
come out as ONE paragraph, however long, EVEN WHEN a human reader would
clearly see several topics in it. This is now the dominant failure mode of
this module (after the 2026-07-26 removal above), not a residual edge case
— entire multi-topic monologues will under-segment by design. Real topic
segmentation needs semantic understanding (an LLM call, or at minimum
embeddings) that this deterministic module deliberately does not attempt —
it must stay free, instant, and available even when the LLM path is
skipped or times out. This is a DELIBERATE engineering choice, not a gap
to quietly patch by growing the connective list again: any list of "words
that start a paragraph" fails the same way 另外 did, because discourse
particles are not reliable topic-shift markers in speech. The honest
answer to "under-segmentation happens" is "yes, and that is the safe
direction" — over-segmentation is the one this module must never do again.
"""

import re






_SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?])")





_WS_RE = re.compile(r"\s+")


def split_units(text):
    """Split TEXT into non-empty sentence units, discarding only whitespace.

    Every non-whitespace character in `text` appears in exactly one returned
    unit, in order — this is the invariant the D1 property test checks.
    """
    if not text:
        return []
    collapsed = _WS_RE.sub(" ", text).strip()
    if not collapsed:
        return []
    parts = _SENT_SPLIT_RE.split(collapsed)
    return [p.strip() for p in parts if p.strip()]















_ENUM_PATTERNS = [
    re.compile(r"^第[一二三四五六七八九十百千萬0-9]{1,4}"),
    re.compile(r"^首先"),
    re.compile(r"^其次"),
    re.compile(r"^再者"),
    re.compile(r"^最後"),
    re.compile(r"^最后"),
    re.compile(r"^最終"),
    re.compile(r"^最终"),
    re.compile(r"^[一二三四五六七八九十]+[、.．]"),
    re.compile(r"^[0-9]+[、.．)）]"),
]














_UNFINISHED_CONNECTORS = ("然後", "然后", "但係", "但系", "但是", "所以", "而")

_TERMINAL_PUNCT = "。！？!?"







_LEADING_MARKER_RE = re.compile(r"^[-•]\s*")


def _starts_with_any(unit, patterns):
    probe = _LEADING_MARKER_RE.sub("", unit, count=1)
    return any(p.match(probe) for p in patterns)


def _is_unfinished(unit):
    """True if `unit` is a fragment that must stay joined to what follows."""
    body = unit.rstrip()
    if not body:
        return True
    if body[-1] not in _TERMINAL_PUNCT:
        return True
    core = body[:-1]
    return any(core.endswith(c) for c in _UNFINISHED_CONNECTORS)


def paragraph_break(text):
    """Return `text` with paragraph breaks placed purely from content.

    No audio-cut/segment-boundary input exists in this function's signature
    by design — see the module docstring. Only whitespace between sentence
    units is ever changed; every non-whitespace character of `text` survives
    in the same order (D1).
    """
    units = split_units(text)
    if not units:
        return text if text is not None else text
    if len(units) == 1:
        return units[0]

    out = [units[0]]
    for i in range(1, len(units)):
        prev, cur = units[i - 1], units[i]
        if _is_unfinished(prev):
            sep = " "
        elif _starts_with_any(cur, _ENUM_PATTERNS):
            sep = "\n\n"
        else:
            sep = " "
        out.append(sep)
        out.append(cur)
    return "".join(out)

#!/usr/bin/env python3
"""Fail-closed output gate for final dictation text.

Tinho's dictation output may contain Traditional Chinese Han characters,
ASCII/Latin English, whitespace, and ordinary punctuation only. Anything
else is a visible failure: log it and strip it, keeping every word around
it. Only a take with nothing allowed left in it becomes a marker.
"""
import unicodedata

BLOCKED_MARKER = "[LANGUAGE-GATE BLOCKED]"


def _is_han(ch):
    name = unicodedata.name(ch, "")
    return "CJK UNIFIED IDEOGRAPH" in name or "CJK COMPATIBILITY IDEOGRAPH" in name


def _is_allowed_char(ch):
    if ch.isspace():
        return True
    codepoint = ord(ch)
    if codepoint < 128:
        return True
    if unicodedata.category(ch).startswith("P"):
        return True
    return _is_han(ch)


def first_disallowed_char(text):
    """Return the first disallowed character and a short description.

    Returns None when the text is clean.
    """
    if not text:
        return None
    for ch in text:
        if not _is_allowed_char(ch):
            return ch, f"U+{ord(ch):04X} {unicodedata.name(ch, 'UNKNOWN')}"
    return None


def strip_disallowed(text):
    """Drop only the offending characters, keeping every allowed one."""
    return "".join(ch for ch in text if _is_allowed_char(ch))


def enforce(text, log=None):
    """Return (output_text, tripped, reason).

    Clean text passes through unchanged. A polluted take has ONLY the
    offending characters removed — the rest of Tinho's words survive.

    IT USED TO REPLACE THE WHOLE TAKE with a marker string, and on 2026-08-09
    that destroyed real dictation: the `auto` recogniser reached for Japanese
    kana mid-Cantonese, one だ tripped this gate, and everything he had just
    said was replaced by
    "[LANGUAGE-GATE BLOCKED] output-language-gate: blocked disallowed
    character U+3060 HIRAGANA LETTER DA". He had to re-speak it, twice.

    That behaviour is a direct violation of D1 (zero content loss), and the
    reasoning behind it does not survive contact with the failure: the gate
    exists so polluted output is never committed SILENTLY, and stripping is
    not silent — it still logs, still returns tripped=True, and the caller
    still records the trip. Deleting the words was never what made it visible.

    A take that is ENTIRELY disallowed still yields the marker: there is no
    content to save, and an empty paste would be the silent failure this gate
    was built to prevent."""
    disallowed = first_disallowed_char(text)
    if disallowed is None:
        return text, False, None
    ch, desc = disallowed
    reason = f"output-language-gate: stripped disallowed character {desc}"
    cleaned = strip_disallowed(text)
    if not cleaned.strip():
        reason = f"output-language-gate: blocked disallowed character {desc}"
        if log is not None:
            log(reason)
        return f"{BLOCKED_MARKER} {reason}", True, reason
    if log is not None:
        log(f"{reason}; kept {len(cleaned)}/{len(text)} chars")
    return cleaned, True, reason

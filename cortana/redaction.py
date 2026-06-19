"""Precision redaction of high-confidence secrets before they enter memory.

Tuned for **few false positives** — a journal redacted into Swiss cheese is useless
(docs/DESIGN.md Decision 2). We match only unambiguous secret shapes and Luhn-verify
card numbers; benign text is left intact. Accept some false negatives.
"""

from __future__ import annotations

import re
from dataclasses import replace

from cortana.perception import Observation

# (pattern, replacement) for unambiguous secret shapes.
_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
                re.DOTALL), "[REDACTED PRIVATE KEY]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED AWS KEY]"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "[REDACTED API KEY]"),
    (re.compile(r"\bghp_[A-Za-z0-9]{36}\b"), "[REDACTED TOKEN]"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{20,}\b"), "[REDACTED TOKEN]"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED SSN]"),
]

# A run of 13–19 digits, optionally grouped by spaces/dashes (card-number shape).
_CARD_RE = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")


def _luhn_ok(digits: str) -> bool:
    """Luhn checksum — gates card redaction so arbitrary long numbers aren't hit."""
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = ord(ch) - 48
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _redact_cards(text: str) -> str:
    def repl(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return "[REDACTED CARD]"
        return m.group()
    return _CARD_RE.sub(repl, text)


def redact(text: str) -> str:
    """Return ``text`` with high-confidence secrets replaced by markers."""
    if not text:
        return text
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return _redact_cards(text)


def redact_observation(obs: Observation) -> Observation:
    """Redact the free-text fields of an observation before it is stored/summarized."""
    return replace(obs, ocr_text=redact(obs.ocr_text), focused_text=redact(obs.focused_text))

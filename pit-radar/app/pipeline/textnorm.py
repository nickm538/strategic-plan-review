"""Text normalization shared by the prefilter, verifier and dedupe (docs/05 §5).

`norm_for_match` is the ONE function used on both sides of every substring check, so a quote and the
post text are always compared in the same form.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

_QUOTES = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-", "−": "-",
    " ": " ", " ": " ", " ": " ", " ": " ",
    "​": "", "‌": "", "‍": "", "﻿": "",
})
_WS = re.compile(r"\s+")


def clean(text: str) -> str:
    """NFKC, straight quotes, unified dashes, zero-width chars removed, whitespace collapsed. Case kept."""
    t = unicodedata.normalize("NFKC", text or "").translate(_QUOTES)
    return _WS.sub(" ", t).strip()


def norm_for_match(text: str) -> str:
    """clean() + casefold. Use on BOTH sides of every substring comparison."""
    return clean(text).casefold()


def word_count(text: str) -> int:
    return len(clean(text).split())


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


_URLISH = re.compile(r"https?://\S+|\bwww\.\S+|\[link\]", re.I)
_NONWORD = re.compile(r"[^\w\s]")


def content_fingerprint(text: str) -> str:
    """sha256 of lowercased, URL-stripped, punctuation-stripped, whitespace-collapsed text (dedupe)."""
    t = _URLISH.sub(" ", norm_for_match(text))
    t = _NONWORD.sub(" ", t)
    return sha256_hex(_WS.sub(" ", t).strip())

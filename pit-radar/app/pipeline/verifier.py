"""Deterministic verifier V1-V3, V5-V8 (docs/05 §5). V4 needs the geocode and lives in decision.py.
STUB - reasoning builder."""
from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from app.pipeline.types import NormalizedItem, Verdict, VerificationResult

if TYPE_CHECKING:
    from app.pipeline.lexicon import Lexicon

TYPE_LABELS = {
    "encampment": "Encampment",
    "individual_unsheltered": "Person(s) sleeping outside",
    "vehicle_dwelling": "Vehicle dwelling",
    "self_report": "Self-report",
    "unknown": "Possible signal",
}


def template_summary(signal_type: str, place_label: str | None, time_reference: str) -> str:
    """V6 replacement: '{Type label} reported at {place label} ({time label}).'"""
    raise NotImplementedError


def verify(item: NormalizedItem, verdict: Verdict, *, lexicon: "Lexicon", now: dt.datetime) -> VerificationResult:
    """Apply V1 (verbatim quotes via textnorm.norm_for_match, <=30 words, <=2 kept, no ellipsis-joined spans),
    V2 (privacy patterns/drop -> drop + evidence_redacted; mask -> masked copy), V3 (location span),
    V5 (time), V6 (summary/decision_basis privacy + 240 chars), V7 (consistency), V8 (residence).
    After V1-V2 a relevant verdict with 0 quotes adds review reason 'no_verified_quote'."""
    raise NotImplementedError

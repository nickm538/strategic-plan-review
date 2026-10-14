"""Shared helpers for the reasoning tests (classifier, verifier, decision, red-team).

* FakeLexicon: a small stand-in for the privacy half of app.pipeline.lexicon.Lexicon (privacy_drop_matches,
  privacy_mask, privacy_any) with the same match shape and the same text contract (input already
  textnorm.clean()-ed). Verifier tests use it so they do not depend on lexicon tuning; guarded tests
  repeat the important cases with the real Lexicon.
* make_item / make_verdict: synthetic NormalizedItem and Verdict builders.
* FakeAnthropic: an object with `beta.messages.create(**kwargs)` (async) that replays scripted responses or
  raises scripted exceptions, recording every call.

All post texts here and in tests/fixtures/redteam/ are synthetic.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from app.pipeline import textnorm
from app.pipeline.types import ItemContext, NormalizedItem, Verdict

NOW = dt.datetime(2026, 10, 14, 16, 0, tzinfo=dt.timezone.utc)


# ---------------------------------------------------------------------------
# Lexicon
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FakeMatch:
    list_name: str
    entry: str
    start: int
    end: int
    matched: str


class FakeLexicon:
    """Privacy matchers with the Lexicon contract (whole words, case-insensitive unless (?-i:...))."""

    DROP: dict[str, list[str]] = {
        "privacy_denylist.drop.age": [
            r"\b\d{1,3}\s*(-|\s)?\s*(yo|y/o|years?\s+old|year-old)\b",
            r"\b(old|older|elderly|young)\s+(man|woman|guy|lady|person|people|couple)\b",
            r"\bkids?\b", r"\bteens?\b", r"\bchild(ren)?\b",
        ],
        "privacy_denylist.drop.race_ethnicity_nationality": [r"\bwhite\b", r"\bblack\b", r"\bhispanic\b"],
        "privacy_denylist.drop.immigration_status": [r"(?-i:\bICE\b)", r"\bmigrants?\b", r"\bundocumented\b"],
        "privacy_denylist.drop.appearance_and_clothing": [
            r"\bhoodie\b", r"\bjacket\b", r"\bwearing\b", r"\bbeard(ed)?\b", r"\bdirty\b",
        ],
        "privacy_denylist.drop.disability_and_health": [r"\bwheelchair\b", r"\bmentally\s+ill\b", r"\bcrazy\b"],
        "privacy_denylist.drop.substance_use": [r"\bdrunk\b", r"\bdrugs\b", r"\bhigh\s+on\b"],
        "privacy_patterns.handle": [r"(?<![\w.])@[A-Za-z0-9_.]{2,30}\b"],
        "privacy_patterns.email": [r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"],
        "privacy_patterns.phone": [r"\(?\b\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"],
        "privacy_patterns.url": [r"\bhttps?://\S+|\bwww\.\S+"],
        "privacy_patterns.street_address": [
            r"\b\d{1,5}\s+(\w+\s+){1,3}(st|street|ave|avenue|rd|road|ln|lane|dr|drive|blvd|hwy|highway)\b\.?",
        ],
        "privacy_patterns.license_plate_word": [r"\b(license\s+plate|plate\s+number|plates?)\b"],
    }
    MASK: dict[str, list[str]] = {
        "[person]": ["man", "woman", "guy", "lady", "dude"],
        "[people]": ["men", "women", "guys", "ladies"],
        "[they]": ["he", "she"],
        "[them]": ["him"],
        "[their]": ["his", "her"],
    }

    def __init__(self) -> None:
        self._drop = [(name, re.compile(p, re.IGNORECASE)) for name, pats in self.DROP.items() for p in pats]
        self._mask = [(f"privacy_denylist.mask.{tok}", re.compile(rf"(?<!\w){re.escape(w)}(?!\w)", re.IGNORECASE))
                      for tok, words in self.MASK.items() for w in words]

    @staticmethod
    def _find(text: str, pats: list[tuple[str, re.Pattern[str]]]) -> list[FakeMatch]:
        cands = [(m.start(), m.end(), i, name, pat.pattern, m.group(0))
                 for i, (name, pat) in enumerate(pats) for m in pat.finditer(text or "") if m.end() > m.start()]
        cands.sort(key=lambda c: (c[0] - c[1], c[0], c[2]))
        taken: list[tuple[int, int]] = []
        out: list[FakeMatch] = []
        for s, e, _i, name, entry, matched in cands:
            if any(s < te and ts < e for ts, te in taken):
                continue
            taken.append((s, e))
            out.append(FakeMatch(name, entry, s, e, matched))
        out.sort(key=lambda m: (m.start, m.end))
        return out

    def privacy_drop_matches(self, text: str) -> list[FakeMatch]:
        return self._find(text, self._drop)

    def privacy_mask(self, text: str) -> tuple[str, bool]:
        matches = self._find(text, self._mask)
        if not matches:
            return text, False
        parts, pos = [], 0
        for m in matches:
            parts.append(text[pos:m.start])
            parts.append(m.list_name.rsplit(".", 1)[-1])
            pos = m.end
        parts.append(text[pos:])
        return "".join(parts), True

    def privacy_any(self, text: str) -> list[FakeMatch]:
        out = self._find(text, self._mask) + self.privacy_drop_matches(text)
        out.sort(key=lambda m: (m.start, m.end))
        return out


def load_real_lexicon():
    """The real Lexicon, or pytest.skip while app.pipeline.lexicon is unfinished."""
    try:
        from app.pipeline.lexicon import Lexicon
        lex = Lexicon.load()
        lex.privacy_mask("a guy")          # exercise the methods the verifier uses
        lex.privacy_drop_matches("a guy")
        lex.privacy_any("a guy")
        return lex
    except NotImplementedError:
        pytest.skip("waiting for app.pipeline.lexicon")


# ---------------------------------------------------------------------------
# Items and verdicts
# ---------------------------------------------------------------------------
def make_item(text: str, *, published_at: Optional[dt.datetime] = None, age_hours: float = 3.0,
              platform: str = "reddit", provider: str = "socialcrawl", title: str = "",
              context: Optional[ItemContext] = None, item_hash: str = "a" * 64) -> NormalizedItem:
    """A NormalizedItem shaped like normalize.build_item output (each line textnorm.clean()-ed)."""
    clean = "\n".join(textnorm.clean(line) for line in text.split("\n"))
    pub = published_at or (NOW - dt.timedelta(hours=age_hours))
    return NormalizedItem(
        item_hash=item_hash,
        content_hash=textnorm.content_fingerprint(clean),
        provider=provider,  # type: ignore[arg-type]
        platform=platform,  # type: ignore[arg-type]
        url="https://www.reddit.com/r/longisland/comments/synthetic1/",
        platform_post_id="synthetic1",
        published_at=pub,
        date_source="platform",
        collected_at=NOW,
        text=clean,
        context=context or ItemContext(query="homeless", source_scope="li_scoped", subreddit="longisland"),
        title=title,
    )


def verdict_dict(**overrides: Any) -> dict[str, Any]:
    """A schema-valid verdict dict: a relevant, current encampment at a named place, unless overridden.
    Nested keys can be overridden with 'location__location_text' style names."""
    d: dict[str, Any] = {
        "relevance": "relevant",
        "signal_type": "encampment",
        "not_relevant_reason": "none",
        "in_long_island_counties": "yes",
        "area_basis": "names the Ronkonkoma train station",
        "time_reference": "current",
        "location": {"mentioned": True, "location_text": "the Ronkonkoma train station",
                     "location_kind": "named_place", "is_private_residence": False},
        "people_count_estimate": -1,
        "flags": {"possible_minors": False, "medical_concern": False, "weather_exposure": False,
                  "safety_concern": False},
        "evidence_quotes": [{"text": "two tents in the woods behind the Ronkonkoma train station",
                             "supports": "situation"}],
        "neutral_summary": "Post reports two tents in the woods behind the Ronkonkoma train station.",
        "decision_basis": "Post says two tents are behind the Ronkonkoma train station this week.",
        "confidence": 0.86,
    }
    for key, value in overrides.items():
        if "__" in key:
            outer, inner = key.split("__", 1)
            d[outer] = {**d[outer], inner: value}
        else:
            d[key] = value
    return d


def make_verdict(**overrides: Any) -> Verdict:
    return Verdict.model_validate(verdict_dict(**overrides))


DEFAULT_POST = ("There are two tents in the woods behind the Ronkonkoma train station this week. "
                "Saw them again this morning on my way to the platform.")


# ---------------------------------------------------------------------------
# Fake Anthropic client
# ---------------------------------------------------------------------------
def fake_message(text: Optional[str] = None, *, stop_reason: str = "end_turn", model: str = "claude-opus-5-5",
                 input_tokens: int = 900, output_tokens: int = 600, cache_read: int = 0, cache_write: int = 0,
                 stop_details: Any = None, request_id: str = "req_test_1", iterations: Optional[list] = None,
                 content: Optional[list] = None) -> SimpleNamespace:
    """A BetaMessage-shaped object (only the fields the classifier reads)."""
    if content is None:
        content = [SimpleNamespace(type="thinking", thinking="", signature="sig")]
        if text is not None:
            content.append(SimpleNamespace(type="text", text=text))
    usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens,
                            cache_read_input_tokens=cache_read, cache_creation_input_tokens=cache_write,
                            iterations=iterations)
    msg = SimpleNamespace(id="msg_test", type="message", role="assistant", content=content, model=model,
                          stop_reason=stop_reason, stop_details=stop_details, usage=usage)
    msg._request_id = request_id
    return msg


class _FakeMessages:
    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self.script:
            raise AssertionError("FakeAnthropic: no scripted response left")
        nxt = self.script.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


class FakeAnthropic:
    """client.beta.messages.create(**kwargs) replaying `script` (responses or exceptions to raise)."""

    def __init__(self, *script: Any) -> None:
        self.beta = SimpleNamespace(messages=_FakeMessages(list(script)))

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.beta.messages.calls


def api_error(cls: type, status: int, *, headers: Optional[dict[str, str]] = None, message: str = "error",
              body: Any = None) -> Exception:
    """Construct an anthropic APIStatusError subclass the way the SDK does (httpx2 response)."""
    import httpx2
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx2.Response(status, headers=headers or {}, request=req)
    return cls(message, response=resp, body=body)


def connection_error(cls: type) -> Exception:
    import httpx2
    return cls(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))


# A sanity test so this support module is also a valid test module.
def test_fake_lexicon_matches_contract_shape():
    lex = FakeLexicon()
    assert lex.privacy_mask("a guy and his dog") == ("a [person] and [their] dog", True)
    assert [m.list_name for m in lex.privacy_drop_matches("ice on the platform")] == []
    ice = lex.privacy_drop_matches("ICE was there")
    assert [m.list_name for m in ice] == ["privacy_denylist.drop.immigration_status"]
    hits = lex.privacy_drop_matches("parked at 123 Main St")
    assert hits and hits[0].list_name == "privacy_patterns.street_address"

"""Claude classifier (docs/05 §1-4, prompts/classifier-system.md).

One item per request through `client.beta.messages.create` with structured output (the JSON schema in
schemas/classification.schema.json), a cached, byte-stable system block, and the server-side refusal
fallback. Claude proposes; app.pipeline.verifier checks; staff decide (R12).

Privacy: the post text is never logged (docs/05 §8). Logs carry the item hash prefix, counts, the served
model, token usage and the API request id only.
"""
from __future__ import annotations

import datetime as dt
import email.utils
import functools
import json
import logging
import re
from typing import TYPE_CHECKING, Any, Callable, Optional

import anthropic
from pydantic import ValidationError

from app.paths import PROMPTS_DIR, SCHEMAS_DIR
from app.pipeline import textnorm
from app.pipeline.types import ClassifierResult, LLMUsage, NormalizedItem, Verdict

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

    from app.pipeline.geo.gazetteer import Gazetteer
    from app.pipeline.lexicon import Lexicon

logger = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"   # pairs with fallbacks="default" (array form uses -06-01)
MAX_TOKENS = 6000                                    # adaptive thinking + the JSON (docs/05 §1)
REQUEST_TIMEOUT_S = 300.0                            # per attempt; the SDK retries 408/409/429/5xx itself

PROMPT_FILE = PROMPTS_DIR / "classifier-system.md"
SCHEMA_FILE = SCHEMAS_DIR / "classification.schema.json"

# docs/05 §4: the model sees at most this much post text. Longer text is excerpted: the first
# HEAD_CHARS (the title comes first in NormalizedItem.text) plus every later paragraph containing a
# signal term, with EXCERPT_MARKER between excerpts.
MAX_POST_CHARS = 6000
HEAD_CHARS = 2500
MAX_PARAGRAPH_CHARS = 1500
EXCERPT_MARKER = "[...]"

# max_tokens -> one retry at the next lower effort (docs/05 §2).
_LOWER_EFFORT = {"max": "xhigh", "xhigh": "high", "high": "medium", "medium": "low"}

# Price table, USD per million tokens (Anthropic model catalog, shared/models.md + shared/prompt-caching.md,
# cached 2026-10). Cache writes use the 5-minute TTL multiplier (1.25x input); this module only ever sends
# 5-minute cache_control. [verify] against the pricing page before the pilot; a price change only affects
# the daily-budget estimate, never correctness.
_PRICES: dict[str, dict[str, float]] = {
    #                  input   output  cache_read
    "claude-opus-5-5":   {"in": 4.00, "out": 20.00, "cache_read": 0.20},
    "claude-opus-5":     {"in": 5.00, "out": 25.00, "cache_read": 0.50},   # server-side fallback target
    "claude-opus-4-8":   {"in": 5.00, "out": 25.00, "cache_read": 0.50},   # server-side fallback target
    "claude-sonnet-5-5": {"in": 2.00, "out": 10.00, "cache_read": 0.20},
    "claude-sonnet-5":   {"in": 2.00, "out": 10.00, "cache_read": 0.20},
    "claude-haiku-5-5":  {"in": 0.10, "out": 0.50, "cache_read": 0.01},    # prompts <= 100K tokens
    "claude-fable-5-1":  {"in": 10.00, "out": 50.00, "cache_read": 0.25},
    "claude-mythos-5-1": {"in": 10.00, "out": 50.00, "cache_read": 0.25},
    "claude-fable-5":    {"in": 10.00, "out": 50.00, "cache_read": 1.00},
    "claude-mythos-5":   {"in": 10.00, "out": 50.00, "cache_read": 1.00},
}
# Unknown model ids are priced at the most expensive Opus row so the daily budget errs on the safe side.
_UNKNOWN_PRICE = {"in": 5.00, "out": 25.00, "cache_read": 0.50}
_CACHE_WRITE_MULT = 1.25

# Models whose requests were rejected because of the fallback beta/parameter. Requests to them are sent
# without it for the rest of the process (refusals then go straight to human review). See _is_fallback_400.
_FALLBACK_UNSUPPORTED: set[str] = set()

_TAG_RE = re.compile(r"<\s*/?\s*(post|source_context)\s*>", re.IGNORECASE)


# ---------------------------------------------------------------------------
# System prompt and schema
# ---------------------------------------------------------------------------
@functools.lru_cache(maxsize=1)
def _read_prompt_file() -> str:
    return PROMPT_FILE.read_text(encoding="utf-8")


def load_system_prompt_text() -> str:
    """The exact text between the two '---' lines of prompts/classifier-system.md."""
    lines = _read_prompt_file().replace("\r\n", "\n").split("\n")
    marks = [i for i, line in enumerate(lines) if line.strip() == "---"]
    if len(marks) < 2:
        raise ValueError(f"{PROMPT_FILE.name}: expected two '---' lines around the system prompt")
    text = "\n".join(lines[marks[0] + 1:marks[1]]).strip()
    if not text:
        raise ValueError(f"{PROMPT_FILE.name}: the system prompt between the '---' lines is empty")
    return text


def build_system_blocks(gazetteer: Optional["Gazetteer"]) -> list[dict[str, Any]]:
    """[{type:'text', text: prompt + '\\n\\n' + gazetteer.reference_list_text(), cache_control:{type:'ephemeral'}}].
    Byte-stable for a given gazetteer (no timestamps)."""
    text = load_system_prompt_text()
    if gazetteer is not None:
        try:
            ref = (gazetteer.reference_list_text() or "").strip()
        except Exception as exc:  # noqa: BLE001 - a broken gazetteer must not stop classification
            logger.warning("classifier: place reference list unavailable (%s); using the prompt alone",
                           type(exc).__name__)
            ref = ""
        if ref:
            text = f"{text}\n\n{ref}"
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


@functools.lru_cache(maxsize=1)
def _read_schema_file() -> str:
    return SCHEMA_FILE.read_text(encoding="utf-8")


def _strip_comments(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip_comments(v) for k, v in node.items() if k != "$comment"}
    if isinstance(node, list):
        return [_strip_comments(v) for v in node]
    return node


def load_schema() -> dict[str, Any]:
    """schemas/classification.schema.json without '$comment' keys (sent as output_config.format.schema)."""
    return _strip_comments(json.loads(_read_schema_file()))


# ---------------------------------------------------------------------------
# User message
# ---------------------------------------------------------------------------
def _utc(d: dt.datetime) -> dt.datetime:
    return d.replace(tzinfo=dt.timezone.utc) if d.tzinfo is None else d.astimezone(dt.timezone.utc)


def age_human(delta: dt.timedelta) -> str:
    """'under 1 minute', '1 minute', '3 hours', '2 days' (negative deltas from clock skew -> 'under 1 minute')."""
    secs = delta.total_seconds()
    if secs < 60:
        return "under 1 minute"
    minutes = int(secs // 60)
    if minutes < 60:
        return f"{minutes} minute{'' if minutes == 1 else 's'}"
    hours = int(secs // 3600)
    if hours < 24:
        return f"{hours} hour{'' if hours == 1 else 's'}"
    days = int(secs // 86400)
    return f"{days} day{'' if days == 1 else 's'}"


def _neutralize_tags(text: str) -> str:
    """Keep post text from closing or opening the prompt's own <post>/<source_context> tags."""
    return _TAG_RE.sub(lambda m: f"[{m.group(1).lower()} tag]", text)


def _field(value: Optional[str]) -> str:
    """One-line context field value ('' when absent)."""
    if not value:
        return ""
    return _neutralize_tags(textnorm.clean(str(value)))


def _cut_words(text: str, limit: int) -> str:
    """text[:limit], cut back to the last whitespace so no word is split."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    ws = max(cut.rfind(" "), cut.rfind("\n"))
    if ws > limit // 2:
        cut = cut[:ws]
    return cut.rstrip()


@functools.lru_cache(maxsize=1)
def _default_lexicon() -> Optional["Lexicon"]:
    try:
        from app.pipeline.lexicon import Lexicon
        return Lexicon.load()
    except Exception as exc:  # noqa: BLE001 - excerpting degrades to the head only
        logger.warning("classifier: lexicon unavailable for excerpting (%s)", type(exc).__name__)
        return None


def _signal_finder(item: NormalizedItem, lexicon: Optional["Lexicon"]) -> Callable[[str], Optional[int]]:
    """Return f(clean_text) -> offset of the first signal term, or None."""
    lex = lexicon
    if lex is None:
        terms = [t for t in (item.prefilter.matched_signal_terms if item.prefilter else []) if t and t.strip()]
        if terms:
            pats = [re.compile(r"(?<!\w)" + r"\s+".join(re.escape(w) for w in textnorm.clean(t).split()) + r"(?!\w)",
                               re.IGNORECASE) for t in sorted(set(terms), key=len, reverse=True)]

            def by_terms(s: str) -> Optional[int]:
                hits = [m.start() for p in pats for m in [p.search(s)] if m]
                return min(hits) if hits else None
            return by_terms
        lex = _default_lexicon()
    if lex is None:
        return lambda s: None

    def by_lexicon(s: str) -> Optional[int]:
        try:
            matches = lex.signal_matches(s)
        except Exception:  # noqa: BLE001
            return None
        return matches[0].start if matches else None
    return by_lexicon


def excerpt_post_text(item: NormalizedItem, *, lexicon: Optional["Lexicon"] = None,
                      max_chars: int = MAX_POST_CHARS) -> str:
    """docs/05 §4. Text up to max_chars is passed whole. Longer text becomes: the first HEAD_CHARS
    (title first) + each later paragraph that contains a signal term, with EXCERPT_MARKER between
    excerpts (and at the end when text was left out). Every excerpt is a verbatim span of item.text
    (modulo whitespace), so quotes copied from it still pass verifier V1."""
    text = (item.text or "").strip()
    if len(text) <= max_chars:
        return text
    sep = f"\n{EXCERPT_MARKER}\n"
    tail = f"\n{EXCERPT_MARKER}"
    head = _cut_words(text, min(HEAD_CHARS, max_chars))
    parts = [head]
    used = len(head)
    find = _signal_finder(item, lexicon)
    rest = text[len(head):]
    for raw in re.split(r"\n+", rest):
        para = textnorm.clean(raw)
        if not para:
            continue
        pos = find(para)
        if pos is None:
            continue
        if len(para) > MAX_PARAGRAPH_CHARS:
            start = para.rfind(" ", 0, max(0, pos - 200)) + 1 if pos > 200 else 0
            para = _cut_words(para[start:], MAX_PARAGRAPH_CHARS)
        room = max_chars - used - len(sep) - len(tail)          # keep space for the trailing marker
        if room <= 0:
            break
        if len(para) > room:
            para = _cut_words(para, room)
            if len(para) < 80:
                break
        parts.append(para)
        used += len(sep) + len(para)
    return sep.join(parts) + tail


def render_user_message(item: NormalizedItem, *, now: dt.datetime, lexicon: Optional["Lexicon"] = None) -> str:
    """User message per the template in prompts/classifier-system.md (source_context + <post>). Applies the
    docs/05 §4 truncation rule for long articles."""
    now_u = _utc(now)
    pub = _utc(item.published_at)
    ctx = item.context
    post = _neutralize_tags(excerpt_post_text(item, lexicon=lexicon))
    lines = [
        f"Today's date (UTC): {now_u.date().isoformat()}",
        "",
        "<source_context>",
        f"platform: {_field(item.platform)}",
        f"provider: {_field(item.provider)}",
        f"source_scope: {_field(ctx.source_scope)}",
        f"subreddit: {_field(ctx.subreddit)}",
        f"outlet_domain: {_field(ctx.outlet_domain)}",
        f"is_comment: {'true' if ctx.is_comment else 'false'}",
        f"parent_title: {_field(ctx.parent_title)}",
        f"published_at: {pub.strftime('%Y-%m-%dT%H:%M:%SZ')}  ({age_human(now_u - pub)} ago)",
        f"platform_place_tag: {_field(ctx.platform_place_tag)}",
        "</source_context>",
        "",
        "<post>",
        post,
        "</post>",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Cost and usage
# ---------------------------------------------------------------------------
def _price_for(model: str) -> dict[str, float]:
    m = (model or "").strip().lower()
    best: Optional[str] = None
    for key in _PRICES:
        if (m == key or m.startswith(key + "-")) and (best is None or len(key) > len(best)):
            best = key
    return _PRICES[best] if best else _UNKNOWN_PRICE


def estimate_cost_usd(model: str, usage: LLMUsage) -> float:
    """Price table (per MTok) for claude-opus-5-5 ($4 in / $20 out / $0.20 cache read / cache write 1.25x in),
    claude-sonnet-5-5 ($2/$10), claude-haiku-5-5 ($0.10/$0.50). Unknown model -> Opus prices (conservative)."""
    p = _price_for(model)
    total = (
        max(0, usage.input_tokens) * p["in"]
        + max(0, usage.output_tokens) * p["out"]
        + max(0, usage.cache_read_tokens) * p["cache_read"]
        + max(0, usage.cache_write_tokens) * p["in"] * _CACHE_WRITE_MULT
    ) / 1_000_000
    return round(total, 8)


def _int(obj: Any, name: str) -> int:
    try:
        return int(getattr(obj, name, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _attempt_usages(resp: Any, requested_model: str) -> list[LLMUsage]:
    """Per-model usage of one response. With server-side fallback, usage.iterations is the per-attempt
    source of truth and top-level usage covers only the final attempt; without iterations, use the top level."""
    served = str(getattr(resp, "model", "") or requested_model)
    u = getattr(resp, "usage", None)
    if u is None:
        return []
    iters = [it for it in (getattr(u, "iterations", None) or [])
             if getattr(it, "type", None) in ("message", "fallback_message")]
    rows = iters if iters else [u]
    out: list[LLMUsage] = []
    for it in rows:
        model = str(getattr(it, "model", "") or served) if iters else served
        lu = LLMUsage(
            input_tokens=_int(it, "input_tokens"),
            output_tokens=_int(it, "output_tokens"),
            cache_read_tokens=_int(it, "cache_read_input_tokens"),
            cache_write_tokens=_int(it, "cache_creation_input_tokens"),
            model=model,
        )
        lu.cost_usd = estimate_cost_usd(model, lu)
        out.append(lu)
    return out


def _add_usage(total: LLMUsage, resp: Any, requested_model: str) -> None:
    for part in _attempt_usages(resp, requested_model):
        total.input_tokens += part.input_tokens
        total.output_tokens += part.output_tokens
        total.cache_read_tokens += part.cache_read_tokens
        total.cache_write_tokens += part.cache_write_tokens
        total.cost_usd = round(total.cost_usd + part.cost_usd, 8)
    served = getattr(resp, "model", None)
    if served:
        total.model = str(served)
    rid = getattr(resp, "_request_id", None)
    if rid:
        total.request_id = str(rid)


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------
def _supports_fallback(model: str) -> bool:
    """Haiku models have no server-side fallback (a fallbacks list is a 400 there)."""
    return not (model or "").strip().lower().startswith("claude-haiku") and model not in _FALLBACK_UNSUPPORTED


def _request_kwargs(*, model: str, effort: str, system_blocks: list[dict[str, Any]], schema: dict[str, Any],
                    user_message: str, with_fallback: bool, timeout_s: float) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": system_blocks,
        "messages": [{"role": "user", "content": user_message}],
        "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        "timeout": timeout_s,
    }
    if with_fallback:
        kwargs["betas"] = [FALLBACK_BETA]
        kwargs["fallbacks"] = "default"
    return kwargs


def _first_text(resp: Any) -> Optional[str]:
    for block in getattr(resp, "content", None) or []:
        if getattr(block, "type", None) == "text":
            return getattr(block, "text", None)
    return None


def _retry_after_seconds(exc: Exception) -> Optional[float]:
    """Seconds from the retry-after-ms / retry-after headers (number or HTTP date); None when absent."""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if not headers:
        return None
    try:
        raw_ms = headers.get("retry-after-ms")
        if raw_ms is not None:
            return max(0.0, float(raw_ms) / 1000.0)
    except (TypeError, ValueError):
        pass
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        pass
    try:
        when = email.utils.parsedate_to_datetime(str(raw))
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    return max(0.0, (_utc(when) - dt.datetime.now(dt.timezone.utc)).total_seconds())


def _is_fallback_400(exc: anthropic.BadRequestError) -> bool:
    """A 400 that names the fallback beta/parameter (e.g. the beta is not enabled for this organization)."""
    msg = (getattr(exc, "message", None) or str(exc) or "").lower()
    return "fallback" in msg or "anthropic-beta" in msg


def _safe_msg(exc: Exception) -> str:
    """Short API error message for WARNING logs. API errors describe request structure, not content."""
    msg = getattr(exc, "message", None) or str(exc) or ""
    return textnorm.clean(str(msg))[:300]


def _result(kind: str, *, usage: LLMUsage, reason: str = "", verdict: Optional[Verdict] = None,
            retry_after_s: Optional[float] = None) -> ClassifierResult:
    return ClassifierResult(kind=kind, verdict=verdict, reason=reason, model=usage.model, usage=usage,  # type: ignore[arg-type]
                            retry_after_s=retry_after_s)


# ---------------------------------------------------------------------------
# Classify
# ---------------------------------------------------------------------------
async def classify(item: NormalizedItem, *, client: "AsyncAnthropic", model: str, effort: str,
                   system_blocks: list[dict[str, Any]], schema: dict[str, Any], now: dt.datetime,
                   lexicon: Optional["Lexicon"] = None, timeout_s: float = REQUEST_TIMEOUT_S) -> ClassifierResult:
    """One item per request (docs/05 §2). Uses client.beta.messages.create with betas=[FALLBACK_BETA],
    fallbacks='default' (omitted for Haiku models), output_config={'effort':..., 'format':{'type':'json_schema',
    'schema': schema}}, max_tokens=6000, no thinking parameter. Handles refusal / max_tokens (one retry at
    lower effort, then needs_human_review) / invalid JSON / typed exceptions exactly as docs/05 §2-3.

    Never raises. Usage from every attempt (including fallback hops) is summed into result.usage, with
    result.usage.model / result.model = the model that served the last response."""
    usage = LLMUsage(model=model)
    tag = (item.item_hash or "")[:12]
    try:
        user_message = render_user_message(item, now=now, lexicon=lexicon)
    except Exception as exc:  # noqa: BLE001 - malformed item: never crash the scan
        logger.warning("classifier: item %s could not be rendered (%s)", tag, type(exc).__name__)
        return _result("error", usage=usage, reason="render_failed")

    cur_effort = effort
    max_tokens_retried = False
    invalid_json_retried = False
    fallback_retried = False

    while True:
        with_fallback = _supports_fallback(model)
        kwargs = _request_kwargs(model=model, effort=cur_effort, system_blocks=system_blocks, schema=schema,
                                 user_message=user_message, with_fallback=with_fallback, timeout_s=timeout_s)
        try:
            resp = await client.beta.messages.create(**kwargs)
        except anthropic.BadRequestError as exc:
            if with_fallback and not fallback_retried and _is_fallback_400(exc):
                _FALLBACK_UNSUPPORTED.add(model)
                fallback_retried = True
                logger.warning("classifier: API rejected the server-side fallback for %s (%s); continuing "
                               "without it - refusals will go straight to human review", model, _safe_msg(exc))
                continue
            logger.warning("classifier: item %s bad request (%s, request_id=%s): %s", tag,
                           getattr(exc, "type", None), getattr(exc, "request_id", None), _safe_msg(exc))
            return _result("error", usage=usage, reason="bad_request")
        except anthropic.AuthenticationError:
            logger.error("classifier: Anthropic rejected the API key (401)")
            return _result("provider_down", usage=usage, reason="anthropic_auth")
        except anthropic.PermissionDeniedError as exc:
            logger.error("classifier: Anthropic denied access (403): %s", _safe_msg(exc))
            return _result("provider_down", usage=usage, reason="anthropic_forbidden")
        except anthropic.NotFoundError as exc:
            logger.error("classifier: model %s not found or not available to this key (404): %s", model, _safe_msg(exc))
            return _result("provider_down", usage=usage, reason="anthropic_model_not_found")
        except anthropic.RateLimitError as exc:
            retry_after = _retry_after_seconds(exc)
            logger.warning("classifier: rate limited (retry-after=%s)", retry_after)
            return _result("retry_later", usage=usage, reason="rate_limited", retry_after_s=retry_after)
        except anthropic.APIStatusError as exc:
            status = int(getattr(exc, "status_code", 0) or 0)
            if status >= 500:
                logger.warning("classifier: Anthropic server error %s (request_id=%s)", status,
                               getattr(exc, "request_id", None))
                return _result("retry_later", usage=usage, reason=f"server_error_{status}",
                               retry_after_s=_retry_after_seconds(exc))
            if status == 402:
                logger.error("classifier: Anthropic billing problem (402): %s", _safe_msg(exc))
                return _result("provider_down", usage=usage, reason="anthropic_billing")
            logger.warning("classifier: item %s API error %s (%s)", tag, status, _safe_msg(exc))
            return _result("error", usage=usage, reason=f"api_{status or 'error'}")
        except anthropic.APITimeoutError:
            logger.warning("classifier: request timed out")
            return _result("retry_later", usage=usage, reason="timeout")
        except anthropic.APIConnectionError:
            logger.warning("classifier: connection error")
            return _result("retry_later", usage=usage, reason="connection_error")
        except anthropic.AnthropicError as exc:   # e.g. APIResponseValidationError
            logger.warning("classifier: item %s unexpected SDK error %s", tag, type(exc).__name__)
            return _result("error", usage=usage, reason="api_error")
        except Exception as exc:  # noqa: BLE001 - classification must never crash the scan
            logger.warning("classifier: item %s unexpected error %s", tag, type(exc).__name__)
            return _result("error", usage=usage, reason="unexpected_error")

        _add_usage(usage, resp, model)
        stop = getattr(resp, "stop_reason", None)

        if stop == "refusal":
            details = getattr(resp, "stop_details", None)
            category = getattr(details, "category", None) if details is not None else None
            reason = f"refusal:{category or 'unspecified'}"
            _log_done(tag, "needs_human_review", reason, usage)
            return _result("needs_human_review", usage=usage, reason=reason)

        if stop == "max_tokens":
            lower = _LOWER_EFFORT.get(cur_effort)
            if lower and not max_tokens_retried:
                max_tokens_retried = True
                logger.info("classifier: item %s hit max_tokens at effort %s; retrying at %s", tag, cur_effort, lower)
                cur_effort = lower
                continue
            _log_done(tag, "needs_human_review", "max_tokens", usage)
            return _result("needs_human_review", usage=usage, reason="max_tokens")

        if stop == "model_context_window_exceeded":
            _log_done(tag, "needs_human_review", "context_window", usage)
            return _result("needs_human_review", usage=usage, reason="context_window")

        text = _first_text(resp)
        try:
            if not text:
                raise ValueError("no text block")
            verdict = Verdict.model_validate_json(text)
        except (ValidationError, ValueError, TypeError):
            # Never log the exception: pydantic messages echo the input, which can quote the post.
            if not invalid_json_retried:
                invalid_json_retried = True
                logger.info("classifier: item %s returned invalid or off-schema JSON; retrying once", tag)
                continue
            _log_done(tag, "needs_human_review", "invalid_json", usage)
            return _result("needs_human_review", usage=usage, reason="invalid_json")

        _log_done(tag, "ok", verdict.relevance, usage)
        return _result("ok", usage=usage, verdict=verdict)


def _log_done(tag: str, kind: str, reason: str, usage: LLMUsage) -> None:
    logger.info("classifier: item %s -> %s (%s) model=%s in=%d out=%d cache_read=%d cache_write=%d "
                "cost=$%.5f request_id=%s", tag, kind, reason, usage.model, usage.input_tokens,
                usage.output_tokens, usage.cache_read_tokens, usage.cache_write_tokens, usage.cost_usd,
                usage.request_id)


def reset_fallback_state() -> None:
    """Forget models marked as rejecting the fallback beta (tests; admin model changes)."""
    _FALLBACK_UNSUPPORTED.clear()

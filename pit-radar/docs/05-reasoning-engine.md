# 05 — Reasoning Engine (Claude classifier + verifier)

The classifier reads one post at a time and returns a strict JSON verdict. The **verifier** is deterministic code that checks the verdict against the post before anything is stored. Claude proposes; code verifies; staff decide (R12).

## 1. Model and settings

| Setting | Default | Notes |
|---|---|---|
| `ANTHROPIC_MODEL` | `claude-opus-5-5` | Anthropic's current default model [verified in Anthropic's API reference, cached 2026-10-06; the key used in planning authenticated successfully against `/v1/models`]. Confirm the id appears in `/v1/models` for your key at build time. Configurable. |
| Thinking | omit the parameter | On Claude Opus 5.5 thinking is always adaptive; `{"type":"disabled"}` and `budget_tokens` return 400 [verified in API reference]. |
| `CLASSIFIER_EFFORT` | `medium` | `output_config.effort`; Opus 5.5's default is `medium`. Try `low` during the pilot if precision holds (cost lever). |
| `max_tokens` | 6000 | Room for adaptive thinking plus the JSON (non-streaming request; well under SDK timeout guidance). |
| Structured output | `output_config.format = {"type": "json_schema", "schema": <classification.schema.json>}` | Guarantees the first text block is valid JSON for the schema [verified pattern]. |
| Refusal fallback | beta header `server-side-fallback-2026-07-01` + `fallbacks: "default"` | Anthropic's recommendation for Opus 5.5 code. A classifier false positive on a post about vulnerable people must not silently drop the post [verified pattern]. |
| Prompt caching | `cache_control: {"type": "ephemeral"}` on the system block | Prefix-stable system prompt (no timestamps). Verify `usage.cache_read_input_tokens > 0` on the 2nd call of a scan. |
| `LLM_CONCURRENCY` | 4 | Parallel requests per scan |
| SDK retries | `max_retries=3` | SDK retries 408/409/429/5xx and connection errors with backoff [verified] |

**Do not** ask the model to "explain its reasoning step by step" in the output. That pattern can trigger `reasoning_extraction` refusals on current models. The schema's `decision_basis` is a one-sentence justification tied to quotes, which is different.

## 2. Request (Python, official SDK)

Uses only patterns from Anthropic's Python SDK reference: `AsyncAnthropic`, `client.beta.messages.create(..., betas=[...], fallbacks=...)`, `output_config`, a cached system block, `stop_reason` checks, and typed exceptions. **[verify]** that the installed SDK version accepts `fallbacks="default"` on `beta.messages.create`. If not, upgrade, or use the array form `fallbacks=[{"model": "<a model listed in allowed_fallback_models>"}]` with header `server-side-fallback-2026-06-01`.

```python
# app/pipeline/classifier.py (sketch)
import json
import anthropic
from pydantic import BaseModel, ValidationError

client = anthropic.AsyncAnthropic(max_retries=3)          # reads ANTHROPIC_API_KEY from env

SYSTEM_BLOCKS = [{
    "type": "text",
    "text": load_system_prompt(),                          # prompts/classifier-system.md + reference place list; byte-stable
    "cache_control": {"type": "ephemeral"},
}]
SCHEMA = json.load(open("schemas/classification.schema.json"))
SCHEMA.pop("$comment", None)

class Verdict(BaseModel):                                  # mirrors classification.schema.json
    relevance: str
    signal_type: str
    not_relevant_reason: str
    in_long_island_counties: str
    area_basis: str
    time_reference: str
    location: dict
    people_count_estimate: int
    flags: dict
    evidence_quotes: list[dict]
    neutral_summary: str
    decision_basis: str
    confidence: float

async def classify(item: NormalizedItem, settings) -> ClassifierResult:
    try:
        resp = await client.beta.messages.create(
            model=settings.anthropic_model,                # "claude-opus-5-5"
            max_tokens=6000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=SYSTEM_BLOCKS,
            output_config={"effort": settings.classifier_effort,
                           "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": render_user_message(item)}],
        )
    except anthropic.BadRequestError as e:                 # schema/param problem: do not retry blindly
        return ClassifierResult.error("bad_request", str(e))
    except anthropic.AuthenticationError:
        return ClassifierResult.provider_down("anthropic_auth")
    except anthropic.RateLimitError as e:
        return ClassifierResult.retry_later(e.response.headers.get("retry-after"))
    except anthropic.APIStatusError as e:
        return ClassifierResult.retry_later(None) if e.status_code >= 500 else ClassifierResult.error("api", str(e))
    except anthropic.APIConnectionError:
        return ClassifierResult.retry_later(None)

    record_usage(resp)                                      # tokens incl. cache reads -> api_usage; resp.model = model that served

    if resp.stop_reason == "refusal":                      # whole fallback chain declined
        cat = resp.stop_details.category if resp.stop_details else None
        return ClassifierResult.needs_human_review(f"refusal:{cat}")
    if resp.stop_reason == "max_tokens":
        return ClassifierResult.error("max_tokens", "")    # one retry with effort lowered, then needs_human_review

    text = next((b.text for b in resp.content if b.type == "text"), None)
    try:
        verdict = Verdict.model_validate_json(text)
    except (ValidationError, TypeError):
        return ClassifierResult.needs_human_review("invalid_json")
    return ClassifierResult.ok(verdict, model=resp.model)
```

## 3. Refusals

Posts about vulnerable people, sometimes with harsh language, can trip safety classifiers. Order of handling:
1. Server-side fallback (`fallbacks: "default"`) retries on Anthropic's recommended model within the same call.
2. If the final `stop_reason` is still `refusal`: **no AI verdict is invented.** The item becomes `needs_human_review` with reason `refusal:<category>`. It appears in the reviewer queue with the post link only (no summary) and is never mapped automatically.
3. Track the refusal rate per day. If it exceeds 2%, review the prompt wording with Anthropic's guidance.

## 4. Pre-classification safeguards

- Feed the model only **normalized, scrubbed** text (docs/04 §5): handles, emails, phone numbers and links are already replaced.
- Truncate to 6,000 characters. For long news articles, pass title + first 2,500 characters + any paragraph containing a signal term (marked `[…]` between excerpts). Quotes must still come from the passed text.
- One item per request. Batching several posts into one prompt invites cross-contamination of quotes and places.

## 5. Verifier (deterministic, `app/pipeline/verifier.py`)

Normalize both sides the same way: Unicode NFKC, curly quotes → straight, dashes unified, whitespace collapsed, case-folded.

| Check | Rule | On failure |
|---|---|---|
| V1 quotes | Each `evidence_quotes[i].text` is a substring of the post text; ≤ 30 words; ≤ 2 quotes kept | Drop the failing quote (a quote over 30 words is dropped, not trimmed) |
| V2 quote privacy | Check each quote that passed V1 against `config/lexicon.yaml`: `privacy_patterns` (handle, email, phone, URL, street address, plate) and `privacy_denylist`. **mask** terms (gendered words) are replaced in the **stored** copy with neutral brackets ("[person]", "[they]") and the quote gets `masked: true`. **drop** terms cover age, race/ethnicity/nationality, immigration status, gender identity, appearance and clothing, disability and health, and substance use | Pattern or drop-term match: drop the quote and set `evidence_redacted = true`. Mask-term match only: keep the masked copy |
| V3 location span | `location.location_text` is a substring of the post text | Treat as no location → `unlocated` (not mapped) |
| V4 county | `in_long_island_counties == "yes"` **and** the geocoded point is inside the Nassau ∪ Suffolk polygon (docs/06) | `no` → discard (`outside_area`); `unclear` or geofence fail with `yes` → `needs_human_review` |
| V5 time | `time_reference` ∈ {current, ongoing}; or `unclear` with `published_at` ≤ 7 days old | `past` → discard; `unclear` and older → `needs_human_review` |
| V6 summary privacy | `neutral_summary` and `decision_basis` contain no denylist word, handle, email or phone, and ≤ 240 chars | Replace with a template: "{Type label} reported at {place label} ({time label})." |
| V7 consistency | `relevance = not_relevant` ⇒ `signal_type = none`; `signal_type = none` ⇒ not relevant; `people_count_estimate` ≥ -1 | Coerce to the conservative value (not relevant / -1). -1 is stored as NULL |
| V8 residence | `location.is_private_residence = true` | Snap to hamlet/village centroid only (R5) |

**After V1 and V2:** a relevant verdict left with **0** quotes can never be `ai_verified`. It goes to `needs_human_review`, where staff see the post link (acceptance criterion A3).

## 6. Decision table

| Verdict after verification | Result |
|---|---|
| relevant, confidence ≥ `MIN_CONFIDENCE_MAP` (0.70), V1–V5 pass, located, in area, type ≠ self_report | `ai_verified` → mapped, joins hotspot |
| relevant, type = self_report, town known and in area | `self_report` → town-level list (R7) |
| relevant, located but confidence `MIN_CONFIDENCE_REVIEW` (0.50) to < 0.70, **or** any "→ needs_human_review" outcome above, **or** relevance = uncertain | `needs_human_review` → reviewer queue, not mapped until confirmed |
| relevant but no verifiable location | `unlocated` → feed (county-level if known), not mapped |
| not_relevant, or confidence < `MIN_CONFIDENCE_REVIEW` (0.50) | not stored as a signal; `items_seen.decision = not_relevant:<reason>` |

Thresholds are settings. During the pilot, export reviewer decisions and pick the thresholds that hit ≥ 0.90 precision for mapped signals (docs/10 §5).

## 7. Cost model

Prices from Anthropic's API reference (cached 2026-10-06) for `claude-opus-5-5`: **$4 / MTok input, $20 / MTok output, cache reads $0.20 / MTok** [verified in reference; confirm on the pricing page]. Rough per-item cost:

| Part | Tokens (est.) | Cost |
|---|---|---|
| Cached system prompt + place list (cache read) | ~6,000 | ~$0.0012 |
| Post + context (input) | ~800 | ~$0.0032 |
| Output: adaptive thinking + JSON at `medium` effort | ~800–1,500 | ~$0.016–0.030 |
| **Per classification** | | **≈ $0.02–0.035** |

At 150 classifications/day that is **≈ $3–5/day**. `DAILY_LLM_BUDGET_USD` (default $5) is a hard stop. Levers, in order: tighten the prefilter (fewer calls); lower effort to `low` if pilot precision holds; switch `ANTHROPIC_MODEL` to `claude-sonnet-5-5` ($2/$10); add a cheap triage pass with `claude-haiku-5-5` ($0.10/$0.50) that only forwards relevant/uncertain items to the main model. Note: Haiku 5.5 has **no** server-side fallback, so don't send `fallbacks` on its requests. Model changes are the LICH team's call; measure precision on the golden set before switching.

## 8. Logging

Per call, store in `api_usage`: `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, the served model (`resp.model`), and the response request id (`resp._request_id`) in `query_log`, for support. **Never log the post text** at INFO level; DEBUG logging of post text is off in production.

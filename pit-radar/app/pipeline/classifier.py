"""Claude classifier (docs/05 §1-4, prompts/classifier-system.md). STUB - reasoning builder."""
from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any, Optional

from app.pipeline.types import ClassifierResult, LLMUsage, NormalizedItem

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

    from app.pipeline.geo.gazetteer import Gazetteer

FALLBACK_BETA = "server-side-fallback-2026-07-01"


def load_system_prompt_text() -> str:
    """The exact text between the two '---' lines of prompts/classifier-system.md."""
    raise NotImplementedError


def build_system_blocks(gazetteer: Optional["Gazetteer"]) -> list[dict[str, Any]]:
    """[{type:'text', text: prompt + '\\n\\n' + gazetteer.reference_list_text(), cache_control:{type:'ephemeral'}}].
    Byte-stable for a given gazetteer (no timestamps)."""
    raise NotImplementedError


def load_schema() -> dict[str, Any]:
    """schemas/classification.schema.json without '$comment' keys (sent as output_config.format.schema)."""
    raise NotImplementedError


def render_user_message(item: NormalizedItem, *, now: dt.datetime) -> str:
    """User message per the template in prompts/classifier-system.md (source_context + <post>). Applies the
    docs/05 §4 truncation rule for long articles."""
    raise NotImplementedError


def estimate_cost_usd(model: str, usage: LLMUsage) -> float:
    """Price table (per MTok) for claude-opus-5-5 ($4 in / $20 out / $0.20 cache read / cache write 1.25x in),
    claude-sonnet-5-5 ($2/$10), claude-haiku-5-5 ($0.10/$0.50). Unknown model -> Opus prices (conservative)."""
    raise NotImplementedError


async def classify(item: NormalizedItem, *, client: "AsyncAnthropic", model: str, effort: str,
                   system_blocks: list[dict[str, Any]], schema: dict[str, Any], now: dt.datetime) -> ClassifierResult:
    """One item per request (docs/05 §2). Uses client.beta.messages.create with betas=[FALLBACK_BETA],
    fallbacks='default' (omitted for Haiku models), output_config={'effort':..., 'format':{'type':'json_schema',
    'schema': schema}}, max_tokens=6000, no thinking parameter. Handles refusal / max_tokens (one retry at
    lower effort, then needs_human_review) / invalid JSON / typed exceptions exactly as docs/05 §2-3."""
    raise NotImplementedError

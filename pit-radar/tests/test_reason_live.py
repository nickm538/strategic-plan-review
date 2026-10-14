"""Live contract test of the classifier against the real Anthropic API (docs/10 §2 item 4).

SKIPPED unless RUN_LIVE=1 and ANTHROPIC_API_KEY are set. Never run in CI. Spends a few cents.

  RUN_LIVE=1 ANTHROPIC_API_KEY=... .venv/bin/pytest tests/test_reason_live.py -s

Classifies three synthetic posts sequentially with the system prompt built WITHOUT a gazetteer, asserts each
result is a schema-valid verdict, prints usage, and checks prompt caching: calls 2 and 3 must report
cache_read_input_tokens > 0 (docs/05 §1).

Key lookup: tests/conftest.py removes ANTHROPIC_API_KEY from os.environ when it is imported (before this
module), so the key is read from LIVE_ANTHROPIC_API_KEY, else ANTHROPIC_API_KEY, else the process's initial
environment (Linux /proc/self/environ, which os.environ.pop does not change). The key is never printed.
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest

from app.pipeline import classifier as clf
from app.pipeline.types import ItemContext, Verdict
from tests.test_reason_support import make_item


def _initial_env(name: str) -> str:
    try:
        raw = Path("/proc/self/environ").read_bytes()
    except OSError:
        return ""
    prefix = name.encode() + b"="
    for entry in raw.split(b"\0"):
        if entry.startswith(prefix):
            return entry[len(prefix):].decode("utf-8", "replace")
    return ""


def _live_key() -> str:
    return (os.environ.get("LIVE_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
            or _initial_env("ANTHROPIC_API_KEY")).strip()


_RUN = os.environ.get("RUN_LIVE") == "1" and bool(_live_key())

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not _RUN, reason="live test: set RUN_LIVE=1 and ANTHROPIC_API_KEY"),
]

POSTS = [
    ("encampment_ronkonkoma",
     "There are two tents set up in the woods right behind the Ronkonkoma LIRR station again, "
     "someone was sleeping there when I walked past this morning.",
     ItemContext(query="homeless", source_scope="li_scoped", subreddit="longisland")),
    ("queens_false_positive",
     "Homeless guy sleeping at Jamaica station again on the LIRR platform, third night this week.",
     ItemContext(query="homeless LIRR", source_scope="general")),
    ("fundraiser",
     "Join us Saturday for our coat drive and gala raising money for homeless families on Long Island! "
     "Tickets at the link.",
     ItemContext(query="homeless Long Island", source_scope="regional")),
]


async def test_live_classify_three_posts_and_cache_reads():
    import anthropic

    model = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5")
    effort = os.environ.get("CLASSIFIER_EFFORT", "medium")
    now = dt.datetime.now(dt.timezone.utc)
    blocks = clf.build_system_blocks(None)
    schema = clf.load_schema()
    results = []
    async with anthropic.AsyncAnthropic(api_key=_live_key(), max_retries=3) as client:
        for i, (name, text, ctx) in enumerate(POSTS):
            item = make_item(text, published_at=now - dt.timedelta(hours=2), context=ctx, item_hash=f"{i:064d}")
            res = await clf.classify(item, client=client, model=model, effort=effort, system_blocks=blocks,
                                     schema=schema, now=now)
            u = res.usage
            print(f"\n[{name}] kind={res.kind} reason={res.reason!r} model={res.model} in={u.input_tokens} "
                  f"out={u.output_tokens} cache_read={u.cache_read_tokens} cache_write={u.cache_write_tokens} "
                  f"cost=${u.cost_usd:.5f} request_id={u.request_id}")
            if res.verdict is not None:
                v = res.verdict
                print(f"    relevance={v.relevance} type={v.signal_type} area={v.in_long_island_counties} "
                      f"time={v.time_reference} confidence={v.confidence}")
            results.append(res)

    for (name, _text, _ctx), res in zip(POSTS, results, strict=True):
        assert res.kind == "ok", f"{name}: {res.kind} {res.reason}"
        assert isinstance(res.verdict, Verdict)
        Verdict.model_validate(res.verdict.model_dump())     # round-trips through the strict schema model

    assert results[1].usage.cache_read_tokens > 0, "call 2 did not read the cached system prompt"
    assert results[2].usage.cache_read_tokens > 0, "call 3 did not read the cached system prompt"

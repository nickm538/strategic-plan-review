"""De-duplication (docs/04 §5).

Two keys: item_hash = sha256(canonical_url) catches the same post found twice; content_hash
(textnorm.content_fingerprint) catches the same story under different URLs (syndicated copies,
reposts). `items_seen` remembers both across scans and never stores content (docs/03).
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Iterable, Iterator, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import ItemSeen
from app.pipeline.types import NormalizedItem

logger = logging.getLogger(__name__)

_SCOPE_RANK = {"general": 0, "regional": 1, "li_scoped": 2}
_IN_CHUNK = 500
_MAX_DECISION_LEN = 200


def _merge(a: NormalizedItem, b: NormalizedItem) -> NormalizedItem:
    """Keep the earliest-published copy (ties: the first seen). Its context gains the strongest source
    scope and any query place the other copy carried, so the prefilter credits an LI-scoped find."""
    keep, other = (b, a) if b.published_at < a.published_at else (a, b)
    ctx = keep.context
    changes: dict[str, object] = {}
    if _SCOPE_RANK.get(other.context.source_scope, 0) > _SCOPE_RANK.get(ctx.source_scope, 0):
        changes["source_scope"] = other.context.source_scope
    if ctx.query_place_id is None and other.context.query_place_id is not None:
        changes["query_place_id"] = other.context.query_place_id
    if not ctx.query_place_name and other.context.query_place_name:
        changes["query_place_name"] = other.context.query_place_name
    if not changes:
        return keep
    return dataclasses.replace(keep, context=dataclasses.replace(ctx, **changes))


def _collapse(items: Iterable[NormalizedItem], key_of) -> tuple[list[NormalizedItem], int]:
    index: dict[str, int] = {}
    out: list[NormalizedItem] = []
    removed = 0
    for it in items:
        key = key_of(it)
        if not key:
            out.append(it)
            continue
        pos = index.get(key)
        if pos is None:
            index[key] = len(out)
            out.append(it)
        else:
            out[pos] = _merge(out[pos], it)
            removed += 1
    return out, removed


def dedupe_batch(items: list[NormalizedItem]) -> tuple[list[NormalizedItem], int]:
    """Within one scan: collapse by item_hash, then by content_hash; keep the earliest published_at.
    Returns (unique_items, n_removed). Order of first appearance is preserved."""
    by_url, n1 = _collapse(items, lambda it: it.item_hash)
    by_content, n2 = _collapse(by_url, lambda it: it.content_hash)
    return by_content, n1 + n2


def _chunks(values: list[str], size: int = _IN_CHUNK) -> Iterator[list[str]]:
    for i in range(0, len(values), size):
        yield values[i:i + size]


def filter_seen(db: Session, items: list[NormalizedItem]) -> tuple[list[NormalizedItem], int]:
    """Drop items whose item_hash, or (content_hash, provider), is already in items_seen. Returns (new, n_seen)."""
    if not items:
        return [], 0
    hashes = sorted({it.item_hash for it in items if it.item_hash})
    seen_hashes: set[str] = set()
    for chunk in _chunks(hashes):
        seen_hashes.update(db.scalars(select(ItemSeen.item_hash).where(ItemSeen.item_hash.in_(chunk))))
    contents = sorted({it.content_hash for it in items if it.content_hash})
    seen_content: set[tuple[str, str]] = set()
    for chunk in _chunks(contents):
        rows = db.execute(select(ItemSeen.content_hash, ItemSeen.provider).where(ItemSeen.content_hash.in_(chunk)))
        seen_content.update((ch, prov) for ch, prov in rows)
    new = [it for it in items
           if it.item_hash not in seen_hashes and (it.content_hash, it.provider) not in seen_content]
    return new, len(items) - len(new)


def _pending_match(db: Session, item: NormalizedItem) -> Optional[ItemSeen]:
    for obj in db.new:
        if isinstance(obj, ItemSeen) and (
            obj.item_hash == item.item_hash
            or (obj.content_hash == item.content_hash and obj.provider == item.provider)
        ):
            return obj
    return None


def mark_seen(db: Session, item: NormalizedItem, decision: str) -> None:
    """Insert or update the items_seen row (no content). Caller commits.

    Upsert order: a row with this item_hash -> update its decision; else a row with the same
    (content_hash, provider) (same story, another URL) -> update that row's decision, so the unique
    constraint is never violated; else insert. Rows still pending in this session are honoured too."""
    decision = (decision or "unknown")[:_MAX_DECISION_LEN]
    with db.no_autoflush:
        row = _pending_match(db, item)
        if row is None:
            row = db.get(ItemSeen, item.item_hash)
        if row is None and item.content_hash:
            row = db.scalars(
                select(ItemSeen).where(ItemSeen.content_hash == item.content_hash,
                                       ItemSeen.provider == item.provider).limit(1)
            ).first()
        if row is not None:
            row.decision = decision
            return
        db.add(ItemSeen(item_hash=item.item_hash, content_hash=item.content_hash, provider=item.provider,
                        first_seen=utcnow(), decision=decision))

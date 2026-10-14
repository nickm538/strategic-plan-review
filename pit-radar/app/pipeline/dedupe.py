"""De-duplication (docs/04 §5). STUB - text-pipeline builder."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.pipeline.types import NormalizedItem


def dedupe_batch(items: list[NormalizedItem]) -> tuple[list[NormalizedItem], int]:
    """Within one scan: collapse by item_hash, then by content_hash; keep the earliest published_at.
    Returns (unique_items, n_removed)."""
    raise NotImplementedError


def filter_seen(db: Session, items: list[NormalizedItem]) -> tuple[list[NormalizedItem], int]:
    """Drop items whose item_hash, or (content_hash, provider), is already in items_seen. Returns (new, n_seen)."""
    raise NotImplementedError


def mark_seen(db: Session, item: NormalizedItem, decision: str) -> None:
    """Insert or update the items_seen row (no content). Caller commits."""
    raise NotImplementedError

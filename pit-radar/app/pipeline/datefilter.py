"""Date window filter (docs/04 §5, F3). STUB - text-pipeline builder."""
from __future__ import annotations

import datetime as dt

from app.pipeline.types import NormalizedItem


def date_filter(items: list[NormalizedItem], *, now: dt.datetime, max_age_days: int) -> tuple[list[NormalizedItem], dict[str, int]]:
    """Keep now - max_age_days <= published_at <= now + 1h. Returns (kept, {"too_old": n, "future": n})."""
    raise NotImplementedError

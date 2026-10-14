"""Scan orchestration (docs/02 §4-5). STUB - runner builder."""
from __future__ import annotations

from typing import Literal, Optional

from app.state import AppState


async def run_scan(state: AppState, *, trigger: Literal["schedule", "manual", "pit_mode"],
                   force_mode: Optional[Literal["standard", "pit"]] = None) -> Optional[int]:
    """Lock -> plan -> collect (providers in parallel, errors isolated) -> normalize -> dedupe -> seen filter ->
    date filter -> prefilter -> (reddit comment pass) -> queue to pending_items -> classify pending (newest,
    then highest score; <= MAX_CLASSIFICATIONS_PER_SCAN, LLM_CONCURRENCY, daily $ cap) -> verify -> geocode ->
    decide -> privacy.build_signal_values -> persist -> hotspots -> SSE -> usage -> release lock.
    Records stage_counts / drop_reasons / errors on the scan_runs row. Returns scan_id, or None if skipped."""
    raise NotImplementedError


async def run_discovery(state: AppState) -> dict:
    """SocialCrawl endpoint discovery; persist to settings (KEY_SC_ENDPOINTS)."""
    raise NotImplementedError

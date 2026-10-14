"""Hotspot assignment, scoring and weekly rollups (docs/03 §4).

Assignment (a mapped signal, or a needs_human_review signal with coordinates):
  1. an ACTIVE hotspot with the same place_id; else
  2. the nearest ACTIVE hotspot within the radius (haversine): HOTSPOT_RADIUS_M, or the East End radius when
     the signal's town (or its gazetteer place's town) is one of EAST_END_TOWNS; else
  3. a new hotspot at the signal's snapped coordinate, labelled from place_label, else town.

Score (members not dismissed):
  min(2, 1 + 0.25 * (distinct_sources_30d - 1)) * Σ status_w * type_w * ai_confidence * 0.5 ** (age_days / half_life)
Distinct sources are distinct (provider, platform) pairs, with the URL host in place of the platform for news
and web items. primary_type is the type with > 60% of the weighted sum, else 'mixed'.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
from collections import defaultdict
from typing import Iterable, Optional
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Hotspot, HotspotWeekly, Place, Signal

logger = logging.getLogger(__name__)

EAST_END_TOWNS = frozenset({"East Hampton", "Southampton", "Riverhead", "Southold", "Shelter Island"})
STATUS_W = {"confirmed": 1.5, "ai_verified": 1.0, "needs_human_review": 0.5}
TYPE_W = {"encampment": 1.5, "individual_unsheltered": 1.0, "vehicle_dwelling": 1.0}

ASSIGNABLE_STATUSES = frozenset(STATUS_W)          # dismissed / unlocated / self_report never join
HOTSPOT_PRIMARY_TYPES = ("encampment", "individual_unsheltered", "vehicle_dwelling")
PRIMARY_SHARE = 0.60
MAX_DIVERSITY = 2.0
DIVERSITY_STEP = 0.25
UNKNOWN_TYPE_W = 1.0                               # a member without a known type (should not happen)
EARTH_RADIUS_M = 6_371_008.8
_COUNTIES = ("Nassau", "Suffolk")
_EAST_END_LOWER = frozenset(t.lower() for t in EAST_END_TOWNS)
_HOST_PLATFORMS = frozenset({"news", "web"})
_MAX_LABEL = 120


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------
def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def _bbox(lat: float, lon: float, radius_m: float) -> tuple[float, float, float, float]:
    dlat = math.degrees(radius_m / EARTH_RADIUS_M)
    coslat = max(0.01, math.cos(math.radians(lat)))
    dlon = math.degrees(radius_m / (EARTH_RADIUS_M * coslat))
    return lat - dlat, lat + dlat, lon - dlon, lon + dlon


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _aware(d: Optional[dt.datetime]) -> Optional[dt.datetime]:
    if d is None:
        return None
    return d.replace(tzinfo=dt.timezone.utc) if d.tzinfo is None else d.astimezone(dt.timezone.utc)


def _has_point(signal: Signal) -> bool:
    try:
        return (signal.lat is not None and signal.lon is not None
                and math.isfinite(float(signal.lat)) and math.isfinite(float(signal.lon)))
    except (TypeError, ValueError):
        return False


def _assignable(signal: Signal) -> bool:
    return (signal.status in ASSIGNABLE_STATUSES and signal.signal_type != "self_report"
            and _has_point(signal))


def _url_host(url: str) -> str:
    try:
        host = (urlsplit(url or "").hostname or "").rstrip(".").lower()
    except ValueError:
        return ""
    for prefix in ("www.", "m.", "amp.", "mobile."):
        if host.startswith(prefix) and host.count(".") >= 2:
            host = host[len(prefix):]
    return host


def source_key(signal: Signal) -> tuple[str, str]:
    """(provider, platform), or (provider, url host) for news/web items."""
    platform = signal.platform or ""
    if platform in _HOST_PLATFORMS:
        return signal.provider or "", _url_host(signal.url) or platform
    return signal.provider or "", platform


def diversity_multiplier(distinct_sources: int) -> float:
    return min(MAX_DIVERSITY, 1.0 + DIVERSITY_STEP * (max(1, distinct_sources) - 1))


def signal_weight(signal: Signal, *, now: dt.datetime, half_life_days: float) -> float:
    """status_w * type_w * ai_confidence * 0.5 ** (age_days / half_life_days) (0 for non-members)."""
    sw = STATUS_W.get(signal.status or "")
    if sw is None:
        return 0.0
    tw = TYPE_W.get(signal.signal_type or "", UNKNOWN_TYPE_W)
    try:
        conf = float(signal.ai_confidence or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    conf = min(1.0, max(0.0, conf)) if math.isfinite(conf) else 0.0
    published = _aware(signal.published_at) or now
    age_days = max(0.0, (now - published).total_seconds() / 86400.0)
    hl = half_life_days if half_life_days and half_life_days > 0 else 7.0
    return sw * tw * conf * (0.5 ** (age_days / hl))


def _place_chain(db: Session, place_id: Optional[int], depth: int = 3) -> list[Place]:
    out: list[Place] = []
    seen: set[int] = set()
    pid = place_id
    while pid is not None and pid not in seen and len(out) < depth:
        seen.add(pid)
        p = db.get(Place, pid)
        if p is None:
            break
        out.append(p)
        pid = p.parent_id
    return out


def _is_east_end(db: Session, signal: Signal) -> bool:
    """The signal's town, or the town of its gazetteer place (or a parent), is an East End town."""
    if (signal.town or "").strip().lower() in _EAST_END_LOWER:
        return True
    for p in _place_chain(db, signal.place_id):
        if (p.town or "").strip().lower() in _EAST_END_LOWER:
            return True
        if p.kind in ("town", "city") and (p.name or "").strip().lower() in _EAST_END_LOWER:
            return True
    return False


def _nearest(cands: Iterable[Hotspot], lat: float, lon: float, max_m: Optional[float]) -> Optional[Hotspot]:
    best: Optional[Hotspot] = None
    best_d = math.inf
    for h in cands:
        d = haversine_m(lat, lon, h.lat, h.lon)
        if max_m is not None and d > max_m:
            continue
        if d < best_d or (d == best_d and best is not None and h.id < best.id):
            best, best_d = h, d
    return best


def _create_hotspot(db: Session, signal: Signal, places: list[Place]) -> Optional[Hotspot]:
    place = places[0] if places else None
    county = signal.county if signal.county in _COUNTIES else (place.county if place and place.county in _COUNTIES
                                                                   else None)
    if county is None:
        logger.warning("hotspots: signal %s has coordinates but no county; not assigned", signal.id)
        return None
    town = signal.town or (place.town if place else None)
    label = (signal.place_label or signal.town or (place.name if place else None) or f"{county} County location")
    label = " ".join(str(label).split())[:_MAX_LABEL] or f"{county} County location"
    published = _aware(signal.published_at)
    stype = signal.signal_type if signal.signal_type in HOTSPOT_PRIMARY_TYPES else "mixed"
    hs = Hotspot(
        place_id=signal.place_id,
        label=label,
        county=county,
        town=town,
        lat=float(signal.lat),
        lon=float(signal.lon),
        primary_type=stype,
        score=0.0,
        signals_7d=0,
        signals_30d=0,
        distinct_sources_30d=0,
        confirmed_30d=0,
        needs_review_30d=0,
        first_seen_at=published,
        last_signal_at=published,
        status="active",
    )
    db.add(hs)
    db.flush()
    return hs


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def assign_signal(db: Session, signal: Signal, *, radius_m: float, east_end_radius_m: float, now: dt.datetime,
                  half_life_days: Optional[float] = None) -> Optional[Hotspot]:
    """Attach a mapped (or needs_human_review with coordinates) signal to a hotspot: same place_id, else
    nearest active hotspot within radius (East End towns use east_end_radius_m), else create one labelled from
    the signal's place_label/town. Sets signal.hotspot_id. Self-reports/unlocated/dismissed are never assigned.

    The hotspot is recomputed before returning (half_life_days defaults to HALF_LIFE_DAYS from settings)."""
    if not _assignable(signal):
        return None
    now = _aware(now) or dt.datetime.now(dt.timezone.utc)
    hl = half_life_days if half_life_days is not None else float(get_settings().half_life_days)
    if signal not in db:
        db.add(signal)

    hs: Optional[Hotspot] = None
    if signal.hotspot_id is not None:                   # already a member (e.g. re-assessed after review)
        current = db.get(Hotspot, signal.hotspot_id)
        if current is not None and current.status != "archived":
            hs = current
    lat, lon = float(signal.lat), float(signal.lon)
    if hs is None and signal.place_id is not None:
        same_place = db.scalars(select(Hotspot).where(Hotspot.status == "active",
                                                      Hotspot.place_id == signal.place_id)).all()
        hs = _nearest(same_place, lat, lon, None)
    if hs is None:
        radius = float(east_end_radius_m if _is_east_end(db, signal) else radius_m)
        lo_lat, hi_lat, lo_lon, hi_lon = _bbox(lat, lon, radius)
        near = db.scalars(select(Hotspot).where(
            Hotspot.status == "active",
            Hotspot.lat >= lo_lat, Hotspot.lat <= hi_lat,
            Hotspot.lon >= lo_lon, Hotspot.lon <= hi_lon,
        )).all()
        hs = _nearest(near, lat, lon, radius)
    if hs is None:
        hs = _create_hotspot(db, signal, _place_chain(db, signal.place_id, depth=1))
        if hs is None:
            return None
    signal.hotspot_id = hs.id
    db.flush()
    recompute(db, hs, now=now, half_life_days=hl)
    return hs


def _apply(hs: Hotspot, members: list[Signal], *, now: dt.datetime, half_life_days: float) -> None:
    live = [s for s in members if s.status in ASSIGNABLE_STATUSES]
    d7 = now - dt.timedelta(days=7)
    d30 = now - dt.timedelta(days=30)
    in30 = [s for s in live if (_aware(s.published_at) or now) >= d30]
    sources = {source_key(s) for s in in30}

    weights: dict[str, float] = defaultdict(float)
    total = 0.0
    for s in live:
        w = signal_weight(s, now=now, half_life_days=half_life_days)
        total += w
        if s.signal_type in HOTSPOT_PRIMARY_TYPES:
            weights[s.signal_type] += w

    hs.distinct_sources_30d = len(sources)
    hs.score = round(diversity_multiplier(len(sources)) * total, 6) if live else 0.0
    hs.signals_7d = sum(1 for s in live if (_aware(s.published_at) or now) >= d7)
    hs.signals_30d = len(in30)
    hs.confirmed_30d = sum(1 for s in in30 if s.status == "confirmed")
    hs.needs_review_30d = sum(1 for s in in30 if s.status == "needs_human_review")

    typed_total = sum(weights.values())
    if typed_total > 0:
        top, top_w = max(weights.items(), key=lambda kv: (kv[1], -HOTSPOT_PRIMARY_TYPES.index(kv[0])))
        hs.primary_type = top if top_w / typed_total > PRIMARY_SHARE else "mixed"
    elif live:
        counts: dict[str, int] = defaultdict(int)
        for s in live:
            if s.signal_type in HOTSPOT_PRIMARY_TYPES:
                counts[s.signal_type] += 1
        n = sum(counts.values())
        if n:
            top, top_n = max(counts.items(), key=lambda kv: (kv[1], -HOTSPOT_PRIMARY_TYPES.index(kv[0])))
            hs.primary_type = top if top_n / n > PRIMARY_SHARE else "mixed"
    if hs.primary_type not in HOTSPOT_PRIMARY_TYPES + ("mixed",):
        hs.primary_type = "mixed"

    if live:
        pubs = [_aware(s.published_at) for s in live if s.published_at is not None]
        if pubs:
            hs.last_signal_at = max(pubs)
            first = min(pubs)
            cur_first = _aware(hs.first_seen_at)
            hs.first_seen_at = first if cur_first is None or first < cur_first else cur_first
    if hs.first_seen_at is None:
        hs.first_seen_at = hs.last_signal_at or now
    if hs.last_signal_at is None:
        hs.last_signal_at = hs.first_seen_at
    hs.updated_at = now


def _members_by_hotspot(db: Session, hotspot_ids: list[int]) -> dict[int, list[Signal]]:
    out: dict[int, list[Signal]] = defaultdict(list)
    for i in range(0, len(hotspot_ids), 500):
        chunk = hotspot_ids[i:i + 500]
        for s in db.scalars(select(Signal).where(Signal.hotspot_id.in_(chunk))):
            out[s.hotspot_id].append(s)
    return out


def recompute(db: Session, hotspot: Hotspot, *, now: dt.datetime, half_life_days: float) -> None:
    """Recompute score, counts (7d/30d), distinct_sources_30d (provider+outlet), confirmed_30d,
    needs_review_30d, primary_type (>60% weighted share else 'mixed'), last_signal_at."""
    now = _aware(now) or dt.datetime.now(dt.timezone.utc)
    if hotspot.id is None:
        db.flush()
    members = list(db.scalars(select(Signal).where(Signal.hotspot_id == hotspot.id)))
    _apply(hotspot, members, now=now, half_life_days=half_life_days)


def _archive(hs: Hotspot, now: dt.datetime) -> None:
    """No signal in archive_days: drop staff notes and team (R6); window counts are zero by definition."""
    hs.status = "archived"
    hs.notes = None
    hs.assigned_team = None
    hs.score = 0.0
    hs.signals_7d = hs.signals_30d = hs.distinct_sources_30d = 0
    hs.confirmed_30d = hs.needs_review_30d = 0
    hs.updated_at = now


def recompute_all(db: Session, *, now: dt.datetime, half_life_days: float, archive_days: int) -> int:
    """Refresh every non-archived hotspot (score decays with time); archive those with no signal in
    archive_days (clear notes and assigned_team). Returns number updated."""
    now = _aware(now) or dt.datetime.now(dt.timezone.utc)
    hotspots = list(db.scalars(select(Hotspot).where(Hotspot.status != "archived")))
    if not hotspots:
        return 0
    members = _members_by_hotspot(db, [h.id for h in hotspots])
    cutoff = now - dt.timedelta(days=archive_days)
    archived = 0
    for hs in hotspots:
        _apply(hs, members.get(hs.id, []), now=now, half_life_days=half_life_days)
        last = _aware(hs.last_signal_at)
        if last is not None and last < cutoff:
            _archive(hs, now)
            archived += 1
    db.flush()
    logger.info("hotspots: refreshed %d, archived %d", len(hotspots), archived)
    return len(hotspots)


def archive_stale(db: Session, *, now: dt.datetime, archive_days: int) -> int:
    """Archive non-archived hotspots whose last signal is older than archive_days (notes and team cleared)."""
    now = _aware(now) or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=archive_days)
    stale = list(db.scalars(select(Hotspot).where(Hotspot.status != "archived", Hotspot.last_signal_at < cutoff)))
    for hs in stale:
        _archive(hs, now)
    if stale:
        db.flush()
    return len(stale)


def week_start_local(d: dt.datetime, tz: Optional[str] = None) -> dt.date:
    """Monday (local APP_TIMEZONE date) of the week containing d."""
    zone = ZoneInfo(tz or get_settings().app_timezone)
    local = (_aware(d) or dt.datetime.now(dt.timezone.utc)).astimezone(zone).date()
    return local - dt.timedelta(days=local.weekday())


def rollup_weekly(db: Session, signals: list[Signal]) -> None:
    """Add these signals' counts into hotspot_weekly (week_start = Monday, local) BEFORE they are deleted.
    Only hotspot members that are not dismissed count; n_confirmed counts confirmed ones."""
    tz = get_settings().app_timezone
    agg: dict[tuple[int, dt.date, str], list[int]] = defaultdict(lambda: [0, 0])
    for s in signals:
        if s.hotspot_id is None or s.status not in ASSIGNABLE_STATUSES or s.published_at is None:
            continue
        key = (int(s.hotspot_id), week_start_local(s.published_at, tz), s.signal_type or "unknown")
        agg[key][0] += 1
        if s.status == "confirmed":
            agg[key][1] += 1
    for (hid, week, stype), (n, n_conf) in sorted(agg.items()):
        row = db.get(HotspotWeekly, (hid, week, stype))
        if row is None:
            db.add(HotspotWeekly(hotspot_id=hid, week_start=week, signal_type=stype,
                                 n_signals=n, n_confirmed=n_conf))
        else:
            row.n_signals = (row.n_signals or 0) + n
            row.n_confirmed = (row.n_confirmed or 0) + n_conf
    if agg:
        db.flush()

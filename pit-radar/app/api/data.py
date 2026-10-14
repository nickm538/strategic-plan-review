"""Read and review endpoints (docs/07 §2-3 and the §7 addendum).

Role rules (R7, R11, R12):
  * viewers see only mapped signals (ai_verified, confirmed) and never individual signal coordinates
  * reviewers/admins also see needs_human_review and unlocated; coordinates only for precision public_place
  * self-reports are reviewer-only and never carry coordinates
"""
from __future__ import annotations

import datetime as dt
import logging
from collections import Counter
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app import events as ev
from app.api import serializers as ser
from app.api.common import (
    COUNTIES,
    DEFAULT_SELFREPORT_TEMPLATE,
    KEY_SELFREPORT_TEMPLATE,
    PURPOSE_NOTICE,
    SIGNAL_TYPES,
    check_choice,
    decode_cursor,
    encode_cursor,
    get_state,
    iso,
    parse_bbox,
    window_start,
)
from app.api.deps import Principal, current_principal, get_client_ip, request_now, require_role, role_at_least, writer
from app.audit import audit
from app.db import get_db
from app.models import (
    MAPPED_STATUSES,
    Hotspot,
    HotspotWeekly,
    Review,
    ScanRun,
    Signal,
    User,
)
from app.runtime_settings import KEY_HEALTH_PREFIX, effective, get_value

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", dependencies=[Depends(current_principal)])

REVIEWER_LIST_STATUSES = ("ai_verified", "confirmed", "needs_human_review", "unlocated")
HOTSPOT_VISIBLE = ("active", "visited", "not_found")
# Hotspots backed only by signals awaiting review are not on the viewers' map (docs/05 §6).
VERIFIED_EXPR = Hotspot.signals_30d - Hotspot.needs_review_30d
DISMISS_REASONS = ("not_relevant", "outside_area", "too_old", "duplicate", "wrong_location", "other")
PROVIDERS = ("socialcrawl", "exa", "firecrawl", "anthropic", "nominatim")
PLATFORMS = ("reddit", "x", "tiktok", "instagram", "threads", "bluesky", "facebook", "youtube", "news", "web")


def _is_reviewer(p: Principal) -> bool:
    return role_at_least(p.role, "reviewer")


def _visible_statuses(p: Principal) -> tuple[str, ...]:
    return REVIEWER_LIST_STATUSES if _is_reviewer(p) else MAPPED_STATUSES


def _signal_or_404(db: Session, signal_id: int, p: Principal) -> Signal:
    sig = db.get(Signal, signal_id)
    if sig is None:
        raise HTTPException(404, "Signal not found")
    if not _is_reviewer(p) and sig.status not in MAPPED_STATUSES:
        raise HTTPException(404, "Signal not found")
    if sig.status == "self_report" and not _is_reviewer(p):
        raise HTTPException(404, "Signal not found")
    return sig


def _hotspot_or_404(db: Session, hotspot_id: int) -> Hotspot:
    hs = db.get(Hotspot, hotspot_id)
    if hs is None:
        raise HTTPException(404, "Hotspot not found")
    return hs


# --------------------------------------------------------------------------------------------- hotspots
@router.get("/hotspots")
def list_hotspots(
    request: Request,
    window: Optional[str] = None,
    county: Optional[str] = None,
    type: Optional[str] = None,  # noqa: A002 - API parameter name from docs/07
    status: Optional[str] = None,
    bbox: Optional[str] = None,
    team: Optional[str] = None,
    db: Session = Depends(get_db),
    now: dt.datetime = Depends(request_now),
    p: Principal = Depends(current_principal),
) -> dict[str, Any]:
    start = window_start(window, now)
    county = check_choice(county, COUNTIES, "county")
    htype = check_choice(type, SIGNAL_TYPES + ("mixed",), "type")
    statuses = (check_choice(status, ("active", "visited", "not_found", "resolved", "archived"), "status"),) \
        if status else HOTSPOT_VISIBLE
    q = select(Hotspot).where(Hotspot.status.in_(statuses), Hotspot.last_signal_at >= start)
    if not _is_reviewer(p):
        q = q.where(VERIFIED_EXPR > 0)
    if county:
        q = q.where(Hotspot.county == county)
    if htype:
        q = q.where(Hotspot.primary_type == htype)
    if team:
        q = q.where(Hotspot.assigned_team == team)
    box = parse_bbox(bbox)
    if box:
        q = q.where(Hotspot.lon >= box[0], Hotspot.lat >= box[1], Hotspot.lon <= box[2], Hotspot.lat <= box[3])
    rows = db.scalars(q.order_by(Hotspot.score.desc(), Hotspot.id).limit(1000)).all()
    return {"items": [ser.hotspot_to_dict(h) for h in rows], "updated_at": iso(now)}


@router.get("/hotspots/{hotspot_id}")
def get_hotspot(hotspot_id: int, window: Optional[str] = None, db: Session = Depends(get_db),
                now: dt.datetime = Depends(request_now), p: Principal = Depends(current_principal)) -> dict[str, Any]:
    hs = _hotspot_or_404(db, hotspot_id)
    if not _is_reviewer(p) and ser.verified_count(hs) == 0:
        raise HTTPException(404, "Hotspot not found")
    start = window_start(window, now, default="30d")
    sigs = db.scalars(select(Signal).where(Signal.hotspot_id == hs.id, Signal.status.in_(_visible_statuses(p)),
                                           Signal.published_at >= start)
                      .order_by(Signal.published_at.desc()).limit(200)).all()
    weekly = db.scalars(select(HotspotWeekly).where(HotspotWeekly.hotspot_id == hs.id)
                        .order_by(HotspotWeekly.week_start)).all()
    out = ser.hotspot_to_dict(hs)
    out["signals"] = [ser.signal_to_dict(s, role=p.role) for s in sigs]
    out["weekly"] = [{"week_start": w.week_start.isoformat(), "signal_type": w.signal_type,
                      "n_signals": w.n_signals, "n_confirmed": w.n_confirmed} for w in weekly]
    return out


class HotspotStatusBody(BaseModel):
    status: Literal["visited", "not_found", "resolved", "active"]
    note: Optional[str] = Field(None, max_length=2000)


class AssignBody(BaseModel):
    team: Optional[str] = Field(None, max_length=40)


class NotesBody(BaseModel):
    notes: str = Field("", max_length=2000)


def _check_text(request: Request, text: Optional[str]) -> None:
    if not text:
        return
    from app.pipeline.privacy import check_note
    msg = check_note(text, get_state(request).lexicon)
    if msg:
        raise HTTPException(422, msg)


def _publish_hotspot(request: Request, hs: Hotspot) -> None:
    try:
        for payload, min_role, max_role in ser.hotspot_events(hs):
            get_state(request).events.publish(ev.HOTSPOT_UPDATED, payload, min_role=min_role, max_role=max_role)
    except Exception as exc:  # noqa: BLE001 - SSE is best effort
        logger.warning("hotspot.updated publish failed (%s)", type(exc).__name__)


@router.post("/hotspots/{hotspot_id}/status")
def set_hotspot_status(hotspot_id: int, body: HotspotStatusBody, request: Request, db: Session = Depends(get_db),
                       now: dt.datetime = Depends(request_now), p: Principal = Depends(writer("reviewer"))) -> dict[str, Any]:
    _check_text(request, body.note)
    hs = _hotspot_or_404(db, hotspot_id)
    if hs.status == "archived" and body.status != "active":
        raise HTTPException(409, "This hotspot is archived. Reopen it as active first.")
    old = hs.status
    hs.status = body.status
    hs.updated_at = now
    action = "reopen" if body.status == "active" else body.status
    db.add(Review(object_type="hotspot", object_id=hs.id, user_id=p.user.id, action=action, note=body.note, at=now))
    audit(db, "review", user_id=p.user.id, object_type="hotspot", object_id=hs.id,
          details={"action": action, "from": old}, ip=get_client_ip(request))
    db.commit()
    _publish_hotspot(request, hs)
    return ser.hotspot_to_dict(hs)


@router.post("/hotspots/{hotspot_id}/assign")
def assign_hotspot(hotspot_id: int, body: AssignBody, request: Request, db: Session = Depends(get_db),
                   now: dt.datetime = Depends(request_now), p: Principal = Depends(writer("reviewer"))) -> dict[str, Any]:
    team = (body.team or "").strip() or None
    _check_text(request, team)
    hs = _hotspot_or_404(db, hotspot_id)
    hs.assigned_team = team
    hs.updated_at = now
    db.add(Review(object_type="hotspot", object_id=hs.id, user_id=p.user.id, action="assign", note=team, at=now))
    audit(db, "review", user_id=p.user.id, object_type="hotspot", object_id=hs.id,
          details={"action": "assign", "team": team}, ip=get_client_ip(request))
    db.commit()
    _publish_hotspot(request, hs)
    return ser.hotspot_to_dict(hs)


@router.put("/hotspots/{hotspot_id}/notes")
def set_hotspot_notes(hotspot_id: int, body: NotesBody, request: Request, db: Session = Depends(get_db),
                      now: dt.datetime = Depends(request_now), p: Principal = Depends(writer("reviewer"))) -> dict[str, Any]:
    notes = body.notes.strip()
    _check_text(request, notes)
    hs = _hotspot_or_404(db, hotspot_id)
    hs.notes = notes or None
    hs.updated_at = now
    audit(db, "review", user_id=p.user.id, object_type="hotspot", object_id=hs.id,
          details={"action": "notes", "length": len(notes)}, ip=get_client_ip(request))
    db.commit()
    _publish_hotspot(request, hs)
    return ser.hotspot_to_dict(hs)


# ---------------------------------------------------------------------------------------------- signals
@router.get("/signals")
def list_signals(
    window: Optional[str] = None,
    county: Optional[str] = None,
    type: Optional[str] = None,  # noqa: A002
    status: Optional[str] = None,
    min_conf: Optional[float] = Query(None, ge=0, le=1),
    source: Optional[str] = None,
    hotspot_id: Optional[int] = None,
    bbox: Optional[str] = None,
    cursor: Optional[str] = None,
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    now: dt.datetime = Depends(request_now),
    p: Principal = Depends(current_principal),
) -> dict[str, Any]:
    start = window_start(window, now)
    visible = _visible_statuses(p)
    if status:
        allowed = visible + (("dismissed",) if _is_reviewer(p) else ())
        statuses: tuple[str, ...] = (check_choice(status, allowed, "status"),)  # type: ignore[assignment]
    else:
        statuses = visible
    q = select(Signal).where(Signal.status.in_(statuses), Signal.published_at >= start)
    if (county := check_choice(county, COUNTIES, "county")):
        q = q.where(Signal.county == county)
    if (stype := check_choice(type, SIGNAL_TYPES, "type")):
        q = q.where(Signal.signal_type == stype)
    if min_conf is not None:
        q = q.where(Signal.ai_confidence >= min_conf)
    if (platform := check_choice(source, PLATFORMS, "source")):
        q = q.where(Signal.platform == platform)
    if hotspot_id is not None:
        q = q.where(Signal.hotspot_id == hotspot_id)
    box = parse_bbox(bbox)
    if box:
        q = q.join(Hotspot, Hotspot.id == Signal.hotspot_id).where(
            Hotspot.lon >= box[0], Hotspot.lat >= box[1], Hotspot.lon <= box[2], Hotspot.lat <= box[3])
    cur = decode_cursor(cursor)
    if cur:
        when, rid = cur
        q = q.where(or_(Signal.published_at < when, and_(Signal.published_at == when, Signal.id < rid)))
    rows = db.scalars(q.order_by(Signal.published_at.desc(), Signal.id.desc()).limit(limit + 1)).all()
    more = len(rows) > limit
    rows = rows[:limit]
    nxt = encode_cursor(rows[-1].published_at, rows[-1].id) if more and rows else None
    return {"items": [ser.signal_to_dict(s, role=p.role) for s in rows], "next_cursor": nxt}


@router.get("/signals/{signal_id}")
def get_signal(signal_id: int, db: Session = Depends(get_db), p: Principal = Depends(current_principal)) -> dict[str, Any]:
    return ser.signal_to_dict(_signal_or_404(db, signal_id, p), role=p.role)


class ReviewBody(BaseModel):
    action: Literal["confirm", "dismiss", "needs_followup", "reopen"]
    reason: Optional[Literal["not_relevant", "outside_area", "too_old", "duplicate", "wrong_location", "other"]] = None
    note: Optional[str] = Field(None, max_length=2000)


@router.post("/signals/{signal_id}/review")
def review_signal(signal_id: int, body: ReviewBody, request: Request, db: Session = Depends(get_db),
                  now: dt.datetime = Depends(request_now), p: Principal = Depends(writer("reviewer"))) -> dict[str, Any]:
    from app.pipeline import hotspots as hotspots_mod

    _check_text(request, body.note)
    sig = _signal_or_404(db, signal_id, p)
    if sig.status == "self_report":
        raise HTTPException(409, "Self-reports are handled on the Self-reports screen.")
    old = sig.status
    if body.action == "confirm":
        if sig.lat is None or sig.lon is None:
            raise HTTPException(409, "This signal has no mappable location, so it cannot be confirmed onto the map.")
        sig.status = "confirmed"
    elif body.action == "dismiss":
        if body.reason is None:
            raise HTTPException(422, "Choose a reason to dismiss.")
        sig.status = "dismissed"
    elif body.action == "reopen":
        if old not in ("confirmed", "dismissed"):
            raise HTTPException(409, "Only confirmed or dismissed signals can be reopened.")
        has_verdict = sig.signal_type != "unknown" and sig.lat is not None
        sig.status = "ai_verified" if has_verdict and not sig.review_reason else "needs_human_review"
    sig.reviewed_by = p.user.id
    sig.reviewed_at = now
    db.add(Review(object_type="signal", object_id=sig.id, user_id=p.user.id, action=body.action,
                  reason=body.reason if body.action == "dismiss" else None, note=body.note, at=now))
    audit(db, "review", user_id=p.user.id, object_type="signal", object_id=sig.id,
          details={"action": body.action, "from": old, "to": sig.status, "reason": body.reason},
          ip=get_client_ip(request))
    settings = get_state(request).settings
    hs: Optional[Hotspot] = None
    if sig.hotspot_id is not None:
        hs = db.get(Hotspot, sig.hotspot_id)
    elif sig.status == "confirmed":
        hs = hotspots_mod.assign_signal(db, sig, radius_m=settings.hotspot_radius_m,
                                        east_end_radius_m=settings.hotspot_radius_east_end_m, now=now)
    db.flush()
    if hs is not None:
        if hs.status == "archived" and sig.status == "confirmed":
            hs.status = "active"
        hotspots_mod.recompute(db, hs, now=now, half_life_days=settings.half_life_days)
    db.commit()
    _publish_signal_update(request, sig, hs)
    return ser.signal_to_dict(sig, role=p.role)


def _publish_signal_update(request: Request, sig: Signal, hs: Optional[Hotspot]) -> None:
    try:
        bus = get_state(request).events
        bus.publish(ev.SIGNAL_UPDATED, ser.signal_to_dict(sig, role="reviewer"), min_role="reviewer")
        if sig.status in MAPPED_STATUSES:
            viewer = ser.signal_to_dict(sig, role="viewer")
            bus.publish(ev.SIGNAL_UPDATED, viewer, min_role="viewer", max_role="viewer")
        else:  # tell viewers to drop it if they had it
            bus.publish(ev.SIGNAL_UPDATED, {"id": int(sig.id), "status": sig.status, "removed": True,
                                            "hotspot_id": sig.hotspot_id}, min_role="viewer", max_role="viewer")
        if hs is not None:
            for payload, min_role, max_role in ser.hotspot_events(hs):
                bus.publish(ev.HOTSPOT_UPDATED, payload, min_role=min_role, max_role=max_role)
    except Exception as exc:  # noqa: BLE001
        logger.warning("signal.updated publish failed (%s)", type(exc).__name__)


@router.get("/review-queue")
def review_queue(limit: int = Query(200, ge=1, le=500), db: Session = Depends(get_db),
                 p: Principal = Depends(require_role("reviewer"))) -> dict[str, Any]:
    cond = or_(Signal.status == "needs_human_review",
               and_(Signal.status == "ai_verified", Signal.reviewed_at.is_(None)))
    total = db.scalar(select(func.count()).select_from(Signal).where(cond)) or 0
    rows = db.scalars(select(Signal).where(cond).order_by(Signal.published_at.asc(), Signal.id.asc())
                      .limit(limit)).all()
    return {"items": [ser.signal_to_dict(s, role=p.role) for s in rows], "total": int(total)}


# ----------------------------------------------------------------------------------------- self-reports
def _selfreport_template(db: Session) -> str:
    v = get_value(db, KEY_SELFREPORT_TEMPLATE)
    return v if isinstance(v, str) and v.strip() else DEFAULT_SELFREPORT_TEMPLATE


@router.get("/self-reports")
def self_reports(window: Optional[str] = None, db: Session = Depends(get_db), now: dt.datetime = Depends(request_now),
                 p: Principal = Depends(require_role("reviewer"))) -> dict[str, Any]:
    start = window_start(window, now, default="30d")
    rows = db.scalars(select(Signal).where(Signal.status == "self_report", Signal.published_at >= start)
                      .order_by(Signal.published_at.desc()).limit(300)).all()
    responded = set(db.scalars(select(Review.object_id).where(
        Review.object_type == "signal", Review.action == "responded",
        Review.object_id.in_([s.id for s in rows] or [-1]))).all())
    template = _selfreport_template(db)
    items = [{"id": int(s.id), "town": s.town, "county": s.county, "published_at": iso(s.published_at),
              "neutral_summary": s.neutral_summary, "url": s.url, "platform": s.platform,
              "suggested_reply": template, "responded": int(s.id) in responded} for s in rows]
    return {"items": items, "template_needs_review": "verify]" in template}


@router.post("/self-reports/{signal_id}/responded")
def mark_responded(signal_id: int, request: Request, db: Session = Depends(get_db),
                   now: dt.datetime = Depends(request_now), p: Principal = Depends(writer("reviewer"))) -> dict[str, Any]:
    sig = db.get(Signal, signal_id)
    if sig is None or sig.status != "self_report":
        raise HTTPException(404, "Self-report not found")
    db.add(Review(object_type="signal", object_id=sig.id, user_id=p.user.id, action="responded", at=now))
    audit(db, "review", user_id=p.user.id, object_type="signal", object_id=sig.id, details={"action": "responded"},
          ip=get_client_ip(request))
    db.commit()
    return {"id": int(sig.id), "responded": True}


# ------------------------------------------------------------------------------------------ stats, scans
def _health_records(db: Session) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for prov in PROVIDERS:
        rec = get_value(db, KEY_HEALTH_PREFIX + prov)
        out[prov] = rec if isinstance(rec, dict) else {"provider": prov, "status": "unknown", "message": "Not checked yet"}
    return out


def _scan_dict(r: ScanRun) -> dict[str, Any]:
    return {"id": r.id, "started_at": iso(r.started_at), "finished_at": iso(r.finished_at), "trigger": r.trigger,
            "mode": r.mode, "status": r.status, "stage_counts": r.stage_counts or {},
            "drop_reasons": r.drop_reasons or {}, "errors": (r.errors or [])[:20], "usage": r.usage or {}}


@router.get("/stats")
def stats(request: Request, db: Session = Depends(get_db), now: dt.datetime = Depends(request_now),
          p: Principal = Depends(current_principal)) -> dict[str, Any]:
    from app.pipeline.usage import usage_summary

    reviewer = _is_reviewer(p)
    active = db.scalars(select(Hotspot).where(Hotspot.status.in_(HOTSPOT_VISIBLE), VERIFIED_EXPR > 0,
                                              Hotspot.last_signal_at >= now - dt.timedelta(days=7))).all()
    by_county = Counter(h.county for h in active)
    by_type = Counter(h.primary_type for h in active)
    new_24h = db.scalar(select(func.count()).select_from(Signal).where(
        Signal.status.in_(_visible_statuses(p)), Signal.collected_at >= now - dt.timedelta(hours=24))) or 0
    unreviewed = None
    if reviewer:
        unreviewed = db.scalar(select(func.count()).select_from(Signal).where(
            or_(Signal.status == "needs_human_review",
                and_(Signal.status == "ai_verified", Signal.reviewed_at.is_(None))))) or 0
    finished = ("ok", "partial", "failed")
    last = db.scalars(select(ScanRun).where(ScanRun.status.in_(finished)).order_by(ScanRun.id.desc()).limit(1)).first()
    week = db.scalars(select(ScanRun).where(ScanRun.started_at >= now - dt.timedelta(days=7),
                                            ScanRun.status.in_(finished))).all()
    funnel_7d: Counter[str] = Counter()
    drops_7d: Counter[str] = Counter()
    for r in week:
        funnel_7d.update({k: int(v) for k, v in (r.stage_counts or {}).items() if isinstance(v, (int, float))})
        drops_7d.update({k: int(v) for k, v in (r.drop_reasons or {}).items() if isinstance(v, (int, float))})
    eff = effective(db, now)
    state = get_state(request)
    out: dict[str, Any] = {
        "active_hotspots_7d": len(active),
        "new_signals_24h": int(new_24h),
        "unreviewed": unreviewed,
        "by_county": {c: by_county.get(c, 0) for c in COUNTIES},
        "by_type": {t: by_type.get(t, 0) for t in SIGNAL_TYPES + ("mixed",)},
        "last_scan_at": iso(last.started_at) if last else None,
        "last_scan_status": last.status if last else None,
        "next_scan_at": iso(state.scheduler.next_scan_at),
        "mode": eff.mode,
        "scans_paused": eff.scans_paused,
        "pit_mode_start": eff.get("pit_mode_start") or None,
        "pit_mode_end": eff.get("pit_mode_end") or None,
        "providers": {k: {"status": v.get("status"), "message": v.get("message")} for k, v in _health_records(db).items()},
        "funnel_last_scan": (last.stage_counts or {}) if last else {},
        "funnel_7d": dict(funnel_7d),
        "drop_reasons_7d": dict(drops_7d),
        "geo": {"state": state.geo_status.get("state"), "message": state.geo_status.get("message")},
    }
    if reviewer:
        out["usage"] = usage_summary(db, now=now)
    return out


@router.get("/scans")
def scans(limit: int = Query(20, ge=1, le=100), db: Session = Depends(get_db),
          p: Principal = Depends(require_role("reviewer"))) -> dict[str, Any]:
    rows = db.scalars(select(ScanRun).order_by(ScanRun.id.desc()).limit(limit)).all()
    return {"items": [_scan_dict(r) for r in rows]}


@router.get("/health")
def health(request: Request, db: Session = Depends(get_db), now: dt.datetime = Depends(request_now),
           p: Principal = Depends(current_principal)) -> dict[str, Any]:
    state = get_state(request)
    eff = effective(db, now)
    sched = getattr(request.app.state, "scheduler", None)
    sched_status = sched.status() if sched is not None and hasattr(sched, "status") else {
        "running": state.scheduler.running, "current_scan_id": state.scheduler.current_scan_id,
        "last_scan_at": iso(state.scheduler.last_scan_at), "last_scan_status": state.scheduler.last_scan_status,
        "next_scan_at": iso(state.scheduler.next_scan_at)}
    if not sched_status.get("last_scan_at"):  # after a restart the in-memory status is empty; use the DB
        last = db.scalars(select(ScanRun).where(ScanRun.status.in_(("ok", "partial", "failed")))
                          .order_by(ScanRun.id.desc()).limit(1)).first()
        if last is not None:
            sched_status = {**sched_status, "last_scan_at": iso(last.started_at), "last_scan_status": last.status}
    return {"providers": _health_records(db), "geo": dict(state.geo_status), "scheduler": sched_status,
            "mode": eff.mode, "scans_paused": eff.scans_paused}


# --------------------------------------------------------------------------------- places, geo, config
@router.get("/places/search")
def place_search(request: Request, q: str = Query("", max_length=80)) -> dict[str, Any]:
    gaz = get_state(request).gazetteer
    if gaz is None or not q.strip():
        return {"items": []}
    items = []
    for rec in gaz.search(q.strip(), limit=10):
        items.append({"id": rec.id, "name": rec.name, "label": rec.label, "kind": rec.kind, "county": rec.county,
                      "town": rec.town, "lat": rec.lat, "lon": rec.lon})
    return {"items": items}


@router.get("/geo/counties")
def geo_counties(request: Request) -> dict[str, Any]:
    state = get_state(request)
    if state.geofence is None:
        return {"type": "FeatureCollection", "features": [], "ready": False}
    cached = getattr(state, "_counties_geojson", None)
    if cached is None or cached[0] is not state.geofence:
        gj = state.geofence.to_geojson()
        cached = (state.geofence, gj)
        setattr(state, "_counties_geojson", cached)
    return {**cached[1], "ready": True}


LABEL_KINDS = ("town", "city", "village", "cdp")


@router.get("/geo/labels")
def geo_labels(request: Request) -> dict[str, Any]:
    gaz = get_state(request).gazetteer
    if gaz is None:
        return {"items": []}
    items = [{"name": r.name, "kind": r.kind, "county": r.county, "lat": r.lat, "lon": r.lon}
             for r in gaz.places if r.kind in LABEL_KINDS]
    return {"items": items}


@router.get("/config/client")
def config_client(request: Request, db: Session = Depends(get_db), now: dt.datetime = Depends(request_now),
                  p: Principal = Depends(current_principal)) -> dict[str, Any]:
    state = get_state(request)
    s = state.settings
    bounds = None
    if state.geofence is not None:
        try:
            b = state.geofence.bbox()
            bounds = [[b[0], b[1]], [b[2], b[3]]]
        except Exception:  # noqa: BLE001
            bounds = None
    eff = effective(db, now)
    return {
        "map": {
            "bounds": bounds or [[-73.78, 40.54], [-71.85, 41.30]],
            "pitch": 50, "bearing": -17,
            "style_url": s.map_style_url or None,
            "tile_hosts": s.tile_hosts,
            "vector_tiles": "https://tiles.openfreemap.org/planet",
            # Verified Oct 2026: OpenFreeMap publishes these styles with no key (openfreemap.org/quick_start).
            "styles": {"streets": "https://tiles.openfreemap.org/styles/liberty",
                       "dark": "https://tiles.openfreemap.org/styles/dark"},
            # New York State orthoimagery (2022-2025), Web Mercator, ArcGIS export as 512 px tiles.
            "aerial": {"tiles": ["https://orthos.its.ny.gov/arcgis/rest/services/wms/Latest/MapServer/export"
                                 "?bbox={bbox-epsg-3857}&bboxSR=3857&imageSR=3857&size=512,512&format=jpg"
                                 "&transparent=false&f=image"],
                       "attribution": "Aerial imagery: NYS ITS Geospatial Services"},
        },
        "banner": PURPOSE_NOTICE,
        "pit_mode": eff.pit_mode,
        "pit_mode_start": eff.get("pit_mode_start") or None,
        "pit_mode_end": eff.get("pit_mode_end") or None,
        "role": p.role,
        "user": p.user.name,
        "self_report_template": _selfreport_template(db) if _is_reviewer(p) else None,
        "dismiss_reasons": list(DISMISS_REASONS),
    }


@router.get("/teams")
def teams(db: Session = Depends(get_db), p: Principal = Depends(current_principal)) -> dict[str, Any]:
    names = db.scalars(select(Hotspot.assigned_team).where(Hotspot.assigned_team.is_not(None)).distinct()).all()
    return {"items": sorted(n for n in names if n)}


@router.get("/users/names")
def user_names(db: Session = Depends(get_db), p: Principal = Depends(require_role("reviewer"))) -> dict[str, Any]:
    """Reviewer names for the review history display (no roles, no other fields)."""
    rows = db.execute(select(User.id, User.name)).all()
    return {"items": [{"id": r.id, "name": r.name} for r in rows]}

"""Exports (docs/07 §4). Hotspot-level only: no post URLs, quotes or signal text (R2-R4). Every file carries the
R9 notice and every export writes an audit row (R10)."""
from __future__ import annotations

import csv
import datetime as dt
import html
import io
import json
import math
from typing import Any, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.common import COUNTIES, PURPOSE_NOTICE, SIGNAL_TYPES, check_choice, window_start
from app.api.deps import Principal, current_principal, get_client_ip, request_now
from app.audit import audit
from app.db import get_db
from app.models import Hotspot
from app.security import rate_limit_export

NY = ZoneInfo("America/New_York")

router = APIRouter(prefix="/api/export", dependencies=[Depends(current_principal)])

EXPORT_STATUSES = ("active", "visited", "not_found")
TYPE_LABELS = {"encampment": "Encampment", "individual_unsheltered": "Person(s) sleeping outside",
               "vehicle_dwelling": "Vehicle dwelling", "mixed": "Mixed"}
STATUS_LABELS = {"active": "Active", "visited": "Visited", "not_found": "Not found", "resolved": "Resolved",
                 "archived": "Archived"}


def _hotspots(db: Session, *, window: Optional[str], county: Optional[str], htype: Optional[str],
              team: Optional[str], now: dt.datetime) -> list[Hotspot]:
    # Only hotspots with AI-verified or staff-confirmed signals: unreviewed items never go out on route sheets.
    q = select(Hotspot).where(Hotspot.status.in_(EXPORT_STATUSES),
                              Hotspot.signals_30d - Hotspot.needs_review_30d > 0,
                              Hotspot.last_signal_at >= window_start(window, now, default="30d"))
    if county:
        q = q.where(Hotspot.county == county)
    if htype:
        q = q.where(Hotspot.primary_type == htype)
    if team:
        q = q.where(Hotspot.assigned_team == team)
    return list(db.scalars(q.order_by(Hotspot.score.desc(), Hotspot.id)).all())


def _row(h: Hotspot) -> dict[str, Any]:
    return {
        "id": h.id, "label": h.label, "town": h.town or "", "county": h.county,
        "lat": round(float(h.lat), 5), "lon": round(float(h.lon), 5),
        "type": TYPE_LABELS.get(h.primary_type, h.primary_type), "status": STATUS_LABELS.get(h.status, h.status),
        "score": round(float(h.score or 0), 2), "signals_7d": h.signals_7d, "signals_30d": h.signals_30d,
        "independent_sources_30d": h.distinct_sources_30d, "staff_confirmed_30d": h.confirmed_30d,
        "last_signal_at": _ny(h.last_signal_at),
        "team": h.assigned_team or "",
    }


def _ny(d: dt.datetime) -> str:
    """Local New York time (EST/EDT shown explicitly), the time zone staff work in."""
    return d.astimezone(NY).strftime("%Y-%m-%d %I:%M %p %Z")


def _audit(db: Session, request: Request, p: Principal, fmt: str, filters: dict[str, Any], n: int) -> None:
    audit(db, "export", user_id=p.user.id, object_type="hotspots", details={"format": fmt, "filters": filters,
          "rows": n}, ip=get_client_ip(request))
    db.commit()


def _filename(stem: str, now: dt.datetime, ext: str) -> str:
    return f"pit-radar-{stem}-{now.astimezone(NY).strftime('%Y%m%d-%H%M')}.{ext}"


@router.get("/hotspots.geojson")
def export_geojson(request: Request, window: Optional[str] = None, county: Optional[str] = None,
                   type: Optional[str] = None, team: Optional[str] = None,  # noqa: A002
                   db: Session = Depends(get_db), now: dt.datetime = Depends(request_now),
                   p: Principal = Depends(rate_limit_export())) -> Response:
    county = check_choice(county, COUNTIES, "county")
    htype = check_choice(type, SIGNAL_TYPES + ("mixed",), "type")
    rows = _hotspots(db, window=window, county=county, htype=htype, team=team, now=now)
    fc = {"type": "FeatureCollection", "notice": PURPOSE_NOTICE, "generated_at": now.astimezone(NY).isoformat(),
          "features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
                        "properties": {**{k: v for k, v in r.items() if k not in ("lat", "lon")},
                                       "notice": PURPOSE_NOTICE}}
                       for r in map(_row, rows)]}
    _audit(db, request, p, "geojson", {"window": window, "county": county, "type": htype, "team": team}, len(rows))
    return Response(json.dumps(fc, indent=1), media_type="application/geo+json", headers={
        "Content-Disposition": f'attachment; filename="{_filename("hotspots", now, "geojson")}"',
        "X-Purpose-Notice": PURPOSE_NOTICE})


@router.get("/hotspots.csv")
def export_csv(request: Request, window: Optional[str] = None, county: Optional[str] = None,
               type: Optional[str] = None, team: Optional[str] = None,  # noqa: A002
               db: Session = Depends(get_db), now: dt.datetime = Depends(request_now),
               p: Principal = Depends(rate_limit_export())) -> Response:
    county = check_choice(county, COUNTIES, "county")
    htype = check_choice(type, SIGNAL_TYPES + ("mixed",), "type")
    rows = sorted(map(_row, _hotspots(db, window=window, county=county, htype=htype, team=team, now=now)),
                  key=lambda r: (r["county"], r["town"], -r["score"]))
    buf = io.StringIO()
    buf.write(f"# {PURPOSE_NOTICE}\n")
    cols = ["town", "county", "label", "type", "status", "score", "signals_7d", "signals_30d",
            "independent_sources_30d", "staff_confirmed_30d", "last_signal_at", "team", "lat", "lon", "id"]
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: _csv_safe(v) for k, v in r.items()})
    _audit(db, request, p, "csv", {"window": window, "county": county, "type": htype, "team": team}, len(rows))
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{_filename("hotspots", now, "csv")}"',
        "X-Purpose-Notice": PURPOSE_NOTICE})


def _csv_safe(v: Any) -> Any:
    """Neutralize spreadsheet formula injection in text cells."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def _haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def nearest_neighbor_order(points: list[Hotspot]) -> list[Hotspot]:
    """Simple tour: start at the highest-score hotspot, then always go to the nearest unvisited one."""
    if not points:
        return []
    remaining = sorted(points, key=lambda h: (-float(h.score or 0), h.id))
    route = [remaining.pop(0)]
    while remaining:
        cur = (route[-1].lat, route[-1].lon)
        nxt = min(remaining, key=lambda h: (_haversine_m(cur, (h.lat, h.lon)), h.id))
        remaining.remove(nxt)
        route.append(nxt)
    return route


@router.get("/route-sheet", response_class=HTMLResponse)
def route_sheet(request: Request, team: Optional[str] = None, window: Optional[str] = None,
                county: Optional[str] = None, db: Session = Depends(get_db), now: dt.datetime = Depends(request_now),
                p: Principal = Depends(rate_limit_export())) -> HTMLResponse:
    county = check_choice(county, COUNTIES, "county")
    rows = nearest_neighbor_order(_hotspots(db, window=window, county=county, htype=None, team=team, now=now))
    e = html.escape
    title = f"Route sheet - {team}" if team else "Route sheet - all hotspots"
    total_m = sum(_haversine_m((a.lat, a.lon), (b.lat, b.lon)) for a, b in zip(rows, rows[1:]))
    items = []
    for i, h in enumerate(rows, 1):
        r = _row(h)
        items.append(
            "<li class='stop'>"
            f"<div class='num'>{i}</div><div class='body'><h2>{e(r['label'])}</h2>"
            f"<p class='meta'>{e(r['town'])}{', ' if r['town'] else ''}{e(r['county'])} County · {e(r['type'])} · "
            f"status {e(r['status'])}</p>"
            f"<p class='meta'>{r['signals_30d']} signal(s) in 30 days · {r['independent_sources_30d']} independent "
            f"source(s) · {r['staff_confirmed_30d']} staff-confirmed · last signal {e(r['last_signal_at'])}</p>"
            f"<p class='coords'>{r['lat']:.5f}, {r['lon']:.5f}</p>"
            "<div class='notes'><span>Visited:</span> ☐ yes ☐ no ☐ not found &nbsp; Count: ______ &nbsp; "
            "Notes (place and situation only): ____________________________</div></div></li>")
    body = "\n".join(items) or "<li class='empty'>No hotspots match this filter.</li>"
    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>{e(title)}</title>
<link rel="stylesheet" href="/static/css/route-sheet.css"></head>
<body><div class="notice" role="note">{e(PURPOSE_NOTICE)}</div>
<header><h1>{e(title)}</h1><p>PIT Radar · NY-603 · generated {e(now.astimezone(NY).strftime('%b %d, %Y %I:%M %p %Z'))} ·
{len(rows)} stop(s) · about {total_m / 1609.34:.1f} miles straight-line between stops</p></header>
<ol class="route">{body}</ol>
<footer>{e(PURPOSE_NOTICE)}</footer></body></html>"""
    _audit(db, request, p, "route_sheet", {"team": team, "window": window, "county": county}, len(rows))
    return HTMLResponse(doc, headers={"X-Purpose-Notice": PURPOSE_NOTICE,
                                      "Content-Security-Policy": "default-src 'none'; style-src 'self'; img-src 'self'; "
                                                                 "frame-ancestors 'none'; base-uri 'none'"})

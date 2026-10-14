"""Admin endpoints (docs/07 §3 and §7 addendum). Every change is audited (R10)."""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Literal, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.common import DEFAULT_SELFREPORT_TEMPLATE, KEY_SELFREPORT_TEMPLATE, body_dict, get_state, iso
from app.api.deps import Principal, get_client_ip, request_now, require_role, writer
from app.audit import audit
from app.auth import find_user_by_name, hash_password, revoke_user_sessions, validate_new_password
from app.db import get_db
from app.models import ROLES, AuditLog, Setting, User
from app.runtime_settings import (
    KEY_LEXICON_OVERRIDES,
    KEY_SC_ENDPOINTS,
    KEY_SOURCES_OVERRIDES,
    OVERRIDABLE,
    PIT_MODE_MANUAL_KEY,
    SCANS_PAUSED_KEY,
    effective,
    get_value,
    set_value,
    validate_override,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", dependencies=[Depends(require_role("admin"))])

SWITCH_KEYS = (SCANS_PAUSED_KEY, PIT_MODE_MANUAL_KEY)
MAX_TEMPLATE_CHARS = 1500


# ------------------------------------------------------------------------------------------- settings
@router.get("/admin/settings")
def get_settings_view(db: Session = Depends(get_db), now: dt.datetime = Depends(request_now)) -> dict[str, Any]:
    eff = effective(db, now)
    values = {env_key: eff.get(attr) for env_key, (attr, _p) in OVERRIDABLE.items()}
    template = get_value(db, KEY_SELFREPORT_TEMPLATE)
    return {
        "effective": values,
        "overrides": eff.overrides,
        "overridable": list(OVERRIDABLE) + list(SWITCH_KEYS) + [KEY_SELFREPORT_TEMPLATE],
        "pit_mode": eff.pit_mode,
        "pit_mode_manual": bool(get_value(db, PIT_MODE_MANUAL_KEY, False)),
        "scans_paused": eff.scans_paused,
        "selfreport_reply_template": template if isinstance(template, str) and template.strip()
        else DEFAULT_SELFREPORT_TEMPLATE,
    }


@router.put("/admin/settings")
def put_settings(request: Request, payload: dict[str, Any] = Body(...), db: Session = Depends(get_db),
                 now: dt.datetime = Depends(request_now), p: Principal = Depends(writer("admin"))) -> dict[str, Any]:
    payload = body_dict(payload)
    errors: dict[str, str] = {}
    parsed: dict[str, Any] = {}
    for key, value in payload.items():
        if key == KEY_SELFREPORT_TEMPLATE:
            if value is None:
                parsed[key] = None
            elif not isinstance(value, str) or not value.strip() or len(value) > MAX_TEMPLATE_CHARS:
                errors[key] = f"must be text up to {MAX_TEMPLATE_CHARS} characters"
            else:
                parsed[key] = value.strip()
            continue
        if value is None:
            if key in OVERRIDABLE or key in SWITCH_KEYS:
                parsed[key] = None
            else:
                errors[key] = "unknown setting"
            continue
        try:
            parsed[key] = validate_override(key, value)
        except KeyError:
            errors[key] = "unknown setting"
        except (ValueError, TypeError) as exc:
            errors[key] = str(exc) or "invalid value"
    if errors:
        return JSONResponse(status_code=422, content={"detail": "Some settings are invalid.", "errors": errors})
    lo = parsed.get("MIN_CONFIDENCE_REVIEW")
    hi = parsed.get("MIN_CONFIDENCE_MAP")
    eff = effective(db, now)
    lo = eff.get("min_confidence_review") if lo is None and "MIN_CONFIDENCE_REVIEW" not in parsed else lo
    hi = eff.get("min_confidence_map") if hi is None and "MIN_CONFIDENCE_MAP" not in parsed else hi
    if lo is not None and hi is not None and float(lo) > float(hi):
        return JSONResponse(status_code=422, content={
            "detail": "MIN_CONFIDENCE_REVIEW must not be higher than MIN_CONFIDENCE_MAP.",
            "errors": {"MIN_CONFIDENCE_REVIEW": "must be <= MIN_CONFIDENCE_MAP"}})
    for key, value in parsed.items():
        if value is None:
            row = db.get(Setting, key)
            if row is not None:
                db.delete(row)
        else:
            set_value(db, key, value, user_id=p.user.id)
    audit(db, "admin_change", user_id=p.user.id, object_type="settings", object_id=",".join(sorted(parsed)),
          details={"changes": {k: v for k, v in parsed.items() if k != KEY_SELFREPORT_TEMPLATE}},
          ip=get_client_ip(request))
    db.commit()
    sched = getattr(request.app.state, "scheduler", None)
    if sched is not None and hasattr(sched, "_wake"):
        sched._wake.set()  # re-evaluate the scan schedule now (interval or pause may have changed)
    return get_settings_view(db=db, now=now)


# ---------------------------------------------------------------------------------------------- users
class NewUser(BaseModel):
    name: str = Field(..., min_length=2, max_length=80)
    role: Literal["viewer", "reviewer", "admin"]
    password: str = Field(..., max_length=256)


class PatchUser(BaseModel):
    role: Optional[Literal["viewer", "reviewer", "admin"]] = None
    active: Optional[bool] = None
    new_password: Optional[str] = Field(None, max_length=256)


def _user_dict(u: User) -> dict[str, Any]:
    return {"id": u.id, "name": u.name, "role": u.role, "active": u.active, "created_at": iso(u.created_at),
            "last_login_at": iso(u.last_login_at), "locked": bool(u.locked_until and u.locked_until > dt.datetime.now(dt.timezone.utc))}


def _active_admins(db: Session) -> int:
    return int(db.scalar(select(func.count()).select_from(User).where(User.role == "admin", User.active.is_(True))) or 0)


@router.get("/admin/users")
def list_users(db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"items": [_user_dict(u) for u in db.scalars(select(User).order_by(User.name)).all()]}


@router.post("/admin/users", status_code=201)
def create_user(body: NewUser, request: Request, db: Session = Depends(get_db),
                now: dt.datetime = Depends(request_now), p: Principal = Depends(writer("admin"))) -> dict[str, Any]:
    name = body.name.strip()
    if find_user_by_name(db, name) is not None:
        raise HTTPException(409, "A user with that name already exists.")
    err = validate_new_password(body.password, name=name)
    if err:
        raise HTTPException(422, err)
    u = User(name=name, role=body.role, password_hash=hash_password(body.password), active=True,
             created_at=now, password_changed_at=now)
    db.add(u)
    db.flush()
    audit(db, "admin_change", user_id=p.user.id, object_type="user", object_id=u.id,
          details={"action": "create", "name": name, "role": body.role}, ip=get_client_ip(request))
    db.commit()
    return _user_dict(u)


@router.patch("/admin/users/{user_id}")
def patch_user(user_id: int, body: PatchUser, request: Request, db: Session = Depends(get_db),
               now: dt.datetime = Depends(request_now), p: Principal = Depends(writer("admin"))) -> dict[str, Any]:
    u = db.get(User, user_id)
    if u is None:
        raise HTTPException(404, "User not found")
    losing_admin = u.role == "admin" and u.active and (
        (body.role is not None and body.role != "admin") or body.active is False)
    if losing_admin and _active_admins(db) <= 1:
        raise HTTPException(409, "This is the last active admin. Make another admin first.")
    changes: dict[str, Any] = {}
    revoke = False
    if body.role is not None and body.role != u.role:
        changes["role"] = [u.role, body.role]
        revoke = revoke or ROLES.index(body.role) < ROLES.index(u.role)
        u.role = body.role
    if body.active is not None and body.active != u.active:
        changes["active"] = body.active
        u.active = body.active
        revoke = revoke or not body.active
    if body.new_password:
        err = validate_new_password(body.new_password, name=u.name)
        if err:
            raise HTTPException(422, err)
        u.password_hash = hash_password(body.new_password)
        u.password_changed_at = now
        u.failed_login_count = 0
        u.locked_until = None
        changes["password"] = "reset"
        revoke = True
    if revoke:
        revoke_user_sessions(db, u.id)
    audit(db, "admin_change", user_id=p.user.id, object_type="user", object_id=u.id,
          details={"action": "update", **changes}, ip=get_client_ip(request))
    db.commit()
    return _user_dict(u)


@router.delete("/admin/users/{user_id}")
def deactivate_user(user_id: int, request: Request, db: Session = Depends(get_db),
                    p: Principal = Depends(writer("admin"))) -> dict[str, Any]:
    """Deactivates (reviews and audit rows keep referring to the user)."""
    u = db.get(User, user_id)
    if u is None:
        raise HTTPException(404, "User not found")
    if u.role == "admin" and u.active and _active_admins(db) <= 1:
        raise HTTPException(409, "This is the last active admin. Make another admin first.")
    u.active = False
    revoke_user_sessions(db, u.id)
    audit(db, "admin_change", user_id=p.user.id, object_type="user", object_id=u.id,
          details={"action": "deactivate"}, ip=get_client_ip(request))
    db.commit()
    return _user_dict(u)


# -------------------------------------------------------------------------------------- scans, providers
class ScanBody(BaseModel):
    mode: Optional[Literal["standard", "pit"]] = None


@router.post("/scans", status_code=202)
async def trigger_scan(request: Request, body: ScanBody = Body(default_factory=ScanBody),
                       db: Session = Depends(get_db), p: Principal = Depends(writer("admin"))) -> dict[str, Any]:
    sched = getattr(request.app.state, "scheduler", None)
    if sched is None:
        raise HTTPException(503, "The scheduler is not running.")
    started = await sched.trigger_scan(mode=body.mode)
    if not started:
        raise HTTPException(409, "A scan is already running.")
    audit(db, "scan_trigger", user_id=p.user.id, details={"mode": body.mode or "auto"}, ip=get_client_ip(request))
    db.commit()
    return {"started": True}


@router.get("/admin/socialcrawl/endpoints")
def socialcrawl_endpoints(db: Session = Depends(get_db)) -> dict[str, Any]:
    resolved = get_value(db, KEY_SC_ENDPOINTS) or {}
    return {"resolved": resolved}


@router.post("/admin/socialcrawl/discover")
async def socialcrawl_discover(request: Request, db: Session = Depends(get_db),
                               p: Principal = Depends(writer("admin"))) -> dict[str, Any]:
    from app.pipeline.runner import DiscoveryUnavailable, run_discovery

    try:
        resolved = await run_discovery(get_state(request))
    except DiscoveryUnavailable as exc:
        raise HTTPException(409, str(exc)) from None
    except Exception as exc:  # noqa: BLE001
        logger.warning("admin discovery failed (%s)", type(exc).__name__)
        raise HTTPException(502, f"Discovery failed ({type(exc).__name__}). Check Sources & Health.") from None
    audit(db, "admin_change", user_id=p.user.id, object_type="socialcrawl", details={"action": "discover"},
          ip=get_client_ip(request))
    db.commit()
    return {"resolved": resolved}


@router.post("/admin/geo/rebuild", status_code=202)
async def geo_rebuild(request: Request, db: Session = Depends(get_db),
                      p: Principal = Depends(writer("admin"))) -> dict[str, Any]:
    sched = getattr(request.app.state, "scheduler", None)
    if sched is None:
        raise HTTPException(503, "The scheduler is not running.")
    if not await sched.rebuild_geography():
        raise HTTPException(409, "A geography build is already running.")
    audit(db, "geo_rebuild", user_id=p.user.id, ip=get_client_ip(request))
    db.commit()
    return {"started": True}


# --------------------------------------------------------------------------------------- lexicon, sources
@router.get("/admin/lexicon")
def get_lexicon(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    lex = get_state(request).lexicon
    return {"effective": lex.to_dict(), "overrides": get_value(db, KEY_LEXICON_OVERRIDES) or {},
            "override_errors": list(getattr(lex, "override_errors", []) or [])}


@router.put("/admin/lexicon")
def put_lexicon(request: Request, payload: dict[str, Any] = Body(...), db: Session = Depends(get_db),
                p: Principal = Depends(writer("admin"))) -> dict[str, Any]:
    from app.pipeline.lexicon import Lexicon

    overrides = body_dict(payload).get("overrides") or {}
    if not isinstance(overrides, dict):
        raise HTTPException(422, "overrides must be an object")
    errors = Lexicon.validate_overrides(overrides)
    if errors:
        return JSONResponse(status_code=422, content={"detail": "Some lexicon entries are invalid.", "errors": errors})
    lex = Lexicon.load(overrides=overrides or None)
    state = get_state(request)
    state.lexicon = lex
    set_value(db, KEY_LEXICON_OVERRIDES, overrides, user_id=p.user.id)
    audit(db, "admin_change", user_id=p.user.id, object_type="lexicon",
          details={"lists": sorted(overrides)}, ip=get_client_ip(request))
    db.commit()
    return get_lexicon(request, db)


@router.get("/admin/sources")
def get_sources(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"effective": get_state(request).sources.to_dict(), "overrides": get_value(db, KEY_SOURCES_OVERRIDES) or {}}


@router.put("/admin/sources")
def put_sources(request: Request, payload: dict[str, Any] = Body(...), db: Session = Depends(get_db),
                p: Principal = Depends(writer("admin"))) -> dict[str, Any]:
    from app.pipeline.sources import SourcesConfig, validate_overrides

    overrides = body_dict(payload).get("overrides") or {}
    if not isinstance(overrides, dict):
        raise HTTPException(422, "overrides must be an object")
    errors = validate_overrides(overrides)
    if errors:
        return JSONResponse(status_code=422, content={"detail": "Some source settings are invalid.", "errors": errors})
    get_state(request).sources = SourcesConfig.load(overrides=overrides or None)
    set_value(db, KEY_SOURCES_OVERRIDES, overrides, user_id=p.user.id)
    audit(db, "admin_change", user_id=p.user.id, object_type="sources",
          details={"keys": sorted(overrides)}, ip=get_client_ip(request))
    db.commit()
    return get_sources(request, db)


# ------------------------------------------------------------------------------------------- audit, api
@router.get("/admin/audit")
def audit_log(limit: int = 100, db: Session = Depends(get_db)) -> dict[str, Any]:
    limit = max(1, min(int(limit), 500))
    names = dict(db.execute(select(User.id, User.name)).all())
    rows = db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)).all()
    return {"items": [{"id": r.id, "at": iso(r.at), "user": names.get(r.user_id), "action": r.action,
                       "object_type": r.object_type, "object_id": r.object_id, "details": r.details} for r in rows]}


@router.get("/openapi.json", include_in_schema=False)
def openapi_json(request: Request) -> dict[str, Any]:
    return request.app.openapi()

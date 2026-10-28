from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from jgevents.db.models import AuditLog, User
from jgevents.services.permissions import require_ho, visible_centre_ids


def _jsonable(v: Any) -> Any:
    if isinstance(v, (date, datetime, time)):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x) for x in v]
    return v


def log(session: Session, actor: User | None, action: str, entity_type: str, entity_id: int | None,
        summary: str, centre_id: int | None = None, details: dict | None = None) -> None:
    session.add(AuditLog(
        actor_id=actor.id if actor else None, action=action, entity_type=entity_type,
        entity_id=entity_id, centre_id=centre_id, summary=summary[:500],
        details=_jsonable(details or {}),
    ))


def list_logs(session: Session, actor: User, entity_type: str | None = None, entity_id: int | None = None,
              since: datetime | None = None, limit: int = 500) -> list[AuditLog]:
    q = select(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    if entity_type:
        q = q.where(AuditLog.entity_type == entity_type)
    if entity_id is not None:
        q = q.where(AuditLog.entity_id == entity_id)
    if since:
        q = q.where(AuditLog.created_at >= since)
    if entity_id is None:
        # The global audit trail is a Head Office view.
        require_ho(actor)
    else:
        scope = visible_centre_ids(actor)
        if scope is not None:
            q = q.where((AuditLog.centre_id.in_(scope)) | (AuditLog.centre_id.is_(None)))
    return list(session.scalars(q.limit(limit)))

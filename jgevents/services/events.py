"""Event lifecycle: create, edit, transitions, conflicts, checklists, national events."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from jgevents.db.models import (
    ApprovalAction,
    AuditLog,
    Centre,
    ChecklistItem,
    Event,
    EventCategory,
    EventReference,
    User,
)
from jgevents.domain.constants import (
    ACTION_LABELS,
    APPROVAL_TO_STATUSES,
    CENTRE_EDITABLE,
    EXECUTION_EDITABLE,
    LIVE_STATUSES,
    MATERIAL_FIELDS,
    TRANSITIONS,
    Action,
    Status,
)
from jgevents.domain.schema import clean_details, validate_event_payload
from jgevents.services import audit
from jgevents.services.config_service import get_setting
from jgevents.services.errors import (
    ConflictError,
    InvalidTransition,
    NotFound,
    PermissionDenied,
    ScheduleConflict,
    ValidationError,
)
from jgevents.services.permissions import (
    can_manage_centre,
    is_ho,
    require_editor,
    require_ho,
    require_manage_centre,
    require_manage_event,
    require_view_event,
    role,
    visible_centre_ids,
)
from jgevents.utils.dates import now_utc, times_overlap, today_ist

PLAN_FIELDS = ["title", "subtype", "event_date", "start_time", "end_time", "description", "objective",
               "responsible_person", "expected_participation", "target_audience", "venue",
               "preparation_deadline", "remarks"]


# ---------------------------------------------------------------- queries

@dataclass
class EventFilter:
    date_from: date | None = None
    date_to: date | None = None
    centre_ids: list[int] | None = None
    category_ids: list[int] | None = None
    statuses: list[str] | None = None
    approval_statuses: list[str] | None = None
    responsible: str | None = None
    search: str | None = None
    include_national_parents: bool = False
    exclude_ids: list[int] = field(default_factory=list)


def scoped_query(actor: User, f: EventFilter | None = None) -> Select:
    f = f or EventFilter()
    q = select(Event)
    scope = visible_centre_ids(actor)
    if scope is not None:
        q = q.where(or_(Event.centre_id.in_(scope), Event.is_national_parent.is_(True)))
    if not f.include_national_parents:
        q = q.where(Event.is_national_parent.is_(False))
    if f.date_from:
        q = q.where(Event.event_date >= f.date_from)
    if f.date_to:
        q = q.where(Event.event_date <= f.date_to)
    if f.centre_ids:
        q = q.where(or_(Event.centre_id.in_(f.centre_ids), Event.is_national_parent.is_(True))
                    if f.include_national_parents else Event.centre_id.in_(f.centre_ids))
    if f.category_ids:
        q = q.where(Event.category_id.in_(f.category_ids))
    statuses = set(f.statuses or [])
    if f.approval_statuses:
        appr = {s for a in f.approval_statuses for s in APPROVAL_TO_STATUSES.get(a, [])}
        statuses = statuses & appr if statuses else appr
        if not statuses:
            statuses = {"__none__"}
    if statuses:
        q = q.where(Event.status.in_(statuses))
    if f.responsible:
        q = q.where(func.lower(Event.responsible_person).contains(f.responsible.strip().lower()))
    if f.search:
        s = f"%{f.search.strip().lower()}%"
        q = q.where(or_(func.lower(Event.title).like(s), func.lower(Event.description).like(s),
                        func.lower(Event.venue).like(s)))
    if f.exclude_ids:
        q = q.where(Event.id.not_in(f.exclude_ids))
    return q.order_by(Event.event_date, Event.start_time.nulls_first(), Event.id)


def list_events(session: Session, actor: User, f: EventFilter | None = None, limit: int | None = None) -> list[Event]:
    q = scoped_query(actor, f)
    if limit:
        q = q.limit(limit)
    return list(session.scalars(q).unique())


def get_event(session: Session, actor: User, event_id: int) -> Event:
    ev = session.get(Event, event_id)
    if ev is None:
        raise NotFound(f"Event {event_id} not found.")
    require_view_event(actor, ev)
    return ev


def _get_for_update(session: Session, event_id: int) -> Event:
    ev = session.scalar(select(Event).where(Event.id == event_id).with_for_update(of=Event))
    if ev is None:
        raise NotFound(f"Event {event_id} not found.")
    return ev


def _check_version(ev: Event, expected_version: int | None) -> None:
    if expected_version is not None and ev.version != expected_version:
        who = ev.updated_by.full_name if ev.updated_by else "another user"
        raise ConflictError(f"{ev.code} was changed by {who} after you opened it. Reload and re-apply your edits.")


def _flush(session: Session, ev: Event) -> None:
    try:
        session.flush()
    except StaleDataError as exc:
        raise ConflictError(f"{ev.code} was changed by someone else while you were saving. Reload and retry.") from exc


def find_conflicts(session: Session, centre_id: int | None, event_date: date, start: time | None, end: time | None,
                   exclude_id: int | None = None) -> list[Event]:
    if centre_id is None:
        return []
    q = (select(Event).join(EventCategory, Event.category_id == EventCategory.id)
         .where(Event.centre_id == centre_id, Event.event_date == event_date,
                Event.status.in_([s.value for s in LIVE_STATUSES]), EventCategory.occupies_slot.is_(True)))
    if exclude_id:
        q = q.where(Event.id != exclude_id)
    return [e for e in session.scalars(q).unique() if times_overlap(start, end, e.start_time, e.end_time)]


def _conflict_check(session: Session, category: EventCategory, centre_id: int | None, common: dict,
                    exclude_id: int | None) -> list[str]:
    """Raise for hard conflicts (two 'block' categories), return warnings otherwise."""
    if not category.occupies_slot or category.conflict_policy == "none":
        return []
    clashes = find_conflicts(session, centre_id, common["event_date"], common.get("start_time"),
                             common.get("end_time"), exclude_id)
    if not clashes:
        return []
    hard = [e for e in clashes if category.conflict_policy == "block" and e.category.conflict_policy == "block"]
    def describe(e: Event) -> str:
        t = f"{e.start_time:%H:%M}–{e.end_time:%H:%M}" if e.start_time and e.end_time else "all day"
        return f"{e.code} '{e.title}' ({e.category.name}, {t})"
    if hard:
        raise ScheduleConflict(
            "Time slot clashes with " + "; ".join(describe(e) for e in hard)
            + ". Two batch launches at a centre cannot overlap – pick another slot.", hard)
    return ["Possible scheduling conflict with " + "; ".join(describe(e) for e in clashes)]


def _validate_link(session: Session, category: EventCategory, centre_id: int | None,
                   linked_event_id: int | None) -> None:
    if not linked_event_id:
        return
    if not category.link_target_key:
        raise ValidationError(f"{category.name} events cannot be linked to another event.")
    target = session.get(Event, linked_event_id)
    if target is None or target.category.key != category.link_target_key:
        raise ValidationError("The linked event must be a "
                              f"{category.link_target_key.replace('_', ' ')} event.")
    if target.centre_id != centre_id:
        raise ValidationError("The linked event must belong to the same centre.")
    if target.status in (Status.CANCELLED, Status.REJECTED):
        raise ValidationError("Cannot link to a cancelled or rejected event.")


def _normalise_common(common: dict) -> dict:
    out = {k: common.get(k) for k in PLAN_FIELDS if k in common}
    for k in ("title", "subtype", "description", "objective", "responsible_person", "target_audience", "venue",
              "remarks"):
        if k in out:
            out[k] = (out[k] or "").strip() or None
    if out.get("title"):
        out["title"] = out["title"][:200]
    for k in ("expected_participation",):
        if k in out and out[k] in ("",):
            out[k] = None
    return out


def _record(session: Session, actor: User, ev: Event, action: str, frm: str | None, to: str | None,
            comment: str | None = None) -> None:
    session.add(ApprovalAction(event_id=ev.id, action=action, from_status=frm, to_status=to, comment=comment,
                               actor_id=actor.id))


def _refresh_plan(session: Session, ev: Event) -> None:
    if ev.centre_id is None:
        return
    from jgevents.services import planning  # local import: planning depends on events
    planning.refresh_plan_status(session, ev.centre_id, ev.event_date.year, ev.event_date.month)


# ---------------------------------------------------------------- create / update

def create_event(session: Session, actor: User, *, category_id: int, centre_id: int | None, common: dict,
                 details: dict | None = None, linked_event_id: int | None = None, submit: bool = False,
                 parent_event_id: int | None = None, is_proposed: bool = False,
                 recurrence_rule_id: int | None = None, _national_parent: bool = False) -> tuple[Event, list[str]]:
    require_editor(actor)
    category = session.get(EventCategory, category_id)
    if category is None or not category.is_active:
        raise ValidationError("Choose an active event category.")
    if _national_parent:
        require_ho(actor)
    else:
        if centre_id is None:
            raise ValidationError("Centre is required.")
        require_manage_centre(actor, centre_id)
        centre = session.get(Centre, centre_id)
        if centre is None or not centre.is_active:
            raise ValidationError("This centre is inactive; events cannot be added.")
    common = _normalise_common(common)
    details = clean_details(category, details or {})
    errors = validate_event_payload(category, common, details, strict=submit)
    if errors:
        raise ValidationError(errors)
    _validate_link(session, category, centre_id, linked_event_id)
    warnings = [] if _national_parent else _conflict_check(session, category, centre_id, common, None)
    ev = Event(category_id=category.id, centre_id=centre_id, details=details, linked_event_id=linked_event_id,
               parent_event_id=parent_event_id, is_national_parent=_national_parent, is_proposed=is_proposed,
               recurrence_rule_id=recurrence_rule_id, status=Status.DRAFT,
               created_by_id=actor.id, updated_by_id=actor.id, **common)
    session.add(ev)
    session.flush()
    for i, label in enumerate(category.default_checklist or []):
        session.add(ChecklistItem(event_id=ev.id, label=label, sort_order=i))
    _record(session, actor, ev, "created", None, Status.DRAFT)
    audit.log(session, actor, "event.created", "event", ev.id, f"{ev.code} '{ev.title}' created",
              centre_id=centre_id, details={"category": category.key, "date": ev.event_date})
    if submit:
        transition(session, actor, ev.id, Action.SUBMIT)
    else:
        _refresh_plan(session, ev)
    return ev, warnings


def update_event(session: Session, actor: User, event_id: int, expected_version: int | None, *,
                 common: dict, details: dict | None = None, linked_event_id: int | None = None,
                 centre_id: int | None = None) -> tuple[Event, list[str]]:
    require_editor(actor)
    ev = _get_for_update(session, event_id)
    require_manage_event(actor, ev)
    _check_version(ev, expected_version)
    ho = is_ho(actor)
    editable = CENTRE_EDITABLE | ({Status.IN_PROGRESS} if ho else set())
    if Status(ev.status) not in editable:
        raise InvalidTransition(f"Planning details cannot be edited while the event is "
                                f"{ev.status.replace('_', ' ')}. Use the execution section instead.")
    new_centre = ev.centre_id if centre_id is None else centre_id
    if new_centre != ev.centre_id:
        if ev.is_national_parent or ev.parent_event_id:
            raise ValidationError("Centre cannot be changed for national mock records.")
        require_manage_centre(actor, new_centre)
    category = ev.category
    common = _normalise_common(common)
    merged_details = dict(ev.details or {})
    merged_details.update(clean_details(category, details or {}))
    strict = ev.status != Status.DRAFT and not ev.is_national_parent  # parents carry no centre-level detail
    errors = validate_event_payload(category, common, merged_details, strict=strict)
    if errors:
        raise ValidationError(errors)
    _validate_link(session, category, new_centre, linked_event_id)
    warnings = [] if ev.is_national_parent else _conflict_check(session, category, new_centre, common, ev.id)

    changes = {}
    for k, v in common.items():
        if getattr(ev, k) != v:
            changes[k] = (getattr(ev, k), v)
            setattr(ev, k, v)
    if new_centre != ev.centre_id:
        changes["centre_id"] = (ev.centre_id, new_centre)
        ev.centre_id = new_centre
    if merged_details != (ev.details or {}):
        changes["details"] = (ev.details, merged_details)
        ev.details = merged_details
    if (ev.linked_event_id or None) != (linked_event_id or None):
        changes["linked_event_id"] = (ev.linked_event_id, linked_event_id)
        ev.linked_event_id = linked_event_id
    if not changes:
        return ev, warnings
    ev.updated_by_id = actor.id
    ev.is_proposed = False
    material = MATERIAL_FIELDS & set(changes)
    if (ev.status == Status.APPROVED and material and not ho
            and get_setting(session, "reapproval_on_material_change")):
        _record(session, actor, ev, "resubmitted_after_change", ev.status, Status.SUBMITTED,
                "Material change after approval: " + ", ".join(sorted(material)))
        ev.status = Status.SUBMITTED
        ev.changed_after_approval = True
        ev.submitted_at = now_utc()
        warnings.append("Approved event changed materially – it has been sent back to Head Office for re-approval.")
    _flush(session, ev)
    if ev.is_national_parent and material & {"title", "event_date", "start_time", "end_time"}:
        _sync_children(session, actor, ev)
    audit.log(session, actor, "event.updated", "event", ev.id, f"{ev.code} updated ({', '.join(sorted(changes))})",
              centre_id=ev.centre_id, details={k: {"from": a, "to": b} for k, (a, b) in changes.items()})
    _refresh_plan(session, ev)
    return ev, warnings


def update_execution(session: Session, actor: User, event_id: int, expected_version: int | None, *,
                     actual_participation: int | None, post_event_remarks: str | None,
                     details: dict | None = None) -> Event:
    require_editor(actor)
    ev = _get_for_update(session, event_id)
    require_manage_event(actor, ev)
    _check_version(ev, expected_version)
    if Status(ev.status) not in EXECUTION_EDITABLE:
        raise InvalidTransition("Execution details can be recorded only for approved events.")
    if actual_participation is not None and actual_participation < 0:
        raise ValidationError("Actual participation cannot be negative.")
    exec_keys = {f["key"] for f in ev.category.field_schema or [] if f.get("phase") == "execution"}
    incoming = clean_details(ev.category, {k: v for k, v in (details or {}).items() if k in exec_keys})
    merged = dict(ev.details or {})
    merged.update(incoming)
    errors = validate_event_payload(ev.category, {"title": ev.title, "event_date": ev.event_date}, merged, False)
    if errors:
        raise ValidationError(errors)
    ev.actual_participation = actual_participation
    ev.post_event_remarks = (post_event_remarks or "").strip() or None
    ev.details = merged
    ev.updated_by_id = actor.id
    _flush(session, ev)
    audit.log(session, actor, "event.execution_updated", "event", ev.id, f"{ev.code} execution details updated",
              centre_id=ev.centre_id, details={"actual": actual_participation, **incoming})
    return ev


# ---------------------------------------------------------------- transitions

def allowed_actions(actor: User, ev: Event) -> list[Action]:
    if not actor.is_active:
        return []
    out = []
    for action, (froms, _to, roles, _c) in TRANSITIONS.items():
        if Status(ev.status) not in froms or role(actor) not in roles:
            continue
        if ev.is_national_parent and action not in (Action.CANCEL, Action.REOPEN):
            continue
        if not is_ho(actor) and not can_manage_centre(actor, ev.centre_id):
            continue
        if action in (Action.COMPLETE, Action.START) and ev.event_date > today_ist():
            continue
        out.append(action)
    return out


def transition(session: Session, actor: User, event_id: int, action: Action | str, comment: str | None = None,
               expected_version: int | None = None) -> Event:
    action = Action(action)
    ev = _get_for_update(session, event_id)
    require_manage_event(actor, ev)
    _check_version(ev, expected_version)
    froms, to, roles, needs_comment = TRANSITIONS[action]
    if role(actor) not in roles:
        if action in (Action.APPROVE, Action.RETURN, Action.REJECT):
            raise PermissionDenied("Only Head Office can approve, return or reject events.")
        raise PermissionDenied(f"Your role cannot '{ACTION_LABELS[action]}'.")
    if Status(ev.status) not in froms:
        raise InvalidTransition(f"Cannot {ACTION_LABELS[action].lower()} an event that is "
                                f"{ev.status.replace('_', ' ')}.")
    comment = (comment or "").strip() or None
    if needs_comment and not comment:
        raise ValidationError(f"A comment/reason is required to {ACTION_LABELS[action].lower()}.")
    if ev.is_national_parent and action not in (Action.CANCEL, Action.REOPEN):
        raise InvalidTransition("National parent events are managed through their centre records.")
    if action == Action.SUBMIT:
        common = {k: getattr(ev, k) for k in PLAN_FIELDS}
        errors = validate_event_payload(ev.category, common, ev.details or {}, strict=True)
        if errors:
            raise ValidationError(["Complete the event before submitting:"] + errors)
        _conflict_check(session, ev.category, ev.centre_id, common, ev.id)  # raises on hard clash
        ev.submitted_at = now_utc()
    if action in (Action.START, Action.COMPLETE) and ev.event_date > today_ist():
        raise InvalidTransition("An event can be started or completed only on or after its date.")
    frm = ev.status
    ev.status = to
    ev.updated_by_id = actor.id
    if action == Action.APPROVE:
        ev.approved_at, ev.approved_by_id, ev.changed_after_approval = now_utc(), actor.id, False
    if action in (Action.APPROVE, Action.RETURN, Action.REJECT):
        ev.last_review_comment = comment
    if action == Action.CANCEL:
        ev.cancel_reason = comment
    if action == Action.COMPLETE:
        ev.completed_at = now_utc()
    if action == Action.REOPEN:
        ev.cancel_reason = None
    _flush(session, ev)
    _record(session, actor, ev, action.value, frm, to, comment)
    audit.log(session, actor, f"event.{action.value}", "event", ev.id,
              f"{ev.code} {ACTION_LABELS[action].lower()} ({frm} → {to})", centre_id=ev.centre_id,
              details={"comment": comment})
    if ev.is_national_parent and action == Action.CANCEL:
        for child in children(session, ev.id):
            if Status(child.status) in TRANSITIONS[Action.CANCEL][0]:
                transition(session, actor, child.id, Action.CANCEL, f"National event cancelled: {comment}")
    _refresh_plan(session, ev)
    return ev


def approval_queue(session: Session, actor: User, f: EventFilter | None = None) -> list[Event]:
    require_ho(actor)
    f = f or EventFilter()
    f.statuses = [Status.SUBMITTED]
    return list_events(session, actor, f)


# ---------------------------------------------------------------- checklist & references

def checklist(session: Session, actor: User, event_id: int) -> list[ChecklistItem]:
    get_event(session, actor, event_id)
    return list(session.scalars(select(ChecklistItem).where(ChecklistItem.event_id == event_id)
                                .order_by(ChecklistItem.sort_order, ChecklistItem.id)))


def set_checklist_item(session: Session, actor: User, item_id: int, done: bool) -> ChecklistItem:
    item = session.get(ChecklistItem, item_id)
    if not item:
        raise NotFound("Checklist item not found.")
    ev = session.get(Event, item.event_id)
    require_editor(actor)
    require_manage_event(actor, ev)
    if Status(ev.status) in (Status.CANCELLED, Status.REJECTED):
        raise InvalidTransition("Checklist is locked for cancelled or rejected events.")
    item.is_done = done
    item.done_by_id = actor.id if done else None
    item.done_at = now_utc() if done else None
    audit.log(session, actor, "checklist.toggled", "event", ev.id,
              f"{ev.code} checklist '{item.label}' {'done' if done else 'reopened'}", centre_id=ev.centre_id)
    return item


def add_checklist_item(session: Session, actor: User, event_id: int, label: str) -> ChecklistItem:
    require_editor(actor)
    ev = get_event(session, actor, event_id)
    require_manage_event(actor, ev)
    label = (label or "").strip()
    if not label:
        raise ValidationError("Checklist item cannot be empty.")
    n = session.scalar(select(func.count()).select_from(ChecklistItem).where(ChecklistItem.event_id == event_id))
    item = ChecklistItem(event_id=event_id, label=label[:200], sort_order=n or 0)
    session.add(item)
    session.flush()
    audit.log(session, actor, "checklist.added", "event", ev.id, f"{ev.code} checklist item added: {label}",
              centre_id=ev.centre_id)
    return item


def delete_checklist_item(session: Session, actor: User, item_id: int) -> None:
    item = session.get(ChecklistItem, item_id)
    if not item:
        raise NotFound("Checklist item not found.")
    ev = session.get(Event, item.event_id)
    require_editor(actor)
    require_manage_event(actor, ev)
    session.delete(item)
    audit.log(session, actor, "checklist.removed", "event", ev.id, f"{ev.code} checklist item removed: {item.label}",
              centre_id=ev.centre_id)


def references(session: Session, actor: User, event_id: int) -> list[EventReference]:
    get_event(session, actor, event_id)
    return list(session.scalars(select(EventReference).where(EventReference.event_id == event_id)
                                .order_by(EventReference.id)))


def add_reference(session: Session, actor: User, event_id: int, label: str, url: str) -> EventReference:
    require_editor(actor)
    ev = get_event(session, actor, event_id)
    require_manage_event(actor, ev)
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ValidationError("Reference must be a link starting with http:// or https://")
    ref = EventReference(event_id=event_id, label=(label or url)[:200], url=url[:1000], added_by_id=actor.id)
    session.add(ref)
    session.flush()
    audit.log(session, actor, "reference.added", "event", ev.id, f"{ev.code} reference added", centre_id=ev.centre_id)
    return ref


def delete_reference(session: Session, actor: User, ref_id: int) -> None:
    ref = session.get(EventReference, ref_id)
    if not ref:
        raise NotFound("Reference not found.")
    ev = session.get(Event, ref.event_id)
    require_editor(actor)
    require_manage_event(actor, ev)
    session.delete(ref)
    audit.log(session, actor, "reference.removed", "event", ev.id, f"{ev.code} reference removed", centre_id=ev.centre_id)


# ---------------------------------------------------------------- relationships & history

def children(session: Session, parent_id: int) -> list[Event]:
    return list(session.scalars(select(Event).where(Event.parent_event_id == parent_id).order_by(Event.centre_id)))


def visible_children(session: Session, actor: User, parent_id: int) -> list[Event]:
    scope = visible_centre_ids(actor)
    return [c for c in children(session, parent_id) if scope is None or c.centre_id in scope]


def linked_campaigns(session: Session, actor: User, batch_event_id: int) -> list[Event]:
    get_event(session, actor, batch_event_id)
    return list(session.scalars(select(Event).where(Event.linked_event_id == batch_event_id)
                                .order_by(Event.event_date, Event.start_time)))


def linkable_targets(session: Session, actor: User, category: EventCategory, centre_id: int) -> list[Event]:
    if not category.link_target_key or centre_id is None:
        return []
    return list(session.scalars(
        select(Event).join(EventCategory, Event.category_id == EventCategory.id)
        .where(EventCategory.key == category.link_target_key, Event.centre_id == centre_id,
               Event.status.not_in([Status.CANCELLED, Status.REJECTED]),
               Event.event_date >= date.fromordinal(today_ist().toordinal() - 90))
        .order_by(Event.event_date)).unique())


def history(session: Session, actor: User, event_id: int) -> tuple[list[ApprovalAction], list[AuditLog]]:
    get_event(session, actor, event_id)
    acts = list(session.scalars(select(ApprovalAction).where(ApprovalAction.event_id == event_id)
                                .order_by(ApprovalAction.created_at, ApprovalAction.id)))
    logs = list(session.scalars(select(AuditLog).where(AuditLog.entity_type == "event", AuditLog.entity_id == event_id)
                                .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())))
    return acts, logs


# ---------------------------------------------------------------- national (parent/child) events

def create_national_event(session: Session, actor: User, *, category_id: int, common: dict, details: dict | None,
                          centre_ids: list[int]) -> tuple[Event, list[Event]]:
    require_ho(actor)
    category = session.get(EventCategory, category_id)
    if category is None or not category.supports_national:
        raise ValidationError("This category does not support national events.")
    parent, _ = create_event(session, actor, category_id=category_id, centre_id=None, common=common,
                             details=details, _national_parent=True)
    # A Head-Office-created national event is approved at source.
    frm = parent.status
    parent.status, parent.approved_at, parent.approved_by_id = Status.APPROVED, now_utc(), actor.id
    _record(session, actor, parent, "approve", frm, Status.APPROVED, "Created by Head Office")
    kids = assign_centres(session, actor, parent.id, centre_ids)
    return parent, kids


def assign_centres(session: Session, actor: User, parent_id: int, centre_ids: list[int]) -> list[Event]:
    require_ho(actor)
    parent = session.get(Event, parent_id)
    if parent is None or not parent.is_national_parent:
        raise NotFound("National event not found.")
    existing = {c.centre_id for c in children(session, parent_id)}
    created = []
    for cid in centre_ids:
        if cid in existing:
            continue
        centre = session.get(Centre, cid)
        if centre is None or not centre.is_active:
            continue
        child = Event(category_id=parent.category_id, centre_id=cid, parent_event_id=parent.id,
                      title=parent.title, event_date=parent.event_date, start_time=parent.start_time,
                      end_time=parent.end_time, description=parent.description, objective=parent.objective,
                      target_audience=parent.target_audience, venue=None,
                      preparation_deadline=parent.preparation_deadline,
                      details={k: v for k, v in (parent.details or {}).items() if k != "participation_plan"},
                      status=Status.DRAFT, created_by_id=actor.id, updated_by_id=actor.id)
        session.add(child)
        session.flush()
        for i, label in enumerate(parent.category.default_checklist or []):
            session.add(ChecklistItem(event_id=child.id, label=label, sort_order=i))
        _record(session, actor, child, "created", None, Status.DRAFT, f"Assigned from national event {parent.code}")
        audit.log(session, actor, "event.national_assigned", "event", child.id,
                  f"{child.code} created for {centre.name} from {parent.code}", centre_id=cid)
        created.append(child)
        _refresh_plan(session, child)
    return created


def _sync_children(session: Session, actor: User, parent: Event) -> None:
    for child in children(session, parent.id):
        if Status(child.status) in (Status.CANCELLED, Status.REJECTED, Status.COMPLETED):
            continue
        child.title, child.event_date = parent.title, parent.event_date
        child.start_time, child.end_time = parent.start_time, parent.end_time
        child.updated_by_id = actor.id
        audit.log(session, actor, "event.synced_from_parent", "event", child.id,
                  f"{child.code} schedule synced from {parent.code}", centre_id=child.centre_id)
    session.flush()


def national_events(session: Session, actor: User) -> list[Event]:
    return list(session.scalars(select(Event).where(Event.is_national_parent.is_(True))
                                .order_by(Event.event_date.desc())))


# ---------------------------------------------------------------- derived flags

def risk_flags(ev: Event, window_days: int = 7) -> list[str]:
    today = today_ist()
    flags = []
    st = Status(ev.status)
    if st in (Status.APPROVED, Status.IN_PROGRESS) and ev.event_date < today:
        flags.append("Overdue: outcome not recorded")
    if st in (Status.DRAFT, Status.SUBMITTED, Status.RETURNED):
        days = (ev.event_date - today).days
        if days < 0:
            flags.append("Date passed without approval")
        elif days <= window_days:
            flags.append(f"Not approved, {days} day(s) to go")
        if ev.preparation_deadline and ev.preparation_deadline < today and days >= 0:
            flags.append("Preparation deadline passed")
    if ev.changed_after_approval and st == Status.SUBMITTED:
        flags.append("Changed after approval")
    return flags


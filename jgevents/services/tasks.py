"""v2 – pre-event readiness: playbooks, generated tasks, deadlines, readiness and audit flags.

Rules implemented here follow the signed-off spec ("Pre-Event Readiness Playbooks – Specification v2"):
* T-N = N calendar days before the event date (weekends count); due by end of that day IST.
* Tasks are snapshots of the playbook version used; later playbook edits never alter existing events.
* One task per (event, key) – generation is idempotent (unique constraint + existence check).
* Centre Heads tick their centre's tasks; Head Office ticks national tasks; nobody ticks the WhatsApp
  task by hand – it completes when a linked WhatsApp campaign is completed.
* Overdue and blocked are computed, never stored.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from jgevents.db.models import (
    Event,
    EventCategory,
    EventTask,
    Playbook,
    PlaybookTask,
    TaskFlag,
    User,
)
from jgevents.domain.constants import Status
from jgevents.services import audit
from jgevents.services.errors import ConflictError, InvalidTransition, NotFound, PermissionDenied, ValidationError
from jgevents.services.permissions import (
    is_ho,
    require_editor,
    require_ho,
    require_manage_centre,
    require_view_centre,
    visible_centre_ids,
)
from jgevents.utils.dates import fmt_date, now_utc, to_ist, today_ist

AT_RISK_DAYS = 2
CLOSED_FOR_EVENT = "Closed – event cancelled or rejected"
FINISHED = ("done", "waived", "not_needed")
PROOF_TYPES = {"none", "url", "count", "seats", "whatsapp_auto"}
FLAG_TYPES = {"not_done_properly": "Not done properly", "other": "Other"}

READINESS_LABELS = {
    "ready": "Ready", "on_track": "On track", "at_risk": "At risk", "off_track": "Off track",
    "blocked_ho": "Blocked by Head Office", "cancelled": "Cancelled",
}
READINESS_ICONS = {
    "ready": "🟢", "on_track": "🔵", "at_risk": "🟠", "off_track": "🔴", "blocked_ho": "🟣", "cancelled": "⚪",
}
READINESS_ORDER = {"off_track": 0, "blocked_ho": 1, "at_risk": 2, "on_track": 3, "ready": 4, "cancelled": 5}


def t_label(offset: int) -> str:
    return "T-0" if offset == 0 else (f"T{offset}" if offset < 0 else f"T+{offset}")


# ---------------------------------------------------------------- playbooks

def active_playbook(session: Session, category_id: int) -> Playbook | None:
    return session.scalar(select(Playbook).where(Playbook.category_id == category_id, Playbook.is_active.is_(True))
                          .order_by(Playbook.version.desc()))


def playbook_versions(session: Session, category_id: int) -> list[Playbook]:
    return list(session.scalars(select(Playbook).where(Playbook.category_id == category_id)
                                .order_by(Playbook.version.desc())))


def _scope_for(ev_is_parent: bool) -> set[str]:
    return {"national"} if ev_is_parent else {"centre"}


def min_lead_days(pb: Playbook | None, national_parent: bool = False) -> int:
    """Earliest pre-event task in days. For a national parent every task counts (it seeds all centres)."""
    if pb is None:
        return 0
    offs = [-t.offset_days for t in pb.tasks if t.offset_days < 0
            and (national_parent or t.scope == "centre")]
    return max(offs, default=0)


def check_lead_time(session: Session, actor: User, category: EventCategory, event_date: date,
                    late_reason: str | None, *, national_parent: bool = False) -> tuple[bool, str | None]:
    """Returns (booked_late, reason). Raises if the event is inside the minimum lead time and the
    actor is not Head Office, or Head Office gave no reason."""
    pb = active_playbook(session, category.id)
    lead = min_lead_days(pb, national_parent)
    if not lead:
        return False, None
    days = (event_date - today_ist()).days
    if days >= lead:
        return False, None
    if not is_ho(actor):
        raise ValidationError(
            f"{category.name} needs at least {lead} days' notice (its first preparation task is T-{lead}). "
            f"This date is {days} day(s) away. Pick a later date, or ask Head Office to book it as an exception.")
    reason = (late_reason or "").strip()
    if not reason:
        raise ValidationError(
            f"{category.name} is inside its {lead}-day minimum lead time ({days} day(s) away). "
            "Enter a reason to book it as a Head Office exception.")
    return True, reason


@dataclass
class PreviewRow:
    key: str
    title: str
    t: str
    due: date
    owner: str
    critical: bool
    scope: str
    past: bool
    not_needed: bool


def preview_schedule(session: Session, category_id: int, event_date: date, *, national_parent: bool = False,
                     include_national: bool = False, mode: str | None = None) -> list[PreviewRow]:
    pb = active_playbook(session, category_id)
    if pb is None:
        return []
    scopes = {"national", "centre"} if include_national else _scope_for(national_parent)
    today = today_ist()
    rows = []
    for t in pb.tasks:
        if t.scope not in scopes:
            continue
        due = event_date + timedelta(days=t.offset_days)
        rows.append(PreviewRow(t.key, t.title, t_label(t.offset_days), due, t.owner_label, t.is_critical, t.scope,
                               due < today, bool(t.skip_if_mode and mode and mode == t.skip_if_mode)))
    return sorted(rows, key=lambda r: (r.due, r.key))


# ---------------------------------------------------------------- generation

def _mode(ev: Event) -> str | None:
    return (ev.details or {}).get("mode")


def generate_tasks(session: Session, actor: User | None, ev: Event, playbook: Playbook | None = None) -> list[EventTask]:
    """Create any playbook tasks the event does not have yet. Safe to call repeatedly."""
    pb = playbook or active_playbook(session, ev.category_id)
    if pb is None or Status(ev.status) in (Status.CANCELLED, Status.REJECTED):
        return []
    existing = {t.key for t in session.scalars(select(EventTask).where(EventTask.event_id == ev.id))}
    scopes = _scope_for(ev.is_national_parent)
    created = []
    for pt in pb.tasks:
        if pt.scope not in scopes or pt.key in existing:
            continue
        task = EventTask(
            event_id=ev.id, centre_id=ev.centre_id, playbook_task_id=pt.id, playbook_version=pb.version, key=pt.key,
            title=pt.title, description=pt.description, owner_label=pt.owner_label, offset_days=pt.offset_days,
            due_date=ev.event_date + timedelta(days=pt.offset_days), is_critical=pt.is_critical, scope=pt.scope,
            depends_on=list(pt.depends_on or []), proof_type=pt.proof_type, proof_label=pt.proof_label,
            skip_if_mode=pt.skip_if_mode, sort_order=pt.sort_order, status="open")
        if pt.skip_if_mode and _mode(ev) == pt.skip_if_mode:
            task.status, task.status_reason = "not_needed", f"Not needed – mode is {pt.skip_if_mode}"
        session.add(task)
        created.append(task)
    if created:
        session.flush()
        audit.log(session, actor, "tasks.generated", "event", ev.id,
                  f"{ev.code}: {len(created)} preparation task(s) added from {pb.category.name} playbook v{pb.version}",
                  centre_id=ev.centre_id, details={"keys": [t.key for t in created], "version": pb.version})
        _auto_complete_from_campaigns(session, actor, ev)
    return created


def has_playbook(session: Session, category_id: int) -> bool:
    return active_playbook(session, category_id) is not None


# ---------------------------------------------------------------- queries

def event_tasks(session: Session, event_id: int) -> list[EventTask]:
    return list(session.scalars(select(EventTask).where(EventTask.event_id == event_id)
                                .order_by(EventTask.due_date, EventTask.sort_order, EventTask.id)).unique())


def tasks_for_event(session: Session, actor: User, event_id: int) -> list[EventTask]:
    ev = session.get(Event, event_id)
    if ev is None:
        raise NotFound("Event not found.")
    require_view_centre(actor, ev.centre_id)
    return event_tasks(session, event_id)


def get_task(session: Session, actor: User, task_id: int) -> EventTask:
    t = session.get(EventTask, task_id)
    if t is None:
        raise NotFound("Task not found.")
    require_view_centre(actor, t.centre_id)
    return t


@dataclass
class TaskFilter:
    date_from: date | None = None
    date_to: date | None = None
    centre_ids: list[int] | None = None
    category_ids: list[int] | None = None
    event_id: int | None = None
    owner_labels: list[str] | None = None
    states: list[str] | None = None  # open, overdue, blocked, done, done_late, waived, not_needed, closed
    critical_only: bool = False
    include_national: bool = True


def list_tasks(session: Session, actor: User, f: TaskFilter | None = None) -> list[EventTask]:
    f = f or TaskFilter()
    q = select(EventTask).join(Event, EventTask.event_id == Event.id)
    scope = visible_centre_ids(actor)
    if scope is not None:
        q = q.where(or_(EventTask.centre_id.in_(scope), EventTask.centre_id.is_(None)))
    if not f.include_national:
        q = q.where(EventTask.centre_id.is_not(None))
    if f.date_from:
        q = q.where(EventTask.due_date >= f.date_from)
    if f.date_to:
        q = q.where(EventTask.due_date <= f.date_to)
    if f.centre_ids:
        q = q.where(EventTask.centre_id.in_(f.centre_ids))
    if f.category_ids:
        q = q.where(Event.category_id.in_(f.category_ids))
    if f.event_id:
        q = q.where(EventTask.event_id == f.event_id)
    if f.owner_labels:
        q = q.where(EventTask.owner_label.in_(f.owner_labels))
    if f.critical_only:
        q = q.where(EventTask.is_critical.is_(True))
    tasks = list(session.scalars(q.order_by(EventTask.due_date, EventTask.sort_order, EventTask.id)).unique())
    if f.states:
        dep = dependency_map(session, tasks)
        tasks = [t for t in tasks if state_of(t, dep) in f.states or
                 ("overdue" in f.states and is_overdue(t)) or
                 ("blocked" in f.states and bool(blockers(t, dep)))]
    return tasks


# ---------------------------------------------------------------- computed state

def is_overdue(t: EventTask, today: date | None = None) -> bool:
    return t.status == "open" and t.due_date < (today or today_ist())


def dependency_map(session: Session, tasks: list[EventTask]) -> dict[int, dict[str, EventTask]]:
    """event_id -> {key: task} including the national parent's tasks for centre records."""
    event_ids = {t.event_id for t in tasks}
    parents = {}
    for eid in event_ids:
        ev = session.get(Event, eid)
        if ev is not None and ev.parent_event_id:
            parents[eid] = ev.parent_event_id
    all_ids = event_ids | set(parents.values())
    rows = list(session.scalars(select(EventTask).where(EventTask.event_id.in_(all_ids)))) if all_ids else []
    by_event: dict[int, dict[str, EventTask]] = {}
    for r in rows:
        by_event.setdefault(r.event_id, {})[r.key] = r
    out = {}
    for eid in event_ids:
        m = dict(by_event.get(parents.get(eid), {}))
        m.update(by_event.get(eid, {}))
        out[eid] = m
    return out


def blockers(t: EventTask, dep: dict[int, dict[str, EventTask]]) -> list[EventTask]:
    if t.status != "open":
        return []
    m = dep.get(t.event_id, {})
    return [m[k] for k in (t.depends_on or []) if k in m and m[k].status not in FINISHED]


def state_of(t: EventTask, dep: dict | None = None) -> str:
    if t.status == "done":
        return "done_late" if t.done_late else "done"
    if t.status != "open":
        return t.status
    if is_overdue(t):
        return "overdue"
    if dep is not None and blockers(t, dep):
        return "blocked"
    return "open"


STATE_LABELS = {
    "open": "Not started", "overdue": "Overdue", "blocked": "Blocked", "done": "Done", "done_late": "Done late",
    "waived": "Waived", "not_needed": "Not needed", "closed": "Closed",
}
STATE_ICONS = {
    "open": "☐", "overdue": "🔴", "blocked": "⛓️", "done": "☑️", "done_late": "🟧", "waived": "➖",
    "not_needed": "▫️", "closed": "✖️",
}


@dataclass
class Readiness:
    status: str | None
    reasons: list[str] = field(default_factory=list)
    total: int = 0
    done: int = 0
    done_late: int = 0
    waived: int = 0
    open: int = 0
    overdue: int = 0
    blocked: int = 0
    critical_open: int = 0
    not_needed: int = 0

    @property
    def label(self) -> str:
        return READINESS_LABELS.get(self.status, "No playbook") if self.status else "No playbook"

    @property
    def icon(self) -> str:
        return READINESS_ICONS.get(self.status, "") if self.status else ""

    @property
    def pct(self) -> int:
        counted = self.total - self.not_needed
        return round(100 * (self.done + self.waived) / counted) if counted else 0


def readiness(session: Session, ev: Event, tasks: list[EventTask] | None = None,
              dep: dict | None = None) -> Readiness:
    tasks = tasks if tasks is not None else event_tasks(session, ev.id)
    if not tasks:
        return Readiness(None)
    if Status(ev.status) in (Status.CANCELLED, Status.REJECTED):
        return Readiness("cancelled", ["Event cancelled or rejected"], total=len(tasks))
    dep = dep if dep is not None else dependency_map(session, tasks)
    today = today_ist()
    r = Readiness(None, total=len(tasks))
    for t in tasks:
        st = state_of(t, dep)
        if st in ("done", "done_late"):
            r.done += 1
            r.done_late += st == "done_late"
        elif st == "waived":
            r.waived += 1
        elif st == "not_needed":
            r.not_needed += 1
        elif t.status == "open":
            r.open += 1
            r.overdue += is_overdue(t, today)
            r.blocked += bool(blockers(t, dep))
    crit_pre = [t for t in tasks if t.is_critical and t.offset_days <= 0 and t.status == "open"]
    r.critical_open = len(crit_pre)
    if not crit_pre:
        r.status = "ready"
        return r
    centre_late, ho_late = [], []
    for t in crit_pre:
        late_ho_blockers = [b for b in blockers(t, dep) if b.scope == "national" and b.due_date < today]
        if late_ho_blockers and t.scope == "centre":
            ho_late.append((t, late_ho_blockers))
        elif t.due_date < today:
            centre_late.append(t)
    soon = [t for t in crit_pre if 0 <= (t.due_date - today).days <= AT_RISK_DAYS and t not in centre_late]
    for t in centre_late:
        d = (today - t.due_date).days
        r.reasons.append(f"{t.key} {t.title} – overdue by {d} day{'s' if d != 1 else ''}")
    for t, bl in ho_late:
        r.reasons.append(f"{t.key} waiting on Head Office: " + ", ".join(f"{b.key} {b.title}" for b in bl))
    for t in soon:
        if not any(t is x for x, _ in ho_late):
            d = (t.due_date - today).days
            r.reasons.append(f"{t.key} {t.title} – due {'today' if d == 0 else f'in {d} day(s)'}")
    if centre_late:
        r.status = "off_track"
    elif ho_late:
        r.status = "blocked_ho"
    elif soon:
        r.status = "at_risk"
    else:
        r.status = "on_track"
        nxt = min(crit_pre, key=lambda t: t.due_date)
        r.reasons.append(f"Next: {nxt.key} {nxt.title} due {fmt_date(nxt.due_date)}")
    return r


def completion_blockers(session: Session, ev: Event) -> list[EventTask]:
    """Critical post-event tasks that must be finished before the event can be marked Completed."""
    return [t for t in event_tasks(session, ev.id) if t.is_critical and t.offset_days > 0 and t.status == "open"]


# ---------------------------------------------------------------- actions

def _lock(session: Session, task_id: int, expected_version: int | None) -> EventTask:
    t = session.scalar(select(EventTask).where(EventTask.id == task_id).with_for_update(of=EventTask))
    if t is None:
        raise NotFound("Task not found.")
    if expected_version is not None and t.version != expected_version:
        raise ConflictError(f"{t.code} was changed by someone else after you opened it. Reload and try again.")
    return t


def _flush(session: Session, t: EventTask) -> None:
    try:
        session.flush()
    except StaleDataError as exc:
        raise ConflictError(f"{t.code} was changed by someone else while you were saving. Reload and retry.") from exc


def _require_manage_task(actor: User, t: EventTask) -> None:
    require_editor(actor)
    if t.scope == "national" or t.centre_id is None:
        require_ho(actor)
    else:
        require_manage_centre(actor, t.centre_id)


def _event_live(ev: Event) -> None:
    if Status(ev.status) in (Status.CANCELLED, Status.REJECTED):
        raise InvalidTransition("Tasks of a cancelled or rejected event cannot be changed.")


def complete_task(session: Session, actor: User, task_id: int, expected_version: int | None = None, *,
                  proof_url: str | None = None, proof_count: int | None = None, proof_count2: int | None = None,
                  notes: str | None = None) -> EventTask:
    t = _lock(session, task_id, expected_version)
    _require_manage_task(actor, t)
    _event_live(t.event)
    if t.status != "open":
        raise InvalidTransition(f"{t.key} is already {STATE_LABELS.get(t.status, t.status).lower()}.")
    if t.proof_type == "whatsapp_auto":
        raise InvalidTransition(
            f"{t.key} ticks itself when a WhatsApp campaign linked to this event is marked completed. "
            "Create or link the campaign, or ask Head Office to waive this task.")
    dep = dependency_map(session, [t])
    bl = blockers(t, dep)
    if bl:
        raise InvalidTransition(f"{t.key} is waiting on " + ", ".join(f"{b.key} {b.title}" for b in bl)
                                + ". Finish that first.")
    errors = []
    url = (proof_url or "").strip() or None
    if t.proof_type == "url":
        if not url or not url.startswith(("http://", "https://")):
            errors.append(f"{t.proof_label or 'Link'} is required and must start with http:// or https://")
    if t.proof_type in ("count", "seats"):
        if proof_count is None or proof_count < 0:
            errors.append(f"{(t.proof_label or 'Count').split(' / ')[0]} is required (0 or more).")
    if t.proof_type == "seats":
        if proof_count2 is None or proof_count2 < 0:
            errors.append("Registrations is required (0 or more).")
        elif proof_count is not None and proof_count < proof_count2:
            errors.append(f"Seats arranged ({proof_count}) are fewer than registrations ({proof_count2}). "
                          "Arrange more seats before marking the centre ready.")
    if errors:
        raise ValidationError(errors)
    t.status = "done"
    t.done_at = now_utc()
    t.done_by_id = actor.id
    t.done_late = to_ist(t.done_at).date() > t.due_date
    t.proof_url = url if t.proof_type == "url" else None
    t.proof_count = proof_count if t.proof_type in ("count", "seats") else None
    t.proof_count2 = proof_count2 if t.proof_type == "seats" else None
    t.notes = (notes or "").strip() or t.notes
    t.status_reason, t.status_changed_by_id = None, actor.id
    _flush(session, t)
    audit.log(session, actor, "task.done", "task", t.id,
              f"{t.event.code} {t.key} done{' (late)' if t.done_late else ''}{' by Head Office' if is_ho(actor) and t.scope == 'centre' else ''}",
              centre_id=t.centre_id, details={"event_id": t.event_id, "url": t.proof_url, "count": t.proof_count,
                                              "count2": t.proof_count2, "late": t.done_late})
    return t


def reopen_task(session: Session, actor: User, task_id: int, reason: str, expected_version: int | None = None) -> EventTask:
    t = _lock(session, task_id, expected_version)
    _require_manage_task(actor, t)
    _event_live(t.event)
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("Give a reason for reopening.")
    if t.status not in ("done", "waived"):
        raise InvalidTransition(f"{t.key} is not done or waived.")
    if not is_ho(actor):
        if t.status == "waived":
            raise PermissionDenied("Only Head Office can undo a waiver.")
        if today_ist() > t.event.event_date:
            raise PermissionDenied("After the event date only Head Office can reopen tasks.")
    before = {"status": t.status, "done_at": t.done_at, "done_by": t.done_by_id, "url": t.proof_url,
              "count": t.proof_count, "count2": t.proof_count2, "late": t.done_late}
    t.status, t.done_at, t.done_by_id, t.done_late = "open", None, None, False
    t.status_reason, t.status_changed_by_id = f"Reopened: {reason}", actor.id
    _flush(session, t)
    audit.log(session, actor, "task.reopened", "task", t.id, f"{t.event.code} {t.key} reopened: {reason}",
              centre_id=t.centre_id, details={"event_id": t.event_id, "previous": before})
    return t


def waive_task(session: Session, actor: User, task_id: int, reason: str, expected_version: int | None = None) -> EventTask:
    require_ho(actor)
    t = _lock(session, task_id, expected_version)
    _event_live(t.event)
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("A waiver needs a reason.")
    if t.status != "open":
        raise InvalidTransition(f"Only open tasks can be waived; {t.key} is {t.status}.")
    t.status, t.status_reason, t.status_changed_by_id = "waived", reason, actor.id
    _flush(session, t)
    audit.log(session, actor, "task.waived", "task", t.id, f"{t.event.code} {t.key} waived: {reason}",
              centre_id=t.centre_id, details={"event_id": t.event_id})
    return t


def set_task_notes(session: Session, actor: User, task_id: int, notes: str) -> EventTask:
    t = _lock(session, task_id, None)
    _require_manage_task(actor, t)
    t.notes = (notes or "").strip() or None
    _flush(session, t)
    return t


# ---------------------------------------------------------------- hooks called by the events service

def on_event_date_changed(session: Session, actor: User | None, ev: Event, old_date: date) -> list[tuple]:
    """Recalculate open tasks; done/waived tasks keep their original deadline and completion."""
    changes = []
    for t in event_tasks(session, ev.id):
        if t.status not in ("open", "not_needed"):
            continue
        new_due = ev.event_date + timedelta(days=t.offset_days)
        if new_due != t.due_date:
            changes.append((t.key, t.due_date, new_due))
            t.due_date = new_due
    if changes:
        session.flush()
        audit.log(session, actor, "tasks.rescheduled", "event", ev.id,
                  f"{ev.code} moved {fmt_date(old_date)} → {fmt_date(ev.event_date)}: {len(changes)} task deadline(s) recalculated",
                  centre_id=ev.centre_id, details={"changes": [{"key": k, "from": a, "to": b} for k, a, b in changes]})
    return changes


@dataclass
class DateChangeRow:
    key: str
    title: str
    status: str
    old_due: date
    new_due: date | None  # None = unchanged (finished task keeps its history)
    past: bool


def preview_date_change(session: Session, actor: User, event_id: int, new_date: date) -> list[DateChangeRow]:
    ev = session.get(Event, event_id)
    if ev is None:
        raise NotFound("Event not found.")
    require_view_centre(actor, ev.centre_id)
    today = today_ist()
    out = []
    for t in event_tasks(session, event_id):
        if t.status in ("open", "not_needed"):
            nd = new_date + timedelta(days=t.offset_days)
            out.append(DateChangeRow(t.key, t.title, t.status, t.due_date, nd, t.status == "open" and nd < today))
        else:
            out.append(DateChangeRow(t.key, t.title, t.status, t.due_date, None, False))
    return out


def on_event_status_changed(session: Session, actor: User, ev: Event, new_status: str) -> None:
    if new_status in (Status.CANCELLED, Status.REJECTED):
        n = 0
        for t in event_tasks(session, ev.id):
            if t.status == "open":
                t.status, t.status_reason, t.status_changed_by_id = "closed", CLOSED_FOR_EVENT, actor.id
                n += 1
        if n:
            session.flush()
            audit.log(session, actor, "tasks.closed", "event", ev.id, f"{ev.code}: {n} open task(s) closed with the event",
                      centre_id=ev.centre_id)
    elif new_status == Status.DRAFT:  # reopened by Head Office
        n = 0
        for t in event_tasks(session, ev.id):
            if t.status == "closed" and t.status_reason == CLOSED_FOR_EVENT:
                t.status, t.status_reason, t.status_changed_by_id = "open", "Reopened with the event", actor.id
                n += 1
        if n:
            session.flush()
    elif new_status == Status.COMPLETED and ev.linked_event_id and ev.category.key == "whatsapp_campaign":
        target = session.get(Event, ev.linked_event_id)
        if target is not None:
            _auto_complete_from_campaigns(session, actor, target)


def on_event_centre_changed(session: Session, ev: Event) -> None:
    for t in event_tasks(session, ev.id):
        t.centre_id = ev.centre_id
    session.flush()


def on_event_details_changed(session: Session, actor: User | None, ev: Event) -> None:
    """Apply skip_if_mode rules (e.g. 'Centre ready' is not needed for an online mock)."""
    mode = _mode(ev)
    for t in event_tasks(session, ev.id):
        if not t.skip_if_mode:
            continue
        if mode == t.skip_if_mode and t.status == "open":
            t.status, t.status_reason = "not_needed", f"Not needed – mode is {t.skip_if_mode}"
        elif mode != t.skip_if_mode and t.status == "not_needed":
            t.status, t.status_reason = "open", f"Needed again – mode changed to {mode or 'not set'}"
    session.flush()


def _auto_complete_from_campaigns(session: Session, actor: User | None, ev: Event) -> None:
    campaign = session.scalar(select(Event).join(EventCategory, Event.category_id == EventCategory.id)
                              .where(Event.linked_event_id == ev.id, EventCategory.key == "whatsapp_campaign",
                                     Event.status == Status.COMPLETED).order_by(Event.completed_at).limit(1))
    if campaign is None:
        return
    for t in event_tasks(session, ev.id):
        if t.proof_type == "whatsapp_auto" and t.status == "open":
            t.status = "done"
            t.done_at = campaign.completed_at or now_utc()
            t.done_by_id = actor.id if actor else campaign.updated_by_id
            t.done_late = to_ist(t.done_at).date() > t.due_date
            t.status_reason = f"Completed automatically by WhatsApp campaign {campaign.code}"
            session.flush()
            audit.log(session, actor, "task.done", "task", t.id,
                      f"{ev.code} {t.key} done automatically by {campaign.code}{' (late)' if t.done_late else ''}",
                      centre_id=t.centre_id, details={"event_id": ev.id, "campaign_id": campaign.id})


# ---------------------------------------------------------------- audit flags

def flag_task(session: Session, actor: User, task_id: int, flag_type: str, comment: str) -> TaskFlag:
    require_ho(actor)
    t = session.get(EventTask, task_id)
    if t is None:
        raise NotFound("Task not found.")
    comment = (comment or "").strip()
    if flag_type not in FLAG_TYPES:
        raise ValidationError("Choose a flag type.")
    if not comment:
        raise ValidationError("A flag needs a comment the Centre Head can act on.")
    f = TaskFlag(task_id=t.id, flag_type=flag_type, comment=comment, created_by_id=actor.id)
    session.add(f)
    session.flush()
    audit.log(session, actor, "task.flagged", "task", t.id, f"{t.event.code} {t.key} flagged: {comment[:200]}",
              centre_id=t.centre_id, details={"event_id": t.event_id, "type": flag_type})
    return f


def reply_flag(session: Session, actor: User, flag_id: int, reply: str) -> TaskFlag:
    f = session.get(TaskFlag, flag_id)
    if f is None:
        raise NotFound("Flag not found.")
    _require_manage_task(actor, f.task)
    reply = (reply or "").strip()
    if not reply:
        raise ValidationError("Reply cannot be empty.")
    f.centre_reply, f.replied_by_id, f.replied_at = reply, actor.id, now_utc()
    session.flush()
    audit.log(session, actor, "task.flag_replied", "task", f.task_id, f"Reply to audit flag on {f.task.key}",
              centre_id=f.task.centre_id)
    return f


def resolve_flag(session: Session, actor: User, flag_id: int) -> TaskFlag:
    require_ho(actor)
    f = session.get(TaskFlag, flag_id)
    if f is None:
        raise NotFound("Flag not found.")
    f.resolved_at, f.resolved_by_id = now_utc(), actor.id
    session.flush()
    audit.log(session, actor, "task.flag_resolved", "task", f.task_id, f"Audit flag on {f.task.key} resolved",
              centre_id=f.task.centre_id)
    return f


def list_flags(session: Session, actor: User, open_only: bool = True, task_ids: list[int] | None = None) -> list[TaskFlag]:
    q = select(TaskFlag).join(EventTask, TaskFlag.task_id == EventTask.id)
    scope = visible_centre_ids(actor)
    if scope is not None:
        q = q.where(or_(EventTask.centre_id.in_(scope), EventTask.centre_id.is_(None)))
    if open_only:
        q = q.where(TaskFlag.resolved_at.is_(None))
    if task_ids is not None:
        q = q.where(TaskFlag.task_id.in_(task_ids or [-1]))
    return list(session.scalars(q.order_by(TaskFlag.created_at.desc())).unique())


# ---------------------------------------------------------------- Head Office: playbook maintenance

def apply_latest_playbook(session: Session, actor: User, event_id: int) -> list[EventTask]:
    """Adds tasks from the active playbook that the event does not have. Never removes or resets tasks."""
    require_ho(actor)
    ev = session.get(Event, event_id)
    if ev is None:
        raise NotFound("Event not found.")
    _event_live(ev)
    pb = active_playbook(session, ev.category_id)
    if pb is None:
        raise ValidationError(f"{ev.category.name} has no playbook.")
    created = generate_tasks(session, actor, ev, pb)
    if not ev.is_national_parent and ev.parent_event_id is None:
        return created
    if ev.is_national_parent:  # also bring centre records up to date
        from jgevents.services.events import children
        for child in children(session, ev.id):
            created += generate_tasks(session, actor, child, pb)
    return created


def save_playbook(session: Session, actor: User, category_id: int, rows: list[dict], notes: str | None = None) -> Playbook:
    """Create a new version from edited rows. Existing events keep their tasks."""
    require_ho(actor)
    cat = session.get(EventCategory, category_id)
    if cat is None:
        raise NotFound("Category not found.")
    errors, keys, clean = [], set(), []
    for i, r in enumerate(rows, 1):
        key = str(r.get("key") or "").strip().upper()
        title = str(r.get("title") or "").strip()
        if not key and not title:
            continue
        where = f"Row {i}"
        if not key or len(key) > 10 or not key.replace("_", "").isalnum():
            errors.append(f"{where}: key must be 1–10 letters/digits (e.g. BL6).")
        if key in keys:
            errors.append(f"{where}: duplicate key {key}.")
        keys.add(key)
        if not title:
            errors.append(f"{where}: title is required.")
        try:
            off = int(r.get("offset_days"))
        except (TypeError, ValueError):
            errors.append(f"{where}: offset must be a whole number (e.g. -7 for T-7).")
            off = 0
        proof = str(r.get("proof_type") or "none")
        if proof not in PROOF_TYPES:
            errors.append(f"{where}: proof type must be one of {sorted(PROOF_TYPES)}.")
        scope = str(r.get("scope") or "centre")
        if scope not in ("centre", "national"):
            errors.append(f"{where}: scope must be centre or national.")
        if scope == "national" and not cat.supports_national:
            errors.append(f"{where}: national tasks are only allowed for categories with national events.")
        deps = r.get("depends_on") or []
        if isinstance(deps, str):
            deps = [d.strip().upper() for d in deps.split(",") if d.strip()]
        clean.append(dict(key=key, title=title[:200], offset_days=off, owner_label=str(r.get("owner_label") or "Centre Head")[:60],
                          is_critical=bool(r.get("is_critical")), scope=scope, depends_on=deps, proof_type=proof,
                          proof_label=(str(r.get("proof_label") or "").strip() or None),
                          skip_if_mode=(str(r.get("skip_if_mode") or "").strip() or None)))
    if not clean:
        errors.append("A playbook needs at least one task.")
    for c in clean:
        for d in c["depends_on"]:
            if d not in keys:
                errors.append(f"{c['key']}: depends on unknown task {d}.")
            elif d == c["key"]:
                errors.append(f"{c['key']}: cannot depend on itself.")
    graph = {c["key"]: set(c["depends_on"]) for c in clean}
    seen, stack = set(), set()

    def cyclic(k: str) -> bool:
        if k in stack:
            return True
        if k in seen:
            return False
        seen.add(k)
        stack.add(k)
        res = any(cyclic(d) for d in graph.get(k, ()) if d in graph)
        stack.discard(k)
        return res
    if any(cyclic(k) for k in graph):
        errors.append("Dependencies form a loop – remove one.")
    if errors:
        raise ValidationError(errors)
    current = playbook_versions(session, category_id)
    version = (current[0].version + 1) if current else 1
    for p in current:
        p.is_active = False
    pb = Playbook(category_id=category_id, version=version, is_active=True, notes=(notes or "").strip() or None,
                  created_by_id=actor.id)
    session.add(pb)
    session.flush()
    for i, c in enumerate(clean):
        session.add(PlaybookTask(playbook_id=pb.id, sort_order=i, **c))
    session.flush()
    session.refresh(pb)
    audit.log(session, actor, "playbook.saved", "playbook", pb.id, f"{cat.name} playbook v{version} saved ({len(clean)} tasks)",
              details={"tasks": [c["key"] for c in clean], "notes": notes})
    return pb


def all_playbook_categories(session: Session) -> list[EventCategory]:
    ids = {p.category_id for p in session.scalars(select(Playbook))}
    return [c for c in session.scalars(select(EventCategory).order_by(EventCategory.sort_order)) if c.id in ids]

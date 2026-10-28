"""Monthly planning cycle: one plan per centre per month, submitted as a unit."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from jgevents.db.models import ApprovalAction, Centre, Event, EventCategory, MonthlyPlan, User
from jgevents.domain.constants import (
    APPROVED_LIKE,
    LIVE_STATUSES,
    PLAN_STATUS_LABELS,
    Action,
    PlanStatus,
    Requirement,
    Status,
)
from jgevents.domain.schema import validate_event_payload
from jgevents.services import audit, config_service
from jgevents.services.errors import DuplicateSubmission, NotFound, ValidationError
from jgevents.services.permissions import (
    require_ho,
    require_manage_centre,
    require_view_centre,
    visible_centre_ids,
)
from jgevents.utils.dates import month_bounds, month_label, now_utc, today_ist


def _plan_query(centre_id: int, year: int, month: int):
    return select(MonthlyPlan).where(MonthlyPlan.centre_id == centre_id, MonthlyPlan.year == year,
                                     MonthlyPlan.month == month)


def get_or_create_plan(session: Session, centre_id: int, year: int, month: int, lock: bool = False) -> MonthlyPlan:
    q = _plan_query(centre_id, year, month)
    plan = session.scalar(q.with_for_update(of=MonthlyPlan) if lock else q)
    if plan:
        return plan
    try:
        with session.begin_nested():
            plan = MonthlyPlan(centre_id=centre_id, year=year, month=month, status=PlanStatus.DRAFT)
            session.add(plan)
    except IntegrityError:  # created concurrently by another user
        pass
    return session.scalar(q.with_for_update(of=MonthlyPlan) if lock else q)


def find_plan(session: Session, centre_id: int, year: int, month: int) -> MonthlyPlan | None:
    return session.scalar(_plan_query(centre_id, year, month))


def month_events(session: Session, centre_id: int, year: int, month: int) -> list[Event]:
    start, end = month_bounds(year, month)
    return list(session.scalars(select(Event).where(
        Event.centre_id == centre_id, Event.event_date >= start, Event.event_date <= end,
        Event.is_national_parent.is_(False)).order_by(Event.event_date, Event.start_time, Event.id)).unique())


# ---------------------------------------------------------------- completeness

@dataclass
class CategoryLine:
    category: EventCategory
    requirement: str
    min_count: int
    planned: int

    @property
    def missing(self) -> int:
        if self.requirement != Requirement.MANDATORY:
            return 0
        return max(self.min_count - self.planned, 0)


@dataclass
class Completeness:
    centre: Centre
    year: int
    month: int
    plan: MonthlyPlan | None
    deadline: date
    lines: list[CategoryLine]
    incomplete: list[tuple[Event, list[str]]]
    status_counts: dict[str, int]
    unsubmitted: int
    total: int
    proposed: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def plan_status(self) -> str:
        return self.plan.status if self.plan else PlanStatus.DRAFT

    @property
    def plan_status_label(self) -> str:
        return PLAN_STATUS_LABELS[PlanStatus(self.plan_status)]

    @property
    def submitted(self) -> bool:
        return bool(self.plan and self.plan.submission_count > 0)

    @property
    def missing_mandatory(self) -> list[CategoryLine]:
        return [ln for ln in self.lines if ln.missing]

    @property
    def mandatory_lines(self) -> list[CategoryLine]:
        return [ln for ln in self.lines if ln.requirement == Requirement.MANDATORY]

    @property
    def score(self) -> int:
        """0-100: share of mandatory slots filled and events complete."""
        need = sum(ln.min_count for ln in self.mandatory_lines)
        have = sum(min(ln.planned, ln.min_count) for ln in self.mandatory_lines)
        parts = []
        if need:
            parts.append(have / need)
        if self.total:
            parts.append(1 - len(self.incomplete) / self.total)
        return round(100 * sum(parts) / len(parts)) if parts else 0

    @property
    def is_complete(self) -> bool:
        return self.total > 0 and not self.missing_mandatory and not self.incomplete

    @property
    def days_to_deadline(self) -> int:
        return (self.deadline - today_ist()).days

    @property
    def overdue(self) -> bool:
        return not self.submitted and self.days_to_deadline < 0


def completeness(session: Session, actor: User, centre_id: int, year: int, month: int) -> Completeness:
    require_view_centre(actor, centre_id)
    centre = session.get(Centre, centre_id)
    if not centre:
        raise NotFound("Centre not found.")
    events = month_events(session, centre_id, year, month)
    live = [e for e in events if Status(e.status) in LIVE_STATUSES]
    reqs = config_service.effective_requirements(session, centre_id)
    lines = []
    for cat in config_service.list_categories(session):
        req, n = reqs.get(cat.id, (Requirement.OPTIONAL, 0))
        if req == Requirement.NOT_APPLICABLE:
            continue
        planned = sum(1 for e in live if e.category_id == cat.id)
        lines.append(CategoryLine(cat, req, n, planned))
    incomplete = []
    for e in live:
        if e.status in (Status.DRAFT, Status.RETURNED):
            errs = validate_event_payload(e.category, {k: getattr(e, k) for k in (
                "title", "subtype", "event_date", "start_time", "end_time", "description", "objective",
                "responsible_person", "expected_participation", "target_audience", "venue",
                "preparation_deadline")}, e.details or {}, strict=True)
            if errs:
                incomplete.append((e, errs))
    counts: dict[str, int] = {}
    for e in events:
        counts[e.status] = counts.get(e.status, 0) + 1
    return Completeness(
        centre=centre, year=year, month=month, plan=find_plan(session, centre_id, year, month),
        deadline=config_service.planning_deadline(session, year, month), lines=lines, incomplete=incomplete,
        status_counts=counts, unsubmitted=sum(1 for e in live if e.status in (Status.DRAFT, Status.RETURNED)),
        total=len(live), proposed=sum(1 for e in live if e.is_proposed),
    )


def network_overview(session: Session, actor: User, year: int, month: int) -> list[Completeness]:
    scope = visible_centre_ids(actor)
    q = select(Centre).where(Centre.is_active.is_(True)).order_by(Centre.sort_order, Centre.name)
    if scope is not None:
        q = q.where(Centre.id.in_(scope))
    return [completeness(session, actor, c.id, year, month) for c in session.scalars(q)]


# ---------------------------------------------------------------- status maintenance

def refresh_plan_status(session: Session, centre_id: int, year: int, month: int) -> None:
    plan = find_plan(session, centre_id, year, month)
    if plan is None:
        if month_events(session, centre_id, year, month):
            get_or_create_plan(session, centre_id, year, month)
        return
    if plan.submission_count == 0:
        return  # nothing reviewed yet; stays draft
    statuses = {e.status for e in month_events(session, centre_id, year, month)
                if Status(e.status) in LIVE_STATUSES}
    if Status.RETURNED in statuses:
        new = PlanStatus.RETURNED
    elif Status.SUBMITTED in statuses:
        new = PlanStatus.SUBMITTED
    elif statuses & APPROVED_LIKE:
        new = PlanStatus.APPROVED
    else:
        new = PlanStatus(plan.status)
    if new != plan.status:
        plan.status = new
        session.flush()


# ---------------------------------------------------------------- submit / review

def submit_plan(session: Session, actor: User, centre_id: int, year: int, month: int,
                note: str | None = None) -> MonthlyPlan:
    require_manage_centre(actor, centre_id)
    from jgevents.services import events as ev_svc

    plan = get_or_create_plan(session, centre_id, year, month, lock=True)
    events = month_events(session, centre_id, year, month)
    pending = [e for e in events if e.status in (Status.DRAFT, Status.RETURNED)]
    live = [e for e in events if Status(e.status) in LIVE_STATUSES]
    if not live:
        raise ValidationError(f"Add at least one event before submitting the {month_label(year, month)} plan.")
    if not pending:
        if plan.submission_count > 0:
            raise DuplicateSubmission(
                f"The {month_label(year, month)} plan is already submitted and nothing new or revised is waiting. "
                "Add or revise events to resubmit.")
        raise ValidationError("There are no draft events to submit.")
    comp = completeness(session, actor, centre_id, year, month)
    errors = []
    for e, errs in comp.incomplete:
        errors.append(f"{e.code} '{e.title}': " + "; ".join(errs))
    note = (note or "").strip() or None
    if comp.missing_mandatory and not note:
        missing = ", ".join(f"{ln.category.name} (need {ln.min_count}, have {ln.planned})"
                            for ln in comp.missing_mandatory)
        errors.append(f"Mandatory activities missing: {missing}. Plan them, or add a note explaining why.")
    if errors:
        raise ValidationError(errors)
    for e in pending:
        ev_svc.transition(session, actor, e.id, Action.SUBMIT)
    plan.status = PlanStatus.SUBMITTED
    plan.submission_count += 1
    plan.submitted_at, plan.submitted_by_id = now_utc(), actor.id
    if note:
        plan.centre_note = note
    session.flush()
    label = "resubmitted" if plan.submission_count > 1 else "submitted"
    session.add(ApprovalAction(plan_id=plan.id, action=f"plan_{label}", from_status=None,
                               to_status=PlanStatus.SUBMITTED, comment=note, actor_id=actor.id))
    audit.log(session, actor, f"plan.{label}", "plan", plan.id,
              f"{plan.centre.name} plan for {month_label(year, month)} {label} ({len(pending)} event(s))",
              centre_id=centre_id, details={"events": [e.id for e in pending], "note": note})
    return plan


def review_plan(session: Session, actor: User, plan_id: int, decision: str, comment: str | None = None) -> MonthlyPlan:
    """decision: 'approve' approves every submitted event in the plan; 'return' sends them all back."""
    require_ho(actor)
    from jgevents.services import events as ev_svc

    plan = session.get(MonthlyPlan, plan_id)
    if not plan:
        raise NotFound("Plan not found.")
    if decision not in ("approve", "return"):
        raise ValidationError("Decision must be approve or return.")
    comment = (comment or "").strip() or None
    if decision == "return" and not comment:
        raise ValidationError("Add a comment explaining what needs to change.")
    pending = [e for e in month_events(session, plan.centre_id, plan.year, plan.month) if e.status == Status.SUBMITTED]
    if not pending:
        raise ValidationError("No submitted events are waiting in this plan.")
    action = Action.APPROVE if decision == "approve" else Action.RETURN
    for e in pending:
        ev_svc.transition(session, actor, e.id, action, comment)
    plan.reviewed_at, plan.reviewed_by_id, plan.review_comment = now_utc(), actor.id, comment
    session.add(ApprovalAction(plan_id=plan.id, action=f"plan_{decision}", comment=comment, actor_id=actor.id,
                               to_status=plan.status))
    audit.log(session, actor, f"plan.{decision}", "plan", plan.id,
              f"{plan.centre.name} plan {month_label(plan.year, plan.month)}: {decision} ({len(pending)} event(s))",
              centre_id=plan.centre_id, details={"comment": comment})
    session.flush()
    return plan


def plan_history(session: Session, actor: User, plan: MonthlyPlan) -> list[ApprovalAction]:
    require_view_centre(actor, plan.centre_id)
    return list(session.scalars(select(ApprovalAction).where(ApprovalAction.plan_id == plan.id)
                                .order_by(ApprovalAction.created_at)))

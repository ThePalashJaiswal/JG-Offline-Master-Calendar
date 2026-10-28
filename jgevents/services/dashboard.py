"""Head Office / centre metrics. Each metric carries the ids of the records behind
it so the UI can drill into the underlying events."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy.orm import Session

from jgevents.db.models import Event, User
from jgevents.domain.constants import APPROVED_LIKE, LIVE_STATUSES, Status
from jgevents.services import config_service, planning
from jgevents.services import events as ev_svc
from jgevents.services.directory import list_centres
from jgevents.services.events import EventFilter
from jgevents.utils.dates import month_bounds, today_ist


@dataclass
class Metric:
    label: str
    ids: list[int] = field(default_factory=list)
    centre_ids: list[int] = field(default_factory=list)
    help: str = ""

    @property
    def value(self) -> int:
        return len(self.centre_ids) if self.centre_ids and not self.ids else len(self.ids)


@dataclass
class DashboardData:
    metrics: dict[str, Metric]
    by_category: Counter
    by_centre: Counter
    events: list[Event]
    overview: list
    completion_rate: float | None
    upcoming: list[Event]


def build(session: Session, actor: User, year: int, month: int, base: EventFilter | None = None) -> DashboardData:
    base = base or EventFilter()
    start, end = month_bounds(year, month)
    f = EventFilter(date_from=start, date_to=end, centre_ids=base.centre_ids, category_ids=base.category_ids,
                    statuses=base.statuses, approval_statuses=base.approval_statuses, responsible=base.responsible)
    events = ev_svc.list_events(session, actor, f)
    live = [e for e in events if Status(e.status) in LIVE_STATUSES]
    window = int(config_service.get_setting(session, "at_risk_window_days") or 7)
    today = today_ist()

    def ids(pred):
        return [e.id for e in events if pred(e)]

    centres = list_centres(session, actor)
    if base.centre_ids:
        centres = [c for c in centres if c.id in base.centre_ids]
    overview = [c for c in planning.network_overview(session, actor, year, month) if c.centre.id in {x.id for x in centres}]

    m = {
        "centres": Metric("Active centres", centre_ids=[c.id for c in centres]),
        "planned": Metric("Planned events", [e.id for e in live], help="All non-cancelled, non-rejected events"),
        "draft": Metric("Not yet submitted", ids(lambda e: e.status == Status.DRAFT)),
        "pending": Metric("Pending approval", ids(lambda e: e.status == Status.SUBMITTED)),
        "approved": Metric("Approved", ids(lambda e: e.status in APPROVED_LIKE)),
        "returned": Metric("Returned / rejected", ids(lambda e: e.status in (Status.RETURNED, Status.REJECTED))),
        "completed": Metric("Completed", ids(lambda e: e.status == Status.COMPLETED)),
        "cancelled": Metric("Cancelled", ids(lambda e: e.status == Status.CANCELLED)),
        "at_risk": Metric("Overdue / at risk", ids(lambda e: bool(ev_svc.risk_flags(e, window))),
                          help=f"Past events without an outcome, or unapproved events within {window} days"),
        "not_submitted": Metric("Plans not submitted",
                                centre_ids=[o.centre.id for o in overview if not o.submitted]),
        "incomplete": Metric("Incomplete plans", centre_ids=[o.centre.id for o in overview if not o.is_complete]),
    }
    due = [e for e in events if e.status in APPROVED_LIKE and e.event_date <= today]
    rate = (sum(1 for e in due if e.status == Status.COMPLETED) / len(due)) if due else None
    up_f = EventFilter(date_from=today, date_to=today + timedelta(days=7), centre_ids=base.centre_ids,
                       category_ids=base.category_ids, statuses=base.statuses,
                       approval_statuses=base.approval_statuses, responsible=base.responsible)
    upcoming = [e for e in ev_svc.list_events(session, actor, up_f) if Status(e.status) in LIVE_STATUSES]
    m["upcoming"] = Metric("Next 7 days", [e.id for e in upcoming])
    return DashboardData(
        metrics=m,
        by_category=Counter(e.category.name for e in live),
        by_centre=Counter(e.centre.name for e in live if e.centre),
        events=events, overview=overview, completion_rate=rate, upcoming=upcoming,
    )

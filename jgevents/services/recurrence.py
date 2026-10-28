"""Recurring events are never created silently: rules produce *proposals* that a
person reviews; generating them creates draft events flagged `is_proposed`
which still need editing/submission and Head Office approval."""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from jgevents.db.models import Centre, Event, RecurrenceRule, User
from jgevents.services import audit, config_service
from jgevents.services.errors import ValidationError
from jgevents.services.permissions import can_manage_centre, require_editor
from jgevents.utils.dates import clamp_day, last_weekday_of_month, month_bounds, nth_weekday_of_month


def occurrence_date(rule: RecurrenceRule, year: int, month: int) -> date | None:
    if rule.rule_type == "last_weekday":
        return last_weekday_of_month(year, month, rule.weekday)
    if rule.rule_type == "nth_weekday":
        return nth_weekday_of_month(year, month, rule.weekday, rule.nth or 1)
    if rule.rule_type == "fixed_day":
        return clamp_day(year, month, rule.day_of_month or 1)
    return None


def describe_rule(rule: RecurrenceRule) -> str:
    wd = calendar.day_name[rule.weekday] if rule.weekday is not None else ""
    if rule.rule_type == "last_weekday":
        return f"Last {wd} of each month"
    if rule.rule_type == "nth_weekday":
        suffix = {1: "1st", 2: "2nd", 3: "3rd"}.get(rule.nth or 1, f"{rule.nth}th")
        return f"{suffix} {wd} of each month"
    return f"Day {rule.day_of_month} of each month (or last day if shorter)"


@dataclass
class Proposal:
    rule: RecurrenceRule
    centre: Centre
    date: date
    title: str
    exists: bool


def proposals(session: Session, actor: User, year: int, month: int, centre_ids: list[int] | None = None) -> list[Proposal]:
    start, end = month_bounds(year, month)
    out = []
    centres = list(session.scalars(select(Centre).where(Centre.is_active.is_(True)).order_by(Centre.sort_order)))
    for rule in config_service.list_rules(session, active_only=True):
        if not rule.is_active:
            continue
        d = occurrence_date(rule, year, month)
        if d is None:
            continue
        for c in centres:
            if rule.centre_ids and c.id not in rule.centre_ids:
                continue
            if centre_ids and c.id not in centre_ids:
                continue
            if not can_manage_centre(actor, c.id):
                continue
            exists = session.scalar(select(Event.id).where(
                Event.recurrence_rule_id == rule.id, Event.centre_id == c.id,
                Event.event_date >= start, Event.event_date <= end)) is not None
            title = rule.title_template.format(centre=c.name, month_name=calendar.month_name[month], year=year,
                                               month=month)
            out.append(Proposal(rule, c, d, title, exists))
    return out


def generate(session: Session, actor: User, year: int, month: int, selected: list[tuple[int, int]]) -> list[Event]:
    """selected: (rule_id, centre_id) pairs the user ticked. Creates drafts only."""
    require_editor(actor)
    from jgevents.services import events as ev_svc

    available = {(p.rule.id, p.centre.id): p for p in proposals(session, actor, year, month)}
    created = []
    for key in selected:
        p = available.get(tuple(key))
        if p is None:
            raise ValidationError("A selected proposal is not available to you.")
        if p.exists:
            continue
        defaults = dict(p.rule.defaults or {})
        common = {"title": p.title, "event_date": p.date, "start_time": p.rule.start_time,
                  "end_time": p.rule.end_time, **{k: v for k, v in defaults.items()
                                                  if k in ev_svc.PLAN_FIELDS and k not in ("title", "event_date")}}
        details = defaults.get("details", {})
        ev, _ = ev_svc.create_event(session, actor, category_id=p.rule.category_id, centre_id=p.centre.id,
                                    common=common, details=details, is_proposed=True,
                                    recurrence_rule_id=p.rule.id)
        created.append(ev)
    if created:
        audit.log(session, actor, "recurrence.generated", "recurrence_rule", None,
                  f"{len(created)} proposed event(s) generated for {month:02d}-{year}",
                  details={"events": [e.id for e in created]})
    return created

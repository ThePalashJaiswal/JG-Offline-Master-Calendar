"""Head Office configuration: settings, categories, requirements, deadlines, recurrence rules."""
from __future__ import annotations

import re
from datetime import date, time
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from jgevents.db.models import (
    AppSetting,
    CategoryRequirement,
    EventCategory,
    PlanningCycle,
    RecurrenceRule,
    User,
)
from jgevents.db.seed import DEFAULT_SETTINGS
from jgevents.domain.constants import CATEGORY_COLOURS, Requirement
from jgevents.domain.schema import validate_field_schema
from jgevents.services import audit
from jgevents.services.errors import NotFound, ValidationError
from jgevents.services.permissions import require_ho
from jgevents.utils.dates import add_months, clamp_day

# ---------------------------------------------------------------- settings


def get_setting(session: Session, key: str) -> Any:
    row = session.get(AppSetting, key)
    return row.value if row else DEFAULT_SETTINGS.get(key)


def set_setting(session: Session, actor: User, key: str, value: Any) -> None:
    require_ho(actor)
    row = session.get(AppSetting, key)
    old = row.value if row else None
    if row:
        row.value = value
    else:
        session.add(AppSetting(key=key, value=value))
    audit.log(session, actor, "setting.updated", "setting", None, f"Setting {key} changed",
              details={"key": key, "old": old, "new": value})


# ---------------------------------------------------------------- deadlines

def planning_deadline(session: Session, year: int, month: int) -> date:
    override = session.scalar(select(PlanningCycle).where(PlanningCycle.year == year, PlanningCycle.month == month))
    if override:
        return override.deadline
    day = int(get_setting(session, "planning_deadline_day") or 25)
    py, pm = add_months(year, month, -1)
    return clamp_day(py, pm, day)


def set_planning_deadline(session: Session, actor: User, year: int, month: int, deadline: date,
                          notes: str | None = None) -> PlanningCycle:
    require_ho(actor)
    row = session.scalar(select(PlanningCycle).where(PlanningCycle.year == year, PlanningCycle.month == month))
    if row:
        row.deadline, row.notes = deadline, notes
    else:
        row = PlanningCycle(year=year, month=month, deadline=deadline, notes=notes)
        session.add(row)
    session.flush()
    audit.log(session, actor, "planning.deadline_set", "planning_cycle", row.id,
              f"Planning deadline for {month:02d}-{year} set to {deadline.strftime('%d-%m-%Y')}")
    return row


def list_planning_cycles(session: Session) -> list[PlanningCycle]:
    return list(session.scalars(select(PlanningCycle).order_by(PlanningCycle.year.desc(), PlanningCycle.month.desc())))


# ---------------------------------------------------------------- categories

def list_categories(session: Session, include_inactive: bool = False) -> list[EventCategory]:
    q = select(EventCategory).order_by(EventCategory.sort_order, EventCategory.name)
    if not include_inactive:
        q = q.where(EventCategory.is_active.is_(True))
    return list(session.scalars(q))


def get_category(session: Session, category_id: int) -> EventCategory:
    cat = session.get(EventCategory, category_id)
    if not cat:
        raise NotFound("Category not found.")
    return cat


def category_by_key(session: Session, key: str) -> EventCategory | None:
    return session.scalar(select(EventCategory).where(EventCategory.key == key))


_CATEGORY_FIELDS = {"name", "description", "colour", "icon", "group", "is_informal", "field_schema",
                    "common_overrides", "subtype_options", "default_checklist", "occupies_slot",
                    "conflict_policy", "supports_national", "link_target_key", "sort_order", "is_active"}


def _validate_category(values: dict) -> None:
    errors = []
    if "name" in values and not (values["name"] or "").strip():
        errors.append("Name is required.")
    if "colour" in values and values["colour"] not in CATEGORY_COLOURS:
        errors.append(f"Colour must be one of {CATEGORY_COLOURS}.")
    if "conflict_policy" in values and values["conflict_policy"] not in {"block", "warn", "none"}:
        errors.append("Conflict policy must be block, warn or none.")
    if "field_schema" in values:
        errors += validate_field_schema(values["field_schema"])
    for list_key in ("subtype_options", "default_checklist"):
        if list_key in values and not isinstance(values[list_key], list):
            errors.append(f"{list_key} must be a list.")
    if "common_overrides" in values and not isinstance(values["common_overrides"], dict):
        errors.append("common_overrides must be an object.")
    if errors:
        raise ValidationError(errors)


def create_category(session: Session, actor: User, key: str, name: str, **values) -> EventCategory:
    require_ho(actor)
    key = (key or "").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,49}", key):
        raise ValidationError("Key must be 3–50 chars: lowercase letters, digits, underscore.")
    values = {k: v for k, v in values.items() if k in _CATEGORY_FIELDS}
    values["name"] = name
    _validate_category(values)
    cat = EventCategory(key=key, is_system=False, **values)
    try:
        with session.begin_nested():  # savepoint: a duplicate must not undo the caller's other work
            session.add(cat)
            session.flush()
    except IntegrityError as exc:
        raise ValidationError("A category with this key already exists.") from exc
    session.add(CategoryRequirement(category_id=cat.id, centre_id=None, requirement=Requirement.OPTIONAL,
                                    min_count=0))
    audit.log(session, actor, "category.created", "category", cat.id, f"Category {name} created")
    return cat


def update_category(session: Session, actor: User, category_id: int, **values) -> EventCategory:
    require_ho(actor)
    cat = get_category(session, category_id)
    unknown = set(values) - _CATEGORY_FIELDS
    if unknown:
        raise ValidationError(f"Unknown fields: {', '.join(sorted(unknown))}")
    _validate_category(values)
    before = {k: getattr(cat, k) for k in values}
    for k, v in values.items():
        setattr(cat, k, v)
    session.flush()
    audit.log(session, actor, "category.updated", "category", cat.id, f"Category {cat.name} updated",
              details={"before": before, "after": values})
    return cat


# ---------------------------------------------------------------- requirements

def effective_requirements(session: Session, centre_id: int) -> dict[int, tuple[str, int]]:
    rows = list(session.scalars(select(CategoryRequirement).where(
        (CategoryRequirement.centre_id.is_(None)) | (CategoryRequirement.centre_id == centre_id))))
    out: dict[int, tuple[str, int]] = {}
    for r in sorted(rows, key=lambda r: r.centre_id is not None):  # centre overrides last
        out[r.category_id] = (r.requirement, r.min_count)
    return out


def list_requirements(session: Session) -> list[CategoryRequirement]:
    return list(session.scalars(select(CategoryRequirement)))


def set_requirement(session: Session, actor: User, category_id: int, centre_id: int | None,
                    requirement: str, min_count: int) -> CategoryRequirement | None:
    """centre_id=None sets the network default. requirement='inherit' removes a centre override."""
    require_ho(actor)
    q = select(CategoryRequirement).where(CategoryRequirement.category_id == category_id)
    q = q.where(CategoryRequirement.centre_id.is_(None) if centre_id is None
                else CategoryRequirement.centre_id == centre_id)
    row = session.scalar(q)
    if requirement == "inherit":
        if centre_id is None:
            raise ValidationError("The network default cannot inherit.")
        if row:
            session.delete(row)
            audit.log(session, actor, "requirement.cleared", "category", category_id,
                      "Centre requirement override removed", centre_id=centre_id)
        return None
    if requirement not in {r.value for r in Requirement}:
        raise ValidationError("Invalid requirement.")
    if min_count < 0:
        raise ValidationError("Minimum count cannot be negative.")
    if requirement == Requirement.MANDATORY and min_count < 1:
        min_count = 1
    if row:
        row.requirement, row.min_count = requirement, min_count
    else:
        row = CategoryRequirement(category_id=category_id, centre_id=centre_id, requirement=requirement,
                                  min_count=min_count)
        session.add(row)
    session.flush()
    audit.log(session, actor, "requirement.set", "category", category_id,
              f"Requirement set to {requirement} (min {min_count})", centre_id=centre_id)
    return row


# ---------------------------------------------------------------- recurrence rules

def list_rules(session: Session, active_only: bool = False) -> list[RecurrenceRule]:
    q = select(RecurrenceRule).order_by(RecurrenceRule.id)
    if active_only:
        q = q.where(RecurrenceRule.is_active.is_(True))
    return list(session.scalars(q))


def save_rule(session: Session, actor: User, rule_id: int | None, *, category_id: int, name: str,
              rule_type: str, weekday: int | None, nth: int | None, day_of_month: int | None,
              start_time: time | None, end_time: time | None, title_template: str,
              centre_ids: list[int] | None, defaults: dict | None, is_active: bool) -> RecurrenceRule:
    require_ho(actor)
    errors = []
    if rule_type not in {"last_weekday", "nth_weekday", "fixed_day"}:
        errors.append("Invalid rule type.")
    if rule_type in {"last_weekday", "nth_weekday"} and weekday not in range(7):
        errors.append("Choose a weekday.")
    if rule_type == "nth_weekday" and nth not in range(1, 6):
        errors.append("Occurrence must be 1–5.")
    if rule_type == "fixed_day" and (day_of_month is None or not 1 <= day_of_month <= 31):
        errors.append("Day of month must be 1–31.")
    if start_time and end_time and end_time <= start_time:
        errors.append("End time must be after start time.")
    if not (title_template or "").strip():
        errors.append("Title template is required.")
    if errors:
        raise ValidationError(errors)
    rule = session.get(RecurrenceRule, rule_id) if rule_id else RecurrenceRule()
    if rule_id and not rule:
        raise NotFound("Rule not found.")
    for k, v in dict(category_id=category_id, name=name, rule_type=rule_type, weekday=weekday, nth=nth,
                     day_of_month=day_of_month, start_time=start_time, end_time=end_time,
                     title_template=title_template, centre_ids=centre_ids or None, defaults=defaults or {},
                     is_active=is_active).items():
        setattr(rule, k, v)
    if not rule_id:
        session.add(rule)
    session.flush()
    audit.log(session, actor, "recurrence.saved", "recurrence_rule", rule.id, f"Recurrence rule '{name}' saved")
    return rule

"""Relational schema (SQLAlchemy 2.0, PostgreSQL in production, SQLite for local dev)."""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from jgevents.domain.constants import PlanStatus, Requirement, Role, Status
from jgevents.utils.dates import now_utc

JSONType = JSON().with_variant(JSONB(), "postgresql")


def _in(col: str, values) -> str:
    return f"{col} IN ({', '.join(repr(str(v)) for v in values)})"


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )


class Centre(TimestampMixin, Base):
    __tablename__ = "centres"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(10), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    city: Mapped[str | None] = mapped_column(String(100))
    address: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint(_in("role", Role), name="ck_users_role"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(20))
    password_hash: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    scope_all_centres: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    centres: Mapped[list[Centre]] = relationship(secondary="user_centres", lazy="selectin")


class UserCentre(Base):
    __tablename__ = "user_centres"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    centre_id: Mapped[int] = mapped_column(ForeignKey("centres.id", ondelete="RESTRICT"), primary_key=True)


class EventCategory(TimestampMixin, Base):
    """A configurable event type. Category-specific fields live in `field_schema`
    so admins can add categories without code changes."""
    __tablename__ = "event_categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    colour: Mapped[str] = mapped_column(String(20), default="gray", nullable=False)
    icon: Mapped[str] = mapped_column(String(8), default="📌", nullable=False)
    group: Mapped[str] = mapped_column(String(20), default="academic", nullable=False)  # academic|marketing|engagement|assessment
    is_informal: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    field_schema: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list, nullable=False)
    common_overrides: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    subtype_options: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    default_checklist: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    occupies_slot: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    conflict_policy: Mapped[str] = mapped_column(String(10), default="warn", nullable=False)  # block|warn|none
    supports_national: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    link_target_key: Mapped[str | None] = mapped_column(String(50))
    sort_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class CategoryRequirement(Base):
    """Per-category planning requirement; centre_id NULL = network default."""
    __tablename__ = "category_requirements"
    __table_args__ = (
        UniqueConstraint("category_id", "centre_id", name="uq_catreq_category_centre",
                         postgresql_nulls_not_distinct=True),
        CheckConstraint(_in("requirement", Requirement), name="ck_catreq_requirement"),
        CheckConstraint("min_count >= 0", name="ck_catreq_min_count"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("event_categories.id", ondelete="CASCADE"), nullable=False)
    centre_id: Mapped[int | None] = mapped_column(ForeignKey("centres.id", ondelete="CASCADE"))
    requirement: Mapped[str] = mapped_column(String(20), nullable=False)
    min_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class RecurrenceRule(TimestampMixin, Base):
    __tablename__ = "recurrence_rules"
    __table_args__ = (
        CheckConstraint(_in("rule_type", ["last_weekday", "nth_weekday", "fixed_day"]), name="ck_rr_type"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("event_categories.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    rule_type: Mapped[str] = mapped_column(String(20), nullable=False)
    weekday: Mapped[int | None] = mapped_column(Integer)  # 0=Mon..6=Sun
    nth: Mapped[int | None] = mapped_column(Integer)
    day_of_month: Mapped[int | None] = mapped_column(Integer)
    start_time: Mapped[time | None] = mapped_column(Time)
    end_time: Mapped[time | None] = mapped_column(Time)
    title_template: Mapped[str] = mapped_column(String(200), nullable=False)
    centre_ids: Mapped[list[int] | None] = mapped_column(JSONType)  # None = all active centres
    defaults: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class PlanningCycle(Base):
    """Per-month override of the planning deadline."""
    __tablename__ = "planning_cycles"
    __table_args__ = (UniqueConstraint("year", "month", name="uq_cycle_year_month"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)
    deadline: Mapped[date] = mapped_column(Date, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)


class MonthlyPlan(TimestampMixin, Base):
    __tablename__ = "monthly_plans"
    __table_args__ = (
        UniqueConstraint("centre_id", "year", "month", name="uq_plan_centre_month"),
        CheckConstraint(_in("status", PlanStatus), name="ck_plan_status"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    centre_id: Mapped[int] = mapped_column(ForeignKey("centres.id", ondelete="RESTRICT"), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=PlanStatus.DRAFT, nullable=False)
    submission_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    review_comment: Mapped[str | None] = mapped_column(Text)
    centre_note: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    centre: Mapped[Centre] = relationship(lazy="joined")
    __mapper_args__ = {"version_id_col": version}


class Event(TimestampMixin, Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint(_in("status", Status), name="ck_events_status"),
        CheckConstraint("end_time IS NULL OR start_time IS NULL OR end_time > start_time", name="ck_events_times"),
        CheckConstraint("centre_id IS NOT NULL OR is_national_parent", name="ck_events_centre_required"),
        CheckConstraint("expected_participation IS NULL OR expected_participation >= 0", name="ck_events_expected"),
        CheckConstraint("actual_participation IS NULL OR actual_participation >= 0", name="ck_events_actual"),
        Index("ix_events_centre_date", "centre_id", "event_date"),
        Index("ix_events_date", "event_date"),
        Index("ix_events_status", "status"),
        Index("ix_events_category", "category_id"),
        Index("ix_events_parent", "parent_event_id"),
        Index("ix_events_linked", "linked_event_id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("event_categories.id", ondelete="RESTRICT"), nullable=False)
    centre_id: Mapped[int | None] = mapped_column(ForeignKey("centres.id", ondelete="RESTRICT"))
    is_national_parent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    parent_event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id", ondelete="RESTRICT"))
    linked_event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id", ondelete="SET NULL"))
    recurrence_rule_id: Mapped[int | None] = mapped_column(ForeignKey("recurrence_rules.id", ondelete="SET NULL"))
    is_proposed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    subtype: Mapped[str | None] = mapped_column(String(100))
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    start_time: Mapped[time | None] = mapped_column(Time)
    end_time: Mapped[time | None] = mapped_column(Time)
    description: Mapped[str | None] = mapped_column(Text)
    objective: Mapped[str | None] = mapped_column(Text)
    responsible_person: Mapped[str | None] = mapped_column(String(120))
    expected_participation: Mapped[int | None] = mapped_column(Integer)
    target_audience: Mapped[str | None] = mapped_column(String(300))
    venue: Mapped[str | None] = mapped_column(String(200))
    preparation_deadline: Mapped[date | None] = mapped_column(Date)
    remarks: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)

    status: Mapped[str] = mapped_column(String(20), default=Status.DRAFT, nullable=False)
    changed_after_approval: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    last_review_comment: Mapped[str | None] = mapped_column(Text)
    cancel_reason: Mapped[str | None] = mapped_column(Text)

    actual_participation: Mapped[int | None] = mapped_column(Integer)
    post_event_remarks: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    updated_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    category: Mapped[EventCategory] = relationship(lazy="joined")
    centre: Mapped[Centre | None] = relationship(lazy="joined")
    created_by: Mapped[User | None] = relationship(foreign_keys=[created_by_id], lazy="joined")
    updated_by: Mapped[User | None] = relationship(foreign_keys=[updated_by_id], lazy="joined")

    __mapper_args__ = {"version_id_col": version}

    @property
    def code(self) -> str:
        return f"EVT-{self.id:06d}" if self.id else "EVT-new"


class ChecklistItem(Base):
    __tablename__ = "event_checklist_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    is_done: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    done_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class EventReference(Base):
    """Links to briefs, creatives, sheets etc. (no binary storage in v1)."""
    __tablename__ = "event_references"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    added_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class ApprovalAction(Base):
    __tablename__ = "approval_actions"
    __table_args__ = (Index("ix_approval_event", "event_id"), Index("ix_approval_plan", "plan_id"))
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("monthly_plans.id", ondelete="CASCADE"))
    action: Mapped[str] = mapped_column(String(30), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(20))
    to_status: Mapped[str | None] = mapped_column(String(20))
    comment: Mapped[str | None] = mapped_column(Text)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)

    actor: Mapped[User] = relationship(lazy="joined")


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_created", "created_at"),
        Index("ix_audit_entity", "entity_type", "entity_id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False)
    entity_id: Mapped[int | None] = mapped_column(Integer)
    centre_id: Mapped[int | None] = mapped_column(ForeignKey("centres.id"))
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)

    actor: Mapped[User | None] = relationship(lazy="joined")


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONType)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)

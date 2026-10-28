"""Roles, lifecycle states and the role-aware transition table.

One lifecycle field (`events.status`) drives everything. Approval status and
execution status are *derived* from it, so the two can never disagree.
"""
from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    SUPER_ADMIN = "super_admin"
    HO_ADMIN = "ho_admin"
    CENTRE_HEAD = "centre_head"
    VIEWER = "viewer"


ROLE_LABELS = {
    Role.SUPER_ADMIN: "Super Admin",
    Role.HO_ADMIN: "Head Office Admin",
    Role.CENTRE_HEAD: "Centre Head",
    Role.VIEWER: "Read-only Viewer",
}

HO_ROLES = {Role.SUPER_ADMIN, Role.HO_ADMIN}
EDITOR_ROLES = {Role.SUPER_ADMIN, Role.HO_ADMIN, Role.CENTRE_HEAD}


class Status(StrEnum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    RETURNED = "returned"
    APPROVED = "approved"
    REJECTED = "rejected"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


STATUS_LABELS = {
    Status.DRAFT: "Draft",
    Status.SUBMITTED: "Submitted",
    Status.RETURNED: "Returned for revision",
    Status.APPROVED: "Approved",
    Status.REJECTED: "Rejected",
    Status.IN_PROGRESS: "In progress",
    Status.COMPLETED: "Completed",
    Status.CANCELLED: "Cancelled",
}

STATUS_ICONS = {
    Status.DRAFT: "✏️",
    Status.SUBMITTED: "⏳",
    Status.RETURNED: "↩️",
    Status.APPROVED: "✅",
    Status.REJECTED: "⛔",
    Status.IN_PROGRESS: "▶️",
    Status.COMPLETED: "🏁",
    Status.CANCELLED: "✖️",
}

# Events that still "exist" on the plan (count toward completeness / conflicts)
LIVE_STATUSES = {
    Status.DRAFT, Status.SUBMITTED, Status.RETURNED, Status.APPROVED,
    Status.IN_PROGRESS, Status.COMPLETED,
}
APPROVED_LIKE = {Status.APPROVED, Status.IN_PROGRESS, Status.COMPLETED}
CENTRE_EDITABLE = {Status.DRAFT, Status.RETURNED, Status.SUBMITTED, Status.APPROVED}
EXECUTION_EDITABLE = {Status.APPROVED, Status.IN_PROGRESS, Status.COMPLETED}


def approval_status(status: str) -> str:
    s = Status(status)
    if s == Status.DRAFT:
        return "Not submitted"
    if s == Status.SUBMITTED:
        return "Pending"
    if s == Status.RETURNED:
        return "Returned"
    if s == Status.REJECTED:
        return "Rejected"
    if s == Status.CANCELLED:
        return "Withdrawn"
    return "Approved"


APPROVAL_STATUS_OPTIONS = ["Not submitted", "Pending", "Returned", "Approved", "Rejected", "Withdrawn"]

APPROVAL_TO_STATUSES = {
    "Not submitted": [Status.DRAFT],
    "Pending": [Status.SUBMITTED],
    "Returned": [Status.RETURNED],
    "Approved": [Status.APPROVED, Status.IN_PROGRESS, Status.COMPLETED],
    "Rejected": [Status.REJECTED],
    "Withdrawn": [Status.CANCELLED],
}


class Action(StrEnum):
    SUBMIT = "submit"
    APPROVE = "approve"
    RETURN = "return"
    REJECT = "reject"
    START = "start"
    COMPLETE = "complete"
    CANCEL = "cancel"
    REOPEN = "reopen"


ACTION_LABELS = {
    Action.SUBMIT: "Submit for approval",
    Action.APPROVE: "Approve",
    Action.RETURN: "Return for revision",
    Action.REJECT: "Reject",
    Action.START: "Mark in progress",
    Action.COMPLETE: "Mark completed",
    Action.CANCEL: "Cancel event",
    Action.REOPEN: "Reopen as draft",
}

# action -> (allowed from-states, to-state, roles allowed, comment required)
TRANSITIONS: dict[Action, tuple[set[Status], Status, set[Role], bool]] = {
    Action.SUBMIT: ({Status.DRAFT, Status.RETURNED}, Status.SUBMITTED, EDITOR_ROLES, False),
    Action.APPROVE: ({Status.SUBMITTED}, Status.APPROVED, HO_ROLES, False),
    Action.RETURN: ({Status.SUBMITTED}, Status.RETURNED, HO_ROLES, True),
    Action.REJECT: ({Status.SUBMITTED}, Status.REJECTED, HO_ROLES, True),
    Action.START: ({Status.APPROVED}, Status.IN_PROGRESS, EDITOR_ROLES, False),
    Action.COMPLETE: ({Status.APPROVED, Status.IN_PROGRESS}, Status.COMPLETED, EDITOR_ROLES, False),
    Action.CANCEL: (
        {Status.DRAFT, Status.SUBMITTED, Status.RETURNED, Status.APPROVED, Status.IN_PROGRESS},
        Status.CANCELLED, EDITOR_ROLES, True,
    ),
    Action.REOPEN: ({Status.REJECTED, Status.CANCELLED}, Status.DRAFT, HO_ROLES, False),
}


class PlanStatus(StrEnum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    RETURNED = "returned"
    APPROVED = "approved"


PLAN_STATUS_LABELS = {
    PlanStatus.DRAFT: "Not submitted",
    PlanStatus.SUBMITTED: "Submitted – awaiting review",
    PlanStatus.RETURNED: "Returned for revision",
    PlanStatus.APPROVED: "Approved",
}


class Requirement(StrEnum):
    MANDATORY = "mandatory"
    OPTIONAL = "optional"
    NOT_APPLICABLE = "not_applicable"


# Colours accepted by Streamlit markdown (:blue[...] etc.)
CATEGORY_COLOURS = ["blue", "green", "orange", "red", "violet", "gray", "primary"]
COLOUR_HEX = {
    "blue": "#1c83e1", "green": "#21c354", "orange": "#ff8700", "red": "#ff2b2b",
    "violet": "#803df5", "gray": "#808495", "primary": "#b8860b",
}

# Common (column-backed) event fields that the forms know about.
COMMON_FIELDS = [
    "title", "subtype", "event_date", "start_time", "end_time", "description", "objective",
    "responsible_person", "expected_participation", "target_audience", "venue",
    "preparation_deadline", "remarks", "actual_participation", "post_event_remarks",
]
COMMON_LABELS = {
    "title": "Event title",
    "subtype": "Subtype",
    "event_date": "Event date",
    "start_time": "Start time",
    "end_time": "End time",
    "description": "Description",
    "objective": "Objective",
    "responsible_person": "Responsible person",
    "expected_participation": "Expected participation",
    "target_audience": "Target audience",
    "venue": "Venue",
    "preparation_deadline": "Preparation deadline",
    "remarks": "Additional remarks",
    "actual_participation": "Actual participation",
    "post_event_remarks": "Post-event remarks",
}
# Fields that, when changed on an approved event, trigger re-approval.
MATERIAL_FIELDS = {"title", "event_date", "start_time", "end_time", "venue", "category_id", "centre_id"}
EXECUTION_COMMON_FIELDS = {"actual_participation", "post_event_remarks"}

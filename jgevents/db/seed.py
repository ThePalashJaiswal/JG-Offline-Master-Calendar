"""Idempotent reference data. Runs after migrations; only inserts what is missing,
so admin edits made through the app are never overwritten."""
from __future__ import annotations

from datetime import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from jgevents.db.models import AppSetting, CategoryRequirement, Centre, EventCategory, RecurrenceRule
from jgevents.domain.constants import Requirement

CENTRES = [
    ("DEL", "Delhi"), ("LKO", "Lucknow"), ("PRY", "Prayagraj"), ("JBP", "Jabalpur"),
    ("BPL", "Bhopal"), ("IDR", "Indore"), ("JAI", "Jaipur"),
]

PROGRAMMES = ["Civil Judge / Judicial Services", "ADPO / APO / Prosecution", "Foundation", "Crash course",
              "Test series", "Other"]

CATEGORIES: list[dict] = [
    {
        "key": "batch_launch", "name": "Batch Launch", "icon": "🎓", "colour": "blue", "group": "academic",
        "sort_order": 10, "occupies_slot": True, "conflict_policy": "block",
        "description": "Launch of a new batch. Two launches at a centre may share a date but not a time slot.",
        "common_overrides": {
            "title": {"label": "Launch title"},
            "event_date": {"label": "Launch date"},
            "end_time": {"required": True},
            "expected_participation": {"label": "Expected attendance", "required": True},
            "target_audience": {"label": "Target student group", "required": True},
            "preparation_deadline": {"label": "Preparation deadline"},
        },
        "field_schema": [
            {"key": "batch_name", "label": "Batch name / identifier", "type": "text", "required": True},
            {"key": "subject", "label": "Subject", "type": "text", "required": True},
            {"key": "programme", "label": "Batch type / programme", "type": "select", "required": True,
             "options": PROGRAMMES},
            {"key": "faculty", "label": "Faculty / speaker", "type": "text"},
            {"key": "preparation_requirements", "label": "Preparation requirements", "type": "textarea"},
            {"key": "promo_deadline", "label": "Promotional activity deadline", "type": "date",
             "before_event": True},
        ],
        "default_checklist": ["Classroom & AV ready", "Faculty confirmed", "Study material ready",
                              "Promotion completed", "WhatsApp campaign scheduled", "Registration desk set up"],
    },
    {
        "key": "whatsapp_campaign", "name": "Bulk WhatsApp Campaign", "icon": "💬", "colour": "green",
        "group": "marketing", "sort_order": 20, "occupies_slot": False, "conflict_policy": "none",
        "link_target_key": "batch_launch",
        "description": "Planned bulk WhatsApp communication. Tracked manually – the app does not send messages.",
        "common_overrides": {
            "title": {"label": "Campaign / communication title"},
            "event_date": {"label": "Planned dispatch date"},
            "start_time": {"label": "Planned dispatch time"},
            "end_time": {"hidden": True},
            "target_audience": {"label": "Target audience / student segment", "required": True},
            "objective": {"label": "Communication objective", "required": True},
            "expected_participation": {"label": "Planned recipients"},
            "venue": {"hidden": True},
            "preparation_deadline": {"label": "Creative / copy approval deadline"},
        },
        "subtype_options": ["Announcement", "Reminder", "Follow-up", "Result / offer", "Other"],
        "field_schema": [
            {"key": "message_ref", "label": "Message / campaign reference", "type": "text", "required": True,
             "help": "Template name, Drive link or copy reference"},
            {"key": "actual_dispatch_at", "label": "Actual dispatch date & time", "type": "datetime",
             "phase": "execution"},
            {"key": "recipients_sent", "label": "Messages sent", "type": "int", "phase": "execution"},
            {"key": "delivered", "label": "Delivered", "type": "int", "phase": "execution"},
            {"key": "read", "label": "Read", "type": "int", "phase": "execution"},
            {"key": "responses", "label": "Responses / enquiries", "type": "int", "phase": "execution"},
        ],
        "default_checklist": ["Copy approved", "Audience list cleaned", "Dispatched", "Responses followed up"],
    },
    {
        "key": "offline_seminar", "name": "Offline Seminar", "icon": "🎤", "colour": "violet", "group": "academic",
        "sort_order": 30, "occupies_slot": True, "conflict_policy": "warn",
        "common_overrides": {
            "title": {"label": "Seminar title"}, "venue": {"required": True},
            "end_time": {"required": True},
            "expected_participation": {"label": "Expected attendance", "required": True},
            "target_audience": {"required": True},
            "actual_participation": {"label": "Actual attendance"},
            "post_event_remarks": {"label": "Post-event notes"},
        },
        "field_schema": [
            {"key": "topic", "label": "Topic", "type": "text", "required": True},
            {"key": "speaker", "label": "Speaker / faculty", "type": "text", "required": True},
            {"key": "promo_deadline", "label": "Promotion deadline", "type": "date", "before_event": True},
        ],
        "default_checklist": ["Venue booked", "Speaker confirmed", "Promotion live", "Registrations tracked",
                              "AV & seating checked", "Feedback form ready"],
    },
    {
        "key": "scholarship_test", "name": "Month-End Scholarship Test", "icon": "🏆", "colour": "orange",
        "group": "assessment", "sort_order": 40, "occupies_slot": True, "conflict_policy": "warn",
        "common_overrides": {
            "title": {"label": "Test name"}, "event_date": {"label": "Test date"},
            "end_time": {"required": True},
            "venue": {"label": "Centre / venue", "required": True},
            "expected_participation": {"label": "Expected participants", "required": True},
            "actual_participation": {"label": "Actual participation"},
        },
        "field_schema": [
            {"key": "syllabus", "label": "Subjects / syllabus", "type": "textarea", "required": True},
            {"key": "eligibility", "label": "Eligibility criteria", "type": "textarea", "required": True},
            {"key": "registration_deadline", "label": "Registration deadline", "type": "date", "required": True,
             "before_event": True},
            {"key": "promo_start", "label": "Promotion start date", "type": "date", "before_event": True},
            {"key": "registrations", "label": "Registrations received", "type": "int", "phase": "execution"},
            {"key": "scholarships_awarded", "label": "Scholarships awarded", "type": "int", "phase": "execution"},
        ],
        "default_checklist": ["Paper set & reviewed", "Registration form live", "Promotion started",
                              "Invigilators assigned", "Results published"],
    },
    {
        "key": "pmi", "name": "PMI – Pre-Mains Interview Test", "icon": "⚖️", "colour": "red",
        "group": "assessment", "sort_order": 50, "occupies_slot": True, "conflict_policy": "warn",
        "common_overrides": {
            "title": {"label": "PMI title"}, "venue": {"required": True}, "end_time": {"required": True},
            "target_audience": {"label": "Target examination / student group", "required": True},
            "expected_participation": {"label": "Expected participants", "required": True},
        },
        "field_schema": [
            {"key": "target_exam", "label": "Target examination", "type": "text", "required": True},
            {"key": "format", "label": "Format", "type": "select", "required": True,
             "options": ["Mock interview panel", "Written + interview", "Group discussion", "Other"]},
            {"key": "evaluators", "label": "Faculty / evaluators", "type": "text", "required": True},
        ],
        "default_checklist": ["Panel confirmed", "Student slots shared", "Evaluation sheets ready",
                              "Feedback shared with students"],
    },
    {
        "key": "informal_engagement", "name": "Informal Centre Engagement", "icon": "☕", "colour": "gray",
        "group": "engagement", "is_informal": True, "sort_order": 60, "occupies_slot": True,
        "conflict_policy": "warn",
        "description": "Candid interactions between students and the Centre Head. Custom titles welcome.",
        "subtype_options": ["Chai Chai Par Charcha", "Candid Conversations with the Centre Head",
                            "Student Open House", "Informal Mentorship Meet", "Student Feedback Circle", "Other"],
        "common_overrides": {
            "objective": {"required": True}, "venue": {"required": False},
            "subtype": {"label": "Format"},
        },
        "field_schema": [
            {"key": "discussion_themes", "label": "Key discussion themes", "type": "textarea"},
            {"key": "feedback_outcomes", "label": "Feedback / outcomes", "type": "textarea", "phase": "execution"},
        ],
        "default_checklist": [],
    },
    {
        "key": "open_mock", "name": "All India Open Mock", "icon": "📝", "colour": "primary",
        "group": "assessment", "sort_order": 70, "occupies_slot": True, "conflict_policy": "warn",
        "supports_national": True,
        "common_overrides": {
            "title": {"label": "Mock title"}, "event_date": {"label": "Test date"}, "end_time": {"required": True},
            "venue": {"label": "Venue or mode", "required": True},
            "expected_participation": {"label": "Expected participants", "required": True},
            "actual_participation": {"label": "Actual participation"},
        },
        "field_schema": [
            {"key": "programme", "label": "Examination / programme", "type": "text", "required": True},
            {"key": "mode", "label": "Mode", "type": "select", "options": ["Offline at centre", "Online", "Hybrid"]},
            {"key": "registration_deadline", "label": "Registration deadline", "type": "date", "before_event": True},
            {"key": "participation_plan", "label": "Centre-level participation plan", "type": "textarea",
             "required": True},
            {"key": "promo_start", "label": "Promotion start date", "type": "date", "before_event": True},
            {"key": "registrations", "label": "Registrations received", "type": "int", "phase": "execution"},
        ],
        "default_checklist": ["Registrations open", "Promotion started", "Hall & invigilation arranged",
                              "Papers / login received", "Results communicated"],
    },
]

# Business-rule defaults. These are ASSUMPTIONS to be confirmed by Head Office;
# all are editable in Admin → Categories & rules.
DEFAULT_REQUIREMENTS = {"scholarship_test": (Requirement.MANDATORY, 1)}
DEFAULT_SETTINGS = {
    "planning_deadline_day": 25,          # plans due by the 25th of the previous month
    "reapproval_on_material_change": True,
    "at_risk_window_days": 7,
}


def seed_reference_data(session: Session) -> None:
    if not session.scalar(select(Centre.id).limit(1)):
        for i, (code, name) in enumerate(CENTRES):
            session.add(Centre(code=code, name=name, city=name, sort_order=(i + 1) * 10))
    existing = {c.key for c in session.scalars(select(EventCategory))}
    for spec in CATEGORIES:
        if spec["key"] in existing:
            continue
        session.add(EventCategory(
            key=spec["key"], name=spec["name"], icon=spec["icon"], colour=spec["colour"], group=spec["group"],
            description=spec.get("description"), is_informal=spec.get("is_informal", False),
            field_schema=spec["field_schema"], common_overrides=spec.get("common_overrides", {}),
            subtype_options=spec.get("subtype_options", []), default_checklist=spec.get("default_checklist", []),
            occupies_slot=spec["occupies_slot"], conflict_policy=spec["conflict_policy"],
            supports_national=spec.get("supports_national", False), link_target_key=spec.get("link_target_key"),
            sort_order=spec["sort_order"], is_system=True, is_active=True,
        ))
    session.flush()
    cats = {c.key: c for c in session.scalars(select(EventCategory))}
    if not session.scalar(select(CategoryRequirement.id).limit(1)):
        for key, cat in cats.items():
            req, n = DEFAULT_REQUIREMENTS.get(key, (Requirement.OPTIONAL, 0))
            session.add(CategoryRequirement(category_id=cat.id, centre_id=None, requirement=req, min_count=n))
    if not session.scalar(select(RecurrenceRule.id).limit(1)) and "scholarship_test" in cats:
        session.add(RecurrenceRule(
            category_id=cats["scholarship_test"].id, name="Month-end scholarship test (last Sunday)",
            rule_type="last_weekday", weekday=6, start_time=time(10, 0), end_time=time(12, 0),
            title_template="{centre} Scholarship Test – {month_name} {year}", centre_ids=None,
            defaults={"venue": "Centre classroom"}, is_active=True,
        ))
    for key, value in DEFAULT_SETTINGS.items():
        if session.get(AppSetting, key) is None:
            session.add(AppSetting(key=key, value=value))
    session.flush()

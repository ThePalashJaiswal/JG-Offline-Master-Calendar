"""Category field schemas and validation.

A category's `field_schema` is a list of field definitions:

    {"key": "batch_name", "label": "Batch name / ID", "type": "text",
     "required": true, "phase": "plan", "help": "...", "options": [...]}

types : text | textarea | int | date | time | datetime | select | multiselect | bool | url
phase : "plan" (filled when planning; required ones are enforced on submit)
        "execution" (filled after the event; never required for submission)
date fields may set "before_event": true to require date <= event date.

`common_overrides` tweaks column-backed fields per category:
    {"end_time": {"hidden": true}, "venue": {"required": true, "label": "Venue or mode"}}
"""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from jgevents.domain.constants import COMMON_LABELS
from jgevents.utils.dates import fmt_date, parse_date, parse_time

FIELD_TYPES = {"text", "textarea", "int", "date", "time", "datetime", "select", "multiselect", "bool", "url"}

# Common fields required on *submission* unless a category overrides it.
DEFAULT_REQUIRED_ON_SUBMIT = {"title", "event_date", "start_time", "responsible_person"}
ALWAYS_REQUIRED = {"title", "event_date"}  # even for drafts


def validate_field_schema(schema: Any) -> list[str]:
    """Used when admins edit a category definition."""
    errors: list[str] = []
    if not isinstance(schema, list):
        return ["Field schema must be a list of field definitions."]
    seen = set()
    for i, f in enumerate(schema):
        where = f"Field #{i + 1}"
        if not isinstance(f, dict):
            errors.append(f"{where}: must be an object.")
            continue
        key = f.get("key")
        if not key or not str(key).replace("_", "").isalnum():
            errors.append(f"{where}: 'key' must be alphanumeric/underscore.")
        elif key in seen:
            errors.append(f"{where}: duplicate key '{key}'.")
        seen.add(key)
        if not f.get("label"):
            errors.append(f"{where}: 'label' is required.")
        if f.get("type") not in FIELD_TYPES:
            errors.append(f"{where}: type must be one of {sorted(FIELD_TYPES)}.")
        if f.get("type") in {"select", "multiselect"} and not f.get("options"):
            errors.append(f"{where}: select fields need 'options'.")
        if f.get("phase", "plan") not in {"plan", "execution"}:
            errors.append(f"{where}: phase must be 'plan' or 'execution'.")
    return errors


def common_field_config(category, field: str) -> dict:
    ov = (category.common_overrides or {}).get(field, {})
    required = ov.get("required", field in DEFAULT_REQUIRED_ON_SUBMIT)
    if field in ALWAYS_REQUIRED:
        required = True
    return {
        "label": ov.get("label", COMMON_LABELS.get(field, field)),
        "required": bool(required) and not ov.get("hidden", False),
        "hidden": bool(ov.get("hidden", False)) and field not in ALWAYS_REQUIRED,
        "help": ov.get("help"),
    }


def coerce_detail(field: dict, value: Any) -> Any:
    """Normalise a raw form value into its JSON-storable form."""
    t = field.get("type", "text")
    if value in (None, "", []):
        return None
    if t == "int":
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field['label']} must be a whole number.") from exc
    if t == "date":
        d = parse_date(value)
        return d.isoformat() if d else None
    if t == "time":
        tm = parse_time(value)
        return tm.strftime("%H:%M") if tm else None
    if t == "datetime":
        if isinstance(value, datetime):
            return value.replace(second=0, microsecond=0).isoformat()
        return datetime.fromisoformat(str(value)).replace(second=0, microsecond=0).isoformat()
    if t == "bool":
        return bool(value)
    if t == "multiselect":
        return list(value)
    return str(value).strip() or None


def detail_as_python(field: dict, value: Any) -> Any:
    if value in (None, ""):
        return None
    t = field.get("type")
    try:
        if t == "date":
            return date.fromisoformat(value)
        if t == "time":
            return time.fromisoformat(value)
        if t == "datetime":
            return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return value


def display_detail(field: dict, value: Any) -> str:
    v = detail_as_python(field, value)
    if v in (None, "", []):
        return "—"
    if isinstance(v, datetime):
        return v.strftime("%d-%m-%Y %H:%M")
    if isinstance(v, date):
        return fmt_date(v)
    if isinstance(v, time):
        return v.strftime("%H:%M")
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, list):
        return ", ".join(map(str, v))
    return str(v)


def validate_event_payload(category, common: dict, details: dict, strict: bool) -> list[str]:
    """strict=True is used on submission: all plan-phase required fields must be present.
    Drafts only need a title and date, but whatever is entered must be valid."""
    errors: list[str] = []
    # --- common fields
    for field in ("title", "event_date", "start_time", "end_time", "responsible_person", "objective",
                  "target_audience", "venue", "expected_participation", "preparation_deadline", "description",
                  "subtype"):
        cfg = common_field_config(category, field)
        val = common.get(field)
        empty = val in (None, "")
        if empty and cfg["required"] and (strict or field in ALWAYS_REQUIRED):
            errors.append(f"{cfg['label']} is required.")
    title = common.get("title") or ""
    if len(title) > 200:
        errors.append("Title must be at most 200 characters.")
    st, et = common.get("start_time"), common.get("end_time")
    if st and et and et <= st:
        errors.append("End time must be after start time.")
    ev_date = common.get("event_date")
    prep = common.get("preparation_deadline")
    if prep and ev_date and prep > ev_date:
        errors.append("Preparation deadline cannot be after the event date.")
    for num in ("expected_participation", "actual_participation"):
        v = common.get(num)
        if v is not None and v < 0:
            errors.append(f"{COMMON_LABELS[num]} cannot be negative.")
    # --- category-specific fields
    for f in category.field_schema or []:
        key, label = f["key"], f["label"]
        raw = details.get(key)
        empty = raw in (None, "", [])
        if empty:
            if strict and f.get("required") and f.get("phase", "plan") == "plan":
                errors.append(f"{label} is required.")
            continue
        try:
            val = detail_as_python(f, coerce_detail(f, raw))
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if f.get("type") == "select" and raw not in f.get("options", []):
            errors.append(f"{label}: '{raw}' is not an allowed option.")
        if f.get("type") == "int" and val is not None and val < 0:
            errors.append(f"{label} cannot be negative.")
        if f.get("type") == "url" and not str(raw).startswith(("http://", "https://")):
            errors.append(f"{label} must start with http:// or https://")
        if f.get("type") == "date" and f.get("before_event") and ev_date and val and val > ev_date:
            errors.append(f"{label} cannot be after the event date.")
    return errors


def clean_details(category, details: dict) -> dict:
    """Drop unknown keys and coerce values."""
    out = {}
    for f in category.field_schema or []:
        if f["key"] in details:
            out[f["key"]] = coerce_detail(f, details.get(f["key"]))
    return out

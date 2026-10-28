"""Reusable widgets: filters, event tables, event form."""
from __future__ import annotations

from datetime import date, datetime, time

import pandas as pd
import streamlit as st

from jgevents.db.models import Centre, Event, EventCategory, User
from jgevents.domain.constants import (
    APPROVAL_STATUS_OPTIONS,
    STATUS_ICONS,
    STATUS_LABELS,
    Status,
    approval_status,
)
from jgevents.domain.schema import common_field_config, detail_as_python
from jgevents.services import config_service, directory
from jgevents.services import events as ev_svc
from jgevents.services.events import EventFilter
from jgevents.utils.dates import fmt_date, fmt_time
from ui.core import db, open_event, time_range

DATE_FMT = "DD-MM-YYYY"


# ---------------------------------------------------------------- filters

def event_filters(prefix: str, user: User, show_status: bool = True, expanded: bool = False,
                  centres: list[Centre] | None = None) -> EventFilter:
    with db() as s:
        centres = centres if centres is not None else directory.list_centres(s, user, include_inactive=True)
        cats = config_service.list_categories(s, include_inactive=True)
    with st.expander("Filters", expanded=expanded, icon=":material/filter_list:"):
        c1, c2, c3 = st.columns(3)
        centre_ids = c1.multiselect("Centre", [c.id for c in centres], key=f"{prefix}_centres",
                                    format_func=lambda i: next(c.name + ("" if c.is_active else " (inactive)")
                                                               for c in centres if c.id == i),
                                    placeholder="All centres")
        cat_ids = c2.multiselect("Category", [c.id for c in cats], key=f"{prefix}_cats",
                                 format_func=lambda i: next(f"{c.icon} {c.name}" for c in cats if c.id == i),
                                 placeholder="All categories")
        responsible = c3.text_input("Responsible person contains", key=f"{prefix}_resp")
        statuses, approvals = [], []
        if show_status:
            c4, c5 = st.columns(2)
            statuses = c4.multiselect("Event status", [s.value for s in Status], key=f"{prefix}_status",
                                      format_func=lambda v: f"{STATUS_ICONS[Status(v)]} {STATUS_LABELS[Status(v)]}",
                                      placeholder="Any status")
            approvals = c5.multiselect("Approval status", APPROVAL_STATUS_OPTIONS, key=f"{prefix}_appr",
                                       placeholder="Any approval status")
    return EventFilter(centre_ids=centre_ids or None, category_ids=cat_ids or None, statuses=statuses or None,
                       approval_statuses=approvals or None, responsible=responsible or None)


# ---------------------------------------------------------------- tables

def events_frame(events: list[Event]) -> pd.DataFrame:
    rows = []
    for e in events:
        flags = ev_svc.risk_flags(e)
        rows.append({
            "id": e.id, "Event": e.code, "Date": e.event_date, "Time": time_range(e),
            "Centre": e.centre.name if e.centre else "National",
            "Category": f"{e.category.icon} {e.category.name}", "Title": e.title,
            "Status": f"{STATUS_ICONS[Status(e.status)]} {STATUS_LABELS[Status(e.status)]}",
            "Approval": approval_status(e.status), "Responsible": e.responsible_person or "",
            "Expected": e.expected_participation, "Actual": e.actual_participation,
            "Flags": " · ".join(flags) if flags else "",
        })
    return pd.DataFrame(rows)


def events_table(events: list[Event], key: str, height: int | str = "auto", empty: str = "No events match.") -> None:
    """Table where clicking a row opens the event."""
    if not events:
        st.info(empty, icon=":material/event_busy:")
        return
    df = events_frame(events)
    sel = st.dataframe(
        df, key=key, hide_index=True, on_select="rerun", selection_mode="single-row", height=height,
        column_order=["Event", "Date", "Time", "Centre", "Category", "Title", "Status", "Approval", "Responsible",
                      "Expected", "Actual", "Flags"],
        column_config={
            "Date": st.column_config.DateColumn(format="DD-MM-YYYY"),
            "Title": st.column_config.TextColumn(width="large"),
            "Expected": st.column_config.NumberColumn(width="small"),
            "Actual": st.column_config.NumberColumn(width="small"),
        },
    )
    rows = sel.selection.rows if sel and sel.selection else []
    if rows:
        open_event(int(df.iloc[rows[0]]["id"]))
    st.caption("Select a row to open the event.")


# ---------------------------------------------------------------- event form

def _detail_widget(f: dict, value, key: str, required_mark: bool):
    label = f["label"] + (" *" if required_mark else "")
    t = f.get("type", "text")
    help_ = f.get("help")
    v = detail_as_python(f, value)
    if t == "textarea":
        return st.text_area(label, value=v or "", key=key, help=help_, height=80)
    if t == "int":
        return st.number_input(label, min_value=0, value=v, step=1, key=key, help=help_)
    if t == "date":
        return st.date_input(label, value=v, format=DATE_FMT, key=key, help=help_)
    if t == "time":
        return st.time_input(label, value=v, key=key, help=help_)
    if t == "datetime":
        c1, c2 = st.columns(2)
        d = c1.date_input(label, value=v.date() if v else None, format=DATE_FMT, key=key + "_d", help=help_)
        tm = c2.time_input(" ", value=v.time() if v else None, key=key + "_t")
        return datetime.combine(d, tm) if d and tm else None
    if t == "select":
        opts = f.get("options", [])
        return st.selectbox(label, opts, index=opts.index(v) if v in opts else None, key=key, help=help_,
                            placeholder="Choose…")
    if t == "multiselect":
        return st.multiselect(label, f.get("options", []), default=[x for x in (v or []) if x in f.get("options", [])],
                              key=key, help=help_)
    if t == "bool":
        return st.checkbox(label, value=bool(v), key=key, help=help_)
    return st.text_input(label, value=v or "", key=key, help=help_)


def event_form(key: str, user: User, category: EventCategory, centre_id: int | None, *, event: Event | None = None,
               default_date: date | None = None, mode: str = "create") -> dict | None:
    """Renders a category-specific form. Returns a dict with the submitted values and the
    button pressed ('draft' | 'submit' | 'save'), or None if nothing was submitted."""
    e = event
    cfg = {k: common_field_config(category, k) for k in (
        "title", "subtype", "event_date", "start_time", "end_time", "description", "objective",
        "responsible_person", "expected_participation", "target_audience", "venue", "preparation_deadline")}

    def lab(k):
        return cfg[k]["label"] + (" *" if cfg[k]["required"] else "")

    linkables = []
    if category.link_target_key and centre_id:
        with db() as s:
            linkables = ev_svc.linkable_targets(s, user, category, centre_id)
            linkables = [(x.id, f"{x.code} · {fmt_date(x.event_date)} · {x.title}") for x in linkables]

    with st.form(key, border=True, enter_to_submit=False):
        if category.is_informal:
            st.caption("☕ Informal engagement – a candid interaction, not a formal academic event.")
        if category.description:
            st.caption(category.description)
        title = st.text_input(lab("title"), value=e.title if e else "", max_chars=200, key=f"{key}_title")
        subtype = None
        if category.subtype_options:
            opts = category.subtype_options
            cur = e.subtype if e else None
            subtype = st.selectbox(cfg["subtype"]["label"], opts, index=opts.index(cur) if cur in opts else None,
                                   placeholder="Choose…", key=f"{key}_subtype")
        c1, c2, c3 = st.columns(3)
        ev_date = c1.date_input(lab("event_date"), value=e.event_date if e else default_date, format=DATE_FMT,
                                key=f"{key}_date")
        start = c2.time_input(lab("start_time"), value=e.start_time if e else None, key=f"{key}_start") if not cfg["start_time"]["hidden"] else None
        end = c3.time_input(lab("end_time"), value=e.end_time if e else None, key=f"{key}_end") if not cfg["end_time"]["hidden"] else None
        c1, c2 = st.columns(2)
        responsible = c1.text_input(lab("responsible_person"), value=(e.responsible_person if e else None)
                                    or (user.full_name if user.role == "centre_head" else ""), key=f"{key}_resp")
        expected = None
        if not cfg["expected_participation"]["hidden"]:
            expected = c2.number_input(lab("expected_participation"), min_value=0, step=1,
                                       value=e.expected_participation if e else None, key=f"{key}_exp")
        c1, c2 = st.columns(2)
        audience = c1.text_input(lab("target_audience"), value=(e.target_audience if e else "") or "", key=f"{key}_aud") \
            if not cfg["target_audience"]["hidden"] else None
        venue = c2.text_input(lab("venue"), value=(e.venue if e else "") or "", key=f"{key}_venue") if not cfg["venue"]["hidden"] else None
        objective = st.text_area(lab("objective"), value=(e.objective if e else "") or "", height=70, key=f"{key}_obj")
        description = st.text_area(lab("description"), value=(e.description if e else "") or "", height=70,
                                   key=f"{key}_desc")
        prep = st.date_input(lab("preparation_deadline"), value=e.preparation_deadline if e else None,
                             format=DATE_FMT, key=f"{key}_prep") if not cfg["preparation_deadline"]["hidden"] else None

        plan_fields = [f for f in category.field_schema or [] if f.get("phase", "plan") == "plan"]
        details = {}
        if plan_fields:
            st.markdown(f"**{category.icon} {category.name} details**")
            cols = st.columns(2)
            for i, f in enumerate(plan_fields):
                with cols[i % 2] if f.get("type") not in ("textarea",) else st.container():
                    details[f["key"]] = _detail_widget(f, (e.details or {}).get(f["key"]) if e else None,
                                                       f"{key}_d_{f['key']}", bool(f.get("required")))
        linked = None
        if category.link_target_key:
            ids = [None] + [i for i, _ in linkables]
            labels = dict(linkables)
            cur = e.linked_event_id if e else st.session_state.get(f"{key}_prelink")
            linked = st.selectbox("Linked batch launch", ids, index=ids.index(cur) if cur in ids else 0,
                                  format_func=lambda i: "— not linked —" if i is None else labels.get(i, f"EVT-{i:06d}"),
                                  key=f"{key}_link")
        remarks = st.text_area("Additional remarks", value=(e.remarks if e else "") or "", height=60, key=f"{key}_rem")
        st.caption("Fields marked * are required to submit. Drafts can be saved incomplete.")
        if mode == "create":
            b1, b2, _ = st.columns([1, 1, 2])
            draft = b1.form_submit_button("Save draft", width="stretch")
            submit = b2.form_submit_button("Save & submit", type="primary", width="stretch")
            action = "submit" if submit else "draft" if draft else None
        else:
            action = "save" if st.form_submit_button("Save changes", type="primary") else None
    if not action:
        return None
    common = dict(title=title, subtype=subtype, event_date=ev_date, start_time=start, end_time=end,
                  responsible_person=responsible, expected_participation=expected, target_audience=audience,
                  venue=venue, objective=objective, description=description, preparation_deadline=prep,
                  remarks=remarks)
    if e is not None:  # hidden fields keep their stored values
        for k in ("start_time", "end_time", "target_audience", "venue", "preparation_deadline",
                  "expected_participation"):
            if cfg[k]["hidden"]:
                common[k] = getattr(e, k)
        if not category.subtype_options:
            common["subtype"] = e.subtype
    return {"action": action, "common": common, "details": details, "linked": linked}


def execution_form(key: str, ev: Event) -> dict | None:
    exec_fields = [f for f in ev.category.field_schema or [] if f.get("phase") == "execution"]
    cfg_actual = common_field_config(ev.category, "actual_participation")
    cfg_post = common_field_config(ev.category, "post_event_remarks")
    with st.form(key, border=True, enter_to_submit=False):
        actual = st.number_input(cfg_actual["label"], min_value=0, step=1, value=ev.actual_participation)
        details = {}
        cols = st.columns(2)
        for i, f in enumerate(exec_fields):
            with cols[i % 2] if f.get("type") != "textarea" else st.container():
                details[f["key"]] = _detail_widget(f, (ev.details or {}).get(f["key"]), f"{key}_x_{f['key']}", False)
        post = st.text_area(cfg_post["label"], value=ev.post_event_remarks or "", height=90)
        if st.form_submit_button("Save execution details", type="primary"):
            return {"actual": actual, "post": post, "details": details}
    return None


def pick_category(key: str, categories: list[EventCategory], default_key: str | None = None) -> EventCategory | None:
    if not categories:
        st.warning("No active event categories. Ask Head Office to configure them.")
        return None
    keys = [c.key for c in categories]
    idx = keys.index(default_key) if default_key in keys else 0
    chosen = st.selectbox("Event category", keys, index=idx, key=key,
                          format_func=lambda k: next(f"{c.icon} {c.name}" + (" · informal" if c.is_informal else "")
                                                     for c in categories if c.key == k))
    return next(c for c in categories if c.key == chosen)


def fmt_t(t: time | None) -> str:
    return fmt_time(t)

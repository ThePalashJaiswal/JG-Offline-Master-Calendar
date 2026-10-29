from __future__ import annotations

from datetime import timedelta

import pandas as pd
import streamlit as st

from jgevents.domain.constants import APPROVED_LIKE, LIVE_STATUSES, Status
from jgevents.services import dashboard, directory, planning
from jgevents.services import events as ev_svc
from jgevents.services import tasks as task_svc
from jgevents.services.events import EventFilter
from jgevents.services.permissions import can_manage_centre
from jgevents.utils.dates import add_months, fmt_date, month_bounds, month_label, today_ist
from ui.components.tasks_ui import overdue_banner, task_table
from ui.components.widgets import event_filters, events_table
from ui.core import db, goto, me, month_picker, page_header

# ---------------------------------------------------------------- Centre Head

def centre_home() -> None:
    user = me()
    with db() as s:
        centres = directory.list_centres(s, user)
    if not centres:
        page_header("My centre")
        st.warning("You are not assigned to any active centre. Contact Head Office.")
        return
    if len(centres) > 1:
        cid = st.selectbox("Centre", [c.id for c in centres], key="home_centre",
                           format_func=lambda i: next(c.name for c in centres if c.id == i))
    else:
        cid = centres[0].id
    centre = next(c for c in centres if c.id == cid)
    page_header(f"{centre.name} centre", f"Signed in as {user.full_name}")
    overdue_banner(user, [cid])

    today = today_ist()
    ny, nm = add_months(today.year, today.month, 1)
    with db() as s:
        comp = planning.completeness(s, user, cid, ny, nm)
        cur_s, cur_e = month_bounds(today.year, today.month)
        nxt_s, nxt_e = month_bounds(ny, nm)
        evs = ev_svc.list_events(s, user, EventFilter(centre_ids=[cid], date_from=today - timedelta(days=120),
                                                      date_to=nxt_e + timedelta(days=60)))

    with st.container(border=True):
        c1, c2, c3, c4 = st.columns([2.2, 1.3, 1.3, 1.6], vertical_alignment="center")
        c1.markdown(f"**{month_label(ny, nm)} plan** · {comp.plan_status_label}")
        dl = comp.days_to_deadline
        c1.caption(f"Deadline {fmt_date(comp.deadline)} · "
                   + ("overdue" if dl < 0 and not comp.submitted else f"{dl} day(s) left" if dl >= 0 else "closed"))
        c2.metric("Planned", comp.total)
        c3.metric("Completeness", f"{comp.score}%")
        if c4.button("Open planning workspace", type="primary", width="stretch"):
            st.session_state["plan_centre"] = cid
            st.session_state["plan_y"], st.session_state["plan_m"] = ny, nm
            goto("planning")
        if comp.missing_mandatory:
            st.warning("Missing mandatory activities: " + ", ".join(
                f"{ln.category.icon} {ln.category.name} ({ln.planned}/{ln.min_count})" for ln in comp.missing_mandatory))
        if comp.incomplete:
            st.info(f"{len(comp.incomplete)} event(s) need more details before they can be submitted.")

    buckets = {
        "Upcoming (14 days)": [e for e in evs if today <= e.event_date <= today + timedelta(days=14)
                               and Status(e.status) in LIVE_STATUSES],
        "This month": [e for e in evs if cur_s <= e.event_date <= cur_e],
        "Next month": [e for e in evs if nxt_s <= e.event_date <= nxt_e],
        "Awaiting submission": [e for e in evs if e.status == Status.DRAFT],
        "Awaiting HO approval": [e for e in evs if e.status == Status.SUBMITTED],
        "Returned for revision": [e for e in evs if e.status == Status.RETURNED],
        "Approved": [e for e in evs if e.status in APPROVED_LIKE and e.status != Status.COMPLETED
                     and e.event_date >= today],
        "Needs outcome": [e for e in evs if e.status in (Status.APPROVED, Status.IN_PROGRESS) and e.event_date < today],
        "Completed & cancelled": [e for e in evs if e.status in (Status.COMPLETED, Status.CANCELLED, Status.REJECTED)],
    }
    with db() as s:
        my_tasks = task_svc.list_tasks(s, user, task_svc.TaskFilter(
            centre_ids=[cid], date_to=today + timedelta(days=21), include_national=False))
        my_tasks = [t for t in my_tasks if t.status == "open" and t.event.status not in ("cancelled", "rejected")]
        for t in my_tasks:
            _ = t.event, t.centre
    my_tasks.sort(key=lambda t: (not task_svc.is_overdue(t), t.due_date))
    tabs = st.tabs([f"Preparation tasks ({len(my_tasks)})"] + [f"{k} ({len(v)})" for k, v in buckets.items()])
    with tabs[0]:
        task_table(my_tasks, user, key="home_tasks", hide=("Centre",),
                   empty="No open preparation tasks due in the next 3 weeks.")
    for tab, (name, items) in zip(tabs[1:], buckets.items(), strict=True):
        with tab:
            events_table(items, key=f"home_{name}", empty=f"Nothing in “{name}”.")


# ---------------------------------------------------------------- Head Office

def ho_home() -> None:
    user = me()
    page_header("Head Office dashboard", "Network-wide planning and execution across all centres")
    y, m = month_picker("dash")
    f = event_filters("dash", user)
    with db() as s:
        data = dashboard.build(s, user, y, m, f)

    st.markdown(f"#### {month_label(y, m)}")
    order = ["centres", "planned", "pending", "approved", "returned", "completed", "cancelled", "draft",
             "at_risk", "not_submitted", "incomplete", "upcoming"]
    cols = st.columns(6)
    for i, key in enumerate(order):
        metric = data.metrics[key]
        with cols[i % 6]:
            active = st.session_state.get("dash_drill") == key
            if st.button(f"**{metric.value}**  \n{metric.label}", key=f"dash_m_{key}", width="stretch",
                         type="primary" if active else "secondary", help=metric.help or "Show records"):
                st.session_state["dash_drill"] = None if active else key
                st.rerun()
    rate = data.completion_rate
    st.caption("Execution completion rate (completed ÷ approved events whose date has passed): "
               + (f"**{rate:.0%}**" if rate is not None else "n/a – no approved events have reached their date yet"))

    drill = st.session_state.get("dash_drill")
    if drill:
        metric = data.metrics[drill]
        with st.container(border=True):
            st.markdown(f"**{metric.label}** – underlying records")
            if metric.centre_ids and not metric.ids:
                rows = [o for o in data.overview if o.centre.id in metric.centre_ids]
                _overview_table(rows, key=f"drill_{drill}")
            else:
                ids = set(metric.ids)
                pool = data.upcoming if drill == "upcoming" else data.events
                events_table([e for e in pool if e.id in ids], key=f"drill_tbl_{drill}")

    left, right = st.columns(2)
    with left:
        st.markdown("**Events by category**")
        if data.by_category:
            st.bar_chart(pd.DataFrame({"Events": data.by_category}).sort_values("Events"), horizontal=True,
                         height=260)
        else:
            st.caption("No events.")
    with right:
        st.markdown("**Events by centre**")
        if data.by_centre:
            st.bar_chart(pd.DataFrame({"Events": data.by_centre}).sort_values("Events"), horizontal=True, height=260)
        else:
            st.caption("No events.")

    st.markdown("**Planning status by centre** · select a row to see that centre's events")
    _overview_table(data.overview, key="dash_overview", drill_events=data.events)

    st.markdown("**Next 7 days**")
    events_table(data.upcoming, key="dash_upcoming", empty="Nothing scheduled in the next 7 days.")


def _overview_table(rows, key: str, drill_events=None) -> None:
    if not rows:
        st.info("No centres.")
        return
    df = pd.DataFrame([{
        "Centre": o.centre.name, "Plan": o.plan_status_label, "Submissions": o.plan.submission_count if o.plan else 0,
        "Deadline": fmt_date(o.deadline), "Events": o.total, "Unsubmitted": o.unsubmitted,
        "Incomplete": len(o.incomplete),
        "Missing mandatory": ", ".join(f"{ln.category.name} {ln.planned}/{ln.min_count}" for ln in o.missing_mandatory),
        "Completeness %": o.score, "Overdue": "⚠️" if o.overdue else "", "_id": o.centre.id,
    } for o in rows])
    sel = st.dataframe(df, key=key, hide_index=True, on_select="rerun", selection_mode="single-row",
                       column_order=[c for c in df.columns if c != "_id"],
                       column_config={"Completeness %": st.column_config.ProgressColumn(min_value=0, max_value=100,
                                                                                        format="%d%%")})
    picked = sel.selection.rows if sel and sel.selection else []
    if picked and drill_events is not None:
        cid = int(df.iloc[picked[0]]["_id"])
        name = df.iloc[picked[0]]["Centre"]
        st.markdown(f"**{name}** events this month")
        events_table([e for e in drill_events if e.centre_id == cid], key=f"{key}_centre_{cid}")
        if can_manage_centre(me(), cid) and st.button(f"Open {name} planning workspace", key=f"{key}_plan_{cid}"):
            st.session_state["plan_centre"] = cid
            goto("planning")

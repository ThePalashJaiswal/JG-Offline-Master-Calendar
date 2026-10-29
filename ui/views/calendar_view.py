from __future__ import annotations

from datetime import time

import streamlit as st

from jgevents.domain.constants import Status
from jgevents.services import config_service
from jgevents.services import events as ev_svc
from jgevents.services import tasks as task_svc
from jgevents.services.events import EventFilter
from jgevents.services.permissions import is_ho
from jgevents.utils.dates import month_bounds, month_label, today_ist
from ui.components.calendar import agenda_view, conflict_ids, legend, list_view, month_view
from ui.components.widgets import event_filters
from ui.core import db, me, month_picker, page_header


def render() -> None:
    user = me()
    page_header("Master calendar", "All centres in one view" if is_ho(user) or user.scope_all_centres
                else "Your centre's events")
    y, m = month_picker("cal")
    f = event_filters("cal", user)
    c1, c2, c3 = st.columns([1.4, 2, 1.6], vertical_alignment="center")
    show = c1.segmented_control("Show", ["Events", "Tasks", "Both"], default="Both", key="cal_show",
                                label_visibility="collapsed") or "Both"
    task_states = c2.pills("Tasks", ["Overdue", "Open", "Done", "Critical only"], selection_mode="multi",
                           key="cal_task_states", label_visibility="collapsed",
                           help="Narrow the preparation tasks shown") if show != "Events" else []
    show_cancelled = c3.toggle("Show cancelled / rejected", value=False, key="cal_show_cancelled")
    start, end = month_bounds(y, m)
    flt = EventFilter(date_from=start, date_to=end, centre_ids=f.centre_ids, category_ids=f.category_ids,
                      statuses=f.statuses, approval_statuses=f.approval_statuses, responsible=f.responsible)
    with db() as s:
        events = ev_svc.list_events(s, user, flt)
        cats = config_service.list_categories(s)
        nat = [n for n in ev_svc.national_events(s, user) if start <= n.event_date <= end
               and not ev_svc.visible_children(s, user, n.id)]
        tasks = []
        if show != "Events":
            tasks = task_svc.list_tasks(s, user, task_svc.TaskFilter(
                date_from=start, date_to=end, centre_ids=f.centre_ids, category_ids=f.category_ids,
                critical_only="Critical only" in (task_states or [])))
            tasks = [t for t in tasks if t.status != "closed" and t.event.status not in ("cancelled", "rejected")]
            wanted = {"Overdue": {"overdue"}, "Open": {"open", "blocked", "overdue"},
                      "Done": {"done", "done_late", "waived"}}
            picked = set().union(*[wanted[x] for x in (task_states or []) if x in wanted]) if any(
                x in wanted for x in (task_states or [])) else None
            if picked is not None:
                tasks = [t for t in tasks if task_svc.state_of(t) in picked]
            for t in tasks:
                _ = t.event, t.centre
    if show == "Tasks":
        events = []
    if not show_cancelled:
        events = [e for e in events if e.status not in (Status.CANCELLED, Status.REJECTED)]
    events += [n for n in nat if not f.category_ids or n.category_id in f.category_ids]
    events.sort(key=lambda e: (e.event_date, e.start_time or time(0), e.id))

    clashes = conflict_ids(events)
    overdue = sum(1 for t in tasks if task_svc.is_overdue(t))
    st.markdown(f"#### {month_label(y, m)} · {len(events)} event(s)"
                + (f" · {len(tasks)} task(s)" if show != "Events" else "")
                + (f" · :red[{overdue} overdue]" if overdue else "")
                + (f" · ⚠️ {len(clashes)} in possible clashes" if clashes else ""))
    legend(cats)
    view = st.segmented_control("View", ["Month", "Day agenda", "List"], default="Month", key="cal_view",
                                label_visibility="collapsed")
    if view == "Day agenda":
        default_day = today_ist() if start <= today_ist() <= end else start
        if not (start <= st.session_state.get("cal_day", default_day) <= end):
            st.session_state["cal_day"] = default_day
        day = st.date_input("Day", key="cal_day", min_value=start, max_value=end, format="DD-MM-YYYY")
        agenda_view(events, day, user, key="cal", tasks=tasks)
    elif view == "List":
        list_view(events, user, key="cal", tasks=tasks)
    else:
        st.caption("Tip: on a phone, the List or Day agenda views are easier to read. "
                   "Tasks: ☐ open · 🔴 overdue · ☑️ done.")
        month_view(events, y, m, user, key="cal", tasks=tasks)

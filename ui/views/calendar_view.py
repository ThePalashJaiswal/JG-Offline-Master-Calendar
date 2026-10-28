from __future__ import annotations

from datetime import time

import streamlit as st

from jgevents.domain.constants import Status
from jgevents.services import config_service
from jgevents.services import events as ev_svc
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
    show_cancelled = st.toggle("Show cancelled / rejected", value=False, key="cal_show_cancelled")
    start, end = month_bounds(y, m)
    flt = EventFilter(date_from=start, date_to=end, centre_ids=f.centre_ids, category_ids=f.category_ids,
                      statuses=f.statuses, approval_statuses=f.approval_statuses, responsible=f.responsible)
    with db() as s:
        events = ev_svc.list_events(s, user, flt)
        cats = config_service.list_categories(s)
        nat = [n for n in ev_svc.national_events(s, user) if start <= n.event_date <= end
               and not ev_svc.visible_children(s, user, n.id)]
    if not show_cancelled:
        events = [e for e in events if e.status not in (Status.CANCELLED, Status.REJECTED)]
    events += [n for n in nat if not f.category_ids or n.category_id in f.category_ids]
    events.sort(key=lambda e: (e.event_date, e.start_time or time(0), e.id))

    clashes = conflict_ids(events)
    st.markdown(f"#### {month_label(y, m)} · {len(events)} event(s)"
                + (f" · ⚠️ {len(clashes)} in possible clashes" if clashes else ""))
    legend(cats)
    view = st.segmented_control("View", ["Month", "Day agenda", "List"], default="Month", key="cal_view",
                                label_visibility="collapsed")
    if view == "Day agenda":
        default_day = today_ist() if start <= today_ist() <= end else start
        if not (start <= st.session_state.get("cal_day", default_day) <= end):
            st.session_state["cal_day"] = default_day
        day = st.date_input("Day", key="cal_day", min_value=start, max_value=end, format="DD-MM-YYYY")
        agenda_view(events, day, user, key="cal")
    elif view == "List":
        list_view(events, user, key="cal")
    else:
        st.caption("Tip: on a phone, the List or Day agenda views are easier to read.")
        month_view(events, y, m, user, key="cal")

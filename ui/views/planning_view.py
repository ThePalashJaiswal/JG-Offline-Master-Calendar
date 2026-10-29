from __future__ import annotations

import pandas as pd
import streamlit as st

from jgevents.domain.constants import PlanStatus, Status
from jgevents.services import config_service, directory, planning, recurrence
from jgevents.services import events as ev_svc
from jgevents.services.permissions import can_manage_centre, is_ho
from jgevents.utils.dates import add_months, fmt_date, fmt_dt, month_bounds, month_label, today_ist
from ui.components.calendar import month_view
from ui.components.tasks_ui import creation_gate
from ui.components.widgets import event_form, events_table, pick_category
from ui.core import db, flash, me, month_picker, page_header, run


def render() -> None:
    user = me()
    page_header("Monthly planning", "Plan, complete and submit your centre's events for the month")
    with db() as s:
        centres = directory.list_centres(s, user)
    if not centres:
        st.warning("No centres available to you.")
        return
    ids = [c.id for c in centres]
    if st.session_state.get("plan_centre") not in ids:
        st.session_state["plan_centre"] = ids[0]
    c1, c2 = st.columns([1, 2.4])
    cid = c1.selectbox("Centre", ids, key="plan_centre", format_func=lambda i: next(c.name for c in centres if c.id == i))
    with c2:
        t = today_ist()
        y, m = month_picker("plan", default=add_months(t.year, t.month, 1))
    can_edit = can_manage_centre(user, cid)

    with db() as s:
        comp = planning.completeness(s, user, cid, y, m)
        events = planning.month_events(s, cid, y, m)
        history = planning.plan_history(s, user, comp.plan) if comp.plan else []
        for e in events:
            _ = e.category, e.centre

    # ---- status strip
    with st.container(border=True):
        a, b, c, d = st.columns([2.4, 1, 1, 1])
        a.markdown(f"### {comp.centre.name} · {month_label(y, m)}")
        dl = comp.days_to_deadline
        a.markdown(f"Plan: **{comp.plan_status_label}** · Deadline **{fmt_date(comp.deadline)}** "
                   + (f"({dl} day(s) left)" if dl >= 0 else ("(:red[overdue])" if not comp.submitted else "(closed)")))
        b.metric("Events", comp.total)
        c.metric("Unsubmitted", comp.unsubmitted)
        d.metric("Completeness", f"{comp.score}%")
        st.progress(comp.score / 100)
        if comp.plan and comp.plan.review_comment and comp.plan_status == PlanStatus.RETURNED:
            st.error(f"Head Office feedback: {comp.plan.review_comment}")
        if comp.plan and comp.plan.centre_note:
            st.caption(f"Centre note: {comp.plan.centre_note}")

    left, right = st.columns([1.1, 1])
    with left:
        st.markdown("**Category coverage**")
        rows = [{"Category": f"{ln.category.icon} {ln.category.name}",
                 "Requirement": {"mandatory": f"Mandatory (min {ln.min_count})", "optional": "Optional"}[ln.requirement],
                 "Planned": ln.planned,
                 "Status": ("⚠️ Missing " + str(ln.missing)) if ln.missing else ("✅" if ln.planned else "—")}
                for ln in comp.lines]
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    with right:
        st.markdown("**Entries needing attention**")
        if not comp.incomplete:
            st.success("All draft/returned entries are complete.", icon=":material/check_circle:")
        for e, errs in comp.incomplete:
            with st.container(border=True):
                st.markdown(f"`{e.code}` **{e.title}** · {fmt_date(e.event_date)}")
                st.caption("; ".join(errs))
        returned = [e for e in events if e.status == Status.RETURNED]
        for e in returned:
            st.warning(f"{e.code} {e.title} returned: {e.last_review_comment or 'see event'}")

    # ---- Head Office review
    if is_ho(user) and any(e.status == Status.SUBMITTED for e in events) and comp.plan:
        with st.container(border=True):
            st.markdown("**Head Office review of this plan**")
            comment = st.text_area("Comment to centre (required to return)", key=f"rev_c_{comp.plan.id}", height=70)
            r1, r2, _ = st.columns([1, 1, 3])
            n = sum(1 for e in events if e.status == Status.SUBMITTED)
            if r1.button(f"Approve all {n} submitted", type="primary", key=f"rev_a_{comp.plan.id}"):
                run(lambda s: planning.review_plan(s, user, comp.plan.id, "approve", comment), "Plan approved")
            if r2.button("Return plan", key=f"rev_r_{comp.plan.id}"):
                run(lambda s: planning.review_plan(s, user, comp.plan.id, "return", comment), "Plan returned to centre")

    # ---- add events
    if can_edit:
        with st.expander("➕ Add an event to this plan", expanded=not events):
            with db() as s:
                cats = config_service.list_categories(s)
            cat = pick_category("plan_new_cat", cats)
            if cat:
                start, end = month_bounds(y, m)
                default = max(start, today_ist()) if start <= today_ist() <= end else start
                fkey = f"plan_new_{cat.key}_{cid}_{y}_{m}"
                res = event_form(fkey, user, cat, cid, default_date=default)
                res, late_reason = creation_gate(f"{fkey}_gate", user, cat, res)
                if res:
                    _create(user, cat, cid, res, late_reason)
        with st.expander("🔁 Recurring events (review before adding)"):
            with db() as s:
                props = recurrence.proposals(s, user, y, m, centre_ids=[cid])
                labels = [(p.rule.id, p.centre.id, p.title, p.date, p.exists, recurrence.describe_rule(p.rule)) for p in props]
            if not labels:
                st.caption("No recurring rules apply to this centre.")
            pick = []
            for rid, ccid, title, d, exists, desc in labels:
                if exists:
                    st.markdown(f"✅ {title} – {fmt_date(d)} *(already in plan)*")
                elif st.checkbox(f"{title} – {fmt_date(d)} ({desc})", key=f"rec_{rid}_{ccid}_{y}_{m}", value=True):
                    pick.append((rid, ccid))
            if pick and st.button("Add selected as draft proposals", key="rec_go"):
                run(lambda s: recurrence.generate(s, user, y, m, pick),
                    "Proposed events added as drafts – review dates/times, complete details, then submit.")

    # ---- events in the month
    st.markdown(f"#### Events in {month_label(y, m)}")
    view = st.segmented_control("View", ["List", "Calendar"], default="List", key="plan_view",
                                label_visibility="collapsed")
    if view == "Calendar":
        month_view(events, y, m, user, key=f"plancal_{cid}", show_centre=False)
    else:
        events_table(events, key=f"plan_tbl_{cid}_{y}_{m}", empty="No events planned for this month yet.")

    # ---- submit
    if can_edit:
        with st.container(border=True):
            st.markdown("**Submit plan to Head Office**")
            note = st.text_area("Note to Head Office (required if a mandatory activity is missing)",
                                key=f"plan_note_{cid}_{y}_{m}", height=70)
            pending = comp.unsubmitted
            label = "Resubmit plan" if comp.submitted else "Submit plan"
            if st.button(f"{label} ({pending} event(s))", type="primary", disabled=pending == 0,
                         key=f"plan_submit_{cid}_{y}_{m}"):
                run(lambda s: planning.submit_plan(s, user, cid, y, m, note),
                    f"{month_label(y, m)} plan submitted to Head Office")
            if pending == 0 and comp.submitted:
                st.caption("Nothing new to submit. Add or revise events to resubmit.")

    if history:
        with st.expander("Plan history"):
            st.dataframe(pd.DataFrame([{"When (IST)": fmt_dt(h.created_at), "Action": h.action.replace("_", " "),
                                        "By": h.actor.full_name, "Comment": h.comment or ""} for h in history]),
                         hide_index=True, width="stretch")


def _create(user, cat, centre_id, res, late_reason=None) -> None:
    def do(s):
        e, warnings = ev_svc.create_event(s, user, category_id=cat.id, centre_id=centre_id, common=res["common"],
                                          details=res["details"], linked_event_id=res["linked"],
                                          submit=res["action"] == "submit", late_reason=late_reason)
        for w in warnings:
            flash("warning", w)
        return e
    verb = "created and submitted" if res["action"] == "submit" else "saved as draft"
    run(do, f"Event {verb}.")


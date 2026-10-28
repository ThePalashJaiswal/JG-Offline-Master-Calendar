"""Events list, new event, approvals queue and national events."""
from __future__ import annotations

from datetime import timedelta

import streamlit as st

from jgevents.domain.constants import Action, Status
from jgevents.services import config_service, directory, reports
from jgevents.services import events as ev_svc
from jgevents.services.permissions import is_ho
from jgevents.utils.dates import fmt_date, today_ist
from ui.components.widgets import event_filters, event_form, events_table, pick_category
from ui.core import category_md, db, esc, flash, me, open_event, page_header, run, time_range

# ---------------------------------------------------------------- all events

def events_list() -> None:
    user = me()
    page_header("Events", "Search across months and centres")
    c1, c2, c3 = st.columns([1, 1, 2])
    today = today_ist()
    d_from = c1.date_input("From", value=today - timedelta(days=30), format="DD-MM-YYYY", key="ev_from")
    d_to = c2.date_input("To", value=today + timedelta(days=90), format="DD-MM-YYYY", key="ev_to")
    search = c3.text_input("Search title, description or venue", key="ev_search")
    f = event_filters("ev", user, expanded=False)
    f.date_from, f.date_to, f.search = d_from, d_to, search or None
    with db() as s:
        events = ev_svc.list_events(s, user, f, limit=2000)
    st.caption(f"{len(events)} event(s)" + (" (showing first 2000)" if len(events) == 2000 else ""))
    events_table(events, key="ev_tbl", height=560)
    if events:
        df = reports.events_df(events, with_details=True)
        c1, c2, _ = st.columns([1, 1, 3])
        c1.download_button("⬇ CSV", reports.to_csv(df), "jg_events.csv", "text/csv", on_click="ignore")
        c2.download_button("⬇ Excel", reports.to_excel({"Events": df}), "jg_events.xlsx",
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", on_click="ignore")


# ---------------------------------------------------------------- new event

def new_event() -> None:
    user = me()
    page_header("New event")
    with db() as s:
        centres = directory.manageable_centres(s, user)
        cats = config_service.list_categories(s)
    if not centres:
        st.warning("You cannot create events for any centre.")
        return
    c1, c2 = st.columns(2)
    with c1:
        cid = st.selectbox("Centre", [c.id for c in centres], key="new_centre",
                           format_func=lambda i: next(c.name for c in centres if c.id == i))
    with c2:
        cat = pick_category("new_cat", cats)
    if not cat:
        return
    if cat.supports_national and is_ho(user):
        st.info("For a network-wide All India Open Mock, use **National events** so every centre gets its own "
                "participation record linked to one parent.")
    res = event_form(f"new_{cat.key}_{cid}", user, cat, cid, default_date=today_ist() + timedelta(days=7))
    if res:
        def do(s):
            e, warnings = ev_svc.create_event(s, user, category_id=cat.id, centre_id=cid, common=res["common"],
                                              details=res["details"], linked_event_id=res["linked"],
                                              submit=res["action"] == "submit")
            for w in warnings:
                flash("warning", w)
            return e.id
        e_id = run(do, "Event created.", rerun=False)
        if e_id:
            open_event(e_id)


# ---------------------------------------------------------------- approvals

def approvals() -> None:
    user = me()
    page_header("Approvals", "Submitted events waiting for a Head Office decision")
    f = event_filters("appr", user, show_status=False)
    with db() as s:
        queue = ev_svc.approval_queue(s, user, f)
        for e in queue:
            _ = e.category, e.centre
    if not queue:
        st.success("Nothing waiting for approval. 🎉")
        return
    st.caption(f"{len(queue)} event(s) pending · oldest first by event date")
    by_centre: dict[str, list] = {}
    for e in queue:
        by_centre.setdefault(e.centre.name if e.centre else "National", []).append(e)
    for centre, items in by_centre.items():
        with st.expander(f"**{centre}** · {len(items)} pending", expanded=True):
            sel_key = f"appr_sel_{centre}"
            chosen = []
            for e in items:
                with st.container(border=True):
                    c0, c1, c2 = st.columns([0.4, 5, 3], vertical_alignment="center")
                    if c0.checkbox("Select", key=f"{sel_key}_{e.id}", label_visibility="collapsed"):
                        chosen.append(e)
                    flags = ev_svc.risk_flags(e)
                    c1.markdown(f"`{e.code}` {category_md(e)} · **{esc(e.title)}**  \n"
                                f"{fmt_date(e.event_date)} · {time_range(e)} · {esc(e.responsible_person) or '—'}"
                                + (f"  \n:orange[{' · '.join(flags)}]" if flags else ""))
                    comment = c2.text_input("Comment", key=f"appr_c_{e.id}", label_visibility="collapsed",
                                            placeholder="Comment (required to return/reject)")
                    b1, b2, b3, b4 = c2.columns(4)
                    if b1.button("✔", key=f"appr_a_{e.id}", help="Approve", width="stretch", type="primary"):
                        run(lambda s, e=e, c=comment: ev_svc.transition(s, user, e.id, Action.APPROVE, c, e.version),
                            f"{e.code} approved")
                    if b2.button("↩", key=f"appr_r_{e.id}", help="Return for revision", width="stretch"):
                        run(lambda s, e=e, c=comment: ev_svc.transition(s, user, e.id, Action.RETURN, c, e.version),
                            f"{e.code} returned")
                    if b3.button("⛔", key=f"appr_x_{e.id}", help="Reject", width="stretch"):
                        run(lambda s, e=e, c=comment: ev_svc.transition(s, user, e.id, Action.REJECT, c, e.version),
                            f"{e.code} rejected")
                    if b4.button("👁", key=f"appr_o_{e.id}", help="Open details", width="stretch"):
                        open_event(e.id)
            if chosen and st.button(f"Approve {len(chosen)} selected", key=f"bulk_{centre}", type="primary"):
                def do(s, chosen=chosen):
                    for e in chosen:
                        ev_svc.transition(s, user, e.id, Action.APPROVE, None, e.version)
                run(do, f"{len(chosen)} event(s) approved")


# ---------------------------------------------------------------- national events

def national() -> None:
    user = me()
    page_header("National events", "All India Open Mock and other network-wide events with per-centre records")
    with db() as s:
        cats = [c for c in config_service.list_categories(s) if c.supports_national]
        centres = directory.list_centres(s, user)
        parents = ev_svc.national_events(s, user)
        summary = []
        for p in parents:
            kids = ev_svc.children(s, p.id)
            summary.append((p, kids))
    if not cats:
        st.info("No category is enabled for national events (Admin → Categories).")
        return
    with st.expander("➕ Create national event", expanded=not parents):
        cat = pick_category("nat_cat", cats)
        chosen = st.multiselect("Centres taking part", [c.id for c in centres], default=[c.id for c in centres],
                                format_func=lambda i: next(c.name for c in centres if c.id == i), key="nat_centres")
        st.caption("Head Office sets the common details. Each centre then completes its own venue, expected "
                   "participants and participation plan, and submits it with its monthly plan.")
        res = event_form(f"nat_new_{cat.key}", user, cat, None, default_date=today_ist() + timedelta(days=20))
        if res:
            def do(s):
                p, kids = ev_svc.create_national_event(s, user, category_id=cat.id, common=res["common"],
                                                       details=res["details"], centre_ids=chosen)
                return p.id
            pid = run(do, "National event created and assigned to centres.", rerun=False)
            if pid:
                open_event(pid)
    for p, kids in summary:
        with st.container(border=True):
            c1, c2 = st.columns([4, 1], vertical_alignment="center")
            done = {s_: sum(1 for k in kids if k.status == s_) for s_ in Status}
            c1.markdown(f"`{p.code}` {category_md(p)} · **{esc(p.title)}** · {fmt_date(p.event_date)} · {time_range(p)}  \n"
                        f"{len(kids)} centre(s) · {done[Status.DRAFT]} draft · {done[Status.SUBMITTED]} submitted · "
                        f"{sum(done[s_] for s_ in (Status.APPROVED, Status.IN_PROGRESS, Status.COMPLETED))} approved · "
                        f"{done[Status.COMPLETED]} completed"
                        + (" · :gray-badge[Cancelled]" if p.status == Status.CANCELLED else ""))
            if c2.button("Open", key=f"nat_open_{p.id}", width="stretch"):
                open_event(p.id)


"""v2 pages: preparation task list, Head Office readiness board, playbook editor."""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

import pandas as pd
import streamlit as st

from jgevents.domain.constants import APPROVED_LIKE
from jgevents.services import config_service, directory
from jgevents.services import events as ev_svc
from jgevents.services import tasks as task_svc
from jgevents.services.events import EventFilter
from jgevents.utils.dates import fmt_date, fmt_dt, month_bounds, month_label, today_ist
from ui.components.tasks_ui import task_table
from ui.core import db, esc, me, month_picker, open_event, page_header, run

STATE_FILTERS = {"Overdue": "overdue", "Blocked": "blocked", "Not started": "open", "Done": "done",
                 "Done late": "done_late", "Waived": "waived", "Not needed": "not_needed", "Closed": "closed"}


# ---------------------------------------------------------------- task list (all roles)

def tasks_page() -> None:
    user = me()
    page_header("Preparation tasks", "Every playbook task across your events. Overdue first.")
    with db() as s:
        centres = directory.list_centres(s, user, include_inactive=True)
        cats = [c for c in config_service.list_categories(s, include_inactive=True) if task_svc.has_playbook(s, c.id)]
    today = today_ist()
    c1, c2, c3 = st.columns([1, 1, 2])
    d_from = c1.date_input("Due from", value=today - timedelta(days=14), format="DD-MM-YYYY", key="tk_from")
    d_to = c2.date_input("Due to", value=today + timedelta(days=30), format="DD-MM-YYYY", key="tk_to")
    states = c3.multiselect("Status", list(STATE_FILTERS), default=["Overdue", "Blocked", "Not started"],
                            key="tk_states", placeholder="Any status")
    c4, c5, c6, c7 = st.columns([1.3, 1.3, 1.3, 0.8], vertical_alignment="bottom")
    cids = c4.multiselect("Centre", [c.id for c in centres], key="tk_centres", placeholder="All centres",
                          format_func=lambda i: next(c.name for c in centres if c.id == i)) \
        if len(centres) > 1 else None
    catids = c5.multiselect("Category", [c.id for c in cats], key="tk_cats", placeholder="All playbook categories",
                            format_func=lambda i: next(f"{c.icon} {c.name}" for c in cats if c.id == i))
    owners = c6.multiselect("Owner", ["Centre Head", "Counselling", "Marketing", "HO Academic"], key="tk_owner",
                            placeholder="Any owner")
    crit = c7.toggle("Critical only", key="tk_crit")
    with db() as s:
        rows = task_svc.list_tasks(s, user, task_svc.TaskFilter(
            date_from=d_from, date_to=d_to, centre_ids=cids or None, category_ids=catids or None,
            owner_labels=owners or None, critical_only=crit,
            states=[STATE_FILTERS[x] for x in states] or None))
        rows = [t for t in rows if t.status != "closed" or "Closed" in states]
        for t in rows:
            _ = t.event, t.centre
    rows.sort(key=lambda t: (not task_svc.is_overdue(t), t.due_date, t.sort_order))
    st.caption(f"{len(rows)} task(s)")
    task_table(rows, user, key="tk_tbl", height=520, empty="No tasks match these filters.")
    if rows:
        from jgevents.services import reports
        with db() as s:
            df = pd.DataFrame([{
                "Task": t.code, "Centre": t.centre.name if t.centre else "Head Office", "Event": t.event.code,
                "Event title": t.event.title, "Event date": fmt_date(t.event.event_date), "Key": t.key,
                "Task title": t.title, "T": task_svc.t_label(t.offset_days), "Due": fmt_date(t.due_date),
                "Owner": t.owner_label, "Critical": "Yes" if t.is_critical else "",
                "Status": task_svc.STATE_LABELS[task_svc.state_of(t)], "Done (IST)": fmt_dt(t.done_at),
                "Proof link": t.proof_url or "", "Proof count": t.proof_count, "Registrations": t.proof_count2,
                "Reason": t.status_reason or ""} for t in rows])
        st.download_button("⬇ Excel", reports.to_excel({"Tasks": df}), "jg_preparation_tasks.xlsx",
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", on_click="ignore")

    with db() as s:
        flags = task_svc.list_flags(s, user)
        for f in flags:
            _ = f.task.event, f.task.centre
    if flags:
        st.markdown(f"#### 🚩 Open audit flags ({len(flags)})")
        for f in flags:
            with st.container(border=True):
                t = f.task
                st.markdown(f"**{t.centre.name if t.centre else 'HO'} · {t.event.code} {esc(t.event.title)} · "
                            f"{t.key} {esc(t.title)}**  \n{task_svc.FLAG_TYPES[f.flag_type]}: {esc(f.comment)}"
                            + (f"  \n*Centre reply:* {esc(f.centre_reply)}" if f.centre_reply else
                               "  \n:orange[Awaiting centre reply]"))
                if st.button("Open task", key=f"flag_open_{f.id}"):
                    from ui.components.tasks_ui import task_dialog
                    task_dialog(t.id, user)


# ---------------------------------------------------------------- readiness board (Head Office / viewers)

def readiness_board() -> None:
    user = me()
    page_header("Readiness board", "Which events are prepared, which are slipping, and where to intervene")
    y, m = month_picker("rb")
    start, end = month_bounds(y, m)
    with db() as s:
        centres = directory.list_centres(s, user)
        cats = [c for c in config_service.list_categories(s) if task_svc.has_playbook(s, c.id)]
    c1, c2, c3 = st.columns(3)
    cids = c1.multiselect("Centre", [c.id for c in centres], key="rb_centres", placeholder="All centres",
                          format_func=lambda i: next(c.name for c in centres if c.id == i))
    catids = c2.multiselect("Category", [c.id for c in cats], key="rb_cats", placeholder="All playbook categories",
                            format_func=lambda i: next(f"{c.icon} {c.name}" for c in cats if c.id == i))
    rstates = c3.multiselect("Readiness", list(task_svc.READINESS_LABELS), key="rb_ready",
                             format_func=lambda k: f"{task_svc.READINESS_ICONS[k]} {task_svc.READINESS_LABELS[k]}",
                             placeholder="Any readiness")
    today = today_ist()
    with db() as s:
        evs = ev_svc.list_events(s, user, EventFilter(date_from=start, date_to=end, centre_ids=cids or None,
                                                      category_ids=catids or None, include_national_parents=True))
        evs = [e for e in evs if not cids or e.centre_id in cids]
        all_tasks = task_svc.list_tasks(s, user, task_svc.TaskFilter(centre_ids=cids or None,
                                                                     category_ids=catids or None))
        ev_ids = {e.id for e in evs}
        ev_tasks = [t for t in all_tasks if t.event_id in ev_ids]
        dep = task_svc.dependency_map(s, ev_tasks)
        by_event = defaultdict(list)
        for t in ev_tasks:
            by_event[t.event_id].append(t)
        rows = []
        for e in evs:
            ts = by_event.get(e.id)
            if not ts:
                continue
            r = task_svc.readiness(s, e, ts, dep)
            nxt = min((t for t in ts if t.status == "open"), key=lambda t: t.due_date, default=None)
            rows.append((e, r, nxt, ts))
        period_tasks = [t for t in all_tasks if start <= t.due_date <= end and t.status != "closed"
                        and t.event.status not in ("cancelled", "rejected")]
        pdep = task_svc.dependency_map(s, period_tasks)
        flags = task_svc.list_flags(s, user, task_ids=[t.id for t in period_tasks])
        for e, _r, _nxt, ts in rows:
            _ = e.category, e.centre
            for t in ts:
                _ = t.event, t.centre
        for t in period_tasks:
            _ = t.event, t.centre
    if rstates:
        rows = [x for x in rows if x[1].status in rstates]
    rows.sort(key=lambda x: (task_svc.READINESS_ORDER.get(x[1].status, 9), x[0].event_date))

    # ---- metrics (each opens its records)
    def st_(t):
        return task_svc.state_of(t, pdep)
    due_so_far = [t for t in period_tasks if t.due_date <= today and t.status != "not_needed"]
    metrics = {
        "events": ("Events with a playbook", [x[0].id for x in rows], "event"),
        "approved": ("Plans approved", [x[0].id for x in rows if x[0].status in APPROVED_LIKE], "event"),
        "ready": ("Ready", [x[0].id for x in rows if x[1].status == "ready"], "event"),
        "risk": ("Off track / at risk", [x[0].id for x in rows if x[1].status in ("off_track", "at_risk",
                                                                                  "blocked_ho")], "event"),
        "due": ("Tasks due so far", [t.id for t in due_so_far], "task"),
        "ontime": ("Done on time", [t.id for t in period_tasks if st_(t) == "done"], "task"),
        "late": ("Done late", [t.id for t in period_tasks if st_(t) == "done_late"], "task"),
        "overdue": ("Overdue", [t.id for t in period_tasks if task_svc.is_overdue(t)], "task"),
        "crit": ("Critical overdue", [t.id for t in period_tasks if task_svc.is_overdue(t) and t.is_critical], "task"),
        "blocked": ("Blocked", [t.id for t in period_tasks if task_svc.blockers(t, pdep)], "task"),
        "waived": ("Waived", [t.id for t in period_tasks if t.status == "waived"], "task"),
        "flags": ("Open audit flags", sorted({f.task_id for f in flags}), "task"),
    }
    st.markdown(f"#### {month_label(y, m)}")
    cols = st.columns(6)
    for i, (k, (label, ids, _kind)) in enumerate(metrics.items()):
        active = st.session_state.get("rb_drill") == k
        if cols[i % 6].button(f"**{len(ids)}**  \n{label}", key=f"rb_m_{k}", width="stretch",
                              type="primary" if active else "secondary"):
            st.session_state["rb_drill"] = None if active else k
            st.rerun()
    finished = len(metrics["ontime"][1]) + len(metrics["late"][1])
    st.caption("On-time rate (done on time ÷ all done): "
               + (f"**{len(metrics['ontime'][1]) / finished:.0%}**" if finished else "n/a yet"))
    drill = st.session_state.get("rb_drill")
    if drill:
        label, ids, kind = metrics[drill]
        with st.container(border=True):
            st.markdown(f"**{label}** – records")
            if kind == "task":
                task_table([t for t in period_tasks if t.id in set(ids)], user, key=f"rb_drill_{drill}")
            else:
                _board_table([x for x in rows if x[0].id in set(ids)], key=f"rb_drill_{drill}")

    crit_delays = [x for x in rows if x[1].status in ("off_track", "blocked_ho")
                   or (x[1].status not in ("ready", "cancelled") and (x[0].event_date - today).days <= 1
                       and x[0].event_date >= today)]
    if crit_delays:
        st.error("**Critical delays** – " + "; ".join(
            f"{e.centre.code if e.centre else 'HO'} {e.title} ({fmt_date(e.event_date)}): {r.reasons[0] if r.reasons else r.label}"
            for e, r, _n, _t in crit_delays[:8]), icon=":material/priority_high:")

    st.markdown("**Events – worst first** · select a row to open")
    _board_table(rows, key="rb_board")

    st.markdown("**Centre comparison**")
    comp = defaultdict(lambda: dict(events=0, ready=0, off=0, due=0, ontime=0, late=0, overdue=0))
    for e, r, _n, _ts in rows:
        c = comp[e.centre.name if e.centre else "Head Office"]
        c["events"] += 1
        c["ready"] += r.status == "ready"
        c["off"] += r.status in ("off_track", "at_risk")
    for t in period_tasks:
        c = comp[t.centre.name if t.centre else "Head Office"]
        s_ = st_(t)
        c["due"] += t.due_date <= today and t.status != "not_needed"
        c["ontime"] += s_ == "done"
        c["late"] += s_ == "done_late"
        c["overdue"] += s_ == "overdue"
    if comp:
        st.dataframe(pd.DataFrame([{
            "Centre": k, "Events": v["events"], "Ready": v["ready"], "Off track / at risk": v["off"],
            "Tasks due so far": v["due"], "Done on time": v["ontime"], "Done late": v["late"], "Overdue now": v["overdue"],
            "On-time rate %": round(100 * v["ontime"] / (v["ontime"] + v["late"])) if v["ontime"] + v["late"] else None,
        } for k, v in sorted(comp.items())]), hide_index=True, width="stretch",
            column_config={"On-time rate %": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%d%%")})
    else:
        st.caption("No playbook events in this month.")


def _board_table(rows: list[tuple], key: str) -> None:
    if not rows:
        st.info("No events with preparation tasks for these filters.", icon=":material/fact_check:")
        return
    df = pd.DataFrame([{
        "id": e.id, "Readiness": f"{r.icon} {r.label}", "Date": e.event_date,
        "Centre": e.centre.name if e.centre else "National", "Event": f"{e.category.icon} {e.title}",
        "Event status": e.status.replace("_", " ").title(), "Done": f"{r.done + r.waived}/{r.total - r.not_needed}",
        "Overdue": r.overdue, "Why": " | ".join(r.reasons[:2]),
        "Next deadline": f"{n.key} {fmt_date(n.due_date)} ({n.owner_label})" if n else "",
        "Late booking": "Yes" if e.booked_late else "",
    } for e, r, n, _ts in rows])
    sel = st.dataframe(df, key=key, hide_index=True, on_select="rerun", selection_mode="single-row",
                       column_order=[c for c in df.columns if c != "id"],
                       column_config={"Date": st.column_config.DateColumn(format="DD-MM-YYYY"),
                                      "Why": st.column_config.TextColumn(width="large")})
    picked = sel.selection.rows if sel and sel.selection else []
    if picked:
        open_event(int(df.iloc[picked[0]]["id"]))


# ---------------------------------------------------------------- playbook editor (Head Office)

PROOF_CHOICES = ["none", "url", "count", "seats", "whatsapp_auto"]


def playbooks_page() -> None:
    user = me()
    page_header("Playbooks", "Preparation tasks generated for each new event. Saving creates a new version; "
                             "existing events keep theirs.")
    with db() as s:
        cats = config_service.list_categories(s)
    cid = st.selectbox("Category", [c.id for c in cats], key="pb_cat",
                       format_func=lambda i: next(f"{c.icon} {c.name}" for c in cats if c.id == i))
    cat = next(c for c in cats if c.id == cid)
    with db() as s:
        versions = task_svc.playbook_versions(s, cid)
        active = next((v for v in versions if v.is_active), None)
        for v in versions:
            _ = v.tasks
    if active:
        st.markdown(f"Active: **v{active.version}** · created {fmt_dt(active.created_at)} · minimum lead time "
                    f"**{task_svc.min_lead_days(active, cat.supports_national)} days**"
                    + (f" · {esc(active.notes)}" if active.notes else ""))
    else:
        st.info("This category has no playbook yet. Add rows below to create version 1.")
    base = [dict(key=t.key, title=t.title, offset_days=t.offset_days, owner_label=t.owner_label,
                 is_critical=t.is_critical, scope=t.scope, depends_on=", ".join(t.depends_on or []),
                 proof_type=t.proof_type, proof_label=t.proof_label or "", skip_if_mode=t.skip_if_mode or "")
            for t in (active.tasks if active else [])]
    df = pd.DataFrame(base or [dict(key="", title="", offset_days=-7, owner_label="Centre Head", is_critical=True,
                                    scope="centre", depends_on="", proof_type="none", proof_label="",
                                    skip_if_mode="")])
    st.caption("offset_days: negative = before the event (-7 = T-7), positive = after. depends_on: comma-separated keys. "
               "proof_type: none · url · count · seats (seats ≥ registrations) · whatsapp_auto (ticks when a linked "
               "WhatsApp campaign is completed). scope 'national' = Head Office task on a national event.")
    edited = st.data_editor(
        df, key=f"pb_editor_{cid}_{active.version if active else 0}", num_rows="dynamic", hide_index=True,
        width="stretch",
        column_config={
            "key": st.column_config.TextColumn("Key", max_chars=10, width="small"),
            "title": st.column_config.TextColumn("Task", width="large"),
            "offset_days": st.column_config.NumberColumn("Offset (days)", step=1),
            "owner_label": st.column_config.SelectboxColumn("Owner", options=[
                "Centre Head", "Counselling", "Marketing", "Academic", "HO Academic", "HO Marketing", "Operations"]),
            "is_critical": st.column_config.CheckboxColumn("Critical"),
            "scope": st.column_config.SelectboxColumn("Scope", options=["centre", "national"]),
            "depends_on": st.column_config.TextColumn("Depends on"),
            "proof_type": st.column_config.SelectboxColumn("Proof", options=PROOF_CHOICES),
            "proof_label": st.column_config.TextColumn("Proof label"),
            "skip_if_mode": st.column_config.TextColumn("Skip if mode"),
        })
    notes = st.text_input("What changed (saved with the version)", key=f"pb_notes_{cid}")
    if st.button("Save as new version", type="primary", key=f"pb_save_{cid}"):
        rows = edited.to_dict("records")
        run(lambda s: task_svc.save_playbook(s, user, cid, rows, notes),
            "Playbook saved. New events use it; existing events are unchanged.")
    if len(versions) > 1:
        with st.expander(f"Version history ({len(versions)})"):
            for v in versions:
                st.markdown(f"**v{v.version}**{' (active)' if v.is_active else ''} · {fmt_dt(v.created_at)} · "
                            f"{len(v.tasks)} tasks · {esc(v.notes or '')}")


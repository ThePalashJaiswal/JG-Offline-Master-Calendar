"""v2 UI pieces: readiness badges, task tables, task dialog, schedule preview gate."""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from jgevents.db.models import Event, EventCategory, EventTask, User
from jgevents.services import tasks as task_svc
from jgevents.services.permissions import can_manage_centre, is_ho
from jgevents.utils.dates import fmt_date, fmt_dt, today_ist
from ui.core import db, esc, open_event, run

READINESS_COLOURS = {"ready": "green", "on_track": "blue", "at_risk": "orange", "off_track": "red",
                     "blocked_ho": "violet", "cancelled": "gray"}


def readiness_md(r: task_svc.Readiness) -> str:
    if not r.status:
        return ""
    return f":{READINESS_COLOURS[r.status]}-badge[{r.icon} {r.label}]"


def state_md(t: EventTask, dep: dict | None = None) -> str:
    st_ = task_svc.state_of(t, dep)
    colour = {"overdue": "red", "blocked": "violet", "done": "green", "done_late": "orange", "waived": "gray",
              "not_needed": "gray", "closed": "gray", "open": "blue"}[st_]
    return f":{colour}-badge[{task_svc.STATE_ICONS[st_]} {task_svc.STATE_LABELS[st_]}]"


def days_text(t: EventTask) -> str:
    if t.status != "open":
        return ""
    d = (t.due_date - today_ist()).days
    return "due today" if d == 0 else (f"{-d}d overdue" if d < 0 else f"{d}d left")


def proof_text(t: EventTask) -> str:
    if t.proof_type == "url":
        return t.proof_url or ""
    if t.proof_type == "count":
        return "" if t.proof_count is None else f"{t.proof_count} ({t.proof_label or 'count'})"
    if t.proof_type == "seats":
        return "" if t.proof_count is None else f"{t.proof_count} seats / {t.proof_count2} registered"
    if t.proof_type == "whatsapp_auto":
        return t.status_reason or "Waiting for linked WhatsApp campaign"
    return ""


def tasks_frame(tasks: list[EventTask], dep: dict, flagged: set[int] | None = None) -> pd.DataFrame:
    flagged = flagged or set()
    rows = []
    for t in tasks:
        st_ = task_svc.state_of(t, dep)
        bl = task_svc.blockers(t, dep)
        rows.append({
            "id": t.id, "Due": t.due_date, "When": days_text(t), "T": task_svc.t_label(t.offset_days),
            "Centre": t.centre.code if t.centre else "HO", "Event": f"{t.event.code} · {t.event.title}",
            "Task": f"{t.key} · {t.title}", "Owner": t.owner_label, "Critical": "★" if t.is_critical else "",
            "Status": f"{task_svc.STATE_ICONS[st_]} {task_svc.STATE_LABELS[st_]}",
            "Waiting on": ", ".join(b.key for b in bl), "Proof": proof_text(t),
            "Flag": "🚩" if t.id in flagged else "",
        })
    return pd.DataFrame(rows)


def task_table(tasks: list[EventTask], user: User, key: str, empty: str = "No tasks.",
               height: int | str = "auto", hide: tuple[str, ...] = ()) -> None:
    if not tasks:
        st.info(empty, icon=":material/task_alt:")
        return
    with db() as s:
        dep = task_svc.dependency_map(s, tasks)
        flagged = {f.task_id for f in task_svc.list_flags(s, user, task_ids=[t.id for t in tasks])}
    df = tasks_frame(tasks, dep, flagged)
    cols = [c for c in ["Due", "When", "T", "Centre", "Event", "Task", "Owner", "Critical", "Status", "Waiting on",
                        "Proof", "Flag"] if c not in hide]
    sel = st.dataframe(df, key=key, hide_index=True, on_select="rerun", selection_mode="single-row", height=height,
                       column_order=cols,
                       column_config={"Due": st.column_config.DateColumn(format="DD-MM-YYYY"),
                                      "Task": st.column_config.TextColumn(width="large"),
                                      "Event": st.column_config.TextColumn(width="medium")})
    rows = sel.selection.rows if sel and sel.selection else []
    if rows:
        task_dialog(int(df.iloc[rows[0]]["id"]), user)
    st.caption("Select a row to open the task. ★ = critical.")


@st.dialog("Preparation task", width="large")
def task_dialog(task_id: int, user: User) -> None:
    task_panel(task_id, user, key="dlg")


def task_panel(task_id: int, user: User, key: str) -> None:
    with db() as s:
        t = task_svc.get_task(s, user, task_id)
        dep = task_svc.dependency_map(s, [t])
        bl = task_svc.blockers(t, dep)
        flags = task_svc.list_flags(s, user, open_only=False, task_ids=[t.id])
        ev: Event = t.event
        _ = ev.category, t.done_by
    manage = is_ho(user) if t.scope == "national" else can_manage_centre(user, t.centre_id)
    live = ev.status not in ("cancelled", "rejected")
    st.markdown(f"#### {t.key} · {esc(t.title)}")
    st.markdown(f"{state_md(t, dep)} &nbsp; `{t.code}` &nbsp; {'★ critical' if t.is_critical else 'non-critical'}")
    c1, c2, c3 = st.columns(3)
    c1.markdown(f"**Due**  \n{fmt_date(t.due_date)} ({task_svc.t_label(t.offset_days)}) {days_text(t)}")
    c2.markdown(f"**Owner**  \n{t.owner_label}")
    c3.markdown(f"**Centre**  \n{t.centre.name if t.centre else 'Head Office'}")
    if st.button(f"Event: {ev.code} · {ev.title} ({fmt_date(ev.event_date)})", key=f"{key}_ev_{t.id}", type="tertiary"):
        open_event(ev.id)
    if bl:
        st.warning("Waiting on: " + ", ".join(f"{b.key} {b.title} ({task_svc.STATE_LABELS[task_svc.state_of(b)]})"
                                              for b in bl))
    if t.status in ("done", "waived", "closed", "not_needed"):
        who = t.done_by.full_name if t.done_by else ""
        if t.status == "done":
            st.success(f"Done {fmt_dt(t.done_at)}{' by ' + who if who else ''}"
                       + (" – after the deadline" if t.done_late else ""))
        if proof_text(t):
            st.markdown(f"**Proof:** {esc(proof_text(t))}")
        if t.status_reason:
            st.caption(t.status_reason)
    if t.notes:
        st.caption(f"Notes: {t.notes}")

    if manage and live and t.status == "open" and not bl and t.proof_type != "whatsapp_auto":
        with st.form(f"{key}_done_{t.id}", border=True):
            st.markdown("**Mark done**")
            url = cnt = cnt2 = None
            if t.proof_type == "url":
                url = st.text_input(f"{t.proof_label or 'Link'} *", placeholder="https://…")
            if t.proof_type == "count":
                cnt = st.number_input(f"{t.proof_label or 'Count'} *", min_value=0, step=1, value=None)
            if t.proof_type == "seats":
                a, b = st.columns(2)
                cnt = a.number_input("Seats arranged *", min_value=0, step=1, value=None)
                cnt2 = b.number_input("Registrations *", min_value=0, step=1, value=None)
            notes = st.text_input("Note (optional)")
            if st.form_submit_button("Mark done", type="primary"):
                run(lambda s: task_svc.complete_task(s, user, t.id, t.version, proof_url=url, proof_count=cnt,
                                                     proof_count2=cnt2, notes=notes), f"{t.key} marked done.")
    elif t.status == "open" and t.proof_type == "whatsapp_auto":
        st.info("This task ticks itself when a WhatsApp campaign linked to this event is marked completed.")

    if live and t.status in ("done", "waived") and manage:
        can_reopen = is_ho(user) or (t.status == "done" and today_ist() <= ev.event_date)
        if can_reopen:
            with st.expander("Reopen"):
                reason = st.text_input("Reason", key=f"{key}_ro_r_{t.id}")
                if st.button("Reopen task", key=f"{key}_ro_{t.id}"):
                    run(lambda s: task_svc.reopen_task(s, user, t.id, reason, t.version), f"{t.key} reopened.")
    if is_ho(user) and live and t.status == "open":
        with st.expander("Waive (Head Office)"):
            reason = st.text_input("Reason for waiver", key=f"{key}_wv_r_{t.id}")
            if st.button("Waive task", key=f"{key}_wv_{t.id}"):
                run(lambda s: task_svc.waive_task(s, user, t.id, reason, t.version), f"{t.key} waived.")

    st.markdown("**Audit flags**")
    if not flags:
        st.caption("None.")
    for f in flags:
        with st.container(border=True):
            st.markdown(f"🚩 **{task_svc.FLAG_TYPES[f.flag_type]}** · {fmt_dt(f.created_at)} · {f.created_by.full_name}"
                        + (" · *resolved*" if f.resolved_at else ""))
            st.markdown(esc(f.comment))
            if f.centre_reply:
                st.caption(f"Centre reply: {f.centre_reply}")
            if not f.resolved_at and manage and not is_ho(user):
                reply = st.text_input("Reply", key=f"{key}_fr_{f.id}")
                if st.button("Send reply", key=f"{key}_frb_{f.id}"):
                    run(lambda s, f=f: task_svc.reply_flag(s, user, f.id, reply), "Reply sent.")
            if not f.resolved_at and is_ho(user):
                if st.button("Resolve flag", key=f"{key}_fres_{f.id}"):
                    run(lambda s, f=f: task_svc.resolve_flag(s, user, f.id), "Flag resolved.")
    if is_ho(user):
        with st.expander("Add audit flag (Head Office)"):
            ftype = st.selectbox("Type", list(task_svc.FLAG_TYPES), format_func=task_svc.FLAG_TYPES.get,
                                 key=f"{key}_ft_{t.id}")
            comment = st.text_area("Comment for the Centre Head", key=f"{key}_fc_{t.id}", height=70)
            if st.button("Flag task", key=f"{key}_fb_{t.id}"):
                run(lambda s: task_svc.flag_task(s, user, t.id, ftype, comment), "Flag added; the Centre Head sees it.")


# ---------------------------------------------------------------- creation gate with schedule preview

def schedule_table(rows: list[task_svc.PreviewRow]) -> None:
    st.dataframe(pd.DataFrame([{
        "Task": f"{r.key} · {r.title}", "T": r.t, "Due": r.due, "Owner": r.owner, "Critical": "★" if r.critical else "",
        "Note": "Not needed (online)" if r.not_needed else ("⚠️ already past" if r.past else ""),
    } for r in rows]), hide_index=True, width="stretch",
        column_config={"Due": st.column_config.DateColumn(format="DD-MM-YYYY")})


def creation_gate(state_key: str, user: User, category: EventCategory, res: dict | None,
                  national_parent: bool = False) -> tuple[dict | None, str | None]:
    """For categories with a playbook, show the generated schedule and the lead-time check before saving.
    Returns (form result, late reason) once the user confirms; (None, None) while waiting."""
    if res:
        st.session_state[state_key] = res
    pending = st.session_state.get(state_key)
    if not pending:
        return None, None
    ev_date: date | None = pending["common"].get("event_date")
    with db() as s:
        pb = task_svc.active_playbook(s, category.id)
        if pb is None or ev_date is None:
            st.session_state.pop(state_key, None)
            return pending, None
        rows = task_svc.preview_schedule(s, category.id, ev_date, national_parent=national_parent,
                                         mode=(pending.get("details") or {}).get("mode"))
        lead = task_svc.min_lead_days(pb, national_parent)
    days = (ev_date - today_ist()).days
    with st.container(border=True):
        st.markdown(f"**Preparation schedule** – {category.name} playbook v{pb.version}, event on {fmt_date(ev_date)}")
        schedule_table(rows)
        late = days < lead
        reason = None
        if late and not is_ho(user):
            st.error(f"{category.name} needs at least {lead} days' notice; this date is {days} day(s) away. "
                     "Pick a later date, or ask Head Office to book it as an exception.")
            if st.button("Change the date", key=f"{state_key}_back"):
                st.session_state.pop(state_key, None)
                st.rerun()
            return None, None
        if late:
            st.warning(f"Inside the {lead}-day minimum lead time. Booking it is a Head Office exception and "
                       "missed deadlines will show as overdue.")
            reason = st.text_input("Reason for the exception *", key=f"{state_key}_reason")
        past = [r.key for r in rows if r.past and not r.not_needed]
        if past and not late:
            st.warning("Already past: " + ", ".join(past))
        c1, c2, _ = st.columns([1.2, 1, 3])
        confirm = c1.button("Confirm and save" if pending["action"] != "submit" else "Confirm and submit",
                            type="primary", key=f"{state_key}_ok")
        if c2.button("Back to form", key=f"{state_key}_cancel"):
            st.session_state.pop(state_key, None)
            st.rerun()
    if confirm:
        if late and not (reason or "").strip():
            st.error("Enter a reason for the exception.")
            return None, None
        st.session_state.pop(state_key, None)
        return pending, reason
    return None, None


def date_change_gate(state_key: str, user: User, ev: Event, res: dict | None) -> tuple[dict | None, str | None]:
    """When an edit moves the event date, show old vs new deadlines before saving."""
    if res and res["common"].get("event_date") == ev.event_date:
        return res, None
    if res:
        st.session_state[state_key] = res
    pending = st.session_state.get(state_key)
    if not pending:
        return None, None
    new_date = pending["common"]["event_date"]
    with db() as s:
        rows = task_svc.preview_date_change(s, user, ev.id, new_date)
        pb = task_svc.active_playbook(s, ev.category_id)
        lead = task_svc.min_lead_days(pb, ev.is_national_parent) if pb else 0
    if not rows:
        st.session_state.pop(state_key, None)
        return pending, None
    days = (new_date - today_ist()).days
    with st.container(border=True):
        st.markdown(f"**Date change {fmt_date(ev.event_date)} → {fmt_date(new_date)}: effect on preparation tasks**")
        st.dataframe(pd.DataFrame([{
            "Task": r.key + " · " + r.title, "Status": task_svc.STATE_LABELS.get(r.status, r.status),
            "Old due": r.old_due, "New due": r.new_due,
            "Note": "kept – already finished" if r.new_due is None else ("⚠️ already past" if r.past else ""),
        } for r in rows]), hide_index=True, width="stretch",
            column_config={"Old due": st.column_config.DateColumn(format="DD-MM-YYYY"),
                           "New due": st.column_config.DateColumn(format="DD-MM-YYYY")})
        reason = None
        if days < lead:
            if not is_ho(user):
                st.error(f"The new date is inside the {lead}-day minimum lead time. Ask Head Office.")
                if st.button("Back", key=f"{state_key}_back"):
                    st.session_state.pop(state_key, None)
                    st.rerun()
                return None, None
            reason = st.text_input("Reason for the exception *", key=f"{state_key}_reason")
        c1, c2, _ = st.columns([1.2, 1, 3])
        ok = c1.button("Apply date change", type="primary", key=f"{state_key}_ok")
        if c2.button("Cancel", key=f"{state_key}_cancel"):
            st.session_state.pop(state_key, None)
            st.rerun()
    if ok:
        st.session_state.pop(state_key, None)
        return pending, reason
    return None, None


def overdue_banner(user: User, centre_ids: list[int] | None = None) -> None:
    today = today_ist()
    with db() as s:
        open_tasks = task_svc.list_tasks(s, user, task_svc.TaskFilter(
            date_to=today + timedelta(days=1), centre_ids=centre_ids, include_national=False))
        open_tasks = [t for t in open_tasks if t.status == "open" and t.event.status not in ("cancelled", "rejected")]
        flags = [f for f in task_svc.list_flags(s, user) if not f.centre_reply]
    overdue = [t for t in open_tasks if t.due_date < today]
    due = [t for t in open_tasks if t.due_date >= today]
    if not (overdue or due or flags):
        return
    parts = []
    if overdue:
        parts.append(f"**{len(overdue)} overdue**: " + ", ".join(f"{t.centre.code if t.centre else ''} {t.key} ({t.event.title[:30]})"
                                                               for t in overdue[:5]) + ("…" if len(overdue) > 5 else ""))
    if due:
        parts.append(f"**{len(due)} due today/tomorrow**: " + ", ".join(f"{t.key} ({t.event.title[:30]})" for t in due[:5]))
    if flags:
        parts.append(f"**{len(flags)} audit flag(s)** awaiting your reply")
    (st.error if overdue else st.warning)("  \n".join(parts), icon=":material/notifications:")


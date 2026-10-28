from __future__ import annotations

import pandas as pd
import streamlit as st

from jgevents.domain.constants import (
    ACTION_LABELS,
    CENTRE_EDITABLE,
    EXECUTION_EDITABLE,
    TRANSITIONS,
    Action,
    Status,
)
from jgevents.domain.schema import common_field_config, display_detail
from jgevents.services import directory
from jgevents.services import events as ev_svc
from jgevents.services.errors import ServiceError
from jgevents.services.permissions import can_manage_centre, is_ho
from jgevents.utils.dates import fmt_date, fmt_dt
from ui.components.widgets import event_form, events_table, execution_form
from ui.core import category_md, centre_label, db, esc, flash, me, open_event, page_header, run, status_md, time_range


def _event_id() -> int | None:
    qp = st.query_params.get("id")
    if qp and str(qp).replace("EVT-", "").isdigit():
        st.session_state["event_id"] = int(str(qp).replace("EVT-", ""))
    return st.session_state.get("event_id")


def render() -> None:
    user = me()
    eid = _event_id()
    if not eid:
        page_header("Event details")
        _lookup()
        return
    try:
        with db() as s:
            e = ev_svc.get_event(s, user, eid)
            actions = ev_svc.allowed_actions(user, e)
            items = ev_svc.checklist(s, user, eid)
            refs = ev_svc.references(s, user, eid)
            approvals, logs = ev_svc.history(s, user, eid)
            kids = ev_svc.visible_children(s, user, eid) if e.is_national_parent else []
            camps = ev_svc.linked_campaigns(s, user, eid) if e.category.key == "batch_launch" else []
            parent = s.get(type(e), e.parent_event_id) if e.parent_event_id else None
            linked = s.get(type(e), e.linked_event_id) if e.linked_event_id else None
            for x in [*kids, *camps]:
                _ = x.category, x.centre
    except ServiceError as exc:
        page_header("Event details")
        st.error(str(exc))
        _lookup()
        return

    page_header(f"{e.category.icon} {e.title}")
    st.markdown(f"{status_md(e.status)} &nbsp; {category_md(e)} &nbsp; **{centre_label(e)}** &nbsp; `{e.code}`"
                + (" &nbsp; :orange-badge[Changed after approval]" if e.changed_after_approval else "")
                + (" &nbsp; :violet-badge[Proposed from recurring rule]" if e.is_proposed else ""))
    flags = ev_svc.risk_flags(e)
    if flags:
        st.warning(" · ".join(flags))
    if e.last_review_comment and e.status in (Status.RETURNED, Status.REJECTED, Status.APPROVED):
        (st.error if e.status != Status.APPROVED else st.info)(f"Head Office: {e.last_review_comment}")
    if e.cancel_reason and e.status == Status.CANCELLED:
        st.info(f"Cancelled: {e.cancel_reason}")

    _actions(user, e, actions)

    manage = (is_ho(user) if e.is_national_parent else can_manage_centre(user, e.centre_id))
    editable = manage and (Status(e.status) in CENTRE_EDITABLE or (is_ho(user) and e.status == Status.IN_PROGRESS))
    tabs = st.tabs(["Overview", "Edit" if editable else "Edit (locked)", "Execution",
                    f"Checklist ({sum(i.is_done for i in items)}/{len(items)})", "Links & references", "History"])

    with tabs[0]:
        _overview(e, parent, linked)
        if e.is_national_parent:
            st.markdown("**Centre participation records**")
            events_table(kids, key=f"kids_{e.id}", empty="No centres assigned yet.")
            if is_ho(user):
                with db() as s:
                    centres = directory.list_centres(s, user)
                have = {k.centre_id for k in kids}
                add = st.multiselect("Assign more centres", [c.id for c in centres if c.id not in have],
                                     format_func=lambda i: next(c.name for c in centres if c.id == i), key=f"assign_{e.id}")
                if add and st.button("Assign", key=f"assign_go_{e.id}"):
                    run(lambda s: ev_svc.assign_centres(s, user, e.id, add), f"{len(add)} centre(s) assigned")
        if camps:
            st.markdown("**Linked WhatsApp campaigns**")
            events_table(camps, key=f"camps_{e.id}")

    with tabs[1]:
        if not editable:
            st.info("Planning details are locked for this status or your role. "
                    + ("Use the Execution tab for outcomes." if Status(e.status) in EXECUTION_EDITABLE else ""))
        else:
            if e.status == Status.APPROVED and not is_ho(user):
                st.caption("Changing title, date, time or venue of an approved event sends it back for re-approval.")
            res = event_form(f"edit_{e.id}_{e.version}", user, e.category, e.centre_id, event=e, mode="edit")
            if res:
                def do(s):
                    _, warnings = ev_svc.update_event(s, user, e.id, e.version, common=res["common"],
                                                      details=res["details"], linked_event_id=res["linked"])
                    for w in warnings:
                        flash("warning", w)
                run(do, "Changes saved.")

    with tabs[2]:
        cfg_a = common_field_config(e.category, "actual_participation")
        if manage and Status(e.status) in EXECUTION_EDITABLE:
            res = execution_form(f"exec_{e.id}_{e.version}", e)
            if res:
                run(lambda s: ev_svc.update_execution(s, user, e.id, e.version, actual_participation=res["actual"],
                                                      post_event_remarks=res["post"], details=res["details"]),
                    "Execution details saved.")
        else:
            st.caption("Execution details can be recorded once the event is approved.")
            st.markdown(f"**{cfg_a['label']}:** {e.actual_participation if e.actual_participation is not None else '—'}")
            st.markdown(f"**Post-event remarks:** {esc(e.post_event_remarks) or '—'}")

    with tabs[3]:
        locked = not manage or e.status in (Status.CANCELLED, Status.REJECTED)
        if not items:
            st.caption("No checklist items.")
        for it in items:
            c1, c2 = st.columns([10, 1])
            c1.checkbox(it.label + (f"  ·  done {fmt_dt(it.done_at)}" if it.is_done and it.done_at else ""),
                        value=it.is_done, key=f"chk_{it.id}", disabled=locked,
                        on_change=_toggle, args=(user, it.id))
            if not locked and c2.button("✕", key=f"chk_del_{it.id}", help="Remove item"):
                run(lambda s: ev_svc.delete_checklist_item(s, user, it.id))
        if not locked:
            with st.form(f"chk_add_{e.id}", clear_on_submit=True, border=False):
                c1, c2 = st.columns([5, 1], vertical_alignment="bottom")
                label = c1.text_input("New checklist item")
                if c2.form_submit_button("Add"):
                    run(lambda s: ev_svc.add_checklist_item(s, user, e.id, label))

    with tabs[4]:
        for r in refs:
            c1, c2 = st.columns([10, 1])
            c1.markdown(f"🔗 [{esc(r.label)}]({r.url})")
            if manage and c2.button("✕", key=f"ref_del_{r.id}"):
                run(lambda s: ev_svc.delete_reference(s, user, r.id))
        if not refs:
            st.caption("No links yet – add briefs, creatives or registration sheets.")
        if manage:
            with st.form(f"ref_add_{e.id}", clear_on_submit=True, border=False):
                c1, c2, c3 = st.columns([2, 3, 1], vertical_alignment="bottom")
                label = c1.text_input("Label")
                url = c2.text_input("Link (https://…)")
                if c3.form_submit_button("Add"):
                    run(lambda s: ev_svc.add_reference(s, user, e.id, label, url))

    with tabs[5]:
        st.markdown("**Approval trail**")
        st.dataframe(pd.DataFrame([{"When (IST)": fmt_dt(a.created_at), "Action": a.action.replace("_", " "),
                                    "From": a.from_status or "", "To": a.to_status or "", "By": a.actor.full_name,
                                    "Comment": a.comment or ""} for a in approvals]), hide_index=True, width="stretch")
        st.markdown("**Change log**")
        st.dataframe(pd.DataFrame([{"When (IST)": fmt_dt(lg.created_at), "By": lg.actor.full_name if lg.actor else "system",
                                    "What": lg.summary} for lg in logs]), hide_index=True, width="stretch")
        st.caption(f"Created {fmt_dt(e.created_at)} by {e.created_by.full_name if e.created_by else '—'} · "
                   f"last modified {fmt_dt(e.updated_at)} by {e.updated_by.full_name if e.updated_by else '—'} · "
                   f"version {e.version}")


def _toggle(user, item_id: int) -> None:
    done = st.session_state[f"chk_{item_id}"]
    try:
        with db() as s:
            ev_svc.set_checklist_item(s, user, item_id, done)
    except ServiceError as exc:
        flash("error", str(exc))


def _actions(user, e, actions) -> None:
    if not actions:
        return
    needs_comment = [a for a in actions if TRANSITIONS[a][3]]
    with st.container(border=True):
        comment = ""
        if needs_comment:
            comment = st.text_input("Comment / reason (required for " + ", ".join(
                ACTION_LABELS[a].lower() for a in needs_comment) + ")", key=f"act_c_{e.id}_{e.version}")
        cols = st.columns(len(actions))
        for col, a in zip(cols, actions, strict=True):
            primary = a in (Action.SUBMIT, Action.APPROVE, Action.COMPLETE)
            if col.button(ACTION_LABELS[a], key=f"act_{a}_{e.id}", type="primary" if primary else "secondary",
                          width="stretch"):
                if a == Action.CANCEL:
                    st.session_state[f"confirm_cancel_{e.id}"] = comment
                    st.rerun()
                run(lambda s, a=a: ev_svc.transition(s, user, e.id, a, comment, e.version),
                    f"{e.code}: {ACTION_LABELS[a].lower()} done.")
        pending = st.session_state.get(f"confirm_cancel_{e.id}")
        if pending is not None:
            st.warning(f"Cancel {e.code}? This cannot be undone by a Centre Head.")
            c1, c2, _ = st.columns([1, 1, 3])
            if c1.button("Yes, cancel event", type="primary", key=f"cc_yes_{e.id}"):
                st.session_state.pop(f"confirm_cancel_{e.id}", None)
                run(lambda s: ev_svc.transition(s, user, e.id, Action.CANCEL, pending, e.version), f"{e.code} cancelled.")
            if c2.button("Keep event", key=f"cc_no_{e.id}"):
                st.session_state.pop(f"confirm_cancel_{e.id}", None)
                st.rerun()


def _overview(e, parent, linked) -> None:
    def cfg(k):
        return common_field_config(e.category, k)
    c1, c2, c3 = st.columns(3)
    c1.markdown(f"**{cfg('event_date')['label']}**  \n{fmt_date(e.event_date)} ({e.event_date.strftime('%A')})")
    c2.markdown(f"**Time**  \n{time_range(e)}")
    c3.markdown(f"**{cfg('responsible_person')['label']}**  \n{esc(e.responsible_person) or '—'}")
    pairs = [(k, getattr(e, k)) for k in ("subtype", "target_audience", "venue", "expected_participation",
                                           "preparation_deadline", "objective", "description", "remarks")]
    rows = []
    for k, v in pairs:
        c = cfg(k)
        if c["hidden"] or v in (None, ""):
            continue
        rows.append((c["label"], fmt_date(v) if k == "preparation_deadline" else v))
    for f in e.category.field_schema or []:
        val = (e.details or {}).get(f["key"])
        if val not in (None, "", []):
            rows.append((f["label"] + (" (outcome)" if f.get("phase") == "execution" else ""), display_detail(f, val)))
    if e.actual_participation is not None:
        rows.append((cfg("actual_participation")["label"], e.actual_participation))
    if e.post_event_remarks:
        rows.append(("Post-event remarks", e.post_event_remarks))
    for label, v in rows:
        st.markdown(f"**{label}:** {esc(str(v))}")
    if parent:
        if st.button(f"↑ National event {parent.code}: {parent.title}", key=f"parent_{e.id}", type="tertiary"):
            open_event(parent.id)
    if linked:
        if st.button(f"🔗 Linked to {linked.code}: {linked.title}", key=f"linked_{e.id}", type="tertiary"):
            open_event(linked.id)


def _lookup() -> None:
    with st.form("lookup", border=False):
        c1, c2 = st.columns([3, 1], vertical_alignment="bottom")
        code = c1.text_input("Open event by ID", placeholder="EVT-000123 or 123")
        if c2.form_submit_button("Open"):
            digits = "".join(ch for ch in code if ch.isdigit())
            if digits:
                open_event(int(digits))
    st.caption("Or pick an event from the calendar, dashboard or events list.")

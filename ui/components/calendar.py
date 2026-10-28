"""Macro calendar: month grid, day agenda and chronological list."""
from __future__ import annotations

from collections import defaultdict
from datetime import date

import streamlit as st

from jgevents.db.models import Event, User
from jgevents.domain.constants import (
    LIVE_STATUSES,
    STATUS_ICONS,
    STATUS_LABELS,
    Action,
    Status,
    approval_status,
)
from jgevents.services import events as ev_svc
from jgevents.services.permissions import is_ho
from jgevents.utils.dates import fmt_date, fmt_time, month_grid, times_overlap, today_ist
from ui.core import category_md, centre_label, esc, open_event, run, status_md, time_range

MAX_PER_CELL = 4
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def conflict_ids(events: list[Event]) -> set[int]:
    """Events overlapping another slot-occupying live event at the same centre."""
    groups = defaultdict(list)
    for e in events:
        if e.centre_id and e.category.occupies_slot and Status(e.status) in LIVE_STATUSES:
            groups[(e.centre_id, e.event_date)].append(e)
    out = set()
    for evs in groups.values():
        for i, a in enumerate(evs):
            for b in evs[i + 1:]:
                if times_overlap(a.start_time, a.end_time, b.start_time, b.end_time):
                    out |= {a.id, b.id}
    return out


def chip_label(e: Event, clash: bool, show_centre: bool = True) -> str:
    st_ = Status(e.status)
    t = fmt_time(e.start_time) or "All day"
    code = (e.centre.code if e.centre else "NAT") if show_centre else ""
    title = esc(e.title[:26] + ("…" if len(e.title) > 26 else ""))
    txt = f"{e.category.icon} {t} {code} {title}".replace("  ", " ")
    if st_ in (Status.CANCELLED, Status.REJECTED):
        txt = f"~~{txt}~~"
    elif st_ in (Status.DRAFT,):
        txt = f"_{txt}_"
    return f"{'⚠️' if clash else ''}{STATUS_ICONS[st_]} :{e.category.colour}[{txt}]"


def legend(categories) -> None:
    cats = " · ".join(f":{c.colour}[{c.icon} {c.name}]" for c in categories)
    stats = " · ".join(f"{STATUS_ICONS[s]} {STATUS_LABELS[s]}" for s in Status)
    st.caption(f"{cats}  \n{stats} · ⚠️ possible clash · _italic_ = draft · ~~struck~~ = cancelled/rejected")


@st.dialog("Event", width="large")
def quick_view(event_id: int, user: User) -> None:
    from ui.core import db
    with db() as s:
        e = ev_svc.get_event(s, user, event_id)
        actions = ev_svc.allowed_actions(user, e)
        _ = e.category, e.centre
    st.markdown(f"### {e.category.icon} {esc(e.title)}")
    st.markdown(f"{status_md(e.status)} &nbsp; {category_md(e)} &nbsp; `{e.code}`")
    c1, c2, c3 = st.columns(3)
    c1.markdown(f"**Centre**  \n{centre_label(e)}")
    c2.markdown(f"**When**  \n{fmt_date(e.event_date)} · {time_range(e)}")
    c3.markdown(f"**Responsible**  \n{esc(e.responsible_person) or '—'}")
    if e.venue:
        st.markdown(f"**Venue:** {esc(e.venue)}")
    if e.objective:
        st.markdown(f"**Objective:** {esc(e.objective)}")
    if e.last_review_comment:
        st.info(f"Head Office comment: {e.last_review_comment}")
    flags = ev_svc.risk_flags(e)
    if flags:
        st.warning(" · ".join(flags))
    if is_ho(user) and Action.APPROVE in actions:
        comment = st.text_input("Comment (required to return or reject)", key=f"qv_c_{e.id}")
        b1, b2, b3 = st.columns(3)
        if b1.button("Approve", type="primary", key=f"qv_a_{e.id}", width="stretch"):
            run(lambda s: ev_svc.transition(s, user, e.id, Action.APPROVE, comment, e.version), f"{e.code} approved")
        if b2.button("Return", key=f"qv_r_{e.id}", width="stretch"):
            run(lambda s: ev_svc.transition(s, user, e.id, Action.RETURN, comment, e.version), f"{e.code} returned")
        if b3.button("Reject", key=f"qv_x_{e.id}", width="stretch"):
            run(lambda s: ev_svc.transition(s, user, e.id, Action.REJECT, comment, e.version), f"{e.code} rejected")
    if st.button("Open full details →", key=f"qv_open_{e.id}"):
        open_event(e.id)


def month_view(events: list[Event], year: int, month: int, user: User, key: str, show_centre: bool = True) -> None:
    by_day = defaultdict(list)
    for e in events:
        by_day[e.event_date].append(e)
    clashes = conflict_ids(events)
    today = today_ist()
    head = st.columns(7, gap="small")
    for i, d in enumerate(WEEKDAYS):
        head[i].markdown(f"<div style='text-align:center;font-weight:600;opacity:.7'>{d}</div>",
                         unsafe_allow_html=True)
    for week in month_grid(year, month):
        cols = st.columns(7, gap="small")
        for i, d in enumerate(week):
            with cols[i]:
                with st.container(border=True, height=190 if d else 60):
                    if d is None:
                        continue
                    day_events = by_day.get(d, [])
                    style = "background:#1c83e1;color:white;border-radius:10px;padding:0 6px" if d == today else ""
                    count = f" <span style='opacity:.6;font-size:.8em'>· {len(day_events)}</span>" if day_events else ""
                    st.markdown(f"<span style='font-weight:700;{style}'>{d.day}</span>{count}",
                                unsafe_allow_html=True)
                    for e in day_events[:MAX_PER_CELL]:
                        if st.button(chip_label(e, e.id in clashes, show_centre), key=f"{key}_{e.id}",
                                     type="tertiary", help=f"{centre_label(e)} · {e.title} · {time_range(e)} · "
                                                           f"{STATUS_LABELS[Status(e.status)]}"):
                            quick_view(e.id, user)
                    if len(day_events) > MAX_PER_CELL:
                        st.button(f"+{len(day_events) - MAX_PER_CELL} more", key=f"{key}_more_{d}",
                                  type="tertiary", on_click=_to_agenda, args=(key, d))


def _to_agenda(key: str, d: date) -> None:
    st.session_state[f"{key}_view"] = "Day agenda"
    st.session_state[f"{key}_day"] = d


def agenda_view(events: list[Event], day: date, user: User, key: str) -> None:
    todays = [e for e in events if e.event_date == day]
    clashes = conflict_ids(todays)
    st.subheader(f"{day.strftime('%A')}, {fmt_date(day)}")
    if not todays:
        st.info("No events on this day.", icon=":material/event_available:")
        return
    by_centre = defaultdict(list)
    for e in todays:
        by_centre[centre_label(e)].append(e)
    for centre, evs in sorted(by_centre.items()):
        st.markdown(f"**{centre}**")
        for e in evs:
            with st.container(border=True):
                c1, c2, c3 = st.columns([1.2, 4, 1.4], vertical_alignment="center")
                c1.markdown(f"**{time_range(e)}**")
                c2.markdown(f"{'⚠️ ' if e.id in clashes else ''}{category_md(e)} · **{esc(e.title)}**  \n"
                            f"{esc(e.responsible_person) or '—'} · {esc(e.venue) or ''}")
                c3.markdown(status_md(e.status))
                if c3.button("Open", key=f"{key}_ag_{e.id}"):
                    quick_view(e.id, user)


def list_view(events: list[Event], user: User, key: str) -> None:
    if not events:
        st.info("No events for these filters.", icon=":material/event_busy:")
        return
    clashes = conflict_ids(events)
    current = None
    for e in events:
        if e.event_date != current:
            current = e.event_date
            st.markdown(f"#### {e.event_date.strftime('%a')} {fmt_date(e.event_date)}")
        c1, c2, c3, c4 = st.columns([1.1, 1.3, 4, 1.6], vertical_alignment="center")
        c1.markdown(time_range(e))
        c2.markdown(f"**{centre_label(e)}**")
        c3.markdown(f"{'⚠️ ' if e.id in clashes else ''}{category_md(e)} · {esc(e.title)}")
        if c4.button(f"{STATUS_ICONS[Status(e.status)]} {approval_status(e.status)}", key=f"{key}_li_{e.id}",
                     width="stretch"):
            quick_view(e.id, user)


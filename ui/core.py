"""Shared Streamlit plumbing: database handle, signed-in user, error handling, formatting."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

import streamlit as st
from sqlalchemy.orm import Session

from jgevents.config import database_url
from jgevents.db.migrate import upgrade_database
from jgevents.db.models import Event, User
from jgevents.db.session import session_scope
from jgevents.domain.constants import STATUS_ICONS, STATUS_LABELS, Status
from jgevents.services import auth
from jgevents.services.errors import ServiceError, ValidationError
from jgevents.utils.dates import add_months, fmt_time, now_utc, today_ist

SESSION_HOURS = 10

STATUS_COLOURS = {
    Status.DRAFT: "gray", Status.SUBMITTED: "blue", Status.RETURNED: "orange", Status.APPROVED: "green",
    Status.REJECTED: "red", Status.IN_PROGRESS: "violet", Status.COMPLETED: "green", Status.CANCELLED: "gray",
}


@st.cache_resource(show_spinner="Preparing database…")
def _init_db(url: str) -> str:
    upgrade_database(url)
    return url


def db_url() -> str:
    return _init_db(database_url())


@contextmanager
def db() -> Iterator[Session]:
    with session_scope(db_url()) as s:
        yield s


# ---------------------------------------------------------------- signed-in user

def current_user() -> User | None:
    uid = st.session_state.get("uid")
    if not uid:
        return None
    started = st.session_state.get("login_at")
    if started and now_utc() - started > timedelta(hours=SESSION_HOURS):
        logout("Your session expired. Please sign in again.")
        return None
    with db() as s:
        user = auth.load_active_user(s, uid, started)
        if user is None:
            logout("You have been signed out (account changed or deactivated).")
            return None
        _ = user.centres  # load before the session closes
        return user


def logout(message: str | None = None) -> None:
    for k in list(st.session_state.keys()):
        del st.session_state[k]
    if message:
        st.session_state["flash"] = ("info", message)


def me() -> User:
    return st.session_state["_user"]


# ---------------------------------------------------------------- feedback

def flash(kind: str, message: str) -> None:
    st.session_state.setdefault("flashes", []).append((kind, message))


def show_flashes() -> None:
    for kind, msg in st.session_state.pop("flashes", []):
        getattr(st, kind if kind in ("success", "info", "warning", "error") else "info")(msg)
    if "flash" in st.session_state:
        kind, msg = st.session_state.pop("flash")
        getattr(st, kind)(msg)


def run(fn: Callable[[Session], Any], success: str | None = None, rerun: bool = True) -> Any:
    """Execute a service call in its own transaction and surface errors nicely."""
    try:
        with db() as s:
            result = fn(s)
    except ValidationError as exc:
        st.error("\n".join(f"- {e}" for e in exc.errors) if len(exc.errors) > 1 else exc.errors[0])
        return None
    except ServiceError as exc:
        st.error(str(exc))
        return None
    if success:
        flash("success", success)
    if rerun:
        st.rerun()
    return result


# ---------------------------------------------------------------- navigation helpers

def open_event(event_id: int) -> None:
    st.session_state["event_id"] = event_id
    st.query_params["id"] = str(event_id)
    st.switch_page(st.session_state["_pages"]["event"])


def goto(page_key: str) -> None:
    st.switch_page(st.session_state["_pages"][page_key])


def month_picker(prefix: str, default: tuple[int, int] | None = None) -> tuple[int, int]:
    t = today_ist()
    ky, km = f"{prefix}_y", f"{prefix}_m"
    if ky not in st.session_state:
        st.session_state[ky], st.session_state[km] = default or (t.year, t.month)

    def shift(delta: int) -> None:
        st.session_state[ky], st.session_state[km] = add_months(st.session_state[ky], st.session_state[km], delta)

    def today() -> None:
        st.session_state[ky], st.session_state[km] = t.year, t.month

    c1, c2, c3, c4, c5 = st.columns([1, 1, 3, 2, 1], vertical_alignment="bottom")
    c1.button("◀", key=f"{prefix}_prev", on_click=shift, args=(-1,), help="Previous month", width="stretch")
    c2.button("Today", key=f"{prefix}_today", on_click=today, width="stretch")
    months = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
              "November", "December"]
    c3.selectbox("Month", range(1, 13), format_func=lambda m: months[m - 1], key=km, label_visibility="collapsed")
    years = list(range(min(2024, st.session_state[ky]), max(t.year + 3, st.session_state[ky] + 1)))
    c4.selectbox("Year", years, key=ky, label_visibility="collapsed")
    c5.button("▶", key=f"{prefix}_next", on_click=shift, args=(1,), help="Next month", width="stretch")
    return st.session_state[ky], st.session_state[km]


# ---------------------------------------------------------------- formatting

def status_md(status: str) -> str:
    s = Status(status)
    return f":{STATUS_COLOURS[s]}-badge[{STATUS_ICONS[s]} {STATUS_LABELS[s]}]"


def category_md(ev: Event) -> str:
    return f":{ev.category.colour}[{ev.category.icon} {ev.category.name}]"


def time_range(ev: Event) -> str:
    if ev.start_time and ev.end_time:
        return f"{fmt_time(ev.start_time)}–{fmt_time(ev.end_time)}"
    return fmt_time(ev.start_time) or "All day"


def centre_label(ev: Event) -> str:
    return ev.centre.name if ev.centre else "🇮🇳 National"


def esc(text: str | None) -> str:
    """Escape characters Streamlit markdown would interpret."""
    if not text:
        return ""
    for ch in ("\\", "[", "]", "*", "_", "`", "$", "~", "#", ":"):
        text = text.replace(ch, "\\" + ch)
    return text


def page_header(title: str, caption: str | None = None) -> None:
    st.title(title)
    if caption:
        st.caption(caption)
    show_flashes()


def stamp() -> datetime:
    return now_utc()

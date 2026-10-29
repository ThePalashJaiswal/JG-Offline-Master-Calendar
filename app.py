"""Master Events Tracker for JG Offline – Streamlit entry point."""
from __future__ import annotations

import streamlit as st

st.set_page_config(page_title="JG Master Events Tracker", page_icon="📅", layout="wide",
                   initial_sidebar_state="expanded")

from jgevents.domain.constants import ROLE_LABELS, Role  # noqa: E402
from jgevents.services.permissions import is_ho  # noqa: E402
from ui import core  # noqa: E402
from ui.views import (  # noqa: E402
    admin_views,
    auth_view,
    calendar_view,
    dashboards,
    event_view,
    events_views,
    planning_view,
    readiness_views,
    reports_view,
)

st.markdown("""<style>
.block-container{padding-top:2.2rem;padding-bottom:3rem}
div[data-testid="stVerticalBlockBorderWrapper"] button[kind="tertiary"]{padding:0;min-height:0;line-height:1.25;
  text-align:left;justify-content:flex-start;font-size:.80rem;width:100%}
div[data-testid="stVerticalBlockBorderWrapper"] button[kind="tertiary"] p{font-size:.80rem}
</style>""", unsafe_allow_html=True)

try:
    core.db_url()
except RuntimeError as exc:  # missing DATABASE_URL
    st.error(str(exc))
    st.stop()
except Exception as exc:  # cannot reach / log in to the database: show a safe diagnosis
    from sqlalchemy.engine import make_url

    from jgevents.config import database_url
    try:
        u = make_url(database_url())
        target = f"user `{u.username}` at `{u.host}:{u.port}` / database `{u.database}`"
        secret = u.password or ""
    except Exception:
        target, secret = "an unparseable DATABASE_URL (check for @ # / ? : % in the password)", ""
    reason = str(getattr(exc, "orig", None) or exc).strip().splitlines()[0][:400]
    if secret:
        reason = reason.replace(secret, "•••")
    st.error(f"**Cannot connect to the database.** Tried {target}.\n\nDatabase says: `{reason}`")
    st.caption("Common fixes: use Supabase's Session pooler string (port 5432) copied from the Connect button; "
               "remove [ ] around the password; use a password of letters and digits only. "
               "After editing Secrets, reboot the app.")
    st.stop()

user = core.current_user()
if user is None:
    auth_view.login_screen()
    st.stop()
if user.must_change_password:
    auth_view.forced_change_screen(user)
    st.stop()
st.session_state["_user"] = user


def account() -> None:
    core.page_header("My account")
    st.markdown(f"**{user.full_name}** · {ROLE_LABELS[Role(user.role)]} · `{user.username}`")
    if user.centres:
        st.caption("Centres: " + ", ".join(c.name for c in user.centres))
    auth_view.change_password_form(user, forced=False)


role = Role(user.role)
P = {
    "home": st.Page(dashboards.centre_home if role == Role.CENTRE_HEAD else dashboards.ho_home,
                    title="My centre" if role == Role.CENTRE_HEAD else "Dashboard", icon=":material/dashboard:",
                    url_path="home", default=True),
    "calendar": st.Page(calendar_view.render, title="Master calendar", icon=":material/calendar_month:",
                        url_path="calendar"),
    "planning": st.Page(planning_view.render, title="Monthly planning", icon=":material/edit_calendar:",
                        url_path="planning"),
    "new": st.Page(events_views.new_event, title="New event", icon=":material/add_circle:", url_path="new"),
    "events": st.Page(events_views.events_list, title="All events", icon=":material/list_alt:", url_path="events"),
    "event": st.Page(event_view.render, title="Event details", icon=":material/event_note:", url_path="event"),
    "approvals": st.Page(events_views.approvals, title="Approvals", icon=":material/approval:", url_path="approvals"),
    "national": st.Page(events_views.national, title="National events", icon=":material/public:", url_path="national"),
    "reports": st.Page(reports_view.render, title="Reports & exports", icon=":material/download:", url_path="reports"),
    "centres": st.Page(admin_views.centres, title="Centres", icon=":material/apartment:", url_path="centres"),
    "users": st.Page(admin_views.users, title="Users & access", icon=":material/group:", url_path="users"),
    "config": st.Page(admin_views.configuration, title="Rules & categories", icon=":material/tune:", url_path="config"),
    "audit": st.Page(admin_views.audit_log, title="Audit log", icon=":material/history:", url_path="audit"),
    "account": st.Page(account, title="My account", icon=":material/person:", url_path="account"),
    "tasks": st.Page(readiness_views.tasks_page, title="Preparation tasks", icon=":material/task_alt:",
                     url_path="tasks"),
    "readiness": st.Page(readiness_views.readiness_board, title="Readiness board", icon=":material/fact_check:",
                         url_path="readiness"),
    "playbooks": st.Page(readiness_views.playbooks_page, title="Playbooks", icon=":material/checklist:",
                         url_path="playbooks"),
}
st.session_state["_pages"] = P

# Pages a role cannot use are never registered, so their URLs do not resolve.
# (The services enforce the same rules independently.)
if role == Role.CENTRE_HEAD:
    nav = {"Plan & track": [P["home"], P["tasks"], P["planning"], P["new"], P["calendar"], P["events"], P["event"],
                            P["reports"]],
           "": [P["account"]]}
elif role == Role.VIEWER:
    nav = {"Overview": [P["home"], P["readiness"], P["tasks"], P["calendar"], P["events"], P["event"], P["reports"]],
           "": [P["account"]]}
else:
    nav = {"Head Office": [P["home"], P["readiness"], P["approvals"], P["tasks"], P["calendar"], P["planning"],
                           P["events"], P["event"], P["national"], P["new"], P["reports"]],
           "Administration": [P["centres"], P["users"], P["playbooks"], P["config"], P["audit"]], "": [P["account"]]}

page = st.navigation(nav)

with st.sidebar:
    st.markdown(f"**{user.full_name}**  \n{ROLE_LABELS[role]}"
                + (f" · {', '.join(c.code for c in user.centres)}" if user.centres and not is_ho(user) else ""))
    if st.button("Sign out", icon=":material/logout:", width="stretch"):
        core.logout("Signed out.")
        st.rerun()
    st.caption("Master Events Tracker · JG Offline")

page.run()

"""Renders a single view for AppTest smoke tests (callable pages cannot be switched to directly)."""
import streamlit as st

from jgevents.services import auth
from ui import core
from ui.views import admin_views, calendar_view, dashboards, event_view, events_views, planning_view, reports_view

VIEWS = {
    "centre_home": dashboards.centre_home, "ho_home": dashboards.ho_home, "calendar": calendar_view.render,
    "planning": planning_view.render, "events": events_views.events_list, "new": events_views.new_event,
    "approvals": events_views.approvals, "national": events_views.national, "event": event_view.render,
    "reports": reports_view.render, "centres": admin_views.centres, "users": admin_views.users,
    "config": admin_views.configuration, "audit": admin_views.audit_log,
}

with core.db() as s:
    user = auth.load_active_user(s, st.session_state["uid"])
    _ = user.centres
st.session_state["_user"] = user
st.session_state.setdefault("_pages", {})
VIEWS[st.session_state["view"]]()

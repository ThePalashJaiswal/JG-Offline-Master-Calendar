"""Sign-in, first-run bootstrap and forced password change."""
from __future__ import annotations

import hmac

import streamlit as st

from jgevents.config import get_setting
from jgevents.services import auth, directory
from jgevents.services.errors import ServiceError, ValidationError
from jgevents.utils.dates import now_utc
from ui.core import db, show_flashes


def _brand() -> None:
    st.markdown("## 📅 Master Events Tracker")
    st.caption("Judiciary Gold · Offline Centres")


def login_screen() -> None:
    _, mid, _ = st.columns([1, 1.4, 1])
    with mid:
        _brand()
        show_flashes()
        with db() as s:
            empty = directory.count_users(s) == 0
        if empty:
            bootstrap_screen()
            return
        with st.form("login", border=True):
            username = st.text_input("Username", autocomplete="username")
            password = st.text_input("Password", type="password", autocomplete="current-password")
            ok = st.form_submit_button("Sign in", type="primary", width="stretch")
        if ok:
            try:
                with db() as s:
                    user = auth.authenticate(s, username, password)
                    uid = user.id
            except ServiceError as exc:
                st.error(str(exc))
                return
            st.session_state["uid"] = uid
            st.session_state["login_at"] = now_utc()
            st.rerun()
        st.caption("Forgot your password? Ask a Head Office admin to reset it.")


def bootstrap_screen() -> None:
    token = get_setting("BOOTSTRAP_TOKEN")
    st.info("No users exist yet. Create the first Super Admin.")
    if not token:
        st.warning("Set `BOOTSTRAP_TOKEN` in the app secrets to enable this screen, or run "
                   "`python manage.py create-superadmin` against the database.")
        return
    with st.form("bootstrap", border=True):
        given = st.text_input("Bootstrap token", type="password")
        username = st.text_input("Username")
        full_name = st.text_input("Full name")
        email = st.text_input("Email (optional)")
        pw = st.text_input("Password", type="password", help="At least 10 characters, letters plus digits/symbols")
        pw2 = st.text_input("Repeat password", type="password")
        go = st.form_submit_button("Create Super Admin", type="primary")
    if go:
        if not hmac.compare_digest(given.encode(), token.encode()):
            st.error("Invalid bootstrap token.")
            return
        if pw != pw2:
            st.error("Passwords do not match.")
            return
        try:
            with db() as s:
                directory.bootstrap_super_admin(s, username, full_name, pw, email or None)
        except ValidationError as exc:
            st.error("; ".join(exc.errors))
            return
        except ServiceError as exc:
            st.error(str(exc))
            return
        st.session_state["flash"] = ("success", "Super Admin created. Sign in now, then remove BOOTSTRAP_TOKEN "
                                                "from the secrets.")
        st.rerun()


def change_password_form(user, forced: bool) -> None:
    if forced:
        st.warning("Please set a new password before continuing.")
    with st.form("chpw", border=True, clear_on_submit=True):
        cur = st.text_input("Current password", type="password")
        new = st.text_input("New password", type="password", help="At least 10 characters, letters plus digits/symbols")
        new2 = st.text_input("Repeat new password", type="password")
        go = st.form_submit_button("Change password", type="primary")
    if go:
        if new != new2:
            st.error("New passwords do not match.")
            return
        try:
            with db() as s:
                auth.change_password(s, user, cur, new)
        except ValidationError as exc:
            st.error("; ".join(exc.errors))
            return
        st.session_state["login_at"] = now_utc()
        st.session_state["flash"] = ("success", "Password changed.")
        st.rerun()


def forced_change_screen(user) -> None:
    _, mid, _ = st.columns([1, 1.4, 1])
    with mid:
        _brand()
        st.markdown(f"Signed in as **{user.full_name}**")
        change_password_form(user, forced=True)

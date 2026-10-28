"""Head Office administration: centres, users, categories & rules, settings, audit log."""
from __future__ import annotations

import json
from datetime import timedelta

import pandas as pd
import streamlit as st

from jgevents.domain.constants import CATEGORY_COLOURS, ROLE_LABELS, Requirement, Role
from jgevents.services import audit, config_service, directory
from jgevents.services.permissions import is_super
from jgevents.services.recurrence import describe_rule
from jgevents.utils.dates import add_months, fmt_date, fmt_dt, month_label, now_utc, today_ist
from ui.core import db, me, page_header, run

# ---------------------------------------------------------------- centres


def centres() -> None:
    user = me()
    page_header("Centres", "Add, rename or deactivate centres. Deactivated centres keep their full history.")
    with db() as s:
        rows = directory.list_centres(s, user, include_inactive=True)
    st.dataframe(pd.DataFrame([{"Code": c.code, "Name": c.name, "City": c.city, "Active": c.is_active,
                                "Order": c.sort_order} for c in rows]), hide_index=True, width="stretch")
    c1, c2 = st.columns(2)
    with c1, st.form("centre_add", clear_on_submit=True):
        st.markdown("**Add centre**")
        name = st.text_input("Name", placeholder="e.g. Patna")
        code = st.text_input("Code", placeholder="e.g. PAT", max_chars=10)
        city = st.text_input("City")
        order = st.number_input("Display order", value=100, step=10)
        if st.form_submit_button("Add centre", type="primary"):
            run(lambda s: directory.create_centre(s, user, name, code, city or None, sort_order=int(order)),
                f"Centre {name} added.")
    with c2:
        st.markdown("**Edit centre**")
        if rows:
            cid = st.selectbox("Centre", [c.id for c in rows], format_func=lambda i: next(c.name for c in rows if c.id == i),
                               key="centre_edit_id")
            c = next(x for x in rows if x.id == cid)
            with st.form(f"centre_edit_{cid}"):
                name = st.text_input("Name", value=c.name)
                code = st.text_input("Code", value=c.code, max_chars=10)
                city = st.text_input("City", value=c.city or "")
                order = st.number_input("Display order", value=c.sort_order, step=10)
                active = st.checkbox("Active", value=c.is_active,
                                     help="Inactive centres disappear from planning but keep all history")
                if st.form_submit_button("Save", type="primary"):
                    run(lambda s: directory.update_centre(s, user, cid, name=name, code=code, city=city,
                                                          sort_order=int(order), is_active=active), "Centre saved.")


# ---------------------------------------------------------------- users

def users() -> None:
    user = me()
    page_header("Users & access", "Roles are enforced on the server for every action")
    with db() as s:
        people = directory.list_users(s, user)
        cs = directory.list_centres(s, user, include_inactive=True)
    cname = {c.id: c.name for c in cs}
    st.dataframe(pd.DataFrame([{
        "Username": u.username, "Name": u.full_name, "Role": ROLE_LABELS[Role(u.role)],
        "Centres": "All" if u.role in (Role.SUPER_ADMIN, Role.HO_ADMIN) or u.scope_all_centres
        else ", ".join(cname.get(c.id, "?") for c in u.centres),
        "Active": u.is_active, "Last sign-in": fmt_dt(u.last_login_at),
        "Must change password": u.must_change_password,
    } for u in people]), hide_index=True, width="stretch")
    roles = [r.value for r in Role if is_super(user) or r != Role.SUPER_ADMIN]
    tab_add, tab_edit = st.tabs(["Add user", "Edit / reset"])
    with tab_add, st.form("user_add", clear_on_submit=True):
        c1, c2 = st.columns(2)
        username = c1.text_input("Username", placeholder="firstname.lastname")
        full_name = c2.text_input("Full name")
        email = c1.text_input("Email")
        phone = c2.text_input("Phone")
        role = c1.selectbox("Role", roles, format_func=lambda r: ROLE_LABELS[Role(r)])
        centre_ids = c2.multiselect("Centres (Centre Heads / scoped viewers)", [c.id for c in cs],
                                    format_func=lambda i: cname[i])
        scope_all = st.checkbox("Viewer can see all centres")
        if st.form_submit_button("Create user", type="primary"):
            res = run(lambda s: directory.create_user(s, user, username, full_name, role, email=email or None,
                                                      phone=phone or None, centre_ids=centre_ids,
                                                      scope_all_centres=scope_all)[1], rerun=False)
            if res:
                st.success(f"User created. Temporary password: `{res}` – share it privately; "
                           "they must change it at first sign-in. It will not be shown again.")
    with tab_edit:
        editable = [u for u in people if is_super(user) or u.role != Role.SUPER_ADMIN]
        if not editable:
            return
        uid = st.selectbox("User", [u.id for u in editable], key="user_edit_id",
                           format_func=lambda i: next(f"{u.full_name} ({u.username})" for u in editable if u.id == i))
        u = next(x for x in editable if x.id == uid)
        with st.form(f"user_edit_{uid}"):
            c1, c2 = st.columns(2)
            full_name = c1.text_input("Full name", value=u.full_name)
            email = c2.text_input("Email", value=u.email or "")
            role = c1.selectbox("Role", roles, index=roles.index(u.role) if u.role in roles else 0,
                                format_func=lambda r: ROLE_LABELS[Role(r)])
            centre_ids = c2.multiselect("Centres", [c.id for c in cs], default=[c.id for c in u.centres],
                                        format_func=lambda i: cname[i])
            scope_all = st.checkbox("Viewer can see all centres", value=u.scope_all_centres)
            active = st.checkbox("Active", value=u.is_active,
                                 help="Deactivating signs the user out; their history is kept")
            if st.form_submit_button("Save user", type="primary"):
                run(lambda s: directory.update_user(s, user, uid, full_name=full_name, email=email or None, role=role,
                                                    centre_ids=centre_ids, scope_all_centres=scope_all,
                                                    is_active=active), "User saved.")
        if st.button("Reset password", key=f"reset_{uid}"):
            pw = run(lambda s: directory.reset_password(s, user, uid), rerun=False)
            if pw:
                st.success(f"Temporary password: `{pw}` – share privately; it will not be shown again.")


# ---------------------------------------------------------------- categories, requirements, rules, settings

def configuration() -> None:
    user = me()
    page_header("Planning rules & categories")
    t1, t2, t3, t4 = st.tabs(["Requirements", "Deadlines & settings", "Recurring events", "Categories"])
    with db() as s:
        cats = config_service.list_categories(s, include_inactive=True)
        cs = directory.list_centres(s, user)
        reqs = config_service.list_requirements(s)
        rules = config_service.list_rules(s)
        cycles = config_service.list_planning_cycles(s)
        settings = {k: config_service.get_setting(s, k) for k in
                    ("planning_deadline_day", "reapproval_on_material_change", "at_risk_window_days")}
    cname = {c.id: c.name for c in cs}

    with t1:
        st.caption("Network defaults apply to every centre unless a centre override exists. "
                   "Mandatory categories must be planned (or explained) before a centre can submit.")
        rows = []
        for r in reqs:
            cat = next((c for c in cats if c.id == r.category_id), None)
            if cat:
                rows.append({"Category": f"{cat.icon} {cat.name}", "Scope": cname.get(r.centre_id, "All centres")
                             if r.centre_id else "All centres (default)", "Requirement": r.requirement,
                             "Min per month": r.min_count})
        st.dataframe(pd.DataFrame(rows).sort_values(["Category", "Scope"]), hide_index=True, width="stretch")
        with st.form("req_form"):
            c1, c2, c3, c4 = st.columns(4)
            cat_id = c1.selectbox("Category", [c.id for c in cats], format_func=lambda i: next(c.name for c in cats if c.id == i))
            scope = c2.selectbox("Applies to", [None] + [c.id for c in cs],
                                 format_func=lambda i: "All centres (default)" if i is None else cname[i])
            req = c3.selectbox("Requirement", [r.value for r in Requirement] + ["inherit"],
                               format_func=lambda r: {"inherit": "Remove centre override"}.get(r, r.replace("_", " ").title()))
            n = c4.number_input("Min per month", min_value=0, value=1, step=1)
            if st.form_submit_button("Save requirement", type="primary"):
                run(lambda s: config_service.set_requirement(s, user, cat_id, scope, req, int(n)), "Requirement saved.")

    with t2:
        with st.form("settings_form"):
            day = st.number_input("Default planning deadline: day of the previous month", 1, 28,
                                  value=int(settings["planning_deadline_day"] or 25))
            reapp = st.checkbox("Require re-approval when an approved event's title/date/time/venue changes",
                                value=bool(settings["reapproval_on_material_change"]))
            win = st.number_input("At-risk window (days)", 1, 30, value=int(settings["at_risk_window_days"] or 7))
            if st.form_submit_button("Save settings", type="primary"):
                def do(s):
                    config_service.set_setting(s, user, "planning_deadline_day", int(day))
                    config_service.set_setting(s, user, "reapproval_on_material_change", bool(reapp))
                    config_service.set_setting(s, user, "at_risk_window_days", int(win))
                run(do, "Settings saved.")
        st.markdown("**Month-specific deadline overrides**")
        if cycles:
            st.dataframe(pd.DataFrame([{"Month": month_label(c.year, c.month), "Deadline": fmt_date(c.deadline),
                                        "Notes": c.notes or ""} for c in cycles]), hide_index=True, width="stretch")
        with st.form("cycle_form"):
            t = today_ist()
            ny, nm = add_months(t.year, t.month, 1)
            c1, c2, c3 = st.columns(3)
            y = c1.number_input("Year", 2024, 2100, value=ny)
            m = c2.number_input("Month", 1, 12, value=nm)
            d = c3.date_input("Deadline", value=t + timedelta(days=7), format="DD-MM-YYYY")
            notes = st.text_input("Notes")
            if st.form_submit_button("Set deadline", type="primary"):
                run(lambda s: config_service.set_planning_deadline(s, user, int(y), int(m), d, notes or None),
                    "Deadline saved.")

    with t3:
        st.caption("Rules only *propose* events. Centres (or HO) review proposals in the planning workspace "
                   "and they are added as drafts that still need submission and approval.")
        for r in rules:
            cat = next((c for c in cats if c.id == r.category_id), None)
            st.markdown(f"- **{r.name}** · {cat.name if cat else '?'} · {describe_rule(r)} · "
                        f"{r.start_time:%H:%M}–{r.end_time:%H:%M} · "
                        f"{'all centres' if not r.centre_ids else ', '.join(cname.get(i, '?') for i in r.centre_ids)}"
                        f" · {'active' if r.is_active else 'inactive'}" if r.start_time and r.end_time else
                        f"- **{r.name}** · {describe_rule(r)}")
        options = [None] + [r.id for r in rules]
        rid = st.selectbox("Edit rule", options, format_func=lambda i: "➕ New rule" if i is None
                           else next(r.name for r in rules if r.id == i), key="rule_pick")
        r = next((x for x in rules if x.id == rid), None)
        with st.form(f"rule_form_{rid}"):
            c1, c2 = st.columns(2)
            name = c1.text_input("Rule name", value=r.name if r else "")
            cat_id = c2.selectbox("Category", [c.id for c in cats], index=[c.id for c in cats].index(r.category_id) if r else 0,
                                  format_func=lambda i: next(c.name for c in cats if c.id == i))
            types = ["last_weekday", "nth_weekday", "fixed_day"]
            rtype = c1.selectbox("Pattern", types, index=types.index(r.rule_type) if r else 0,
                                 format_func={"last_weekday": "Last <weekday> of month", "nth_weekday": "Nth <weekday> of month",
                                              "fixed_day": "Fixed day of month"}.get)
            wd = c2.selectbox("Weekday", list(range(7)), index=r.weekday if r and r.weekday is not None else 6,
                              format_func=lambda i: ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][i])
            nth = c1.number_input("Occurrence (for Nth weekday)", 1, 5, value=r.nth or 1 if r else 1)
            dom = c2.number_input("Day of month (for fixed day)", 1, 31, value=r.day_of_month or 28 if r else 28)
            st_ = c1.time_input("Start", value=r.start_time if r else None)
            en_ = c2.time_input("End", value=r.end_time if r else None)
            title = st.text_input("Title template", value=r.title_template if r else "{centre} – {month_name} {year}",
                                  help="Placeholders: {centre} {month_name} {year}")
            only = st.multiselect("Only these centres (empty = all)", [c.id for c in cs],
                                  default=[i for i in (r.centre_ids or []) if i in cname] if r else [],
                                  format_func=lambda i: cname[i])
            venue = st.text_input("Default venue", value=(r.defaults or {}).get("venue", "") if r else "")
            active = st.checkbox("Active", value=r.is_active if r else True)
            if st.form_submit_button("Save rule", type="primary"):
                run(lambda s: config_service.save_rule(
                    s, user, rid, category_id=cat_id, name=name, rule_type=rtype, weekday=wd, nth=int(nth),
                    day_of_month=int(dom), start_time=st_, end_time=en_, title_template=title, centre_ids=only,
                    defaults={"venue": venue} if venue else {}, is_active=active), "Rule saved.")

    with t4:
        st.caption("Categories drive the forms. Field definitions are JSON – see docs/CATEGORIES.md for the format. "
                   "Changes apply immediately to new and edited events; stored data is kept.")
        options = [None] + [c.id for c in cats]
        cid = st.selectbox("Category", options, key="cat_pick",
                           format_func=lambda i: "➕ New category" if i is None else
                           next(f"{c.icon} {c.name}{'' if c.is_active else ' (inactive)'}" for c in cats if c.id == i))
        c = next((x for x in cats if x.id == cid), None)
        with st.form(f"cat_form_{cid}"):
            c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
            name = c1.text_input("Name", value=c.name if c else "")
            key = c2.text_input("Key", value=c.key if c else "", disabled=c is not None, help="Permanent identifier")
            icon = c3.text_input("Icon", value=c.icon if c else "📌", max_chars=4)
            colour = c4.selectbox("Colour", CATEGORY_COLOURS, index=CATEGORY_COLOURS.index(c.colour) if c else 5)
            c1, c2, c3, c4 = st.columns(4)
            group = c1.selectbox("Group", ["academic", "marketing", "engagement", "assessment"],
                                 index=["academic", "marketing", "engagement", "assessment"].index(c.group) if c else 0)
            policy = c2.selectbox("Same-slot clash", ["warn", "block", "none"],
                                  index=["warn", "block", "none"].index(c.conflict_policy) if c else 0)
            occupies = c3.checkbox("Uses a time slot / room", value=c.occupies_slot if c else True)
            informal = c4.checkbox("Informal engagement", value=c.is_informal if c else False)
            national = c3.checkbox("Supports national parent", value=c.supports_national if c else False)
            active = c4.checkbox("Active", value=c.is_active if c else True)
            desc = st.text_input("Description", value=(c.description or "") if c else "")
            subtypes = st.text_input("Subtype options (comma separated)", value=", ".join(c.subtype_options) if c else "")
            checklist = st.text_area("Default checklist (one per line)", value="\n".join(c.default_checklist) if c else "")
            fields = st.text_area("Field schema (JSON)", value=json.dumps(c.field_schema if c else [], indent=2,
                                                                         ensure_ascii=False), height=260)
            overrides = st.text_area("Common-field overrides (JSON)", value=json.dumps(
                c.common_overrides if c else {}, indent=2, ensure_ascii=False), height=160)
            if st.form_submit_button("Save category", type="primary"):
                try:
                    fs, ov = json.loads(fields or "[]"), json.loads(overrides or "{}")
                except json.JSONDecodeError as exc:
                    st.error(f"Invalid JSON: {exc}")
                else:
                    vals = dict(name=name, icon=icon or "📌", colour=colour, group=group, conflict_policy=policy,
                                occupies_slot=occupies, is_informal=informal, supports_national=national,
                                is_active=active, description=desc or None,
                                subtype_options=[x.strip() for x in subtypes.split(",") if x.strip()],
                                default_checklist=[x.strip() for x in checklist.splitlines() if x.strip()],
                                field_schema=fs, common_overrides=ov)
                    if c:
                        run(lambda s: config_service.update_category(s, user, c.id, **vals), "Category saved.")
                    else:
                        run(lambda s: config_service.create_category(s, user, key, **vals), "Category created.")


# ---------------------------------------------------------------- audit

def audit_log() -> None:
    user = me()
    page_header("Audit log", "Who did what, when (IST)")
    c1, c2 = st.columns(2)
    days = c1.selectbox("Period", [7, 30, 90, 365], format_func=lambda d: f"Last {d} days", key="audit_days")
    q = c2.text_input("Contains", key="audit_q")
    with db() as s:
        logs = audit.list_logs(s, user, since=now_utc() - timedelta(days=days), limit=3000)
    rows = [{"When (IST)": fmt_dt(lg.created_at), "By": lg.actor.full_name if lg.actor else "system",
             "Action": lg.action, "Entity": f"{lg.entity_type} {lg.entity_id or ''}", "Summary": lg.summary}
            for lg in logs if not q or q.lower() in lg.summary.lower() or q.lower() in lg.action]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", height=600)
    if rows:
        st.download_button("⬇ CSV", pd.DataFrame(rows).to_csv(index=False).encode("utf-8-sig"), "jg_audit_log.csv",
                           "text/csv", on_click="ignore")

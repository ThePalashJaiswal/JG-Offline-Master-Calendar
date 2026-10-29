"""Streamlit smoke tests: every page renders for every role without exceptions,
and the real login flow works end to end."""
import os

import pytest
from sqlalchemy import select
from streamlit.testing.v1 import AppTest

from jgevents import config
from jgevents.db.models import Event, User
from jgevents.db.session import session_scope
from jgevents.utils.dates import now_utc

ROOT = os.path.dirname(os.path.dirname(__file__))


@pytest.fixture
def demo(db_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_url)
    config.database_url.cache_clear()
    import manage
    manage.cmd_seed_demo(db_url)
    with session_scope(db_url) as s:
        users = {u.username: u.id for u in s.scalars(select(User))}
        event_ids = [e.id for e in s.scalars(select(Event))]
    yield users, event_ids
    config.database_url.cache_clear()


def _view(uid, view, **state):
    at = AppTest.from_file(os.path.join(ROOT, "tests", "ui_harness.py"), default_timeout=60)
    at.session_state["uid"] = uid
    at.session_state["login_at"] = now_utc()
    at.session_state["view"] = view
    for k, v in state.items():
        at.session_state[k] = v
    return at.run()


HO_VIEWS = ["ho_home", "calendar", "planning", "events", "new", "approvals", "national", "event", "reports",
            "centres", "users", "config", "audit", "tasks", "readiness", "playbooks"]
CH_VIEWS = ["centre_home", "calendar", "planning", "events", "new", "event", "reports", "tasks"]
VIEWER_VIEWS = ["ho_home", "calendar", "events", "event", "reports", "tasks", "readiness"]


@pytest.mark.parametrize("username,views", [("admin", HO_VIEWS), ("ho.admin", HO_VIEWS),
                                            ("ch.bpl", CH_VIEWS), ("viewer", VIEWER_VIEWS)])
def test_pages_render_for_role(demo, username, views):
    users, event_ids = demo
    for view in views:
        extra = {"event_id": event_ids[0]} if view == "event" else {}
        at = _view(users[username], view, **extra)
        assert not at.exception, f"{username}/{view}: {at.exception}"
        assert at.title, f"{username}/{view} rendered no title"


def test_centre_head_event_page_denies_other_centre(demo):
    users, _ = demo
    with session_scope(os.environ["DATABASE_URL"]) as s:
        other = s.scalar(select(Event).where(Event.centre_id.is_not(None), Event.title.like("%Delhi%")))
        oid = other.id
    at = _view(users["ch.bpl"], "event", event_id=oid)
    assert not at.exception
    assert any("do not have access" in e.value for e in at.error)


def test_calendar_month_navigation(demo):
    users, _ = demo
    at = _view(users["ho.admin"], "calendar")
    y0, m0 = at.session_state["cal_y"], at.session_state["cal_m"]
    at.button(key="cal_next").click().run()
    assert (at.session_state["cal_y"] * 12 + at.session_state["cal_m"]) == y0 * 12 + m0 + 1
    for _ in range(14):
        at.button(key="cal_prev").click().run()
    assert not at.exception
    assert (at.session_state["cal_y"] * 12 + at.session_state["cal_m"]) == y0 * 12 + m0 - 13  # crosses a year


def test_login_flow_through_real_app(demo):
    at = AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=60).run()
    assert not at.exception
    at.text_input[0].input("ch.bpl")
    at.text_input[1].input("wrong-password")
    at.button[0].click().run()
    assert any("Invalid username or password" in e.value for e in at.error)
    at.text_input[0].input("ch.bpl")
    at.text_input[1].input("Centre@BPL123")
    at.button[0].click().run()
    assert not at.exception, at.exception
    assert any("Bhopal" in t.value for t in at.title)


def test_ui_create_submit_and_approve_flow(demo):
    """Centre Head fills the batch-launch form in the planning workspace, submits the
    plan, and Head Office approves it from the approvals queue."""
    from datetime import time

    from jgevents.utils.dates import add_months, today_ist
    users, _ = demo
    at = _view(users["ch.bpl"], "planning")
    assert not at.exception
    y, m = at.session_state["plan_y"], at.session_state["plan_m"]
    ny, nm = add_months(today_ist().year, today_ist().month, 1)
    assert (y, m) == (ny, nm)  # planning defaults to the upcoming month
    with session_scope(os.environ["DATABASE_URL"]) as s:
        from jgevents.db.models import Centre
        cid = s.scalar(select(Centre).where(Centre.code == "BPL")).id
    p = f"plan_new_batch_launch_{cid}_{y}_{m}"
    from datetime import date
    at.text_input(key=f"{p}_title").input("UI batch launch")
    at.date_input(key=f"{p}_date").set_value(date(y, m, 12))
    at.time_input(key=f"{p}_start").set_value(time(9, 0))
    at.time_input(key=f"{p}_end").set_value(time(11, 0))
    at.text_input(key=f"{p}_resp").input("Bhopal CH")
    at.number_input(key=f"{p}_exp").set_value(35)
    at.text_input(key=f"{p}_aud").input("LLB graduates")
    at.text_input(key=f"{p}_venue").input("Room 2")
    at.text_input(key=f"{p}_d_batch_name").input("CJ-UI-01")
    at.text_input(key=f"{p}_d_subject").input("CPC")
    at.selectbox(key=f"{p}_d_programme").select("Foundation")
    next(b for b in at.button if b.label == "Save draft").click().run()
    assert not at.exception, at.exception
    # v2: the preparation schedule is shown before saving
    assert any("Preparation schedule" in m.value for m in at.markdown)
    next(b for b in at.button if b.label == "Confirm and save").click().run()
    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
    with session_scope(os.environ["DATABASE_URL"]) as s:
        ev = s.scalar(select(Event).where(Event.title == "UI batch launch"))
        assert ev is not None and ev.status == "draft" and ev.details["programme"] == "Foundation"
        eid = ev.id
    # the demo national mock record for Bhopal is incomplete, so the UI must block submission ...
    at.text_area(key=f"plan_note_{cid}_{y}_{m}").input("Scholarship test to be added after calendar review")
    at.button(key=f"plan_submit_{cid}_{y}_{m}").click().run()
    assert any("participation plan is required" in e.value for e in at.error)
    # ... until the centre completes it
    from jgevents.services import events as ev_svc
    with session_scope(os.environ["DATABASE_URL"]) as s:
        ch = s.get(User, users["ch.bpl"])
        child = s.scalar(select(Event).where(Event.centre_id == cid, Event.parent_event_id.is_not(None)))
        ev_svc.update_event(s, ch, child.id, child.version, common=dict(
            title=child.title, event_date=child.event_date, start_time=child.start_time, end_time=child.end_time,
            responsible_person="Bhopal CH", venue="Hall A", expected_participation=90),
            details={"participation_plan": "Two halls, 45 each"})
    at = _view(users["ch.bpl"], "planning")
    # submit the plan (scholarship test is mandatory by default -> note required)
    at.text_area(key=f"plan_note_{cid}_{y}_{m}").input("Scholarship test to be added after calendar review")
    at.button(key=f"plan_submit_{cid}_{y}_{m}").click().run()
    assert not at.exception and not at.error, [e.value for e in at.error]
    with session_scope(os.environ["DATABASE_URL"]) as s:
        assert s.get(Event, eid).status == "submitted"
    # Head Office approves
    ho = _view(users["ho.admin"], "approvals")
    ho.button(key=f"appr_a_{eid}").click().run()
    assert not ho.exception and not ho.error, [e.value for e in ho.error]
    with session_scope(os.environ["DATABASE_URL"]) as s:
        assert s.get(Event, eid).status == "approved"



def test_ui_readiness_pages_show_demo_tasks(demo):
    """Event page shows a Readiness tab with tasks; calendar shows task chips; board lists events."""
    users, _ = demo
    with session_scope(os.environ["DATABASE_URL"]) as s:
        launch = s.scalar(select(Event).where(Event.title.like("Civil Judge Foundation%")))
        eid = launch.id
    at = _view(users["ch.del"] if "Delhi" in launch.title else users["admin"], "event", event_id=eid)
    assert not at.exception, at.exception
    assert any(t.label.startswith("Readiness") for t in at.tabs)
    ho = _view(users["ho.admin"], "readiness")
    assert not ho.exception, ho.exception
    assert any("Events with a playbook" in b.label for b in ho.button)
    ho.button(key="rb_m_events").click().run()
    assert not ho.exception
    cal = _view(users["ho.admin"], "calendar")
    assert not cal.exception
    assert any("task(s)" in m.value for m in cal.markdown)
    pb = _view(users["admin"], "playbooks")
    assert not pb.exception and any("minimum lead time" in m.value for m in pb.markdown)


def test_ui_centre_head_cannot_book_launch_inside_lead_time(demo):
    from datetime import date, time, timedelta

    from jgevents.utils.dates import today_ist
    users, _ = demo
    with session_scope(os.environ["DATABASE_URL"]) as s:
        from jgevents.db.models import Centre
        cid = s.scalar(select(Centre).where(Centre.code == "BPL")).id
    at = _view(users["ch.bpl"], "new")
    p = f"new_batch_launch_{cid}"
    d = today_ist() + timedelta(days=3)
    at.text_input(key=f"{p}_title").input("Too soon")
    at.date_input(key=f"{p}_date").set_value(d)
    at.time_input(key=f"{p}_start").set_value(time(9, 0))
    next(b for b in at.button if b.label == "Save draft").click().run()
    assert not at.exception
    assert any("at least 10 days" in e.value for e in at.error)
    assert not any(b.label == "Confirm and save" for b in at.button)
    _ = date


def test_ui_centre_head_marks_task_done_from_event_page(demo):
    users, _ = demo
    with session_scope(os.environ["DATABASE_URL"]) as s:
        launch = s.scalar(select(Event).where(Event.title == "Civil Judge Foundation – Delhi"))
        eid = launch.id
    at = _view(users["ch.del"], "event", event_id=eid)
    assert not at.exception, at.exception
    next(b for b in at.button if b.label == "Mark done").click().run()  # BL1 has no proof requirement
    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
    from jgevents.db.models import EventTask
    with session_scope(os.environ["DATABASE_URL"]) as s:
        t = s.scalar(select(EventTask).where(EventTask.event_id == eid, EventTask.key == "BL1"))
        assert t.status == "done" and t.done_by_id == users["ch.del"]

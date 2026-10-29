"""v2 – pre-event readiness. Covers the nine acceptance scenarios from the spec."""
from datetime import date, time, timedelta

import pytest
from sqlalchemy import func, select

from jgevents.db.models import Event, EventTask, Playbook
from jgevents.domain.constants import Status
from jgevents.services import events as ev
from jgevents.services import reports, tasks
from jgevents.services.errors import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)
from jgevents.services.events import EventFilter

from .conftest import BATCH_DETAILS, batch_common

TODAY = date(2026, 10, 25)
LAUNCH = date(2026, 11, 10)


@pytest.fixture
def frozen(monkeypatch):
    """Pin 'today' to 25-10-2026 so the spec's worked example (launch on 10 Nov) is exact."""
    import jgevents.services.events as e_mod
    import jgevents.services.tasks as t_mod
    monkeypatch.setattr(t_mod, "today_ist", lambda: TODAY)
    monkeypatch.setattr(e_mod, "today_ist", lambda: TODAY)
    return TODAY


def launch(c, user=None, centre=None, d=LAUNCH, late_reason=None, **kw):
    with c.s() as s:
        e, w = ev.create_event(s, user or c.ch_bpl, category_id=c.cats["batch_launch"].id,
                               centre_id=(centre or c.bpl).id, common=batch_common(d, **kw), details=BATCH_DETAILS,
                               late_reason=late_reason)
        return e.id, w


def tmap(c, eid):
    with c.s() as s:
        return {t.key: t for t in tasks.event_tasks(s, eid)}


def done(c, user, eid, key, **proof):
    with c.s() as s:
        t = next(t for t in tasks.event_tasks(s, eid) if t.key == key)
        return tasks.complete_task(s, user, t.id, t.version, **proof)


def whatsapp_campaign(c, eid, user=None, d=None, complete=False):
    with c.s() as s:
        target = s.get(Event, eid)
        w, _ = ev.create_event(s, user or c.ho, category_id=c.cats["whatsapp_campaign"].id, centre_id=target.centre_id,
                               common=dict(title="Launch blast", event_date=d or TODAY,
                                           start_time=time(18), responsible_person="CH", target_audience="Leads",
                                           objective="Attendance"),
                               details={"message_ref": "T1"}, linked_event_id=eid, submit=True)
        wid = w.id
    if complete:
        with c.s() as s:
            ev.transition(s, c.ho, wid, "approve")
            ev.transition(s, c.ho, wid, "complete")
    return wid


# ---------------------------------------------------------------- scenario 1: normal batch launch

def test_s1_batch_launch_generates_approved_schedule(ctx, frozen):
    eid, warnings = launch(ctx)
    assert warnings == []
    t = tmap(ctx, eid)
    assert {k: v.due_date for k, v in t.items()} == {
        "BL1": date(2026, 10, 31), "BL2": date(2026, 11, 3), "BL3": date(2026, 11, 4),
        "BL4": date(2026, 11, 8), "BL5": date(2026, 11, 12)}
    assert {k for k, v in t.items() if v.is_critical} == {"BL1", "BL2", "BL3", "BL4"}
    assert t["BL2"].owner_label == "Marketing" and t["BL2"].depends_on == ["BL1"]
    assert all(v.centre_id == ctx.bpl.id and v.status == "open" for v in t.values())
    with ctx.s() as s:  # shows on the centre head's task list for the period (what the calendar draws)
        mine = tasks.list_tasks(s, ctx.ch_bpl, tasks.TaskFilter(date_from=date(2026, 10, 1), date_to=date(2026, 11, 30)))
        assert {x.key for x in mine} == set(t)
        r = tasks.readiness(s, s.get(Event, eid))
        assert r.status == "on_track" and r.critical_open == 4 and "BL1" in r.reasons[0]
    # preview shown before saving matches what is generated
    with ctx.s() as s:
        prev = tasks.preview_schedule(s, ctx.cats["batch_launch"].id, LAUNCH)
        assert {p.key: p.due for p in prev} == {k: v.due_date for k, v in t.items()}


def test_s1_proof_dependencies_and_ready(ctx, frozen):
    eid, _ = launch(ctx)
    with pytest.raises(InvalidTransition, match="waiting on BL1"):
        done(ctx, ctx.ch_bpl, eid, "BL2", proof_url="https://jg.example/launch")
    done(ctx, ctx.ch_bpl, eid, "BL1")
    with pytest.raises(ValidationError, match="Admission link"):
        done(ctx, ctx.ch_bpl, eid, "BL2", proof_url="not a link")
    done(ctx, ctx.ch_bpl, eid, "BL2", proof_url="https://jg.example/launch")
    with pytest.raises(ValidationError, match="Calls made"):
        done(ctx, ctx.ch_bpl, eid, "BL3")
    done(ctx, ctx.ch_bpl, eid, "BL3", proof_count=180)
    with pytest.raises(InvalidTransition, match="ticks itself"):
        done(ctx, ctx.ch_bpl, eid, "BL4")
    with ctx.s() as s:  # 3 of 4 critical done ≠ ready
        assert tasks.readiness(s, s.get(Event, eid)).status != "ready"
    whatsapp_campaign(ctx, eid, complete=True)
    t = tmap(ctx, eid)
    assert t["BL4"].status == "done" and "campaign" in t["BL4"].status_reason
    with ctx.s() as s:
        assert tasks.readiness(s, s.get(Event, eid)).status == "ready"


def test_s1_scholarship_seats_rule_and_completion_gate(ctx, frozen):
    d = date(2026, 11, 29)
    with ctx.s() as s:
        e, _ = ev.create_event(s, ctx.ch_bpl, category_id=ctx.cats["scholarship_test"].id, centre_id=ctx.bpl.id,
                               common=dict(title="ST Nov", event_date=d, start_time=time(10), end_time=time(12),
                                           responsible_person="CH", venue="Hall", expected_participation=80),
                               details=dict(syllabus="CPC", eligibility="LLB", registration_deadline=d - timedelta(days=2)))
        eid = e.id
    t = tmap(ctx, eid)
    assert {k: v.due_date for k, v in t.items()} == {"ST1": date(2026, 11, 24), "ST2": date(2026, 11, 27),
                                                    "ST3": date(2026, 11, 28), "ST4": date(2026, 11, 30)}
    done(ctx, ctx.ch_bpl, eid, "ST2")
    with pytest.raises(ValidationError, match="fewer than registrations"):
        done(ctx, ctx.ch_bpl, eid, "ST3", proof_count=40, proof_count2=55)
    done(ctx, ctx.ch_bpl, eid, "ST3", proof_count=60, proof_count2=55)
    # event cannot be completed until the critical post-event task (scholarship list) is done
    with ctx.s() as s:
        e = s.get(Event, eid)
        e.status = Status.APPROVED
        e.event_date = TODAY  # pretend the test has happened
    with ctx.s() as s, pytest.raises(InvalidTransition, match="ST4"):
        ev.transition(s, ctx.ch_bpl, eid, "complete")
    done(ctx, ctx.ch_bpl, eid, "ST4", proof_count=6)
    with ctx.s() as s:
        assert ev.transition(s, ctx.ch_bpl, eid, "complete").status == Status.COMPLETED


# ---------------------------------------------------------------- scenario 2: multiple centres

def test_s2_two_centres_same_day_tasks_not_mixed(ctx, frozen):
    a, _ = launch(ctx)
    b, _ = launch(ctx, user=ctx.ch_idr, centre=ctx.idr)
    ta, tb = tmap(ctx, a), tmap(ctx, b)
    assert {t.id for t in ta.values()}.isdisjoint({t.id for t in tb.values()})
    assert {t.centre_id for t in ta.values()} == {ctx.bpl.id} and {t.centre_id for t in tb.values()} == {ctx.idr.id}
    done(ctx, ctx.ch_bpl, a, "BL1")
    assert tmap(ctx, b)["BL1"].status == "open"
    with ctx.s() as s:
        ho_view = tasks.list_tasks(s, ctx.ho)
        assert {t.centre_id for t in ho_view} == {ctx.bpl.id, ctx.idr.id}
        assert {t.event_id for t in tasks.list_tasks(s, ctx.ch_idr)} == {b}


# ---------------------------------------------------------------- scenario 3: rescheduling

def test_s3_date_change_recalculates_open_tasks_keeps_history(ctx, frozen):
    eid, _ = launch(ctx)
    done(ctx, ctx.ch_bpl, eid, "BL1")
    with ctx.s() as s:
        prev = {r.key: r for r in tasks.preview_date_change(s, ctx.ch_bpl, eid, date(2026, 11, 12))}
        assert prev["BL1"].new_due is None  # finished task keeps its history
        assert prev["BL2"].new_due == date(2026, 11, 5)
        e = s.get(Event, eid)
        ev.update_event(s, ctx.ch_bpl, eid, e.version, common=batch_common(date(2026, 11, 12)))
    t = tmap(ctx, eid)
    assert t["BL1"].status == "done" and t["BL1"].due_date == date(2026, 10, 31)  # unchanged
    assert t["BL2"].due_date == date(2026, 11, 5) and t["BL5"].due_date == date(2026, 11, 14)
    with ctx.s() as s:
        from jgevents.db.models import AuditLog
        log = s.scalar(select(AuditLog).where(AuditLog.action == "tasks.rescheduled", AuditLog.entity_id == eid))
        assert log is not None and len(log.details["changes"]) == 4


def test_s3_cancel_closes_open_tasks_and_reopen_restores(ctx, frozen):
    eid, _ = launch(ctx)
    done(ctx, ctx.ch_bpl, eid, "BL1")
    with ctx.s() as s:
        ev.transition(s, ctx.ch_bpl, eid, "cancel", "Faculty left")
    t = tmap(ctx, eid)
    assert t["BL1"].status == "done" and {t[k].status for k in ("BL2", "BL3", "BL4", "BL5")} == {"closed"}
    with ctx.s() as s:
        assert tasks.readiness(s, s.get(Event, eid)).status == "cancelled"
        ev.transition(s, ctx.ho, eid, "reopen")
    assert tmap(ctx, eid)["BL2"].status == "open"


# ---------------------------------------------------------------- scenario 4: late creation

def test_s4_lead_time_enforced_with_head_office_exception(ctx, frozen):
    close = TODAY + timedelta(days=4)
    with pytest.raises(ValidationError, match="at least 10 days"):
        launch(ctx, d=close)
    with pytest.raises(ValidationError, match="reason"):
        launch(ctx, user=ctx.ho, d=close)
    eid, warnings = launch(ctx, user=ctx.ho, d=close, late_reason="Faculty only confirmed today")
    assert any("Head Office exception" in w for w in warnings)
    with ctx.s() as s:
        e = s.get(Event, eid)
        assert e.booked_late and e.late_approved_by_id == ctx.ho.id
        r = tasks.readiness(s, e)
        assert r.status == "off_track" and r.overdue == 3  # BL1 (T-10), BL2 (T-7), BL3 (T-6) already missed
        assert any("BL1" in x and "overdue" in x for x in r.reasons)
    # a centre head cannot move an existing launch inside the window either
    ok, _ = launch(ctx, start=time(14), end=time(16), title="Second")
    with ctx.s() as s, pytest.raises(ValidationError, match="at least 10 days"):
        e = s.get(Event, ok)
        ev.update_event(s, ctx.ch_bpl, ok, e.version, common=batch_common(close, start=time(14), end=time(16)))


# ---------------------------------------------------------------- scenario 5: blocked critical task

def test_s5_national_mock_blocked_by_head_office(ctx, frozen):
    d = date(2026, 11, 15)
    with ctx.s() as s:
        parent, kids = ev.create_national_event(s, ctx.ho, category_id=ctx.cats["open_mock"].id, common=dict(
            title="AIOM", event_date=d, start_time=time(10), end_time=time(12), responsible_person="Academics"),
            details={"programme": "PCS-J"}, centre_ids=[ctx.bpl.id, ctx.idr.id])
        pid, kid = parent.id, next(k.id for k in kids if k.centre_id == ctx.bpl.id)
    assert set(tmap(ctx, pid)) == {"H1", "H2"} and set(tmap(ctx, kid)) == {"C1", "C2", "C3", "C4", "C5"}
    # centre heads cannot tick Head Office tasks
    with pytest.raises(PermissionDenied):
        done(ctx, ctx.ch_bpl, pid, "H1")
    with pytest.raises(InvalidTransition, match="waiting on H1"):
        done(ctx, ctx.ch_bpl, kid, "C4", proof_count=50, proof_count2=40)
    # centre does all its own work; HO's H1 slips past its deadline -> blocked by Head Office, not centre's fault
    done(ctx, ctx.ch_bpl, kid, "C1", proof_url="https://lsq.example/aiom")
    done(ctx, ctx.ch_bpl, kid, "C2", proof_count=90)
    whatsapp_campaign(ctx, kid, complete=True)
    import jgevents.services.tasks as t_mod
    t_mod.today_ist = lambda: date(2026, 11, 14)  # monkeypatch fixture restores it
    with ctx.s() as s:
        r = tasks.readiness(s, s.get(Event, kid))
        assert r.status == "blocked_ho" and "waiting on Head Office" in r.reasons[0]
        assert tasks.readiness(s, s.get(Event, pid)).status == "off_track"
    done(ctx, ctx.ho, pid, "H1")
    done(ctx, ctx.ch_bpl, kid, "C4", proof_count=50, proof_count2=40)
    with ctx.s() as s:
        assert tasks.readiness(s, s.get(Event, kid)).status == "ready"


def test_s5_online_mode_drops_centre_ready(ctx, frozen):
    with ctx.s() as s:
        parent, kids = ev.create_national_event(s, ctx.ho, category_id=ctx.cats["open_mock"].id, common=dict(
            title="AIOM online", event_date=date(2026, 11, 20), start_time=time(10), end_time=time(12),
            responsible_person="Academics"), details={"programme": "PCS-J", "mode": "Online"}, centre_ids=[ctx.bpl.id])
        kid = kids[0].id
    assert tmap(ctx, kid)["C4"].status == "not_needed"


# ---------------------------------------------------------------- scenario 6: template changes

def test_s6_playbook_change_does_not_alter_existing_events(ctx, frozen):
    old, _ = launch(ctx)
    with ctx.s() as s:
        pb = tasks.active_playbook(s, ctx.cats["batch_launch"].id)
        rows = [dict(key=t.key, title=t.title, offset_days=t.offset_days, owner_label=t.owner_label,
                     is_critical=t.is_critical, scope=t.scope, depends_on=t.depends_on, proof_type=t.proof_type,
                     proof_label=t.proof_label) for t in pb.tasks]
        rows[1]["offset_days"] = -8  # BL2 now T-8
        rows.append(dict(key="BL6", title="Paid ads live", offset_days=-5, owner_label="Marketing", is_critical=False,
                         scope="centre", depends_on="BL2", proof_type="url", proof_label="Ad link"))
        with pytest.raises(ValidationError, match="loop"):
            tasks.save_playbook(s, ctx.ho, ctx.cats["batch_launch"].id,
                                rows[:1] + [dict(rows[1], depends_on=["BL2"])] + rows[2:], "bad")
    with ctx.s() as s:
        new_pb = tasks.save_playbook(s, ctx.ho, ctx.cats["batch_launch"].id, rows, "Paid ads added")
        assert new_pb.version == 2
        with pytest.raises(PermissionDenied):
            tasks.save_playbook(s, ctx.ch_bpl, ctx.cats["batch_launch"].id, rows)
    t_old = tmap(ctx, old)
    assert "BL6" not in t_old and t_old["BL2"].due_date == date(2026, 11, 3) and t_old["BL2"].playbook_version == 1
    new, _ = launch(ctx, start=time(14), end=time(16), title="New launch")
    t_new = tmap(ctx, new)
    assert t_new["BL2"].due_date == date(2026, 11, 2) and "BL6" in t_new and t_new["BL6"].playbook_version == 2
    # explicit update of an old event: adds missing tasks only, never resets done ones
    done(ctx, ctx.ch_bpl, old, "BL1")
    with ctx.s() as s:
        added = tasks.apply_latest_playbook(s, ctx.ho, old)
        assert [t.key for t in added] == ["BL6"]
    assert tmap(ctx, old)["BL1"].status == "done"
    with ctx.s() as s:
        assert s.scalar(select(func.count()).select_from(Playbook)
                        .where(Playbook.category_id == ctx.cats["batch_launch"].id, Playbook.is_active)) == 1


# ---------------------------------------------------------------- scenario 7: permissions

def test_s7_permissions(ctx, frozen):
    a, _ = launch(ctx)
    b, _ = launch(ctx, user=ctx.ch_idr, centre=ctx.idr)
    with pytest.raises(PermissionDenied):
        done(ctx, ctx.ch_idr, a, "BL1")
    with pytest.raises(PermissionDenied):
        done(ctx, ctx.viewer_all, a, "BL1")
    with ctx.s() as s:
        t = next(x for x in tasks.event_tasks(s, a) if x.key == "BL5")
        with pytest.raises(PermissionDenied):
            tasks.waive_task(s, ctx.ch_bpl, t.id, "not needed")
        with pytest.raises(PermissionDenied):
            tasks.flag_task(s, ctx.ch_bpl, t.id, "other", "x")
        with pytest.raises(PermissionDenied):
            tasks.get_task(s, ctx.ch_idr, t.id)
        tasks.waive_task(s, ctx.ho, t.id, "No no-shows expected for a demo batch")
    assert tmap(ctx, a)["BL5"].status == "waived"
    done(ctx, ctx.ho, b, "BL1")  # Head Office can tick on a centre's behalf
    # reopen: centre head before event date only; waived only by HO
    done(ctx, ctx.ch_bpl, a, "BL1")
    with ctx.s() as s:
        t = next(x for x in tasks.event_tasks(s, a) if x.key == "BL1")
        with pytest.raises(ValidationError):
            tasks.reopen_task(s, ctx.ch_bpl, t.id, "")
        tasks.reopen_task(s, ctx.ch_bpl, t.id, "Fee changed")
        w = next(x for x in tasks.event_tasks(s, a) if x.key == "BL5")
        with pytest.raises(PermissionDenied):
            tasks.reopen_task(s, ctx.ch_bpl, w.id, "undo waiver")


def test_s7_audit_flags_visible_to_centre_and_late_marking(ctx, frozen):
    eid, _ = launch(ctx)
    import jgevents.services.tasks as t_mod
    with ctx.s() as s:  # make BL1 overdue, then complete it -> marked done late automatically
        t = next(x for x in tasks.event_tasks(s, eid) if x.key == "BL1")
        t.due_date = date(2026, 10, 1)
    t = done(ctx, ctx.ch_bpl, eid, "BL1")
    assert t.done_late is True
    with ctx.s() as s:
        f = tasks.flag_task(s, ctx.ho, t.id, "not_done_properly", "Fee missing on the details sheet")
        fid = f.id
    with ctx.s() as s:
        mine = tasks.list_flags(s, ctx.ch_bpl)
        assert [x.id for x in mine] == [fid]
        assert tasks.list_flags(s, ctx.ch_idr) == []
        tasks.reply_flag(s, ctx.ch_bpl, fid, "Updated the sheet")
        tasks.resolve_flag(s, ctx.ho, fid)
        assert tasks.list_flags(s, ctx.ch_bpl) == []
    _ = t_mod


# ---------------------------------------------------------------- scenario 8: persistence & duplicates

def test_s8_generation_is_idempotent_and_persistent(ctx, frozen):
    eid, _ = launch(ctx)
    with ctx.s() as s:
        e = s.get(Event, eid)
        assert tasks.generate_tasks(s, ctx.ho, e) == []
        assert tasks.apply_latest_playbook(s, ctx.ho, eid) == []
    from jgevents.db.session import get_engine, get_sessionmaker
    get_engine(ctx.url).dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    with ctx.s() as s:
        assert s.scalar(select(func.count()).select_from(EventTask).where(EventTask.event_id == eid)) == 5
    # the database itself refuses a duplicate key per event
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError):
        with ctx.s() as s:
            t = s.scalar(select(EventTask).where(EventTask.event_id == eid, EventTask.key == "BL1"))
            s.add(EventTask(event_id=eid, centre_id=t.centre_id, key="BL1", title="dup", owner_label="x",
                            offset_days=-10, due_date=t.due_date, depends_on=[]))


def test_s8_stale_task_update_rejected(ctx, frozen):
    eid, _ = launch(ctx)
    with ctx.s() as s:
        t = next(x for x in tasks.event_tasks(s, eid) if x.key == "BL1")
        v = t.version
    with ctx.s() as s:
        tasks.complete_task(s, ctx.ho, t.id, v)
    with ctx.s() as s, pytest.raises(ConflictError):
        tasks.reopen_task(s, ctx.ch_bpl, t.id, "x", expected_version=v)


# ---------------------------------------------------------------- scenario 9 & reporting

def test_s9_events_without_playbook_unchanged_and_reports(ctx, frozen):
    with ctx.s() as s:
        e, _ = ev.create_event(s, ctx.ch_bpl, category_id=ctx.cats["offline_seminar"].id, centre_id=ctx.bpl.id,
                               common=dict(title="Seminar", event_date=TODAY + timedelta(days=1)))
        assert tasks.event_tasks(s, e.id) == [] and tasks.readiness(s, e).status is None
    eid, _ = launch(ctx)
    done(ctx, ctx.ch_bpl, eid, "BL1")
    with ctx.s() as s:
        sheets = reports.build_report(s, ctx.ho, "readiness", EventFilter(), 2026, 11)
        assert set(sheets) >= {"By centre", "Events", "Tasks"}
        assert "Bhopal" in set(sheets["By centre"]["Centre"])
        own = reports.build_report(s, ctx.ch_idr, "readiness", EventFilter(), 2026, 11)
        assert own["Tasks"].empty

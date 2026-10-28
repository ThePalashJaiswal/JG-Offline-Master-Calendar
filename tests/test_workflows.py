from datetime import date, time, timedelta

import pytest
from sqlalchemy import select

from jgevents.db.models import ApprovalAction, AuditLog, ChecklistItem, Event
from jgevents.db.session import get_engine, session_scope
from jgevents.domain.constants import PlanStatus, Requirement, Status
from jgevents.services import config_service, dashboard, planning, recurrence
from jgevents.services import events as ev
from jgevents.services.errors import (
    ConflictError,
    DuplicateSubmission,
    InvalidTransition,
    ScheduleConflict,
    ValidationError,
)
from jgevents.services.events import EventFilter
from jgevents.utils.dates import last_weekday_of_month, month_bounds, today_ist

from .conftest import BATCH_DETAILS, batch_common, next_month_date, past_date


def create_batch(c, user=None, centre=None, submit=False, **kw):
    with c.s() as s:
        e, w = ev.create_event(s, user or c.ch_bpl, category_id=c.cats["batch_launch"].id,
                               centre_id=(centre or c.bpl).id, common=batch_common(**kw), details=BATCH_DETAILS,
                               submit=submit)
        return e.id, w


# ---------------------------------------------------------------- creation & validation

def test_draft_is_lenient_submit_is_strict(ctx):
    with ctx.s() as s:
        e, _ = ev.create_event(s, ctx.ch_bpl, category_id=ctx.cats["batch_launch"].id, centre_id=ctx.bpl.id,
                               common=dict(title="Half-planned", event_date=next_month_date()))
        assert e.status == Status.DRAFT
        assert s.scalar(select(ChecklistItem).where(ChecklistItem.event_id == e.id)) is not None
        with pytest.raises(ValidationError) as exc:
            ev.transition(s, ctx.ch_bpl, e.id, "submit")
        msg = str(exc.value)
        assert "Batch name" in msg and "Start time" in msg


def test_field_validation(ctx):
    with ctx.s() as s:
        with pytest.raises(ValidationError, match="End time must be after"):
            ev.create_event(s, ctx.ch_bpl, category_id=ctx.cats["batch_launch"].id, centre_id=ctx.bpl.id,
                            common=batch_common(start=time(12), end=time(10)), details=BATCH_DETAILS)
        with pytest.raises(ValidationError, match="not an allowed option"):
            ev.create_event(s, ctx.ch_bpl, category_id=ctx.cats["batch_launch"].id, centre_id=ctx.bpl.id,
                            common=batch_common(), details={**BATCH_DETAILS, "programme": "Astrology"})
        d = next_month_date()
        with pytest.raises(ValidationError, match="cannot be after the event date"):
            ev.create_event(s, ctx.ch_bpl, category_id=ctx.cats["batch_launch"].id, centre_id=ctx.bpl.id,
                            common=batch_common(d), details={**BATCH_DETAILS, "promo_deadline": d + timedelta(days=1)})


def test_multiple_launches_same_day_non_overlapping_ok_overlap_blocked(ctx):
    create_batch(ctx, start=time(9), end=time(11))
    create_batch(ctx, start=time(11), end=time(13), title="Second launch")  # back-to-back is fine
    with pytest.raises(ScheduleConflict):
        create_batch(ctx, start=time(10), end=time(12), title="Clash")
    # another centre on the same slot is fine
    create_batch(ctx, user=ctx.ch_idr, centre=ctx.idr, start=time(10), end=time(12))
    # a seminar overlapping a launch is allowed but flagged
    with ctx.s() as s:
        _, warnings = ev.create_event(s, ctx.ch_bpl, category_id=ctx.cats["offline_seminar"].id, centre_id=ctx.bpl.id,
                                      common=dict(title="Seminar", event_date=next_month_date(), start_time=time(10),
                                                  end_time=time(11)))
        assert warnings and "conflict" in warnings[0].lower()
        assert len(ev.find_conflicts(s, ctx.bpl.id, next_month_date(), time(10), time(10, 30))) == 2


def test_cancelled_events_do_not_conflict(ctx):
    eid, _ = create_batch(ctx)
    with ctx.s() as s:
        ev.transition(s, ctx.ch_bpl, eid, "cancel", "Faculty unavailable")
    create_batch(ctx, title="Replacement")


def test_whatsapp_linked_to_batch_keeps_own_status(ctx):
    bid, _ = create_batch(ctx, submit=True)
    wa = ctx.cats["whatsapp_campaign"]
    with ctx.s() as s:
        common = dict(title="Launch blast", event_date=next_month_date() - timedelta(days=2), start_time=time(18),
                      responsible_person="Counsellor", target_audience="Leads", objective="Attendance")
        w1, _ = ev.create_event(s, ctx.ch_bpl, category_id=wa.id, centre_id=ctx.bpl.id, common=common,
                                details={"message_ref": "T1"}, linked_event_id=bid, submit=True)
        w2, _ = ev.create_event(s, ctx.ch_bpl, category_id=wa.id, centre_id=ctx.bpl.id,
                                common={**common, "title": "Reminder", "start_time": time(10)},
                                details={"message_ref": "T2"}, linked_event_id=bid)
        assert w1.status == Status.SUBMITTED and w2.status == Status.DRAFT
        assert {c.id for c in ev.linked_campaigns(s, ctx.ch_bpl, bid)} == {w1.id, w2.id}
        assert s.get(Event, bid).status == Status.SUBMITTED  # batch unaffected
        with pytest.raises(ValidationError, match="must be a batch launch"):
            ev.create_event(s, ctx.ch_bpl, category_id=wa.id, centre_id=ctx.bpl.id, common=common,
                            details={"message_ref": "T3"}, linked_event_id=w1.id)
    oid, _ = create_batch(ctx, user=ctx.ch_idr, centre=ctx.idr)
    with ctx.s() as s, pytest.raises(ValidationError, match="same centre"):
        ev.create_event(s, ctx.ho, category_id=wa.id, centre_id=ctx.bpl.id, common=common,
                        details={"message_ref": "T4"}, linked_event_id=oid)


# ---------------------------------------------------------------- approval workflow

def test_submit_return_revise_approve_and_calendar(ctx):
    eid, _ = create_batch(ctx, submit=True)
    with ctx.s() as s:
        assert [e.id for e in ev.approval_queue(s, ctx.ho)] == [eid]
        ev.transition(s, ctx.ho, eid, "return", "Move to 11am – hall booked")
    with ctx.s() as s:
        e = ev.get_event(s, ctx.ch_bpl, eid)
        assert e.status == Status.RETURNED and e.last_review_comment == "Move to 11am – hall booked"
        ev.update_event(s, ctx.ch_bpl, eid, e.version, common=batch_common(start=time(11), end=time(13)))
        ev.transition(s, ctx.ch_bpl, eid, "submit")
    with ctx.s() as s:
        with pytest.raises(ValidationError):
            ev.transition(s, ctx.ho, eid, "reject")  # comment required
    with ctx.s() as s:
        ev.transition(s, ctx.ho, eid, "approve", "OK")
    d = next_month_date()
    start, end = month_bounds(d.year, d.month)
    with ctx.s() as s:
        cal = ev.list_events(s, ctx.ho, EventFilter(date_from=start, date_to=end))
        assert [e.id for e in cal] == [eid] and cal[0].status == Status.APPROVED
        nxt = month_bounds(*((d.year + (d.month == 12), d.month % 12 + 1)))
        assert ev.list_events(s, ctx.ho, EventFilter(date_from=nxt[0], date_to=nxt[1])) == []
        acts = [a.action for a in s.scalars(select(ApprovalAction).where(ApprovalAction.event_id == eid)
                                            .order_by(ApprovalAction.id))]
        assert acts == ["created", "submit", "return", "submit", "approve"]
        a = s.scalar(select(ApprovalAction).where(ApprovalAction.action == "approve"))
        assert a.actor_id == ctx.ho.id and a.created_at is not None and a.comment == "OK"
        assert s.scalar(select(AuditLog).where(AuditLog.action == "event.approve", AuditLog.entity_id == eid))


def test_material_change_after_approval_needs_reapproval(ctx):
    eid, _ = create_batch(ctx, submit=True)
    with ctx.s() as s:
        ev.transition(s, ctx.ho, eid, "approve")
    with ctx.s() as s:
        e = ev.get_event(s, ctx.ch_bpl, eid)
        ev.update_event(s, ctx.ch_bpl, eid, e.version, common=batch_common(remarks="Bring ID cards"))
        assert e.status == Status.APPROVED  # non-material edit keeps approval
        _, warnings = ev.update_event(s, ctx.ch_bpl, eid, e.version, common=batch_common(start=time(14), end=time(16)))
        assert e.status == Status.SUBMITTED and e.changed_after_approval and warnings
    with ctx.s() as s:
        config_service.set_setting(s, ctx.ho, "reapproval_on_material_change", False)
        ev.transition(s, ctx.ho, eid, "approve")
    with ctx.s() as s:
        e = ev.get_event(s, ctx.ch_bpl, eid)
        ev.update_event(s, ctx.ch_bpl, eid, e.version, common=batch_common(start=time(15), end=time(17)))
        assert e.status == Status.APPROVED


def test_execution_lifecycle(ctx):
    with ctx.s() as s:
        e, _ = ev.create_event(s, ctx.ho, category_id=ctx.cats["offline_seminar"].id, centre_id=ctx.bpl.id,
                               common=dict(title="Past seminar", event_date=past_date(), start_time=time(10),
                                           end_time=time(12), responsible_person="CH", venue="Hall",
                                           expected_participation=50, target_audience="All"),
                               details=dict(topic="Evidence Act", speaker="Guest"), submit=True)
        ev.transition(s, ctx.ho, e.id, "approve")
    with ctx.s() as s:
        e = ev.get_event(s, ctx.ch_bpl, e.id)
        assert "Overdue: outcome not recorded" in ev.risk_flags(e)
        ev.update_execution(s, ctx.ch_bpl, e.id, e.version, actual_participation=42, post_event_remarks="Good Q&A")
        ev.transition(s, ctx.ch_bpl, e.id, "complete")
        assert e.status == Status.COMPLETED and e.completed_at
    fid, _ = create_batch(ctx, submit=True)
    with ctx.s() as s:
        ev.transition(s, ctx.ho, fid, "approve")
        with pytest.raises(InvalidTransition):
            ev.transition(s, ctx.ch_bpl, fid, "complete")  # future event
        assert "complete" not in ev.allowed_actions(ctx.ch_bpl, s.get(Event, fid))


# ---------------------------------------------------------------- planning cycle

def test_monthly_plan_submit_duplicate_and_resubmit(ctx):
    d = next_month_date()
    y, m = d.year, d.month
    with ctx.s() as s:
        with pytest.raises(ValidationError, match="at least one event"):
            planning.submit_plan(s, ctx.ch_bpl, ctx.bpl.id, y, m)
    create_batch(ctx)
    create_batch(ctx, start=time(14), end=time(16), title="Afternoon launch")
    with ctx.s() as s:
        comp = planning.completeness(s, ctx.ch_bpl, ctx.bpl.id, y, m)
        assert [ln.category.key for ln in comp.missing_mandatory] == ["scholarship_test"]
        with pytest.raises(ValidationError, match="Mandatory activities missing"):
            planning.submit_plan(s, ctx.ch_bpl, ctx.bpl.id, y, m)
    with ctx.s() as s:
        plan = planning.submit_plan(s, ctx.ch_bpl, ctx.bpl.id, y, m, note="Scholarship test moved to next month")
        assert plan.status == PlanStatus.SUBMITTED and plan.submission_count == 1
    with ctx.s() as s, pytest.raises(DuplicateSubmission):
        planning.submit_plan(s, ctx.ch_bpl, ctx.bpl.id, y, m, note="again")
    with ctx.s() as s:
        plan = planning.find_plan(s, ctx.bpl.id, y, m)
        planning.review_plan(s, ctx.ho, plan.id, "return", "Add the scholarship test")
        assert plan.status == PlanStatus.RETURNED
    with ctx.s() as s:
        ev.create_event(s, ctx.ch_bpl, category_id=ctx.cats["scholarship_test"].id, centre_id=ctx.bpl.id, common=dict(
            title="Scholarship test", event_date=d.replace(day=28), start_time=time(10), end_time=time(12),
            responsible_person="CH", venue="Hall", expected_participation=80),
            details=dict(syllabus="CPC", eligibility="LLB final year+", registration_deadline=d.replace(day=25)))
        plan = planning.submit_plan(s, ctx.ch_bpl, ctx.bpl.id, y, m)
        assert plan.submission_count == 2 and plan.status == PlanStatus.SUBMITTED
        assert planning.completeness(s, ctx.ch_bpl, ctx.bpl.id, y, m).missing_mandatory == []
    with ctx.s() as s:
        planning.review_plan(s, ctx.ho, plan.id, "approve")
        assert planning.find_plan(s, ctx.bpl.id, y, m).status == PlanStatus.APPROVED
        assert all(e.status == Status.APPROVED for e in planning.month_events(s, ctx.bpl.id, y, m))


def test_incomplete_entries_block_plan_submission(ctx):
    d = next_month_date()
    with ctx.s() as s:
        ev.create_event(s, ctx.ch_bpl, category_id=ctx.cats["batch_launch"].id, centre_id=ctx.bpl.id,
                        common=dict(title="Half", event_date=d))
        comp = planning.completeness(s, ctx.ch_bpl, ctx.bpl.id, d.year, d.month)
        assert len(comp.incomplete) == 1
        with pytest.raises(ValidationError, match="Half"):
            planning.submit_plan(s, ctx.ch_bpl, ctx.bpl.id, d.year, d.month, note="n/a")


def test_requirements_and_deadline_configurable(ctx):
    d = next_month_date()
    pmi = ctx.cats["pmi"]
    with ctx.s() as s:
        config_service.set_requirement(s, ctx.ho, pmi.id, ctx.idr.id, Requirement.MANDATORY, 2)
        config_service.set_requirement(s, ctx.ho, ctx.cats["scholarship_test"].id, ctx.idr.id,
                                       Requirement.NOT_APPLICABLE, 0)
        idr = planning.completeness(s, ctx.ho, ctx.idr.id, d.year, d.month)
        bpl = planning.completeness(s, ctx.ho, ctx.bpl.id, d.year, d.month)
        assert [ln.category.key for ln in idr.missing_mandatory] == ["pmi"]
        assert [ln.category.key for ln in bpl.missing_mandatory] == ["scholarship_test"]
        default = config_service.planning_deadline(s, d.year, d.month)
        assert default.day == 25 and default < d.replace(day=1)
        config_service.set_planning_deadline(s, ctx.ho, d.year, d.month, d.replace(day=3))
        assert config_service.planning_deadline(s, d.year, d.month) == d.replace(day=3)
        with pytest.raises(Exception):
            config_service.set_requirement(s, ctx.ch_bpl, pmi.id, None, Requirement.MANDATORY, 1)


def test_plan_history_survives_month_end(ctx):
    with ctx.s() as s:
        e, _ = ev.create_event(s, ctx.ch_bpl, category_id=ctx.cats["informal_engagement"].id, centre_id=ctx.bpl.id,
                               common=dict(title="Last month's chai", event_date=today_ist() - timedelta(days=40)))
    with ctx.s() as s:
        old = today_ist() - timedelta(days=40)
        start, end = month_bounds(old.year, old.month)
        assert [x.id for x in ev.list_events(s, ctx.ho, EventFilter(date_from=start, date_to=end))] == [e.id]


# ---------------------------------------------------------------- recurrence

def test_scholarship_recurrence_generates_reviewable_proposals(ctx):
    d = next_month_date()
    with ctx.s() as s:
        props = recurrence.proposals(s, ctx.ch_bpl, d.year, d.month)
        assert len(props) == 1 and props[0].centre.id == ctx.bpl.id  # only own centre
        assert props[0].date == last_weekday_of_month(d.year, d.month, 6) and props[0].date.weekday() == 6
        assert s.scalar(select(Event).where(Event.is_proposed.is_(True))) is None  # nothing created silently
        ho_props = recurrence.proposals(s, ctx.ho, d.year, d.month)
        assert len(ho_props) == 7
        created = recurrence.generate(s, ctx.ho, d.year, d.month, [(p.rule.id, p.centre.id) for p in ho_props])
        assert len(created) == 7 and all(e.status == Status.DRAFT and e.is_proposed for e in created)
        again = recurrence.generate(s, ctx.ho, d.year, d.month, [(p.rule.id, p.centre.id) for p in ho_props])
        assert again == []
    assert date(2026, 11, 29) == last_weekday_of_month(2026, 11, 6)
    assert date(2027, 2, 28) == last_weekday_of_month(2027, 2, 6)


# ---------------------------------------------------------------- national events

def test_national_open_mock_parent_and_centre_records(ctx):
    mock = ctx.cats["open_mock"]
    d = next_month_date(15)
    with ctx.s() as s:
        parent, kids = ev.create_national_event(s, ctx.ho, category_id=mock.id, common=dict(
            title="AIOM – Prelims", event_date=d, start_time=time(10), end_time=time(12), responsible_person="Academics"),
            details={"programme": "PCS-J Prelims"}, centre_ids=[ctx.bpl.id, ctx.idr.id])
        pid = parent.id
        assert parent.centre_id is None and parent.status == Status.APPROVED and len(kids) == 2
        assert ev.assign_centres(s, ctx.ho, pid, [ctx.bpl.id, ctx.idr.id]) == []  # no duplicates
        assert s.scalar(select(Event).where(Event.title == "AIOM – Prelims", Event.is_national_parent.is_(True))).id == pid
        bpl_child = next(k for k in kids if k.centre_id == ctx.bpl.id)
        idr_child = next(k for k in kids if k.centre_id == ctx.idr.id)
    with ctx.s() as s:
        # Centre head manages own centre record only
        assert [e.id for e in ev.list_events(s, ctx.ch_bpl)] == [bpl_child.id]
        c = ev.get_event(s, ctx.ch_bpl, bpl_child.id)
        ev.update_event(s, ctx.ch_bpl, c.id, c.version, common=dict(
            title=c.title, event_date=c.event_date, start_time=c.start_time, end_time=c.end_time,
            responsible_person="Bhopal CH", venue="Hall A", expected_participation=120),
            details={"participation_plan": "Two halls"})
        ev.transition(s, ctx.ch_bpl, c.id, "submit")
        with pytest.raises(Exception):
            ev.update_event(s, ctx.ch_bpl, pid, None, common=dict(title="x", event_date=d))
        with pytest.raises(Exception):
            ev.transition(s, ctx.ch_bpl, idr_child.id, "submit")
    with ctx.s() as s:
        # calendar shows centre records, not the parent
        assert pid not in [e.id for e in ev.list_events(s, ctx.ho)]
        p = s.get(Event, pid)
        ev.update_event(s, ctx.ho, pid, p.version, common=dict(title=p.title, event_date=d + timedelta(days=1),
                                                                start_time=p.start_time, end_time=p.end_time,
                                                                responsible_person="Academics"))
    with ctx.s() as s:
        assert {k.event_date for k in ev.children(s, pid)} == {d + timedelta(days=1)}


# ---------------------------------------------------------------- concurrency & persistence

def test_stale_edit_is_rejected_not_silently_overwritten(ctx):
    eid, _ = create_batch(ctx)
    with ctx.s() as s:
        v = ev.get_event(s, ctx.ch_bpl, eid).version
    with ctx.s() as s:  # Head Office edits first
        ev.update_event(s, ctx.ho, eid, v, common=batch_common(remarks="HO note"))
    with ctx.s() as s, pytest.raises(ConflictError):  # centre head's form still holds old version
        ev.update_event(s, ctx.ch_bpl, eid, v, common=batch_common(remarks="CH note"))
    with ctx.s() as s:
        assert s.get(Event, eid).remarks == "HO note"


def test_concurrent_sessions_flush_conflict(ctx):
    if ctx.url.startswith("sqlite"):
        pytest.skip("Row-level concurrency is a PostgreSQL guarantee; SQLite is single-writer (dev only)")
    eid, _ = create_batch(ctx)
    from jgevents.db.session import get_sessionmaker
    a, b = get_sessionmaker(ctx.url)(), get_sessionmaker(ctx.url)()
    try:
        ea, eb = a.get(Event, eid), b.get(Event, eid)
        ea.remarks = "A"
        a.commit()
        eb.remarks = "B"
        with pytest.raises(Exception):
            b.commit()  # version_id_col guard
    finally:
        a.close()
        b.close()


def test_data_survives_restart(ctx):
    eid, _ = create_batch(ctx)
    get_engine(ctx.url).dispose()
    get_engine.cache_clear()
    from jgevents.db.session import get_sessionmaker
    get_sessionmaker.cache_clear()
    with session_scope(ctx.url) as s:
        assert s.get(Event, eid).title == "Civil Judge Foundation launch"


def test_dashboard_metrics_follow_filters(ctx):
    d = next_month_date()
    create_batch(ctx, submit=True)
    create_batch(ctx, user=ctx.ch_idr, centre=ctx.idr)
    with ctx.s() as s:
        data = dashboard.build(s, ctx.ho, d.year, d.month)
        assert data.metrics["planned"].value == 2 and data.metrics["pending"].value == 1
        assert data.metrics["centres"].value == 7
        assert data.metrics["not_submitted"].value == 7
        only_idr = dashboard.build(s, ctx.ho, d.year, d.month, EventFilter(centre_ids=[ctx.idr.id]))
        assert only_idr.metrics["planned"].value == 1 and only_idr.metrics["pending"].value == 0
        mine = dashboard.build(s, ctx.ch_bpl, d.year, d.month)
        assert mine.metrics["planned"].value == 1 and mine.metrics["centres"].value == 1

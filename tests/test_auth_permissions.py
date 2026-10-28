import pytest
from sqlalchemy import select

from jgevents.db.models import Event, User
from jgevents.domain.constants import Role, Status
from jgevents.services import auth, directory, reports
from jgevents.services import events as ev
from jgevents.services.errors import AuthError, PermissionDenied, ValidationError
from jgevents.services.events import EventFilter

from .conftest import BATCH_DETAILS, batch_common


def _bpl_event(c, submit=False):
    with c.s() as s:
        e, _ = ev.create_event(s, c.ch_bpl, category_id=c.cats["batch_launch"].id, centre_id=c.bpl.id,
                               common=batch_common(), details=BATCH_DETAILS, submit=submit)
        return e.id


def test_passwords_are_hashed_never_plaintext(ctx):
    with ctx.s() as s:
        u = s.get(User, ctx.ch_bpl.id)
        assert u.password_hash.startswith("$2") and "Bhop@l-2026" not in u.password_hash


def test_login_success_failure_and_lockout(ctx):
    with ctx.s() as s:
        assert auth.authenticate(s, "CH.BPL", "Bhop@l-2026").id == ctx.ch_bpl.id
    for _ in range(auth.MAX_FAILED_LOGINS):
        with pytest.raises(AuthError):
            with ctx.s() as s:
                auth.authenticate(s, "ch.bpl", "wrong-password-1")
    with pytest.raises(AuthError, match="locked"):
        with ctx.s() as s:
            auth.authenticate(s, "ch.bpl", "Bhop@l-2026")


def test_inactive_user_cannot_login_or_act(ctx):
    with ctx.s() as s:
        directory.update_user(s, ctx.super, ctx.ch_idr.id, is_active=False)
    with pytest.raises(AuthError):
        with ctx.s() as s:
            auth.authenticate(s, "ch.idr", "Ind0re-2026")
    with ctx.s() as s:
        assert auth.load_active_user(s, ctx.ch_idr.id) is None


def test_weak_password_rejected(ctx):
    with ctx.s() as s, pytest.raises(ValidationError):
        directory.create_user(s, ctx.super, "weakling", "Weak", Role.VIEWER, password="short")


def test_centre_head_creates_for_own_centre_only(ctx):
    eid = _bpl_event(ctx)
    assert eid
    with ctx.s() as s, pytest.raises(PermissionDenied):
        ev.create_event(s, ctx.ch_bpl, category_id=ctx.cats["batch_launch"].id, centre_id=ctx.idr.id,
                        common=batch_common(), details=BATCH_DETAILS)


def test_centre_head_cannot_touch_other_centre_by_id(ctx):
    """Acceptance #2: tampering with a record id is blocked in the service layer."""
    eid = _bpl_event(ctx, submit=True)
    with ctx.s() as s:
        with pytest.raises(PermissionDenied):
            ev.get_event(s, ctx.ch_idr, eid)
        with pytest.raises(PermissionDenied):
            ev.update_event(s, ctx.ch_idr, eid, None, common=batch_common(title="hijack"))
        with pytest.raises(PermissionDenied):
            ev.transition(s, ctx.ch_idr, eid, "cancel", "nope")
        with pytest.raises(PermissionDenied):
            ev.checklist(s, ctx.ch_idr, eid)
        assert ev.list_events(s, ctx.ch_idr, EventFilter()) == []
    with ctx.s() as s:  # moving own event into another centre is also blocked
        own, _ = ev.create_event(s, ctx.ch_idr, category_id=ctx.cats["informal_engagement"].id,
                                 centre_id=ctx.idr.id, common=dict(title="Chai", event_date=batch_common()["event_date"]))
        with pytest.raises(PermissionDenied):
            ev.update_event(s, ctx.ch_idr, own.id, None, common=dict(title="Chai", event_date=own.event_date),
                            centre_id=ctx.bpl.id)


def test_centre_head_cannot_approve_own_event(ctx):
    eid = _bpl_event(ctx, submit=True)
    with ctx.s() as s, pytest.raises(PermissionDenied):
        ev.transition(s, ctx.ch_bpl, eid, "approve")


def test_viewer_is_read_only_and_scoped(ctx):
    eid = _bpl_event(ctx)
    with ctx.s() as s:
        assert ev.get_event(s, ctx.viewer_bpl, eid).id == eid
        assert len(ev.list_events(s, ctx.viewer_all)) == 1
        with pytest.raises(PermissionDenied):
            ev.create_event(s, ctx.viewer_bpl, category_id=ctx.cats["batch_launch"].id, centre_id=ctx.bpl.id,
                            common=batch_common(), details=BATCH_DETAILS)
        with pytest.raises(PermissionDenied):
            ev.update_event(s, ctx.viewer_all, eid, None, common=batch_common())


def test_only_super_admin_manages_super_admins(ctx):
    with ctx.s() as s, pytest.raises(PermissionDenied):
        directory.create_user(s, ctx.ho, "sneaky", "Sneaky", Role.SUPER_ADMIN, password="Sup3r-sneaky")
    with ctx.s() as s, pytest.raises(PermissionDenied):
        directory.reset_password(s, ctx.ho, ctx.super.id)
    with ctx.s() as s, pytest.raises(PermissionDenied):
        directory.create_user(s, ctx.ch_bpl, "x.y", "X", Role.VIEWER, password="Valid-pass-1")


def test_exports_respect_scope_and_filters(ctx):
    _bpl_event(ctx)
    with ctx.s() as s:
        e, _ = ev.create_event(s, ctx.ch_idr, category_id=ctx.cats["informal_engagement"].id, centre_id=ctx.idr.id,
                               common=dict(title="Indore chai", event_date=batch_common()["event_date"]))
    d = batch_common()["event_date"]
    with ctx.s() as s:
        own = reports.build_report(s, ctx.ch_bpl, "calendar", EventFilter(), d.year, d.month)["Calendar"]
        assert set(own["Centre"]) == {"Bhopal"}
        allc = reports.build_report(s, ctx.ho, "calendar", EventFilter(), d.year, d.month)["Calendar"]
        assert set(allc["Centre"]) == {"Bhopal", "Indore"}
        filt = reports.build_report(s, ctx.ho, "calendar", EventFilter(category_ids=[ctx.cats["informal_engagement"].id]),
                                    d.year, d.month)["Calendar"]
        assert list(filt["Title"]) == ["Indore chai"]
        # a centre head asking for another centre's id gets nothing back
        sneaky = reports.build_report(s, ctx.ch_bpl, "calendar", EventFilter(centre_ids=[ctx.idr.id]), d.year, d.month)
        assert sneaky["Calendar"].empty
        assert reports.to_excel({"x": allc})[:2] == b"PK"


def test_audit_log_is_head_office_only(ctx):
    from jgevents.services import audit
    with ctx.s() as s:
        assert audit.list_logs(s, ctx.ho)
        with pytest.raises(PermissionDenied):
            audit.list_logs(s, ctx.ch_bpl)


def test_deactivated_centre_keeps_history(ctx):
    eid = _bpl_event(ctx)
    with ctx.s() as s:
        directory.update_centre(s, ctx.ho, ctx.bpl.id, is_active=False)
    with ctx.s() as s:
        assert s.get(Event, eid) is not None
        assert ev.get_event(s, ctx.ho, eid).centre.is_active is False
        with pytest.raises(ValidationError):
            ev.create_event(s, ctx.ho, category_id=ctx.cats["batch_launch"].id, centre_id=ctx.bpl.id,
                            common=batch_common(), details=BATCH_DETAILS)


def test_new_centre_added_without_code_change(ctx):
    with ctx.s() as s:
        c = directory.create_centre(s, ctx.ho, "Patna", "PAT")
        directory.update_centre(s, ctx.ho, c.id, name="Patna – Boring Road")
        u, _ = directory.create_user(s, ctx.ho, "ch.pat", "Patna Head", Role.CENTRE_HEAD,
                                     password="Patn@-2026x", centre_ids=[c.id])
        e, _ = ev.create_event(s, u, category_id=ctx.cats["batch_launch"].id, centre_id=c.id,
                               common=batch_common(), details=BATCH_DETAILS)
        assert e.centre.name == "Patna – Boring Road"
        with pytest.raises(ValidationError):
            directory.create_centre(s, ctx.ho, "Patna 2", "PAT")
    with ctx.s() as s:
        assert s.scalar(select(User).where(User.username == "ch.pat")).centres[0].code == "PAT"


def test_status_enum_complete():
    assert {s.value for s in Status} >= {"draft", "submitted", "approved", "in_progress", "completed",
                                          "returned", "rejected", "cancelled"}

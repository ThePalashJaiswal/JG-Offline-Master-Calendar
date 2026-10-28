"""Authorization rules. Every service call passes the acting user through these
checks; the UI only *mirrors* them. Nothing relies on a hidden button."""
from __future__ import annotations

from jgevents.db.models import Event, User
from jgevents.domain.constants import EDITOR_ROLES, HO_ROLES, Role
from jgevents.services.errors import PermissionDenied


def role(user: User) -> Role:
    return Role(user.role)


def is_ho(user: User) -> bool:
    return role(user) in HO_ROLES


def is_super(user: User) -> bool:
    return role(user) == Role.SUPER_ADMIN


def require_active(user: User) -> None:
    if user is None or not user.is_active:
        raise PermissionDenied("Your account is inactive.")


def require_ho(user: User) -> None:
    require_active(user)
    if not is_ho(user):
        raise PermissionDenied("Head Office permission required.")


def require_super(user: User) -> None:
    require_active(user)
    if not is_super(user):
        raise PermissionDenied("Super Admin permission required.")


def require_editor(user: User) -> None:
    require_active(user)
    if role(user) not in EDITOR_ROLES:
        raise PermissionDenied("Your role is read-only.")


def visible_centre_ids(user: User) -> set[int] | None:
    """None means 'all centres'."""
    if is_ho(user):
        return None
    if role(user) == Role.VIEWER and user.scope_all_centres:
        return None
    return {c.id for c in user.centres}


def can_view_centre(user: User, centre_id: int | None) -> bool:
    scope = visible_centre_ids(user)
    if scope is None:
        return True
    if centre_id is None:  # national parent events are visible to everyone
        return True
    return centre_id in scope


def can_manage_centre(user: User, centre_id: int | None) -> bool:
    if not user.is_active:
        return False
    if is_ho(user):
        return True
    if role(user) != Role.CENTRE_HEAD or centre_id is None:
        return False
    return centre_id in {c.id for c in user.centres}


def require_view_centre(user: User, centre_id: int | None) -> None:
    require_active(user)
    if not can_view_centre(user, centre_id):
        raise PermissionDenied("You do not have access to this centre.")


def require_manage_centre(user: User, centre_id: int | None) -> None:
    require_active(user)
    if not can_manage_centre(user, centre_id):
        raise PermissionDenied("You are not authorised to manage events for this centre.")


def require_view_event(user: User, ev: Event) -> None:
    require_view_centre(user, ev.centre_id)


def require_manage_event(user: User, ev: Event) -> None:
    if ev.is_national_parent:
        require_ho(user)
        return
    require_manage_centre(user, ev.centre_id)

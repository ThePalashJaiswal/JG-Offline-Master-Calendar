"""Centres and users administration."""
from __future__ import annotations

import re
import secrets
import string

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from jgevents.db.models import Centre, User
from jgevents.domain.constants import Role
from jgevents.services import audit
from jgevents.services.auth import hash_password, validate_password
from jgevents.services.errors import NotFound, PermissionDenied, ValidationError
from jgevents.services.permissions import is_super, require_ho, visible_centre_ids
from jgevents.utils.dates import now_utc

# ---------------------------------------------------------------- centres


def list_centres(session: Session, actor: User, include_inactive: bool = False) -> list[Centre]:
    q = select(Centre).order_by(Centre.sort_order, Centre.name)
    if not include_inactive:
        q = q.where(Centre.is_active.is_(True))
    scope = visible_centre_ids(actor)
    if scope is not None:
        q = q.where(Centre.id.in_(scope))
    return list(session.scalars(q))


def manageable_centres(session: Session, actor: User) -> list[Centre]:
    from jgevents.services.permissions import can_manage_centre
    return [c for c in list_centres(session, actor) if can_manage_centre(actor, c.id)]


def _clean_code(code: str) -> str:
    code = (code or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{2,10}", code):
        raise ValidationError("Centre code must be 2–10 letters/digits, e.g. BPL.")
    return code


def create_centre(session: Session, actor: User, name: str, code: str, city: str | None = None,
                  address: str | None = None, sort_order: int = 100) -> Centre:
    require_ho(actor)
    name = (name or "").strip()
    if not name:
        raise ValidationError("Centre name is required.")
    centre = Centre(name=name, code=_clean_code(code), city=(city or name).strip(), address=address,
                    sort_order=sort_order, is_active=True)
    try:
        with session.begin_nested():  # savepoint: a duplicate must not undo the caller's other work
            session.add(centre)
            session.flush()
    except IntegrityError as exc:
        raise ValidationError("A centre with this name or code already exists.") from exc
    audit.log(session, actor, "centre.created", "centre", centre.id, f"Centre {name} ({centre.code}) created",
              centre_id=centre.id)
    return centre


def update_centre(session: Session, actor: User, centre_id: int, **changes) -> Centre:
    require_ho(actor)
    centre = session.get(Centre, centre_id)
    if not centre:
        raise NotFound("Centre not found.")
    before = {k: getattr(centre, k) for k in changes}
    for key, value in changes.items():
        if key not in {"name", "code", "city", "address", "is_active", "sort_order"}:
            raise ValidationError(f"Unknown field {key}")
        if key == "code":
            value = _clean_code(value)
        if key == "name":
            value = (value or "").strip()
            if not value:
                raise ValidationError("Centre name is required.")
        setattr(centre, key, value)
    try:
        with session.begin_nested():  # savepoint: a duplicate must not undo the caller's other work
            session.flush()
    except IntegrityError as exc:
        raise ValidationError("A centre with this name or code already exists.") from exc
    verb = "deactivated" if changes.get("is_active") is False else (
        "reactivated" if changes.get("is_active") is True else "updated")
    audit.log(session, actor, f"centre.{verb}", "centre", centre.id, f"Centre {centre.name} {verb}",
              centre_id=centre.id, details={"before": before, "after": changes})
    return centre


# ---------------------------------------------------------------- users

def list_users(session: Session, actor: User, include_inactive: bool = True) -> list[User]:
    require_ho(actor)
    q = select(User).order_by(User.is_active.desc(), User.full_name)
    if not include_inactive:
        q = q.where(User.is_active.is_(True))
    return list(session.scalars(q))


def generate_temp_password(length: int = 14) -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length - 2)) + secrets.choice("!@#$%&*") + secrets.choice(string.digits)
        if any(c.isalpha() for c in pw):
            return pw


def _check_role_change_allowed(actor: User, target_role: str, existing: User | None = None) -> None:
    require_ho(actor)
    if target_role not in {r.value for r in Role}:
        raise ValidationError("Unknown role.")
    touching_super = target_role == Role.SUPER_ADMIN or (existing and existing.role == Role.SUPER_ADMIN)
    if touching_super and not is_super(actor):
        raise PermissionDenied("Only a Super Admin can create or modify Super Admin accounts.")


def _set_centres(session: Session, user: User, centre_ids: list[int]) -> None:
    centres = list(session.scalars(select(Centre).where(Centre.id.in_(centre_ids)))) if centre_ids else []
    if len(centres) != len(set(centre_ids or [])):
        raise ValidationError("One or more selected centres do not exist.")
    user.centres = centres


def create_user(session: Session, actor: User, username: str, full_name: str, role: str,
                password: str | None = None, email: str | None = None, phone: str | None = None,
                centre_ids: list[int] | None = None, scope_all_centres: bool = False) -> tuple[User, str]:
    """Returns (user, password). If no password is given a temporary one is generated;
    the user must change it on first sign-in."""
    _check_role_change_allowed(actor, role)
    username = (username or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9._-]{3,64}", username):
        raise ValidationError("Username must be 3–64 chars: lowercase letters, digits, '.', '_' or '-'.")
    if not (full_name or "").strip():
        raise ValidationError("Full name is required.")
    if role == Role.CENTRE_HEAD and not centre_ids:
        raise ValidationError("A Centre Head must be assigned at least one centre.")
    if session.scalar(select(func.count()).select_from(User).where(func.lower(User.username) == username)):
        raise ValidationError("Username already exists.")
    pw = password or generate_temp_password()
    validate_password(pw, username)
    user = User(username=username, full_name=full_name.strip(), role=role, email=email, phone=phone,
                password_hash=hash_password(pw), must_change_password=True, is_active=True,
                scope_all_centres=bool(scope_all_centres) and role == Role.VIEWER)
    session.add(user)
    _set_centres(session, user, centre_ids or [])
    session.flush()
    audit.log(session, actor, "user.created", "user", user.id, f"User {username} ({role}) created",
              details={"centres": centre_ids or []})
    return user, pw


def update_user(session: Session, actor: User, user_id: int, **changes) -> User:
    user = session.get(User, user_id)
    if not user:
        raise NotFound("User not found.")
    _check_role_change_allowed(actor, changes.get("role", user.role), user)
    if user.id == actor.id and (changes.get("is_active") is False or
                                ("role" in changes and changes["role"] != user.role)):
        raise ValidationError("You cannot deactivate yourself or change your own role.")
    before = {"role": user.role, "is_active": user.is_active, "centres": [c.id for c in user.centres]}
    for key in ("full_name", "email", "phone", "role", "is_active", "scope_all_centres"):
        if key in changes:
            setattr(user, key, changes[key])
    if "centre_ids" in changes:
        _set_centres(session, user, changes["centre_ids"] or [])
    if user.role == Role.CENTRE_HEAD and user.is_active and not user.centres:
        raise ValidationError("A Centre Head must be assigned at least one centre.")
    if user.role != Role.VIEWER:
        user.scope_all_centres = False
    session.flush()
    audit.log(session, actor, "user.updated", "user", user.id, f"User {user.username} updated",
              details={"before": before, "changes": {k: v for k, v in changes.items()}})
    return user


def reset_password(session: Session, actor: User, user_id: int) -> str:
    user = session.get(User, user_id)
    if not user:
        raise NotFound("User not found.")
    _check_role_change_allowed(actor, user.role, user)
    pw = generate_temp_password()
    user.password_hash = hash_password(pw)
    user.must_change_password = True
    user.failed_logins = 0
    user.locked_until = None
    user.password_changed_at = now_utc()  # signs out existing sessions
    audit.log(session, actor, "user.password_reset", "user", user.id, f"Password reset for {user.username}")
    return pw


def count_users(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(User)) or 0


def bootstrap_super_admin(session: Session, username: str, full_name: str, password: str,
                          email: str | None = None) -> User:
    """Create the very first Super Admin. Refuses if any user exists."""
    if count_users(session) > 0:
        raise PermissionDenied("Bootstrap is only allowed on an empty user table.")
    username = username.strip().lower()
    validate_password(password, username)
    user = User(username=username, full_name=full_name.strip(), role=Role.SUPER_ADMIN, email=email,
                password_hash=hash_password(password), must_change_password=False, is_active=True)
    session.add(user)
    session.flush()
    audit.log(session, user, "user.bootstrap", "user", user.id, f"First Super Admin {username} created")
    return user

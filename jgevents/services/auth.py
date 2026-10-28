from __future__ import annotations

from datetime import UTC, datetime, timedelta

import bcrypt
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jgevents.db.models import User
from jgevents.services import audit
from jgevents.services.errors import AuthError, ValidationError
from jgevents.utils.dates import now_utc

MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15
MIN_PASSWORD_LENGTH = 10


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def validate_password(password: str, username: str | None = None) -> None:
    errors = []
    if len(password) < MIN_PASSWORD_LENGTH:
        errors.append(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password.encode("utf-8")) > 72:
        errors.append("Password must be at most 72 bytes.")
    if password.isalpha() or password.isdigit():
        errors.append("Password must mix letters with numbers or symbols.")
    if username and username.lower() in password.lower():
        errors.append("Password must not contain the username.")
    if errors:
        raise ValidationError(errors)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


_DUMMY_HASH = hash_password("timing-equaliser-not-a-real-password")


def authenticate(session: Session, username: str, password: str) -> User:
    username = (username or "").strip().lower()
    user = session.scalar(select(User).where(func.lower(User.username) == username))
    generic = AuthError("Invalid username or password.")
    if user is None:
        verify_password(password, _DUMMY_HASH)  # keep response time similar
        raise generic
    now = now_utc()
    if user.locked_until and _aware(user.locked_until) > now:
        raise AuthError("Account temporarily locked after repeated failed attempts. Try again later.")
    if not verify_password(password, user.password_hash):
        user.failed_logins += 1
        if user.failed_logins >= MAX_FAILED_LOGINS:
            user.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
            user.failed_logins = 0
            audit.log(session, None, "user.locked", "user", user.id, f"{user.username} locked after failed logins")
        session.commit()  # the failure counter must survive the caller's rollback
        raise generic
    if not user.is_active:
        raise AuthError("This account has been deactivated.")
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = now
    audit.log(session, user, "user.login", "user", user.id, f"{user.username} signed in")
    return user


def change_password(session: Session, user: User, current: str, new: str) -> None:
    db_user = session.get(User, user.id)
    if not verify_password(current, db_user.password_hash):
        raise ValidationError("Current password is incorrect.")
    validate_password(new, db_user.username)
    if verify_password(new, db_user.password_hash):
        raise ValidationError("New password must differ from the current one.")
    db_user.password_hash = hash_password(new)
    db_user.must_change_password = False
    db_user.password_changed_at = now_utc()
    audit.log(session, db_user, "user.password_changed", "user", db_user.id, f"{db_user.username} changed password")


def load_active_user(session: Session, user_id: int, session_started: datetime | None = None) -> User | None:
    """Re-validate the signed-in user on every run (deactivation / password
    change take effect immediately)."""
    user = session.get(User, user_id)
    if user is None or not user.is_active:
        return None
    if session_started and user.password_changed_at and _aware(user.password_changed_at) > session_started:
        return None
    return user

"""Admin command line.

    python manage.py migrate                      # create/upgrade schema + seed reference data
    python manage.py create-superadmin            # interactive first Super Admin
    python manage.py seed-demo                    # demo users/events (NOT for production)
    python manage.py status                       # show DB revision and counts

Uses DATABASE_URL from the environment or .streamlit/secrets.toml.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from datetime import time, timedelta

from sqlalchemy import func, select

from jgevents.config import database_url
from jgevents.db.migrate import current_revision, upgrade_database
from jgevents.db.models import Centre, Event, User
from jgevents.db.session import session_scope


def cmd_migrate(url: str) -> None:
    upgrade_database(url)
    print(f"Database at revision {current_revision(url)}; reference data seeded.")


def cmd_create_superadmin(url: str) -> None:
    from jgevents.services.directory import bootstrap_super_admin, count_users
    upgrade_database(url)
    with session_scope(url) as s:
        if count_users(s):
            sys.exit("Users already exist. Create further admins from Admin → Users in the app.")
        username = input("Username: ").strip()
        full_name = input("Full name: ").strip()
        email = input("Email (optional): ").strip() or None
        pw = getpass.getpass("Password (min 10 chars, letters + digits/symbols): ")
        if pw != getpass.getpass("Repeat password: "):
            sys.exit("Passwords do not match.")
        bootstrap_super_admin(s, username, full_name, pw, email)
    print("Super Admin created. Sign in to the app with these credentials.")


def cmd_seed_demo(url: str) -> None:
    """Demo data for local evaluation. Prints the generated passwords."""
    from jgevents.domain.constants import Role
    from jgevents.services import config_service
    from jgevents.services import events as ev
    from jgevents.services.directory import bootstrap_super_admin, count_users, create_user
    from jgevents.utils.dates import add_months, today_ist

    upgrade_database(url)
    with session_scope(url) as s:
        if count_users(s) == 0:
            bootstrap_super_admin(s, "admin", "Demo Super Admin", "JgDemo@2026!")
            print("admin / JgDemo@2026!  (Super Admin)")
        admin = s.scalar(select(User).where(User.role == Role.SUPER_ADMIN))
        centres = list(s.scalars(select(Centre).order_by(Centre.sort_order)))
        if not s.scalar(select(User).where(User.username == "ho.admin")):
            _, pw = create_user(s, admin, "ho.admin", "Head Office Admin", Role.HO_ADMIN, password="HeadOffice@123")
            print(f"ho.admin / {pw}  (Head Office Admin)")
            _, pw = create_user(s, admin, "viewer", "Leadership Viewer", Role.VIEWER, password="Leader@12345",
                                scope_all_centres=True)
            print(f"viewer / {pw}  (Read-only, all centres)")
            for c in centres:
                uname = f"ch.{c.code.lower()}"
                _, pw = create_user(s, admin, uname, f"{c.name} Centre Head", Role.CENTRE_HEAD,
                                    password=f"Centre@{c.code}123", centre_ids=[c.id])
                print(f"{uname} / {pw}  (Centre Head – {c.name})")
            for u in s.scalars(select(User)):
                u.must_change_password = False
        if s.scalar(select(func.count()).select_from(Event)):
            print("Events already present; skipping demo events.")
            return
        cats = {c.key: c for c in config_service.list_categories(s)}
        today = today_ist()
        y, m = add_months(today.year, today.month, 1)
        for i, c in enumerate(centres[:4]):
            d = today + timedelta(days=3 + i)
            b, _ = ev.create_event(s, admin, category_id=cats["batch_launch"].id, centre_id=c.id, common=dict(
                title=f"Civil Judge Foundation – {c.name}", event_date=d, start_time=time(10), end_time=time(12),
                responsible_person=f"{c.name} Centre Head", expected_participation=40,
                target_audience="Graduates preparing for PCS-J", venue="Classroom 1"),
                details=dict(batch_name=f"CJ-F-{c.code}-01", subject="Civil Law", programme="Foundation",
                             faculty="Faculty TBC"), submit=True)
            ev.transition(s, admin, b.id, "approve", "Looks good")
            ev.create_event(s, admin, category_id=cats["whatsapp_campaign"].id, centre_id=c.id, common=dict(
                title=f"Launch announcement – {c.name}", event_date=d - timedelta(days=2), start_time=time(18),
                responsible_person="Counsellor", target_audience="Enquiries last 60 days",
                objective="Drive launch attendance"), details=dict(message_ref="Template LAUNCH-01"),
                linked_event_id=b.id, submit=True)
            ev.create_event(s, admin, category_id=cats["informal_engagement"].id, centre_id=c.id, common=dict(
                title="Chai Chai Par Charcha", subtype="Chai Chai Par Charcha", event_date=today + timedelta(days=9),
                start_time=time(16), end_time=time(17), responsible_person=f"{c.name} Centre Head",
                objective="Hear student concerns on test series"))
        from datetime import date as _d
        ev.create_national_event(s, admin, category_id=cats["open_mock"].id, common=dict(
            title="All India Open Mock – Civil Judge Prelims", event_date=_d(y, m, 15), start_time=time(10),
            end_time=time(12), responsible_person="Academic Head", target_audience="All PCS-J aspirants"),
            details=dict(programme="Civil Judge Prelims"), centre_ids=[c.id for c in centres])
    print("Demo events created.")


def cmd_status(url: str) -> None:
    print("Revision:", current_revision(url))
    with session_scope(url) as s:
        for model in (Centre, User, Event):
            print(f"{model.__tablename__}: {s.scalar(select(func.count()).select_from(model))}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["migrate", "create-superadmin", "seed-demo", "status"])
    p.add_argument("--url", help="Override DATABASE_URL")
    args = p.parse_args()
    url = args.url or database_url()
    {"migrate": cmd_migrate, "create-superadmin": cmd_create_superadmin, "seed-demo": cmd_seed_demo,
     "status": cmd_status}[args.command](url)


if __name__ == "__main__":
    main()

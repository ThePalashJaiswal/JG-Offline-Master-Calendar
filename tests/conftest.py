"""Test fixtures.

Every test gets a freshly migrated database. By default tests run on SQLite;
set TEST_DATABASE_URL to a *throwaway* PostgreSQL database to run the same
suite against Postgres (the database is wiped).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, time, timedelta

import pytest
from sqlalchemy import select, text

from jgevents.db.migrate import upgrade_database
from jgevents.db.models import Base, Centre, EventCategory, User
from jgevents.db.session import get_engine, session_scope
from jgevents.domain.constants import Role
from jgevents.services.directory import bootstrap_super_admin, create_user
from jgevents.utils.dates import add_months, today_ist

PG_URL = os.environ.get("TEST_DATABASE_URL")


def _reset(url: str) -> None:
    engine = get_engine(url)
    with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
        else:
            Base.metadata.drop_all(conn)
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))


@pytest.fixture
def db_url(tmp_path):
    url = PG_URL or f"sqlite:///{tmp_path / 'test.db'}"
    _reset(url)
    upgrade_database(url)
    yield url
    get_engine(url).dispose()


@dataclass
class Ctx:
    url: str
    super: User
    ho: User
    ch_bpl: User
    ch_idr: User
    viewer_bpl: User
    viewer_all: User
    bpl: Centre
    idr: Centre
    cats: dict[str, EventCategory]

    def s(self):
        return session_scope(self.url)


@pytest.fixture
def ctx(db_url) -> Ctx:
    with session_scope(db_url) as s:
        sup = bootstrap_super_admin(s, "root", "Root Admin", "Sup3r-secret!")
        centres = {c.code: c for c in s.scalars(select(Centre))}
        ho, _ = create_user(s, sup, "ho1", "HO One", Role.HO_ADMIN, password="HeadOff1ce!")
        ch_bpl, _ = create_user(s, sup, "ch.bpl", "Bhopal Head", Role.CENTRE_HEAD, password="Bhop@l-2026",
                                centre_ids=[centres["BPL"].id])
        ch_idr, _ = create_user(s, sup, "ch.idr", "Indore Head", Role.CENTRE_HEAD, password="Ind0re-2026",
                                centre_ids=[centres["IDR"].id])
        v1, _ = create_user(s, sup, "view.bpl", "Viewer BPL", Role.VIEWER, password="Look-0nly-1",
                            centre_ids=[centres["BPL"].id])
        v2, _ = create_user(s, sup, "view.all", "Viewer All", Role.VIEWER, password="Look-0nly-2",
                            scope_all_centres=True)
        cats = {c.key: c for c in s.scalars(select(EventCategory))}
        s.flush()
        return Ctx(db_url, sup, ho, ch_bpl, ch_idr, v1, v2, centres["BPL"], centres["IDR"], cats)


def next_month_date(day: int = 10) -> date:
    y, m = add_months(today_ist().year, today_ist().month, 1)
    return date(y, m, day)


def batch_common(d: date | None = None, start=time(10), end=time(12), **kw) -> dict:
    base = dict(title="Civil Judge Foundation launch", event_date=d or next_month_date(), start_time=start,
                end_time=end, responsible_person="Centre Head", expected_participation=40,
                target_audience="Graduates", venue="Room 1")
    base.update(kw)
    return base


BATCH_DETAILS = dict(batch_name="CJ-01", subject="Civil Law", programme="Foundation")


def past_date(days: int = 2) -> date:
    return today_ist() - timedelta(days=days)

"""Schema upgrade + reference seeding, safe to call on every app start.

On PostgreSQL a transaction-level advisory lock serialises concurrent starts
(Streamlit Cloud can boot more than one process)."""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from jgevents.db.seed import seed_reference_data
from jgevents.db.session import get_engine, session_scope

ROOT = Path(__file__).resolve().parents[2]
LOCK_KEY = 8_420_031  # arbitrary constant for pg_advisory_xact_lock


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["url"] = url
    cfg.attributes["skip_logging"] = True
    return cfg


def upgrade_database(url: str) -> None:
    engine = get_engine(url)
    with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK_KEY})
        cfg = alembic_config(url)
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
    with session_scope(url) as session:
        seed_reference_data(session)


def current_revision(url: str) -> str | None:
    from alembic.runtime.migration import MigrationContext
    with get_engine(url).connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()

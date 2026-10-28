from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect

from jgevents.db.migrate import alembic_config, current_revision, upgrade_database
from jgevents.db.models import Base
from jgevents.db.session import get_engine


def test_migrations_match_models(db_url):
    with get_engine(db_url).connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    assert diff == [], f"Models and migrations differ – create a new Alembic revision: {diff}"


def test_upgrade_is_idempotent_and_reversible(db_url):
    upgrade_database(db_url)  # second run is a no-op
    assert current_revision(db_url) == "0001"
    command.downgrade(alembic_config(db_url), "base")
    assert "events" not in inspect(get_engine(db_url)).get_table_names()
    upgrade_database(db_url)
    assert {"events", "centres", "monthly_plans", "audit_logs"} <= set(inspect(get_engine(db_url)).get_table_names())

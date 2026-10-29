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
    assert current_revision(db_url) == "0002"
    command.downgrade(alembic_config(db_url), "base")
    assert "events" not in inspect(get_engine(db_url)).get_table_names()
    upgrade_database(db_url)
    assert {"events", "centres", "monthly_plans", "audit_logs"} <= set(inspect(get_engine(db_url)).get_table_names())


def test_v1_database_upgrades_to_v2_without_data_loss(db_url):
    """A database holding v1 data (revision 0001) upgrades cleanly; events keep their data, get
    booked_late=False and no tasks; WhatsApp linking and checklists are updated as specified."""
    from sqlalchemy import text

    from jgevents.db.session import session_scope
    command.downgrade(alembic_config(db_url), "0001")
    with get_engine(db_url).begin() as conn:
        conn.execute(text("INSERT INTO events (category_id, centre_id, is_national_parent, is_proposed, title, "
                          "event_date, details, status, changed_after_approval, version, created_at, updated_at) "
                          "SELECT c.id, (SELECT id FROM centres ORDER BY id LIMIT 1), false, false, 'Old v1 launch', "
                          "'2026-09-01', '{}', 'completed', false, 1, '2026-08-01', '2026-08-01' "
                          "FROM event_categories c WHERE c.key = 'batch_launch'"))
    upgrade_database(db_url)
    assert current_revision(db_url) == "0002"
    with session_scope(db_url) as s:
        from jgevents.db.models import Event, EventCategory, EventTask, Playbook
        ev = s.query(Event).filter_by(title="Old v1 launch").one()
        assert ev.booked_late is False and ev.status == "completed"
        assert s.query(EventTask).count() == 0
        wa = s.query(EventCategory).filter_by(key="whatsapp_campaign").one()
        assert wa.link_target_key == "batch_launch,open_mock"
        assert s.query(EventCategory).filter_by(key="batch_launch").one().default_checklist == []
        assert s.query(Playbook).count() == 3

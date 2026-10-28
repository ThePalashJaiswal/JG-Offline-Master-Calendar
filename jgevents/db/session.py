from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker


@lru_cache(maxsize=8)
def get_engine(url: str) -> Engine:
    kwargs: dict = {"future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        # Managed Postgres (Supabase pooler / Neon) drops idle connections.
        kwargs.update(pool_pre_ping=True, pool_size=5, max_overflow=5, pool_recycle=300)
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        # pysqlite's implicit transactions break SAVEPOINTs; take over BEGIN ourselves
        # (pattern from the SQLAlchemy SQLite dialect docs) and enforce foreign keys.
        @event.listens_for(engine, "connect")
        def _on_connect(dbapi_conn, _):
            dbapi_conn.isolation_level = None
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        @event.listens_for(engine, "begin")
        def _on_begin(conn):
            conn.exec_driver_sql("BEGIN")
    return engine


@lru_cache(maxsize=8)
def get_sessionmaker(url: str) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(url), expire_on_commit=False, future=True)


@contextmanager
def session_scope(url: str) -> Iterator[Session]:
    """One unit of work: commit on success, roll back on any error."""
    session = get_sessionmaker(url)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

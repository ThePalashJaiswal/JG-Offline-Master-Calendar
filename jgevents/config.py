"""Configuration: Streamlit secrets first, then environment variables.

Required in production:
    DATABASE_URL   postgresql+psycopg://user:pass@host:5432/dbname?sslmode=require
Optional:
    BOOTSTRAP_TOKEN  enables the one-time "create first Super Admin" screen
    APP_ENV          "production" (default) or "development"
"""
from __future__ import annotations

import os
from functools import lru_cache


def _from_streamlit_secrets(key: str) -> str | None:
    try:
        import streamlit as st

        if key in st.secrets:
            return str(st.secrets[key])
    except Exception:  # no secrets file / not running under streamlit
        return None
    return None


def get_setting(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key) or _from_streamlit_secrets(key) or default


def normalise_db_url(url: str) -> str:
    """Accept the plain postgres:// URLs that Supabase/Neon hand out."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


@lru_cache(maxsize=1)
def database_url() -> str:
    url = get_setting("DATABASE_URL")
    if not url:
        env = (get_setting("APP_ENV", "production") or "").lower()
        if env == "development":
            return "sqlite:///./jg_events_dev.db"
        raise RuntimeError(
            "DATABASE_URL is not configured. Add it to .streamlit/secrets.toml or the environment "
            "(set APP_ENV=development to use a local SQLite file instead)."
        )
    return normalise_db_url(url)

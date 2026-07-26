# app/database.py
"""
Database layer for Mistrio.

We talk to Supabase PostgreSQL directly with SQLAlchemy Core.
No ORM models are required — the schema already lives in Supabase and we
keep the backend thin and explicit.

Usage in a router:

    from app.database import db

    rows = db.fetch_all("select * from services where is_active = true")
    row  = db.fetch_one("select * from users where id = :id", {"id": 5})
    db.execute("update users set name = :n where id = :id", {"n": "Vikash", "id": 5})
"""

import logging
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.config import settings

logger = logging.getLogger(__name__)


def _build_engine() -> Engine:
    url = settings.DATABASE_URL
    # Railway/Supabase sometimes hand out the old "postgres://" prefix
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)

    return create_engine(
        url,
        pool_pre_ping=True,      # drop dead connections instead of erroring
        pool_size=5,
        max_overflow=10,
        pool_recycle=1800,
        echo=settings.DEBUG,
        connect_args={"options": "-c timezone=Asia/Kolkata"},
    )


engine: Engine = _build_engine()


class Database:
    """Thin helper around SQLAlchemy Core."""

    def __init__(self, eng: Engine):
        self.engine = eng

    # ---------- reads ----------
    def fetch_all(self, sql: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        with self.engine.connect() as conn:
            result = conn.execute(text(sql), params or {})
            return [dict(r) for r in result.mappings().all()]

    def fetch_one(self, sql: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        with self.engine.connect() as conn:
            result = conn.execute(text(sql), params or {})
            row = result.mappings().first()
            return dict(row) if row else None

    def fetch_value(self, sql: str, params: Optional[Dict[str, Any]] = None) -> Any:
        with self.engine.connect() as conn:
            result = conn.execute(text(sql), params or {})
            row = result.first()
            return row[0] if row else None

    # ---------- writes ----------
    def execute(self, sql: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """
        Runs a write statement and commits.
        Add "returning *" to your SQL to get the affected row back.
        """
        with self.engine.begin() as conn:
            result = conn.execute(text(sql), params or {})
            if result.returns_rows:
                row = result.mappings().first()
                return dict(row) if row else None
            return None

    def execute_many(self, sql: str, params_list: List[Dict[str, Any]]) -> None:
        with self.engine.begin() as conn:
            conn.execute(text(sql), params_list)

    # ---------- transactions ----------
    @contextmanager
    def transaction(self):
        """
        Use when several writes must succeed or fail together.

            with db.transaction() as conn:
                conn.execute(text("..."), {...})
                conn.execute(text("..."), {...})
        """
        with self.engine.begin() as conn:
            yield conn

    # ---------- health ----------
    def ping(self) -> bool:
        try:
            with self.engine.connect() as conn:
                conn.execute(text("select 1"))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Database ping failed: %s", exc)
            return False


db = Database(engine)

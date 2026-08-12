"""Migration: add `leads.email_message_id` (VARCHAR(255) UNIQUE + index).

Adds the dedup key that the email → lead ingest pipeline uses to make ingest
idempotent. Idempotent itself — safe to run twice; checks column existence
first via a raw SELECT trick that works on both SQLite and PostgreSQL.

Usage:
    /var/www/procam-crm/.venv/bin/python \\
        /var/www/procam-crm/scripts/2026_08_12_email_ingest_migration.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from app import app, db  # noqa: E402


COLUMN_NAME = "email_message_id"
TABLE_NAME = "leads"
INDEX_NAME = "ix_leads_email_message_id"
UNIQUE_NAME = "uq_leads_email_message_id"


def column_exists() -> bool:
    """Cross-DB check: try a `SELECT <col> ... LIMIT 0`; failure ⇒ missing."""
    try:
        db.session.execute(text(f"SELECT {COLUMN_NAME} FROM {TABLE_NAME} LIMIT 0"))
        return True
    except Exception:
        db.session.rollback()
        return False


def index_exists(name: str) -> bool:
    dialect = db.engine.dialect.name
    try:
        if dialect == "postgresql":
            row = db.session.execute(
                text("SELECT 1 FROM pg_indexes WHERE indexname = :n"),
                {"n": name},
            ).first()
            return row is not None
        if dialect == "sqlite":
            row = db.session.execute(
                text("SELECT 1 FROM sqlite_master WHERE type='index' AND name = :n"),
                {"n": name},
            ).first()
            return row is not None
    except Exception:
        db.session.rollback()
    return False


def main() -> int:
    with app.app_context():
        dialect = db.engine.dialect.name
        print(f"[migration] dialect={dialect} table={TABLE_NAME} column={COLUMN_NAME}")

        if column_exists():
            print(f"[migration] column {TABLE_NAME}.{COLUMN_NAME} already exists — skipping ADD")
        else:
            print(f"[migration] adding column {TABLE_NAME}.{COLUMN_NAME} ...")
            # SQLite doesn't support adding UNIQUE constraint via ALTER TABLE,
            # so we add plain column, then unique index below (which also
            # enforces uniqueness).
            db.session.execute(text(
                f"ALTER TABLE {TABLE_NAME} ADD COLUMN {COLUMN_NAME} VARCHAR(255)"
            ))
            db.session.commit()
            print(f"[migration] added {TABLE_NAME}.{COLUMN_NAME}")

        # Add UNIQUE index (also serves as the query-speed index)
        if index_exists(INDEX_NAME) or index_exists(UNIQUE_NAME):
            print(f"[migration] index on {COLUMN_NAME} already exists — skipping")
        else:
            print(f"[migration] creating unique index {INDEX_NAME} ...")
            db.session.execute(text(
                f"CREATE UNIQUE INDEX {INDEX_NAME} "
                f"ON {TABLE_NAME} ({COLUMN_NAME})"
            ))
            db.session.commit()
            print(f"[migration] created {INDEX_NAME}")

        print("[migration] done")
        return 0


if __name__ == "__main__":
    sys.exit(main())

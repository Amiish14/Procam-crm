"""Create lead_attachments table for storing files attached to Leads.

Idempotent — safe to re-run. Creates only the LeadAttachment table (leaves
the rest of the schema alone) via SQLAlchemy metadata so we get the right
DDL for whichever backend (SQLite in dev, Postgres in prod) is in use.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, db, LeadAttachment
from sqlalchemy import inspect


def main():
    with app.app_context():
        insp = inspect(db.engine)
        if insp.has_table("lead_attachments"):
            print("[migration] lead_attachments already exists — skipping")
            return
        dialect = db.engine.dialect.name
        print(f"[migration] dialect={dialect}  creating lead_attachments ...")
        LeadAttachment.__table__.create(db.engine)
        print("[migration] created lead_attachments")


if __name__ == "__main__":
    main()

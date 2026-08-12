"""Add Lead.email_extracted_json TEXT column. Idempotent."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import app, db
from sqlalchemy import text, inspect


def column_exists(table: str, column: str) -> bool:
    insp = inspect(db.engine)
    return column in {c["name"] for c in insp.get_columns(table)}


def main():
    with app.app_context():
        if column_exists("leads", "email_extracted_json"):
            print("[migration] leads.email_extracted_json already exists — skipping")
            return
        dialect = db.engine.dialect.name
        print(f"[migration] dialect={dialect}  adding leads.email_extracted_json ...")
        db.session.execute(text("ALTER TABLE leads ADD COLUMN email_extracted_json TEXT"))
        db.session.commit()
        print("[migration] added leads.email_extracted_json")


if __name__ == "__main__":
    main()
